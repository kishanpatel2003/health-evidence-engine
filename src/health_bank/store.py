"""Append-only local storage. Plaintext belongs exclusively in ``.local``.

Record identity describes an assertion, not an individual document. The evidence
stream preserves each document/locator that supports that assertion. Changed
values or status create new records; conflicting assertions remain distinct.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
from typing import Any, Iterator


SCHEMA_VERSION = 1
LANE_RE = re.compile(r"[a-z][a-z0-9_-]*\Z")
SHA_RE = re.compile(r"[0-9a-f]{64}\Z")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _validate_json(value: Any) -> None:
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("JSON object keys must be strings")
        for item in value.values():
            _validate_json(item)
    elif isinstance(value, (list, tuple)):
        if isinstance(value, tuple):
            raise ValueError("JSON arrays must be lists")
        for item in value:
            _validate_json(item)
    elif value is not None and not isinstance(value, (str, bool, int, float)):
        raise ValueError(f"Unsupported JSON value type: {type(value).__name__}")


def canonical_bytes(value: Any) -> bytes:
    _validate_json(value)
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def content_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _private_dir(path: Path) -> None:
    if path.is_symlink():
        raise ValueError("Refusing a symbolic-link storage directory")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def safe_relative_path(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("Invalid relative storage path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in (".", "..", "") for part in value.split("/")):
        raise ValueError("Unsafe relative storage path")
    return path


def _read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    if not path.exists():
        return
    if path.is_symlink():
        raise ValueError("Refusing a symbolic-link storage file")
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                raise ValueError(f"Blank line in {path.name}:{line_number}")
            try:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("Expected a JSON object")
                canonical_bytes(value)
            except (ValueError, TypeError) as error:
                raise ValueError(f"Invalid JSON in {path.name}:{line_number}") from error
            yield value


def _append_jsonl(path: Path, value: dict[str, Any]) -> None:
    _private_dir(path.parent)
    if path.is_symlink():
        raise ValueError("Refusing a symbolic-link storage file")
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "ab") as stream:
        os.chmod(path, 0o600)
        stream.write(canonical_bytes(value) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def record_identity(record: dict[str, Any]) -> dict[str, Any]:
    source = record["source"]
    return {
        "patient_id": record["patient_id"],
        "lane": record["lane"],
        "kind": record["kind"],
        "source": {"system": source["system"], "record_key": source.get("record_key")},
        "effective_time": record.get("effective_time"),
        "status": record.get("status"),
        "data": record["data"],
    }


def record_id(record: dict[str, Any]) -> str:
    return "rec_" + content_hash(canonical_bytes(record_identity(record)))


def validate_envelope(record: dict[str, Any]) -> None:
    """Validate the persisted envelope independently of its identity hash."""
    required = {"schema_version", "record_id", "patient_id", "lane", "kind", "source", "effective_time", "status", "data", "captured_at"}
    if not required.issubset(record) or type(record["schema_version"]) is not int or record["schema_version"] != SCHEMA_VERSION:
        raise ValueError("Incomplete or unsupported record envelope")
    for field in ("patient_id", "lane", "kind"):
        if not isinstance(record[field], str) or not record[field].strip():
            raise ValueError(f"Invalid persisted {field}")
    if not LANE_RE.fullmatch(record["lane"]) or record["lane"] == "evidence":
        raise ValueError("Invalid persisted lane")
    source = record["source"]
    if not isinstance(source, dict) or not {"system", "artifact_sha256", "locator", "record_key"}.issubset(source):
        raise ValueError("Incomplete persisted provenance")
    if not isinstance(source["system"], str) or not source["system"].strip():
        raise ValueError("Invalid persisted source system")
    if source["artifact_sha256"] is not None and not (isinstance(source["artifact_sha256"], str) and SHA_RE.fullmatch(source["artifact_sha256"])):
        raise ValueError("Invalid persisted artifact hash")
    if source["locator"] is not None and not isinstance(source["locator"], (str, dict)):
        raise ValueError("Invalid persisted source locator")
    if source["record_key"] is not None and not isinstance(source["record_key"], str):
        raise ValueError("Invalid persisted source record key")
    time = record["effective_time"]
    if time is not None:
        if not isinstance(time, dict):
            raise ValueError("Invalid persisted effective_time")
        if any(time.get(field) is not None and not isinstance(time[field], str) for field in ("start", "end", "precision")):
            raise ValueError("Invalid persisted clinical time field")
        if time.get("original") is not None and not isinstance(time["original"], (str, dict)):
            raise ValueError("Invalid persisted original clinical time")
    if not isinstance(record["status"], str) or not isinstance(record["captured_at"], str):
        raise ValueError("Invalid persisted status or capture time")
    try:
        capture = datetime.fromisoformat(record["captured_at"].replace("Z", "+00:00"))
        if capture.tzinfo is None:
            raise ValueError("Timezone required")
    except ValueError as error:
        raise ValueError("Invalid persisted capture timestamp") from error
    if record["record_id"] != record_id(record):
        raise ValueError("Record identity/schema verification failed")


class Store:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.local = self.root / ".local"
        _private_dir(self.local)
        self._index_cache: dict[Path, tuple[tuple[int, int], set[bytes | str]]] = {}

    def _index(self, path: Path, evidence: bool = False) -> set[bytes | str]:
        signature = (path.stat().st_mtime_ns, path.stat().st_size) if path.exists() else (0, 0)
        cached = self._index_cache.get(path)
        if cached is not None and cached[0] == signature:
            return cached[1]
        values = {
            canonical_bytes({"record_id": item["record_id"], "source": item["source"]}) if evidence else item["record_id"]
            for item in _read_jsonl(path)
        }
        self._index_cache[path] = (signature, values)
        return values

    def _remember(self, path: Path, values: set[bytes | str]) -> None:
        self._index_cache[path] = ((path.stat().st_mtime_ns, path.stat().st_size), values)

    @contextlib.contextmanager
    def locked(self):
        lock_path = self.local / ".write.lock"
        if lock_path.is_symlink():
            raise ValueError("Refusing a symbolic-link lock file")
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def add_artifact(self, path: str | Path, source_system: str, source_url: str | None = None,
                     kind: str | None = None) -> dict[str, Any]:
        if not isinstance(source_system, str) or not source_system.strip():
            raise ValueError("source_system is required")
        if any(value is not None and not isinstance(value, str) for value in (source_url, kind)):
            raise ValueError("source_url and kind must be strings or null")
        original = Path(path).expanduser()
        if not original.is_file():
            raise ValueError("Artifact must be a regular file")
        content = original.read_bytes()
        sha = content_hash(content)
        name = original.name
        if name in ("", ".", "..") or "\\" in name:
            raise ValueError("Invalid artifact basename")
        relative = PurePosixPath("raw", sha, name).as_posix()
        receipt = {
            "schema_version": SCHEMA_VERSION,
            "artifact_sha256": sha,
            "local_path": ".local/" + relative,
            "original_name": name,
            "size_bytes": len(content),
            "source_system": source_system,
            "source_url": source_url,
            "kind": kind,
            "captured_at": utc_now(),
        }
        canonical_bytes(receipt)
        with self.locked():
            target = self.local.joinpath(*PurePosixPath(relative).parts)
            _private_dir(self.local / "raw")
            _private_dir(target.parent)
            if target.is_symlink():
                raise ValueError("Refusing a symbolic-link artifact")
            if target.exists():
                if content_hash(target.read_bytes()) != sha:
                    raise ValueError("Stored artifact failed its content hash")
            else:
                descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
            _append_jsonl(self.local / "catalog.jsonl", receipt)
        return receipt

    def add_record(self, record: dict[str, Any]) -> str:
        if not isinstance(record, dict):
            raise ValueError("Record must be a JSON object")
        canonical_bytes(record)
        record = json.loads(canonical_bytes(record))
        for field in ("patient_id", "lane", "kind"):
            if not isinstance(record.get(field), str) or not record[field].strip():
                raise ValueError(f"Record requires {field}")
        if not LANE_RE.fullmatch(record["lane"]) or record["lane"] == "evidence":
            raise ValueError("Lane must be a safe identifier other than evidence")
        if "data" not in record:
            raise ValueError("Record requires data (use null for an explicitly unknown value)")
        source = record.get("source")
        if not isinstance(source, dict) or not isinstance(source.get("system"), str) or not source["system"].strip():
            raise ValueError("Record requires source.system")
        for key in ("artifact_sha256", "locator", "record_key"):
            source.setdefault(key, None)
        if source["record_key"] is not None and not isinstance(source["record_key"], str):
            raise ValueError("source.record_key must be a string or null")
        if source["locator"] is not None and not isinstance(source["locator"], (str, dict)):
            raise ValueError("source.locator must be a string, object or null")
        if source["artifact_sha256"] is not None and not (
            isinstance(source["artifact_sha256"], str) and SHA_RE.fullmatch(source["artifact_sha256"])
        ):
            raise ValueError("source.artifact_sha256 must be a SHA-256 hex digest or null")
        if type(record.get("schema_version", SCHEMA_VERSION)) is not int or record.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
            raise ValueError("Unsupported record schema_version")
        record["schema_version"] = SCHEMA_VERSION
        record.setdefault("effective_time", None)
        effective_time = record["effective_time"]
        if effective_time is not None:
            if not isinstance(effective_time, dict):
                raise ValueError("effective_time must be an object or null")
            for key in ("start", "end", "precision"):
                if effective_time.get(key) is not None and not isinstance(effective_time[key], str):
                    raise ValueError(f"effective_time.{key} must be a string or null")
            if effective_time.get("original") is not None and not isinstance(effective_time["original"], (str, dict)):
                raise ValueError("effective_time.original must be a string, object or null")
        if record.get("status") is None:
            record["status"] = "unknown"
        if not isinstance(record["status"], str):
            raise ValueError("status must be a string")
        record.setdefault("captured_at", utc_now())
        if not isinstance(record["captured_at"], str):
            raise ValueError("captured_at must be an ISO timestamp with a timezone")
        try:
            timestamp = datetime.fromisoformat(record["captured_at"].replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                raise ValueError("Timezone required")
        except ValueError as error:
            raise ValueError("captured_at must be an ISO timestamp with a timezone") from error
        identifier = record_id(record)
        if record.get("record_id", identifier) != identifier:
            raise ValueError("Provided record_id does not match record content")
        record["record_id"] = identifier
        validate_envelope(record)
        evidence = {
            "schema_version": SCHEMA_VERSION,
            "record_id": identifier,
            "source": source,
            "captured_at": record["captured_at"],
        }
        with self.locked():
            path = self.local / "canonical" / f"{record['lane']}.jsonl"
            existing = self._index(path)
            if identifier not in existing:
                _append_jsonl(path, record)
                existing.add(identifier)
                self._remember(path, existing)
            evidence_path = self.local / "canonical" / "evidence.jsonl"
            evidence_key = canonical_bytes({"record_id": identifier, "source": source})
            existing_evidence = self._index(evidence_path, evidence=True)
            if evidence_key not in existing_evidence:
                _append_jsonl(evidence_path, evidence)
                existing_evidence.add(evidence_key)
                self._remember(evidence_path, existing_evidence)
        return identifier

    def iter_records(self, lane: str | None = None) -> Iterator[dict[str, Any]]:
        if lane is not None:
            if not LANE_RE.fullmatch(lane) or lane == "evidence":
                raise ValueError("Invalid record lane")
            paths = [self.local / "canonical" / f"{lane}.jsonl"]
        else:
            paths = sorted((self.local / "canonical").glob("*.jsonl"))
        for path in paths:
            if path.name != "evidence.jsonl":
                for item in _read_jsonl(path):
                    if item.get("lane") != path.stem:
                        raise ValueError("Record lane does not match its storage path")
                    yield item

    def stats(self) -> dict[str, Any]:
        with self.locked():
            catalog = list(_read_jsonl(self.local / "catalog.jsonl"))
            lanes: dict[str, int] = {}
            for record in self.iter_records():
                lanes[record["lane"]] = lanes.get(record["lane"], 0) + 1
            return {
                "artifacts": len({item["artifact_sha256"] for item in catalog}),
                "capture_receipts": len(catalog),
                "records": sum(lanes.values()),
                "evidence_assertions": len(list(_read_jsonl(self.local / "canonical" / "evidence.jsonl"))),
                "lanes": lanes,
            }

    def verify(self) -> dict[str, Any]:
        with self.locked():
            receipts = list(_read_jsonl(self.local / "catalog.jsonl"))
            artifact_hashes = set()
            for receipt in receipts:
                relative = safe_relative_path(receipt["local_path"])
                if len(relative.parts) != 4 or relative.parts[:2] != (".local", "raw"):
                    raise ValueError("Unexpected artifact catalog path")
                sha = receipt["artifact_sha256"]
                if not SHA_RE.fullmatch(sha) or relative.parts[2] != sha:
                    raise ValueError("Artifact catalog hash/path mismatch")
                target = self.root.joinpath(*relative.parts)
                if any(part.is_symlink() for part in [target, target.parent, target.parent.parent]):
                    raise ValueError("Symbolic link in artifact storage")
                if content_hash(target.read_bytes()) != sha or target.stat().st_size != receipt["size_bytes"]:
                    raise ValueError("Artifact hash/size verification failed")
                artifact_hashes.add(sha)
            records = list(self.iter_records())
            identifiers = set()
            for record in records:
                validate_envelope(record)
                source_sha = record["source"]["artifact_sha256"]
                if source_sha is not None and source_sha not in artifact_hashes:
                    raise ValueError("Canonical record refers to an unknown artifact")
                if record["record_id"] in identifiers:
                    raise ValueError("Duplicate canonical record")
                identifiers.add(record["record_id"])
            evidence_seen = set()
            supported = set()
            records_by_id = {item["record_id"]: item for item in records}
            for evidence in _read_jsonl(self.local / "canonical" / "evidence.jsonl"):
                if evidence.get("schema_version") != SCHEMA_VERSION or evidence.get("record_id") not in identifiers:
                    raise ValueError("Evidence refers to an unknown record or schema")
                sha = evidence["source"].get("artifact_sha256")
                if sha is not None and sha not in artifact_hashes:
                    raise ValueError("Evidence refers to an unknown artifact")
                evidence_record = records_by_id.get(evidence["record_id"])
                if evidence_record is not None:
                    validate_envelope({**evidence_record, "source": evidence["source"], "captured_at": evidence["captured_at"]})
                key = canonical_bytes({"record_id": evidence["record_id"], "source": evidence["source"]})
                if key in evidence_seen:
                    raise ValueError("Duplicate evidence assertion")
                evidence_seen.add(key)
                supported.add(evidence["record_id"])
            if identifiers - supported:
                raise ValueError("Canonical records missing evidence assertions")
            return {"verified": True, "artifacts": len(artifact_hashes), "records": len(identifiers), "evidence_assertions": len(evidence_seen)}
