---
status: proposed
date: 2026-10-23
decision-makers: [user, claude]
tags: [agents, diagnosis, trust]
supersedes: 
superseded-by:
---

# ADR-0015: Require evidence citations and a deterministic confidence rubric for diagnoses

## Context and problem statement

The brief requires evidence attached to every root-cause recommendation and confidence
indicators, and forbids guaranteeing root causes. LLMs produce fluent but sometimes
unfounded explanations and self-reported confidence that is poorly calibrated. Users must
be able to see *why* a hypothesis is believed.

## Decision drivers

- No uncited claims presented as root causes (FR-21).
- Confidence that means something and can be calibrated (NFR-12, evaluation.md).
- Reproducibility and auditability.

## Considered options

1. **Structured hypotheses whose claims cite evidence ids; deterministic citation checker re-fetches evidence and verifies numeric claims and code locations; confidence computed by a versioned rubric over verified evidence, evidence diversity, temporal alignment, experiments and contradictions; LLM self-confidence ignored.**
2. Free-text diagnosis with LLM-reported confidence.
3. Rules-only diagnosis (pattern library, no LLM).

## Decision outcome

Chosen option: "**Citations + deterministic rubric**", because it binds every displayed
hypothesis to stored facts and produces confidence bands we can calibrate against the
seeded-defect benchmark. Hypotheses with zero verified supporting citations are discarded;
if none remain, the diagnosis is reported as **inconclusive** with the evidence pack and
suggested next steps. Rule-based detectors (e.g. pool saturation, seq scans) feed evidence
and can raise rubric scores but don't replace the LLM's mechanism reasoning.

### Confidence

Recommended.

### Consequences

- Good: trustworthy UI, measurable calibration, defensible claims.
- Bad / trade-off: some correct-but-uncitable insights are dropped; citation checker must understand evidence types (engineering effort).
- Follow-up: rubric weights tuned on the benchmark and versioned (`confidence_factors.rubric_version`).

### Confirmation

DB-level check that displayed hypotheses have ≥ 1 `hypothesis_evidence` row with
`relation='supports' AND check_result='verified'`; calibration metric in CI evals.

## Pros and cons of the options

### Citations + rubric
- Good: evidence-backed, calibratable.
- Bad: more machinery.

### Free text + LLM confidence
- Good: simple.
- Bad: unverifiable, miscalibrated, erodes trust.

### Rules only
- Good: deterministic.
- Bad: misses novel causes; can't read code semantics.

## More information

agent-workflows.md §5; evaluation.md §4.
