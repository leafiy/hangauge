import copy
import json
import unittest
import tempfile
from pathlib import Path

import grading

from grading import GRADING_VERSION, grade_answers, summarize_evaluations


class DirectJudgmentTests(unittest.TestCase):
    def score(self, task_id, reference, candidate):
        item_id = f"{task_id}-case"
        dataset = {"inputs": {item_id: {"task_id": task_id}}}
        baseline = {"id": "accepted-reference", "answers": {item_id: {"raw": json.dumps(reference)}}}
        result = grade_answers(dataset, baseline, {item_id: {"raw": json.dumps(candidate)}})
        return result[item_id]["score"]

    def test_polarity_ignores_evidence_spelling_but_checks_label(self):
        reference = {
            "decision": "answered",
            "result": {"items": [{"unit_id": "u1", "decision": "answered", "polarity": "positive"}]},
            "evidence": [{"supports_id": "result.items[].polarity"}],
        }
        candidate = copy.deepcopy(reference)
        candidate["evidence"][0]["supports_id"] = "result.items[0].polarity"
        self.assertEqual(self.score("CN01", reference, candidate), 100)
        candidate["result"]["items"][0]["polarity"] = "negative"
        self.assertEqual(self.score("CN01", reference, candidate), 0)

    def test_multilabel_order_and_duplicates_do_not_change_judgment(self):
        reference = {"decision": "answered", "result": {"unit_labels": [{"unit_id": "u1", "labels": ["a", "b"]}]}}
        candidate = {"decision": "answered", "result": {"unit_labels": [{"unit_id": "u1", "labels": ["b", "a", "b"]}]}}
        self.assertEqual(self.score("CN08", reference, candidate), 100)
        candidate["result"]["unit_labels"][0]["labels"] = ["a"]
        self.assertEqual(self.score("CN08", reference, candidate), 0)

    def test_nli_direction_is_part_of_the_decision(self):
        reference = {"decision": "answered", "result": {"pairs": [{"pair_id": "p1", "label": "entails", "direction": "premise_to_hypothesis"}]}}
        candidate = copy.deepcopy(reference)
        candidate["result"]["pairs"][0]["direction"] = "hypothesis_to_premise"
        self.assertEqual(self.score("CN07", reference, candidate), 0)

    def test_policy_verdict_alone_cannot_hide_wrong_condition(self):
        reference = {"decision": "answered", "result": {"verdict": "eligible", "conditions": [{"id": "r1", "type": "required", "status": "met"}]}}
        candidate = copy.deepcopy(reference)
        candidate["result"]["conditions"][0]["status"] = "unknown"
        self.assertEqual(self.score("CN15", reference, candidate), 0)

    def test_stance_ids_are_incidental_but_holder_attribution_matters(self):
        reference = {
            "decision": "answered",
            "result": {"stances": [
                {"stance_id": "s1", "holder": {"name": "甲"}, "target": {"name": "方案"}, "stance": "support"},
                {"stance_id": "s2", "holder": {"name": "乙"}, "target": {"name": "方案"}, "stance": "oppose"},
            ]},
        }
        candidate = copy.deepcopy(reference)
        candidate["result"]["stances"].reverse()
        candidate["result"]["stances"][0]["stance_id"] = "local1"
        candidate["result"]["stances"][1]["stance_id"] = "local2"
        self.assertEqual(self.score("CN02", reference, candidate), 100)
        candidate["result"]["stances"][0]["holder"]["name"] = "甲"
        self.assertEqual(self.score("CN02", reference, candidate), 0)


class AggregateScoreTests(unittest.TestCase):
    def setUp(self):
        self.dataset = {
            "tasks": [{"id": "CN01"}, {"id": "CN12"}],
            "domains": [{"id": "D01"}, {"id": "D02"}],
            "inputs": {
                "a": {"task_id": "CN01", "domain_id": "D01"},
                "b": {"task_id": "CN01", "domain_id": "D02"},
                "c": {"task_id": "CN01", "domain_id": "D02"},
                "d": {"task_id": "CN12", "domain_id": "D01"},
            },
        }
        self.answers = {item_id: {"raw": "{}"} for item_id in self.dataset["inputs"]}
        self.evaluations = {
            item_id: {"score": 100 if item_id == "d" else 0, "grading_version": GRADING_VERSION}
            for item_id in self.dataset["inputs"]
        }

    def summarize(self):
        return summarize_evaluations(self.dataset, self.answers, self.evaluations)

    def test_overall_weights_tasks_equally_while_domains_average_their_questions(self):
        result = self.summarize()
        self.assertEqual(result["overall_score"], 50)
        self.assertEqual(result["by_domain"]["D01"]["score"], 50)
        self.assertEqual(result["by_domain"]["D02"]["score"], 0)

    def test_incomplete_run_keeps_only_completed_domain_scores(self):
        del self.evaluations["b"]
        result = self.summarize()
        self.assertIsNone(result["overall_score"])
        self.assertIsNone(result["by_domain"]["D02"]["score"])
        self.assertEqual(result["by_domain"]["D01"]["score"], 50)

    def test_zero_is_a_completed_score_not_missing(self):
        self.evaluations["d"]["score"] = 0
        result = self.summarize()
        self.assertEqual(result["overall_score"], 0)
        self.assertEqual(result["by_domain"]["D01"]["score"], 0)

    def test_invalid_stale_or_unanswered_grades_do_not_complete_a_domain(self):
        for change in ("invalid", "stale", "unanswered"):
            with self.subTest(change=change):
                self.setUp()
                if change == "invalid":
                    self.evaluations["a"]["score"] = 101
                elif change == "stale":
                    self.evaluations["a"]["grading_version"] = "previous"
                else:
                    del self.answers["a"]["raw"]
                result = self.summarize()
                self.assertIsNone(result["overall_score"])
                self.assertIsNone(result["by_domain"]["D01"]["score"])
                self.assertEqual(result["by_domain"]["D02"]["score"], 0)


class ProviderAndSecretTests(unittest.TestCase):
    def test_settings_reject_keys_but_runtime_candidate_config_accepts_transient_key(self):
        from evaluator import validate_candidate_config, validate_settings

        with self.assertRaises(ValueError):
            validate_settings({"candidate": {"api_key": "unit-not-public"}})
        config = validate_candidate_config({
            "base_url": "https://example.test/v1",
            "model": "demo",
            "api_key": "unit-transient-secret",
            "enable_thinking": False,
        })
        self.assertNotIn("api_key", config)
        self.assertFalse(config["enable_thinking"])

    def test_understanding_grade_requires_configured_grader(self):
        dataset = {"inputs": {"case": {"task_id": "CN12"}}}
        baseline = {"id": "base", "answers": {"case": {"raw": "{}"}}}
        with self.assertRaises(RuntimeError):
            grade_answers(dataset, baseline, {"case": {"raw": "{}"}})

    def test_model_grade_captures_provider_and_redacts_grader_secret(self):
        class FakeClient:
            def __init__(self, config, api_key):
                self.config = config
                self.api_key = api_key

            def request(self, messages, *, json_mode=False):
                return {
                    "raw": json.dumps({"score": 88, "reason": f"ok {self.api_key}"}, ensure_ascii=False),
                    "call": {"status": "completed", "model_used": self.api_key, "http_status": 200},
                }

        original = grading.OpenAIChatClient
        grading.OpenAIChatClient = FakeClient
        try:
            dataset = {"inputs": {"case": {"task_id": "CN12"}}}
            baseline = {"id": "base", "answers": {"case": {"raw": "{}"}}}
            result = grade_answers(
                dataset,
                baseline,
                {"case": {"raw": "{}"}},
                grader_config={"base_url": "https://grader.test/v1", "model": "judge", "temperature": 0, "max_tokens": 99},
                grader_api_key="unit-grader-secret",
            )
        finally:
            grading.OpenAIChatClient = original
        evaluation = result["case"]
        self.assertEqual(evaluation["score"], 88)
        self.assertEqual(evaluation["grading_provider"]["model"], "judge")
        serialized = json.dumps(evaluation, ensure_ascii=False)
        self.assertNotIn("unit-grader-secret", serialized)
        self.assertNotIn("model_used", evaluation.get("grading_call", {}))

    def test_partial_model_regrade_refuses_provider_mix(self):
        existing = {
            "old": {
                "method": "gateway",
                "grading_version": GRADING_VERSION,
                "grading_provider": {
                    "type": "openai_chat_completions",
                    "base_url": "https://a.test/v1",
                    "model": "judge-a",
                    "temperature": 0,
                    "max_tokens": 768,
                },
            }
        }
        with self.assertRaises(ValueError):
            grading.ensure_compatible_grading_provider(
                existing,
                {"new": {"raw": "{}"}},
                {
                    "type": "openai_chat_completions",
                    "base_url": "https://b.test/v1",
                    "model": "judge-b",
                    "temperature": 0,
                    "max_tokens": 768,
                },
            )


    def test_manual_grade_rejects_key_in_public_config_before_saving(self):
        from server import ClientError, EvalStore, atomic_write_json

        with tempfile.TemporaryDirectory() as tmp:
            store = EvalStore(Path(tmp))
            run = store.create_run({"name": "manual-grade-secret-check"})
            item_id = next(key for key, value in store.dataset["inputs"].items() if value["task_id"] == "CN12")
            run["answers"][item_id] = {"raw": store.baseline_run["answers"][item_id]["raw"]}
            path = store.run_path(run["id"])
            atomic_write_json(path, run)
            before = path.read_bytes()
            secret = "unit-manual-grader-secret"
            with self.assertRaises(ClientError) as caught:
                store.start_grade(run["id"], {"grader": {
                    "base_url": "https://example.test/v1",
                    "model": "judge-" + secret,
                    "api_key": secret,
                }})
            self.assertEqual(caught.exception.status, 400)
            self.assertEqual(path.read_bytes(), before)
            self.assertIsNone(store.active_run_id)


class BundledProtectionTests(unittest.TestCase):
    def test_read_only_store_ignores_custom_shadow_for_builtin_id(self):
        from server import EvalStore

        protected_id = "2df958eb-e16e-44c4-b537-73b0ece04b29"
        with tempfile.TemporaryDirectory() as tmp:
            shadow = Path(tmp) / f"{protected_id}.json"
            shadow.write_text(json.dumps({"id": protected_id, "name": "shadow", "answers": {}}), encoding="utf-8")
            store = EvalStore(Path(tmp), read_only=True)
            run = store.load_run(protected_id)
        self.assertNotEqual(run.get("name"), "shadow")
        self.assertTrue(store.run_response(run, protected_id)["protected"])
        self.assertFalse(store.run_response(run, protected_id)["is_baseline"])

    def test_only_manifest_baseline_receives_reference_assessment(self):
        from server import EvalStore

        with tempfile.TemporaryDirectory() as tmp:
            store = EvalStore(Path(tmp), read_only=True)
            baseline = store.run_response(store.load_run(store.baseline_id), store.baseline_id)
            other_id = next(run_id for run_id in store.protected_run_ids if run_id != store.baseline_id)
            other = store.run_response(store.load_run(other_id), other_id)
        self.assertTrue(baseline["is_baseline"])
        self.assertEqual(baseline["assessment"]["overall_score"], 100)
        self.assertTrue(other["protected"])
        self.assertFalse(other["is_baseline"])
        self.assertNotEqual(other["assessment"]["overall_score"], 100)


if __name__ == "__main__":
    unittest.main()
