"""No network or models: three-cycle contract and failure boundaries."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.dual_ai import web_batch_runtime

from tools.dual_ai.web_batch import (
    HumanRequired, check_scope, parse_result, run_batch, validate_plan,
)

PLAN = json.loads((Path(__file__).parent / "web_batch_plan.json").read_text(encoding="utf-8"))
# Legacy offline tests intentionally exercise Python code edits. The active batch
# plan can be narrower; never relax its production scope to satisfy a test.
PLAN["allowed_suffixes"] = [".py", ".ts", ".tsx", ".js", ".jsx", ".css", ".json"]


class Bridge:
    def __init__(self, stop=None):
        self.events = []
        self.comments = {}
        self.stop = stop

    def event(self, phase, number):
        self.events.append((phase, number))
        if self.stop == (phase, number):
            raise HumanRequired("operator decision")

    def develop(self, number, focus, last):
        self.event("develop", number)
        if number > 1:
            assert last == {"published": "repair", "number": number - 1}
        return {"published": "development", "number": number}

    def review(self, number, report):
        self.event("review", number)
        assert report == {"published": "development", "number": number}
        return {"published": "review", "number": number}

    def repair(self, number, review):
        self.event("repair", number)
        assert review == {"published": "review", "number": number}
        return {"published": "repair", "number": number}

    def publish(self, number, phase, data):
        self.event("publish_" + phase, number)
        ref = str(number) + phase
        self.comments[ref] = copy.deepcopy(data)
        return ref

    def fetch(self, ref):
        self.events.append(("fetch", ref))
        return copy.deepcopy(self.comments[ref])

    def round_complete(self, number, ref):
        self.event("complete", number)

    def finish(self, rounds, last):
        self.event("finish", rounds)
        return {"rounds": rounds, "last": last}


class BatchContractTests(unittest.TestCase):
    def test_three_complete_cycles_include_third_repair_then_exit(self):
        bridge = Bridge()
        result = run_batch(bridge, PLAN)
        self.assertEqual(result["rounds"], 3)
        phases = [x for x in bridge.events if x[0] in {"develop", "review", "repair", "complete", "finish"}]
        self.assertEqual(phases, [(p, n) for n in (1, 2, 3)
                                  for p in ("develop", "review", "repair", "complete")] + [("finish", 3)])
        self.assertEqual(sum(p == "fetch" for p, _ in bridge.events), 9)
        self.assertEqual(result["last"], {"published": "repair", "number": 3})

    def test_human_stop_at_each_model_phase_never_continues(self):
        for number in (1, 2, 3):
            for phase in ("develop", "review", "repair"):
                with self.subTest(number=number, phase=phase):
                    bridge = Bridge((phase, number))
                    with self.assertRaises(HumanRequired):
                        run_batch(bridge, PLAN)
                    self.assertEqual(bridge.events[-1], (phase, number))
                    self.assertFalse(any(p == "finish" for p, _ in bridge.events))

    def test_recovered_committed_development_is_published_once_without_model_replay(self):
        bridge = Bridge()
        verified_capture = {"published": "development", "number": 1}
        result = run_batch(bridge, PLAN, captured_development=verified_capture)
        self.assertEqual(result["rounds"], 3)
        self.assertNotIn(("develop", 1), bridge.events)
        self.assertEqual(bridge.events.count(("publish_development", 1)), 1)
        self.assertIn(("review", 1), bridge.events)
        self.assertIn(("repair", 3), bridge.events)

    def test_snapshot_reference_backend_is_readonly_context_not_edit_scope(self):
        plan = copy.deepcopy(PLAN)
        plan["allowed_suffixes"] = [".ts", ".tsx"]
        batch = web_batch_runtime.WebBatch.__new__(web_batch_runtime.WebBatch)
        batch.plan = plan
        batch.base_sha = "a" * 40
        batch.head = "b" * 40
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("frontend/src/scanApi.ts", "frontend/tests/scanApi.test.ts",
                         "frontend/src/App.tsx", "frontend/src/fileFilter.ts",
                         "backend/main.py"):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("safe content\n", encoding="utf-8")
            with (patch.object(web_batch_runtime, "ROOT", root),
                  patch.object(web_batch_runtime, "command",
                               return_value="frontend/src/scanApi.ts\nfrontend/tests/scanApi.test.ts"),
                  patch.object(web_batch_runtime, "collect_changed_paths", return_value=[])):
                snapshot = batch.snapshot(False)
            self.assertIn("backend/main.py", snapshot["files"])
            self.assertIn("frontend/src/scanApi.ts", snapshot["files"])
            with (patch.object(web_batch_runtime, "ROOT", root),
                  patch.object(web_batch_runtime, "command", return_value="backend/main.py"),
                  patch.object(web_batch_runtime, "collect_changed_paths", return_value=[])):
                with self.assertRaises(HumanRequired):
                    batch.snapshot(False)

    def test_failed_github_publication_never_calls_next_model(self):
        bridge = Bridge(("publish_review", 1))
        with self.assertRaises(HumanRequired):
            run_batch(bridge, PLAN)
        self.assertNotIn(("repair", 1), bridge.events)

    def test_plan_requires_exactly_three_and_approved_scope(self):
        for key, value in (
            ("round_focus", ["one"]),
            ("allowed_roots", ["tools/"]),
            ("allowed_files", ["frontend/tsconfig.json"]),
            ("allowed_suffixes", [["bad"]]),
        ):
            plan = copy.deepcopy(PLAN)
            plan[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_plan(plan)

    def test_scope_blocks_traversal_protected_paths_and_dependencies(self):
        check_scope(
            [
                "backend/tests/test_scan.py",
                "frontend/src/App.tsx",
                "frontend/package.json",
                "frontend/package-lock.json",
            ],
            PLAN,
        )
        for path in ("backend/../tools/x.py", "tools/x.py", "frontend/tsconfig.json",
                     "backend/.env", "backend/node_modules/x.js", "/backend/x.py",
                     "backend\\x.py"):
            with self.subTest(path=path), self.assertRaises(HumanRequired):
                check_scope([path], PLAN)

    def test_explicit_resume_starts_with_review_of_existing_report(self):
        bridge = Bridge()
        bridge.comments["prior"] = {"published": "development", "number": 1}
        result = run_batch(bridge, PLAN, development_report_ref="prior")
        self.assertEqual(result["rounds"], 3)
        self.assertNotIn(("develop", 1), bridge.events)
        self.assertNotIn(("publish_development", 1), bridge.events)
        phases = [x for x in bridge.events if x[0] in {"review", "repair", "complete"}]
        self.assertEqual(phases, [(p, n) for n in (1, 2, 3)
                                  for p in ("review", "repair", "complete")])
        self.assertEqual(bridge.events[0], ("fetch", "prior"))

    def test_resume_round_two_keeps_three_as_terminal_count(self):
        bridge = Bridge()
        bridge.comments["prior"] = {"published": "development", "number": 2}
        result = run_batch(bridge, PLAN, first_round=2, development_report_ref="prior")
        self.assertEqual(result["rounds"], 3)
        self.assertNotIn(("develop", 1), bridge.events)
        self.assertNotIn(("develop", 2), bridge.events)
        self.assertEqual([n for p, n in bridge.events if p == "complete"], [2, 3])


    def test_resume_final_review_only_repairs_then_stops(self):
        bridge = Bridge()
        bridge.comments["review3"] = {"published": "review", "number": 3}
        result = run_batch(bridge, PLAN, first_round=3, review_report_ref="review3")
        self.assertEqual(result["rounds"], 3)
        self.assertEqual(bridge.events[0], ("fetch", "review3"))
        self.assertFalse(any(phase in {"develop", "review"} for phase, _ in bridge.events))
        self.assertEqual([event for event in bridge.events if event[0] in {"repair", "complete", "finish"}],
                         [("repair", 3), ("complete", 3), ("finish", 3)])


class ResultTests(unittest.TestCase):
    def result(self, **changes):
        data = {"status": "done", "summary": "real result", "instructions": [], "question": ""}
        data.update(changes)
        return "analysis\nWEB_BATCH_RESULT: " + json.dumps(data)

    def test_valid_result(self):
        self.assertEqual(parse_result(self.result())["status"], "done")

    def test_needs_human_is_terminal(self):
        with self.assertRaisesRegex(HumanRequired, "credential"):
            parse_result(self.result(status="needs_human", question="credential"))

    def test_rejects_ambiguous_or_unbounded_result(self):
        for text in ("done", self.result() + "\nextra", self.result() + "\n" + self.result(),
                     self.result(status=[]), self.result(instructions=["x"] * 13)):
            with self.subTest(text=text[:60]), self.assertRaises(HumanRequired):
                parse_result(text)

    def test_presentation_wrappers_preserve_exact_result(self):
        data = {"status": "pass", "summary": "真实审查", "instructions": ["保留意见"], "question": ""}
        raw = json.dumps(data, ensure_ascii=False)
        fence = chr(96) * 3
        for text in ("分析结束。WEB_BATCH_RESULT: " + raw,
                     "**WEB_BATCH_RESULT:** " + raw,
                     "**WEB_BATCH_RESULT**：" + raw,
                     "`WEB_BATCH_RESULT`: " + raw,
                     "WEB_BATCH_RESULT:\n" + fence + "json\n" + raw + "\n" + fence,
                     fence + "json\nWEB_BATCH_RESULT: " + raw + "\n" + fence,
                     raw, fence + "json\n" + raw + "\n" + fence):
            with self.subTest(text=text[:45]):
                self.assertEqual(parse_result(text), data)

    def test_human_request_survives_wrappers_and_trailing_prose(self):
        text = self.result(status="needs_human", question="需要授权") + "\n格式多了一行"
        with self.assertRaisesRegex(HumanRequired, "需要授权"):
            parse_result(text)

    def test_conflicting_results_and_duplicate_keys_remain_rejected(self):
        with self.assertRaises(HumanRequired):
            parse_result(self.result(status="pass") + "\n" + self.result(status="changes_requested"))
        text = 'WEB_BATCH_RESULT: {"status":"pass","status":"done","summary":"x","instructions":[],"question":""}'
        with self.assertRaises(HumanRequired):
            parse_result(text)


if __name__ == "__main__":
    unittest.main()
