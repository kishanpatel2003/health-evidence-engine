# Health Evidence Engine

An evidence-preserving local store for health records and agent navigation.
Keep immutable source artifacts, append source assertions, accumulate precise
citations across reexports, retain conflicting revisions, and rebuild a compact
snapshot plus searchable SQLite index. Interpretation remains separate from
what a source actually documented.

## Why it exists

An assistant needs more than a mutable summary. It needs to recover the source,
understand which time a statement refers to, see what changed, and know which
surfaces were not captured. The engine makes those distinctions explicit:

- SHA-256-addressed source artifacts
- Deterministic assertion identity with append-only revisions
- Additional evidence links without duplicate assertions
- Coverage observations and unresolved capture/extraction gaps
- Source-preserving CDA and saved-page importers
- Rebuildable snapshots, lane indexes, and SQLite search
- Sourced care-coordination events, separated from clinical assertions
- Optional authenticated encryption and isolated restore for private backups

## Run the synthetic demo

Python 3.11+ on macOS or Linux (`fcntl` is used for local locking):

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
python examples/synthetic_demo.py
python -m unittest discover -s tests -v
```

The demo creates two fictional artifacts in a temporary directory, reobserves an
unchanged assertion, adds a corrected revision and a coverage gap, verifies the
store, and builds navigation views. It prints counts only. All test fixtures are
synthetic and generated locally; no patient data, source exports, vaults, keys,
or original private history are included.

## Layout

- `src/health_bank/store.py`: artifact preservation, record identity, evidence,
  locking, integrity checks, and local file permissions
- `cda.py`, `portal.py`, `collection.py`: source-preserving import foundations
- `project.py`: deterministic patient-separated navigation views and index
- `actions.py`: append-only sourced action events and outcome corrections
- `vault.py`: encrypted backup/restore code
- `publish.py`: staged-content checks for private deployment workflows
- `schemas/`: versioned record, evidence, lane, and action formats
- `docs/architecture.md`: identity, time, revision, coverage, and grouping rules
- `CONTEXT.md`: domain vocabulary

## Privacy boundary and limits

This is a public **code** repository. Store any real data separately and keep
`.local/`, encrypted vault objects/manifests, encryption keys, and generated views
out of Git. Hashes establish which bytes were preserved, not clinical correctness.
A successful import does not establish complete extraction or record coverage.
Source assertions may conflict; a newer capture alone does not establish truth.

The package is a local prototype, not a clinical decision system, unattended
portal crawler, multi-user service, or assurance of regulatory compliance. It
does not diagnose, determine eligibility, change treatment, or take external care
actions. MIT applies to the code; bring only data you are authorized to process.
