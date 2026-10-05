#!/usr/bin/env python3
"""Export only integrity-checked bundled records for a read-only public website."""
from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from server import EvalStore

ROOT = Path(__file__).resolve().parent


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")


def build_site(output: Path) -> None:
    output = output.resolve()
    if output == ROOT or ROOT.is_relative_to(output) or output in {ROOT / "bundled", ROOT / "results"}:
        raise ValueError("Choose a separate website output directory, not source or result storage")
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="hangauge-export-") as data_dir:
        store = EvalStore(Path(data_dir), read_only=True)
        dataset = store.public_dataset()
        runs = store.list_runs()
        status = store.status()
        status["storage_path"] = "内置只读结果"
        write_json(output / "dataset.json", dataset)
        write_json(output / "runs.json", runs)
        write_json(output / "settings.json", store.settings())
        write_json(output / "status.json", status)
        for item in runs:
            run_id = item["id"]
            run = store.load_run(run_id)
            write_json(output / "runs" / f"{run_id}.json", store.run_response(run, run_id))
            for task in dataset["tasks"]:
                write_json(output / "runs" / run_id / "cases" / f"{task['id']}.json", store.cases_for_task(run_id, task["id"]))
    for name in ("index.html", "app.js"):
        (output / name).write_bytes((ROOT / name).read_bytes())
    (output / "static-mode.js").write_text("window.HANGAUGE_STATIC = true;\n", encoding="utf-8")
    (output / ".nojekyll").write_text("", encoding="utf-8")
    print(f"Exported {len(runs)} immutable results and {dataset['total']} questions to {output}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "docs", help="website directory (default: docs)")
    args = parser.parse_args()
    build_site(args.output)


if __name__ == "__main__":
    main()
