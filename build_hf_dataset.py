#!/usr/bin/env python3
"""Build hf_dataset/: the results in the layout the Hugging Face dataset viewer reads.

    python3 build_hf_dataset.py

Writes one flat JSON Lines file per finished test (one row per call), the summary
table, and the dataset card. Reads data/ and changes nothing in it.
"""
import csv
import glob
import json
import os
import shutil

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "hf_dataset")
REPO = "https://github.com/diegocp01/llm-random-number-benchmark"

COLUMNS = ["time", "test", "range_max", "is_control", "provider", "family", "model", "served_model",
           "reasoning_effort", "run", "prompt_id", "input", "reasoning", "reasoning_tokens", "output",
           "number", "valid", "number_only", "reason", "input_tokens", "output_tokens", "cost_usd",
           "latency_s", "stop"]


def flat(rec):
    usage = rec.get("usage") or {}
    return {
        "time": rec["time"], "test": rec["test"], "range_max": int(rec["test"].split("-")[1]),
        "is_control": rec["provider"] == "local",
        "provider": rec["provider"], "family": rec["family"], "model": rec["model"],
        "served_model": rec["served_model"], "reasoning_effort": rec.get("reasoning_effort"),
        "run": rec.get("run"), "prompt_id": rec["prompt_id"], "input": rec["input"],
        "reasoning": rec.get("reasoning") or None, "reasoning_tokens": rec.get("reasoning_tokens"),
        "output": rec["output"], "number": rec.get("number"), "valid": rec["valid"],
        "number_only": rec.get("number_only"), "reason": rec["reason"],
        "input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
        "cost_usd": rec["cost_usd"], "latency_s": rec["latency_s"], "stop": rec["stop"],
    }


def main():
    if os.path.isdir(OUT):
        shutil.rmtree(OUT)                     # derived folder only; data/ is never touched
    os.makedirs(os.path.join(OUT, "calls"))
    splits = []
    for path in sorted(glob.glob(os.path.join(ROOT, "data", "test_1-*.json")), key=lambda p: int(p.rsplit("-", 1)[1][:-5])):
        with open(path, encoding="utf-8") as f:
            records = json.load(f)
        if not records:
            continue
        name = os.path.basename(path)[:-5]     # test_1-10
        with open(os.path.join(OUT, "calls", name + ".jsonl"), "w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(flat(rec), ensure_ascii=False) + "\n")
        splits.append((name.replace("-", "_"), "calls/%s.jsonl" % name, len(records)))

    # summary: only rows that have answers
    with open(os.path.join(ROOT, "results", "summary.csv"), encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    kept = [r for r in rows if int(r["n_valid"] or 0) + int(r["n_invalid"] or 0) > 0]
    with open(os.path.join(OUT, "summary.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(kept)

    card = ["---", "license: cc-by-4.0", "language:", "- en", "pretty_name: LLM Random Number Benchmark",
            "tags:", "- llm", "- benchmark", "- randomness", "- gpt", "- claude", "size_categories:", "- 10K<n<100K",
            "configs:", "- config_name: calls", "  default: true", "  data_files:"]
    for split, rel, _ in splits:
        card += ["  - split: %s" % split, "    path: %s" % rel]
    card += ["- config_name: summary", "  data_files: summary.csv", "---", ""]
    card += [
        "# LLM Random Number Benchmark",
        "",
        "[![Code and results on GitHub](https://img.shields.io/badge/GitHub-code%20and%20results-181717?logo=github)](" + REPO + ")"
        " [![Live results](https://img.shields.io/badge/live%20results-gptaiacademy.com-2a78d6)](https://gptaiacademy.com/benchmarks/random-number)",
        "",
        "<img src=\"https://raw.githubusercontent.com/diegocp01/llm-random-number-benchmark/main/docs/figures/summary-card-1-10.png\" alt=\"97 percent of 4,800 answers were 7\" width=\"620\">",
        "",
        "What happens when you ask a language model to pick a random number, thousands of times?",
        "",
        "GPT and Claude models were asked the same one-line question in a fresh conversation every time,",
        "at three reasoning efforts. A real random number generator answered the same question as a control.",
        "",
        "**1–10 test: 97.5% of 4,800 answers were 7.** Six of the ten numbers were never picked.",
        "",
        "**1–100 test: 93% of 4,800 answers were one of five numbers (47, 73, 37, 67, 57).** 80 of the 100 numbers were never picked.",
        "",
        "Code, method, figures and the full write-up: " + REPO,
        "",
        "## What is here",
        "",
        "| Config | Rows | Contents |",
        "|---|---|---|",
    ]
    card += ["| `calls`, split `%s` | %s | every call of that test, one row each, including the control draws |" % (s, format(n, ","))
             for s, _, n in splits]
    card += [
        "| `summary` | %d | one row per model × effort × range × prompt: counts, mean, SD, mode, spread, chi-square, cost |" % len(kept),
        "",
        "Each test is one split. Tests not yet run are added when they finish.",
        "",
        "## Columns of `calls`",
        "",
        "| Column | Meaning |",
        "|---|---|",
        "| `time`, `test`, `range_max` | local timestamp; which test (`1-10`); the top of the range |",
        "| `is_control` | true for the Python random generator rows, false for language models |",
        "| `provider`, `family`, `model`, `served_model` | who was asked; `served_model` is the model ID the API reported back |",
        "| `reasoning_effort` | `low`, `medium` or `high`; empty for the control, which has `run` instead |",
        "| `prompt_id`, `input` | `main`, `para1` or `para2` (two rewordings, asked at low effort only); the exact text sent |",
        "| `reasoning`, `reasoning_tokens` | the reasoning summary the API returned, and the reasoning tokens used |",
        "| `output` | the raw reply |",
        "| `number`, `valid`, `number_only`, `reason` | the parsed answer and the result of the number-only quality check |",
        "| `input_tokens`, `output_tokens`, `cost_usd` | tokens as reported by the API; cost = tokens × list price at run time |",
        "| `latency_s`, `stop` | seconds from request to full reply; stop reason |",
        "",
        "`reasoning` is the summary each API provides. Neither API exposes the raw chain of thought.",
        "",
        "## Method in brief",
        "",
        "- Prompt: \"Pick a random number between 1 and X. Reply with only the number.\"",
        "- One user message, no system prompt, no earlier messages, a new request every time.",
        "- 200 answers per model at each of low, medium and high reasoning effort; API-default sampling.",
        "- Models: GPT-6 Luna, GPT-6.1 Terra, GPT-6.2 Sol, GPT-6.2 Astra; Claude Haiku 5.5, Sonnet 5.5, Opus 5.5, Fable 5.2.",
        "- Control: Python `random` (seeded) and `secrets`, scored by the same code.",
        "",
        "## Loading",
        "",
        "```python",
        "from datasets import load_dataset",
        "calls = load_dataset(\"diegocp01/llm-random-number-benchmark\", \"calls\", split=\"%s\")" % (splits[0][0] if splits else "test_1_10"),
        "summary = load_dataset(\"diegocp01/llm-random-number-benchmark\", \"summary\")",
        "```",
        "",
        "## License and citation",
        "",
        "CC BY 4.0. Cabezas, D. (2026). LLM Random Number Benchmark. " + REPO,
        "",
    ]
    with open(os.path.join(OUT, "README.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(card))
    print("wrote hf_dataset/: splits %s, summary rows %d" % ([(s, n) for s, _, n in splits], len(kept)))


if __name__ == "__main__":
    main()
