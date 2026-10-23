# Benchmarks

Evaluation corpus and harness for the agentic workflows (docs/architecture/evaluation.md).

- `reference-apps/` — small apps in the supported stacks with `labels.json`: expected routes and
  pages (route-recall ground truth), how to run the app, and seeded defects (`DEFECTS=…`) with
  their mechanism and the files a correct fix touches (diagnosis and repair benchmarks).
- `harness/run_evals.py` — runs discovery on every app and, where the app can be started,
  dry-runs the generated tests 3× with k6. Writes `benchmarks/out/report.json` and a Markdown
  summary, and exits non-zero when a gate fails.

```bash
cd services/control-plane
uv run python ../../benchmarks/harness/run_evals.py            # deterministic baseline (no API key)
RE_LLM_PROVIDER=anthropic RE_ANTHROPIC_API_KEY=… \
  uv run python ../../benchmarks/harness/run_evals.py --llm    # with the model step
```

Gates (evaluation §4): route recall ≥ 95%, generated-test validity ≥ 85%.
