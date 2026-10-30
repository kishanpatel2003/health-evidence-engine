# Health Evidence Engine

A local store that preserves health-record evidence and makes it navigable by agents. Keep immutable source artifacts, append source assertions, retain conflicting revisions, and rebuild a compact snapshot with a searchable SQLite index.

## Design

- SHA-256-addressed source artifacts
- Deterministic assertion identity and append-only revisions
- Additional evidence links without duplicate assertions
- Coverage observations and unresolved capture/extraction gaps
- Source-preserving CDA and saved-page importers
- Rebuildable snapshots, lane indexes, and SQLite search
- Sourced care-coordination events, separate from clinical assertions
- Authenticated encryption and isolated restore for backups

The engine separates what a source documented from later interpretations. Agents can trace an assertion to its source, inspect revisions, and see which surfaces remain uncaptured.

## Quick start

Python 3.11+ on macOS or Linux (`fcntl` provides local locking):

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
python examples/synthetic_demo.py
python -m unittest discover -s tests -v
```

The example creates a temporary store with generated records, adds a corrected revision and a coverage gap, verifies evidence links, and builds navigation views.

## Code map

- `src/health_bank/store.py`: artifacts, record identity, evidence, locking, integrity checks, and local file permissions
- `cda.py`, `portal.py`, `collection.py`: import foundations
- `project.py`: patient-separated navigation views and search index
- `actions.py`: sourced action events and outcome corrections
- `vault.py`: encrypted backup and restore
- `publish.py`: staged-content checks for private deployment workflows
- `schemas/`: record, evidence, lane, and action formats
- `docs/architecture.md`: identity, time, revision, coverage, and grouping rules
- `CONTEXT.md`: domain vocabulary

## Storage

Keep real records and backups in a separate local store. `.local/`, vault objects, manifests, keys, and generated views stay outside Git. Encryption keys are stored separately from backups.

Hashes verify preserved bytes. Coverage records describe extraction status; a successful import does not establish complete coverage or resolve conflicting assertions.

[MIT](LICENSE)
