# Workload Operations Platform (WOP) - V1

The Workload Operations Platform is an internal web application that centralizes the management
of operational workload from intake through verified completion. V1 delivers the core
workflow: ingestion, assignment, execution, material runner workflow, verification, and final
completion.

## Project status

**Completed through Task 23** (the final checkpoint). The Workload Operations Platform now
includes:

- **Python backend** - AWS Lambda handlers and a pure domain layer.
- **DynamoDB persistence** - multi-table data access layer (main, audit, events, idempotency).
- **RBAC / auth boundary** - a replaceable authentication abstraction and server-side RBAC.
- **Idempotency** - keyed, fingerprinted idempotent writes for all mutating operations.
- **Work package / work unit workflows** - full state machine from claim through completion.
- **Material workflow** - material-requirement claim and delivery.
- **Rollover** - scheduled, exactly-once per-cycle rollover of eligible work.
- **Weekly reporting** - deterministic reports derived from immutable events.
- **Immutable audit / events** - append-only AuditEvents, ProductionEvents, MaterialEvents.
- **Observability** - structured logging, custom metrics, and error handling.
- **AWS CDK infrastructure** - data, compute, and monitoring stacks (Python CDK).
- **React frontend** - React 18 + TypeScript (Vite) SPA consuming the `/v1/` API.
- **Role-based tutorial** - a replayable, role-adaptive in-app Help/Tutorial.
- **CI validation** - a pipeline-agnostic definition plus a GitHub Actions workflow (lint,
  backend tests, infra synth tests, frontend tests, build).

The backend test suite enforces a **>=80% coverage gate**. All validation is deploy-free:
nothing is deployed to AWS, no AWS credentials are used, and no production deployment has
occurred. DEP-001 (RATS ingestion) and DEP-002 (Midway authentication) remain documented,
unresolved external integration boundaries (fail-closed / not fabricated).

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
