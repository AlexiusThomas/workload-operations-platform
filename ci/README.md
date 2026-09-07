# WOP CI pipeline (Task 21)

This directory holds the **pipeline-agnostic** CI definition for the Workload
Operations Platform.

## Files
- `pipeline.yml` — tool-neutral source of truth: stages, order, gates, runtimes.
- `../.github/workflows/ci.yml` — concrete runnable reference (GitHub Actions) that
  mirrors `pipeline.yml` exactly.

## Why two files?
The approved internal CI/CD tool is unconfirmed (DEP-005, OQ-009/OQ-010 —
INTERNAL-INTEGRATION-TODO). `pipeline.yml` captures the intended pipeline in a form
that translates 1:1 into whatever tool is selected; the GitHub Actions workflow gives
an immediately runnable implementation for this repository.

## Stages (fail-fast; any failure blocks the pipeline)
1. **lint** — black --check, flake8, mypy (frontend ESLint/Prettier conditional until Task 22).
2. **unit_and_property_tests** — pytest unit + property with the 80% coverage gate (NFR-005 AC-1).
3. **infra_tests** — Task 20 CDK synth/assertion tests (deploy-free).
4. **build** — Lambda source package + deploy-free `cdk synth`; frontend `vite build` conditional.
5. **integration_concurrency_e2e** — moto-backed integration, concurrency, and E2E synthetic tests.

## Guarantees
- **Reproducible installs**: `pip install -r requirements.txt` (pinned); `npm ci` for frontend.
- **No real AWS credentials**: DynamoDB/S3 are mocked with moto; `cdk synth` is deploy-free.
- **No secrets hardcoded**: environment configuration is injected at deploy time.
- **Node LTS**: Node 22.x is used for the jsii/CDK toolchain (Node 24.x is incompatible with
  the pinned jsii runtime and must not be used).
- **No deploy in CI**: deployment to AWS is out of scope and gated behind DEP-005.
- **No committed build output**: `dist_artifacts/`, `cdk.out/`, and caches are git-ignored.
