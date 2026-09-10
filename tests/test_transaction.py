from __future__ import annotations

import copy
import os
import shutil
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from memory_harness.common import (
    HarnessError, canonical_hash, hash_file, read_json, safe_project_path, sha256_bytes, utc_now, write_json,
)
from memory_harness import transaction
from memory_harness.collect import collect


def _junction(target: Path, link: Path) -> None:
    """Create a Windows junction; no privilege is needed, unlike a symlink."""
    import _winapi
    _winapi.CreateJunction(str(target), str(link))


class TransactionTests(unittest.TestCase):
    """Exercise file safety with a fixed, trusted planner output."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        self.run = self.root / "run"
        self.run.mkdir()
        self.original = b"# Existing instructions\r\nKeep the exact bytes.\r\n"
        (self.project / "CLAUDE.md").write_bytes(self.original)
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
        self.expected = [
            self.operation("CLAUDE.md", self.original, self.original.decode() + "\nNew rule.\n", "a"),
            self.operation(".claude/rules/mh-b.md", None, "Scoped rule.\n", "b"),
        ]
        self.fixture()
        # Planner assembly has its own tests. Keeping this output fixed also
        # proves that a modified approval cannot supply different operations.
        patcher = patch("memory_harness.plan.assemble_operations", side_effect=lambda _plan, _ids: copy.deepcopy(self.expected))
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def operation(path, before, after, candidate):
        return {
            "relative_path": path,
            "before_sha256": sha256_bytes(before) if before is not None else None,
            "after_sha256": sha256_bytes(after.encode()),
            "after_content": after,
            "candidate_ids": [candidate],
        }

    def fixture(self):
        plan = {
            "schema_version": 1, "project": str(self.project), "complete": True,
            "inventory_sha256": hash_file(self.run / "inventory.json"),
            "analysis_sha256": hash_file(self.run / "candidates.json"),
            "relations_sha256": hash_file(self.run / "relations.json"),
            "items": [{"candidate_id": item_id, "blocked_reasons": []} for item_id in ("a", "b")],
        }
        write_json(self.run / "plan.json", plan)
        approval = {
            "schema_version": 1, "project": str(self.project),
            "plan_sha256": hash_file(self.run / "plan.json"),
            "approved_at": utc_now(), "candidate_ids": ["a", "b"],
            "notes": "Reviewed", "operations": copy.deepcopy(self.expected),
        }
        self.save_approval(approval)

    def save_approval(self, approval):
        approval.pop("approval_hash", None)
        approval["approval_hash"] = canonical_hash(approval)
        write_json(self.run / "approval.json", approval)

    def assert_original(self):
        self.assertEqual((self.project / "CLAUDE.md").read_bytes(), self.original)
        self.assertFalse((self.project / ".claude/rules/mh-b.md").exists())

    def write_evaluation(self, *, mean_delta=5, regressions=(), complete=True,
                         approval_hash=None):
        """Place an evaluation report of a given verdict beside the approval."""
        if approval_hash is None:
            approval_hash = read_json(self.run / "approval.json")["approval_hash"]
        write_json(self.run / "evaluation.json", {
            "schema_version": 1, "approval_hash": approval_hash,
            "kind": "test-evaluator", "complete": complete, "rows": [],
            "summary": {"paired_runs": 1, "baseline_mean": 10,
                        "candidate_mean": 10 + mean_delta, "mean_delta": mean_delta,
                        "delta_stdev": None, "regressions": list(regressions)},
        })

    def test_apply_records_a_passing_evaluation(self):
        self.write_evaluation(mean_delta=5)
        journal = transaction.apply_approval(self.run)
        self.assertEqual(journal["state"], "applied")
        self.assertEqual(journal["evaluation"]["state"], "passed")

    def test_apply_without_evaluation_still_proceeds(self):
        self.assertFalse((self.run / "evaluation.json").exists())
        journal = transaction.apply_approval(self.run)
        self.assertEqual(journal["state"], "applied")
        self.assertEqual(journal["evaluation"]["state"], "not_run")

    def test_worse_evaluation_blocks_apply(self):
        for verdict in ({"mean_delta": -1}, {"regressions": ["case-1"]}, {"complete": False}):
            with self.subTest(**verdict):
                self.write_evaluation(**verdict)
                with self.assertRaisesRegex(HarnessError, "評価"):
                    transaction.apply_approval(self.run)
                self.assert_original()
                self.assertFalse((self.run / "transaction.json").exists())

    def test_stale_evaluation_blocks_apply(self):
        self.write_evaluation(approval_hash="0" * 64)
        with self.assertRaisesRegex(HarnessError, "別の承認"):
            transaction.apply_approval(self.run)
        self.assert_original()

    def test_malformed_evaluation_report_is_rejected(self):
        write_json(self.run / "evaluation.json", [])
        with self.assertRaisesRegex(HarnessError, "evaluation.json"):
            transaction.apply_approval(self.run)
        self.assert_original()
        self.assertFalse((self.run / "transaction.json").exists())

    def test_override_needs_notes_and_is_recorded(self):
        self.write_evaluation(mean_delta=-3)
        with self.assertRaisesRegex(HarnessError, "理由"):
            transaction.apply_approval(self.run, ignore_evaluation=True)
        self.assert_original()
        journal = transaction.apply_approval(
            self.run, ignore_evaluation=True, override_notes="退行は無関係な課題によるものと確認した")
        self.assertEqual(journal["state"], "applied")
        self.assertEqual(journal["evaluation"]["state"], "override")
        self.assertIn("スコアが低下", " ".join(journal["evaluation"]["reasons"]))
        self.assertTrue(journal["evaluation"]["override_notes"])

    def test_apply_idempotent_and_rollback_exact_bytes(self):
        result = transaction.apply_approval(self.run)
        self.assertEqual(result["state"], "applied")
        self.assertEqual(transaction.apply_approval(self.run), result)
        self.assertEqual(transaction.rollback(self.run)["state"], "rolled_back")
        self.assert_original()
        self.assertFalse((self.project / ".claude").exists())
        self.assertEqual(transaction.rollback(self.run)["state"], "rolled_back")
        with self.assertRaises(HarnessError):
            transaction.apply_approval(self.run)

    def test_stale_base_preflights_all_files_before_writing(self):
        target = self.project / ".claude/rules/mh-b.md"
        target.parent.mkdir(parents=True)
        target.write_text("A human created this.")
        with self.assertRaisesRegex(HarnessError, "再収集"):
            transaction.apply_approval(self.run)
        self.assertEqual((self.project / "CLAUDE.md").read_bytes(), self.original)
        self.assertEqual(target.read_text(), "A human created this.")
        self.assertFalse((self.run / "transaction.json").exists())

    def test_unselected_harness_change_after_approval_blocks_first_apply(self):
        self.unrelated.write_text("Later local policy", encoding="utf-8")
        with self.assertRaisesRegex(HarnessError, "再収集"):
            transaction.apply_approval(self.run)
        self.assert_original()
        self.assertEqual(self.unrelated.read_text(encoding="utf-8"), "Later local policy")
        self.assertFalse((self.run / "transaction.json").exists())

    def test_new_unselected_harness_after_approval_blocks_first_apply(self):
        added = self.project / ".claude/skills/human/SKILL.md"
        added.parent.mkdir(parents=True)
        added.write_text("New skill", encoding="utf-8")
        with self.assertRaisesRegex(HarnessError, "追加"):
            transaction.apply_approval(self.run)
        self.assert_original()
        self.assertEqual(added.read_text(encoding="utf-8"), "New skill")

    def test_missing_inventory_is_not_exempted(self):
        (self.run / "inventory.json").unlink()
        with self.assertRaisesRegex(HarnessError, "inventory.json"):
            transaction.apply_approval(self.run)
        self.assert_original()

    def test_applied_idempotency_and_rollback_allow_unrelated_harness_changes(self):
        applied = transaction.apply_approval(self.run)
        self.unrelated.write_text("Later local policy", encoding="utf-8")
        self.assertEqual(transaction.apply_approval(self.run), applied)
        self.assertEqual(transaction.rollback(self.run)["state"], "rolled_back")
        self.assert_original()
        self.assertEqual(self.unrelated.read_text(encoding="utf-8"), "Later local policy")

    def test_partial_write_failure_restores_all_before_images(self):
        real_write = transaction.atomic_write
        failed = False

        def fail_once(path, data):
            nonlocal failed
            if Path(path).name == "mh-b.md" and not failed:
                failed = True
                raise OSError("Simulated disk write failure")
            real_write(path, data)

        with patch.object(transaction, "atomic_write", side_effect=fail_once):
            with self.assertRaisesRegex(HarnessError, "original files restored"):
                transaction.apply_approval(self.run)
        self.assert_original()
        self.assertEqual(read_json(self.run / "transaction.json")["state"], "rolled_back")
        self.assertEqual(transaction.apply_approval(self.run)["state"], "applied")

    def test_crash_after_replace_is_recovered_on_rerun(self):
        real_write = transaction.atomic_write

        def crash_after_write(path, data):
            real_write(path, data)
            if Path(path).name == "mh-b.md":
                raise KeyboardInterrupt("Simulated process termination")

        with patch.object(transaction, "atomic_write", side_effect=crash_after_write):
            with self.assertRaises(KeyboardInterrupt):
                transaction.apply_approval(self.run)
        self.assertEqual(read_json(self.run / "transaction.json")["state"], "applying")
        result = transaction.apply_approval(self.run)
        self.assertEqual(result["state"], "applied")
        transaction.rollback(self.run)
        self.assert_original()

    def test_recovery_restores_before_images_then_rejects_unrelated_changes(self):
        real_write = transaction.atomic_write

        def crash_after_write(path, data):
            real_write(path, data)
            if Path(path).name == "mh-b.md":
                raise KeyboardInterrupt("Simulated process termination")

        with patch.object(transaction, "atomic_write", side_effect=crash_after_write):
            with self.assertRaises(KeyboardInterrupt):
                transaction.apply_approval(self.run)
        self.unrelated.write_text("Policy changed during interruption", encoding="utf-8")
        with self.assertRaisesRegex(HarnessError, "再収集"):
            transaction.apply_approval(self.run)
        self.assert_original()
        self.assertEqual(read_json(self.run / "transaction.json")["state"], "rolled_back")
        self.assertEqual(self.unrelated.read_text(encoding="utf-8"), "Policy changed during interruption")

    def test_rollback_conflict_does_not_partially_restore_other_files(self):
        transaction.apply_approval(self.run)
        changed = self.project / ".claude/rules/mh-b.md"
        changed.write_text("Later human edit.")
        with self.assertRaisesRegex(HarnessError, "preserved later edits"):
            transaction.rollback(self.run)
        self.assertEqual(changed.read_text(), "Later human edit.")
        self.assertEqual(hash_file(self.project / "CLAUDE.md"), self.expected[0]["after_sha256"])
        self.assertEqual(read_json(self.run / "transaction.json")["state"], "rollback_conflict")
        # newline="" keeps the exact bytes: the default translates "\n" to
        # "\r\n" on Windows, so the restored file would no longer match.
        changed.write_text(self.expected[1]["after_content"], newline="")
        self.assertEqual(transaction.rollback(self.run)["state"], "rolled_back")
        self.assert_original()

    def test_reapply_preserves_human_changes(self):
        transaction.apply_approval(self.run)
        (self.project / "CLAUDE.md").write_text("A later edit")
        with self.assertRaisesRegex(HarnessError, "later edits"):
            transaction.apply_approval(self.run)
        self.assertEqual((self.project / "CLAUDE.md").read_text(), "A later edit")

    def test_approval_checksum_tampering_is_rejected(self):
        approval = read_json(self.run / "approval.json")
        approval["operations"][0]["after_content"] = "Tampered."
        write_json(self.run / "approval.json", approval)
        with self.assertRaisesRegex(HarnessError, "checksum"):
            transaction.apply_approval(self.run)
        self.assert_original()

    def test_recomputed_checksum_cannot_substitute_plan_operations(self):
        approval = read_json(self.run / "approval.json")
        approval["operations"][0]["after_content"] = "Tampered."
        approval["operations"][0]["after_sha256"] = sha256_bytes(b"Tampered.")
        self.save_approval(approval)
        with self.assertRaisesRegex(HarnessError, "selected plan"):
            transaction.apply_approval(self.run)
        self.assert_original()

    def test_plan_change_invalidates_approval(self):
        plan = read_json(self.run / "plan.json")
        plan["complete"] = False
        write_json(self.run / "plan.json", plan)
        with self.assertRaisesRegex(HarnessError, "Plan changed"):
            transaction.apply_approval(self.run)
        self.assert_original()

    def test_tampered_backup_prevents_rollback(self):
        transaction.apply_approval(self.run)
        (self.run / "backups/0000.before").write_bytes(b"Corrupted")
        with self.assertRaisesRegex(HarnessError, "backup"):
            transaction.rollback(self.run)
        self.assertEqual(hash_file(self.project / "CLAUDE.md"), self.expected[0]["after_sha256"])
        self.assertTrue((self.project / ".claude/rules/mh-b.md").exists())

    def test_path_attacks_rejected_on_posix_and_windows(self):
        attacks = ["../escape.md", "/tmp/escape.md", "C:/escape.md", "C:escape.md",
                   "..\\escape.md", "\\\\server\\share\\evil.md", "rules/file:stream",
                   "rules/../../evil.md", "rules//evil.md", "NUL.md", "rules/trailing. "]
        for path in attacks:
            with self.subTest(path=path):
                self.expected[1]["relative_path"] = path
                self.fixture()
                with self.assertRaises(HarnessError):
                    transaction.apply_approval(self.run)
                self.assertEqual((self.project / "CLAUDE.md").read_bytes(), self.original)

    def test_parent_symlink_rejected_without_touching_destination(self):
        outside = self.root / "outside"
        outside.mkdir()
        link = self.project / ".claude"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Symlink creation unavailable on this system")
        with self.assertRaises(HarnessError):
            transaction.apply_approval(self.run)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual((self.project / "CLAUDE.md").read_bytes(), self.original)

    def test_parent_junction_rejected_without_touching_destination(self):
        if os.name != "nt":
            self.skipTest("Junctions exist only on Windows")
        outside = self.root / "outside"
        outside.mkdir()
        try:
            _junction(outside, self.project / ".claude")
        except (OSError, ImportError, AttributeError):
            self.skipTest("Junction creation unavailable")
        # Python 3.11 has no Path.is_junction, so the check must not depend on it.
        with patch.object(Path, "is_junction", create=True,
                          side_effect=AssertionError("Path.is_junction is absent on Python 3.11")):
            with self.assertRaisesRegex(HarnessError, "リンク"):
                safe_project_path(self.project, ".claude/rules/mh-b.md")
            with self.assertRaises(HarnessError):
                transaction.apply_approval(self.run)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual((self.project / "CLAUDE.md").read_bytes(), self.original)

    def test_rollback_keeps_new_unrelated_directory_contents(self):
        transaction.apply_approval(self.run)
        added = self.project / ".claude/rules/human.md"
        added.write_text("Keep this")
        transaction.rollback(self.run)
        self.assert_original()
        self.assertEqual(added.read_text(), "Keep this")

    def test_project_lock_excludes_concurrent_runs_and_releases(self):
        with transaction._project_lock(self.project):
            with self.assertRaisesRegex(HarnessError, "project lock"):
                transaction.apply_approval(self.run)
        self.assertEqual(transaction.apply_approval(self.run)["state"], "applied")


if __name__ == "__main__":
    unittest.main()
