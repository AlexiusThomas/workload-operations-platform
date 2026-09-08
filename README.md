# Workload Operations Platform (WOP) - V1

The Workload Operations Platform is an internal web application that centralizes the management
of operational workload from intake through verified completion. V1 delivers the core
workflow: ingestion, assignment, execution, material runner workflow, verification, and final
completion.

## Project status

Implemented through **Task 22**:

- **Backend** - Python AWS Lambda handlers, domain layer, auth/RBAC, idempotency, and the
  DynamoDB data access layer, with unit, property, integration, concurrency, and end-to-end
  synthetic tests.
- **AWS CDK infrastructure (Task 20)** - data, compute, and monitoring stacks defined in
  Python CDK, validated by deploy-free synth/assertion tests. Nothing is deployed to AWS.
- **CI pipeline (Task 21)** - a pipeline-agnostic definition plus a GitHub Actions reference
  workflow running lint, the full backend suite with the 80% coverage gate, infra synth
  tests, and frontend checks. No AWS credentials or deployment occur in CI.
- **React frontend (Task 22)** - React 18 + TypeScript (Vite) SPA consuming the backend
  `/v1/` API, with role-gated screens for queue, work tracking, materials, audit, and reports.
- **Role-based Help/Tutorial (Task 22)** - a replayable, role-adaptive in-app tutorial whose
  steps derive from the actual RBAC model and show only actions each role may perform.

**Task 23 (final checkpoint - ensure all tests pass) is the remaining outstanding task.**

## Technology stack

Fixed per design (CON-001, CON-002):

- **Backend:** Python + AWS Lambda
- **Datastore:** Amazon DynamoDB (on-demand, multi-table)
- **Storage:** Amazon S3 (import / archive / error prefixes)
- **Scheduling:** Amazon EventBridge
- **Observability:** Amazon CloudWatch + AWS X-Ray
- **Infrastructure as Code:** AWS CDK (Python)
- **Frontend:** React 18 + TypeScript (Vite)

ECS, RDS, Redis, Kubernetes, and AWS Step Functions are explicitly out of scope.

## Repository layout

```
.
|-- backend/            # Python AWS Lambda source, domain layer, shared libraries
|   |-- domain/         # Pure domain logic (state machine, quantity, aggregate state)
|   |-- auth/           # Authentication/authorization abstraction + RBAC matrix
|   |-- idempotency/    # Idempotency layer
|   |-- data/           # DynamoDB data access layer
|   |-- handlers/       # Lambda handlers (ingestion, workunit, material, audit, report, ...)
|   |-- observability/  # Structured logging, metrics, error handling
|   `-- synthetic/      # Synthetic data generator (prod-guarded)
|-- infra/              # AWS CDK (Python) IaC - data / compute / monitoring stacks (Task 20)
|-- frontend/           # React + TypeScript SPA (Task 22)
|-- ci/                 # Pipeline-agnostic CI definition (Task 21)
|-- .github/workflows/  # GitHub Actions reference workflow (Task 21)
|-- docs/               # Operational runbook
|-- tests/              # unit / property / integration / concurrency / e2e / infra
|-- pyproject.toml      # Packaging, pytest, coverage, black, mypy config
|-- requirements.txt    # Dependency list (mirror of pyproject)
`-- .flake8             # flake8 config (consistent with black)
```

## Getting started (backend)

```bash
python -m venv .venv
# Windows PowerShell:  .venv\Scripts\Activate.ps1
# Linux/macOS:         source .venv/bin/activate

pip install -e ".[dev]"       # or: pip install -r requirements.txt
```

### Lint, type-check, and test

```bash
black --check backend infra tests
flake8 backend infra tests
mypy backend infra
pytest tests/unit tests/property tests/integration tests/concurrency tests/e2e \
  --cov=backend --cov-fail-under=80         # 80% coverage gate (NFR-005 AC-1)
pytest tests/infra                          # CDK synth/assertion tests (needs Node 22 LTS)
```

The 80% line-coverage threshold (NFR-005 AC-1) is configured in `pyproject.toml`
(`[tool.coverage.report] fail_under = 80`) and enforced by CI against the full backend suite.

## Frontend

See `frontend/README.md`. Requires Node.js 22 LTS (matches CI). `npm ci` then `npm run dev`;
set `VITE_API_BASE_URL` to point at the API (nothing is hardcoded).

## Infrastructure

See `infra/README.md`. The CDK app synthesizes CloudFormation without deploying. The
jsii/CDK toolchain requires Node.js 22 LTS.

## Deferred internal integrations

The following depend on internal systems and remain **INTERNAL-INTEGRATION-TODO**. They are not
implemented, deployed, or fabricated in this repository:

- **Authentication (Midway, DEP-002):** production identity provider. Development uses a
  replaceable synthetic auth abstraction only.
- **Ingestion schema (RATS, DEP-001/DEP-007):** upstream workload file format.
- **Frontend hosting (Harmony Console, DEP-006).**
- **CI/CD deploy tooling (DEP-005) and event retention policy (OQ-007).**
