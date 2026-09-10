from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from memory_harness.common import HarnessError, canonical_hash, hash_file, read_json, sha256_bytes, utc_now, write_json
from memory_harness.evaluate import evaluate
from memory_harness.plan import assemble_operations
from memory_harness.collect import collect
from memory_harness.process import DEFAULT_MAX_OUTPUT_BYTES


def _junction(target: Path, link: Path) -> None:
    """Create a Windows junction; no privilege is needed, unlike a symlink."""
    import _winapi
    _winapi.CreateJunction(str(target), str(link))


EVALUATOR_SCRIPT = r'''
import json
from pathlib import Path
import sys
import time

request = json.loads(sys.stdin.read())
case = request["case"]
arm = request["arm"]
mode = case.get("mode", "ok")
if mode == "exit":
    print("Expected failure", file=sys.stderr)
    sys.exit(7)
if mode == "bad_json":
    print("This is not JSON")
    sys.exit(0)
if mode == "timeout":
    time.sleep(2)
if mode == "bad_score":
    print(json.dumps({"score": float("nan")}))
    sys.exit(0)
if mode == "bad_checks":
    print(json.dumps({"score": 10, "checks": {"invalid": "true"}}))
    sys.exit(0)
if mode == "flood":
    sys.stdout.buffer.write(b"x" * (case["limit"] + 1))
    sys.exit(0)

marker = Path("evaluation-was-here.txt")
fresh = not marker.exists()
content = Path("CLAUDE.md").read_bytes().decode("utf-8")
rule_exists = Path(".claude/rules/mh-b.md").exists()
# The arm label is opaque, so decide from the copy's own contents. This is how a
# real evaluator has to work: it observes the project, not which arm it is in.
applied = "A reviewed additional rule." in content and rule_exists
marker.write_text("Evaluation mutates only its working copy.")
Path("CLAUDE.md").write_text("Changed by this isolated evaluation.")
checks = {"expected_files": True, "fresh_copy": fresh, "stable": True}
if case.get("regression"):
    checks["behavior"] = not applied
feedback = json.dumps({"cwd": str(Path.cwd()), "fresh": fresh,
                       "content": content, "arm_argument": sys.argv[-1],
                       "project_argument": sys.argv[-2], "case_id": case["id"],
                       "prompt": case.get("prompt", "")})
score = 20 if applied else 10
if case.get("score_regression") and applied:
    score = 5
print(json.dumps({"score": score, "checks": checks, "feedback": feedback}))
'''


class EvaluateTests(unittest.TestCase):
    """Run a real subprocess evaluator against paired temporary project copies."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        self.run = self.root / "run"
        self.run.mkdir()
        self.original = b"# Original instructions\r\nPreserve these bytes.\r\n"
        (self.project / "CLAUDE.md").write_bytes(self.original)
        (self.project / "source.py").write_text("print('original')\n")
        self.unrelated = self.project / "CLAUDE.local.md"
        self.unrelated.write_text("Existing local policy", encoding="utf-8")
        collected = self.root / "collection"
        collect(self.project, [], [], collected)
        shutil.copytree(collected, self.run, dirs_exist_ok=True)
        write_json(self.run / "candidates.json", {
            "schema_version": 1, "project": str(self.project),
            "inventory_sha256": hash_file(self.run / "inventory.json"),
            "created_at": utc_now(), "provider": "test-fixture",
            "records": [], "coverage": [], "errors": [], "complete": True,
        })
        write_json(self.run / "relations.json", {
            "schema_version": 1, "analysis_sha256": hash_file(self.run / "candidates.json"),
            "complete": True, "pairs_checked": 0, "relations": [], "errors": [],
        })
        before_hash = sha256_bytes(self.original)
        plan = {
            "schema_version": 1, "project": str(self.project), "complete": True,
            "inventory_sha256": hash_file(self.run / "inventory.json"),
            "analysis_sha256": hash_file(self.run / "candidates.json"),
            "relations_sha256": hash_file(self.run / "relations.json"),
            "items": [
                {"candidate_id": "a", "target": "claude_md", "relative_path": "CLAUDE.md",
                 "before_sha256": before_hash, "content": "A reviewed additional rule.\n",
                 "blocked_reasons": [], "needs_review": False},
                {"candidate_id": "b", "target": "rule", "relative_path": ".claude/rules/mh-b.md",
                 "before_sha256": None, "content": "Scoped additional rule.\n",
                 "blocked_reasons": [], "needs_review": False},
            ],
            "bases": {"CLAUDE.md": {"sha256": before_hash, "content": self.original.decode()},
                      ".claude/rules/mh-b.md": {"sha256": None, "content": ""}},
            "relations": [],
        }
        write_json(self.run / "plan.json", plan)
        approval = {
            "schema_version": 1, "project": str(self.project),
            "plan_sha256": hash_file(self.run / "plan.json"),
            "approved_at": utc_now(), "candidate_ids": ["a", "b"],
            "notes": "Reviewed for test", "operations": assemble_operations(plan, ["a", "b"]),
        }
        approval["approval_hash"] = canonical_hash(approval)
        write_json(self.run / "approval.json", approval)
        self.script = self.root / "evaluator.py"
        self.script.write_text(EVALUATOR_SCRIPT)
        self.config = self.root / "evaluator.json"
        write_json(self.config, {
            "argv": [sys.executable, "{config_dir}/evaluator.py", "{project}", "{arm}"],
            "kind": "integration-test-evaluator",
        })
        self.cases = self.root / "cases.json"
        self.set_cases([{"id": "first"}])

    def set_cases(self, cases):
        write_json(self.cases, {"cases": cases})

    def assert_original_project(self):
        self.assertEqual((self.project / "CLAUDE.md").read_bytes(), self.original)
        self.assertFalse((self.project / "evaluation-was-here.txt").exists())
        self.assertFalse((self.project / ".claude/rules/mh-b.md").exists())
        self.assertEqual((self.project / "source.py").read_text(), "print('original')\n")

    def test_real_evaluator_sees_blinded_arms_and_preserves_source(self):
        report = evaluate(self.run, self.cases, self.config)
        self.assertTrue(report["complete"])
        row = report["rows"][0]
        self.assertEqual(row["delta"], 10)
        self.assertEqual(report["summary"]["baseline_mean"], 10)
        self.assertEqual(report["summary"]["candidate_mean"], 20)
        self.assertEqual(report["summary"]["regressions"], [])
        observed = {}
        labels = set()
        for arm in ("baseline", "candidate"):
            self.assertEqual(row[arm]["status"], "ok")
            self.assertTrue(all(row[arm]["checks"].values()))
            feedback = json.loads(row[arm]["feedback"])
            observed[arm] = feedback
            self.assertEqual(feedback["project_argument"], feedback["cwd"])
            # The evaluator must not be able to tell the arms apart: neither the
            # argv label nor the working directory may name baseline/candidate.
            label = feedback["arm_argument"]
            self.assertNotIn(label, ("baseline", "candidate"))
            self.assertRegex(label, r"^[0-9a-f]{12}$")
            labels.add(label)
            self.assertNotIn("baseline", feedback["cwd"])
            self.assertNotIn("candidate", feedback["cwd"])
            self.assertIn(label, feedback["cwd"])
            self.assertNotEqual(feedback["cwd"], str(self.project))
            self.assertFalse(Path(feedback["cwd"]).exists(), "Temporary copies must be removed")
        self.assertEqual(len(labels), 2, "Each arm needs its own opaque label")
        self.assertEqual(observed["baseline"]["content"], self.original.decode())
        self.assertIn("A reviewed additional rule.", observed["candidate"]["content"])
        self.assertEqual(read_json(self.run / "evaluation.json"), report)
        self.assert_original_project()

    def test_every_case_repeat_and_arm_receives_a_fresh_copy(self):
        self.set_cases([{"id": "first"}, {"id": "second"}])
        report = evaluate(self.run, self.cases, self.config, repeats=2)
        self.assertTrue(report["complete"])
        self.assertEqual(len(report["rows"]), 4)
        self.assertEqual(report["summary"]["paired_runs"], 4)
        workdirs = set()
        for row in report["rows"]:
            for arm in ("baseline", "candidate"):
                feedback = json.loads(row[arm]["feedback"])
                self.assertTrue(row[arm]["checks"]["fresh_copy"])
                self.assertTrue(row[arm]["checks"]["expected_files"])
                workdirs.add(feedback["cwd"])
        self.assertEqual(len(workdirs), 8)
        self.assert_original_project()

    def test_evaluator_failures_leave_visible_incomplete_report(self):
        self.set_cases([{"id": "success"}, {"id": "failure", "mode": "exit"}])
        report = evaluate(self.run, self.cases, self.config)
        self.assertFalse(report["complete"])
        self.assertEqual(report["summary"]["paired_runs"], 1)
        failed = report["rows"][1]
        self.assertNotIn("delta", failed)
        for arm in ("baseline", "candidate"):
            self.assertEqual(failed[arm]["status"], "error")
            self.assertIn("7", failed[arm]["error"])
        self.assertFalse(read_json(self.run / "evaluation.json")["complete"])
        self.assert_original_project()

    def test_invalid_evaluator_output_is_not_scored(self):
        self.set_cases([{"id": mode, "mode": mode} for mode in ("bad_json", "bad_score", "bad_checks")])
        report = evaluate(self.run, self.cases, self.config)
        self.assertFalse(report["complete"])
        self.assertNotIn("summary", report)
        for row in report["rows"]:
            self.assertNotIn("delta", row)
            for arm in ("baseline", "candidate"):
                self.assertEqual(row[arm]["status"], "error")
        self.assert_original_project()

    def test_timeout_is_an_incomplete_error(self):
        self.set_cases([{"id": "timeout", "mode": "timeout"}])
        report = evaluate(self.run, self.cases, self.config, timeout=0.1)
        self.assertFalse(report["complete"])
        for arm in ("baseline", "candidate"):
            self.assertEqual(report["rows"][0][arm]["status"], "error")
            self.assertIn("timed out", report["rows"][0][arm]["error"])
        self.assert_original_project()

    def test_check_and_score_regressions_are_reported(self):
        self.set_cases([{"id": "check-regression", "regression": True},
                        {"id": "score-regression", "score_regression": True}])
        report = evaluate(self.run, self.cases, self.config)
        self.assertTrue(report["complete"])
        self.assertEqual(report["rows"][0]["check_regressions"], ["behavior"])
        self.assertEqual(report["rows"][1]["delta"], -5)
        self.assertEqual(set(report["summary"]["regressions"]), {"check-regression", "score-regression"})

    def test_stale_base_rejected_before_evaluation(self):
        (self.project / "CLAUDE.md").write_text("Later human edit")
        with self.assertRaises(HarnessError):
            evaluate(self.run, self.cases, self.config)
        self.assertEqual((self.project / "CLAUDE.md").read_text(), "Later human edit")
        self.assertFalse((self.run / "evaluation.json").exists())

    def test_unselected_harness_change_rejected_before_evaluator_runs(self):
        self.unrelated.write_text("Later local policy", encoding="utf-8")
        with patch("memory_harness.evaluate.run_process") as runner:
            with self.assertRaisesRegex(HarnessError, "再収集"):
                evaluate(self.run, self.cases, self.config)
            runner.assert_not_called()
        self.assert_original_project()
        self.assertEqual(self.unrelated.read_text(encoding="utf-8"), "Later local policy")

    def test_new_unselected_skill_rejected_before_evaluator_runs(self):
        added = self.project / ".claude/skills/human/SKILL.md"
        added.parent.mkdir(parents=True)
        added.write_text("New skill", encoding="utf-8")
        with patch("memory_harness.evaluate.run_process") as runner:
            with self.assertRaisesRegex(HarnessError, "追加"):
                evaluate(self.run, self.cases, self.config)
            runner.assert_not_called()
        self.assert_original_project()

    def test_missing_inventory_rejected_before_evaluator_runs(self):
        (self.run / "inventory.json").unlink()
        with patch("memory_harness.evaluate.run_process") as runner:
            with self.assertRaisesRegex(HarnessError, "inventory.json"):
                evaluate(self.run, self.cases, self.config)
            runner.assert_not_called()

    def test_approval_content_tampering_rejected_even_with_recomputed_hash(self):
        approval = read_json(self.run / "approval.json")
        approval["operations"][0]["after_content"] = "An unapproved replacement."
        approval["operations"][0]["after_sha256"] = sha256_bytes(b"An unapproved replacement.")
        approval.pop("approval_hash")
        approval["approval_hash"] = canonical_hash(approval)
        write_json(self.run / "approval.json", approval)
        with self.assertRaises(HarnessError):
            evaluate(self.run, self.cases, self.config)
        self.assert_original_project()

    def test_unrelated_file_symlink_is_rejected(self):
        outside = self.root / "outside.txt"
        outside.write_text("Outside data")
        try:
            (self.project / "linked.txt").symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("Symlink creation unavailable")
        with self.assertRaises(HarnessError):
            evaluate(self.run, self.cases, self.config)
        self.assertEqual(outside.read_text(), "Outside data")
        self.assertFalse((self.run / "evaluation.json").exists())
        self.assert_original_project()

    def test_unrelated_directory_symlink_is_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        try:
            (self.project / "linked-dir").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Symlink creation unavailable")
        with self.assertRaises(HarnessError):
            evaluate(self.run, self.cases, self.config)
        self.assertEqual(list(outside.iterdir()), [])
        self.assert_original_project()

    def test_junction_in_project_is_rejected_without_path_is_junction(self):
        if os.name != "nt":
            self.skipTest("Junctions exist only on Windows")
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.md").write_text("Outside data")
        try:
            _junction(outside, self.project / "linked-dir")
        except (OSError, ImportError, AttributeError):
            self.skipTest("Junction creation unavailable")
        # Python 3.11 has no Path.is_junction, so the check must not depend on it.
        with patch.object(Path, "is_junction", create=True,
                          side_effect=AssertionError("Path.is_junction is absent on Python 3.11")):
            with patch("memory_harness.evaluate.run_process") as runner:
                with self.assertRaisesRegex(HarnessError, "リンク"):
                    evaluate(self.run, self.cases, self.config)
                runner.assert_not_called()
        self.assertEqual((outside / "secret.md").read_text(), "Outside data")
        self.assertFalse((self.run / "evaluation.json").exists())
        self.assert_original_project()

    def test_flooding_evaluator_is_stopped_and_reported(self):
        self.set_cases([{"id": "flood", "mode": "flood", "limit": DEFAULT_MAX_OUTPUT_BYTES}])
        started = time.monotonic()
        report = evaluate(self.run, self.cases, self.config, timeout=60)
        self.assertLess(time.monotonic() - started, 30)
        self.assertFalse(report["complete"])
        for arm in ("baseline", "candidate"):
            self.assertEqual(report["rows"][0][arm]["status"], "error")
            self.assertIn("上限", report["rows"][0][arm]["error"])
        self.assert_original_project()

    def test_case_text_survives_the_evaluator_stdin_locale(self):
        # A Python evaluator decodes stdin with its locale (cp932 on Japanese
        # Windows); the case must still arrive intact.
        prompt = "日本語の課題文 — テスト"
        self.set_cases([{"id": "japanese", "prompt": prompt}])
        report = evaluate(self.run, self.cases, self.config)
        self.assertTrue(report["complete"])
        for arm in ("baseline", "candidate"):
            self.assertEqual(json.loads(report["rows"][0][arm]["feedback"])["prompt"], prompt)


if __name__ == "__main__":
    unittest.main()
