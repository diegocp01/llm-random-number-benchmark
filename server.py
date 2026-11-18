#!/usr/bin/env python3
"""Random-number benchmark: local server, scheduler and live stats.

    python server.py            real run (needs keys in .env)
    python server.py --demo     simulated answers, separate data folder

Every answer is appended to <data>/results.jsonl the moment it arrives, so the
run can be paused, closed and resumed at any point.
"""
import argparse
import csv
import io
import json
import os
import random
import secrets
import sys
import threading
import time
import traceback
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import providers
import stats

ROOT = os.path.dirname(os.path.abspath(__file__))
KEY_NAMES = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}
PROVIDER_NAMES = {"openai": "OpenAI", "anthropic": "Anthropic", "local": "Python baseline"}


def read_env_file():
    out = {}
    path = os.path.join(ROOT, ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, value = line.split("=", 1)
                out[name.strip()] = value.strip().strip('"').strip("'")
    return out


def get_key(provider):
    name = KEY_NAMES[provider]
    return read_env_file().get(name) or os.environ.get(name) or ""


class Cell:
    """One (model, effort, range, prompt) combination."""

    def __init__(self, order, model, effort, k, prompt_id, target):
        self.order = order
        self.provider = model["provider"]
        self.family = model["family"]
        self.model = model["id"]
        self.label = model.get("label", model["id"])
        self.price_in = model.get("price_in", 0.0)
        self.price_out = model.get("price_out", 0.0)
        self.control = bool(model.get("control"))      # local random generator, not an LLM
        self.generator = model.get("generator")
        self.effort = effort
        self.k = k
        self.prompt_id = prompt_id
        self.target = target
        self.done = 0          # answers recorded (valid or not)
        self.inflight = 0
        self.counts = {}       # number -> count, valid answers only
        self.invalid = 0       # replies with no usable number (left out)
        self.extra = 0         # replies with a number plus other text (counted, flagged)
        self.cost = 0.0
        self.out_tok = 0
        self.rtok = 0          # reasoning tokens
        self.lats = []         # seconds from request to full reply, one per call
        self.disabled = None   # reason string when skipped

    @property
    def key(self):
        return (self.provider, self.model, self.effort, self.k, self.prompt_id)


class Bench:
    def __init__(self, cfg, demo):
        self.cfg = cfg
        self.demo = demo
        self.lock = threading.RLock()
        self.data_dir = os.path.join(ROOT, "data_demo" if demo else "data")
        os.makedirs(self.data_dir, exist_ok=True)
        self.results_path = os.path.join(self.data_dir, "results.jsonl")
        self.errors_path = os.path.join(self.data_dir, "errors.log")

        self.cells = []
        self.by_key = {}
        order = 0
        for model in cfg["models"]:
            for effort in model.get("efforts", cfg["efforts"]):
                for k in cfg["ranges"]:
                    prompt_ids = ["main"]
                    if effort == cfg.get("paraphrase_effort"):
                        prompt_ids += [p for p in cfg["prompts"] if p != "main"]
                    for pid in prompt_ids:
                        target = cfg["samples_per_cell"] if pid == "main" else cfg["paraphrase_samples"]
                        cell = Cell(order, model, effort, k, pid, target)
                        order += 1
                        self.cells.append(cell)
                        self.by_key[cell.key] = cell

        self.providers = {}
        for pid in dict.fromkeys(m["provider"] for m in cfg["models"]):
            self.providers[pid] = {
                "spent": 0.0, "spent_by": {k: 0.0 for k in cfg["ranges"]}, "state": "idle",
                "cap": float("inf") if pid == "local" else float(cfg["budget_usd"].get(pid, 0)),
                "stopped": True, "inflight": 0, "backoff_until": 0.0, "streak": 0, "max_cost": 0.0,
            }

        # One test per range. Each is started by hand; only one runs at a time.
        self.tests = {k: {"status": "idle", "message": ""} for k in cfg["ranges"]}
        self.active = None
        self.stop_flag = False
        self.version = 0
        self.recent = deque(maxlen=40)
        self.issues = deque(maxlen=12)
        self.done_times = deque(maxlen=6000)
        self.run_started = 0.0
        self._views = (-1, None)
        self._rngs = {}
        # Round = one more answer from every model at every effort (the "epoch").
        self.bad = {k: deque(maxlen=8) for k in cfg["ranges"]}   # latest replies that were not number-only
        self.history = {k: [] for k in cfg["ranges"]}
        self._round_seen = {k: 0 for k in cfg["ranges"]}
        self._load()
        self._out = open(self.results_path, "a", encoding="utf-8")
        self._detail = {}
        self._detail_empty = {}
        for k in cfg["ranges"]:
            self.tests[k]["status"] = self._rest_status(k)

    # ------------------------------------------------------------ persistence

    def _load(self):
        if not os.path.exists(self.results_path):
            return
        with open(self.results_path, encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue  # a half-written last line after a crash
                p = self.providers.get(row.get("provider"))
                if p:
                    p["spent"] += row.get("cost", 0.0)
                    if row.get("range") in p["spent_by"]:
                        p["spent_by"][row["range"]] += row.get("cost", 0.0)
                cell = self.by_key.get((row.get("provider"), row.get("model"), row.get("effort"),
                                        row.get("range"), row.get("prompt_id")))
                if cell:
                    self._apply(cell, row)

    def _apply(self, cell, row):
        cell.done += 1
        cell.cost += row.get("cost", 0.0)
        cell.out_tok += row.get("out_tok", 0)
        cell.rtok += row.get("reason_tok") or 0
        cell.lats.append(row.get("latency_s") or 0.0)
        if row.get("valid"):
            n = row["number"]
            cell.counts[n] = cell.counts.get(n, 0) + 1
        else:
            cell.invalid += 1
        if not cell.control and row.get("reason") != "ok":
            if row.get("valid"):
                cell.extra += 1
            self.bad[cell.k].appendleft({"label": cell.label, "effort": cell.effort,
                                         "text": str(row.get("output", ""))[:80], "kind": row.get("reason")})
        if not cell.control and cell.prompt_id == "main":
            self._tick(cell.k)

    def _round(self, k):
        """Rounds completed in test k: the answer count of its slowest cell.
        Providers that have not run and skipped models do not hold it back."""
        cells = [c for c in self.cells if c.k == k and not c.control and c.prompt_id == "main"]
        top = {}
        for c in cells:
            top[c.provider] = max(top.get(c.provider, 0), c.done)
        live = [min(c.done, c.target) for c in cells
                if top[c.provider] > 0 and not (c.done == 0 and top[c.provider] > 3)]
        return min(live) if live else 0

    def _tick(self, k):
        """Log one line of metrics each time a round completes, like a training epoch."""
        r = self._round(k)
        if r <= self._round_seen[k]:
            return
        self._round_seen[k] = r
        size = max(1, -(-k // 100))
        fam_counts, model_counts = {}, {}
        calls, spent = 0, 0.0
        for c in self.cells:
            if c.k != k or c.control:
                continue
            calls += c.done
            spent += c.cost
            if c.prompt_id != "main":
                continue
            fc = fam_counts.setdefault(c.family, {})
            mc = model_counts.setdefault((c.family, c.model), {})
            for v, n in c.counts.items():
                fc[v] = fc.get(v, 0) + n
                b = (v - 1) // size
                mc[b] = mc.get(b, 0) + n
        within, between = [], []
        keys = [m for m, cnt in model_counts.items() if sum(cnt.values()) >= 5]
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                d = stats.jsd(model_counts[keys[i]], model_counts[keys[j]])
                (within if keys[i][0] == keys[j][0] else between).append(d)
        mean = lambda xs: sum(xs) / len(xs) if xs else None
        self.history[k].append({
            "round": r, "calls": calls, "spent": spent,
            "spread": {f: stats.cell_stats(cnt, k).get("entropy") for f, cnt in fam_counts.items()},
            "within": mean(within), "between": mean(between),
        })

    def _log_error(self, text):
        with open(self.errors_path, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text + "\n")

    def _issue(self, text):
        if text not in self.issues:
            self.issues.append(text)
        self._log_error(text)

    # ------------------------------------------------------------ per-test call log

    def detail_path(self, k):
        return os.path.join(self.data_dir, "test_1-%d.json" % k)

    def _append_detail(self, k, record):
        """Append one call to that test's JSON file, keeping it a valid JSON array
        after every write: one record per line between "[" and "]"."""
        data = json.dumps(record, ensure_ascii=False).encode("utf-8")
        f = self._detail.get(k)
        if f is None:
            path = self.detail_path(k)
            lines = []
            if os.path.exists(path):
                with open(path, "rb") as old:
                    for line in old.read().split(b"\n"):
                        line = line.strip().rstrip(b",")
                        if line.startswith(b"{"):
                            try:
                                json.loads(line)
                                lines.append(line)
                            except ValueError:
                                pass  # a half-written record after a crash
            f = open(path, "w+b")
            f.write(b"[\n" + b",\n".join(lines) + (b"\n" if lines else b"") + b"]\n")
            self._detail[k] = f
            self._detail_empty[k] = not lines
        if self._detail_empty[k]:
            self._detail_empty[k] = False
            f.seek(2)
            f.write(data + b"\n]\n")
        else:
            f.seek(-3, os.SEEK_END)          # step back over "\n]\n"
            f.write(b",\n" + data + b"\n]\n")
        f.flush()

    # ------------------------------------------------------------ control

    def _remaining(self, k, pid=None, api_only=False):
        return sum(max(0, c.target - c.done) for c in self.cells
                   if c.k == k and (pid is None or c.provider == pid) and not (api_only and c.control))

    def _rest_status(self, k):
        if self._remaining(k) == 0:
            return "done"
        return "paused" if any(c.done for c in self.cells if c.k == k and not c.control) else "idle"

    def _limit(self, pid, k):
        """How far this provider's total spend may go while test k runs: the cap,
        minus an equal share held back for each other test that is not finished."""
        p = self.providers[pid]
        if pid == "local":
            return float("inf")
        share = p["cap"] / len(self.cfg["ranges"])
        held = sum(max(0.0, share - p["spent_by"][o]) for o in self.cfg["ranges"]
                   if o != k and self._remaining(o, pid) > 0)
        return p["cap"] - held

    def start(self, k):
        with self.lock:
            if self.active is not None or k not in self.tests:
                return
            test = self.tests[k]
            for c in self.cells:
                c.disabled = None
            self.issues.clear()
            test["message"] = ""
            self.stop_flag = False
            threads = []
            for pid, p in self.providers.items():
                p.update(stopped=True, backoff_until=0.0, streak=0, inflight=0)
                if self._remaining(k, pid) == 0:
                    p["state"] = "done"
                    continue
                if p["spent"] >= self._limit(pid, k):
                    p["state"] = "budget reached"
                    continue
                client = None
                if not self.demo and pid != "local":
                    key = get_key(pid)
                    if not key:
                        p["state"] = "no key"
                        continue
                    try:
                        client = providers.make_openai(key) if pid == "openai" else providers.make_anthropic(key)
                    except ImportError:
                        p["state"] = "error: SDK not installed, start with the .command file"
                        continue
                p["state"] = "running"
                p["stopped"] = False
                # one thread for the local baseline keeps its seeded sequence in order
                n = 1 if pid == "local" else 8 if self.demo else int(self.cfg["concurrency"].get(pid, 4))
                for _ in range(n):
                    threads.append(threading.Thread(target=self._worker, args=(pid, client), daemon=True))
            missing = [PROVIDER_NAMES.get(pid, pid) for pid, p in self.providers.items() if p["state"] == "no key"]
            if missing:
                test["message"] = "Add your API keys to the .env file, then press Run. Missing: " + ", ".join(missing)
            self.version += 1
            if not threads:
                test["status"] = self._rest_status(k)
                return
            self.active = k
            test["status"] = "running"
            self.run_started = time.time()
            self.done_times.clear()
        for t in threads:
            t.start()
        threading.Thread(target=self._monitor, args=(threads, k), daemon=True).start()

    def pause(self):
        with self.lock:
            if self.active is not None and self.tests[self.active]["status"] == "running":
                self.stop_flag = True
                self.tests[self.active]["status"] = "pausing"
                self.version += 1

    def _monitor(self, threads, k):
        for t in threads:
            t.join()
        with self.lock:
            test = self.tests[k]
            for pid, p in self.providers.items():
                if p["state"] in ("running", "rate limited, retrying"):
                    p["state"] = "paused"
            test["status"] = self._rest_status(k)
            if test["status"] != "done" and not self.stop_flag and not test["message"]:
                waiting = [PROVIDER_NAMES.get(pid, pid) + ": " + p["state"] for pid, p in self.providers.items()
                           if p["state"] not in ("done", "paused")]
                if waiting:
                    test["message"] = "Stopped before finishing. " + "; ".join(waiting)
            self.active = None
            self.version += 1

    # ------------------------------------------------------------ scheduling

    def _pick(self, pid):
        """Next cell for this provider: always the one with the fewest answers,
        so every cell advances together and a budget stop leaves a balanced design."""
        p = self.providers[pid]
        with self.lock:
            k = self.active
            if self.stop_flag or p["stopped"] or k is None:
                return None, 0
            now = time.time()
            if now < p["backoff_until"]:
                return None, min(1.0, p["backoff_until"] - now)
            if p["spent"] + p["inflight"] * p["max_cost"] >= self._limit(pid, k):
                if p["inflight"] == 0:
                    p["state"] = "budget reached"
                    p["stopped"] = True
                    return None, 0
                return None, 0.5
            best = None
            for c in self.cells:
                if c.provider != pid or c.k != k or c.disabled or c.done + c.inflight >= c.target:
                    continue
                if best is None or c.done + c.inflight < best.done + best.inflight:
                    best = c
            if best is None:
                if p["inflight"] == 0:
                    skipped = any(c.disabled for c in self.cells if c.provider == pid and c.k == k)
                    p["state"] = "done, some skipped" if skipped else "done"
                    p["stopped"] = True
                    return None, 0
                return None, 0.3
            best.inflight += 1
            p["inflight"] += 1
            return best, 0

    def _release(self, cell):
        cell.inflight -= 1
        self.providers[cell.provider]["inflight"] -= 1

    def _worker(self, pid, client):
        p = self.providers[pid]
        rng = random.Random()
        while True:
            cell, wait = self._pick(pid)
            if cell is None:
                if wait:
                    time.sleep(wait)
                    continue
                return
            prompt = self.cfg["prompts"][cell.prompt_id].format(max=cell.k)
            started = time.time()
            try:
                if pid == "local":
                    res = self._local_draw(cell)
                elif self.demo:
                    res = providers.call_demo(cell.family, cell.model, cell.effort, cell.k, rng)
                elif pid == "openai":
                    res = providers.call_openai(client, cell.model, cell.effort, prompt, self.cfg["max_output_tokens"])
                else:
                    res = providers.call_anthropic(client, cell.model, cell.effort, prompt, self.cfg["max_output_tokens"])
            except providers.Fatal as e:
                with self.lock:
                    self._release(cell)
                    p["state"] = "error: " + str(e)
                    p["stopped"] = True
                    self._issue("%s stopped: %s" % (PROVIDER_NAMES.get(pid, pid), e))
                    self.version += 1
                continue
            except providers.CellError as e:
                with self.lock:
                    self._release(cell)
                    for c in self.cells:
                        if c.provider == pid and c.model == cell.model and (e.scope == "model" or c.effort == cell.effort):
                            c.disabled = str(e)
                    where = cell.label if e.scope == "model" else "%s · %s" % (cell.label, cell.effort)
                    self._issue("Skipped %s: %s" % (where, e))
                    self.version += 1
                continue
            except Exception as e:  # Transient, or anything unexpected: back off and retry
                with self.lock:
                    self._release(cell)
                    p["streak"] += 1
                    if not isinstance(e, providers.Transient):
                        self._log_error("unexpected error on %s\n%s" % (cell.model, traceback.format_exc()))
                        if p["streak"] > 8:
                            p["state"] = "error: " + str(e)[:160]
                            p["stopped"] = True
                            self._issue("%s stopped after repeated errors: %s" % (PROVIDER_NAMES.get(pid, pid), str(e)[:160]))
                            self.version += 1
                            continue
                    delay = getattr(e, "retry_after", None) or min(60.0, 2.0 ** min(p["streak"], 6))
                    p["backoff_until"] = time.time() + delay + rng.uniform(0, 0.5)
                    p["state"] = "rate limited, retrying"
                    self.version += 1
                continue
            self._record(cell, res, time.time() - started, res.get("input", prompt))

    def _local_draw(self, cell):
        """Control condition: a real random generator answering the same question.

        mt19937 is Python's `random` module, seeded per cell so the run is
        reproducible (on resume the generator is replayed past the saved draws).
        os is the `secrets` module, i.e. operating-system entropy, not reproducible.
        """
        if cell.generator == "os":
            n = secrets.randbelow(cell.k) + 1
            call = "secrets.randbelow(%d) + 1" % cell.k
        else:
            call = "random.Random(seed).randint(1, %d)" % cell.k
            rng = self._rngs.get(cell.key)
            if rng is None:
                rng = random.Random("%s|%s|%s|%d" % (self.cfg.get("baseline_seed", 0), cell.model, cell.effort, cell.k))
                for _ in range(cell.done):
                    rng.randint(1, cell.k)
                self._rngs[cell.key] = rng
            n = rng.randint(1, cell.k)
        return {"text": str(n), "input": call, "reasoning": None, "reasoning_note": "not a language model: no reasoning",
                "in_tok": 0, "out_tok": 0, "reason_tok": None, "usage": None, "served_model": cell.model, "stop": "local"}

    def _record(self, cell, res, latency, prompt):
        number, reason = providers.parse_answer(res["text"], cell.k)
        stop = str(res["stop"])
        if (number is None and stop in ("refusal", "max_tokens")) or stop.startswith("incomplete"):
            reason = stop  # refused or cut off: never count a partial answer
            number = None
        cost = (res["in_tok"] * cell.price_in + res["out_tok"] * cell.price_out) / 1e6
        now = time.time()
        # results.jsonl holds the result of every call, across all tests
        row = {
            "ts": round(now, 3), "provider": cell.provider, "family": cell.family, "model": cell.model,
            "effort": cell.effort, "range": cell.k, "prompt_id": cell.prompt_id,
            "number": number, "valid": number is not None, "number_only": reason == "ok", "reason": reason,
            "in_tok": res["in_tok"], "out_tok": res["out_tok"], "reason_tok": res["reason_tok"],
            "cost": cost, "latency_s": round(latency, 3),
        }
        if reason != "ok":
            row["output"] = res["text"][:200]   # keep what was actually said when the check fails
        # test_1-K.json holds the full call: what was sent, the reasoning, what came back
        detail = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
            "test": "1-%d" % cell.k,
            "provider": cell.provider, "family": cell.family,
            "model": cell.model, "served_model": res["served_model"],
            "reasoning_effort": None if cell.control else cell.effort,
            "run": cell.effort if cell.control else None,
            "prompt_id": cell.prompt_id,
            "input": prompt,
            "reasoning": res.get("reasoning"),
            "reasoning_tokens": res.get("reason_tok"),
            "reasoning_note": res.get("reasoning_note"),
            "output": res["text"],
            "number": number, "valid": number is not None, "number_only": reason == "ok", "reason": reason,
            "usage": res.get("usage"), "cost_usd": cost, "latency_s": round(latency, 3), "stop": stop,
        }
        with self.lock:
            p = self.providers[cell.provider]
            self._append_detail(cell.k, detail)
            self._out.write(json.dumps(row) + "\n")
            self._out.flush()
            self._release(cell)
            self._apply(cell, row)
            p["spent"] += cost
            p["spent_by"][cell.k] += cost
            p["max_cost"] = max(p["max_cost"], cost)
            p["streak"] = 0
            if p["state"] == "rate limited, retrying":
                p["state"] = "running"
            if not cell.control:
                self.done_times.append(now)
            if cell.prompt_id == "main" and not cell.control:
                self.recent.appendleft({"t": row["ts"], "label": cell.label, "effort": cell.effort, "k": cell.k,
                                        "text": str(number) if number is not None else res["text"].strip()[:40],
                                        "valid": row["valid"]})
            self.version += 1

    # ------------------------------------------------------------ reporting

    def _projected(self, pid, k):
        """Spend on test k so far plus its remaining calls at each cell's own average
        cost (falling back to the same model and effort in other tests)."""
        cells = [c for c in self.cells if c.provider == pid]
        done = sum(c.done for c in cells)
        if not done:
            return None
        fallback = sum(c.cost for c in cells) / done
        by_model = {}
        for c in cells:
            agg = by_model.setdefault((c.model, c.effort), [0.0, 0])
            agg[0] += c.cost
            agg[1] += c.done
        total = self.providers[pid]["spent_by"][k]
        for c in cells:
            if c.disabled or c.k != k:
                continue
            cost, n = by_model[(c.model, c.effort)]
            avg = c.cost / c.done if c.done else (cost / n if n else fallback)
            total += max(0, c.target - c.done) * avg
        return total

    def _row(self, counts, k, done=None, target=None, invalid=0, disabled=None):
        bins, _ = stats.to_bins(counts, k)
        return {"bins": bins, "stats": stats.cell_stats(counts, k), "done": done, "target": target,
                "invalid": invalid, "disabled": disabled}

    def _build_views(self):
        views = {}
        main = [c for c in self.cells if c.prompt_id == "main"]
        for k in self.cfg["ranges"]:
            mean, sd = stats.uniform_mean_sd(k)
            size = max(1, -(-k // 100))
            families, pooled_by_model, model_meta = [], [], []
            for fam in dict.fromkeys(m["family"] for m in self.cfg["models"]):
                fam_counts, models = {}, []
                for m in self.cfg["models"]:
                    if m["family"] != fam:
                        continue
                    rows, m_counts = [], {}
                    for c in main:
                        if c.model != m["id"] or c.k != k:
                            continue
                        r = self._row(c.counts, k, c.done, c.target, c.invalid, c.disabled)
                        r["effort"] = c.effort
                        if c.lats:
                            lats = sorted(c.lats)
                            r["time"] = {"median": lats[len(lats) // 2], "p90": lats[min(len(lats) - 1, int(len(lats) * 0.9))]}
                            r["rtok"] = c.rtok / len(lats)
                        rows.append(r)
                        for v, n in c.counts.items():
                            m_counts[v] = m_counts.get(v, 0) + n
                            fam_counts[v] = fam_counts.get(v, 0) + n
                    # the same question in each wording, all at the rewording-check effort
                    wording = []
                    for c in self.cells:
                        if c.model == m["id"] and c.k == k and not c.control and c.effort == self.cfg.get("paraphrase_effort"):
                            w = self._row(c.counts, k, c.done, c.target, c.invalid, c.disabled)
                            w["prompt_id"] = c.prompt_id
                            wording.append(w)
                    models.append({"id": m["id"], "label": m.get("label", m["id"]), "rows": rows,
                                   "pooled": self._row(m_counts, k), "wording": wording})
                    pooled_by_model.append(m_counts)
                    model_meta.append((m.get("label", m["id"]), fam, bool(m.get("control"))))
                families.append({"family": fam, "control": all(c for _, f, c in model_meta if f == fam),
                                 "pooled": self._row(fam_counts, k), "models": models})

            # Distances between models (efforts pooled). JSD compares exact answers
            # (10-wide bins for 1..1000, where exact matches are too sparse);
            # Wasserstein also counts near misses as close.
            llm_counts = {}  # every language model pooled, for the summary card
            for counts, meta in zip(pooled_by_model, model_meta):
                if not meta[2]:
                    for v, cnt in counts.items():
                        llm_counts[v] = llm_counts.get(v, 0) + cnt
            n = len(pooled_by_model)
            jsd_in = pooled_by_model
            if size > 1:
                jsd_in = [{} for _ in range(n)]
                for i, counts in enumerate(pooled_by_model):
                    for v, cnt in counts.items():
                        b = (v - 1) // size
                        jsd_in[i][b] = jsd_in[i].get(b, 0) + cnt
            enough = [sum(c.values()) >= 20 for c in pooled_by_model]
            jsd_m = [[None] * n for _ in range(n)]
            was_m = [[None] * n for _ in range(n)]
            for i in range(n):
                for j in range(i + 1, n):
                    if enough[i] and enough[j]:
                        jsd_m[i][j] = jsd_m[j][i] = stats.jsd(jsd_in[i], jsd_in[j])
                        was_m[i][j] = was_m[j][i] = stats.wasserstein(pooled_by_model[i], pooled_by_model[j], k)
            fams = [f for _, f, _ in model_meta]
            ctrl = [c for _, _, c in model_meta]
            llm = [i for i in range(n) if not ctrl[i]]

            def llm_test(mat):
                return stats.family_test([[mat[i][j] for j in llm] for i in llm], [fams[i] for i in llm])

            def noise(mat):  # distance between two truly random samplers: the sampling-noise floor
                vals = [mat[i][j] for i in range(n) for j in range(i + 1, n) if ctrl[i] and ctrl[j] and mat[i][j] is not None]
                return sum(vals) / len(vals) if vals else None
            views[str(k)] = {
                "k": k, "bin_size": size, "uniform": {"mean": mean, "sd": sd},
                "families": families,
                "llm": dict(self._row(llm_counts, k),
                            top=sorted(llm_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:5]),
                "matrix": {
                    "labels": [l for l, _, _ in model_meta], "families": fams, "control": ctrl,
                    "jsd": jsd_m, "wass": was_m,
                    "test": {"jsd": llm_test(jsd_m), "wass": llm_test(was_m)},
                    "noise": {"jsd": noise(jsd_m), "wass": noise(was_m)},
                },
            }
        return views

    def state(self):
        with self.lock:
            if self._views[0] != self.version:
                self._views = (self.version, self._build_views())
            now = time.time()
            window = [t for t in self.done_times if now - t <= 60]
            span = max(1.0, min(60.0, now - self.run_started)) if self.run_started else 60.0
            rate = len(window) / span if self.active is not None else 0.0
            tests = []
            for k in self.cfg["ranges"]:
                api = [c for c in self.cells if c.k == k and not c.control]
                runnable = sum(max(0, c.target - c.done) for c in api
                               if not c.disabled and not self.providers[c.provider]["stopped"])
                live = k == self.active
                tests.append({
                    "k": k, "status": self.tests[k]["status"], "message": self.tests[k]["message"],
                    "done": sum(c.done for c in api), "target": sum(c.target for c in api),
                    "per_min": rate * 60 if live else 0,
                    "eta_s": (runnable / rate) if live and rate > 0 else None,
                    "file": os.path.relpath(self.detail_path(k), ROOT),
                    "quality": {
                        "total": sum(c.done for c in api),
                        "clean": sum(c.done - c.invalid - c.extra for c in api),
                        "extra": sum(c.extra for c in api), "unusable": sum(c.invalid for c in api),
                        "examples": list(self.bad[k])[:6],
                    },
                    "round": self._round_seen[k], "rounds": self.cfg["samples_per_cell"],
                    "history": self.history[k],
                })
            provs = []
            for pid, p in self.providers.items():
                if pid == "local":
                    continue  # free and instant: not shown as an API provider
                by_test = {}
                for k in self.cfg["ranges"]:
                    cells = [c for c in self.cells if c.provider == pid and c.k == k]
                    by_test[str(k)] = {"spent": p["spent_by"][k], "projected": self._projected(pid, k),
                                       "done": sum(c.done for c in cells), "target": sum(c.target for c in cells)}
                provs.append({
                    "id": pid, "name": PROVIDER_NAMES.get(pid, pid),
                    "key": True if self.demo else bool(get_key(pid)),
                    "spent": p["spent"], "cap": p["cap"], "state": p["state"], "by_test": by_test,
                })
            return {
                "demo": self.demo, "version": self.version, "active": self.active, "tests": tests,
                "providers": provs, "ranges": self.cfg["ranges"], "views": self._views[1],
                "recent": list(self.recent)[:14], "issues": list(self.issues),
                "data_file": os.path.relpath(self.results_path, ROOT),
                "prompts": self.cfg["prompts"],
                "samples": {"main": self.cfg["samples_per_cell"], "para": self.cfg["paraphrase_samples"],
                            "para_effort": self.cfg.get("paraphrase_effort")},
            }

    def export_csv(self):
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["provider", "family", "model", "effort", "range", "prompt_id", "n_valid", "n_invalid",
                    "mean", "sd", "uniform_mean", "uniform_sd", "mode", "mode_share", "distinct_values",
                    "entropy_norm", "entropy_uniform_ref_at_n", "chi2", "chi2_p", "cost_usd", "avg_output_tokens"])
        with self.lock:
            for c in self.cells:
                s = stats.cell_stats(c.counts, c.k)
                um, usd = stats.uniform_mean_sd(c.k)
                g = lambda name: "" if s.get(name) is None else s[name]
                w.writerow([c.provider, c.family, c.model, c.effort, c.k, c.prompt_id, s["n"], c.invalid,
                            g("mean"), g("sd"), um, usd, g("mode"), g("mode_share"), g("distinct"),
                            g("entropy"), g("entropy_ref"), g("chi2"), g("chi2_p"),
                            round(c.cost, 6), round(c.out_tok / c.done, 1) if c.done else ""])
        return buf.getvalue()


class Handler(BaseHTTPRequestHandler):
    bench = None

    def log_message(self, *args):
        pass

    def _local(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def _send(self, code, body, ctype):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._local():
            return self._send(403, "forbidden", "text/plain")
        path = self.path.split("?")[0]
        if path == "/":
            with open(os.path.join(ROOT, "ui", "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if path == "/api/state":
            return self._send(200, json.dumps(self.bench.state()), "application/json")
        if path == "/api/summary.csv":
            return self._send(200, self.bench.export_csv(), "text/csv; charset=utf-8")
        self._send(404, "not found", "text/plain")

    def do_POST(self):
        # The custom header blocks cross-site form posts from other pages.
        if not self._local() or self.headers.get("X-Bench") != "1":
            return self._send(403, "forbidden", "text/plain")
        path, _, query = self.path.partition("?")
        if path == "/api/run":
            try:
                self.bench.start(int(query.split("range=")[1].split("&")[0]))
            except (IndexError, ValueError):
                return self._send(400, "range is required", "text/plain")
        elif path == "/api/pause":
            self.bench.pause()
        else:
            return self._send(404, "not found", "text/plain")
        self._send(200, json.dumps(self.bench.state()), "application/json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="simulated answers, no API calls")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    url = "http://127.0.0.1:%d/" % args.port
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError:
        print("Already running. Opening " + url)
        if not args.no_browser:
            webbrowser.open(url)
        return
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    Handler.bench = Bench(cfg, args.demo)
    print(("DEMO mode (simulated answers). " if args.demo else "") + "Benchmark UI: " + url)
    print("Keep this window open. Press Ctrl+C to quit; progress is saved after every answer.")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped. Progress is saved.")


if __name__ == "__main__":
    sys.exit(main())
