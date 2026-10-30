"""Deterministic local navigation views; no clinical inference or recommendations.

Generated output is plaintext and must remain under the ignored .local directory.
The snapshot describes source assertions, not an inferred current medication list.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
from typing import Any

from .store import Store, _private_dir, _read_jsonl, canonical_bytes, content_hash


SUMMARY_LANES = ("medications", "conditions", "allergies", "immunizations")
LIMITATIONS = [
    "This is an evidence navigation view, not a clinical assessment or a complete medical history.",
    "Patient Health Summary sections take priority when available; document creation time selects within the preferred source sections, not the latest observation or capture.",
    "A summary generation date does not establish clinical review, current medication use, or absence of disease.",
    "Historical assertions, revisions and conflicting sources remain in canonical lanes; unresolved ties are retained.",
    "Missing lane records do not establish a documented clinical negative. Read coverage and source artifacts.",
    "Observation groups organize identical source representations; they are not validated unique clinical events or a basis for event incidence counts.",
]
GAP_STATES = {
    "unknown", "unverified", "not_captured", "partially_extracted", "partial_extraction",
    "unavailable", "partial", "failed", "error", "pending", "initial_collecting", "incomplete",
}


def _data(record: dict[str, Any]) -> dict[str, Any]:
    value = record.get("data")
    return value if isinstance(value, dict) else {}


def _context(record: dict[str, Any], key: str) -> Any:
    data = _data(record)
    if key in data:
        return data[key]
    context = data.get("context")
    return context.get(key) if isinstance(context, dict) else None


def _time_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    if isinstance(value, dict):
        for key in ("value", "start", "original"):
            if isinstance(value.get(key), str) and value[key].strip():
                return value[key]
    return None


def _time_key(value: Any) -> tuple[int, ...] | None:
    """Sort recognizable ISO/CDA times without changing their stored precision.

    Defaults are internal ordering boundaries only, never emitted as clinical dates.
    A timezone is honored when provided; absent zones remain source-local ordering.
    """
    text = _time_text(value)
    if text is None:
        return None
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        match = re.fullmatch(r"(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?(?:\.(\d+))?(Z|[+-]\d{4})?", text)
        if not match:
            # ISO year/month precision is also retained and ordered conservatively.
            match_partial = re.fullmatch(r"(\d{4})(?:-(\d{2}))?", text)
            if not match_partial:
                return None
            try:
                stamp = datetime(int(match_partial[1]), int(match_partial[2] or 1), 1)
            except ValueError:
                return None
        else:
            parts = match.groups()
            iso = f"{parts[0]}-{parts[1] or '01'}-{parts[2] or '01'}T{parts[3] or '00'}:{parts[4] or '00'}:{parts[5] or '00'}"
            if parts[6]:
                iso += "." + parts[6]
            if parts[7]:
                iso += "+00:00" if parts[7] == "Z" else parts[7][:3] + ":" + parts[7][3:]
            try:
                stamp = datetime.fromisoformat(iso)
            except ValueError:
                return None
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone(timezone.utc)
    return (stamp.year, stamp.month, stamp.day, stamp.hour, stamp.minute, stamp.second, stamp.microsecond)


def _label(record: dict[str, Any]) -> str:
    data = _data(record)
    for value in (data.get("label"), _context(record, "section_title"), data.get("document_title"), data.get("title")):
        if isinstance(value, str) and value:
            return value
    return record["kind"]


def _effective_start(record: dict[str, Any]) -> str | None:
    value = record.get("effective_time")
    return _time_text(value.get("start")) if isinstance(value, dict) else None


def _visible_data(record: dict[str, Any]) -> Any:
    """Source XML is recoverable in canonical data/artifacts, not duplicated in orientation."""
    def compact(value):
        if isinstance(value,dict):
            return {key:compact(item) for key,item in value.items() if not key.endswith("_xml")}
        if isinstance(value,list):
            return [compact(item) for item in value]
        return value
    return compact(record.get("data"))


def _pointer(record: dict[str, Any], evidence: dict[str, list[dict[str, Any]]], include_data: bool = False) -> dict[str, Any]:
    value = {
        "record_id": record["record_id"], "patient_id": record["patient_id"],
        "lane": record["lane"], "kind": record["kind"], "label": _label(record),
        "status": record.get("status"), "effective_time": record.get("effective_time"),
        "captured_at": record.get("captured_at"), "source": record["source"],
        "evidence": evidence.get(record["record_id"], []),
        "canonical_path": f".local/canonical/{record['lane']}.jsonl",
    }
    if include_data:
        value["data"] = _visible_data(record)
    return value


def _source_sections(records: list[dict[str, Any]], evidence: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for lane in SUMMARY_LANES:
        groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for record in records:
            if record["lane"] == lane and record["kind"] == "cda_section":
                identity = _context(record, "section_code") or _context(record, "section_title") or record["kind"]
                groups[(record["source"]["system"], canonical_bytes(identity).decode())].append(record)
        selected_groups = []
        for (source_system, section_identity), sections in sorted(groups.items()):
            summary_sections = [record for record in sections if str(_context(record,"document_title") or "").strip().lower() == "patient health summary"]
            candidates = summary_sections or sections
            dated = [(key, record) for record in candidates if (key := _time_key(_context(record, "document_created"))) is not None]
            latest = max((key for key, _ in dated), default=None)
            selected = [record for key, record in dated if key == latest]
            undated = [record for record in candidates if _time_key(_context(record, "document_created")) is None]
            selected_groups.append({
                "source_system": source_system, "section_identity": json.loads(section_identity),
                "selection_basis": "Patient Health Summary when present, then latest recognizable document_created within this source and section; all ties retained",
                "source_reported_as_of": sorted({_time_text(_context(record, "document_created")) for record in selected}),
                "as_of_caveat": "Document generation time; clinical review and current treatment are not established.",
                "sections": [_pointer(record, evidence, True) for record in sorted(selected, key=lambda item: item["record_id"])],
                "undated_sections": [_pointer(record, evidence, True) for record in sorted(undated, key=lambda item: item["record_id"])],
                "older_section_count": len(dated) - len(selected),
                "other_document_section_count": len(sections) - len(candidates),
            })
        result[lane] = {
            "assertion_count": sum(record["lane"] == lane for record in records),
            "source_sections": selected_groups,
            "current_state_established": False,
        }
    return result


def _activity(records: list[dict[str, Any]], evidence: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    dates = [(key, text) for record in records if (text := _effective_start(record)) and (key := _time_key(text)) is not None]
    dates.sort()
    captures = sorted({record["captured_at"] for record in records}, key=lambda text: (_time_key(text) or (), text))
    recent = sorted(records, key=lambda record: (_time_key(_effective_start(record)) or (), record["record_id"]), reverse=True)[:8]
    return {
        "record_count": len(records),
        "source_status_counts": dict(sorted(Counter(record.get("status", "unknown") for record in records).items())),
        "earliest_effective_start": dates[0][1] if dates else None,
        "latest_effective_start": dates[-1][1] if dates else None,
        "records_without_orderable_effective_start": len(records) - len(dates),
        "latest_capture": captures[-1] if captures else None,
        "recent_assertions_by_effective_start": [_pointer(record, evidence) for record in recent],
        "ordering_caveat": "Clinical applicability dates are source dates with original precision; capture time is separate.",
    }


def _coverage(records: list[dict[str, Any]], evidence: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Project the most recently observed collection state, preserving history.

    Capture recency is appropriate for coverage inspection state only. This rule
    does not select clinically true assertions in any medical lane.
    """
    coverage = sorted((record for record in records if record["lane"] == "coverage"), key=lambda record: record["record_id"])
    groups: dict[bytes, list[tuple[tuple[int, ...] | None, dict[str, Any]]]] = defaultdict(list)
    pointers = {}
    gaps = {}
    for record in coverage:
        captures = [record.get("captured_at"), *(item.get("captured_at") for item in evidence.get(record["record_id"], []))]
        recognized = [(key, value) for value in captures if (key := _time_key(value)) is not None]
        latest_key, latest_capture = max(recognized, default=(None, None))
        pointer = _pointer(record, evidence, True)
        pointer["latest_evidence_capture"] = latest_capture
        pointers[record["record_id"]] = pointer
        source_key = record["source"].get("record_key")
        item_identity = {"record_key": source_key} if isinstance(source_key, str) and source_key.strip() else {"record_id": record["record_id"]}
        identity = {
            "patient_id": record["patient_id"], "source_system": record["source"]["system"],
            "kind": record["kind"], **item_identity,
        }
        groups[canonical_bytes(identity)].append((latest_key, record))
        data = _data(record)
        states = {key: data[key] for key in ("inspection_status", "capture_status", "extraction_status", "collection_status", "status") if key in data}
        states["record_status"] = record.get("status", "unknown")
        reasons = {key: value for key, value in states.items() if isinstance(value, str) and value.lower().replace("-", "_").replace(" ", "_") in GAP_STATES}
        details = {key: data[key] for key in ("gaps", "limitations", "unsupported_fields", "unsupported_attachments", "errors", "next_action") if data.get(key)}
        if reasons or details:
            gaps[record["record_id"]] = {
                "record_id": record["record_id"], "source": record["source"],
                "latest_evidence_capture": latest_capture, "states": reasons, "details": details,
            }
    current = set()
    for items in groups.values():
        latest = max((key for key, _ in items), key=lambda key: key or ())
        current.update(record["record_id"] for key, record in items if key == latest)
    return {
        "observations": [pointers[key] for key in sorted(pointers)],
        "current_observations": [pointers[key] for key in sorted(current)],
        "open_or_unresolved": [gaps[key] for key in sorted(current & gaps.keys())],
        "historical_gaps": [gaps[key] for key in sorted(gaps.keys() - current)],
        "current_selection": "Newest evidence capture per patient/source system/kind/source record key; ties retained. Unknown keys remain separate by record ID. This selects coverage observation state, not clinical truth.",
        "completeness_established": False,
    }


def _atomic_write(path: Path, content: bytes) -> None:
    if path.is_symlink():
        raise ValueError("Refusing a symbolic-link projection file")
    descriptor, name = tempfile.mkstemp(prefix=".projection-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


DOCUMENT_SECTION_PROVENANCE = frozenset({
    "document_id", "document_title", "document_created", "document_code",
    "document_coverage", "section_title", "section_code",
})


def _clinical_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Remove only recognized document/section metadata, keeping clinical XML.

    Extraction may expose only part of a source node as semantic fields. Raw XML,
    parent relationships and unknown fields must still distinguish representations.
    The optional context wrapper uses the same explicit metadata keys; unrelated
    nested objects are not pruned.
    """
    payload = {key: value for key, value in data.items() if key not in DOCUMENT_SECTION_PROVENANCE}
    if isinstance(payload.get("context"), dict):
        context = {key: value for key, value in payload["context"].items() if key not in DOCUMENT_SECTION_PROVENANCE}
        if context:
            payload["context"] = context
        else:
            del payload["context"]
    return payload


def _has_source_identifier(value: Any) -> bool:
    return isinstance(value, list) and any(
        isinstance(identifier, dict)
        and not identifier.get("nullFlavor")
        and any(isinstance(identifier.get(key), str) and identifier[key].strip() for key in ("root", "extension"))
        for identifier in value
    )


def _observation_groups(records):
    """Group exact source representations, without claiming clinical event identity."""
    groups = {}
    for record in records:
        data = record.get("data")
        if record["lane"] not in {"results", "observations"} or not isinstance(data, dict) or not data.get("values"):
            continue
        payload = _clinical_payload(data)
        identified = _has_source_identifier(payload.get("identifiers"))
        basis = "source_identifier_and_identical_full_payload" if identified else "record_only"
        identity = {
            "patient_id": record["patient_id"], "source_system": record["source"]["system"],
            "source_record_key": record["source"].get("record_key"),
            "lane": record["lane"], "kind": record["kind"],
            "effective_time": record.get("effective_time"), "status": record.get("status"),
            "data": payload,
        }
        if not identified:
            identity["record_id"] = record["record_id"]
        group_id = "obs_" + content_hash(canonical_bytes(identity))
        group = groups.setdefault(group_id, {
            "group_id": group_id, "patient_id": record["patient_id"], "lane": record["lane"],
            "identity_basis": basis, "effective_start": _effective_start(record),
            "status": record.get("status"), "data": _visible_data({"data": payload}), "record_ids": [],
        })
        group["record_ids"].append(record["record_id"])
    return [groups[key] for key in sorted(groups)]


def _sqlite(path: Path, records: list[dict[str, Any]], evidence: list[dict[str, Any]], metadata: dict[str, Any]) -> bool:
    if path.is_symlink():
        raise ValueError("Refusing a symbolic-link index file")
    descriptor, temporary = tempfile.mkstemp(prefix=".index-", dir=path.parent)
    os.close(descriptor)
    connection = None
    try:
        connection = sqlite3.connect(temporary)
        connection.execute("CREATE TABLE records (record_id TEXT PRIMARY KEY, lane TEXT NOT NULL, kind TEXT NOT NULL, effective_start TEXT, recorded_at TEXT, label TEXT, content_json TEXT NOT NULL, source_json TEXT NOT NULL)")
        connection.execute("CREATE INDEX records_lane_time ON records(lane, effective_start)")
        connection.execute("CREATE TABLE evidence (record_id TEXT NOT NULL, source_json TEXT NOT NULL, captured_at TEXT NOT NULL)")
        connection.execute("CREATE INDEX evidence_record ON evidence(record_id)")
        connection.execute("CREATE TABLE observation_groups (group_id TEXT PRIMARY KEY, patient_id TEXT, lane TEXT, identity_basis TEXT, effective_start TEXT, status TEXT, data_json TEXT, representation_count INTEGER)")
        connection.execute("CREATE TABLE observation_members (group_id TEXT, record_id TEXT PRIMARY KEY)")
        for group in _observation_groups(records):
            connection.execute("INSERT INTO observation_groups VALUES (?,?,?,?,?,?,?,?)",(
                group['group_id'],group['patient_id'],group['lane'],group['identity_basis'],
                group['effective_start'],group['status'],canonical_bytes(group['data']).decode(),len(group['record_ids'])))
            connection.executemany("INSERT INTO observation_members VALUES (?,?)",[(group['group_id'],identifier) for identifier in group['record_ids']])
        connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value_json TEXT NOT NULL)")
        try:
            connection.execute("CREATE VIRTUAL TABLE records_fts USING fts5(record_id UNINDEXED, lane UNINDEXED, label, content_json)")
            fts = True
        except sqlite3.OperationalError as error:
            if "no such module" not in str(error).lower():
                raise
            fts = False
        for record in records:
            content = canonical_bytes(record["data"]).decode()
            connection.execute("INSERT INTO records VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (
                record["record_id"], record["lane"], record["kind"], _effective_start(record),
                record["captured_at"], _label(record), content, canonical_bytes(record["source"]).decode(),
            ))
            if fts:
                semantic = canonical_bytes(_visible_data(record)).decode()
                connection.execute("INSERT INTO records_fts VALUES (?, ?, ?, ?)", (record["record_id"], record["lane"], _label(record), semantic))
        connection.executemany("INSERT INTO evidence VALUES (?, ?, ?)", [(item["record_id"], canonical_bytes(item["source"]).decode(), item["captured_at"]) for item in evidence])
        metadata = {**metadata, "fts_available": fts}
        connection.executemany("INSERT INTO metadata VALUES (?, ?)", [(key, canonical_bytes(value).decode()) for key, value in sorted(metadata.items())])
        connection.commit()
        connection.close()
        connection = None
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        return fts
    finally:
        if connection is not None:
            connection.close()
        Path(temporary).unlink(missing_ok=True)


def build(root: str | Path = ".") -> dict[str, Any]:
    """Build local evidence and care-action views; return aggregate-only statistics."""
    store = Store(root)
    with store.locked():
        records = sorted(store.iter_records(), key=lambda record: record["record_id"])
        evidence = sorted(_read_jsonl(store.local / "canonical" / "evidence.jsonl"), key=canonical_bytes)
        catalog = sorted(_read_jsonl(store.local / "catalog.jsonl"), key=canonical_bytes)
    derived = store.local / "derived"
    _private_dir(derived)
    by_evidence: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in evidence:
        by_evidence[item["record_id"]].append({"source": item["source"], "captured_at": item["captured_at"]})
    digest = content_hash(canonical_bytes({"records": records, "evidence": evidence, "catalog": catalog}))
    cutoff = max((item["captured_at"] for item in [*records, *evidence, *catalog]), key=lambda text: (_time_key(text) or (), text), default=None)
    by_patient: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_lane: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        by_patient[record["patient_id"]].append(record)
        by_lane[record["lane"]].append(record)
    counts = {"records": len(records), "patients": len(by_patient), "lanes_with_records": len(by_lane), "evidence_links": len(evidence), "artifacts": len({item["artifact_sha256"] for item in catalog})}
    metadata = {"schema_version": 1, "projection_version": 1, "input_sha256": digest, "evidence_captured_through": cutoff}
    patients = []
    for patient_id, patient_records in sorted(by_patient.items()):
        lane_names = sorted({record["lane"] for record in patient_records})
        patients.append({
            "patient_id": patient_id,
            "sources": sorted({record["source"]["system"] for record in patient_records}),
            "demographics_assertions": [_pointer(record, by_evidence, True) for record in patient_records if record["lane"] == "demographics" and record["kind"] == "patient_demographics"],
            "source_reported_sections": _source_sections(patient_records, by_evidence),
            "lane_activity": {lane: _activity([record for record in patient_records if record["lane"] == lane], by_evidence) for lane in lane_names},
            "timeline": _activity(patient_records, by_evidence),
            "coverage": _coverage(patient_records, by_evidence),
        })
    snapshot = {**metadata, "counts": counts, "limitations": LIMITATIONS, "patients": patients}
    lane_index = {**metadata, "lanes": {lane: {"assertion_count": len(items), "canonical_path": f".local/canonical/{lane}.jsonl", "records": [_pointer(record, by_evidence) for record in items]} for lane, items in sorted(by_lane.items())}}
    fts = _sqlite(derived / "index.sqlite", records, evidence, metadata)
    snapshot["search"] = {
        "sqlite_path": ".local/derived/index.sqlite", "fts_available": fts,
        "observation_grouping_caveat": "Groups describe exact source representations with full clinical payload identity, not validated unique clinical events. Group display omits XML; canonical records retain it.",
    }
    for filename, value in (("patient_snapshot.json", snapshot), ("lane_index.json", lane_index)):
        _atomic_write(derived / filename, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False).encode() + b"\n")
    start = [
        "# Agent start", "", "Read repository AGENTS.md before interpreting this bank.", "",
        "This deterministic projection is a navigation aid, not a clinical assessment.",
        f"Evidence captured through: {cutoff or 'no captured evidence'}. This is not a clinical event date.",
        f"Input digest: `{digest}`.", "",
        f"Available evidence: {counts['patients']} patient(s), {counts['records']} assertions, {counts['artifacts']} artifacts and {counts['evidence_links']} evidence links.", "",
        "1. Read `patient_snapshot.json` for patient-separated source sections, timeline statistics and coverage gaps.",
        "2. Use `lane_index.json` to select assertion IDs and source locators.",
        "3. Read `.local/canonical/<lane>.jsonl` and `evidence.jsonl` for full assertions and provenance.",
        "4. Open the hashed source artifact before making material clinical claims.", "",
        "Medication, condition, allergy and immunization orientation prioritizes Patient Health Summary sections when available, then document generation time within the preferred source sections. Encounter-specific exports remain in canonical lanes. Inclusion does not establish current treatment or recent clinical review. Undated preferred sections and tied revisions stay visible.", "",
        "Missing records are not documented negatives. Coverage observations and unresolved gaps remain part of the snapshot. Agent interpretations belong in the agent_analysis lane with citations and freshness limits.", "",
        "Coverage keeps all observation history and selects current observations per patient/source/kind/stable source item by newest evidence capture, retaining ties. Missing item keys stay separate. A later completed inspection can resolve an earlier collection gap; this recency policy applies to collection state, not clinical truth.", "",
        f"SQLite search: `index.sqlite`; FTS5 available: {str(fts).lower()}.",
        "When FTS5 is available: `SELECT record_id, lane, label FROM records_fts WHERE records_fts MATCH ?;` Join record IDs to records/evidence for provenance. Bind search terms as parameters.", "",
        "SQLite observation_groups and observation_members organize exact source representations. Their identity retains full clinical data and raw XML while excluding document/section metadata; their display is compact. Group counts are not validated unique clinical event counts. Read member records and evidence before analysis.", "",
    ]
    from .actions import write_views
    ledger_counts = write_views(root, records)
    start += [
        "## Care action ledger", "",
        "Read `care_action_ledger.md` for current coordination commitments, next steps and recorded outcomes; `care_action_ledger.json` contains complete event history and source record/artifact/locator citations.", "",
        "Append sourced updates with `python -m health_bank.actions --root . append .local/incoming/<batch>.json`. Do not edit the generated ledger or replace earlier events. Keep proposed actions, caregiver-reported bookings, documented completion and clinical outcomes distinct. See `docs/care-actions.md`.", "",
    ]
    _atomic_write(derived / "AGENT_START.md", "\n".join(start).encode())
    return {**counts, **ledger_counts, "fts_available": fts, "projection_files": 6}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build local patient evidence navigation views")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    arguments = parser.parse_args(argv)
    try:
        result = build(arguments.root)
    except (ValueError, OSError, KeyError, TypeError, sqlite3.Error):
        print("health-bank-project: projection failed; inspect local source integrity and filesystem permissions", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
