# Self-hosted and air-gapped installation (P4-S04)

Spec: [v3 §8](../00-intent/03-spec-v3-platform.md#8-scalability-and-self-hosted-operations-changes-v2-15).
Chart: [`deploy/helm/analystos`](../../deploy/helm/analystos). Tests: `tests/unit/test_helm_chart.py`
(lint + template when `helm` or `ANALYSTOS_HELM` is present), `tests/unit/test_model_providers.py`,
`tests/unit/test_oidc_abac.py`, `tests/integration/test_oidc_sso.py`, `tests/unit/test_sandbox_runner.py`.

## 1. What the chart deploys

| Component | Kind | Notes |
|---|---|---|
| API | Deployment + Service + PDB (+ HPA) | `uvicorn --workers N`; startup/readiness on `/api/health`, liveness on the TCP port only (a dependency outage must not restart every pod) |
| Worker pools | one Deployment per task queue (`workers.<pool>.queues`) + PDB + HPA | `analystos worker` with `ANALYSTOS_WORKER_QUEUES`; grace period above the heartbeat window |
| Scheduler | Deployment | claim-then-execute, so 2 replicas in `values-ha.yaml` never double-fire |
| Web | Deployment + Service + PDB | nginx with an upstream ConfigMap pointing at this release's API Service; SSE unbuffered |
| Migrate | pre-install / pre-upgrade Job | `analystos migrate && analystos seed` (with `ANALYSTOS_ENV=production` no demo accounts are seeded) |
| NetworkPolicy | optional, forced on in air-gapped mode | DNS, the release's own pods and `airGapped.allowedEgressCidrs` only |
| ELT worker pool | Deployment at `replicas: 0` | dbt Core is **not** in the app image; scale it up only with an image that has dbt (`ANALYSTOS_DBT_EXECUTABLE`) and the builder login in the Secret |

Postgres (pgvector), Redis, Temporal and Superset are **external** (separate instances in production,
spec v3 §8 "Database topology"). Pods run non-root (uid 10001), read-only root filesystem, all
capabilities dropped, seccomp `RuntimeDefault`.

Credentials are never chart values: create one Secret and name it in `secrets.existingSecret`:

```bash
kubectl create secret generic analystos-secrets \
  --from-literal=ANALYSTOS_DATABASE_URL='postgresql+psycopg://…' \
  --from-literal=ANALYSTOS_ANALYTICS_LOADER_URL='…' --from-literal=ANALYSTOS_ANALYTICS_READER_URL='…' \
  --from-literal=ANALYSTOS_ANALYTICS_BUILDER_URL='postgresql+psycopg://analystos_builder:<strong password>@…/analytics' \
  --from-literal=ANALYSTOS_REDIS_URL='redis://…' --from-literal=ANALYSTOS_JWT_SECRET="$(openssl rand -hex 32)" \
  --from-literal=ANALYSTOS_BOOTSTRAP_ADMIN_PASSWORD='…' --from-literal=ANALYSTOS_SUPERSET_PASSWORD='…' \
  [--from-literal=OPENROUTER_API_KEY=… | ANTHROPIC_API_KEY=… | AZURE_OPENAI_API_KEY=…] [--from-literal=ANALYSTOS_OIDC_CLIENT_SECRET=…]
helm install analystos deploy/helm/analystos -f deploy/helm/analystos/values-ha.yaml -f my-values.yaml
```

`ANALYSTOS_ANALYTICS_BUILDER_URL` is required: the built-in default carries the development password
`builder`, and with `ANALYSTOS_ENV` other than `dev` both `analystos migrate` (which creates the build
login) and the BuildGateway refuse to use it (`build login provisioning skipped: … development password`).

In-flight workflows must be drained (or reach `continue_as_new`) before an upgrade that changes workflow code.

## 2. Offline image bundle

On a connected machine: `scripts/bundle_images.sh --build --tag 0.1.0 [--with-deps]` writes
`dist/analystos-images-0.1.0/` (one `docker save` tarball per image, `manifest.json`, `SHA256SUMS`,
`load.sh`, the packaged chart when helm is installed). On the air-gapped side:
`REGISTRY=registry.internal:5000 ./load.sh` verifies the checksums, loads and pushes; then install with
`--set image.registry=registry.internal:5000`.

## 3. Air-gapped mode

`values-airgapped.yaml` sets `ANALYSTOS_AIR_GAPPED=true` and `ANALYSTOS_MODELS_CONFIG=config/models.airgapped.yaml`:

* **Models**: only providers declared `egress: internal` are routed to (the example is an
  OpenAI-compatible vLLM/Ollama endpoint; `ANALYSTOS_LOCAL_LLM_URL` overrides its URL). The model
  transport refuses every host that is not such a provider *before* opening a connection
  (`EgressBlocked`); `tests/unit/test_model_providers.py::test_air_gapped_calls_only_reach_the_configured_internal_host`
  drives every routed purpose and asserts the only host ever contacted is the configured one.
* **Decisions**: the DecisionService keeps `rules` and `local_classifier` only; `jev` and `llm_structured` drop out.
* **Settings**: apply the `air_gapped` preset once (`POST /api/admin/settings/preset {"preset":"air_gapped"}`):
  the `offline` preset plus a local model as the last rung of chat purposes (deterministic first), decision
  purposes on rules, JEV off. The preset is data; the egress guard is the deployment flag.
* **Workspace policy**: the default `allowed_providers` is `["openrouter", "typesafe", "internal"]` on every
  install, not only air-gapped ones: `internal` matches any provider declared `egress: internal`, so adding
  such a provider to the models file makes it usable in every workspace that has not narrowed the list.
  Remove `internal` from a workspace policy to forbid it there.
* **Network**: the NetworkPolicy drops egress outside `allowedEgressCidrs`.

Acceptance still open: the §62 scenario on an air-gapped install with a real local model (evidence file).

## 4. Model providers

`config/models.providers.example.yaml` shows every type; a models file may `extends: models.yaml` and restate only what changes.

| `type` | Wire API | Key |
|---|---|---|
| `openrouter` (default) | chat completions + Decisions API | `api_key_env` |
| `openai_compatible` | `<base_url>/chat/completions` (vLLM, Ollama, TGI, OpenAI) | optional `api_key_env` |
| `azure_openai` | `<base_url>/openai/deployments/<deployment>/chat/completions?api-version=` | `api-key` header from `api_key_env` |
| `anthropic` | Messages API (`/v1/messages`), system blocks with cache breakpoints, usage incl. cache reads | `x-api-key` from `api_key_env` |
| `bedrock` | Converse via boto3 (AWS credential chain); without boto3 a clear `bedrock_unavailable` error; the boto3 endpoint host must pass the same egress allowlist | none in config |

`openrouter`, `anthropic`, `bedrock` and `azure_openai` are public endpoints and cannot be declared
`egress: internal`; an Azure OpenAI resource behind a private endpoint may, with an explicit `private_link: true`.

A literal `api_key:` in a models file fails validation. Budgets, allowlists, redaction, caching and call records are the same for every provider.

## 5. Single sign-on (OIDC) and ABAC

Set `oidc.enabled`, `issuer`, `clientId`, `redirectUri` (`https://<host>/api/auth/oidc/callback`), the
client secret in the Secret (confidential clients) and the group mapping (`oidc.mapping`, format of
[`config/oidc.yaml`](../../config/oidc.yaml)). Flow: `/api/auth/oidc/login` (state, nonce and PKCE
verifier in a 10-minute signed HttpOnly cookie; S256 challenge to the IdP) → IdP → `/api/auth/oidc/callback`
(code exchange with the verifier, ID token checked against the JWKS: asymmetric algorithms only, issuer,
audience, expiry, nonce, `azp`) → AnalystOS token in the URL fragment of `<web_url>/login`.

* Users are matched by (issuer, subject) in `user_identity`; an existing local account is linked by
  email only when the IdP says `email_verified`.
* Groups grant workspace roles; each login re-synchronises the memberships SSO granted (revoking those no
  longer backed by a group); manual memberships are never touched.
* `platform_admin_groups` applies to accounts SSO created only: they gain and lose platform admin with the
  group (audited `auth.sso_admin_granted` / `auth.sso_admin_revoked`). A local account linked by email keeps
  the admin flag an administrator set; an admin group on it is audited as `auth.sso_admin_not_elevated`.
* Mapped claims become user attributes. Workspace policy `attribute_rules` (ABAC) remove assets or deny
  columns from `resolve_scope` for callers whose attributes do not match; they never widen a scope, and a
  rule must name at least one required attribute with at least one accepted value.
* Clearances are grants, so the IdP never sets them: `pii_clearance`, any `*clearance*` attribute,
  `sso_managed` and `is_admin` are platform-controlled. A mapping that names one is refused at load, a
  login never writes or removes one, and PII clearance is only the boolean `true` an administrator set on
  the user (a string such as `"true"` is not a clearance).
* `oidc.passwordLogin: false` turns password sign-in off (keep it on for a break-glass admin).

Acceptance still open: a live login against a real IdP (Keycloak/Entra) with a dated evidence file.

## 6. Sandbox network isolation

`sandbox/runner.py` starts each child in a new, empty network namespace (`unshare(CLONE_NEWNET)`,
or with a user namespace when unprivileged). `ANALYSTOS_SANDBOX_NETWORK`: `require` (the chart's
default: refuse to run where the kernel does not allow it), `isolate` (the application default outside
the chart: a networked fallback logs a warning on every run and the result says `network_isolated: false`),
`off` (development only).

Under the chart's `RuntimeDefault` seccomp profile an unprivileged pod usually cannot create namespaces, so
with `require` the Python sandbox refuses to run there rather than run with the pod's network. To run it
anyway, either use nodes that allow unprivileged user namespaces (or gVisor), or set
`config.sandboxNetwork=isolate` **together with** `networkPolicy.enabled=true` and
`airGapped.allowedEgressCidrs` for the external services, so the NetworkPolicy is the boundary (the chart's
NOTES warn when `isolate` is set without one).
