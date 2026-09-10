import importlib
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from memory_harness.collect import collect
from memory_harness.common import HarnessError, is_link, read_json, sha256_bytes


collector = importlib.import_module("memory_harness.collect")


def _junction(target: Path, link: Path) -> None:
    """Create a Windows junction; no privilege is needed, unlike a symlink."""
    import _winapi
    _winapi.CreateJunction(str(target), str(link))


class CollectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.memory = self.root / "memory"
        self.memory.mkdir()
        self.run = self.root / "run"

    def put(self, path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
        return path

    def inventory(self, **kwargs):
        return collect(self.project, [self.memory], [], self.run, **kwargs)

    def test_long_unicode_lines_are_covered_once_without_normalizing_bytes(self):
        text = "先頭\r\n" + "日本語🐦e\u0301" * 80 + "\r\n途中\n末尾\r最後"
        original = self.put(self.memory / "MEMORY.md", text)
        before = original.read_bytes()
        inventory = self.inventory(chunk_chars=17)
        source = inventory["sources"][0]
        chunks = inventory["chunks"]
        self.assertEqual((self.run / source["snapshot"]).read_bytes(), before)
        self.assertEqual(source["sha256"], sha256_bytes(before))
        self.assertEqual(source["line_count"], 5)
        self.assertGreater(len(chunks), 20)
        cursor = 0
        reconstructed = []
        for chunk in chunks:
            self.assertEqual(chunk["source_id"], source["id"])
            self.assertEqual(chunk["start_offset"], cursor)
            fragment = (self.run / chunk["path"]).read_bytes().decode("utf-8")
            self.assertEqual(fragment, text[chunk["start_offset"]:chunk["end_offset"]])
            self.assertLessEqual(len(fragment), 17)
            self.assertGreater(len(fragment), 0)
            self.assertLessEqual(chunk["start_line"], chunk["end_line"])
            reconstructed.append(fragment)
            cursor = chunk["end_offset"]
        self.assertEqual(cursor, len(text))
        self.assertEqual("".join(reconstructed), text)
        self.assertEqual(original.read_bytes(), before)
        self.assertEqual(read_json(self.run / "inventory.json"), inventory)

    def test_chunk_line_numbers_and_source_boundaries(self):
        self.put(self.memory / "a.md", "aa\r\nbb\ncccccccccc")
        self.put(self.memory / "b.md", "ZZ\n")
        inventory = self.inventory(chunk_chars=4)
        source_a, source_b = inventory["sources"]
        a = [chunk for chunk in inventory["chunks"] if chunk["source_id"] == source_a["id"]]
        b = [chunk for chunk in inventory["chunks"] if chunk["source_id"] == source_b["id"]]
        self.assertEqual([(c["start_line"], c["end_line"]) for c in a], [(1, 1), (2, 2), (3, 3), (3, 3), (3, 3)])
        self.assertEqual(len(b), 1)
        self.assertEqual((b[0]["start_offset"], b[0]["end_offset"]), (0, 3))
        self.assertEqual((b[0]["start_line"], b[0]["end_line"]), (1, 1))

    def test_harness_is_reference_even_when_explicit_memory_overlaps(self):
        selected = [
            "CLAUDE.md", "CLAUDE.local.md", ".claude/CLAUDE.md",
            ".claude/rules/python.md", ".claude/rules/nested/tests.md",
            ".claude/skills/review/SKILL.md",
        ]
        for name in selected:
            self.put(self.project / name, "Keep this rule.\n")
        self.put(self.project / ".claude/skills/review/reference.md", "Not loaded as a skill body")
        self.put(self.project / "source/CLAUDE.md", "Nested project instruction outside start-up contract")
        self.put(self.root / "CLAUDE.md", "Parent directory must not be scanned")
        self.put(self.root / "unselected-memory/MEMORY.md", "Not selected")
        inventory = collect(self.project, [], [], self.run)
        self.assertEqual({s["relative_path"] for s in inventory["sources"]}, set(selected))
        self.assertTrue(all(s["kind"] == "harness" for s in inventory["sources"]))
        overlap = collect(self.project, [self.project], [self.project / "CLAUDE.md"], self.root / "overlap")
        by_path = {s["relative_path"]: s for s in overlap["sources"]}
        self.assertEqual(len(overlap["sources"]), len(by_path))
        self.assertTrue(all(by_path[name]["kind"] == "harness" for name in selected))

    def test_empty_invalid_utf8_oversize_and_missing_sources_remain_visible(self):
        self.put(self.memory / "empty.md", b"")
        self.put(self.memory / "invalid.md", b"bad\xffutf8")
        self.put(self.memory / "big.md", "界" * 8)
        self.put(self.memory / "okay.md", "good")
        inventory = collect(self.project, [self.memory, self.root / "missing"], [], self.run, max_file_bytes=20)
        records = {Path(s["origin"]).name: s for s in inventory["sources"]}
        self.assertEqual(records["empty.md"]["status"], "empty")
        self.assertEqual((self.run / records["empty.md"]["snapshot"]).read_bytes(), b"")
        self.assertEqual(records["invalid.md"]["status"], "error")
        self.assertIn("UTF-8", records["invalid.md"]["reason"])
        self.assertEqual(records["big.md"]["status"], "excluded")
        self.assertEqual(records["big.md"]["bytes"], 24)
        self.assertIn("not truncated", records["big.md"]["reason"])
        self.assertEqual(records["missing"]["status"], "error")
        self.assertIsNone(records["invalid.md"]["snapshot"])
        self.assertIsNone(records["big.md"]["snapshot"])
        self.assertEqual(inventory["counts"]["sources"], 5)
        self.assertEqual(inventory["counts"]["error"], 2)
        self.assertEqual(inventory["counts"]["chunks"], 1)

    def test_file_and_directory_read_errors_are_recorded(self):
        blocked_file = self.put(self.memory / "blocked.md", "Do not disappear")
        self.put(self.memory / "okay.md", "Visible")
        blocked_dir = self.memory / "blocked-dir"
        self.put(blocked_dir / "unread.md", "Unreachable")
        real_open, real_scandir = os.open, os.scandir

        def selective_open(path, *args, **kwargs):
            if Path(path) == blocked_file:
                raise PermissionError("simulated unreadable file")
            return real_open(path, *args, **kwargs)

        def selective_scandir(path):
            if Path(path) == blocked_dir:
                raise PermissionError("simulated unreadable directory")
            return real_scandir(path)

        with patch.object(collector.os, "open", side_effect=selective_open), patch.object(collector.os, "scandir", side_effect=selective_scandir):
            inventory = self.inventory()
        records = {Path(s["origin"]).name: s for s in inventory["sources"]}
        self.assertEqual(records["blocked.md"]["status"], "error")
        self.assertEqual(records["blocked-dir"]["status"], "error")
        self.assertEqual(records["okay.md"]["status"], "ok")
        self.assertNotIn("unread.md", records)

    def test_symlinks_are_recorded_and_never_followed(self):
        outside = self.put(self.root / "outside/secret.md", "OUTSIDE SECRET")
        try:
            (self.memory / "file.md").symlink_to(outside)
            (self.memory / "directory").symlink_to(outside.parent, target_is_directory=True)
            (self.memory / "missing.md").symlink_to(self.root / "absent")
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation unavailable to this user")
        inventory = self.inventory()
        self.assertEqual(len(inventory["sources"]), 3)
        self.assertTrue(all(s["status"] == "excluded" for s in inventory["sources"]))
        self.assertEqual(inventory["chunks"], [])
        self.assertFalse((self.run / "snapshots").exists())

    def test_junctions_are_recorded_and_never_followed(self):
        if os.name != "nt":
            self.skipTest("Junctions exist only on Windows")
        outside = self.put(self.root / "outside/secret.md", "OUTSIDE SECRET")
        link = self.memory / "directory"
        try:
            _junction(outside.parent, link)
        except (OSError, ImportError, AttributeError):
            self.skipTest("Junction creation unavailable")
        info = link.lstat()
        self.assertFalse(stat.S_ISLNK(info.st_mode), "A junction is not a symlink; S_ISLNK alone misses it")
        self.assertTrue(is_link(info))
        # Python 3.11 has no Path.is_junction, so the check must not depend on it.
        with patch.object(Path, "is_junction", create=True,
                          side_effect=AssertionError("Path.is_junction is absent on Python 3.11")):
            inventory = self.inventory()
        self.assertEqual([s["status"] for s in inventory["sources"]], ["excluded"])
        self.assertIn("reparse", inventory["sources"][0]["reason"])
        self.assertEqual(inventory["chunks"], [])
        self.assertFalse((self.run / "snapshots").exists())

    def test_default_run_location_is_never_collected(self):
        self.put(self.project / "notes.md", "Keep")
        self.put(self.project / ".memory-harness/old-run/inventory.json", "{}")
        self.put(self.project / ".memory-harness/old-run/snapshots/src-a.txt", "old snapshot")
        self.put(self.project / ".memory-harness/old-run/chunks/src-a-c000001.txt", "old chunk")
        self.put(self.project / ".memory-harness/hook-specs/mh-a.md", "hook draft")
        self.put(self.memory / "MEMORY.md", "Real memory")
        self.put(self.memory / "nested/.memory-harness/run/snapshots/src-b.txt", "nested run output")
        run = self.project / ".memory-harness/new-run"
        inventory = collect(self.project, [self.memory], [self.project], run)
        self.assertEqual(sorted(Path(s["origin"]).name for s in inventory["sources"]), ["MEMORY.md", "notes.md"])
        # The skip is silent: an excluded record would make every later analysis incomplete.
        self.assertEqual(inventory["counts"], dict(inventory["counts"], ok=2, excluded=0, error=0, sources=2))
        self.assertTrue((run / "inventory.json").is_file())
        again = collect(self.project, [], [self.project], self.project / ".memory-harness/second-run")
        self.assertEqual([Path(s["origin"]).name for s in again["sources"]], ["notes.md"])

    def test_source_with_symlink_parent_is_not_read(self):
        self.put(self.root / "actual/MEMORY.md", "Must not read through a linked ancestor")
        linked = self.root / "link"
        try:
            linked.symlink_to(self.root / "actual", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation unavailable to this user")
        inventory = collect(self.project, [linked / "MEMORY.md"], [], self.run)
        self.assertEqual(inventory["sources"][0]["status"], "excluded")
        self.assertIn("ancestor", inventory["sources"][0]["reason"])

    def test_existing_run_even_if_empty_is_protected(self):
        self.run.mkdir()
        sentinel = self.put(self.run / "keep.txt", "retain")
        with self.assertRaises(HarnessError):
            self.inventory()
        self.assertEqual(sentinel.read_bytes(), b"retain")
        with tempfile.TemporaryDirectory(dir=self.root) as empty:
            with self.assertRaises(HarnessError):
                collect(self.project, [], [], Path(empty))

    def test_repeat_collection_ids_and_chunks_are_stable_and_nested_run_not_ingested(self):
        original = self.put(self.memory / "topic.md", "a\r\nbbb\n長" * 7)
        nested_run = self.memory / "new-run"
        first = collect(self.project, [self.memory], [], nested_run, chunk_chars=6)
        self.assertEqual([s["origin"] for s in first["sources"]], [str(original)])
        second = collect(self.project, [original], [], self.run, chunk_chars=6)
        self.assertEqual(first["sources"], second["sources"])
        self.assertEqual(first["chunks"], second["chunks"])
        for chunk in first["chunks"]:
            self.assertEqual((nested_run / chunk["path"]).read_bytes(), (self.run / chunk["path"]).read_bytes())

    def test_explicit_extra_selection_and_reference_file_directory_boundary(self):
        extras = self.root / "extras"
        for name in ("notes.md", "notes.txt", "session.json", "history.jsonl", "ignored.csv"):
            self.put(extras / name, "source")
        self.put(self.project / "CLAUDE.md/private.md", "Do not recursively scan a misnamed directory")
        inventory = collect(self.project, [], [extras], self.run)
        records = {Path(s["origin"]).name: s for s in inventory["sources"]}
        self.assertEqual(set(records), {"notes.md", "notes.txt", "session.json", "history.jsonl", "CLAUDE.md"})
        self.assertEqual(records["CLAUDE.md"]["status"], "error")
        self.assertEqual(records["session.json"]["kind"], "history")
        self.assertEqual(records["history.jsonl"]["kind"], "history")
        self.assertEqual(records["notes.txt"]["kind"], "extra")
        self.assertIsNone(records["notes.txt"]["relative_path"])

    def test_invalid_limits_do_not_create_a_run(self):
        for option in ({"chunk_chars": 0}, {"chunk_chars": True}, {"max_file_bytes": -1}):
            with self.subTest(option=option), self.assertRaises(HarnessError):
                self.inventory(**option)
            self.assertFalse(self.run.exists())


if __name__ == "__main__":
    unittest.main()
