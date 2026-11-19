#!/usr/bin/env python3
"""Build site/index.html: the benchmark UI as one self-contained, read-only page.

The page carries a snapshot of the results inline, so it needs no server and can
be hosted anywhere (a website subpage, GitHub Pages) or opened from disk. It makes
no API calls and has no Run button. Run again after each test to refresh it:

    python3 build_site.py
"""
import json
import os
import time

import server

ROOT = os.path.dirname(os.path.abspath(__file__))
REPO = "https://github.com/diegocp01/llm-random-number-benchmark"
DATASET = "https://huggingface.co/datasets/diegocp01/llm-random-number-benchmark"


def main():
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    bench = server.Bench(cfg, False)          # reads data/, starts nothing
    state = bench.state()
    state["recent"] = []
    state["issues"] = []
    for p in state["providers"]:
        p["key"] = False                      # the snapshot says nothing about local keys
    state["repo"] = REPO
    state["links"] = [{"label": "GitHub", "icon": "github", "url": REPO},
                      {"label": "Hugging Face", "icon": "hf", "url": DATASET}]
    state["snapshot_date"] = time.strftime("%d %b %Y", time.localtime(os.path.getmtime(bench.results_path))).lstrip("0")

    with open(os.path.join(ROOT, "ui", "index.html"), encoding="utf-8") as f:
        html = f.read()
    marker = "<script>\nconst $ ="
    assert html.count(marker) == 1, "could not find the UI script to attach the snapshot to"
    data = json.dumps(state, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = html.replace(marker, "<script>window.SNAPSHOT = " + data + ";</script>\n" + marker)

    out = os.path.join(ROOT, "site", "index.html")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    done = [t["label"] for t in state["tests"] if t["status"] == "done"]
    print("wrote %s (%d KB), tests with complete results: %s" % (os.path.relpath(out, ROOT), os.path.getsize(out) // 1024, done))


if __name__ == "__main__":
    main()
