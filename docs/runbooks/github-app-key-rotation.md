# GitHub App key rotation

The control plane authenticates as the GitHub App with one private key
(`RE_GITHUB_PRIVATE_KEY`, secret `<name>/github-private-key`) and verifies webhooks with
`RE_GITHUB_WEBHOOK_SECRET`. GitHub allows several active private keys per app, so rotation
has no downtime.

## Scheduled rotation (every 6 months) or suspected leak

1. GitHub → App settings → **Generate a private key**. Download the `.pem`.
2. Store it: `aws secretsmanager put-secret-value --secret-id <name>/github-private-key
   --secret-string file://key.pem` (newlines are kept; `\n` escapes are also accepted).
3. Force new deployments of `api`, `worker-github` and `worker-default` (they read secrets at
   start). Check a GitHub call works: open any project's Repository page, or watch
   `/ecs/<name>/worker-github` for authentication errors.
4. **Delete the old key** in GitHub. On a suspected leak do this immediately after step 3,
   and review the App's recent activity (installation tokens issued, PRs/branches created —
   the App can only push `reliability/*` branches and open PRs, never merge).
5. Securely delete the downloaded `.pem`.

## Webhook secret

Set the new secret in GitHub and in `<name>/github-webhook-secret` within a few minutes of
each other and redeploy `api`; deliveries in between fail signature checks and GitHub
retries them (redeliver from the App's Advanced tab if needed).
