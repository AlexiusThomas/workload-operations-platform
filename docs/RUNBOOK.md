# WOP Operational Runbook (NFR-008 AC-3)

Deployment procedures, rollback steps, and common failure scenarios for the Workload
Operations Platform. Amazon-internal integrations remain INTERNAL-INTEGRATION-TODO and
are NOT fabricated here.

## 1. Prerequisites
- Python 3.11, Node.js 22.x LTS (required by the jsii/CDK toolchain; do not use Node 24.x).
- `pip install -r requirements.txt`.
- AWS credentials with least-privilege deploy permissions (deploy only; not needed for CI validation).
- `ENVIRONMENT` (dev|staging|prod) and `AUTH_MODE`. In `prod`, `AUTH_MODE` must be the approved
  provider (never empty/synthetic) or the compute stack fails synth by design (SEC-001 AC-4).

## 2. CI validation (no deploy)
CI (see `ci/pipeline.yml` / `.github/workflows/ci.yml`) runs lint, unit+property tests with the
80% coverage gate, Task 20 infra synth tests, a deploy-free build, and integration/concurrency/e2e
tests. No AWS credentials or deployment occur during validation.

## 3. Deployment procedure (manual until DEP-005 is resolved)
1. Confirm CI is green on the target commit.
2. Export environment configuration: `ENVIRONMENT`, `AUTH_MODE`, and CDK env
   (`CDK_DEFAULT_ACCOUNT`, `CDK_DEFAULT_REGION`) — never hardcode account IDs/regions.
3. `cdk synth` and review the CloudFormation output (IAM scope, encryption, public access).
4. `cdk deploy wop-data-stack-<env> wop-compute-stack-<env> wop-monitoring-stack-<env>`.
5. Post-deploy: verify DynamoDB tables/GSIs, Lambda env vars, EventBridge rules, and alarms.

NOTE: The production build/deploy pipeline tool is unconfirmed (DEP-005). Wire steps 3-4 into
the approved internal tool when selected.

## 4. Rollback
- **Infrastructure**: `cdk deploy` the previous known-good commit, or roll back the
  CloudFormation stack to its last stable version. Data tables use RETAIN in prod, so a stack
  rollback does not delete DynamoDB data.
- **Application**: re-deploy the prior Lambda artifact (build output is environment-agnostic).
- Confirm rollback by re-checking table/GSI presence and alarm state.

## 5. Common failure scenarios
- **Prod synth fails with AUTH_MODE error**: expected fail-closed guard; set `AUTH_MODE` to the
  approved provider (DEP-002 Midway) before deploying to prod.
- **cdk synth crash on Windows (jsii ENOTEMPTY at teardown)**: a Node temp-dir cleanup race; the
  synth output is still produced. Use Node 22.x LTS. Prefer running synth/tests via pytest.
- **Coverage gate failure (<80%)**: add tests; do not lower the threshold.
- **Ingestion failure (DEP-001 RATS)**: partial ingestion must not leave WorkPackages AVAILABLE;
  records stay IMPORTED or move to error state with an AuditEvent (NFR-009 AC-4).
- **Unhandled Lambda exception**: structured error response + CloudWatch alarm within 5 minutes
  (NFR-009 AC-2); check the high-error-rate alarm and X-Ray traces.

## 6. Integration boundaries (INTERNAL-INTEGRATION-TODO)
- DEP-001 RATS ingestion schema/endpoint — unconfirmed.
- DEP-002 Midway authentication — production provider is a documented NotImplemented boundary.
- DEP-005 internal CI/CD deploy tooling — unconfirmed.
- OQ-007 event retention (TTL) — unconfirmed; no TTL set on audit/events tables.
