"""Whole workflow through the command line: fictional memories, hand-written responses."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from memory_harness.analyze import EXTRACT_SCHEMA, REVIEW_SCHEMA
from memory_harness.common import read_json, write_json

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
ORIGINAL_CLAUDE_MD = "# 既存の方針\n説明は日本語で書く。\n".encode("utf-8")

MEMORIES = [
    ("writing.md", "公開ブログでは絵文字を使わない。コード内の文字列リテラルは対象外。読み上げ時に意味のない語が増えるため。\n", {
        "title": "公開ブログの絵文字", "condition": "公開ブログ記事を編集するとき", "action": "本文と見出しに絵文字を使わない。",
        "exceptions": ["コード内の文字列リテラル"], "rationale": "読み上げ時に意味のない語が増えるため。", "target": "rule",
        "paths": ["blog/**/*.md"], "steps": [], "authority": "explicit_user", "confidence": 0.9}),
    ("workflow.md", "入門記事の公開前は、読者像を確認し、用語を説明し、動く例を確認し、出典を確認する。\n", {
        "title": "入門記事の公開前確認", "condition": "入門記事の公開前レビューを依頼されたとき", "action": "読者像・用語・実例・出典を順に確認する。",
        "exceptions": [], "rationale": "読者が実際に理解し、追試できる記事にするため。", "target": "skill", "paths": [],
        "steps": ["対象読者と前提知識を確認する。", "初出の用語を説明する。", "実例が動作するか確認し、未検証なら明記する。", "主張と出典の対応を確認する。"],
        "authority": "explicit_user", "confidence": 0.85}),
    ("background.md", "前回の記事は昨年8月に公開した。これは過去の記録であり、今後の公開日の指定ではない。\n", {
        "title": "過去の記事公開時期", "condition": "前回の記事の公開時期を参照するとき", "action": "前回は昨年8月に公開したという記録を保持する。",
        "exceptions": [], "rationale": "過去の事実であり、今後の作業方針ではない。", "target": "memory", "paths": [], "steps": [],
        "authority": "inferred", "confidence": 0.95}),
]


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / "CLAUDE.md").write_bytes(ORIGINAL_CLAUDE_MD)
        self.memory = self.root / "sample-memory"
        self.memory.mkdir()
        for name, text, _candidate in MEMORIES:
            (self.memory / name).write_bytes(text.encode("utf-8"))
        self.run = self.root / "run"

    def call(self, *args, expect=0):
        proc = subprocess.run([sys.executable, str(ROOT / "run_tool.py"), *args], text=True, encoding="utf-8",
                              capture_output=True, cwd=ROOT, timeout=120, shell=False)
        self.assertEqual(proc.returncode, expect, proc.stdout + proc.stderr)
        return proc

    def evaluator_config(self):
        """The shipped config names "python"; fall back to this interpreter when that name is absent."""
        if shutil.which("python"):
            return EXAMPLES / "mechanical-evaluator.json"
        config = self.root / "mechanical-evaluator.json"
        write_json(config, {"argv": [sys.executable, str(EXAMPLES / "mechanical_evaluator.py")],
                            "kind": "mechanical inspection of generated files"})
        return config

    def extract_responses(self, document):
        origins = {s["id"]: Path(s["origin"]).name for s in read_json(self.run / "inventory.json")["sources"]}
        by_name = {name: candidate for name, _text, candidate in MEMORIES}
        responses = {}
        for item in document["items"]:
            payload = item["payload"]
            if payload["source_kind"] == "harness":
                responses[item["key"]] = {"disposition": "reference", "reason": "既存の方針であり、候補とは無関係。", "candidates": []}
                continue
            raw = dict(by_name[origins[payload["source_id"]]])
            raw["evidence"] = [{"source_id": payload["source_id"], "start_line": payload["line_range"][0],
                                "end_line": payload["line_range"][1], "quote": payload["source_data"].strip()}]
            responses[item["key"]] = {"disposition": "candidates", "reason": "通し試験用に用意した候補。", "candidates": [raw]}
        return {"schema_version": 1, "stage": "extract", "responses": responses}

    @staticmethod
    def review_responses(document):
        return {"schema_version": 1, "stage": "review", "responses": {
            item["key"]: {"pairs": [{"left_id": pair["left"]["id"], "right_id": pair["right"]["id"], "relation": "none",
                                     "reason": "条件も目的も異なる知見。", "example": "", "preferred_id": None}
                                    for pair in item["payload"]["pairs"]]}
            for item in document["items"]}}

    def test_collect_to_rollback_restores_the_original_bytes(self):
        run = str(self.run)
        self.call("collect", "--project", str(self.project), "--memory-dir", str(self.memory), "--run", run)
        manifest = self.call("manifest", "--run", run)
        for name in ("writing.md", "workflow.md", "background.md", "CLAUDE.md"):
            self.assertIn(name, manifest.stdout)
        self.assertNotIn("公開ブログ", manifest.stdout, "manifest lists inputs without showing their text")

        self.call("requests", "--run", run, "--stage", "extract")
        document = read_json(self.run / "requests/extract.json")
        self.assertEqual(document["response_schema"], EXTRACT_SCHEMA)
        self.assertEqual(len(document["items"]), 4)
        write_json(self.run / "responses/extract.json", self.extract_responses(document))
        self.call("validate", "--run", run)
        analysis = read_json(self.run / "candidates.json")
        self.assertTrue(analysis["complete"])
        self.assertEqual(analysis["errors"], [])
        self.assertEqual(len(analysis["records"]), 3)

        proc = self.call("--json", "requests", "--run", run, "--stage", "review")
        self.assertEqual(json.loads(proc.stdout)["pairs_total"], 3)
        document = read_json(self.run / "requests/review.json")
        self.assertEqual(document["response_schema"], REVIEW_SCHEMA)
        write_json(self.run / "responses/review.json", self.review_responses(document))
        self.call("relations", "--run", run)
        relations = read_json(self.run / "relations.json")
        self.assertTrue(relations["complete"])
        self.assertEqual((relations["pairs_checked"], relations["relations"]), (3, []))

        self.call("plan", "--run", run)
        plan = read_json(self.run / "plan.json")
        self.assertTrue(plan["complete"])
        review_text = (self.run / "REVIEW.md").read_text(encoding="utf-8")
        self.assertIn("公開ブログ", review_text)
        self.assertIn("文字列リテラル", review_text)
        selected = [item["candidate_id"] for item in plan["items"] if item["target"] in ("rule", "skill")]
        self.assertEqual(len(selected), 2)
        self.assertEqual(len(plan["items"]), 3)
        self.call("approve", "--run", run, "--ids", *selected,
                  "--notes", "架空データの通し試験。条件・例外・出典と差分を固定応答に照合した。")
        approval = read_json(self.run / "approval.json")
        self.assertEqual(sorted(approval["candidate_ids"]), sorted(selected))

        proc = self.call("--json", "evaluate", "--run", run, "--cases", str(EXAMPLES / "cases.json"),
                         "--evaluator", str(self.evaluator_config()))
        report = json.loads(proc.stdout)
        self.assertTrue(report["complete"])
        self.assertGreater(report["summary"]["mean_delta"], 0)
        self.assertEqual(report["summary"]["regressions"], [])
        self.assertTrue(all(report["rows"][0]["candidate"]["checks"].values()))
        self.assertEqual((self.project / "CLAUDE.md").read_bytes(), ORIGINAL_CLAUDE_MD)
        self.assertFalse((self.project / ".claude").exists())

        proc = self.call("--json", "apply", "--run", run)
        self.assertEqual(json.loads(proc.stdout)["state"], "applied")
        applied = [self.project / operation["relative_path"] for operation in approval["operations"]]
        self.assertEqual(len(applied), 2)
        for path in applied:
            self.assertTrue(path.is_file(), path)
        self.assertIn("blog/**/*.md", (self.project / ".claude/rules").glob("mh-*.md").__next__().read_text(encoding="utf-8"))

        proc = self.call("--json", "rollback", "--run", run)
        self.assertEqual(json.loads(proc.stdout)["state"], "rolled_back")
        self.assertEqual((self.project / "CLAUDE.md").read_bytes(), ORIGINAL_CLAUDE_MD)
        for path in applied:
            self.assertFalse(path.exists(), path)
        self.assertFalse((self.project / ".claude").exists())
        self.assertEqual({p.name for p in self.project.iterdir()}, {"CLAUDE.md", ".memory-harness"})
        for name, text, _candidate in MEMORIES:
            self.assertEqual((self.memory / name).read_bytes(), text.encode("utf-8"))

        proc = self.call("--json", "status", "--run", run)
        status = json.loads(proc.stdout)
        self.assertEqual(status["transaction"]["state"], "rolled_back")
        self.assertEqual(status["candidates"]["count"], 3)
        self.assertTrue(status["evaluation"]["complete"])


if __name__ == "__main__":
    unittest.main()
