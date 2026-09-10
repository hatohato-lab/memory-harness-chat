"""Behavioral safety tests for proposal staging and byte-bound approval."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from memory_harness.common import HarnessError, canonical_hash, hash_file, write_json
from memory_harness.collect import collect
from memory_harness.plan import approve_plan, assemble_operations, make_plan


def candidate(cid="candidate1", target="rule", **changes):
    value = {
        "id": cid, "title": "失敗を再発させない", "condition": "Python のテストを変更するとき",
        "action": "テストの失敗理由を確認してから修正する。", "exceptions": ["対象テストが存在しない場合は報告する"],
        "rationale": "以前、失敗を確認せずに期待値だけを書き換えて問題を隠したため。",
        "target": target, "paths": ["tests/**/*.py"], "steps": ["失敗を再現する", "原因を修正する", "同じテストを再実行する"],
        "authority": "explicit_user", "confidence": 0.92,
        "evidence": [{"source_id": "source1", "start_line": 3, "end_line": 3, "quote": "失敗理由を確認してください。"}],
        "source_kind": "memory", "needs_review": False, "issues": [],
    }
    value.update(changes)
    return value


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.run = self.root / "run"
        self.run.mkdir()
        self.collection_count = 0
        self.inventory = {"schema_version": 1, "project": str(self.project), "sources": [], "chunks": []}
        write_json(self.run / "inventory.json", self.inventory)

    def analysis(self, records, complete=True):
        self.collection_count += 1
        collected = self.root / f"collection-{self.collection_count}"
        self.inventory = collect(self.project, [], [], collected)
        shutil.copytree(collected, self.run, dirs_exist_ok=True)
        value = {"schema_version": 1, "project": str(self.project), "inventory_sha256": hash_file(self.run / "inventory.json"),
                 "records": records, "coverage": [], "errors": [], "complete": complete}
        write_json(self.run / "candidates.json", value)
        write_json(self.run / "relations.json", {"schema_version": 1,
                   "analysis_sha256": hash_file(self.run / "candidates.json"), "complete": True,
                   "pairs_checked": len(records) * (len(records) - 1) // 2, "errors": [], "relations": []})

    def relations(self, left, right, relation="conflict", complete=True):
        value = {"schema_version": 1, "analysis_sha256": hash_file(self.run / "candidates.json"), "complete": complete,
                 "pairs_checked": 1, "errors": [], "relations": [{"left_id": left, "right_id": right,
                 "relation": relation, "reason": "同じ条件で異なる指示", "example": "同一のテスト変更", "preferred_id": None}]}
        write_json(self.run / "relations.json", value)

    def test_subset_claude_merge_preserves_original_bytes(self):
        original = b"# My own instructions\r\n\r\nKeep this exact."
        (self.project / "CLAUDE.md").write_bytes(original)
        self.analysis([candidate("first", "claude_md", action="FIRST ACTION"), candidate("second", "claude_md", action="SECOND ACTION")])
        plan = make_plan(self.run)
        approval = approve_plan(self.run, ["second"])
        self.assertEqual((self.project / "CLAUDE.md").read_bytes(), original)
        self.assertEqual(len(approval["operations"]), 1)
        after = approval["operations"][0]["after_content"].encode("utf-8")
        self.assertTrue(after.startswith(original))
        self.assertIn(b"SECOND ACTION", after)
        self.assertNotIn(b"FIRST ACTION", after)
        self.assertEqual(approval["operations"], assemble_operations(plan, ["second"]))
        self.assertEqual(approval["approval_hash"], canonical_hash({k: v for k, v in approval.items() if k != "approval_hash"}))
        self.assertIn("\\ No newline at end of file", (self.run / "diffs/second.diff").read_text(encoding="utf-8"))

    def test_multiple_claude_candidates_form_one_deterministic_operation(self):
        self.analysis([candidate("first", "claude_md"), candidate("second", "claude_md")])
        make_plan(self.run)
        approval = approve_plan(self.run, ["second", "first"])
        self.assertEqual(len(approval["operations"]), 1)
        self.assertEqual(approval["operations"][0]["candidate_ids"], ["first", "second"])
        self.assertFalse((self.project / "CLAUDE.md").exists())

    def test_owned_output_collision_never_overwrites_existing(self):
        target = self.project / ".claude/rules/mh-candidate1.md"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"human file\r\n")
        self.analysis([candidate()])
        plan = make_plan(self.run)
        self.assertTrue(plan["items"][0]["blocked_reasons"])
        with self.assertRaises(HarnessError):
            approve_plan(self.run, ["candidate1"])
        self.assertEqual(target.read_bytes(), b"human file\r\n")

    def test_stale_project_file_is_rejected(self):
        target = self.project / "CLAUDE.md"
        target.write_text("original", encoding="utf-8")
        self.analysis([candidate(target="claude_md")])
        make_plan(self.run)
        target.write_text("human changed", encoding="utf-8")
        with self.assertRaisesRegex(HarnessError, "再収集"):
            approve_plan(self.run, ["candidate1"])
        self.assertEqual(target.read_text(encoding="utf-8"), "human changed")
        self.assertFalse((self.run / "approval.json").exists())

    def test_new_target_created_after_plan_is_rejected(self):
        self.analysis([candidate()])
        make_plan(self.run)
        target = self.project / ".claude/rules/mh-candidate1.md"
        target.parent.mkdir(parents=True)
        target.write_text("another person", encoding="utf-8")
        with self.assertRaises(HarnessError):
            approve_plan(self.run, ["candidate1"])

    def test_new_rule_or_skill_invalidates_analysis_before_plan_and_approval(self):
        for relative in [".claude/rules/human.md", ".claude/skills/human/SKILL.md"]:
            for when in ["plan", "approve"]:
                with self.subTest(relative=relative, when=when):
                    target = self.project / relative
                    self.analysis([candidate(target="claude_md")])
                    if when == "approve":
                        make_plan(self.run)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text("New user instruction", encoding="utf-8")
                    with self.assertRaisesRegex(HarnessError, "再収集"):
                        make_plan(self.run) if when == "plan" else approve_plan(self.run, ["candidate1"])
                    target.unlink()

    def test_harness_edit_or_removal_before_planning_requires_recollection(self):
        target = self.project / "CLAUDE.md"
        target.write_text("Original instruction", encoding="utf-8")
        self.analysis([candidate()])
        target.write_text("Changed instruction", encoding="utf-8")
        with self.assertRaisesRegex(HarnessError, "更新"):
            make_plan(self.run)
        target.unlink()
        with self.assertRaisesRegex(HarnessError, "削除"):
            make_plan(self.run)

    def test_unselected_harness_edit_invalidates_approval(self):
        reference = self.project / "CLAUDE.local.md"
        reference.write_text("Keep local policies", encoding="utf-8")
        self.analysis([candidate()])
        make_plan(self.run)
        reference.write_text("Changed local policies", encoding="utf-8")
        with self.assertRaisesRegex(HarnessError, "再収集"):
            approve_plan(self.run, ["candidate1"])

    def test_changed_analysis_and_changed_relations_are_rejected(self):
        self.analysis([candidate(), candidate("candidate2")])
        make_plan(self.run)
        self.relations("candidate1", "candidate2")
        with self.assertRaisesRegex(HarnessError, "relations.json"):
            approve_plan(self.run, ["candidate1"])
        make_plan(self.run)
        edited = json.loads((self.run / "candidates.json").read_text(encoding="utf-8"))
        edited["records"][0]["action"] = "Different action"
        write_json(self.run / "candidates.json", edited)
        with self.assertRaisesRegex(HarnessError, "candidates.json"):
            approve_plan(self.run, ["candidate1"])

    def test_conflict_duplicate_and_supersedes_require_one_side(self):
        for relationship in ["conflict", "duplicate", "supersedes"]:
            with self.subTest(relationship=relationship):
                self.analysis([candidate("left"), candidate("right")])
                self.relations("left", "right", relationship)
                make_plan(self.run)
                with self.assertRaisesRegex(HarnessError, "同時"):
                    approve_plan(self.run, ["left", "right"])
                approval = approve_plan(self.run, ["right"])
                self.assertEqual(approval["candidate_ids"], ["right"])

    def test_harness_references_are_not_candidates_and_conflicts_block(self):
        self.analysis([candidate("existing", source_kind="harness"), candidate("fresh")])
        self.relations("existing", "fresh")
        plan = make_plan(self.run)
        self.assertEqual([item["candidate_id"] for item in plan["items"]], ["fresh"])
        self.assertEqual(plan["excluded_reference_count"], 1)
        with self.assertRaisesRegex(HarnessError, "既存"):
            approve_plan(self.run, ["fresh"], notes="cannot resolve by notes")

    def test_incomplete_requires_both_override_and_notes(self):
        self.analysis([candidate()], complete=False)
        make_plan(self.run)
        with self.assertRaises(HarnessError):
            approve_plan(self.run, ["candidate1"])
        with self.assertRaises(HarnessError):
            approve_plan(self.run, ["candidate1"], allow_incomplete=True)
        with self.assertRaises(HarnessError):
            approve_plan(self.run, ["candidate1"], notes="one is enough")
        self.assertTrue(approve_plan(self.run, ["candidate1"], allow_incomplete=True, notes="未処理部分を別途確認した")["operations"])

    def test_missing_relations_is_incomplete_when_comparison_is_needed(self):
        self.analysis([candidate("one"), candidate("two")])
        (self.run / "relations.json").unlink()
        plan = make_plan(self.run)
        self.assertFalse(plan["complete"])
        self.assertTrue(any("意味照合未実施" in warning for warning in plan["warnings"]))
        with self.assertRaises(HarnessError):
            approve_plan(self.run, ["one"])
        self.assertTrue(approve_plan(self.run, ["one"], allow_incomplete=True, notes="関係は手動で確認した")["operations"])

    def test_missing_relations_is_allowed_with_zero_eligible_pairs(self):
        self.analysis([candidate()])
        (self.run / "relations.json").unlink()
        self.assertTrue(make_plan(self.run)["complete"])
        self.assertTrue(approve_plan(self.run, ["candidate1"])["operations"])

    def test_needs_review_and_overlap_need_explanation(self):
        self.analysis([candidate("left", needs_review=True), candidate("right")])
        self.relations("left", "right", "overlap")
        make_plan(self.run)
        with self.assertRaisesRegex(HarnessError, "notes"):
            approve_plan(self.run, ["right"])
        self.assertTrue(approve_plan(self.run, ["left", "right"], notes="適用条件が異なることを確認")["operations"])

    def test_external_authority_is_blocked_even_with_notes(self):
        self.analysis([candidate(authority="external", needs_review=True)])
        make_plan(self.run)
        with self.assertRaisesRegex(HarnessError, "外部"):
            approve_plan(self.run, ["candidate1"], notes="external accepted")

    def test_rule_frontmatter_quotes_globs_as_json_and_blocks_traversal(self):
        paths = ['src/**/file"name.py', "src/{one,two}/**/*.py"]
        self.analysis([candidate(paths=paths)])
        plan = make_plan(self.run)
        frontmatter_value = plan["items"][0]["content"].splitlines()[1].removeprefix("paths: ")
        self.assertEqual(json.loads(frontmatter_value), paths)
        self.assertFalse(plan["items"][0]["blocked_reasons"])
        self.analysis([candidate(paths=["../private/**"])])
        plan = make_plan(self.run)
        self.assertTrue(plan["items"][0]["blocked_reasons"])

    def test_broad_glob_needs_review_and_no_paths_blocks(self):
        self.analysis([candidate(paths=["**/*"])])
        plan = make_plan(self.run)
        self.assertTrue(plan["items"][0]["needs_review"])
        with self.assertRaises(HarnessError):
            approve_plan(self.run, ["candidate1"])
        self.analysis([candidate(paths=[])])
        self.assertTrue(make_plan(self.run)["items"][0]["blocked_reasons"])

    def test_skills_and_hooks_stay_drafts_until_apply(self):
        self.analysis([candidate("skill1", "skill", title='Quote " title'), candidate("hook1", "hook_spec")])
        plan = make_plan(self.run)
        skill = plan["items"][0]
        description = skill["content"].splitlines()[2].removeprefix("description: ")
        self.assertIn('Quote " title', json.loads(description))
        self.assertIn("## 完了確認", skill["content"])
        self.assertIn("1. 失敗を再現する", skill["content"])
        hook = plan["items"][1]
        self.assertTrue(hook["relative_path"].startswith(".memory-harness/hook-specs/"))
        self.assertIn("未実装・未登録", hook["content"])
        approval = approve_plan(self.run, ["skill1", "hook1"])
        self.assertEqual(len(approval["operations"]), 2)
        self.assertEqual(list(self.project.iterdir()), [])

    def test_skill_name_and_directory_follow_skill_naming_rules(self):
        self.analysis([candidate("c_1234abcdef", "skill")])
        item = make_plan(self.run)["items"][0]
        self.assertEqual(item["relative_path"], ".claude/skills/mh-c-1234abcdef/SKILL.md")
        self.assertIn('name: "mh-c-1234abcdef"', item["content"])

    def test_memory_archive_candidates_do_not_modify_harness(self):
        self.analysis([candidate("memory1", "memory"), candidate("archive1", "archive")])
        plan = make_plan(self.run)
        self.assertTrue(all(item["relative_path"] is None for item in plan["items"]))
        with self.assertRaisesRegex(HarnessError, "対象では"):
            approve_plan(self.run, ["memory1"])

    def test_id_and_selection_validation(self):
        self.analysis([candidate("../../escape")])
        with self.assertRaises(HarnessError):
            make_plan(self.run)
        self.analysis([candidate()])
        make_plan(self.run)
        for ids in [[], ["unknown"], ["candidate1", "candidate1"]]:
            with self.subTest(ids=ids), self.assertRaises(HarnessError):
                approve_plan(self.run, ids)

    def test_already_appended_candidate_is_not_repeated(self):
        self.analysis([candidate(target="claude_md")])
        make_plan(self.run)
        approval = approve_plan(self.run, ["candidate1"])
        (self.project / "CLAUDE.md").write_bytes(approval["operations"][0]["after_content"].encode("utf-8"))
        with self.assertRaisesRegex(HarnessError, "再収集"):
            make_plan(self.run)
        self.analysis([candidate(target="claude_md")])
        plan = make_plan(self.run)
        self.assertTrue(plan["items"][0]["blocked_reasons"])
        with self.assertRaisesRegex(HarnessError, "既に反映"):
            approve_plan(self.run, ["candidate1"])

    def test_symlinked_output_parent_is_blocked(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        try:
            (self.project / ".claude").symlink_to(elsewhere, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation unavailable to this user")
        self.analysis([candidate()])
        with self.assertRaises(HarnessError):
            make_plan(self.run)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_provenance_is_a_source_reference_never_an_import(self):
        self.analysis([candidate(target="claude_md")])
        plan = make_plan(self.run)
        content = plan["items"][0]["content"]
        self.assertIn("source1（3–3 行）", content)
        self.assertNotIn("@source", content)
        self.assertIn("理由:", content)
        self.assertTrue((self.run / "REVIEW.md").is_file())
        self.assertTrue((self.run / "diffs/candidate1.diff").is_file())


if __name__ == "__main__":
    unittest.main()
