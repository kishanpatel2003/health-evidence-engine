"""Commands print counts and paths, never clinical records or key bytes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .store import Store
from . import vault


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="health-bank")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Repository root (default: current directory)")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Initialize local storage and generate an external encryption key")
    init.add_argument("--key-file", type=Path, help="Use an existing external 32-byte key")
    ingest = commands.add_parser("ingest-file", help="Preserve one original artifact locally; does not interpret it")
    ingest.add_argument("path", type=Path)
    ingest.add_argument("--source-system", required=True)
    ingest.add_argument("--source-url")
    ingest.add_argument("--kind")
    for name, help_text in (("seal", "Encrypt the local store into vault/"), ("unseal", "Verify and restore into an empty local store")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--key-file", type=Path, help="Original external encryption key; required on another machine")
        if name == "unseal":
            command.add_argument("--destination", type=Path, help="New isolated repository root for restoration (must not exist)")
    verify = commands.add_parser("verify", help="Verify local record identities, hashes and evidence")
    verify.add_argument("--vault", action="store_true", help="Verify encrypted vault instead of local storage")
    verify.add_argument("--key-file", type=Path)
    commands.add_parser("stats", help="Show aggregate local counts without clinical content")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "init":
            result = vault.initialize(arguments.root, arguments.key_file)
        elif arguments.command == "ingest-file":
            receipt = Store(arguments.root).add_artifact(arguments.path, arguments.source_system, arguments.source_url, arguments.kind)
            result = {"artifact_sha256": receipt["artifact_sha256"], "size_bytes": receipt["size_bytes"], "captured_at": receipt["captured_at"]}
        elif arguments.command == "seal":
            result = vault.seal(arguments.root, arguments.key_file)
        elif arguments.command == "unseal":
            result = vault.unseal(arguments.root, arguments.key_file, arguments.destination)
        elif arguments.command == "verify":
            result = vault.verify_vault(arguments.root, arguments.key_file) if arguments.vault else Store(arguments.root).verify()
        else:
            result = Store(arguments.root).stats()
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(f"health-bank: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
