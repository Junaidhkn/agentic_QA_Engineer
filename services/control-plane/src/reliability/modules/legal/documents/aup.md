# Acceptable Use Policy (load-testing consent)

> **Draft for legal review — not yet in force.** Version 2026-10-25.

Load and functional tests send real traffic. This policy exists so that traffic only ever
reaches systems whose owners want it.

## You confirm that
1. **Every target you add is yours or you are authorized in writing to test it**, at the
   volume and times you schedule. Ownership verification (DNS record or well-known file) is a
   technical check, not a substitute for authorization.
2. Tests against **production** systems are approved by someone entitled to approve them and
   run within agreed windows.
3. Third-party services your application calls (payment providers, email, APIs) permit the
   traffic your tests cause, or you stub them in a sandbox.
4. Test accounts and data used by tests are yours to use; you do not put real people's
   personal data into test inputs.

## You must not
- test, scan or load systems you are not authorized to test, or use the service to cause a
  denial of service;
- evade or interfere with verification, rate limits, plan limits, the egress allowlist or the
  sandbox (including running mining, spam or attack tooling in sandboxes);
- use the service to probe for vulnerabilities in systems you do not own;
- upload malware or content that is illegal, or use AI features to generate it;
- resell or share access outside your organization without our agreement.

## How we enforce it
Targets must pass ownership verification, which we re-check before runs and periodically.
Some first load tests of new organizations wait for a short manual safety review. We may
refuse or stop runs, suspend organizations, and cooperate with owners of systems affected by
misuse. Report abuse to the address published with our egress IP list.
