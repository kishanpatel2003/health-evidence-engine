"""Review the Git index for structural publication mistakes.

This is a publication guard, not a detector of medical meaning in prose/code.
It never prints staged names, blob contents, clinical values or encryption keys.
No network access or Git mutation is required.
"""

from __future__ import annotations

import argparse
import base64
from collections import Counter
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
from typing import Iterator

from . import vault


MAX_BLOB_BYTES = 95 * 1024 * 1024
MEDICAL_FORMATS = {".pdf", ".tif", ".tiff", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".dcm", ".dicom", ".zip", ".xml", ".jsonl", ".csv", ".tsv", ".xlsx", ".xls", ".docx", ".pptx", ".html", ".htm"}
KEY_FORMATS = {".key", ".pem", ".p12", ".pfx", ".der", ".jks", ".keystore"}
SOURCE_FORMATS = {".py", ".pyi", ".sh", ".bash", ".zsh", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".json", ".md", ".rst", ".toml", ".yaml", ".yml", ".ini", ".cfg", ".svg"}
FORBIDDEN_COMPONENTS = {".local", "secrets", ".secrets", "keys", ".keys", ".ssh", ".git", ".venv", "venv", "__pycache__", ".pytest_cache"}
ROOT_CONFIG = {"pyproject.toml", ".gitignore", ".gitattributes", ".editorconfig", ".python-version", "requirements.txt", "uv.lock", "ruff.toml", "pytest.ini", "mypy.ini", "makefile", "license", "license.txt", "license.md"}
OBJECT_PATH = re.compile(r"vault/objects/([0-9a-f]{64})\.aesgcm\Z")
OID = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")


class InspectionError(ValueError):
    """An intentionally content-free inspection failure."""


def _environment() -> dict[str, str]:
    return {**os.environ, "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}


def _git(root: Path, *arguments: str, data: bytes | None = None) -> bytes:
    result = subprocess.run(["git", "--no-optional-locks", "-c", "core.fsmonitor=false", *arguments], cwd=root,
                            input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=_environment(), check=False)
    if result.returncode:
        raise InspectionError("Git index inspection failed")
    return result.stdout


def _allowed(path: str) -> bool:
    relative = PurePosixPath(path)
    parts = relative.parts
    if not parts or relative.is_absolute() or "\\" in path or any(part in ("", ".", "..") for part in path.split("/")):
        return False
    lower = [part.lower() for part in parts]
    if any(part in FORBIDDEN_COMPONENTS or part == ".env" or part.startswith(".env.") for part in lower):
        return False
    suffix = relative.suffix.lower()
    if suffix in KEY_FORMATS:
        return False
    if relative.stem.lower() in {"key", "keys", "secret", "secrets", "credential", "credentials", "password", "passwords", "token", "tokens", "api_key", "private_key"}:
        return False
    if re.search(r"(?:^|[._-])(?:secrets?|credentials?|passwords?|private[_-]?key|api[_-]?key)(?:[._-]|$)", lower[-1]):
        return False
    if parts[0] == "vault":
        return path == "vault/manifest.json" or OBJECT_PATH.fullmatch(path) is not None
    if suffix in MEDICAL_FORMATS:
        return False
    if len(parts) == 1:
        return lower[0] in ROOT_CONFIG or suffix in (".md", ".rst")
    return parts[0] in ("src", "tests", "schemas", "docs", "scripts", ".github") and (
        suffix in SOURCE_FORMATS or lower[-1] == "requirements.txt"
    )


def _index(root: Path) -> dict[str, tuple[str, str, int]]:
    result = {}
    for entry in _git(root, "ls-files", "--stage", "-z").split(b"\0"):
        if not entry:
            continue
        try:
            metadata, name = entry.split(b"\t", 1)
            mode, oid, stage = metadata.decode("ascii").split()
            identifier = oid
            if not OID.fullmatch(identifier):
                raise ValueError("Invalid object id")
            result[name.decode("utf-8", errors="surrogateescape")] = (mode, identifier, int(stage))
        except (ValueError, UnicodeError) as error:
            raise InspectionError("Git index metadata is invalid") from error
    return result


def _metadata(root: Path, identifiers: set[str]) -> dict[str, int]:
    if not identifiers:
        return {}
    response = _git(root, "cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)",
                    data="".join(identifier + "\n" for identifier in sorted(identifiers)).encode("ascii"))
    sizes = {}
    for line in response.splitlines():
        try:
            identifier, kind, size = line.decode("ascii").split()
            if identifier not in identifiers or kind != "blob":
                raise ValueError("Not a staged blob")
            sizes[identifier] = int(size)
        except (ValueError, UnicodeError) as error:
            raise InspectionError("Staged object inspection failed") from error
    if sizes.keys() != identifiers:
        raise InspectionError("Staged object inventory is incomplete")
    return sizes


def _blobs(root: Path, sizes: dict[str, int]) -> Iterator[tuple[str, bytes]]:
    if not sizes:
        return
    process = subprocess.Popen(["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "cat-file", "--batch"],
                               cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               env=_environment())
    try:
        for identifier in sorted(sizes):
            process.stdin.write((identifier + "\n").encode("ascii"))
            process.stdin.flush()
            header = process.stdout.readline()
            expected = f"{identifier} blob {sizes[identifier]}\n".encode("ascii")
            if header != expected:
                raise InspectionError("Staged blob stream is invalid")
            content = process.stdout.read(sizes[identifier])
            if len(content) != sizes[identifier] or process.stdout.read(1) != b"\n":
                raise InspectionError("Staged blob stream is incomplete")
            yield identifier, content
        process.stdin.close()
        if process.wait(timeout=30):
            raise InspectionError("Staged blob inspection failed")
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream and not stream.closed:
                stream.close()


def check_publication(root: str | Path, key_path: str | Path | None = None) -> dict:
    root = Path(root).expanduser().resolve()
    repository_root = Path(os.fsdecode(_git(root, "rev-parse", "--show-toplevel").strip())).resolve()
    if root != repository_root:
        raise InspectionError("Publication root must be the Git repository root")
    changed = [os.fsdecode(path) for path in _git(root, "diff", "--cached", "--name-only", "--no-renames", "--no-ext-diff", "--no-textconv", "-z").split(b"\0") if path]
    index = _index(root)
    violations = Counter()
    for path in changed:
        if not _allowed(path):
            violations["disallowed_paths"] += 1
        if path in index and (index[path][0] not in ("100644", "100755") or index[path][2] != 0):
            violations["nonregular_or_unmerged_entries"] += 1
    if any(entry[2] != 0 for entry in index.values()):
        violations["unmerged_index"] += 1
    staged_ids = {index[path][1] for path in changed if path in index and index[path][0] in ("100644", "100755")}
    key = None
    manifest = None
    manifest_content = None
    try:
        manifest = vault._read_manifest(root)
        key, loaded_key_path = vault._read_key(manifest, key_path)
        if root == loaded_key_path or root in loaded_key_path.parents:
            raise ValueError("Key must be outside repository")
        manifest_content = (root / "vault/manifest.json").read_bytes()
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        violations["external_key_or_manifest_unavailable"] += 1
    vault_changed = any(path.startswith("vault/") for path in changed)
    vault_validated = False
    current_objects = set()
    requested_ids = set(staged_ids)
    if vault_changed:
        try:
            vault.verify_vault(root, key_path)
            if manifest_content != (root / "vault/manifest.json").read_bytes():
                raise ValueError("Concurrent vault manifest change")
            vault_validated = True
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            violations["vault_validation_failures"] += 1
        if manifest is not None:
            current_objects = {f"vault/objects/{item['sha256']}.aesgcm" for item in manifest["objects"]}
            violations["manifest_objects_missing_from_index"] += sum(path not in index for path in current_objects)
            violations["manifest_objects_nonregular_in_index"] += sum(
                path in index and index[path][0] not in ("100644", "100755") for path in current_objects
            )
        if "vault/manifest.json" not in index:
            violations["vault_manifest_missing_from_index"] += 1
        else:
            requested_ids.add(index["vault/manifest.json"][1])
        violations["unreferenced_staged_vault_objects"] += sum(
            bool(OBJECT_PATH.fullmatch(path)) and path in index and path not in current_objects for path in changed
        )
    sizes = _metadata(root, requested_ids)
    oversized = {identifier for identifier, size in sizes.items() if size >= MAX_BLOB_BYTES}
    violations["oversized_blobs"] += len(oversized)
    scan_sizes = {identifier: size for identifier, size in sizes.items() if identifier not in oversized}
    paths_by_id: dict[str, list[str]] = {}
    for path in changed:
        if path in index:
            paths_by_id.setdefault(index[path][1], []).append(path)
    index_manifest_id = index.get("vault/manifest.json", (None, None, None))[1]
    scanned = 0
    for identifier, content in _blobs(root, scan_sizes):
        scanned += 1
        if key is not None:
            # Binary, hex (including mixed case), and ordinary base64 encodings
            # are checked. This does not claim to detect every possible leak.
            if key in content or key.hex().encode("ascii") in content.lower() or base64.b64encode(key) in content or base64.urlsafe_b64encode(key) in content:
                violations["key_matches"] += 1
        if vault_changed and identifier == index_manifest_id and content != manifest_content:
            violations["staged_manifest_differs_from_validated_copy"] += 1
        for path in paths_by_id.get(identifier, []):
            if not OBJECT_PATH.fullmatch(path):
                continue
            try:
                working = root / path
                if working.is_symlink() or working.stat().st_size >= MAX_BLOB_BYTES or working.read_bytes() != content:
                    raise ValueError("Different encrypted object bytes")
            except (OSError, ValueError):
                violations["staged_vault_objects_differ_from_working_copy"] += 1
    violations = {category: count for category, count in sorted(violations.items()) if count}
    return {"allowed": not violations, "staged_paths": len(changed), "staged_blobs": len(staged_ids),
            "scanned_blobs": scanned, "vault_validated": vault_validated, "violations": violations}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Check staged publication paths, keys and authenticated vault content")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--key-file", type=Path, help="Matching external key when the default key location is unavailable")
    arguments = parser.parse_args(argv)
    try:
        result = check_publication(arguments.root, arguments.key_file)
    except Exception:
        # Fail closed without echoing parser/Git exceptions that might contain
        # patient-specific paths or source material supplied to a malformed file.
        result = {"allowed": False, "violations": {"inspection_failure": 1}}
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["allowed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
