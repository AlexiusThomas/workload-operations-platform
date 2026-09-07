# infra/ — AWS CDK (Python) Infrastructure as Code

This directory holds the AWS CDK (Python) infrastructure definitions for the Workload
Operations Platform (WOP).

**Status: scaffold placeholder only.** The real stacks are implemented in **Task 20**:

- `wop-data-stack` — DynamoDB tables (main, audit, events, idempotency) with GSIs, encryption,
  PITR; S3 import/archive/error buckets; SSM parameters.
- `wop-compute-stack` — Lambda functions, Lambda Layer, API Gateway (HTTPS-only, `/v1/` routes),
  EventBridge rules (rollover, report), least-privilege IAM roles.
- `wop-monitoring-stack` — CloudWatch log groups, custom metrics, alarms, SNS topics, X-Ray.

No DynamoDB tables, Lambda functions, or other AWS resources are defined yet. Per the design,
the chosen IaC tool is AWS CDK (Python). ECS, RDS, Redis, Kubernetes, and Step Functions are
explicitly out of scope (CON-002).

> The specific IaC tool mandate (OQ-010) and internal CI/CD integration (DEP-005) remain
> INTERNAL-INTEGRATION-TODO.
