---
status: proposed
date: 2026-10-23
decision-makers: [user, claude]
tags: [verification, repair, statistics]
supersedes: 
superseded-by:
---

# ADR-0016: Gate "verified" status on independent checks and statistical load comparison

## Context and problem statement

The brief: "Mark repairs verified only after functional, regression, security, and
load-test gates pass" and "measurable performance improvements". Load-test results are
noisy; single before/after runs routinely differ by 5–20% with no code change. A patch can
also pass its own new test while breaking other behavior or overfitting.

## Decision drivers

- Independence from the patch author (ADR-0007).
- Statistical validity of performance claims.
- Catch overfitting, regressions and unsafe changes.

## Considered options

1. **Fixed gate pipeline: build → existing tests → regression test fail-to-pass (3×/3×) → functional suite → security diff scans → load comparison (interleaved ≥ 3 runs each side, bootstrap CI vs measured noise floor) → invariant probe (if applicable) → diff policy. "Verified" iff every applicable gate passes.**
2. Single before/after load run with a percentage threshold.
3. Human judgement only (show results, let reviewer decide).

## Decision outcome

Chosen option: "**Fixed gate pipeline with statistical comparison**", because each gate
targets a known failure mode: fail-to-pass proves the test captures the defect; the existing
and functional suites catch collateral damage; security diff scans catch introduced
vulnerabilities/secrets/vulnerable deps; interleaved repeated runs with confidence intervals
separate real improvement from noise; the diff policy prevents scope creep and dangerous
file changes. Human review remains mandatory after "verified" (ADR-0017).

### Confidence

Recommended.

### Consequences

- Good: "verified" has a precise, reproducible meaning; comparisons show CIs.
- Bad / trade-off: performance verification costs ≥ 6 load runs (~25–40 sandbox minutes); some real but small improvements are `inconclusive`.
- Follow-up: A/A noise-floor measurement per project/environment (weekly); allow users to request more runs for inconclusive results.

### Confirmation

Invariant check job: no repair in `verified` without all applicable gates `passed`;
evaluation false-verified rate (NFR-13).

## Pros and cons of the options

### Gate pipeline + statistics
- Good: rigorous, explainable.
- Bad: time and compute.

### Single-run threshold
- Good: cheap, fast.
- Bad: false improvements and false regressions.

### Human-only
- Good: flexible.
- Bad: inconsistent, not "independently verified".

## More information

low-level-design.md §9; evaluation.md §6.
