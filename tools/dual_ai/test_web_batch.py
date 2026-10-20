"""No network or models: three-cycle contract and failure boundaries."""
import copy
import json
import unittest
from pathlib import Path

from tools.dual_ai.web_batch import (
    HumanRequired, check_scope, parse_result, run_batch, validate_plan,
)

PLAN = json.loads((Path(__file__).parent / "web_batch_plan.json").read_text(encoding="utf-8"))


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

    def test_failed_github_publication_never_calls_next_model(self):
        bridge = Bridge(("publish_review", 1))
        with self.assertRaises(HumanRequired):
            run_batch(bridge, PLAN)
        self.assertNotIn(("repair", 1), bridge.events)

    def test_plan_requires_exactly_three_and_approved_roots(self):
        for key, value in (("round_focus", ["one"]), ("allowed_roots", ["tools/"]),
                           ("allowed_suffixes", [["bad"]])):
            plan = copy.deepcopy(PLAN)
            plan[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_plan(plan)

    def test_scope_blocks_traversal_protected_paths_and_dependencies(self):
        check_scope(["backend/tests/test_scan.py", "frontend/src/App.tsx"], PLAN)
        for path in ("backend/../tools/x.py", "tools/x.py", "frontend/package.json",
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


if __name__ == "__main__":
    unittest.main()
