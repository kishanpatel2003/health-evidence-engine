# Local operations

Install with Python 3.11+ using `pip install -e .`. The package name is
`health-evidence-engine`; the import namespace and CLI remain `health_bank` and
`health-bank`.

Run the included example with `python examples/synthetic_demo.py`. It allocates a
temporary store and prints aggregate counts only. It never reads a patient
record, connects to a portal, contacts a service, or creates an external key.

For a separate local store, `health-bank --root /private/path init` creates local
storage and an external encryption key. Do not put real data under this public
code checkout. Source evidence can be preserved with `ingest-file`; structured
CDA and saved-page importers are separate modules. `verify` checks hashes and
assertion/evidence references; `stats` reports aggregates. Rebuild navigation
views with `python -m health_bank.project --root /private/path`.

Source documents and imported assertions are not clinical truth. Capture time
is not event time. Repeated observations of an unchanged assertion add evidence,
while changed assertions are retained as revisions. Coverage records describe
what was inspected, captured, extracted, and left unresolved. The generated
snapshot and SQLite index are replaceable navigation views, not the canonical
store.

The optional `seal`/`unseal` commands encrypt and restore a separately held local
store. Keys must be outside the repository; preserve a recoverable backup
separately. Do not commit patient vault objects or manifests to this public
repository, even when encrypted. The `publish` module is an additional staged
payload guard for private deployments; it cannot recognize every personal fact
or replace a content review.

The engine does not log into portals, decide benefits, send messages, schedule
care, reconcile all clinical events, or provide medical recommendations.
