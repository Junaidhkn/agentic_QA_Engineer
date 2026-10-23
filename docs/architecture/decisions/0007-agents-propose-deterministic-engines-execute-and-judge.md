---
status: accepted
date: 2026-10-23
decision-makers: [user, claude]
tags: [agents, safety, verification]
supersedes: 
superseded-by:
---

# ADR-0007: Agents propose; deterministic engines execute and judge

## Context and problem statement

The brief requires "safe, independently verified repairs" and forbids claiming guaranteed
coverage or fixes. LLM agents are good at synthesis (workflows, test code, hypotheses,
patches) but are poorly calibrated about their own correctness and are exposed to prompt
injection from repo content, web pages and logs (threat model A8). If an agent both
produces and judges its output, "verified" becomes self-assessment.

## Decision drivers

- Independence of verification from generation.
- Safety: no agent action can cause target traffic, merges, deployments, or permission changes.
- Reproducibility of anything shown as a fact (findings, thresholds, confidence, verdicts).

## Considered options

1. **Split responsibilities: agents produce proposals (IR, code, hypotheses, patches); deterministic code admits, executes, measures, checks citations, scores confidence, runs gates and creates PRs after human approval.**
2. Fully agentic loop: an agent runs tests, interprets results, and decides when a fix is done (common "autonomous SWE agent" design).
3. Agent judge: a second LLM reviews the first agent's output and decides "verified".

## Decision outcome

Chosen option: "**Agents propose, deterministic engines execute and judge**", because it
makes every user-visible verdict reproducible and keeps safety-critical actions outside the
reach of prompt injection. Concretely:
- Run admission, execution, abort and kill: deterministic (`admission`, `runs`, runner agent).
- Findings from thresholds/baselines: deterministic.
- Hypothesis citations re-checked and confidence computed by a rubric (ADR-0015).
- "Verified" decided by the verification pipeline (ADR-0016), which the repair agent cannot call or influence except by producing a diff.
- PR creation by a template-driven job after human approval (ADR-0017).
- Agent tools are read-only or sandbox-scoped; none can approve, push, merge, deploy, change targets, quotas or secrets.

An LLM "reviewer" may be added later as an **advisory** signal shown to humans, never as a gate.

### Confidence

Recommended.

### Consequences

- Good: trustworthy verdicts; contained blast radius of injection; easy audit.
- Bad / trade-off: less "autonomous" UX; more engineering for deterministic checks (citation checker, gate pipeline, diff policy).
- Follow-up: CODEOWNERS on `admission/`, `verification/`, `runners/`, `integrations/github/` requiring security review.

### Confirmation

Architecture test: agent tool registry contains no tool with write access outside the
sandbox session; static check that `verification` and `github` modules are not importable
from `agents/`. Prompt-injection suite (threat-model §8).

## Pros and cons of the options

### Propose / judge split
- Good: independence, safety, reproducibility.
- Bad: more code paths.

### Fully agentic
- Good: flexible, fewer components.
- Bad: self-graded results, injection reaches privileged actions.

### LLM judge as gate
- Good: catches some semantic errors.
- Bad: correlated failures with the generator; not reproducible.

## More information

agent-workflows.md §1, §7; threat-model.md A8–A10.
