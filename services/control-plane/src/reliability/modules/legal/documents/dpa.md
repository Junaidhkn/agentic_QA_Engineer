# Data Processing Addendum

> **Draft for legal review — not yet in force.** Version 2026-10-25.

This addendum applies where we process personal data on your behalf (GDPR Art. 28). You are
the controller; we are the processor.

## Processing
- **Subject matter:** operating the service — storing code snapshots, test definitions,
  results, telemetry and artifacts; running tests; producing diagnoses and patches.
- **Data subjects:** your users whose data may appear in test results, telemetry or code;
  members of your organization.
- **Categories:** account data of members (name, email); anything you place in tests, code or
  telemetry. Do not put special-category data into tests.
- **Duration:** while your organization exists, plus the retention below.

## Our commitments
1. Process only on your documented instructions (these terms and your use of the service).
2. Confidentiality of everyone with access; operator access is audited.
3. Security measures: tenant isolation enforced in the database, encryption in transit and at
   rest (KMS-managed keys), secrets sealed to the executing host, isolated sandboxes, an
   append-only audit log, monitoring and incident response.
4. Sub-processors only from the published [list](subprocessors.md); 30 days' notice of
   changes, with a right to object.
5. Assist with data-subject requests and impact assessments; notify personal-data breaches
   without undue delay and within 72 hours of becoming aware.
6. **Deletion:** on request or termination we delete your organization's data (rows, stored
   objects, agent state) within 30 days. Database backups expire within 14 days. The
   organization's audit log is retained for up to one year as the record of actions taken,
   then deleted. Telemetry expires after 30 days.
7. No use of your data to train AI models. The AI model provider processes prompts under
   zero/limited-retention terms.
8. Audits: we provide our security documentation and answer reasonable questionnaires once a
   year.

## Transfers
Primary processing in the United States (AWS us-east-1, backups in us-west-2). Transfers from
the EEA/UK rely on the Standard Contractual Clauses.
