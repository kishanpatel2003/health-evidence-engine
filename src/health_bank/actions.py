"""Sourced, append-only care coordination events and disposable ledger views.

Events live in agent_analysis: a coordination status is an interpretation of its
cited evidence, not a new prescription, appointment confirmation or clinical fact.
All patient inputs and rendered output stay under .local; stdout is aggregate only.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from .store import Store, _private_dir, canonical_bytes, content_hash

VERSION = 1
KIND = "care_action_event"
STATUSES = {
    "proposed", "agreed", "planned", "booking_reported", "scheduled_documented",
    "completed_reported", "completed_documented", "awaiting_clinician",
    "waiting_on_records", "paused", "cancelled", "declined",
}
FIELDS = {
    "title", "area", "owner", "status", "next_step", "due", "provider",
    "details", "dependencies", "outcome_to_capture", "decision_date",
}
BASES = {"caregiver_report", "clinical_document", "bank_evidence", "agent_interpretation"}
LABELS = {
    "booking_reported": "Booked — caregiver report",
    "scheduled_documented": "Scheduled — documented",
    "completed_reported": "Completed — reported",
    "completed_documented": "Completed — documented",
}


def _events(records):
    return [r for r in records if r["lane"] == "agent_analysis" and r["kind"] == KIND]


def _validate_event(event):
    required = {"event_id", "action_id", "sequence", "event_type", "created_at",
                "occurred_at", "changes", "outcomes", "evidence_record_ids", "note"}
    if not isinstance(event, dict) or set(event) != required:
        raise ValueError("Invalid care-action event fields")
    for key in ("event_id", "action_id"):
        if not isinstance(event[key], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", event[key]):
            raise ValueError("Invalid local action/event identifier")
    if type(event["sequence"]) is not int or event["sequence"] < 1:
        raise ValueError("Care-action sequence must be a positive integer")
    if event["event_type"] not in {"created", "updated", "outcome_recorded", "corrected"}:
        raise ValueError("Invalid care-action event type")
    if datetime.fromisoformat(event["created_at"].replace("Z", "+00:00")).tzinfo is None:
        raise ValueError("Care-action creation time requires a timezone")
    if event["occurred_at"] is not None and not isinstance(event["occurred_at"], dict):
        raise ValueError("Occurrence time must preserve source wording in an object or be null")
    if isinstance(event["occurred_at"], dict) and "original" not in event["occurred_at"]:
        raise ValueError("Occurrence time requires the original source wording")
    if not isinstance(event["changes"], dict) or set(event["changes"]) - FIELDS:
        raise ValueError("Invalid care-action changes")
    for field in ("title", "area", "owner", "next_step", "outcome_to_capture"):
        if field in event["changes"] and not isinstance(event["changes"][field], str):
            raise ValueError("Care-action display fields must be strings")
    if "due" in event["changes"] and event["changes"]["due"] is not None:
        due = event["changes"]["due"]
        if not isinstance(due, dict) or not isinstance(due.get("original"), str):
            raise ValueError("Due dates must preserve original timing wording")
    if "dependencies" in event["changes"] and (not isinstance(event["changes"]["dependencies"], list) or any(not isinstance(x, str) for x in event["changes"]["dependencies"])):
        raise ValueError("Dependencies must be local action IDs")
    if "status" in event["changes"] and event["changes"]["status"] not in STATUSES:
        raise ValueError("Invalid care-action status")
    if not isinstance(event["note"], str) or not event["note"].strip():
        raise ValueError("Care-action event requires an attributed explanation")
    refs = event["evidence_record_ids"]
    if not isinstance(refs, list) or not refs or any(not isinstance(x, str) for x in refs):
        raise ValueError("Care-action event requires evidence record IDs")
    if not isinstance(event["outcomes"], list):
        raise ValueError("Care-action outcomes must be an array")
    for outcome in event["outcomes"]:
        if not isinstance(outcome, dict) or not {"outcome_id", "description", "basis", "occurred_at", "evidence_record_ids"} <= outcome.keys():
            raise ValueError("Incomplete care-action outcome")
        if outcome["basis"] not in BASES or not isinstance(outcome["description"], str) or not outcome["description"].strip():
            raise ValueError("Invalid care-action outcome attribution")
        if not isinstance(outcome["outcome_id"], str) or not outcome["outcome_id"]:
            raise ValueError("Outcome requires a local identity")
        if not isinstance(outcome["evidence_record_ids"], list) or not outcome["evidence_record_ids"] or set(outcome["evidence_record_ids"]) - set(refs):
            raise ValueError("Outcome citations must be included in the event evidence")
        if outcome["occurred_at"] is not None and (not isinstance(outcome["occurred_at"], dict) or "original" not in outcome["occurred_at"]):
            raise ValueError("Outcome occurrence time requires source wording or null")
    if event["changes"].get("status") in {"completed_documented", "scheduled_documented"}:
        if not any(x["basis"] in {"clinical_document", "bank_evidence"} for x in event["outcomes"]):
            raise ValueError("Documented completion requires a documented outcome, not only a report")


def reduce_events(records):
    """Apply explicit per-action sequence, never clinical/capture recency."""
    actions = {}
    identities = set()
    for record in sorted(_events(records), key=lambda r: (r["patient_id"], r["data"]["event"]["action_id"], r["data"]["event"]["sequence"], r["record_id"])):
        event = record["data"]["event"]
        _validate_event(event)
        identity = (record["patient_id"], event["event_id"])
        if identity in identities:
            raise ValueError("Conflicting persisted care-action event identity")
        identities.add(identity)
        key = (record["patient_id"], event["action_id"])
        current = actions.get(key)
        if current is None:
            if event["sequence"] != 1 or event["event_type"] != "created":
                raise ValueError("Care-action history must begin with creation at sequence one")
            if not {"title", "status", "owner", "next_step"} <= event["changes"].keys():
                raise ValueError("Care-action creation is missing its initial state")
            current = {"patient_id": key[0], "action_id": key[1], "history": [], "outcomes": []}
            actions[key] = current
        elif event["sequence"] != len(current["history"]) + 1 or event["event_type"] == "created":
            raise ValueError("Care-action sequence conflict; append an explicit next revision")
        known_outcomes = {x["outcome_id"] for x in current["outcomes"]}
        for outcome in event["outcomes"]:
            if outcome["outcome_id"] in known_outcomes:
                raise ValueError("Outcome identity already exists; append a correction with a new identity")
            supersedes = outcome.get("supersedes_outcome_id")
            if supersedes is not None and supersedes not in known_outcomes:
                raise ValueError("Corrected outcome must cite an earlier outcome in this action")
            current["outcomes"].append(deepcopy(outcome))
            known_outcomes.add(outcome["outcome_id"])
        current.update(deepcopy(event["changes"]))
        current["history"].append({"record_id": record["record_id"], "source": record["source"],
                                   "event": deepcopy(event), "supporting_evidence": record["data"]["supporting_evidence"]})
    for (patient, _), action in actions.items():
        if any((patient, dependency) not in actions for dependency in action.get("dependencies", [])):
            raise ValueError("Dependency does not identify an action for the same patient")
    return [actions[key] for key in sorted(actions)]


def ingest(root, path):
    store = Store(root)
    lock_path = store.local / ".care-actions.lock"
    if lock_path.is_symlink():
        raise ValueError("Refusing a symbolic-link ledger lock")
    with os.fdopen(os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600), "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            return _ingest(store, path)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _ingest(store, path):
    path = Path(path).resolve()
    if not path.is_relative_to(store.local.resolve()):
        raise ValueError("Care-action input must be inside the private local workspace")
    batch = json.loads(path.read_text())
    if not isinstance(batch, dict) or set(batch) != {"schema_version", "patient_id", "events"} or type(batch.get("schema_version")) is not int or batch.get("schema_version") != VERSION or not isinstance(batch.get("events"), list):
        raise ValueError("Unsupported care-action batch schema")
    patient = batch["patient_id"]
    if not isinstance(patient, str) or not patient.strip():
        raise ValueError("Care-action batch requires an opaque patient ID")
    records = list(store.iter_records())
    by_id = {r["record_id"]: r for r in records}
    existing = {(r["patient_id"], r["data"]["event"]["event_id"]): r for r in _events(records)}
    prepared = []
    staged = list(records)
    for event in batch["events"]:
        _validate_event(event)
        refs = []
        for identifier in event["evidence_record_ids"]:
            ref = by_id.get(identifier)
            if ref is None or ref["patient_id"] != patient or not ref["source"].get("artifact_sha256") or ref["source"].get("locator") is None:
                raise ValueError("Care-action citation is missing, belongs to another patient, or lacks precise provenance")
            refs.append({"record_id": identifier, "lane": ref["lane"], "source": ref["source"], "captured_at": ref["captured_at"]})
        previous = existing.get((patient, event["event_id"]))
        if previous:
            if canonical_bytes(previous["data"]["event"]) != canonical_bytes(event):
                raise ValueError("A saved care-action event cannot be overwritten")
            continue
        for outcome in event["outcomes"]:
            if outcome["basis"] == "clinical_document":
                clinical_refs = [by_id[x] for x in outcome["evidence_record_ids"]]
                if not any(r["lane"] in {"notes", "orders", "results", "observations", "procedures", "encounters", "medications"}
                           and not r["source"]["system"].startswith(("caregiver", "health-bank")) for r in clinical_refs):
                    raise ValueError("Clinical outcome requires a clinical source, not only coordination analysis")
        data = {"schema_version": VERSION, "label": event["changes"].get("title", event["action_id"]),
                "question": "What decision, progress or outcome is being recorded for this care action?",
                "created_at": event["created_at"], "review_status": "coordination interpretation; inspect cited source attribution",
                "supporting_evidence": refs, "conflicting_evidence": [],
                "uncertainty": "Missing event dates, visit completion and outcomes remain unknown unless explicitly supplied in this event.",
                "evidence_coverage_freshness": {"source_capture_times": sorted({r["captured_at"] for r in refs}), "scope": "The cited records only; no new clinical assessment."},
                "event": event}
        record = {"patient_id": patient, "lane": "agent_analysis", "kind": KIND,
                  "source": {"system": "health-bank-care-actions", "artifact_sha256": None, "locator": None, "record_key": event["action_id"] + "/" + event["event_id"]},
                  "status": "unreviewed", "effective_time": None, "captured_at": event["created_at"], "data": data}
        # Validate the complete batch before mutating the store. This synthetic
        # identity is local validation scaffolding and never persisted.
        staged.append({**record, "record_id": "pending-" + str(len(prepared))})
        prepared.append(record)
        existing[(patient, event["event_id"])] = record
    reduce_events(staged)
    if prepared:
        artifact = store.add_artifact(path, "health-bank-care-actions", kind="care-action-event-batch")
        positions = {event["event_id"]: i for i, event in enumerate(batch["events"])}
        for record in prepared:
            record["source"].update(artifact_sha256=artifact["artifact_sha256"], locator=f"/events/{positions[record['data']['event']['event_id']]}")
            store.add_record(record)
    return {"events_added": len(prepared), "events_unchanged": len(batch["events"]) - len(prepared)}


def _write(path, content):
    _private_dir(path.parent)
    if path.is_symlink():
        raise ValueError("Refusing a symbolic-link ledger output")
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            os.chmod(temporary, 0o600)
            output.write(content)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _cell(value):
    return str(value if value is not None else "Not supplied").replace("|", "\\|").replace("\n", " ")


def write_views(root, records=None):
    store = Store(root)
    records = list(store.iter_records()) if records is None else records
    actions = reduce_events(records)
    events = _events(records)
    catalog = store.local / "catalog.jsonl"
    artifacts = {item["artifact_sha256"]: store.root / item["local_path"]
                 for item in (json.loads(line) for line in catalog.read_text().splitlines())} if catalog.exists() else {}
    multiple_patients = len({action["patient_id"] for action in actions}) > 1
    view = {"schema_version": VERSION, "kind": "disposable_care_action_projection",
            "input_sha256": content_hash(canonical_bytes(sorted(events, key=lambda r: r["record_id"]))),
            "recorded_through": max((r["data"]["event"]["created_at"] for r in events), key=lambda x: datetime.fromisoformat(x.replace("Z", "+00:00")), default=None),
            "limitations": ["Coordination interpretations retain source attribution; reports are not clinician-confirmed outcomes.",
                            "Explicit sequence orders ledger revisions; capture time is not an event date.",
                            "A booking is distinct from attendance, treatment initiation and clinical response."], "actions": actions}
    lines = ["# Care action ledger", "", "Decisions, follow-through and outcomes are recorded separately. Earlier entries remain in the event history.", "",
             f"Last recorded update: {view['recorded_through'] or 'No events recorded'}.", "",
             "A reported booking does not establish that the visit happened. Missing dates and outcomes remain unknown.", "",
             ("| Patient | Action | Status | Owner | Next step | Timing |" if multiple_patients else "| Action | Status | Owner | Next step | Timing |"),
             ("| --- | --- | --- | --- | --- | --- |" if multiple_patients else "| --- | --- | --- | --- | --- |")]
    for action in actions:
        due = action.get("due")
        timing = (due.get("original") or due.get("date")) if isinstance(due, dict) else due
        if isinstance(due, dict) and due.get("date"):
            timing = due["date"] + (" (" + due["original"] + ")" if due.get("original") else "")
        cells = [action["action_id"] + " — " + action["title"], LABELS.get(action["status"], action["status"].replace("_", " ").capitalize()), action["owner"], action["next_step"], timing]
        if multiple_patients:
            cells.insert(0, action["patient_id"])
        lines.append("| " + " | ".join(_cell(v) for v in cells) + " |")
    for action in actions:
        lines += ["", f"## {action['action_id']} {action['title']}", ""]
        if multiple_patients:
            lines += ["Patient context: " + action["patient_id"], ""]
        if action.get("details"):
            lines += [action["details"] if isinstance(action["details"], str) else json.dumps(action["details"], ensure_ascii=False), ""]
        if action.get("provider"):
            lines += ["Provider or office: " + _cell(action["provider"]), ""]
        if action.get("dependencies"):
            lines += ["Linked actions: " + ", ".join(action["dependencies"]), ""]
        if action["outcomes"]:
            lines += ["Recorded progress and outcomes:", ""]
            superseded = {x.get("supersedes_outcome_id") for x in action["outcomes"]}
            for outcome in action["outcomes"]:
                suffix = "; superseded by a later correction" if outcome["outcome_id"] in superseded else ""
                lines.append(f"- {outcome['description']} ({outcome['basis'].replace('_', ' ')}{suffix})")
        else:
            lines += ["No completion or clinical outcome has been recorded."]
        lines += ["", "Outcome to capture next: " + _cell(action.get("outcome_to_capture")), "", "Event history:", ""]
        for item in action["history"]:
            event = item["event"]
            lines.append(f"- {event['sequence']}. {event['note']}")
        refs = {ref["record_id"]: ref for item in action["history"] for ref in item["supporting_evidence"]}
        links = []
        for identifier, ref in sorted(refs.items()):
            artifact = artifacts.get(ref["source"]["artifact_sha256"])
            links.append(f"[{identifier[:16]}](<{artifact}>)" if artifact else identifier)
        lines += ["", "Sources: " + ", ".join(links) + ". Full record IDs, hashes and precise locators are in the JSON below."]
        lines += ["", "[Source records and full event history](care_action_ledger.json)"]
    derived = store.local / "derived"
    _write(derived / "care_action_ledger.json", json.dumps(view, ensure_ascii=False, indent=2, sort_keys=True).encode() + b"\n")
    _write(derived / "care_action_ledger.md", ("\n".join(lines) + "\n").encode())
    return {"care_actions": len(actions), "care_action_events": len(events)}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Maintain the private sourced care-action ledger")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("build")
    command = commands.add_parser("append")
    command.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    try:
        result = ingest(args.root, args.path) if args.command == "append" else {}
        result.update(write_views(args.root))
    except (ValueError, OSError, KeyError, TypeError):
        print("Care-action operation failed; inspect the private input and provenance. No patient content is printed.")
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
