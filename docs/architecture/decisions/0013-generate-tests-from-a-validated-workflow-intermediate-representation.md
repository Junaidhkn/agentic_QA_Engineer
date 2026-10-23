---
status: proposed
date: 2026-10-23
decision-makers: [user, claude]
tags: [agents, testing]
supersedes: 
superseded-by:
---

# ADR-0013: Generate tests from a validated Workflow intermediate representation

## Context and problem statement

Discovery output must become executable tests (FR-10), be reviewable by users before
anything runs (UF-3), feed both Playwright and k6, and support invariants for race
conditions (FR-13). Asking an LLM to write test code directly from routes and pages yields
unreviewable, inconsistent code and couples discovery to one test tool.

## Decision drivers

- Human review of *what* is tested, separate from *how*.
- One source for functional tests, load scenarios and concurrency probes.
- Deterministic validation before code generation.
- Stable artifacts across model/prompt changes.

## Considered options

1. **A versioned JSON Workflow IR (steps, API equivalents, data, preconditions, invariants, side effects, load weight) validated by JSON Schema and reference checks; code generated per tool from IR + templates.**
2. Direct LLM generation of Playwright/k6 code from discovery context.
3. Record-and-replay only (user records flows in a browser).

## Decision outcome

Chosen option: "**Workflow IR**", because it is the reviewable contract between discovery
and generation: users approve and edit IR, deterministic validators check it against the
route table and UI traces, and generators fill code templates per tool. Manual workflows
are first-class (users can write IR or record flows that are converted to IR).

### Confidence

Recommended.

### Consequences

- Good: reviewable, tool-agnostic, versioned; regeneration on model upgrades is safe.
- Bad / trade-off: schema design and evolution effort; IR can't express everything (escape hatch: `custom_code` step requiring manual review, Deferred).
- Follow-up: `packages/workflow-ir` with JSON Schema, generated Python/TS types, compatibility tests.

### Confirmation

Schema compatibility check in CI; every generated test case references its IR hash.

## Pros and cons of the options

### Workflow IR
- Good: review, validation, multi-target generation.
- Bad: upfront schema work.

### Direct code generation
- Good: fastest to prototype.
- Bad: hard to review/validate; inconsistent.

### Record-and-replay only
- Good: precise flows.
- Bad: manual effort; contradicts automatic discovery goal (feature 1).

## More information

low-level-design.md §6–7; agent-workflows.md §3–4.
