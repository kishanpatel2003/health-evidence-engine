import copy
import json
from pathlib import Path
import tempfile
import unittest

from health_bank.store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "repo"
        self.store = Store(self.root)
        self.original = Path(self.temporary.name) / "synthetic.xml"
        self.original.write_text("<synthetic>not a real patient</synthetic>")
        self.receipt = self.store.add_artifact(self.original, "synthetic")

    def record(self):
        return {"patient_id": "patient-opaque", "lane": "observations", "kind": "synthetic_result",
                "source": {"system": "synthetic", "artifact_sha256": self.receipt["artifact_sha256"],
                           "locator": "/document/entry[1]", "record_key": "source-key-1"},
                "effective_time": {"start": "2020-03", "precision": "month", "original": "202003"},
                "data": {"value": "001.20", "unit": None, "narrative": "α < β\nexact text"}}

    def test_idempotent_assertions_keep_reexport_evidence(self):
        first = self.record()
        identifier = self.store.add_record(first)
        self.assertEqual(identifier, self.store.add_record(first))
        changed_export = Path(self.temporary.name) / "another.xml"
        changed_export.write_text("<synthetic>same assertion; different document</synthetic>")
        second = copy.deepcopy(first)
        second["source"]["artifact_sha256"] = self.store.add_artifact(changed_export, "synthetic")["artifact_sha256"]
        second["source"]["locator"] = "/document/entry[20]"
        second["captured_at"] = "2026-10-22T00:00:00Z"
        self.assertEqual(identifier, self.store.add_record(second))
        self.assertEqual(self.store.stats()["records"], 1)
        self.assertEqual(self.store.stats()["evidence_assertions"], 2)
        self.assertEqual(self.store.verify()["records"], 1)

    def test_revisions_and_conflicting_sources_remain_distinct(self):
        first = self.record()
        identifiers = {self.store.add_record(first)}
        for field, value in (("status", "corrected"), ("data", {"value": "002.40"})):
            revision = copy.deepcopy(first)
            revision[field] = value
            identifiers.add(self.store.add_record(revision))
        other_system = copy.deepcopy(first)
        other_system["source"]["system"] = "other-source"
        identifiers.add(self.store.add_record(other_system))
        self.assertEqual(len(identifiers), 4)

    def test_lossless_json_and_unknown_time(self):
        for data in (None, ["exact", 123, False, None], "narrative", {"large_integer": 10**50}):
            record = self.record()
            record["data"] = data
            del record["effective_time"]
            self.store.add_record(record)
        persisted = list(self.store.iter_records())
        self.assertEqual(persisted[1]["data"], ["exact", 123, False, None])
        self.assertTrue(all(item["effective_time"] is None and item["status"] == "unknown" for item in persisted))
        self.assertEqual(self.store.verify()["records"], 4)

    def test_input_remains_unchanged_and_local_permissions_private(self):
        record = self.record()
        before = copy.deepcopy(record)
        self.store.add_record(record)
        self.assertEqual(record, before)
        self.assertEqual(self.store.local.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.store.local / "canonical/observations.jsonl").stat().st_mode & 0o777, 0o600)

    def test_reject_invalid_types_and_unsafe_lanes(self):
        for change in ({"lane": "../outside"}, {"lane": "evidence"}, {"data": float("nan")},
                       {"effective_time": "2020"}, {"status": []}, {"captured_at": "2020-01-01"}):
            record = self.record()
            record.update(change)
            with self.assertRaises(ValueError):
                self.store.add_record(record)
        record = self.record()
        record["data"] = {1: "integer keys would be coerced"}
        with self.assertRaises(ValueError):
            self.store.add_record(record)

    def test_multiple_store_instances_share_deduplication(self):
        other = Store(self.root)
        self.store.add_record(self.record())
        other.add_record(self.record())
        revised = self.record()
        revised["status"] = "final"
        other.add_record(revised)
        self.store.add_record(revised)
        self.assertEqual(self.store.stats()["records"], 2)
        self.assertEqual(self.store.stats()["evidence_assertions"], 2)

    def test_tampered_original_detected(self):
        path = self.root / self.receipt["local_path"]
        path.write_text("tampered")
        with self.assertRaisesRegex(ValueError, "verification failed"):
            self.store.verify()

    def test_tampered_normalized_value_detected(self):
        self.store.add_record(self.record())
        path = self.store.local / "canonical/observations.jsonl"
        record = json.loads(path.read_text())
        record["data"]["value"] = "changed"
        path.write_text(json.dumps(record) + "\n")
        with self.assertRaisesRegex(ValueError, "identity/schema"):
            self.store.verify()


if __name__ == "__main__":
    unittest.main()
