import copy
import tempfile
import unittest
from pathlib import Path

from memory_harness.analyze import analyze, extract_requests, review, review_requests
from memory_harness.collect import collect
from memory_harness.common import HarnessError, read_json, write_json
from memory_harness.responses import ResponseFileProvider


class RecordedProvider:
    name = "test-recording (no Claude invocation)"

    def __init__(self, callback):
        self.callback = callback
        self.calls = []

    def respond(self, key, payload, schema):
        self.calls.append((key, payload, schema))
        return self.callback(key, payload)


def candidate(source_id, quote="Always run pytest.", start_line=1, end_line=1):
    return {
        "title": "Run tests", "condition": "After changing Python code",
        "action": "Run pytest", "exceptions": [], "rationale": "Detect regressions",
        "target": "rule", "paths": ["**/*.py"], "steps": [],
        "authority": "explicit_user", "confidence": 0.9,
        "evidence": [{"source_id": source_id, "start_line": start_line,
                      "end_line": end_line, "quote": quote}],
    }


def answer(records=None, disposition="candidates"):
    return {"disposition": disposition, "reason": "Explicitly considered the supplied text", "candidates": records or []}


def verdict(left, right, relation="none", example="", preferred=None):
    return {"left_id": left, "right_id": right, "relation": relation,
            "reason": "Compared applicability and required actions", "example": example,
            "preferred_id": preferred}


class AnalyzeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.memory = self.root / "memory"
        self.memory.mkdir()
        self.serial = 0

    def collect_run(self, text="Always run pytest.\r\nKeep error logs.\r\n", **kwargs):
        self.serial += 1
        (self.memory / "MEMORY.md").write_bytes(text.encode("utf-8"))
        run = self.root / f"run-{self.serial}"
        inventory = collect(self.project, [self.memory], [], run, **kwargs)
        return run, inventory

    def test_valid_literal_evidence_preserves_provenance_and_crlf(self):
        run, inventory = self.collect_run()
        provider = RecordedProvider(lambda key, payload: answer([candidate(payload["source_id"])]))
        result = analyze(run, provider)
        self.assertTrue(result["complete"])
        self.assertEqual(result["errors"], [])
        record = result["records"][0]
        self.assertEqual(record["source_kind"], "memory")
        self.assertTrue(record["needs_review"])
        self.assertTrue(record["issues"])
        self.assertEqual(record["evidence"][0]["source_id"], inventory["sources"][0]["id"])
        self.assertIn("\r\n", provider.calls[0][1]["source_data"])
        self.assertEqual(read_json(run / "candidates.json"), result)

    def test_invented_out_of_range_wrong_source_and_empty_evidence_are_errors(self):
        def invented(record):
            record["evidence"][0]["quote"] = "Always delete all tests."

        def out_of_range(record):
            record["evidence"][0]["end_line"] = 99

        def wrong_line(record):
            record["evidence"][0].update(start_line=2, end_line=2)

        def wrong_source(record):
            record["evidence"][0]["source_id"] = "different-source"

        def empty_evidence(record):
            record["evidence"] = []

        for mutate in (invented, out_of_range, wrong_line, wrong_source, empty_evidence):
            with self.subTest(case=mutate.__name__):
                run, _ = self.collect_run()

                def response(key, payload):
                    record = candidate(payload["source_id"])
                    mutate(record)
                    return answer([record])

                result = analyze(run, RecordedProvider(response))
                self.assertFalse(result["complete"])
                self.assertEqual(result["records"], [])
                self.assertEqual(result["coverage"][0]["status"], "error")
                self.assertEqual(len(result["errors"]), 1)

    def test_quote_elsewhere_on_same_long_line_cannot_escape_chunk(self):
        run, inventory = self.collect_run("LEFT-RIGHT-END", chunk_chars=5)

        def response(key, payload):
            if payload["source_data"] == "LEFT-":
                return answer([candidate(payload["source_id"], quote="END")])
            return answer(disposition="no_change")

        result = analyze(run, RecordedProvider(response))
        self.assertFalse(result["complete"])
        self.assertEqual(len(result["coverage"]), len(inventory["chunks"]))
        self.assertEqual(result["coverage"][0]["status"], "error")

    def test_uncertain_and_inconsistent_dispositions_never_hide_incomplete_input(self):
        for disposition, include_candidate in (("uncertain", False), ("candidates", False), ("no_change", True), ("reference", False)):
            with self.subTest(disposition=disposition):
                run, _ = self.collect_run()
                provider = RecordedProvider(lambda key, payload: answer(
                    [candidate(payload["source_id"])] if include_candidate else [], disposition))
                result = analyze(run, provider)
                self.assertFalse(result["complete"])
                if disposition == "uncertain":
                    self.assertEqual(result["coverage"][0]["status"], "done")
                else:
                    self.assertEqual(result["coverage"][0]["status"], "error")

    def test_one_bad_candidate_rejects_whole_chunk_without_partial_promotion(self):
        run, _ = self.collect_run()

        def response(key, payload):
            good = candidate(payload["source_id"])
            bad = copy.deepcopy(good)
            bad["title"] = "Invented lesson"
            bad["evidence"][0]["quote"] = "This never appeared"
            return answer([good, bad])

        result = analyze(run, RecordedProvider(response))
        self.assertEqual(result["records"], [])
        self.assertFalse(result["complete"])

    def test_snapshot_or_chunk_tampering_is_detected_before_model_call(self):
        for field in ("snapshot", "chunk"):
            with self.subTest(field=field):
                run, inventory = self.collect_run()
                relative = inventory["sources"][0]["snapshot"] if field == "snapshot" else inventory["chunks"][0]["path"]
                (run / relative).write_bytes(b"Modified after collection")
                provider = RecordedProvider(lambda key, payload: answer(disposition="no_change"))
                result = analyze(run, provider)
                self.assertFalse(result["complete"])
                self.assertEqual(provider.calls, [])
                self.assertEqual(len(result["errors"]), 1)

    def test_provider_failure_and_unreadable_source_remain_visible(self):
        run, inventory = self.collect_run("A.\nB.\nC.\n", chunk_chars=3)

        def response(key, payload):
            if payload["source_data"] == "B.\n":
                raise HarnessError("response unavailable")
            return answer(disposition="no_change")

        result = analyze(run, RecordedProvider(response))
        self.assertFalse(result["complete"])
        self.assertEqual(len(result["coverage"]), len(inventory["chunks"]))
        self.assertEqual([entry["status"] for entry in result["coverage"]], ["done", "error", "done"])
        (self.memory / "invalid.md").write_bytes(b"invalid\xff")
        other_run, _ = self.collect_run()
        other = analyze(other_run, RecordedProvider(lambda key, payload: answer(disposition="no_change")))
        self.assertFalse(other["complete"])
        self.assertEqual(len(other["source_failures"]), 1)

    def test_same_lesson_from_multiple_sources_retains_both_evidence_records(self):
        (self.memory / "topic.md").write_bytes(b"Always run pytest.\n")
        run, _ = self.collect_run()
        result = analyze(run, RecordedProvider(lambda key, payload: answer([candidate(payload["source_id"])])))
        self.assertTrue(result["complete"])
        self.assertEqual(len(result["records"]), 1)
        self.assertEqual(len(result["records"][0]["evidence"]), 2)
        self.assertEqual(len({e["source_id"] for e in result["records"][0]["evidence"]}), 2)

    def test_existing_harness_records_remain_reference_only(self):
        (self.project / "CLAUDE.md").write_bytes(b"Always run pytest.\n")
        run = self.root / "harness-run"
        collect(self.project, [], [], run)
        result = analyze(run, RecordedProvider(lambda key, payload: answer([candidate(payload["source_id"])], "reference")))
        self.assertTrue(result["complete"])
        self.assertEqual(result["records"][0]["source_kind"], "harness")
        self.assertFalse(result["records"][0]["needs_review"])

    def test_extract_requests_are_the_provider_calls(self):
        run, inventory = self.collect_run("A.\nB.\nC.\n", chunk_chars=3)
        requests = [(key, payload) for _chunk, _source, key, payload in extract_requests(run)]
        provider = RecordedProvider(lambda key, payload: answer(disposition="no_change"))
        analyze(run, provider)
        self.assertEqual(requests, [(key, payload) for key, payload, _schema in provider.calls])
        self.assertEqual(len(requests), len(inventory["chunks"]))
        self.assertEqual(requests[0][0], "extract:" + inventory["chunks"][0]["id"])
        self.assertEqual(requests[0][1]["source_data"], "A.\n")
        self.assertEqual(requests[0][1]["line_range"], [1, 1])

    def test_extract_requests_stop_at_tampering_unless_a_handler_takes_it(self):
        run, inventory = self.collect_run("A.\nB.\nC.\n", chunk_chars=3)
        (run / inventory["chunks"][1]["path"]).write_bytes(b"X.\n")
        with self.assertRaisesRegex(HarnessError, "chunkとsnapshot"):
            list(extract_requests(run))
        skipped = []
        keys = [key for _chunk, _source, key, _payload in
                extract_requests(run, on_error=lambda chunk, exc: skipped.append(chunk["id"]))]
        self.assertEqual(skipped, [inventory["chunks"][1]["id"]])
        self.assertEqual(len(keys), 2)
        result = analyze(run, RecordedProvider(lambda key, payload: answer(disposition="no_change")))
        self.assertFalse(result["complete"])
        self.assertEqual([entry["status"] for entry in result["coverage"]], ["done", "error", "done"])

    def test_response_file_provider_answers_by_key_and_reports_missing_keys(self):
        run, _ = self.collect_run()
        path = run / "responses/extract.json"
        responses = {key: answer([candidate(payload["source_id"])]) for _chunk, _source, key, payload in extract_requests(run)}
        write_json(path, {"schema_version": 1, "stage": "extract", "responses": responses})
        provider = ResponseFileProvider(path)
        self.assertEqual(provider.name, "chat-responses")
        result = analyze(run, provider)
        self.assertTrue(result["complete"])
        self.assertEqual(result["provider"], "chat-responses")
        self.assertEqual(len(result["records"]), 1)
        with self.assertRaisesRegex(HarnessError, "応答がありません: extract:missing"):
            provider.respond("extract:missing", {}, {})
        absent = analyze(run, ResponseFileProvider(run / "responses/absent.json"))
        self.assertFalse(absent["complete"])
        self.assertEqual(len(absent["errors"]), 1)
        self.assertIn("応答がありません", absent["errors"][0]["error"])
        write_json(path, {"schema_version": 1, "stage": "extract", "responses": []})
        with self.assertRaises(HarnessError):
            ResponseFileProvider(path)


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run = Path(self.temp.name)
        self.records = [{"id": "a", "source_kind": "memory"},
                        {"id": "b", "source_kind": "memory"},
                        {"id": "h", "source_kind": "harness"}]
        write_json(self.run / "candidates.json", {"records": self.records, "complete": True})

    def test_complete_review_accounts_for_none_and_preserves_real_relations(self):
        def response(key, payload):
            return {"pairs": [verdict(pair["left"]["id"], pair["right"]["id"],
                                      relation="duplicate" if index == 0 else "none")
                              for index, pair in enumerate(payload["pairs"])]}

        result = review(self.run, RecordedProvider(response))
        self.assertTrue(result["complete"])
        self.assertEqual(result["pairs_total"], 3)
        self.assertEqual(result["pairs_checked"], 3)
        self.assertEqual(len(result["relations"]), 1)
        self.assertEqual(read_json(self.run / "relations.json"), result)

    def test_missing_duplicate_unknown_and_invalid_conflict_pairs_are_rejected(self):
        def missing(items):
            return items[:-1]

        def duplicate(items):
            return items + [copy.deepcopy(items[0])]

        def unknown(items):
            items[0]["right_id"] = "not-requested"
            return items

        def unsupported_preference(items):
            items[0]["preferred_id"] = "third-candidate"
            return items

        def conflict_without_example(items):
            items[0]["relation"] = "conflict"
            return items

        for mutate in (missing, duplicate, unknown, unsupported_preference, conflict_without_example):
            with self.subTest(case=mutate.__name__):
                def response(key, payload):
                    items = [verdict(p["left"]["id"], p["right"]["id"]) for p in payload["pairs"]]
                    return {"pairs": mutate(items)}

                result = review(self.run, RecordedProvider(response))
                self.assertFalse(result["complete"])
                self.assertEqual(result["pairs_checked"], 0)
                self.assertEqual(result["relations"], [])
                self.assertEqual(len(result["errors"]), 1)

    def test_pair_cap_and_provider_errors_never_claim_complete(self):
        def response(key, payload):
            return {"pairs": [verdict(p["left"]["id"], p["right"]["id"]) for p in payload["pairs"]]}

        capped = review(self.run, RecordedProvider(response), max_pairs=1)
        self.assertFalse(capped["complete"])
        self.assertEqual((capped["pairs_checked"], capped["pairs_total"]), (1, 3))

        def failed(key, payload):
            raise HarnessError("response unavailable")

        result = review(self.run, RecordedProvider(failed), batch_size=1)
        self.assertFalse(result["complete"])
        self.assertEqual(len(result["errors"]), 3)
        self.assertEqual(result["pairs_checked"], 0)

    def test_harness_to_harness_pairs_are_not_needlessly_reviewed(self):
        write_json(self.run / "candidates.json", {"records": [
            {"id": "h1", "source_kind": "harness"}, {"id": "h2", "source_kind": "harness"}]})
        provider = RecordedProvider(lambda key, payload: self.fail("reference-only pair should not call model"))
        result = review(self.run, provider)
        self.assertTrue(result["complete"])
        self.assertEqual(result["pairs_total"], 0)
        self.assertEqual(provider.calls, [])

    def test_review_requests_are_the_provider_calls_and_answer_from_a_file(self):
        pairs_total, batches = review_requests(self.run, batch_size=2)
        self.assertEqual(pairs_total, 3)
        self.assertEqual([len(ids) for _key, ids, _payload in batches], [2, 1])
        provider = RecordedProvider(lambda key, payload: {"pairs": [verdict(p["left"]["id"], p["right"]["id"]) for p in payload["pairs"]]})
        review(self.run, provider, batch_size=2)
        self.assertEqual([(key, payload) for key, _ids, payload in batches],
                         [(key, payload) for key, payload, _schema in provider.calls])
        path = self.run / "responses/review.json"
        write_json(path, {"schema_version": 1, "stage": "review", "responses": {
            key: {"pairs": [verdict(left, right) for left, right in ids]} for key, ids, _payload in batches}})
        result = review(self.run, ResponseFileProvider(path), batch_size=2)
        self.assertTrue(result["complete"])
        self.assertEqual(result["pairs_checked"], 3)
        with self.assertRaises(HarnessError):
            review_requests(self.run, batch_size=0)


if __name__ == "__main__":
    unittest.main()
