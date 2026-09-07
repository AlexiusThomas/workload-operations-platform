# Workload Operations Platform (WOP) — V1

The Workload Operations Platform is an internal web application that centralizes the management
of operational workload from intake through verified completion. V1 delivers the core
workflow: ingestion, assignment, execution, material runner workflow, verification, and final
completion.

This repository is at the **project-scaffolding stage (Task 1)**. Only the directory layout,
Python packaging, and tooling configuration exist. Business logic, handlers, infrastructure,
and the frontend are implemented in later tasks.

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
├── backend/            # Python AWS Lambda source, domain layer, shared libraries
│   ├── domain/         # Pure domain logic (state machine, quantity, aggregate state) — Task 2
│   ├── auth/           # Authentication/authorization abstraction — Task 5
│   ├── idempotency/    # Idempotency layer — Task 6
│   ├── data/           # DynamoDB data access layer — Task 7
│   ├── handlers/       # Lambda handlers (ingestion, workunit, material, ...) — Tasks 9-16
│   ├── observability/  # Structured logging, metrics, error handling — Task 3
│   └── synthetic/      # Synthetic data generator (prod-guarded) — Task 8
├── infra/              # AWS CDK (Python) IaC — real stacks in Task 20
├── frontend/           # React + TypeScript SPA — scaffolded in Task 22.1
├── tests/              # Automated tests
│   ├── unit/           # Unit tests
│   ├── property/       # Property-based tests (Hypothesis)
│   ├── integration/    # Integration tests (moto DynamoDB)
│   ├── concurrency/    # Atomic-claim concurrency tests
│   └── e2e/            # End-to-end synthetic workflow test
├── pyproject.toml      # Packaging, pytest, coverage, black, mypy config
├── requirements.txt    # Dependency list (mirror of pyproject)
└── .flake8             # flake8 config (consistent with black)
```

## Getting started

Create a virtual environment and install dependencies (runtime + dev tooling):

```bash
python -m venv .venv
# Windows PowerShell:  .venv\Scripts\Activate.ps1
# Linux/macOS:         source .venv/bin/activate

pip install -e ".[dev]"       # or: pip install -r requirements.txt
```

## Lint, type-check, and test

```bash
black --check backend infra          # formatting
flake8 backend infra                 # linting
mypy backend infra                   # static type checking
pytest                               # run tests (zero tests at Task 1 — success)
pytest --cov=backend --cov-fail-under=80   # coverage gate (NFR-005 AC-1), once source exists
```

The 80% line-coverage threshold (NFR-005 AC-1) is configured in `pyproject.toml`
(`[tool.coverage.report] fail_under = 80`) and enforced by CI against real source code.

## Deferred internal integrations

The following depend on internal systems and remain **INTERNAL-INTEGRATION-TODO**. They are not
implemented or fabricated in this repository:

- **Authentication (Midway, DEP-002):** production identity provider. Development uses a
  replaceable synthetic auth abstraction (Task 5).
- **Ingestion schema (RATS, DEP-001/DEP-007):** upstream workload file format.
- **Frontend hosting (Harmony Console, DEP-006).**
- **CI/CD tooling (DEP-005) and event retention policy (OQ-007).**
