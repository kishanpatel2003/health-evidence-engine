from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from health_bank.project import _coverage, _observation_groups, build
from health_bank.store import Store


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = Store(self.root)
        incoming = self.store.local / "incoming"
        incoming.mkdir()
        path = incoming / "synthetic.xml"
        path.write_text("<synthetic-document/>")
        self.artifact = self.store.add_artifact(path, "synthetic-source")

    def add_section(self, document_created, label, *, effective="2020-01-01", patient="synthetic-patient", key=None, source="synthetic-source",document_title=None):
        return self.store.add_record({
            "patient_id": patient, "lane": "medications", "kind": "cda_section",
            "source": {"system": source, "artifact_sha256": self.artifact["artifact_sha256"], "locator": "/document/section[1]", "record_key": key or label},
            "effective_time": {"start": effective, "precision": "day"},
            "captured_at": "2026-10-22T20:00:00Z",
            "status": "unknown",
            "data": {"label": label, "text": "syntheticneedle source narrative", "document_title":document_title,"document_created": document_created, "section_code": "synthetic-section", "section_title": "Synthetic medications", "source_xml": "<section>synthetic source</section>"},
        })

    def test_patient_health_summary_precedes_a_newer_encounter_export(self):
        preferred=self.add_section('2025-01-01','Longitudinal portal summary',document_title='Patient Health Summary')
        self.add_section('2026-01-01','Historical encounter snapshot',document_title='Summary of Care')
        build(self.root)
        lane=self.snapshot()['patients'][0]['source_reported_sections']['medications']
        selected=lane['source_sections'][0]
        self.assertEqual(selected['sections'][0]['record_id'],preferred)
        self.assertEqual(selected['other_document_section_count'],1)
        self.assertFalse(lane['current_state_established'])
        orientation = (self.store.local / "derived" / "AGENT_START.md").read_text()
        self.assertIn("prioritizes Patient Health Summary", orientation)
        self.assertIn("not validated unique clinical event counts", orientation)

    def snapshot(self):
        return json.loads((self.store.local / "derived" / "patient_snapshot.json").read_text())

    def test_document_creation_selects_source_section_without_claiming_current_use(self):
        old = self.add_section("20240101090000-0500", "Older source section", effective="2026-01-01")
        new = self.add_section("20250101090000-0500", "Newer source section", effective="2010-01-01")
        # A later individual observation must not displace the selected source section.
        self.store.add_record({
            "patient_id": "synthetic-patient", "lane": "medications", "kind": "cda_entry",
            "source": {"system": "synthetic-source", "artifact_sha256": self.artifact["artifact_sha256"], "locator": "/document/entry[9]", "record_key": "entry-9"},
            "effective_time": {"start": "2026-10-01", "precision": "day"}, "data": {"label": "Later historical entry"},
        })
        build(self.root)
        summary = self.snapshot()["patients"][0]["source_reported_sections"]["medications"]
        self.assertFalse(summary["current_state_established"])
        group = summary["source_sections"][0]
        self.assertEqual(group["source_reported_as_of"], ["20250101090000-0500"])
        self.assertEqual([item["record_id"] for item in group["sections"]], [new])
        self.assertEqual(group["older_section_count"], 1)
        self.assertNotEqual(new, old)
        self.assertIn("clinical review", group["as_of_caveat"])

    def test_source_citations_and_additional_evidence_are_preserved(self):
        identifier = self.add_section("2025-01-01", "Cited section", key="section-key")
        record = next(self.store.iter_records("medications"))
        path = self.store.local / "incoming" / "second.xml"
        path.write_text("<synthetic-document revision='second-evidence'/>")
        second = self.store.add_artifact(path, "synthetic-source")
        record["source"]["artifact_sha256"] = second["artifact_sha256"]
        record["source"]["locator"] = {"page": 2, "section": "synthetic"}
        record["captured_at"] = "2026-10-23T20:00:00Z"
        self.assertEqual(self.store.add_record(record), identifier)
        build(self.root)
        pointer = self.snapshot()["patients"][0]["source_reported_sections"]["medications"]["source_sections"][0]["sections"][0]
        self.assertEqual(pointer["record_id"], identifier)
        self.assertEqual(pointer["source"]["artifact_sha256"], self.artifact["artifact_sha256"])
        self.assertEqual(pointer["source"]["locator"], "/document/section[1]")
        self.assertEqual(len(pointer["evidence"]), 2)
        self.assertEqual({item["source"]["artifact_sha256"] for item in pointer["evidence"]}, {self.artifact["artifact_sha256"], second["artifact_sha256"]})
        self.assertNotIn("source_xml", pointer["data"])

    def test_rerun_is_deterministic_including_sqlite(self):
        self.add_section("2025-01-01", "Stable section")
        first_stats = build(self.root)
        first = {path.name: path.read_bytes() for path in (self.store.local / "derived").iterdir()}
        second_stats = build(self.root)
        second = {path.name: path.read_bytes() for path in (self.store.local / "derived").iterdir()}
        self.assertEqual(first_stats, second_stats)
        self.assertEqual(first, second)

    def test_fts_search_returns_joinable_record_ids(self):
        identifier = self.add_section("2025-01-01", "Searchable section")
        stats = build(self.root)
        with closing(sqlite3.connect(self.store.local / "derived" / "index.sqlite")) as connection:
            if not stats["fts_available"]:
                self.skipTest("This SQLite build does not provide FTS5")
            rows = connection.execute("SELECT r.record_id, r.source_json, e.source_json FROM records_fts f JOIN records r ON r.record_id = f.record_id JOIN evidence e ON e.record_id = r.record_id WHERE records_fts MATCH ?", ("syntheticneedle",)).fetchall()
        self.assertEqual(rows[0][0], identifier)
        self.assertEqual(json.loads(rows[0][1])["artifact_sha256"], self.artifact["artifact_sha256"])
        self.assertEqual(json.loads(rows[0][2])["locator"], "/document/section[1]")

    def test_undated_sections_and_ties_remain_visible_and_patients_separate(self):
        first = self.add_section("2025-01-01", "Tied section one")
        second = self.add_section("2025-01-01", "Tied section two")
        undated = self.add_section(None, "Undated section")
        other = self.add_section("2026-01-01", "Other patient", patient="synthetic-other-patient")
        build(self.root)
        patients = {item["patient_id"]: item for item in self.snapshot()["patients"]}
        group = patients["synthetic-patient"]["source_reported_sections"]["medications"]["source_sections"][0]
        self.assertEqual({item["record_id"] for item in group["sections"]}, {first, second})
        self.assertEqual([item["record_id"] for item in group["undated_sections"]], [undated])
        self.assertNotIn(other, {item["record_id"] for item in group["sections"]})

    def test_coverage_gaps_expose_states_and_source_citations(self):
        identifier = self.store.add_record({
            "patient_id": "synthetic-patient", "lane": "coverage", "kind": "capture-scope",
            "status": "partially_extracted", "source": {"system": "synthetic-source", "artifact_sha256": self.artifact["artifact_sha256"], "locator": "synthetic-scope", "record_key": "coverage-one"},
            "data": {"extraction_status": "partially_extracted", "unsupported_attachments": ["synthetic scan"], "next_action": "Inspect the synthetic attachment"},
        })
        build(self.root)
        coverage = self.snapshot()["patients"][0]["coverage"]
        self.assertFalse(coverage["completeness_established"])
        self.assertEqual(coverage["open_or_unresolved"][0]["record_id"], identifier)
        self.assertEqual(coverage["open_or_unresolved"][0]["states"]["extraction_status"], "partially_extracted")
        self.assertEqual(coverage["observations"][0]["source"]["locator"], "synthetic-scope")

    def add_coverage(self, captured_at, status, *, key="synthetic-surface", kind="surface-inspection", source="synthetic-source", patient="synthetic-patient"):
        return self.store.add_record({
            "patient_id": patient, "lane": "coverage", "kind": kind, "status": status,
            "source": {"system": source, "artifact_sha256": self.artifact["artifact_sha256"], "record_key": key, "locator": "synthetic-coverage-scope"},
            "captured_at": captured_at,
            "data": {"extraction_status": status},
        })

    def test_resolved_surface_keeps_gap_history_without_a_current_gap(self):
        older = self.add_coverage("2025-01-01T12:00:00Z", "partially_extracted")
        resolved = self.add_coverage("2025-02-01T12:00:00Z", "extracted")
        build(self.root)
        coverage = self.snapshot()["patients"][0]["coverage"]
        self.assertEqual({item["record_id"] for item in coverage["observations"]}, {older, resolved})
        self.assertEqual([item["record_id"] for item in coverage["current_observations"]], [resolved])
        self.assertEqual(coverage["open_or_unresolved"], [])
        self.assertEqual([item["record_id"] for item in coverage["historical_gaps"]], [older])

    def test_unknown_coverage_keys_remain_separate(self):
        older = self.add_coverage("2025-01-01T12:00:00Z", "partially_extracted", key=None)
        newer = self.add_coverage("2025-02-01T12:00:00Z", "extracted", key=None)
        build(self.root)
        coverage = self.snapshot()["patients"][0]["coverage"]
        self.assertEqual({item["record_id"] for item in coverage["current_observations"]}, {older, newer})
        self.assertEqual([item["record_id"] for item in coverage["open_or_unresolved"]], [older])
        self.assertEqual(coverage["historical_gaps"], [])

    def test_coverage_ties_retain_conflicting_observation_states(self):
        pending = self.add_coverage("2025-02-01T12:00:00-05:00", "partially_extracted")
        resolved = self.add_coverage("2025-02-01T17:00:00Z", "extracted")
        build(self.root)
        coverage = self.snapshot()["patients"][0]["coverage"]
        self.assertEqual({item["record_id"] for item in coverage["current_observations"]}, {pending, resolved})
        self.assertEqual([item["record_id"] for item in coverage["open_or_unresolved"]], [pending])

    def test_newest_linked_evidence_selects_coverage_state(self):
        gap = self.add_coverage("2025-01-01T12:00:00Z", "partially_extracted")
        resolved = self.add_coverage("2025-02-01T12:00:00Z", "extracted")
        record = next(item for item in self.store.iter_records("coverage") if item["record_id"] == gap)
        record["source"]["locator"] = "synthetic-coverage-reinspection"
        record["captured_at"] = "2025-03-01T12:00:00Z"
        self.assertEqual(self.store.add_record(record), gap)
        build(self.root)
        coverage = self.snapshot()["patients"][0]["coverage"]
        selected = coverage["current_observations"]
        self.assertEqual([item["record_id"] for item in selected], [gap])
        self.assertEqual(selected[0]["captured_at"], "2025-01-01T12:00:00Z")
        self.assertEqual(selected[0]["latest_evidence_capture"], "2025-03-01T12:00:00Z")
        self.assertEqual([item["record_id"] for item in coverage["open_or_unresolved"]], [gap])
        self.assertIn(resolved, {item["record_id"] for item in coverage["observations"]})

    def test_coverage_selection_does_not_cross_patient_source_or_kind(self):
        identifiers = {
            self.add_coverage("2025-01-01T12:00:00Z", "partially_extracted"),
            self.add_coverage("2025-02-01T12:00:00Z", "extracted", source="synthetic-other-source"),
            self.add_coverage("2025-02-01T12:00:00Z", "extracted", kind="different-inspection-kind"),
            self.add_coverage("2025-02-01T12:00:00Z", "extracted", patient="synthetic-other-patient"),
        }
        coverage = _coverage(list(self.store.iter_records("coverage")), {})
        self.assertEqual({item["record_id"] for item in coverage["current_observations"]}, identifiers)

    def test_empty_store_has_no_patient_or_clinical_negatives(self):
        stats = build(self.root)
        self.assertEqual(stats["records"], 0)
        self.assertEqual(self.snapshot()["patients"], [])
        self.assertIn("Missing lane records", " ".join(self.snapshot()["limitations"]))

    def add_observation(self, document, *, key="synthetic-observation", value="0", identified=True, changes=None):
        data = {
            "document_id": document,
            "identifiers": [{"root": "synthetic-identifier"}] if identified else [],
            "codes": [{"code": "synthetic-code"}],
            "values": [{"value": value, "unit": "synthetic"}],
            "source_xml": {"tag": "synthetic-observation", "attributes": {"method": "synthetic-method"}},
            "parent_entry_key": "synthetic-parent",
        }
        data.update(changes or {})
        return self.store.add_record({
            "patient_id": "synthetic-patient", "lane": "observations", "kind": "clinical_observation",
            "source": {"system": "synthetic-source", "artifact_sha256": self.artifact["artifact_sha256"], "record_key": key},
            "effective_time": {"start": "2025-01-01", "precision": "day"}, "data": data,
        })

    def test_observation_groups_link_identical_source_representations(self):
        first = self.add_observation("first-document")
        second = self.add_observation("another-document")
        changed = self.add_observation("amended-document", value="1")
        self.add_observation("other", key="unidentified-one", identified=False)
        self.add_observation("other", key="unidentified-two", identified=False)
        build(self.root)
        with closing(sqlite3.connect(self.store.local/'derived'/'index.sqlite')) as connection:
            counts=connection.execute('SELECT representation_count FROM observation_groups ORDER BY representation_count').fetchall()
            self.assertEqual(counts,[(1,),(1,),(1,),(2,)])
            group=connection.execute('SELECT group_id FROM observation_members WHERE record_id=?',(first,)).fetchone()[0]
            members={r[0] for r in connection.execute('SELECT record_id FROM observation_members WHERE group_id=?',(group,))}
            self.assertEqual(members,{first,second})
            self.assertNotIn(changed,members)

    def test_xml_only_clinical_differences_do_not_merge(self):
        self.add_observation("first-document", changes={"source_xml": {"tag": "observation", "attributes": {"method": "synthetic-alpha"}}})
        self.add_observation("second-document", changes={"source_xml": {"tag": "observation", "attributes": {"method": "synthetic-beta"}}})
        build(self.root)
        with closing(sqlite3.connect(self.store.local / "derived" / "index.sqlite")) as connection:
            rows = connection.execute("SELECT data_json, representation_count FROM observation_groups ORDER BY group_id").fetchall()
        self.assertEqual([row[1] for row in rows], [1, 1])
        # Compact display can match even though full clinical identity differs.
        self.assertEqual(rows[0][0], rows[1][0])
        self.assertNotIn("source_xml", json.loads(rows[0][0]))

    def test_nested_xml_and_parent_relationships_remain_in_identity(self):
        self.add_observation("baseline", changes={"values": [{"value": "0", "unit": "synthetic", "source_xml": {"method": "synthetic-alpha"}}]})
        self.add_observation("nested-xml-difference", changes={"values": [{"value": "0", "unit": "synthetic", "source_xml": {"method": "synthetic-beta"}}]})
        self.add_observation("parent-difference", changes={"values": [{"value": "0", "unit": "synthetic", "source_xml": {"method": "synthetic-alpha"}}], "parent_entry_key": "another-parent"})
        groups = _observation_groups(list(self.store.iter_records("observations")))
        self.assertEqual(len(groups), 3)
        self.assertTrue(all(len(group["record_ids"]) == 1 for group in groups))
        self.assertTrue(all("source_xml" not in group["data"]["values"][0] for group in groups))

    def test_document_section_metadata_is_excluded_but_other_context_is_retained(self):
        first = self.add_observation("first", changes={
            "section_title": "Synthetic first section", "section_code": "synthetic-first-code",
            "context": {"document_created": "2025-01-01", "section_title": "First", "specimen_context": "synthetic-same"},
        })
        second = self.add_observation("second", changes={
            "section_title": "Synthetic second section", "section_code": "synthetic-second-code",
            "context": {"document_created": "2026-01-01", "section_title": "Second", "specimen_context": "synthetic-same"},
        })
        third = self.add_observation("third", changes={
            "context": {"document_created": "2026-01-01", "specimen_context": "synthetic-different"},
        })
        groups = _observation_groups(list(self.store.iter_records("observations")))
        members = [set(group["record_ids"]) for group in groups]
        self.assertEqual(len(groups), 2)
        self.assertIn({first, second}, members)
        self.assertIn({third}, members)

    def test_null_flavor_identifier_does_not_establish_group_identity(self):
        self.add_observation("first", changes={"identifiers": [{"nullFlavor": "UNK"}]})
        self.add_observation("second", changes={"identifiers": [{"nullFlavor": "UNK"}]})
        groups = _observation_groups(list(self.store.iter_records("observations")))
        self.assertEqual(len(groups), 2)
        self.assertTrue(all(group["identity_basis"] == "record_only" for group in groups))


if __name__ == "__main__":
    unittest.main()
