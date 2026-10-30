"""Exercise provenance and revisions using fake data in temporary storage."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile

from health_bank.project import build
from health_bank.store import Store


def run():
    with tempfile.TemporaryDirectory(prefix="health-evidence-demo-") as tmp:
        root = Path(tmp) / "store"
        store = Store(root)
        incoming = Path(tmp) / "synthetic-source.txt"
        incoming.write_text("Fictional evidence for a synthetic patient. Measurement: 0 demo units.")
        first = store.add_artifact(incoming, "synthetic-source")
        assertion = {
            "patient_id": "demo-patient", "lane": "observations", "kind": "synthetic_measurement",
            "source": {"system": "synthetic-source", "artifact_sha256": first["artifact_sha256"],
                       "record_key": "demo-measurement", "locator": "Measurement sentence"},
            "effective_time": {"start": "2030-01", "precision": "month", "original": "January 2030"},
            "data": {"value": "0", "unit": "demo units", "synthetic": True},
        }
        original_id = store.add_record(assertion)
        assert store.add_record(assertion) == original_id
        second_source = Path(tmp) / "synthetic-reexport.txt"
        second_source.write_text("Fictional reexport of the same assertion. Measurement: 0 demo units.")
        second = store.add_artifact(second_source, "synthetic-source")
        recaptured = deepcopy(assertion)
        recaptured["source"]["artifact_sha256"] = second["artifact_sha256"]
        recaptured["source"]["locator"] = "Reexport measurement sentence"
        assert store.add_record(recaptured) == original_id
        revision = deepcopy(assertion)
        revision["status"] = "corrected"
        revision["data"]["value"] = "1"
        assert store.add_record(revision) != original_id
        store.add_record({
            "patient_id": "demo-patient", "lane": "coverage", "kind": "surface-inspection",
            "status": "partially_extracted", "source": {
                "system": "synthetic-source", "artifact_sha256": first["artifact_sha256"],
                "record_key": "demo-source-scope", "locator": "whole fictional source"},
            "data": {"extraction_status": "partially_extracted", "limitations": ["Synthetic demo only"]},
        })
        verification = store.verify()
        assert verification["verified"] and verification["records"] == 3
        assert verification["evidence_assertions"] == 4
        projection = build(root)
        assert (store.local / "derived/patient_snapshot.json").is_file()
        print(json.dumps({"synthetic_only": True, "verified": verification["verified"],
                          "artifacts": verification["artifacts"], "records": verification["records"],
                          "evidence_assertions": verification["evidence_assertions"],
                          "navigation_views_built": True}, indent=2))


if __name__ == "__main__":
    run()
