# LLM provider degradation

**Detect:** `LlmProviderDegraded` (> 20 % of calls `unavailable` for 15 min),
`re_llm_requests_total{outcome=…}` on the dashboard, provider status page.

**Built-in behaviour** (`llm/gateway.py`, agents): calls retry with backoff; every graph has
a deterministic baseline it degrades to (discovery rules, test-generation templates,
diagnosis rules); repairs need the model and wait; LangGraph threads resume from checkpoints
when calls succeed again. Users see "delayed — retrying", never a fabricated answer.

## Steps

1. Confirm it is the provider (status page, 429 vs 5xx in worker logs `/ecs/<name>/worker-agents`).
2. **429 / rate limits:** check tokens per purpose on the dashboard for a runaway graph or
   org; budgets cap per-thread spend, quotas cap per org. Lower concurrency by scaling
   `worker-agents` down if needed.
3. **Prolonged outage (> 30 min):** switch the model-dependent steps to their baselines so
   work completes instead of waiting:
   ```bash
   reliability flag agents.discovery.llm_disabled on
   reliability flag agents.testgen.llm_disabled on
   reliability flag agents.diagnosis.llm_disabled on
   reliability flag agents.repair.llm_disabled on      # repairs pause instead of starting
   ```
   Add `--org <id>` to limit to one org. Turn them `off` when the provider recovers.
4. **Quality incident** (model answers wrong but "succeeds"): disable the affected graph the
   same way, then investigate with the stored inputs/outputs (`llm_usage`, agent threads) and
   the eval harness (`benchmarks/harness/run_evals.py`) before re-enabling.
5. Post a status update; diagnoses/repairs requested during the outage resume on their own.
