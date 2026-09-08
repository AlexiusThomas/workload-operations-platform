# infra/ - AWS CDK (Python) Infrastructure as Code

AWS CDK (Python) infrastructure definitions for the Workload Operations Platform (WOP),
implemented in **Task 20**. The app synthesizes CloudFormation for review; **nothing is
deployed to AWS** from this repository.

## Stacks

- **`wop-data-stack`** (`stacks/data_stack.py`) - DynamoDB tables (main, audit, events,
  idempotency) with GSI-1..7, AWS-managed encryption, point-in-time recovery (all tables
  except idempotency), the idempotency `ttl` attribute; S3 import/archive/error buckets with
  SSE, block-all-public-access, and enforced TLS; SSM parameters publishing table/bucket names.
- **`wop-compute-stack`** (`stacks/compute_stack.py`) - Lambda functions, a shared idempotency
  Lambda Layer, API Gateway (`/v1/` routes), EventBridge rules (rollover daily, report weekly),
  and per-Lambda least-privilege IAM roles. Audit and events tables receive PutItem only. A
  synth-time guard fails a prod build when `AUTH_MODE` is empty or `synthetic`.
- **`wop-monitoring-stack`** (`stacks/monitoring_stack.py`) - CloudWatch log groups, custom
  metric filters, alarms (error rate, p95 latency, claim-conflict spike, ingestion failure,
  unhandled error), an SNS alarm topic, and X-Ray active tracing.

## Environment-agnostic

Account and region come from the CDK environment (`CDK_DEFAULT_ACCOUNT` / `CDK_DEFAULT_REGION`)
at synth/deploy time. No account IDs, regions, secrets, or endpoints are hardcoded. Resource
names are derived from an `environment` string (dev/staging/prod).

## Synthesize (deploy-free)

The jsii/CDK toolchain requires **Node.js 22 LTS** (Node 24.x is incompatible with the pinned
jsii runtime). Synthesis is validated programmatically:

```bash
pip install -r requirements.txt
pytest tests/infra           # Template.from_stack synth/assertion tests (Task 20.4)
python infra/app.py          # bare synth to cdk.out (no deployment)
```

ECS, RDS, Redis, Kubernetes, and Step Functions are out of scope (CON-002).

## Integration boundaries (INTERNAL-INTEGRATION-TODO)

- **DEP-001 (RATS ingestion):** the import bucket receives workload files via S3 PutObject;
  the upstream file schema/owner is unconfirmed. Not fabricated.
- **DEP-002 (Midway auth):** production `AUTH_MODE=midway` selects a documented
  NotImplemented provider; no Midway endpoints or tokens are configured.
- **DEP-005 (CI/CD deploy tooling)** and **OQ-007 (event retention/TTL)** remain unresolved.
