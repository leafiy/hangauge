#!/usr/bin/env python3
"""HanGauge standalone evaluation server and immutable bundled run store."""

from __future__ import annotations

import argparse
import copy
import datetime as _dt
import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from evaluator import (
    default_settings,
    evaluate_case,
    normalize_api_key,
    public_grading_provider,
    sanitize_text,
    validate_candidate_config,
    validate_grader_config,
    validate_settings,
)
from grading import (
    GRADING_VERSION,
    ensure_compatible_grading_provider,
    grade_answers,
    grading_method,
    needs_model_grader,
    reference_evaluations,
    summarize_evaluations,
)

SCRIPT_DIR = Path(__file__).resolve().parent
DATASET_PATH = SCRIPT_DIR / "dataset.json"
INDEX_PATH = SCRIPT_DIR / "index.html"
APP_PATH = SCRIPT_DIR / "app.js"
BUNDLED_DIR = SCRIPT_DIR / "bundled"
MANIFEST_PATH = BUNDLED_DIR / "manifest.json"
DEFAULT_RESULTS_DIR = SCRIPT_DIR / "results"
MAX_BODY_BYTES = 16 * 1024 * 1024
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
PROTECTED_MUTATION_MESSAGE = "内置评测记录不可修改、删除或重新评分；请新建评测记录。"
BASELINE_MUTATION_MESSAGE = "固定对照基线不可修改、删除或重新评分；请新建评测记录。"


def utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_json(path: Path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def atomic_write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as f:
            temp_name = f.name
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temp_name:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


def run_id_from_path(path: str, prefix: str, suffix: str = "") -> str | None:
    if not path.startswith(prefix) or (suffix and not path.endswith(suffix)):
        return None
    run_id = path[len(prefix) : len(path) - len(suffix) if suffix else None]
    if not run_id or "/" in run_id or not RUN_ID_RE.fullmatch(run_id):
        return None
    return run_id


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def canonical_sha256(value) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256_bytes(raw)


def canonical_raw_answer_map(run: dict) -> dict:
    answers = run.get("answers") if isinstance(run.get("answers"), dict) else {}
    return {
        item_id: answer.get("raw")
        for item_id, answer in answers.items()
        if isinstance(item_id, str) and isinstance(answer, dict) and isinstance(answer.get("raw"), str)
    }


def canonical_evaluations_map(run: dict) -> dict:
    evaluations = run.get("evaluations")
    return evaluations if isinstance(evaluations, dict) else {}


def is_loopback_host(host: str) -> bool:
    return host in {"127.0.0.1", "localhost", "::1"}


class EvalStore:
    def __init__(self, results_dir: Path, read_only: bool = False):
        self.results_dir = results_dir
        self.read_only = read_only
        if not self.read_only:
            self.results_dir.mkdir(parents=True, exist_ok=True)
        self.dataset = load_json(DATASET_PATH)
        self.manifest = self.load_and_validate_manifest()
        self.baseline_id = self.manifest["baseline_id"]
        self.protected_run_ids = frozenset(run["id"] for run in self.manifest["runs"])
        self.bundled_runs = self.load_and_validate_bundled_runs()
        self.baseline_run = copy.deepcopy(self.bundled_runs[self.baseline_id])
        self.baseline_name = self.baseline_run.get("name", "")
        self.lock = threading.RLock()
        self.active_run_id: str | None = None
        self.stop_event = threading.Event()
        self.settings_path = results_dir / ".settings.json"
        if not self.read_only:
            self.recover_interrupted()

    def load_and_validate_manifest(self) -> dict:
        raw = MANIFEST_PATH.read_bytes()
        manifest = json.loads(raw.decode("utf-8"))
        if not isinstance(manifest, dict) or manifest.get("version") != 1:
            raise RuntimeError("bundled manifest must be version 1")
        runs = manifest.get("runs")
        if not isinstance(runs, list) or len(runs) != 3:
            raise RuntimeError("bundled manifest must list exactly three runs")
        baseline_id = manifest.get("baseline_id")
        if not isinstance(baseline_id, str) or not RUN_ID_RE.fullmatch(baseline_id):
            raise RuntimeError("bundled manifest baseline_id is invalid")
        if manifest.get("dataset_sha256") != sha256_bytes(DATASET_PATH.read_bytes()):
            raise RuntimeError("dataset.json sha256 does not match bundled manifest")
        ids = [run.get("id") for run in runs if isinstance(run, dict)]
        if len(ids) != 3 or len(set(ids)) != 3 or baseline_id not in ids:
            raise RuntimeError("bundled manifest run ids are invalid")
        return manifest

    def load_and_validate_bundled_runs(self) -> dict[str, dict]:
        inputs = self.dataset.get("inputs")
        if not isinstance(inputs, dict) or len(inputs) != 360:
            raise RuntimeError("dataset must contain exactly 360 inputs")
        input_ids = set(inputs)
        runs: dict[str, dict] = {}
        for entry in self.manifest["runs"]:
            if not isinstance(entry, dict):
                raise RuntimeError("bundled manifest run entry must be an object")
            run_id = entry.get("id")
            file_name = entry.get("file")
            if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
                raise RuntimeError("bundled run id is invalid")
            if file_name != f"{run_id}.json":
                raise RuntimeError(f"bundled run file mismatch for {run_id}")
            path = BUNDLED_DIR / file_name
            raw = path.read_bytes()
            digest = sha256_bytes(raw)
            if digest != entry.get("sha256"):
                raise RuntimeError(f"bundled run sha256 mismatch: {file_name}")
            run = json.loads(raw.decode("utf-8"))
            if not isinstance(run, dict) or run.get("id") != run_id:
                raise RuntimeError(f"bundled run id mismatch: {file_name}")
            answers = run.get("answers")
            if not isinstance(answers, dict) or set(answers) != input_ids:
                raise RuntimeError(f"bundled run answers are incomplete: {run_id}")
            if canonical_sha256(canonical_raw_answer_map(run)) != entry.get("answers_sha256"):
                raise RuntimeError(f"bundled run answers hash mismatch: {run_id}")
            if canonical_sha256(canonical_evaluations_map(run)) != entry.get("evaluations_sha256"):
                raise RuntimeError(f"bundled run evaluations hash mismatch: {run_id}")
            if run.get("dataset_id") != self.dataset.get("id"):
                raise RuntimeError(f"bundled run dataset_id mismatch: {run_id}")
            if run_id != self.manifest["baseline_id"]:
                evaluations = run.get("evaluations")
                if not isinstance(evaluations, dict) or set(evaluations) != input_ids:
                    raise RuntimeError(f"bundled scored run evaluations are incomplete: {run_id}")
            else:
                summary = run.get("summary") if isinstance(run.get("summary"), dict) else {}
                if summary.get("dataset_id") != self.dataset.get("id"):
                    raise RuntimeError("baseline summary dataset_id mismatch")
                dataset_sha = (self.dataset.get("manifest") or {}).get("data_sha256")
                if summary.get("dataset_sha256") != dataset_sha:
                    raise RuntimeError("baseline summary dataset_sha256 mismatch")
            runs[run_id] = run
        return runs

    def run_path(self, run_id: str) -> Path:
        if not RUN_ID_RE.fullmatch(run_id):
            raise ValueError("invalid run id")
        return self.results_dir / f"{run_id}.json"

    def is_baseline_id(self, run_id: str | None) -> bool:
        return run_id == self.baseline_id

    def is_protected_id(self, run_id: str | None) -> bool:
        return isinstance(run_id, str) and run_id in self.protected_run_ids

    def ensure_writable(self) -> None:
        if self.read_only:
            raise ClientError(HTTPStatus.FORBIDDEN, "read-only server: mutations are disabled")

    def ensure_mutable_run(self, run_id: str | None) -> None:
        if self.is_baseline_id(run_id):
            raise ClientError(HTTPStatus.FORBIDDEN, BASELINE_MUTATION_MESSAGE)
        if self.is_protected_id(run_id):
            raise ClientError(HTTPStatus.FORBIDDEN, PROTECTED_MUTATION_MESSAGE)

    def load_run(self, run_id: str) -> dict:
        if self.is_protected_id(run_id):
            return copy.deepcopy(self.bundled_runs[run_id])
        path = self.run_path(run_id)
        if not path.exists():
            raise FileNotFoundError(run_id)
        value = load_json(path)
        if not isinstance(value, dict):
            raise ValueError("saved run is not an object")
        if value.get("id") in self.protected_run_ids:
            raise ValueError("custom data directory contains a protected bundled id shadow")
        return value

    def public_dataset(self) -> dict:
        dataset = self.dataset
        inputs = dataset.get("inputs", {})
        if not isinstance(inputs, dict):
            inputs = {}
        tasks = []
        for task in dataset.get("tasks", []):
            if not isinstance(task, dict):
                continue
            item = dict(task)
            task_id = item.get("id")
            if isinstance(task_id, str) and task_id:
                item["grading_method"] = grading_method(task_id)
            tasks.append(item)
        return {
            "id": dataset.get("id"),
            "title": dataset.get("title"),
            "tasks": tasks,
            "domains": dataset.get("domains", []),
            "total": len(inputs),
            "cases": [{"item_id": item_id, "input": input_obj} for item_id, input_obj in inputs.items()],
        }

    @staticmethod
    def captured_answers(run: dict) -> dict[str, dict]:
        answers = run.get("answers")
        if not isinstance(answers, dict):
            return {}
        captured: dict[str, dict] = {}
        for item_id, answer in answers.items():
            if not isinstance(item_id, str) or not isinstance(answer, dict):
                continue
            if isinstance(answer.get("raw"), str):
                captured[item_id] = answer
        return captured

    @staticmethod
    def current_evaluations(run: dict) -> dict[str, dict]:
        evaluations = run.get("evaluations")
        if not isinstance(evaluations, dict):
            return {}
        return {item_id: value for item_id, value in evaluations.items() if isinstance(item_id, str) and isinstance(value, dict)}

    def current_evaluation_count(self, run: dict) -> int:
        captured = self.captured_answers(run)
        evaluations = self.current_evaluations(run)
        return sum(
            1
            for item_id in captured
            if isinstance(evaluations.get(item_id), dict)
            and evaluations[item_id].get("grading_version") == GRADING_VERSION
        )

    def grading_template(self, status: str = "not_started", total: int = 0, completed: int = 0) -> dict:
        return {
            "status": status,
            "version": GRADING_VERSION,
            "total": total,
            "completed": completed,
            "current_error": None,
            "error": None,
            "started_at": None,
            "finished_at": None,
        }

    def normalized_grading(self, run: dict, run_id: str | None = None) -> dict:
        if self.is_baseline_id(run_id or run.get("id")):
            total = len(self.dataset.get("inputs", {}) or {})
            return {
                "status": "confirmed",
                "version": GRADING_VERSION,
                "total": total,
                "completed": total,
                "current_error": None,
                "error": None,
                "started_at": run.get("created_at"),
                "finished_at": run.get("updated_at") or run.get("created_at"),
            }
        captured = self.captured_answers(run)
        grading = run.get("grading")
        if not isinstance(grading, dict):
            grading = self.grading_template()
        value = self.grading_template(str(grading.get("status") or "not_started"), len(captured), self.current_evaluation_count(run))
        for key in ("version", "current_error", "error", "started_at", "finished_at"):
            if key in grading:
                value[key] = grading.get(key)
        if value["version"] != GRADING_VERSION and value["status"] == "completed":
            value["status"] = "not_started"
        return value

    def run_evaluations(self, run: dict, run_id: str | None = None) -> dict[str, dict]:
        if self.is_baseline_id(run_id or run.get("id")):
            return reference_evaluations(self.dataset, self.baseline_run)
        return self.current_evaluations(run)

    def run_assessment(self, run: dict, run_id: str | None = None) -> dict:
        evaluations = self.run_evaluations(run, run_id)
        return summarize_evaluations(self.dataset, self.captured_answers(run), evaluations)

    def grade_targets(self, run: dict) -> dict[str, dict]:
        captured = self.captured_answers(run)
        evaluations = self.current_evaluations(run)
        return {
            item_id: answer
            for item_id, answer in captured.items()
            if not isinstance(evaluations.get(item_id), dict)
            or evaluations[item_id].get("grading_version") != GRADING_VERSION
        }

    def run_grading_provider(self, run: dict) -> dict | None:
        provider = run.get("grading_provider")
        if isinstance(provider, dict):
            return provider
        models = {
            evaluation.get("grader_model")
            for evaluation in self.current_evaluations(run).values()
            if isinstance(evaluation, dict)
            and evaluation.get("method") != "direct"
            and isinstance(evaluation.get("grader_model"), str)
            and evaluation.get("grader_model")
        }
        if len(models) == 1:
            return {"model": next(iter(models))}
        return None

    def run_list_row(self, run: dict, storage_id: str | None = None) -> dict:
        run_id = storage_id or run.get("id")
        answers = run.get("answers")
        if not isinstance(answers, dict):
            raise ValueError("invalid saved answers")
        grading = self.normalized_grading(run, run_id)
        assessment = self.run_assessment(run, run_id)
        return {
            "id": run_id,
            "name": run.get("name", ""),
            "created_at": run.get("created_at"),
            "updated_at": run.get("updated_at"),
            "answered": assessment.get("counts", {}).get("answered", len(self.captured_answers(run))),
            "graded": assessment.get("counts", {}).get("graded", grading.get("completed", 0)),
            "total": len(self.dataset.get("inputs", {}) or {}),
            "notes": run.get("notes", ""),
            "test_status": run.get("test", {}).get("status") if isinstance(run.get("test"), dict) else None,
            "grading": grading,
            "assessment": assessment,
            "grading_provider": self.run_grading_provider(run),
            "is_baseline": self.is_baseline_id(run_id),
            "protected": self.is_protected_id(run_id),
            "review_required": False,
        }

    def list_runs(self) -> list[dict]:
        runs = [self.run_list_row(self.bundled_runs[run_id], run_id) for run_id in self.protected_run_ids]
        if self.results_dir.exists():
            for path in sorted(self.results_dir.glob("*.json")):
                if path.name.startswith("."):
                    continue
                run_id = path.stem
                if self.is_protected_id(run_id) or not RUN_ID_RE.fullmatch(run_id):
                    continue
                run = load_json(path)
                if not isinstance(run, dict):
                    raise ValueError(f"invalid saved run: {path.name}")
                if run.get("id") in self.protected_run_ids:
                    continue
                try:
                    runs.append(self.run_list_row(run, run_id))
                except ValueError as exc:
                    raise ValueError(f"{exc}: {path.name}") from exc
        runs.sort(key=lambda item: item.get("created_at") or "", reverse=True)
        return runs

    def create_run(self, payload: dict) -> dict:
        self.ensure_writable()
        name = payload.get("name")
        notes = payload.get("notes", "")
        if not isinstance(name, str) or not name.strip():
            raise ClientError(HTTPStatus.BAD_REQUEST, "name must be a non-empty string")
        if not isinstance(notes, str):
            raise ClientError(HTTPStatus.BAD_REQUEST, "notes must be a string")
        run_id = str(uuid.uuid4())
        now = utc_now_iso()
        run = {
            "id": run_id,
            "name": name.strip(),
            "created_at": now,
            "updated_at": now,
            "dataset_id": self.dataset.get("id"),
            "notes": notes,
            "answers": {},
            "evaluations": {},
            "grading": self.grading_template(),
            "baseline_id": self.baseline_id,
        }
        atomic_write_json(self.run_path(run_id), run)
        return run

    def ensure_idle(self, run_id: str) -> None:
        if self.active_run_id != run_id:
            return
        run = self.load_run(run_id)
        grading = run.get("grading") if isinstance(run.get("grading"), dict) else {}
        if grading.get("status") == "running":
            raise ClientError(HTTPStatus.CONFLICT, "评分进行中，请等待完成；已捕获回答不会被停止或重跑")
        raise ClientError(HTTPStatus.CONFLICT, "测试运行中，请先停止并等待当前请求完成")

    def settings(self) -> dict:
        if self.settings_path.exists():
            return validate_settings(load_json(self.settings_path))
        return default_settings()

    def update_settings(self, payload: dict) -> dict:
        self.ensure_writable()
        try:
            settings = validate_settings(payload)
        except ValueError as exc:
            raise ClientError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
        atomic_write_json(self.settings_path, settings)
        return settings

    def status(self) -> dict:
        active_phase = None
        if self.active_run_id:
            try:
                run = self.load_run(self.active_run_id)
                grading = run.get("grading") if isinstance(run.get("grading"), dict) else {}
                test = run.get("test") if isinstance(run.get("test"), dict) else {}
                if grading.get("status") == "running":
                    active_phase = "grading"
                elif test.get("status") in {"running", "stopping"}:
                    active_phase = test.get("phase") or "answering"
            except Exception:
                active_phase = None
        return {
            "app_name": "HanGauge",
            "read_only": self.read_only,
            "active_run_id": self.active_run_id,
            "active_phase": active_phase,
            "storage_path": "" if self.read_only else str(self.results_dir),
            "baseline_run_id": self.baseline_id,
            "baseline_name": self.baseline_name,
            "bundled_run_ids": sorted(self.protected_run_ids),
        }

    def update_run(self, run_id: str, payload: dict) -> dict:
        self.ensure_writable()
        self.ensure_mutable_run(run_id)
        self.ensure_idle(run_id)
        run = self.load_run(run_id)
        name = payload.get("name")
        notes = payload.get("notes", run.get("notes", ""))
        if not isinstance(name, str) or not name.strip() or not isinstance(notes, str):
            raise ClientError(HTTPStatus.BAD_REQUEST, "名称不能为空，备注必须是文本")
        run["name"] = name.strip()
        run["notes"] = notes
        run["updated_at"] = utc_now_iso()
        atomic_write_json(self.run_path(run_id), run)
        return run

    def delete_run(self, run_id: str) -> dict:
        self.ensure_writable()
        self.ensure_mutable_run(run_id)
        self.ensure_idle(run_id)
        self.run_path(run_id).unlink()
        return {"deleted": run_id}

    def select_item_ids(self, scope: str) -> list[str]:
        item_ids = sorted(self.dataset["inputs"])
        if scope == "one":
            return item_ids[:1]
        if scope == "per_task":
            first_by_task = {}
            for item_id in item_ids:
                first_by_task.setdefault(self.dataset["inputs"][item_id]["task_id"], item_id)
            return list(first_by_task.values())
        return item_ids

    def start_test(self, payload: dict) -> dict:
        self.ensure_writable()
        if self.active_run_id is not None:
            raise ClientError(HTTPStatus.CONFLICT, "已有测试运行中，请等待完成或停止")
        try:
            config = validate_candidate_config(payload)
            api_key = normalize_api_key(payload.get("api_key", ""))
            grader_payload = payload.get("grader") if isinstance(payload.get("grader"), dict) else {}
            grader_config = validate_grader_config(grader_payload, require_base_model=bool(grader_payload.get("base_url") or grader_payload.get("model")))
            grader_api_key = normalize_api_key(grader_payload.get("api_key", ""))
        except ValueError as exc:
            raise ClientError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
        name = payload.get("name") or config["model"]
        notes = payload.get("notes", "")
        if not isinstance(name, str) or not isinstance(notes, str):
            raise ClientError(HTTPStatus.BAD_REQUEST, "name and notes must be strings")
        public_text = json.dumps({"candidate": config, "grader": grader_config, "name": name, "notes": notes}, ensure_ascii=False)
        if api_key and api_key in public_text:
            raise ClientError(HTTPStatus.BAD_REQUEST, "公开配置、名称或备注中不能包含候选API Key")
        if grader_api_key and grader_api_key in public_text:
            raise ClientError(HTTPStatus.BAD_REQUEST, "公开配置、名称或备注中不能包含评分API Key")
        item_ids = self.select_item_ids(config["scope"])
        needs_grader = any(grading_method(self.dataset["inputs"][item_id]["task_id"]) != "direct" for item_id in item_ids)
        if needs_grader and (not grader_config.get("base_url") or not grader_config.get("model")):
            raise ClientError(HTTPStatus.BAD_REQUEST, "该范围包含理解类任务，需要配置评分模型")
        run = self.create_run({"name": name, "notes": notes})
        run["provider"] = config
        if needs_grader:
            run["grading_provider"] = public_grading_provider(grader_config)
        run["test"] = {
            "status": "running",
            "phase": "answering",
            "target_ids": item_ids,
            "attempted_ids": [],
            "current_item": None,
            "started_at": utc_now_iso(),
            "finished_at": None,
            "error": None,
        }
        atomic_write_json(self.run_path(run["id"]), run)
        settings = {"candidate": config, "grader": grader_config if grader_config.get("base_url") and grader_config.get("model") else default_settings()["grader"]}
        atomic_write_json(self.settings_path, settings)
        self.active_run_id = run["id"]
        self.stop_event = threading.Event()
        worker = threading.Thread(
            target=self.run_test,
            args=(run["id"], config, api_key, grader_config, grader_api_key, self.stop_event),
            daemon=True,
            name=f"hangauge-{run['id']}",
        )
        worker.start()
        return run

    def stop_test(self, run_id: str) -> dict:
        self.ensure_writable()
        self.ensure_mutable_run(run_id)
        run = self.load_run(run_id)
        if self.active_run_id != run_id:
            raise ClientError(HTTPStatus.CONFLICT, "该评测不在运行中")
        grading = run.get("grading") if isinstance(run.get("grading"), dict) else {}
        if grading.get("status") == "running":
            raise ClientError(HTTPStatus.CONFLICT, "评分进行中，不能停止；已捕获回答正在评分，不会启动新作答")
        self.stop_event.set()
        run["test"]["status"] = "stopping"
        run["test"]["phase"] = "answering"
        run["updated_at"] = utc_now_iso()
        atomic_write_json(self.run_path(run_id), run)
        return run

    def prepare_grading_locked(self, run: dict, run_id: str, auto: bool, grading_provider: dict | None) -> dict[str, dict]:
        self.ensure_writable()
        self.ensure_mutable_run(run_id)
        captured = self.captured_answers(run)
        if not captured:
            raise ClientError(HTTPStatus.CONFLICT, "该记录没有可评分的已捕获回答")
        targets = self.grade_targets(run)
        if not targets:
            raise ClientError(HTTPStatus.CONFLICT, "该记录已完成当前版本评分")
        if needs_model_grader(self.dataset, targets) and grading_provider is None:
            raise ClientError(HTTPStatus.BAD_REQUEST, "需要配置评分模型后才能评分理解类任务")
        try:
            ensure_compatible_grading_provider(self.current_evaluations(run), targets, grading_provider)
        except ValueError as exc:
            raise ClientError(HTTPStatus.CONFLICT, str(exc)) from exc
        now = utc_now_iso()
        grading = self.grading_template("running", len(captured), self.current_evaluation_count(run))
        grading["started_at"] = now
        run["grading"] = grading
        if grading_provider is not None:
            run["grading_provider"] = grading_provider
        run.setdefault("evaluations", {})
        run["updated_at"] = now
        if auto:
            test = run.get("test")
            if isinstance(test, dict):
                test["status"] = "running"
                test["phase"] = "grading"
                test["current_item"] = None
                test["error"] = None
        atomic_write_json(self.run_path(run_id), run)
        return targets

    def persist_grade_result(self, run_id: str, item_id: str, evaluation: dict) -> None:
        self.ensure_writable()
        self.ensure_mutable_run(run_id)
        with self.lock:
            run = self.load_run(run_id)
            evaluations = run.get("evaluations")
            if not isinstance(evaluations, dict):
                evaluations = {}
                run["evaluations"] = evaluations
            evaluations[item_id] = evaluation
            grading = run.get("grading")
            if not isinstance(grading, dict):
                grading = self.grading_template()
                run["grading"] = grading
            grading["completed"] = self.current_evaluation_count(run)
            grading["current_error"] = None
            run["updated_at"] = utc_now_iso()
            atomic_write_json(self.run_path(run_id), run)

    def finish_grading_locked(self, run: dict, run_id: str, status: str, error: str | None = None, auto: bool = False) -> None:
        self.ensure_writable()
        self.ensure_mutable_run(run_id)
        grading = run.get("grading")
        if not isinstance(grading, dict):
            grading = self.grading_template()
            run["grading"] = grading
        grading["status"] = status
        grading["version"] = GRADING_VERSION
        grading["total"] = len(self.captured_answers(run))
        grading["completed"] = self.current_evaluation_count(run)
        grading["current_error"] = error
        grading["error"] = error
        grading["finished_at"] = utc_now_iso()
        if auto and isinstance(run.get("test"), dict):
            run["test"]["status"] = "completed" if status == "completed" else "failed"
            run["test"]["phase"] = "grading"
            run["test"]["finished_at"] = grading["finished_at"]
            run["test"]["error"] = error
        run["updated_at"] = grading["finished_at"]
        atomic_write_json(self.run_path(run_id), run)

    def run_grading(
        self,
        run_id: str,
        targets: dict[str, dict],
        auto: bool,
        release_active: bool,
        grader_config: dict | None,
        grader_api_key: str,
    ) -> None:
        self.ensure_writable()
        self.ensure_mutable_run(run_id)
        try:
            def on_result(item_id: str, evaluation: dict) -> None:
                self.persist_grade_result(run_id, item_id, evaluation)

            grade_answers(
                self.dataset,
                self.baseline_run,
                targets,
                on_result=on_result,
                grader_config=grader_config,
                grader_api_key=grader_api_key,
            )
            with self.lock:
                run = self.load_run(run_id)
                self.finish_grading_locked(run, run_id, "completed", auto=auto)
        except Exception as exc:
            with self.lock:
                run = self.load_run(run_id)
                safe_error = sanitize_text(str(exc), grader_api_key)
                self.finish_grading_locked(run, run_id, "failed", safe_error, auto=auto)
        finally:
            if release_active:
                with self.lock:
                    if self.active_run_id == run_id:
                        self.active_run_id = None

    def start_grade(self, run_id: str, payload: dict) -> dict:
        self.ensure_writable()
        self.ensure_mutable_run(run_id)
        if self.active_run_id is not None:
            raise ClientError(HTTPStatus.CONFLICT, "已有评测或评分运行中，请等待完成")
        run = self.load_run(run_id)
        if run.get("dataset_id") != self.dataset.get("id"):
            raise ClientError(HTTPStatus.CONFLICT, "saved evaluation belongs to a different dataset")
        targets = self.grade_targets(run)
        if not targets:
            raise ClientError(HTTPStatus.CONFLICT, "该记录已完成当前版本评分")
        needs_grader = needs_model_grader(self.dataset, targets)
        grader_config = None
        grader_api_key = ""
        grading_provider = None
        grader_payload = payload.get("grader") if isinstance(payload.get("grader"), dict) else payload
        if needs_grader and not grader_payload:
            grader_payload = self.settings().get("grader", {})
        if needs_grader or grader_payload:
            try:
                grader_config = validate_grader_config(grader_payload, require_base_model=needs_grader)
                grader_api_key = normalize_api_key(grader_payload.get("api_key", "") if isinstance(grader_payload, dict) else "")
                grading_provider = public_grading_provider(grader_config) if grader_config.get("base_url") and grader_config.get("model") else None
                if grader_api_key and grader_api_key in json.dumps(grading_provider or {}, ensure_ascii=False):
                    raise ValueError("评分模型公开配置中不能包含API Key")
            except ValueError as exc:
                raise ClientError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
        if needs_grader and grading_provider is None:
            raise ClientError(HTTPStatus.BAD_REQUEST, "需要配置评分模型后才能评分理解类任务")
        targets = self.prepare_grading_locked(run, run_id, auto=False, grading_provider=grading_provider)
        self.active_run_id = run_id
        worker = threading.Thread(
            target=self.run_grading,
            args=(run_id, targets, False, True, grader_config, grader_api_key),
            daemon=True,
            name=f"hangauge-grade-{run_id}",
        )
        worker.start()
        return self.load_run(run_id)

    def run_test(
        self,
        run_id: str,
        config: dict,
        api_key: str,
        grader_config: dict | None,
        grader_api_key: str,
        stop_event: threading.Event,
    ) -> None:
        self.ensure_writable()
        self.ensure_mutable_run(run_id)
        should_grade = False
        grade_targets_for_run: dict[str, dict] = {}
        try:
            with self.lock:
                targets = self.load_run(run_id)["test"]["target_ids"]
            for item_id in targets:
                with self.lock:
                    run = self.load_run(run_id)
                    if stop_event.is_set():
                        break
                    run["test"]["attempted_ids"].append(item_id)
                    run["test"]["current_item"] = item_id
                    run["test"]["phase"] = "answering"
                    run["updated_at"] = utc_now_iso()
                    atomic_write_json(self.run_path(run_id), run)
                record = evaluate_case(self.dataset["inputs"][item_id], config, api_key)
                with self.lock:
                    run = self.load_run(run_id)
                    run["answers"][item_id] = record
                    run["test"]["current_item"] = None
                    run["updated_at"] = utc_now_iso()
                    if record["call"]["status"] != "completed":
                        run["test"]["status"] = "failed"
                        run["test"]["error"] = sanitize_text(record["error"], api_key)
                    atomic_write_json(self.run_path(run_id), run)
                    if run["test"]["status"] == "failed":
                        break
            with self.lock:
                run = self.load_run(run_id)
                if run["test"]["status"] == "failed":
                    run["test"]["finished_at"] = utc_now_iso()
                    run["updated_at"] = run["test"]["finished_at"]
                    atomic_write_json(self.run_path(run_id), run)
                elif stop_event.is_set():
                    run["test"]["status"] = "stopped"
                    run["test"]["phase"] = "answering"
                    run["test"]["finished_at"] = utc_now_iso()
                    run["updated_at"] = run["test"]["finished_at"]
                    atomic_write_json(self.run_path(run_id), run)
                else:
                    grading_provider = public_grading_provider(grader_config) if grader_config and grader_config.get("base_url") and grader_config.get("model") else None
                    grade_targets_for_run = self.prepare_grading_locked(run, run_id, auto=True, grading_provider=grading_provider)
                    should_grade = True
            if should_grade:
                self.run_grading(run_id, grade_targets_for_run, auto=True, release_active=False, grader_config=grader_config, grader_api_key=grader_api_key)
        except Exception as exc:
            with self.lock:
                run = self.load_run(run_id)
                run["test"]["status"] = "failed"
                run["test"]["phase"] = run["test"].get("phase") or "answering"
                message = sanitize_text(str(exc), api_key, grader_api_key)
                run["test"]["error"] = message
                item_id = run["test"]["current_item"]
                if item_id and item_id not in run["answers"]:
                    run["answers"][item_id] = {
                        "call": {"status": "failed", "tool_audit_status": "not_applicable",
                                 "response_transport": "openai_chat_completions"},
                        "error": message,
                    }
                run["test"]["current_item"] = None
                run["test"]["finished_at"] = utc_now_iso()
                run["updated_at"] = run["test"]["finished_at"]
                atomic_write_json(self.run_path(run_id), run)
        finally:
            with self.lock:
                if self.active_run_id == run_id:
                    self.active_run_id = None

    def run_response(self, run: dict, storage_id: str | None = None) -> dict:
        value = dict(run)
        run_id = storage_id or value.get("id")
        value["is_baseline"] = self.is_baseline_id(run_id)
        value["protected"] = self.is_protected_id(run_id)
        value["review_required"] = False
        value["baseline_id"] = self.baseline_id
        value["evaluations"] = self.run_evaluations(value, run_id)
        value["assessment"] = self.run_assessment(value, run_id)
        value["grading"] = self.normalized_grading(value, run_id)
        value["grading_provider"] = self.run_grading_provider(value)
        return value

    def answer_raw(self, run: dict, item_id: str) -> str | None:
        answer = run.get("answers", {}).get(item_id) if isinstance(run.get("answers"), dict) else None
        if isinstance(answer, dict) and isinstance(answer.get("raw"), str):
            return answer["raw"]
        return None

    def cases_for_task(self, run_id: str, task_id: str) -> dict:
        task_ids = {task.get("id") for task in self.dataset.get("tasks", []) if isinstance(task, dict)}
        if task_id not in task_ids:
            raise ClientError(HTTPStatus.BAD_REQUEST, "invalid task_id")
        run = self.load_run(run_id)
        evaluations = self.run_evaluations(run, run_id)
        cases = []
        for item_id, input_obj in self.dataset.get("inputs", {}).items():
            if not isinstance(input_obj, dict) or input_obj.get("task_id") != task_id:
                continue
            baseline_raw = self.answer_raw(self.baseline_run, item_id)
            model_raw = self.answer_raw(run, item_id)
            cases.append({
                "item_id": item_id,
                "input": input_obj,
                "baseline_raw": baseline_raw if baseline_raw is not None else "",
                "model_raw": model_raw,
                "evaluation": evaluations.get(item_id),
                "grading_method": grading_method(task_id),
            })
        return {"run_id": run_id, "task_id": task_id, "cases": cases}

    def recover_interrupted(self) -> None:
        if self.read_only or not self.results_dir.exists():
            return
        for path in self.results_dir.glob("*.json"):
            if self.is_protected_id(path.stem) or path.name.startswith("."):
                continue
            run = load_json(path)
            if isinstance(run, dict) and run.get("id") in self.protected_run_ids:
                continue
            changed = False
            now = utc_now_iso()
            test = run.get("test")
            if isinstance(test, dict) and test.get("status") in {"running", "stopping"}:
                item_id = test.get("current_item")
                answers = run.get("answers")
                if not isinstance(answers, dict):
                    answers = {}
                    run["answers"] = answers
                if item_id and item_id not in answers:
                    answers[item_id] = {
                        "call": {"status": "interrupted", "tool_audit_status": "not_applicable",
                                 "response_transport": "openai_chat_completions"},
                        "error": "服务中断，未捕获该请求的结果；不会自动重试",
                    }
                test.update({"status": "interrupted", "current_item": None,
                             "finished_at": now, "error": "服务中断；已保存记录保留，不自动重试"})
                changed = True
            grading = run.get("grading")
            if isinstance(grading, dict) and grading.get("status") == "running":
                grading.update({"status": "interrupted", "current_error": "服务中断；不会自动重新评分",
                                "error": "服务中断；不会自动重新评分", "finished_at": now})
                changed = True
            if changed:
                run["updated_at"] = now
                atomic_write_json(path, run)


class ClientError(Exception):
    def __init__(self, status: HTTPStatus, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class EvalHandler(BaseHTTPRequestHandler):
    server_version = "HanGauge/1.0"

    @property
    def store(self) -> EvalStore:
        return self.server.store  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), fmt % args))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        download_name = None
        try:
            if path == "/":
                self.send_file(INDEX_PATH, "text/html; charset=utf-8")
                return
            if path == "/app.js":
                self.send_file(APP_PATH, "application/javascript; charset=utf-8")
                return
            if path == "/static-mode.js":
                self.send_bytes(b"window.HANGAUGE_STATIC=false;\n", "application/javascript; charset=utf-8")
                return
            with self.store.lock:
                if path == "/api/dataset":
                    value = self.store.public_dataset()
                elif path == "/api/runs":
                    value = self.store.list_runs()
                elif path == "/api/settings":
                    value = self.store.settings()
                elif path == "/api/status":
                    value = self.store.status()
                elif (run_id := run_id_from_path(path, "/api/runs/", "/cases")):
                    task_ids = parse_qs(parsed.query).get("task_id", [])
                    if len(task_ids) != 1 or not task_ids[0]:
                        raise ClientError(HTTPStatus.BAD_REQUEST, "task_id is required")
                    value = self.store.cases_for_task(run_id, task_ids[0])
                else:
                    run_id = run_id_from_path(path, "/api/runs/")
                    if run_id is None:
                        raise ClientError(HTTPStatus.NOT_FOUND, "not found")
                    value = self.store.load_run(run_id)
                    value = self.store.run_response(value, run_id)
                    download_name = f"{run_id}.json"
            self.send_json(value, download_name=download_name)
        except ClientError as exc:
            self.send_error_json(exc.status, exc.message)
        except FileNotFoundError:
            self.send_error_json(HTTPStatus.NOT_FOUND, "not found")
        except (OSError, json.JSONDecodeError, ValueError, RuntimeError) as exc:
            self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def do_POST(self) -> None:
        self.mutate("POST")

    def do_PATCH(self) -> None:
        self.mutate("PATCH")

    def do_DELETE(self) -> None:
        self.mutate("DELETE")

    def mutate(self, method: str) -> None:
        path = urlparse(self.path).path
        try:
            if self.store.read_only:
                raise ClientError(HTTPStatus.FORBIDDEN, "read-only server: mutations are disabled")
            self.require_same_origin()
            guarded_run_id = None
            if method == "POST":
                guarded_run_id = (
                    run_id_from_path(path, "/api/runs/", "/stop")
                    or run_id_from_path(path, "/api/runs/", "/grade")
                )
            elif method in {"PATCH", "DELETE"}:
                guarded_run_id = run_id_from_path(path, "/api/runs/")
            if guarded_run_id is not None:
                self.store.ensure_mutable_run(guarded_run_id)
            needs_body = method == "PATCH" or (method == "POST" and (path in {"/api/tests", "/api/settings"} or run_id_from_path(path, "/api/runs/", "/grade")))
            payload = self.read_json_body(optional=method == "POST" and bool(run_id_from_path(path, "/api/runs/", "/grade"))) if needs_body else {}
            status = HTTPStatus.OK
            with self.store.lock:
                is_run_response = False
                response_run_id = None
                if method == "POST" and path == "/api/tests":
                    value = self.store.start_test(payload)
                    status = HTTPStatus.ACCEPTED
                    is_run_response = True
                    response_run_id = value.get("id")
                elif method == "POST" and path == "/api/settings":
                    value = self.store.update_settings(payload)
                elif method == "POST" and (run_id := run_id_from_path(path, "/api/runs/", "/grade")):
                    value = self.store.start_grade(run_id, payload)
                    status = HTTPStatus.ACCEPTED
                    is_run_response = True
                    response_run_id = run_id
                elif method == "POST" and (run_id := run_id_from_path(path, "/api/runs/", "/stop")):
                    value = self.store.stop_test(run_id)
                    is_run_response = True
                    response_run_id = run_id
                elif method == "PATCH" and (run_id := run_id_from_path(path, "/api/runs/")):
                    value = self.store.update_run(run_id, payload)
                    is_run_response = True
                    response_run_id = run_id
                elif method == "DELETE" and (run_id := run_id_from_path(path, "/api/runs/")):
                    value = self.store.delete_run(run_id)
                else:
                    raise ClientError(HTTPStatus.NOT_FOUND, "not found")
                if is_run_response:
                    value = self.store.run_response(value, response_run_id)
            self.send_json(value, status=status)
        except ClientError as exc:
            self.send_error_json(exc.status, exc.message)
        except FileNotFoundError:
            self.send_error_json(HTTPStatus.NOT_FOUND, "run not found")
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def read_json_body(self, optional: bool = False) -> dict:
        length_header = self.headers.get("Content-Length")
        if length_header is None:
            if optional:
                return {}
            raise ClientError(HTTPStatus.LENGTH_REQUIRED, "Content-Length is required")
        try:
            length = int(length_header)
        except ValueError:
            raise ClientError(HTTPStatus.BAD_REQUEST, "Content-Length must be an integer")
        if length < 0 or length > MAX_BODY_BYTES:
            raise ClientError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body is too large")
        if length == 0 and optional:
            return {}
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except UnicodeDecodeError:
            raise ClientError(HTTPStatus.BAD_REQUEST, "request body must be UTF-8 JSON")
        except json.JSONDecodeError as exc:
            raise ClientError(HTTPStatus.BAD_REQUEST, f"malformed JSON: {exc.msg}")
        if not isinstance(payload, dict):
            raise ClientError(HTTPStatus.BAD_REQUEST, "request body must be a JSON object")
        return payload

    def require_same_origin(self) -> None:
        host = self.headers.get("Host", "")
        if not host:
            raise ClientError(HTTPStatus.FORBIDDEN, "Host header is required")
        expected = {f"http://{host}", f"https://{host}"}
        origin = self.headers.get("Origin")
        if origin and origin not in expected:
            raise ClientError(HTTPStatus.FORBIDDEN, "cross-origin POST is not allowed")
        referer = self.headers.get("Referer")
        if referer:
            parsed = urlparse(referer)
            referer_origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
            if referer_origin and referer_origin not in expected:
                raise ClientError(HTTPStatus.FORBIDDEN, "cross-origin POST is not allowed")

    def send_file(self, path: Path, content_type: str) -> None:
        self.send_bytes(path.read_bytes(), content_type)

    def send_bytes(self, body: bytes, content_type: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, value, status: HTTPStatus = HTTPStatus.OK, download_name: str | None = None) -> None:
        body = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if download_name:
            self.send_header("Content-Disposition", f'inline; filename="{download_name}"')
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status: HTTPStatus, message: str) -> None:
        self.send_json({"error": message}, status=status)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve the HanGauge local evaluation dashboard and JSON API.")
    parser.add_argument("--host", default="127.0.0.1", help="bind address; non-loopback requires --read-only")
    parser.add_argument("--port", type=int, default=8960, help="bind port")
    parser.add_argument("--data-dir", default=str(DEFAULT_RESULTS_DIR), help="directory for persistent run JSON files")
    parser.add_argument("--read-only", action="store_true", help="disable all mutations; required for non-loopback hosts")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.read_only and not is_loopback_host(args.host):
        raise SystemExit("Refusing writable non-loopback server; pass --read-only for public browsing")
    results_dir = Path(args.data_dir).expanduser().resolve()
    store = EvalStore(results_dir, read_only=args.read_only)
    server = ThreadingHTTPServer((args.host, args.port), EvalHandler)
    server.store = store  # type: ignore[attr-defined]
    host, port = server.server_address[:2]
    mode = "read-only" if args.read_only else "writable"
    print(f"HanGauge server listening on http://{host}:{port} results={results_dir} mode={mode}", file=sys.stderr, flush=True)
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
