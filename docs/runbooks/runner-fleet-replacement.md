# Runner fleet replacement (gVisor/kernel CVE, bad AMI, dying hosts)

Runner hosts are an EC2 Auto Scaling group (`infra/terraform/modules/execution-plane`) built
from `infra/packer/runner-host.pkr.hcl`. A runner that gets SIGTERM stops taking leases and
finishes the jobs it has (`runner_agent.main`); the host agent publishes `SlotUtilization` for
scaling and drains the host on termination, so normal replacement never cuts a run short.

## Planned or routine (weekly AMI, non-critical CVE)

1. Bake: `packer build infra/packer/runner-host.pkr.hcl` (OS, Docker, gVisor patches,
   pre-pulled runner images by digest). Record the AMI id.
2. Set `runner_ami_id` and `terraform apply` in `envs/<env>` (execution account). The
   launch template changes; start an instance refresh:
   ```bash
   aws autoscaling start-instance-refresh --auto-scaling-group-name <name>-runner \
     --preferences '{"MinHealthyPercentage": 80, "InstanceWarmup": 120}'
   ```
   Terminating instances drain: the `drain` lifecycle hook holds them in
   `Terminating:Wait`, the host agent (`infra/packer/files/runner-host-agent.sh`) stops the
   runner (no new leases, active jobs finish, ≤ 65 min) and completes the action.
3. Watch `re_job_start_seconds` p95 and `re_queue_oldest_age_seconds` during the refresh;
   warm pool absorbs the churn (operations §5).

## Critical (exploitable container escape, active compromise)

1. `reliability kill-switch on` — no new work, active runs cancelled (`kill-switch.md`).
2. Detach the compromised hosts from the ASG and isolate them (security group with no
   egress) for forensics; terminate the rest:
   `aws autoscaling update-auto-scaling-group --min-size 0 --desired-capacity 0 …`.
3. Bake and roll out the fixed AMI (above), restore min/desired, confirm a canary pass
   (`reliability canary`, or wait 15 min), then `reliability kill-switch off`.
4. Rotate the runner signing key (`runner-signing-key` secret → redeploy API) so tokens held
   by compromised hosts stop working; issue new runner tokens
   (`reliability issue-runner-token`) through the bootstrap secret.

## Hosts dying (`RunnerLeasesExpiring`, `JobStartBudgetBurn*`)

* Runs on a lost host end `errored: runner lost (missed heartbeats)` with quota refunded;
  load runs are never retried automatically.
* Check the ASG activity history and the hosts' system logs (CloudWatch agent). Common
  causes: disk full from images (raise volume size), OOM from sandbox runs (lower slots),
  PrivateLink path unhealthy (`<name>-privatelink-unhealthy` alarm in the control plane).
