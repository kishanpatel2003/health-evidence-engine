# Local operations

Install with Python 3.11+ using `pip install -e .`. The package name is
`health-evidence-engine`; the import namespace and CLI remain `health_bank` and
`health-bank`.

Run `python examples/synthetic_demo.py` to create a temporary store with example records and print its counts.

For a separate local store, `health-bank --root /private/path init` creates local
storage and an external encryption key. Keep records outside the code checkout. Source evidence can be preserved with `ingest-file`; structured
CDA and saved-page importers are separate modules. `verify` checks hashes and
assertion/evidence references; `stats` reports aggregates. Rebuild navigation
views with `python -m health_bank.project --root /private/path`.

Capture time and event time are separate. Repeated observations of an unchanged assertion add evidence,
while changed assertions are retained as revisions. Coverage records describe
what was inspected, captured, extracted, and left unresolved. The generated
snapshot and SQLite index are replaceable navigation views, not the canonical
store.

The optional `seal`/`unseal` commands encrypt and restore a separately held local
store. Store keys separately and keep a recoverable backup. Vault objects and manifests stay outside Git. The `publish` module checks staged payloads before a private deployment.
