from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from health_bank.actions import ingest, write_views
from health_bank.project import build
from health_bank.store import Store


class CareActionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root)
        self.incoming = self.store.local / "incoming"
        self.incoming.mkdir()
        source = self.incoming / "synthetic-source.txt"
        source.write_text("Synthetic source for coordination tests.")
        artifact = self.store.add_artifact(source, "synthetic-clinic")
        def evidence(patient, lane, system):
            return self.store.add_record({"patient_id": patient, "lane": lane, "kind": "synthetic-evidence",
                "source": {"system": system, "artifact_sha256": artifact["artifact_sha256"], "locator": "/synthetic", "record_key": patient + lane},
                "data": {"label": "Synthetic source"}})
        self.report = evidence("synthetic-patient", "patient_reported", "caregiver-chat")
        self.clinical = evidence("synthetic-patient", "notes", "synthetic-clinic")
        self.other = evidence("synthetic-other", "notes", "synthetic-clinic")

    def event(self, sequence=1, status="planned", evidence=None):
        return {"event_id": f"synthetic-event-{sequence}", "action_id": "SYN-001", "sequence": sequence,
            "event_type": "created" if sequence == 1 else "updated", "created_at": "2025-02-01T10:00:00Z",
            "occurred_at": None, "changes": {"status": status, **({"title": "Synthetic consultation", "owner": "Synthetic caregiver", "next_step": "Record synthetic follow-up"} if sequence == 1 else {})},
            "outcomes": [], "evidence_record_ids": [evidence or self.report], "note": "Synthetic attributed update."}

    def append(self, *events):
        path = self.incoming / "batch.json"
        path.write_text(json.dumps({"schema_version": 1, "patient_id": "synthetic-patient", "events": list(events)}))
        return ingest(self.root, path)

    def view(self):
        write_views(self.root)
        return json.loads((self.store.local / "derived" / "care_action_ledger.json").read_text())

    def test_reported_booking_is_not_visit_completion_and_retry_is_idempotent(self):
        event = self.event(status="booking_reported")
        self.assertEqual(self.append(event)["events_added"], 1)
        before = (self.store.local / "canonical" / "agent_analysis.jsonl").read_bytes()
        self.assertEqual(self.append(event)["events_added"], 0)
        self.assertEqual(before, (self.store.local / "canonical" / "agent_analysis.jsonl").read_bytes())
        action = self.view()["actions"][0]
        self.assertEqual(action["status"], "booking_reported")
        self.assertEqual(action["outcomes"], [])
        self.assertIsNone(action["history"][0]["event"]["occurred_at"])
        self.assertEqual(action["history"][0]["supporting_evidence"][0]["record_id"], self.report)

    def test_completion_then_correction_preserves_both_outcomes_and_uses_sequence(self):
        self.append(self.event())
        completed = self.event(2, "completed_documented", self.clinical)
        completed["outcomes"] = [{"outcome_id": "synthetic-result-1", "description": "Synthetic documented result.", "basis": "clinical_document", "occurred_at": None, "evidence_record_ids": [self.clinical]}]
        correction = self.event(3, "awaiting_clinician", self.clinical)
        correction["event_type"] = "corrected"
        correction["created_at"] = "2025-01-01T10:00:00Z"
        correction["outcomes"] = [{"outcome_id": "synthetic-result-2", "supersedes_outcome_id": "synthetic-result-1", "description": "Synthetic corrected result.", "basis": "clinical_document", "occurred_at": None, "evidence_record_ids": [self.clinical]}]
        self.append(completed, correction)
        action = self.view()["actions"][0]
        self.assertEqual(action["status"], "awaiting_clinician")
        self.assertEqual(len(action["history"]), 3)
        self.assertEqual(len(action["outcomes"]), 2)
        self.assertEqual(action["outcomes"][0]["description"], "Synthetic documented result.")

    def test_cross_patient_citation_and_false_clinical_attribution_fail_before_mutation(self):
        before = self.store.stats()["records"]
        with self.assertRaises(ValueError):
            self.append(self.event(evidence=self.other))
        false_document = self.event(status="completed_documented")
        false_document["outcomes"] = [{"outcome_id": "synthetic-false", "description": "Synthetic unverified result.", "basis": "clinical_document", "occurred_at": None, "evidence_record_ids": [self.report]}]
        with self.assertRaises(ValueError):
            self.append(false_document)
        self.assertEqual(before, self.store.stats()["records"])

    def test_event_rewrite_and_sequence_gap_are_rejected(self):
        original = self.event()
        self.append(original)
        changed = deepcopy(original)
        changed["changes"]["status"] = "cancelled"
        with self.assertRaises(ValueError):
            self.append(changed)
        with self.assertRaises(ValueError):
            self.append(self.event(3))
        self.assertEqual(len(self.view()["actions"][0]["history"]), 1)

    def test_ledger_is_rebuilt_and_discoverable_from_standard_projection(self):
        self.append(self.event())
        build(self.root)
        derived = self.store.local / "derived"
        before = {name: (derived / name).read_bytes() for name in ("care_action_ledger.json", "care_action_ledger.md", "AGENT_START.md")}
        build(self.root)
        self.assertEqual(before, {name: (derived / name).read_bytes() for name in before})
        self.assertIn("care_action_ledger.md", before["AGENT_START.md"].decode())


if __name__ == "__main__":
    unittest.main()
