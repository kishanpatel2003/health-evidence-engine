# Architecture

The bank preserves what was documented, when it applied, where it came from and when it was observed. It gives an AI agent a small orientation surface and direct routes to the supporting evidence. It does not reduce all evidence to a single mutable profile.

## Layers

| Layer | Purpose | Mutation rule |
| --- | --- | --- |
| Source artifacts | Original exports, attachments, scans, portal views and capture metadata | Immutable, addressed by SHA-256 of the received bytes |
| Source assertions | Lossless JSON records organized into stable lanes | Append only; changed assertions coexist |
| Evidence links | Artifact, locator and capture time supporting an assertion | Append additional provenance without duplicating the assertion |
| Coverage | Scope inspected, capture/extraction results, known limits and next steps | Append observations; derive current coverage |
| Projections | Patient snapshot and optional SQLite query index | Rebuildable from the store |
| Agent analyses | Interpretations, calculations and proposed actions | Separate from source assertions; cite evidence and carry status/freshness |

The raw artifact is the recovery path if an extractor misses a field or a schema changes. The canonical record's `data` retains the original structure and narrative; normalization must preserve source values, date strings, units, codes and status. A field not understood by an extractor must remain reachable through the source artifact and be declared as an extraction gap.

## Record identity and provenance

The persisted envelope is defined by [record.schema.json](../schemas/record.schema.json). Its identity is deterministic over `patient_id`, `lane`, `kind`, `source.system`, `source.record_key`, `effective_time`, `status` and `data`. Capture time, artifact hash and locator are excluded from identity: the same assertion seen in a later export should accumulate evidence, not masquerade as a new clinical event.

`record_id` identifies an assertion, not a permanent real-world object. The source record key groups revisions from a source item when that key exists. Source keys are namespaced by source system; they are not safe to merge across organizations without evidence. Unknown keys remain unknown. An extractor should supply a stable source item key whenever available, especially for repeated observations with identical values.

Evidence links retain each distinct artifact and locator with its capture time. Repeated capture of identical bytes may reuse the artifact. A record's first provenance is not its only provenance; consumers should consult the evidence links before evaluating coverage or freshness. An artifact hash proves which bytes were retained, not that those bytes are medically accurate or authentic.

Cross-lane relationships belong in source-preserving data: encounter identifiers, order identifiers, result panel membership, medication/request identifiers, clinician identifiers and document references. Do not fabricate links from similar labels alone. The [lane registry](../schemas/lanes.json) describes expected relationships, not mandatory joins.

## Time, revision and contradiction

Maintain these different notions of time:

- `effective_time`: when the clinical or administrative assertion applies. Preserve partial dates, intervals, source timezone and original text; do not manufacture midnight or a day for a month-only date.
- `captured_at`: when the bank observed the evidence. It does not establish that the event occurred then.
- Source-specific dates inside `data`: authored, signed, ordered, collected, received, resulted, administered, resolved and amended times can all differ. Preserve each with its label.
- Projection and analysis times: when a view or interpretation was generated and through what evidence cutoff.

A second export changing a medication from active to discontinued adds a new assertion with the same source key. The earlier assertion stays available. A late-signed note about an earlier encounter keeps both the clinical date and signature date. Two laboratories reporting different values for a nominally similar test remain separate observations unless the source explicitly identifies one as a correction.

Source status is retained verbatim when available; `unknown` is used when absent. Labels such as active, historical, final, preliminary, corrected, entered-in-error and resolved must not be flattened into a single Boolean. A resolved problem remains part of the longitudinal record. A stopped medication is not a current medication, and inclusion in a historical note does not establish current use.

The snapshot should identify competing assertions and explain any current-state selection rule. A newer capture alone is not authority. Selection must consider source status, clinical date, explicit corrections and source attribution; uncertain reconciliation remains visible. Cross-source duplicates may be clustered for navigation, but their separate evidence must survive.

Coverage observation state has a narrower recency rule: `current_observations` selects the newest evidence capture for each patient, source system, observation kind and stable source record key, retaining all timestamp ties. Unknown or empty keys remain separate by record ID. `observations` retains the complete history; `open_or_unresolved` reflects current observations only, while superseded gap details remain in `historical_gaps`. Thus a later successful inspection of the same scope can resolve a former collection gap. This chooses the most recently observed collection state, not clinical truth. Coverage keys must identify a consistent surface and scope so unrelated date ranges or filters cannot resolve each other's gaps.

## Agent-facing views

The deterministic snapshot is an evidence inventory for orientation: patient/source identity, coverage window, lane counts, recent evidence and limitations. It states an input digest and evidence capture cutoff; rerunning unchanged input does not insert a new wall-clock timestamp into the projection. The capture cutoff is not a clinical event date or proof of recent source review. Clinical summaries or recommendations generated by an agent belong in `agent_analysis`, with record citations, creation time, evidence cutoff, question, assumptions, contradictory evidence, confidence/uncertainty and review status.

The local projection selects source-reported CDA sections for medications, conditions, allergies and immunizations separately by patient, source and section. It prioritizes Patient Health Summary sections when available, then uses document creation time within the preferred candidates, retains ties and shows undated preferred sections separately. This prevents a newly generated encounter-specific export from displacing the longitudinal summary for orientation. Historical individual observations cannot displace those sections simply because their observation date is newer. The selection is a navigation policy, not a claim that a summary is clinically authoritative or current. Older sections and all assertions remain in the canonical lanes.

SQLite `observation_groups` and `observation_members` organize source representations for navigation. A multi-record group requires a substantive source identifier and identical full clinical payload, including raw XML, parent relationships and unknown fields, together with patient, source system/item, lane, kind, status and clinical time. Only explicit document/section provenance is omitted from comparison. Unidentified records stay separate. Compact group display and full-text search omit XML; grouping identity does not.

These groups are not validated unique clinical events. Differences in markup can keep representations of the same event separate; matching identifiers and payload do not establish that one clinical event occurred. `representation_count` counts canonical source assertions represented by that group, not tests performed, illnesses, visits or independent corroborating sources. Follow member record IDs and evidence links before analysis; event-level reconciliation requires additional source-aware validation.

A useful reading path is snapshot → lane → assertion → evidence link → source artifact. An optional SQLite index may accelerate filtering by lane, date or source, but it is not the authoritative copy. Neither a generated snapshot nor an index replaces artifacts or append-only records.

The [care action ledger implementation](../src/health_bank/actions.py) adds schema-version-1 `care_action_event` analyses to the existing analysis lane. Consecutive per-action revisions preserve decisions, reported progress, corrections and attributed outcomes; source assertions remain separate. The standard projection rebuilds the private Markdown ledger and its JSON audit view. This additive event schema requires no migration of existing assertions.

## Storage and confidentiality

Working plaintext lives in ignored `.local/` storage. Optional AES-GCM vaults
support encrypted backups with external keys. Keep real-data deployments,
backups, vault manifests, and keys in separate private storage. Repository
visibility and encryption protect different exposure paths.

## Extensibility

New information enters an existing lane when its meaning fits; add a lane only when the domain meaning differs. Preserve source details first, then improve normalization through versioned extractors. Schema migrations must be explicit and reproducible from immutable artifacts; changing the schema must not silently reinterpret earlier records. Collection coverage and extractor coverage are separate: retaining a document does not mean every assertion within it has been extracted.
