#!/usr/bin/env bash
# Gate a deploy on a Reliability Engineer run (FR-32).
#
# Starts a run of the project's approved suite against a verified target (or a sandbox build of
# a commit), polls the CI verdict and exits:
#   0  passed                 1  failed (thresholds, functional failures, new findings)
#   2  not runnable / error   3  waiting for approval (first load run on a target, prod targets)
#
# Required: RE_API_URL, RE_API_KEY (scopes runs:write + runs:read), RE_PROJECT_ID, RE_RUN_KIND
#           (functional | load | probe), and RE_TARGET_ID or RE_COMMIT_SHA (sandbox build).
# Optional: RE_LOAD_PROFILE_ID (load runs), RE_SUITE_ID, RE_TIMEOUT_S (default 1800), RE_POLL_S (10).
set -euo pipefail

: "${RE_API_URL:?}" "${RE_API_KEY:?}" "${RE_PROJECT_ID:?}" "${RE_RUN_KIND:?}"
api="${RE_API_URL%/}/v1"
auth=(-H "Authorization: Bearer ${RE_API_KEY}" -H "Content-Type: application/json")
timeout_s="${RE_TIMEOUT_S:-1800}"
# One run per CI attempt: retries of the same job reuse it instead of sending more traffic.
idem="ci-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}-${RE_RUN_KIND}"

if [[ -n "${RE_TARGET_ID:-}" ]]; then
  env_json=$(jq -n --arg t "$RE_TARGET_ID" '{type: "target_url", target_id: $t}')
elif [[ -n "${RE_COMMIT_SHA:-}" ]]; then
  env_json=$(jq -n --arg c "$RE_COMMIT_SHA" '{type: "sandbox_build", commit_sha: $c}')
else
  echo "set RE_TARGET_ID or RE_COMMIT_SHA" >&2; exit 2
fi
body=$(jq -n --arg k "$RE_RUN_KIND" --argjson env "$env_json" \
  --arg p "${RE_LOAD_PROFILE_ID:-}" --arg s "${RE_SUITE_ID:-}" \
  '{kind: $k, environment: $env} + (if $p != "" then {load_profile_id: $p} else {} end)
                                  + (if $s != "" then {suite_id: $s} else {} end)')

resp=$(curl -sS -w '\n%{http_code}' -X POST "${api}/projects/${RE_PROJECT_ID}/runs" "${auth[@]}" \
  -H "Idempotency-Key: ${idem}" -d "$body")
code=$(tail -n1 <<<"$resp"); json=$(sed '$d' <<<"$resp")
if [[ "$code" != "202" ]]; then
  echo "could not start the run (HTTP $code): $(jq -r '.error.message // .' <<<"$json")" >&2; exit 2
fi
run_id=$(jq -r .id <<<"$json")
echo "run ${run_id} $(jq -r .status <<<"$json")"

deadline=$(( $(date +%s) + timeout_s ))
while :; do
  verdict=$(curl -sS -f "${api}/runs/${run_id}/verdict" "${auth[@]}") || { echo "verdict request failed" >&2; exit 2; }
  status=$(jq -r .status <<<"$verdict")
  if [[ "$(jq -r .finished <<<"$verdict")" == "true" ]]; then break; fi
  if [[ "$status" == "pending_approval" ]]; then
    echo "run is waiting for approval: $(jq -r .url <<<"$verdict")"; exit 3
  fi
  if (( $(date +%s) > deadline )); then echo "timed out waiting for run ${run_id} (${status})" >&2; exit 2; fi
  sleep "${RE_POLL_S:-10}"
done

summary=$(jq -r '"status: \(.status)\(if .reason then " (\(.reason))" else "" end)\n" +
  (if .summary then "p95 \(.summary.p95_ms) ms, errors \((.summary.error_rate // 0) * 100)%, \(.summary.rps) req/s\n" else "" end) +
  ([.failed_thresholds[] | "threshold failed: \(.scope // "overall") \(.metric) \(.observed) > \(.threshold)"] | join("\n")) +
  ([.new_findings[] | "\nnew finding [\(.severity)]: \(.title)"] | join("")) +
  "\n\(.url)"' <<<"$verdict")
echo "$summary"
if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
  { echo "### Reliability check: ${status}"; echo; echo '```'; echo "$summary"; echo '```'; } >> "$GITHUB_STEP_SUMMARY"
fi

case "$(jq -r .passed <<<"$verdict")" in
  true) exit 0 ;;
  false) [[ "$status" == "failed" ]] && exit 1 || exit 2 ;;
  *) exit 2 ;;
esac
