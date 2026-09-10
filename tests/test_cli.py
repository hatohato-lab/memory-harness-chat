"""Command-line contract: exit codes, request files and response validation."""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from memory_harness import cli
from memory_harness.analyze import EXTRACT_SCHEMA, REVIEW_SCHEMA, SYSTEM, analyze, review
from memory_harness.common import HarnessError, read_json, write_json

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_COMMANDS = ["collect", "manifest", "requests", "validate", "relations", "plan",
                     "approve", "evaluate", "apply", "rollback", "status"]


def raising(exc):
    def command(_args):
        raise exc
    return command


class RecordedProvider:
    name = "test-recording"

    def __init__(self, callback):
        self.callback = callback
        self.calls = []

    def respond(self, key, payload, schema):
        self.calls.append((key, payload, schema))
        return self.callback(key, payload)


def call(*args, timeout=60):
    return subprocess.run([sys.executable, str(ROOT / "run_tool.py"), *args], text=True, encoding="utf-8",
                          capture_output=True, cwd=ROOT, timeout=timeout, shell=False)


def candidate(payload, title):
    return {
        "title": title, "condition": "テストを変更するとき", "action": payload["source_data"].strip(),
        "exceptions": [], "rationale": "通し試験用の固定候補", "target": "rule", "paths": ["tests/**/*.py"],
        "steps": [], "authority": "explicit_user", "confidence": 0.9,
        "evidence": [{"source_id": payload["source_id"], "start_line": payload["line_range"][0],
                      "end_line": payload["line_range"][1], "quote": payload["source_data"].strip()}],
    }


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        self.memory = self.root / "memory"
        self.memory.mkdir()
        self.run = self.root / "run"

    def collect(self):
        (self.memory / "notes.md").write_bytes("Always run pytest.\nKeep error logs.\n".encode("utf-8"))
        (self.memory / "topic.md").write_bytes("Write commit messages in the imperative.\n".encode("utf-8"))
        proc = call("collect", "--project", str(self.project), "--memory-dir", str(self.memory), "--run", str(self.run))
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_missing_memory_scope_is_an_error_and_creates_no_run(self):
        proc = call("collect", "--project", str(self.project), "--run", str(self.run))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--memory-dir", proc.stderr)
        self.assertFalse(self.run.exists())

    def test_extract_requests_are_exactly_the_analyze_calls(self):
        self.collect()
        proc = call("--json", "requests", "--run", str(self.run), "--stage", "extract")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        summary = json.loads(proc.stdout)
        document = read_json(self.run / "requests/extract.json")
        self.assertEqual(Path(summary["file"]), self.run / "requests/extract.json")
        self.assertEqual(summary["items"], 2)
        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(document["stage"], "extract")
        self.assertEqual(Path(document["run"]), self.run)
        self.assertEqual(document["response_file"], "responses/extract.json")
        self.assertEqual(document["response_schema"], EXTRACT_SCHEMA)
        self.assertTrue(document["instructions"].startswith(SYSTEM))
        self.assertIn("Write only the requested structured output to the response file.", document["instructions"])
        self.assertIn("Do not modify the project while extracting.", document["instructions"])
        self.assertNotIn("Return only the requested", document["instructions"])
        self.assertNotIn("No tool use", document["instructions"])
        provider = RecordedProvider(lambda key, payload: {"disposition": "no_change", "reason": "nothing", "candidates": []})
        analyze(self.run, provider)
        self.assertEqual([(item["key"], item["payload"]) for item in document["items"]],
                         [(key, payload) for key, payload, _schema in provider.calls])
        self.assertTrue(all(schema == EXTRACT_SCHEMA for _key, _payload, schema in provider.calls))
        for item in document["items"]:
            self.assertEqual(document["instructions"], SYSTEM + item["payload"]["instructions"])
            self.assertEqual(set(item["payload"]), {"task", "source_id", "source_kind", "line_range", "source_data", "instructions"})

    def test_validate_without_responses_reports_each_missing_key(self):
        self.collect()
        proc = call("validate", "--run", str(self.run))
        self.assertEqual(proc.returncode, 3, proc.stderr)
        analysis = read_json(self.run / "candidates.json")
        self.assertFalse(analysis["complete"])
        self.assertEqual(analysis["provider"], "chat-responses")
        self.assertEqual(len(analysis["errors"]), 2)
        for error in analysis["errors"]:
            self.assertIn("応答がありません", error["error"])
            self.assertIn("extract:" + error["chunk_id"], error["error"])
        self.assertIn("応答がありません", proc.stdout)

    def test_review_requests_are_exactly_the_review_calls(self):
        self.collect()
        call("requests", "--run", str(self.run), "--stage", "extract")
        document = read_json(self.run / "requests/extract.json")
        responses = {item["key"]: {"disposition": "candidates", "reason": "固定応答",
                                   "candidates": [candidate(item["payload"], f"lesson {index}")]}
                     for index, item in enumerate(document["items"])}
        write_json(self.run / "responses/extract.json", {"schema_version": 1, "stage": "extract", "responses": responses})
        proc = call("validate", "--run", str(self.run))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(read_json(self.run / "candidates.json")["records"]), 2)
        proc = call("--json", "requests", "--run", str(self.run), "--stage", "review")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["pairs_total"], 1)
        document = read_json(self.run / "requests/review.json")
        self.assertEqual(document["response_schema"], REVIEW_SCHEMA)
        self.assertEqual(document["response_file"], "responses/review.json")
        provider = RecordedProvider(lambda key, payload: {"pairs": []})
        review(self.run, provider)
        self.assertEqual([(item["key"], item["payload"]) for item in document["items"]],
                         [(key, payload) for key, payload, _schema in provider.calls])
        self.assertEqual(len(document["items"]), 1)
        self.assertEqual(set(document["items"][0]["payload"]), {"task", "pairs", "instructions"})
        proc = call("relations", "--run", str(self.run))
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertIn("応答がありません: review:", proc.stdout)
        verdicts = {item["key"]: {"pairs": [{"left_id": pair["left"]["id"], "right_id": pair["right"]["id"],
                                             "relation": "none", "reason": "無関係", "example": "", "preferred_id": None}
                                            for pair in item["payload"]["pairs"]]} for item in document["items"]}
        write_json(self.run / "responses/review.json", {"schema_version": 1, "stage": "review", "responses": verdicts})
        proc = call("relations", "--run", str(self.run))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        relations = read_json(self.run / "relations.json")
        self.assertTrue(relations["complete"])
        self.assertEqual(relations["pairs_checked"], 1)
        proc = call("--json", "status", "--run", str(self.run))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        status = json.loads(proc.stdout)
        self.assertEqual(status["requests"], {"extract": True, "review": True})
        self.assertEqual(status["responses"], {"extract": True, "review": True})
        self.assertEqual(status["candidates"]["count"], 2)

    def test_status_of_an_empty_directory_is_an_error(self):
        self.run.mkdir()
        proc = call("status", "--run", str(self.run))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("処理記録", proc.stderr)

    def test_parser_defaults_and_required_options_follow_the_contract(self):
        parser = cli.parser()
        self.assertEqual(sorted(cli.COMMANDS), sorted(CONTRACT_COMMANDS))
        args = parser.parse_args(["collect", "--project", "p", "--memory-dir", "m", "--run", "r"])
        self.assertEqual((args.chunk_chars, args.max_file_bytes, args.source), (8000, 4194304, []))
        for command in ("requests --stage review", "relations"):
            args = parser.parse_args([*command.split(), "--run", "r"])
            self.assertEqual((args.max_pairs, args.review_batch_size), (1000, 8))
        args = parser.parse_args(["approve", "--run", "r", "--ids", "a", "b"])
        self.assertEqual((args.ids, args.notes, args.allow_incomplete), (["a", "b"], "", False))
        args = parser.parse_args(["evaluate", "--run", "r", "--cases", "c", "--evaluator", "e"])
        self.assertEqual((args.repeats, args.timeout, args.max_copy_bytes), (1, 120, 104857600))
        args = parser.parse_args(["apply", "--run", "r"])
        self.assertEqual((args.ignore_evaluation, args.notes), (False, ""))
        self.assertTrue(parser.parse_args(["--json", "status", "--run", "r"]).json)
        # --run has no default location: every command requires it explicitly.
        for argv in (["collect", "--project", "p", "--memory-dir", "m"], ["approve", "--run", "r"],
                     ["requests", "--run", "r", "--stage", "other"], ["status"], ["status", "--run", "r", "--json"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    parser.parse_args(argv)
                self.assertEqual(caught.exception.code, 2)

    def test_main_maps_outcomes_to_exit_codes(self):
        outcomes = {
            "finished": (lambda _args: {"state": "applied"}, 0),
            "incomplete": (lambda _args: {"complete": False, "errors": []}, 3),
            "kernel_error": (raising(HarnessError("整合性エラー")), 2),
            "os_error": (raising(OSError("disk")), 2),
            "interrupt": (raising(KeyboardInterrupt()), 130),
        }
        for name, (command, expected) in outcomes.items():
            with self.subTest(outcome=name), patch.dict(cli.COMMANDS, {"status": command}):
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = cli.main(["--json", "status", "--run", str(self.root)])
                self.assertEqual(code, expected)
                if expected in (0, 3):
                    self.assertEqual(json.loads(out.getvalue()), command(None))
                else:
                    self.assertEqual(out.getvalue(), "")
                    self.assertTrue(err.getvalue().strip())


if __name__ == "__main__":
    unittest.main()
