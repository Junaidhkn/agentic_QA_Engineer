#!/bin/bash
# Runner host agent (runs as runner-host-agent.service next to the runner container).
#  * Every 30 s publishes this host's slot utilization (busy / slots from the runner's status
#    file) as ReliabilityEngineer/Execution SlotUtilization; the autoscaling group tracks its
#    average (infra/terraform/modules/execution-plane).
#  * When the autoscaling group starts terminating the instance (termination lifecycle hook),
#    stops the runner gracefully — no new leases, active jobs finish — then completes the
#    lifecycle action so the instance is terminated only after its runs are done.
set -uo pipefail
source /etc/runner-host.env  # REGION, ASG_NAME, LIFECYCLE_HOOK, STATUS_FILE

imds() {
  local token
  token=$(curl -sf -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')
  curl -sf -H "X-aws-ec2-metadata-token: $token" "http://169.254.169.254/latest/meta-data/$1"
}

INSTANCE_ID=$(imds instance-id)

publish() {
  [ -s "$STATUS_FILE" ] || return 0
  local util
  util=$(python3 -c 'import json,sys; s=json.load(open(sys.argv[1])); print(min(s["busy"]/max(s["slots"],1), 1.0))' "$STATUS_FILE") || return 0
  aws cloudwatch put-metric-data --region "$REGION" --namespace ReliabilityEngineer/Execution \
    --metric-name SlotUtilization --unit None --value "$util" \
    --dimensions AutoScalingGroupName="$ASG_NAME" >/dev/null 2>&1 || true
}

drain_and_release() {
  logger -t runner-host-agent "termination requested: draining runner"
  # runner-agent.service stops the container with a long grace period (see its unit).
  systemctl stop runner-agent
  aws autoscaling complete-lifecycle-action --region "$REGION" --auto-scaling-group-name "$ASG_NAME" \
    --lifecycle-hook-name "$LIFECYCLE_HOOK" --instance-id "$INSTANCE_ID" --lifecycle-action-result CONTINUE
  logger -t runner-host-agent "drained; lifecycle action completed"
  exit 0
}

while true; do
  if [ "$(imds autoscaling/target-lifecycle-state 2>/dev/null)" = "Terminated" ]; then
    drain_and_release
  fi
  publish
  sleep 30
done
