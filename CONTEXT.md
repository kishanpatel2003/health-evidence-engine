# Health Bank

A patient-owned body of health evidence and its interpretations. It supports a traceable view of the patient over time without claiming to represent the whole person.

## Language

**Patient**:
The person whose health is represented by the bank, distinct from an account holder, caregiver, clinician or collecting agent.
_Avoid_: User, account

**Health Bank**:
The accumulated health evidence, source assertions and explicitly identified interpretations associated with a patient.
_Avoid_: Complete patient, digital twin

**Source Artifact**:
A preserved item of evidence as received from a source, such as an export, portal view, report, scan or message attachment.
_Avoid_: Fact, summary

**Source Assertion**:
A statement a source makes about the patient, including its wording, status, clinical time and attribution. It is evidence of what was documented, not a guarantee that the statement is clinically true.
_Avoid_: Ground truth, confirmed fact

**Evidence Link**:
The connection between a source assertion and the artifact location where it was observed during a capture.
_Avoid_: Citationless fact

**Data Lane**:
A stable area of the patient's record organized by the kind of health or administrative information it contains.
_Avoid_: Folder of truth

**Capture**:
An observation of a source at a particular time that preserves evidence of what was accessible then.
_Avoid_: Clinical event, update

**Clinical Time**:
The date or interval to which a source assertion applies, with its original precision and uncertainty.
_Avoid_: Capture date, import date

**Revision**:
A changed source assertion associated with the same source item, retained alongside earlier assertions.
_Avoid_: Replacement, correction unless explicitly documented

**Patient Snapshot**:
A concise, dated view of selected evidence and unresolved gaps that helps an agent orient to the patient's record.
_Avoid_: Complete record, clinical assessment

**Coverage**:
The documented extent and limits of inspection, capture and extraction for a source surface and period.
_Avoid_: Completeness

**Patient Report**:
Information supplied by the patient or an identified caregiver, with its attribution preserved.
_Avoid_: Clinician-confirmed fact

**Agent Analysis**:
A dated interpretation or calculation produced by an agent, with cited evidence, uncertainty and review status.
_Avoid_: Source assertion, diagnosis

**Freshness**:
The relationship between the age of evidence, its inspected coverage and the needs of a particular question.
_Avoid_: Latest equals correct
