# Approved report and alert delivery

External delivery is disabled until an operator sets the relevant environment variables. The
sender needs the workspace `editor` role; a workspace approver then approves the exact recipient,
channel and frozen report or alert. Workspace separation-of-duties policy applies.

| Channel | Required settings | Transport |
|---|---|---|
| Webhook | `ANALYSTOS_EXTERNAL_WEBHOOK_HOSTS` (comma-separated exact hosts), `ANALYSTOS_EXTERNAL_WEBHOOK_SECRET` | HTTPS POST only; public DNS address unless explicitly listed in `ANALYSTOS_OUTBOUND_PRIVATE_HOSTS`; DNS-pinned request, no redirects/proxies. `X-AnalystOS-Signature: sha256=<HMAC>` signs the canonical JSON body (sorted keys, compact separators, UTF-8). `Idempotency-Key` is the approval ID. |
| Email | `ANALYSTOS_EXTERNAL_EMAIL_DOMAINS` (comma-separated exact recipient domains), `ANALYSTOS_EXTERNAL_SMTP_HOST`, `ANALYSTOS_EXTERNAL_SMTP_SENDER`; optional `ANALYSTOS_EXTERNAL_SMTP_PORT` (587), `ANALYSTOS_EXTERNAL_SMTP_USERNAME` and `ANALYSTOS_EXTERNAL_SMTP_PASSWORD` | One recipient per approval; DNS-pinned SMTP with verified STARTTLS. A report is attached in its approved format. `Message-ID` is stable across retries. |

1. `POST /api/workspaces/{workspace_id}/external-deliveries` with
   `{"subject_type":"report|alert","subject_id":"...","channel":"email|webhook","destination":"...","format":"pdf"}`.
   For a report, the format must already exist. The response supplies an approval ID and payload
   hash. The request freezes the report artifact hash and file SHA-256, or the alert content.
2. An approver decides the request through the existing approvals API. The approval page displays
   the exact destination, affected artifact/alert, and payload hash.
3. `POST /api/external-deliveries/{approval_id}/execute` sends it. Current requester/approver
   membership, policy version, destination allowlist and subject bytes are checked again. A second
   call after success returns the recorded result without sending again.

Transient failures leave the approval retryable for up to three attempts while it is valid.
Attempts are serialized across API processes. A process that dies during an attempt can be
retried after five minutes. Delivery after a network timeout has an uncertain remote outcome:
webhook receivers **must deduplicate the `Idempotency-Key`**, and SMTP recipients may see a
duplicate despite the stable `Message-ID`. Operators can inspect `approval.evidence` for attempt
count, last error class and delivery time, and `delivery.failed` / `delivery.sent` audit events.
Webhooks carry the alert snapshot or a base64-encoded report (maximum 1 MB); email attachments
are capped at 10 MB. A `report` or `monitor` schedule can include
`"external_delivery":{"channel":"webhook|email","destination":"...","format":"pdf"}`.
Each fire generates a fresh approval proposal in the schedule result (`delivery_approval_id`, or
one per alert). A human still approves and invokes the execute endpoint for that fire; schedules
never send outside the platform unattended.
