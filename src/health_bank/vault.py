"""Authenticated, content-addressed encryption for a Git-trackable vault.

Only opaque content hashes, object types and format versions remain visible in
the manifest. Filenames, clinical content and source URLs are encrypted. AES-GCM
uses a fresh random nonce for every new object. Unchanged objects are reused.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from pathlib import Path
import secrets
import shutil
import tempfile
from typing import Any
import zlib

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .store import Store, SHA_RE, LANE_RE, _private_dir, canonical_bytes, content_hash, safe_relative_path


VERSION = 1
MAGIC = b"HBV1"
PACK_MAGIC = b"HBZ1"
MAX_OBJECT_BYTES = 95 * 1024 * 1024
MAX_UNPACKED_BYTES = 2 * 1024 * 1024 * 1024
ENCRYPTION_OVERHEAD = len(MAGIC) + 12 + 16


def _check_object_size(size: int) -> None:
    if size >= MAX_OBJECT_BYTES:
        raise ValueError(
            "Encrypted vault object would reach the 95 MiB safety limit; "
            "split the source or implement versioned chunked vault objects before sealing"
        )


def _manifest_path(root: Path) -> Path:
    return root / "vault" / "manifest.json"


def _read_manifest(root: Path) -> dict[str, Any]:
    path = _manifest_path(root)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("Refusing symbolic links in vault metadata")
    manifest = json.loads(path.read_bytes())
    if manifest.get("version") != VERSION or not all(
        isinstance(manifest.get(key), str) and SHA_RE.fullmatch(manifest[key])
        for key in ("repository_id", "key_fingerprint")
    ) or not isinstance(manifest.get("objects"), list):
        raise ValueError("Unsupported or invalid vault manifest")
    seen = set()
    for item in manifest["objects"]:
        if set(item) != {"version", "type", "sha256"} or item["version"] != VERSION or item["type"] not in ("artifact", "snapshot"):
            raise ValueError("Invalid vault object metadata")
        if not isinstance(item["sha256"], str) or not SHA_RE.fullmatch(item["sha256"]) or item["sha256"] in seen:
            raise ValueError("Invalid or duplicate vault object hash")
        seen.add(item["sha256"])
    return manifest


def default_key_path(repository_id: str) -> Path:
    return Path.home() / ".config" / "health-bank" / "keys" / f"{repository_id}.key"


def _read_key(manifest: dict[str, Any], key_path: str | Path | None) -> tuple[bytes, Path]:
    path = Path(key_path).expanduser().resolve() if key_path else default_key_path(manifest["repository_id"])
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"Encryption key unavailable; provide the original key file at {path}")
    key = path.read_bytes()
    if len(key) != 32 or content_hash(key) != manifest["key_fingerprint"]:
        raise ValueError("Encryption key does not match this vault")
    return key, path


def _write_manifest(root: Path, manifest: dict[str, Any]) -> None:
    path = _manifest_path(root)
    _private_dir(path.parent)
    content = canonical_bytes(manifest) + b"\n"
    if path.exists() and path.read_bytes() == content:
        return
    descriptor, temporary = tempfile.mkstemp(prefix=".manifest-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def initialize(root: str | Path, key_path: str | Path | None = None) -> dict[str, Any]:
    store = Store(root)
    with store.locked():
        return _initialize_locked(store.root, key_path)


def _initialize_locked(root: Path, key_path: str | Path | None) -> dict[str, Any]:
    if key_path:
        explicit_path = Path(key_path).expanduser().resolve()
        if explicit_path == root or root in explicit_path.parents:
            raise ValueError("Encryption key must be outside the repository")
    if _manifest_path(root).exists():
        manifest = _read_manifest(root)
        _, path = _read_key(manifest, key_path)
        return {"version": VERSION, "key_file": str(path), "repository_id": manifest["repository_id"]}
    repository_id = content_hash(secrets.token_bytes(32))
    if key_path:
        path = Path(key_path).expanduser().resolve()
        if path == root or root in path.parents:
            raise ValueError("Encryption key must be outside the repository")
        if not path.is_file():
            raise ValueError("Explicit encryption key file does not exist")
        key = path.read_bytes()
        if len(key) != 32:
            raise ValueError("Encryption key must contain exactly 32 random bytes")
    else:
        path = default_key_path(repository_id)
        if path == root or root in path.parents:
            raise ValueError("Default encryption key location is inside the repository; provide an external --key-file")
        _private_dir(path.parent)
        key = secrets.token_bytes(32)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(key)
            stream.flush()
            os.fsync(stream.fileno())
    manifest = {"version": VERSION, "repository_id": repository_id, "key_fingerprint": content_hash(key), "objects": []}
    _write_manifest(root, manifest)
    return {"version": VERSION, "key_file": str(path), "repository_id": repository_id}


def _aad(manifest: dict[str, Any], item: dict[str, Any]) -> bytes:
    return canonical_bytes({"repository_id": manifest["repository_id"], **item})


def _object_path(root: Path, sha: str) -> Path:
    return root / "vault" / "objects" / f"{sha}.aesgcm"


def _decrypt_object(root: Path, manifest: dict[str, Any], item: dict[str, Any], key: bytes) -> bytes:
    path = _object_path(root, item["sha256"])
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("Refusing symbolic links in encrypted objects")
    _check_object_size(path.stat().st_size)
    content = path.read_bytes()
    if not content.startswith(MAGIC) or len(content) < len(MAGIC) + 12 + 16:
        raise ValueError("Invalid encrypted object header")
    try:
        plaintext = AESGCM(key).decrypt(content[4:16], content[16:], _aad(manifest, item))
    except InvalidTag as error:
        raise ValueError("Encrypted object failed authentication") from error
    if content_hash(plaintext) != item["sha256"]:
        raise ValueError("Decrypted object failed content-hash verification")
    return plaintext


def _file_entry(path: Path, relative: str) -> dict[str, Any]:
    if path.is_symlink():
        raise ValueError("Refusing a symbolic-link plaintext file")
    content = path.read_bytes()
    return {"path": relative, "sha256": content_hash(content), "data": base64.b64encode(content).decode("ascii")}


def _pack(files: list[dict[str, Any]]) -> bytes:
    # Stream canonical JSON into compression to avoid holding an additional
    # full uncompressed snapshot while canonical files are already in memory.
    encoder = json.JSONEncoder(sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    compressor = zlib.compressobj(level=6)
    packed = bytearray(PACK_MAGIC)
    unpacked_size = 0
    for chunk in encoder.iterencode({"version": VERSION, "files": files}):
        encoded = chunk.encode("utf-8")
        unpacked_size += len(encoded)
        if unpacked_size > MAX_UNPACKED_BYTES:
            raise ValueError("Vault snapshot exceeds the 2 GiB decompressed payload limit; implement chunking")
        packed.extend(compressor.compress(encoded))
        _check_object_size(len(packed) + ENCRYPTION_OVERHEAD)
    packed.extend(compressor.flush())
    _check_object_size(len(packed) + ENCRYPTION_OVERHEAD)
    return bytes(packed)


def seal(root: str | Path, key_path: str | Path | None = None) -> dict[str, Any]:
    store = Store(root)
    store.verify()
    with store.locked():
        info = _initialize_locked(store.root, key_path)
        manifest = _read_manifest(store.root)
        key, _ = _read_key(manifest, key_path)
        objects: list[dict[str, Any]] = []
        snapshot_files = []
        catalog = store.local / "catalog.jsonl"
        if catalog.exists():
            snapshot_files.append(_file_entry(catalog, "catalog.jsonl"))
        canonical = store.local / "canonical"
        if canonical.is_symlink():
            raise ValueError("Refusing a symbolic-link canonical directory")
        for path in sorted(canonical.glob("*.jsonl")):
            snapshot_files.append(_file_entry(path, "canonical/" + path.name))
        payloads: list[tuple[str, bytes]] = [("snapshot", _pack(snapshot_files))]
        raw = store.local / "raw"
        if raw.is_symlink():
            raise ValueError("Refusing a symbolic-link raw directory")
        for path in sorted(raw.glob("*/*")):
            if path.parent.is_symlink() or not path.is_file():
                raise ValueError("Unexpected raw artifact path")
            entry = _file_entry(path, path.relative_to(store.local).as_posix())
            if not SHA_RE.fullmatch(path.parent.name) or entry["sha256"] != path.parent.name:
                raise ValueError("Raw artifact failed content hash before encryption")
            payloads.append(("artifact", _pack([entry])))
        created = 0
        _private_dir(store.root / "vault" / "objects")
        for object_type, plaintext in payloads:
            _check_object_size(len(plaintext) + ENCRYPTION_OVERHEAD)
            item = {"version": VERSION, "type": object_type, "sha256": content_hash(plaintext)}
            path = _object_path(store.root, item["sha256"])
            if path.exists():
                _decrypt_object(store.root, manifest, item, key)
            else:
                nonce = secrets.token_bytes(12)
                content = MAGIC + nonce + AESGCM(key).encrypt(nonce, plaintext, _aad(manifest, item))
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                created += 1
            objects.append(item)
        manifest["objects"] = sorted(objects, key=lambda item: (item["type"], item["sha256"]))
        _write_manifest(store.root, manifest)
        return {"sealed": True, "objects": len(objects), "new_objects": created, "key_file": info["key_file"]}


def _unpack(plaintext: bytes, object_type: str) -> list[tuple[str, bytes]]:
    if plaintext.startswith(PACK_MAGIC):
        decompressor = zlib.decompressobj()
        try:
            decoded = decompressor.decompress(plaintext[len(PACK_MAGIC):], MAX_UNPACKED_BYTES + 1)
        except zlib.error as error:
            raise ValueError("Invalid compressed vault payload") from error
        if len(decoded) > MAX_UNPACKED_BYTES or decompressor.unconsumed_tail:
            raise ValueError("Vault payload exceeds the bounded decompression limit")
        if not decompressor.eof:
            raise ValueError("Truncated compressed vault payload")
        if decompressor.unused_data:
            raise ValueError("Trailing data in compressed vault payload")
        plaintext = decoded
    elif len(plaintext) > MAX_UNPACKED_BYTES:
        raise ValueError("Legacy vault payload exceeds the bounded size limit")
    # Legacy objects were uncompressed canonical JSON. Keep them readable so a
    # format upgrade never makes previously preserved evidence inaccessible.
    payload = json.loads(plaintext)
    if not isinstance(payload, dict) or payload.get("version") != VERSION or not isinstance(payload.get("files"), list):
        raise ValueError("Invalid decrypted object format")
    if object_type == "artifact" and len(payload["files"]) != 1:
        raise ValueError("Artifact object must contain one file")
    result = []
    for entry in payload["files"]:
        relative = safe_relative_path(entry["path"])
        parts = relative.parts
        if object_type == "artifact":
            if len(parts) != 3 or parts[0] != "raw" or not SHA_RE.fullmatch(parts[1]):
                raise ValueError("Unexpected decrypted artifact path")
        else:
            if parts != ("catalog.jsonl",) and not (
                len(parts) == 2 and parts[0] == "canonical" and parts[1].endswith(".jsonl")
                and LANE_RE.fullmatch(parts[1][:-6])
            ):
                raise ValueError("Unexpected decrypted snapshot path")
        try:
            content = base64.b64decode(entry["data"], validate=True)
        except (ValueError, binascii.Error) as error:
            raise ValueError("Invalid decrypted file encoding") from error
        if content_hash(content) != entry["sha256"] or (object_type == "artifact" and parts[1] != entry["sha256"]):
            raise ValueError("Decrypted file failed content hash")
        result.append((relative.as_posix(), content))
    return result


def verify_vault(root: str | Path, key_path: str | Path | None = None) -> dict[str, Any]:
    root = Path(root).expanduser().resolve()
    if key_path and root in Path(key_path).expanduser().resolve().parents:
        raise ValueError("Encryption key must be outside the repository")
    manifest = _read_manifest(root)
    key, _ = _read_key(manifest, key_path)
    paths = set()
    snapshot_count = 0
    for item in manifest["objects"]:
        snapshot_count += item["type"] == "snapshot"
        for path, _ in _unpack(_decrypt_object(root, manifest, item, key), item["type"]):
            if path in paths:
                raise ValueError("Duplicate file path in vault")
            paths.add(path)
    if manifest["objects"] and snapshot_count != 1:
        raise ValueError("Vault must have exactly one current snapshot")
    return {"verified": True, "objects": len(manifest["objects"]), "files": len(paths)}


def unseal(root: str | Path, key_path: str | Path | None = None,
           destination: str | Path | None = None) -> dict[str, Any]:
    root = Path(root).expanduser().resolve()
    if key_path and root in Path(key_path).expanduser().resolve().parents:
        raise ValueError("Encryption key must be outside the repository")
    manifest = _read_manifest(root)
    key, _ = _read_key(manifest, key_path)
    if not manifest["objects"]:
        raise ValueError("Vault has no sealed snapshot")
    if destination is not None:
        target_root = Path(destination).expanduser().resolve()
        if target_root.exists():
            raise ValueError("Explicit restore destination must not already exist")
        target_root.mkdir(parents=True, mode=0o700)
    else:
        target_root = root
    store = Store(target_root)
    with store.locked():
        if any((store.local / name).exists() for name in ("raw", "canonical", "catalog.jsonl")):
            raise ValueError("Restore requires an empty local store; use a new --destination")
        stage = Path(tempfile.mkdtemp(prefix=".restore-", dir=store.local))
        paths = set()
        snapshot_count = 0
        try:
            staged_root = stage / "store"
            staged_store = Store(staged_root)
            for item in manifest["objects"]:
                snapshot_count += item["type"] == "snapshot"
                for relative, content in _unpack(_decrypt_object(root, manifest, item, key), item["type"]):
                    if relative in paths:
                        raise ValueError("Duplicate file path in vault")
                    paths.add(relative)
                    target = staged_store.local.joinpath(*safe_relative_path(relative).parts)
                    _private_dir(target.parent)
                    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(content)
            if snapshot_count != 1:
                raise ValueError("Vault must have exactly one current snapshot")
            verification = staged_store.verify()
            for name in ("raw", "canonical", "catalog.jsonl"):
                source = staged_store.local / name
                if source.exists():
                    os.replace(source, store.local / name)
        finally:
            shutil.rmtree(stage)
    return {"restored": True, "destination": str(target_root), "files": len(paths), **verification}
