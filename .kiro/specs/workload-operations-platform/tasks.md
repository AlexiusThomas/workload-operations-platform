# Implementation Plan: Workload Operations Platform (WOP) V1

## Overview

This plan converts the WOP design into incremental, code-generation-ready tasks. Work proceeds bottom-up: project scaffolding and pure domain logic first (with unit and property-based tests), then the authentication abstraction, the idempotency layer, and the DynamoDB data access layer, then the Lambda handlers in workflow order (ingestion â†’ workpackage â†’ workunit â†’ material â†’ audit-query â†’ rollover â†’ report), then concurrency and end-to-end tests, then CDK IaC and CI, and finally the React + TypeScript frontend.

Backend is Python + AWS Lambda; datastore is DynamoDB (multi-table); ingestion/archive use S3; scheduling uses EventBridge; observability uses CloudWatch + X-Ray; IaC is AWS CDK (Python); frontend is React + TypeScript. No ECS, RDS, Redis, Kubernetes, or Step Functions are introduced (CON-001, CON-002).

Property-based tests use Hypothesis with `@settings(max_examples=200)` (â‰¥100 required by NFR-005 AC-4). Each of Correctness Properties 1â€“13 is covered by an explicit tagged property test task.

## Tasks

- [x] 1. Set up project structure, tooling, and shared scaffolding
  - Create the repository layout: `backend/` (Python Lambda source, domain layer, shared libs), `infra/` (CDK Python), `frontend/` (React + TypeScript), `tests/` (unit, property, integration, concurrency, e2e)
  - Add Python packaging (`pyproject.toml`/`requirements.txt`) with dependencies: boto3, jsonschema, hypothesis, pytest, pytest-cov, moto[dynamodb], flake8, black, mypy
  - Configure pytest, coverage threshold (80% line coverage per NFR-005 AC-1), and lint/type-check config
  - Add placeholder module packages for `domain`, `auth`, `idempotency`, `data`, `handlers`, `observability`, `synthetic`
  - _Requirements: NFR-005 AC-1, NFR-006 AC-3, CON-001, CON-005_

- [x] 2. Implement the pure domain layer
  - [x] 2.1 Implement the WorkUnit state machine (`domain/state_machine.py`)
    - Define the complete valid-transition table and `apply_transition(current_state, transition) -> TransitionResult` returning `is_valid`, `new_state`, `error_code` (pure function, no I/O)
    - Reject all transitions not in the table with `INVALID_STATE_TRANSITION`
    - _Requirements: FR-021 AC-1, FR-021 AC-2, BR-009_

  - [x] 2.2 Write property test for state machine transition validity
    - **Property 2: State Machine Transition Validity**
    - Generate all (current_state, transition) pairs; assert valid iff in the transition table, else `INVALID_STATE_TRANSITION` and state unchanged
    - **Validates: Requirements FR-021 AC-1, FR-021 AC-2, BR-009**

  - [x] 2.3 Implement quantity validation (`domain/quantity.py`)
    - `validate_quantity_update(required_qty, new_completed_qty)` enforcing `0 <= completed_qty <= required_qty` and `required_qty > 0`; return `QUANTITY_EXCEEDED` when exceeded
    - _Requirements: FR-002 AC-3, FR-009 AC-2, FR-009 AC-3, BR-002_

  - [x] 2.4 Write property tests for quantity invariants
    - **Property 1: WorkUnit Quantity Invariant** and **Property 11: Quantity Update Rejection Above Required**
    - Generate required_qty and completed_qty ranges; assert acceptance/rejection and that completed_qty is unchanged on rejection
    - **Validates: Requirements FR-002 AC-3, FR-009 AC-2, FR-009 AC-3, BR-002, BR-003**

  - [x] 2.5 Implement WorkPackage aggregate state derivation (`domain/aggregate_state.py`)
    - `compute_work_package_state(work_units, material_requirements) -> str` implementing the priority-ordered rules including Priority 4 > Priority 5 and Priority 5a (PARTIALLY_COMPLETE); pure and deterministic
    - _Requirements: FR-021 AC-3_

  - [x] 2.6 Write unit tests for aggregate state derivation examples
    - Cover every row of the Aggregate State Derivation Examples table (all-AVAILABLE, all-COMPLETE, mixed â†’ PARTIALLY_COMPLETE, IN_PROGRESS beats 5a, AWAITING_MATERIAL beats PREP_IN_PROGRESS, no WorkUnits â†’ IMPORTED, etc.)
    - _Requirements: FR-021 AC-3_

  - [x] 2.7 Write property test for aggregate state determinism
    - **Property 13: WorkPackage Aggregate State Determinism**
    - Generate N=1..5 WorkUnits and M=0..3 MaterialRequirements; assert output is a valid derived state, consistent with priority table, and referentially transparent
    - **Validates: Requirements FR-021 AC-3**

  - [x] 2.8 Implement week_key and total_meters helpers (`domain/reporting.py`, `domain/material.py`)
    - `compute_week_key(timestamp_iso)` returning ISO `YYYY-WNN`; `create_material_requirement(...)` computing `total_meters_required = cable_length_m * qty_required` and rejecting non-positive cable_length/qty
    - _Requirements: FR-003 AC-3, FR-003 AC-4, BR-017_

  - [x] 2.9 Write property test for total meters invariant
    - **Property 8: MaterialRequirement Total Meters Invariant**
    - Generate positive cable_length_m and qty_required; assert persisted total equals product and is immutable
    - **Validates: Requirements FR-003 AC-3, BR-017**

- [x] 3. Implement observability and error-handling shared modules
  - [x] 3.1 Implement structured JSON logger (`observability/logger.py`) and metrics helper (`observability/metrics.py`)
    - Emit structured JSON logs with correlation_id, user context, action, entity, duration_ms, http_status; define custom CloudWatch metric emitters (`WOP/Operations` namespace) for state transitions, claim conflicts, ingestion, verification, rollover, report, unhandled errors
    - _Requirements: NFR-004 AC-1, NFR-004 AC-2, NFR-004 AC-4_

  - [x] 3.2 Implement error-handling middleware and standard error responses (`observability/errors.py`, `handlers/middleware.py`)
    - Decorator that maps domain exceptions to the error code reference (401/403/400/404/409/500), emits `wop.unhandled_error` on unhandled exceptions, and never leaks stack traces; authorization errors return no resource information
    - _Requirements: NFR-009 AC-2, SEC-002 AC-3, SEC-006 AC-2_

  - [x] 3.3 Write unit tests for error middleware mapping
    - Assert each exception type maps to the correct HTTP status and error code, and stack traces are never included
    - _Requirements: SEC-006 AC-2, NFR-009 AC-2_

- [x] 4. Implement JSON schema input validation
  - [x] 4.1 Define request JSON schemas and a validator (`handlers/validation.py`)
    - Schemas for WorkPackage create, quantity update, tote-assign, fail-verify, material create, material deliver, assignment, report request; validate before business logic; return 400 `VALIDATION_ERROR` with field detail; pass all string inputs as DynamoDB expression attribute values (no interpolation)
    - _Requirements: SEC-006 AC-1, SEC-006 AC-2, SEC-006 AC-3, FR-002 AC-4_

  - [x] 4.2 Write property test for mandatory attribute validation
    - **Property 10: Mandatory Attribute Validation**
    - Generate all 15 non-empty subsets of mandatory WorkPackage attributes; assert any subset missing a field is rejected and no record is created
    - **Validates: Requirements FR-002 AC-4, SEC-006 AC-1**

- [x] 5. Implement the authentication abstraction
  - [x] 5.1 Implement AuthProvider interface, UserContext, and SyntheticAuthProvider (`auth/provider.py`)
    - Define `AuthProvider` protocol, `UserContext(user_id, role, display_name)`, and `SyntheticAuthProvider` with the static synthetic user registry; `authenticate()` raises `AuthenticationError` on missing/invalid token
    - _Requirements: SEC-001 AC-1, SEC-001 AC-2, FR-018 AC-1, FR-018 AC-2, FR-023 AC-4_

  - [x] 5.2 Implement MidwayAuthProvider stub and fail-closed provider factory
    - `MidwayAuthProvider` raising `NotImplementedError` (INTERNAL-INTEGRATION-TODO, DEP-002); `get_auth_provider(mode)`; cold-start logic that raises `AuthConfigurationError` when `ENVIRONMENT=prod` and `AUTH_MODE` is absent or `synthetic`, defaulting to synthetic only in dev/staging
    - _Requirements: SEC-001 AC-3, SEC-001 AC-4, NFR-007 AC-3_

  - [x] 5.3 Implement authorization middleware (`auth/middleware.py`)
    - `authenticate(event)` and `authorize(user_ctx, required_roles)`; RBAC matrix enforcement server-side; ownership checks (start/quantity/prep-complete/label/tote-assign/ready-to-verify owner rules; Lead/Admin bypass; material deliver runner match); identity resolved only from session; log `IDENTITY_MISMATCH_WARNING` when client identity differs
    - _Requirements: FR-018 AC-3, FR-018 AC-4, FR-019 AC-1, FR-019 AC-3, FR-019 AC-4, SEC-002 AC-1, SEC-002 AC-2, BR-008_

  - [x] 5.4 Write unit tests for auth and RBAC/ownership rules
    - Test synthetic token resolution, fail-closed production behavior, role checks per RBAC matrix, and ownership rule 4 (Technician vs Lead/Admin) for label/tote/ready-to-verify
    - _Requirements: SEC-001 AC-2, SEC-001 AC-4, FR-019 AC-3, SEC-002 AC-2_

- [x] 6. Implement the idempotency layer
  - [x] 6.1 Implement IdempotencyRecord model and check/store logic (`idempotency/layer.py`)
    - `check_idempotency(key)` and `store_idempotency_result(key, status_code, response_body)`; lock via PutItem with `attribute_not_exists(pk)`; compute SHA-256 `request_fingerprint` over `operation + resource_id + canonical_request_body`; store `resource_id`, `status`, `ttl >= now+86400`
    - _Requirements: FR-020 AC-1, FR-020 AC-2, FR-020 AC-3, FR-020 AC-4_

  - [x] 6.2 Implement fingerprint conflict and IN_FLIGHT crash recovery
    - On COMPLETED with matching fingerprint replay stored response; on mismatch return 409 IDEMPOTENCY_CONFLICT; implement 30-second staleness recovery: re-check with back-off, then GetItem the entity, mark COMPLETED if state matches expected, else delete orphaned IN_FLIGHT record and re-execute (only permitted delete)
    - _Requirements: FR-020 AC-4_

  - [x] 6.3 Write property test for idempotency key deduplication
    - **Property 4: Idempotency Key Deduplication**
    - Send same request twice with same key; assert identical status/body and that event counts do not increase on the second call
    - **Validates: Requirements FR-020 AC-2, FR-020 AC-5, FR-020 AC-6**

- [x] 7. Implement the DynamoDB data access layer
  - [x] 7.1 Implement table clients and config loading (`data/tables.py`, `data/config.py`)
    - Resolve table/bucket names from SSM/environment variables at cold start; fail with descriptive error if a required config value is absent; clients for wop-main-table, wop-audit-table, wop-events-table, wop-idempotency-table
    - _Requirements: NFR-007 AC-1, NFR-007 AC-2, NFR-007 AC-3_

  - [x] 7.2 Implement repositories with denormalized dual-write via TransactWriteItems (`data/repositories.py`)
    - WorkPackage/WorkUnit/MaterialRequirement read (canonical + projection), and transactional writes that always include canonical update + projection update + AuditEvent Put (adding ProductionEvent/MaterialEvent as a 4th item where applicable); AuditEvent PK/SK layout and chronological SK
    - _Requirements: FR-017 AC-1, FR-017 AC-5, NFR-009 AC-1, BR-003_

  - [x] 7.3 Implement GSI-backed query access patterns
    - Implement access patterns: queue by state/site/work_type/date (GSI-1), children by WorkPackage (GSI-2), material by status (GSI-3), event queries by week/technician/runner (GSI-4..7), stale active work (GSI-1 per non-terminal state), audit by entity; native LastEvaluatedKey pagination capped at 100 items
    - _Requirements: FR-005 AC-1, FR-005 AC-2, FR-005 AC-5, FR-017 AC-3, FR-022 AC-7, NFR-010 AC-1_

  - [x] 7.4 Write integration tests for repositories against moto DynamoDB
    - Verify dual-write consistency, audit write atomicity with state change, and pagination cursor behavior
    - _Requirements: FR-017 AC-5, NFR-005 AC-2_

- [x] 8. Implement the synthetic data generator with production guard
  - [x] 8.1 Implement factory functions and parameterized dataset generation (`synthetic/generator.py`)
    - `make_work_package`, `make_work_unit`, `make_material_requirement`, `generate_dataset(work_package_count, units_per_package, work_types)` using only synthetic identifiers (SITE-A, RACK-001, TECH-001, RUNNER-001, VERIFY-001); raise `RuntimeError` when `ENVIRONMENT=prod`
    - _Requirements: FR-023 AC-1, FR-023 AC-2, FR-023 AC-3, FR-023 AC-4, CON-003, RISK-006_

  - [x] 8.2 Write unit test for production environment guard
    - Assert `generate_dataset` raises when `ENVIRONMENT=prod` and succeeds otherwise
    - _Requirements: FR-023 AC-3_

- [x] 9. Implement the ingestion handler (two-phase staging/commit)
  - [x] 9.1 Implement `wop-ingestion-handler` (`handlers/ingestion.py`)
    - S3-triggered; schema-validate file (DEP-001 INTERNAL-INTEGRATION-TODO); Phase 1 stage WorkPackage `INGESTING` + WorkUnits `STAGING` in TransactWriteItems batches (â‰¤25); Phase 2 commit WorkUnits to AVAILABLE then set `committed=true` on WorkPackage as the visibility gate; queue handler suppresses `committed=false`; move file to archive/error prefix; emit staging/commit/failure AuditEvents; idempotency key = S3 object key; duplicate WorkPackage IDs are no-ops; no partial AVAILABLE on failure
    - _Requirements: FR-001 AC-1..AC-7, FR-002 AC-1, FR-002 AC-2, FR-002 AC-5, FR-002 AC-6, FR-020 AC-7, NFR-009 AC-4_

  - [x] 9.2 Write property test for ingestion idempotency
    - **Property 3: Ingestion Idempotency**
    - Ingest same payload N times; assert identical WorkPackage/WorkUnit counts and attributes as one ingestion
    - **Validates: Requirements FR-001 AC-5, FR-020 AC-7**

  - [x] 9.3 Write integration test for ingestion staging/commit and error path
    - Verify staged units are invisible to the queue until commit; verify failure leaves INGESTION_ERROR and moves file to error prefix
    - _Requirements: FR-001 AC-4, FR-001 AC-7_

- [x] 10. Implement the workpackage handler
  - [x] 10.1 Implement `wop-workpackage-handler` (`handlers/workpackage.py`)
    - `GET /v1/work-packages/{id}` with derived aggregate state, `GET /v1/work-packages` (filters + pagination), `POST /v1/work-packages` (ADMIN, idempotent) creating WorkPackage and optional WorkUnits with one AuditEvent per created entity
    - _Requirements: FR-002 AC-1..AC-6, FR-021 AC-3, FR-019 AC-2_

  - [x] 10.2 Write integration tests for workpackage endpoints
    - Test creation validation (missing mandatory attribute â†’ 400), derived-state response, and pagination
    - _Requirements: FR-002 AC-4, FR-021 AC-3_

- [x] 11. Implement the workunit handler (all state transitions)
  - [x] 11.1 Implement claim and exception assignment routes
    - `POST /claim` (AVAILABLEâ†’CLAIMED and REWORK_REQUIREDâ†’IN_PROGRESS rework-claim preserving completed_qty/history) and `POST /assign` (Lead/Admin, AVAILABLE/CLAIMED) via TransactWriteItems (canonical + projection + AuditEvent), conditional write returning 409 CLAIM_CONFLICT on race; identity from session
    - _Requirements: FR-006 AC-1..AC-7, FR-007 AC-1..AC-4, FR-015 AC-3, FR-015 AC-5, BR-001, BR-010, BR-013_

  - [x] 11.2 Implement start, quantity update, and prep-complete routes
    - `POST /start` (owner), `PATCH /quantity` (owner, conditional `new_qty <= required_qty`), `POST /prep-complete` (owner, requires completed_qty == required_qty); each writes AuditEvent atomically
    - _Requirements: FR-008 AC-1..AC-4, FR-009 AC-1..AC-4, FR-010 AC-1..AC-4, BR-002, BR-003_

  - [x] 11.3 Implement label, tote-assign, ready-to-verify routes
    - `POST /label`, `POST /tote-assign` (non-empty tote_id required), `POST /ready-to-verify` (tote_id must be recorded); Technician ownership vs Lead/Admin bypass per RBAC; AuditEvents include tote_id metadata
    - _Requirements: FR-011 AC-1..AC-3, FR-012 AC-1..AC-5, FR-013 AC-1..AC-3, BR-004_

  - [x] 11.4 Implement verify, fail-verify, and complete routes
    - `POST /verify` (Lead/Admin, idempotent) 3-item transaction creating immutable ProductionEvent; `POST /fail-verify` (non-empty failure_reason, increments rework_count); `POST /complete` (VERIFIEDâ†’COMPLETE, excluded from queue); `GET /v1/work-units` and `/{id}` and `/stale`
    - _Requirements: FR-014 AC-1..AC-5, FR-015 AC-1, FR-015 AC-2, FR-015 AC-4, FR-016 AC-1..AC-4, FR-005 AC-1..AC-5, FR-022 AC-7, BR-005, BR-006, BR-007_

  - [x] 11.5 Write property test for ProductionEvents created only on verification
    - **Property 12: ProductionEvents Created Only on Verification**
    - Generate transition sequences ending before VERIFIED; assert zero ProductionEvents exist
    - **Validates: Requirements FR-014 AC-5, BR-007**

  - [x] 11.6 Write property test for audit log chronological order and completeness
    - **Property 9: Audit Log Chronological Order and Completeness**
    - Perform N transitions; assert exactly N AuditEvents returned in ascending timestamp order with no gaps
    - **Validates: Requirements FR-017 AC-3, FR-017 AC-4**

- [x] 12. Checkpoint - Ensure all tests pass
  - Ensure all unit, property, and integration tests for the domain layer, auth, idempotency, data layer, and workunit/workpackage/ingestion handlers pass, ask the user if questions arise.

- [x] 13. Implement the material handler
  - [x] 13.1 Implement material CRUD and material-claim
    - `GET`/`POST` MaterialRequirements, `POST /material-claim` (RUNNER/ADMIN) as 3-item TransactWriteItems (canonical status + projection + AuditEvent) with conditional write returning 409 MATERIAL_CLAIM_CONFLICT; identity from session
    - _Requirements: FR-003 AC-1..AC-6, FR-004 AC-1, FR-004 AC-2, FR-004 AC-3, FR-004 AC-8, FR-004 AC-9, FR-004 AC-10_

  - [x] 13.2 Implement material-deliver with MaterialEvent and auto READY_FOR_PREP
    - `POST /material-deliver` (idempotent) transitioning to MATERIAL_DELIVERED, persisting qty/meters/runner, emitting AuditEvent + immutable MaterialEvent; auto-evaluate READY_FOR_PREP when all requirements delivered; runner ownership match
    - _Requirements: FR-003 AC-7, FR-003 AC-8, FR-004 AC-4, FR-004 AC-5, FR-004 AC-6, FR-004 AC-7, FR-020 AC-6, BR-011, BR-012_

- [x] 14. Implement the audit-query handler
  - [x] 14.1 Implement `wop-audit-query-handler` (`handlers/audit_query.py`)
    - Read-only `GET /v1/audit/{entity_type}/{entity_id}` returning AuditEvents in chronological order with pagination; Lead/Admin only; no write permissions
    - _Requirements: FR-017 AC-2, FR-017 AC-3, FR-017 AC-4_

- [ ] 15. Implement the rollover handler
  - [ ] 15.1 Implement `wop-rollover-handler` (`handlers/rollover.py`)
    - EventBridge-triggered; query AVAILABLE units (GSI-1); per unit 3-item TransactWriteItems (canonical date advance + rollover_count increment, projection update, WORK_UNIT_ROLLED_OVER AuditEvent) with conditional guard on state=AVAILABLE and current_scheduled_date; rollover_cycle_key idempotency; skip silently on cancellation; preserve completed_qty and original_scheduled_date
    - _Requirements: FR-022 AC-1..AC-6, FR-022 AC-8, FR-020 AC-8, BR-014, BR-015, RISK-007_

  - [ ] 15.2 Write property tests for rollover eligibility, preservation, and idempotency
    - **Property 5: Rollover Eligibility Invariant**, **Property 6: Rollover Data Preservation**, **Property 7: Rollover Idempotency**
    - Assert non-AVAILABLE units unchanged; AVAILABLE units advance date/increment count once preserving qty and original date; N executions per cycle equal one
    - **Validates: Requirements FR-022 AC-2, FR-022 AC-3, FR-022 AC-4, FR-022 AC-5, FR-022 AC-6, FR-022 AC-8, FR-020 AC-8, BR-014, BR-015**

- [ ] 16. Implement the report handler
  - [ ] 16.1 Implement `wop-report-handler` (`handlers/report.py`)
    - `GET`/`POST /v1/reports/weekly` and EventBridge weekly schedule; aggregate verified units by work_type/site, meters by material type, verified qty by Technician, runs/meters by Runner; derive exclusively from ProductionEvents and MaterialEvents; separate Technician and Runner sections (never combined); deterministic given same events
    - _Requirements: FR-024 AC-1..AC-5, BR-011, BR-016, NFR-010 AC-3, NFR-010 AC-5_

  - [ ] 16.2 Write integration test for report reconciliation
    - Seed ProductionEvents/MaterialEvents; assert report totals reconcile exactly and no combined metric appears
    - _Requirements: FR-024 AC-2, FR-024 AC-4, BR-016_

- [ ] 17. Concurrency tests for atomic claims
  - [ ] 17.1 Write concurrency test for atomic WorkUnit claim
    - Fire N simultaneous claims on one AVAILABLE WorkUnit against moto DynamoDB; assert exactly one 200 and N-1 409 conflicts, single claimed_by
    - _Requirements: FR-006 AC-2, FR-006 AC-5, NFR-005 AC-3, BR-001_

  - [ ] 17.2 Write concurrency test for atomic MaterialRequirement claim
    - Fire N simultaneous material-claims on one MATERIAL_REQUIRED requirement; assert exactly one success and N-1 conflicts
    - _Requirements: FR-004 AC-2, NFR-005 AC-3_

- [ ] 18. End-to-end synthetic workflow test
  - [ ] 18.1 Write the full synthetic end-to-end workflow test
    - Cover ingestion â†’ material requirement calculation â†’ material claim/delivery â†’ technician claim â†’ partial work/quantity update â†’ prep completion â†’ labeling â†’ tote assignment â†’ ready-to-verify â†’ fail-verify/rework (qty preserved) â†’ re-progress â†’ verification (ProductionEvent) â†’ completion â†’ weekly report â†’ eligible next-day rollover; assert report reconciles exactly to immutable ProductionEvents and MaterialEvents
    - _Requirements: NFR-005 AC-6, FR-024 AC-4, BR-016_

- [ ] 19. Checkpoint - Ensure all backend tests pass
  - Ensure all handler, concurrency, and end-to-end tests pass, ask the user if questions arise.

- [ ] 20. Define AWS CDK infrastructure as code (Python)
  - [ ] 20.1 Implement `wop-data-stack`
    - DynamoDB tables (main, audit, events, idempotency) with GSI-1..7, AWS_OWNED_KMS encryption, PITR (all except idempotency), idempotency TTL attribute; S3 import/archive/error buckets with SSE and public access block; SSM parameters for table/bucket names
    - _Requirements: SEC-005 AC-1, SEC-005 AC-2, SEC-005 AC-3, SEC-007 AC-2, NFR-003 AC-2, NFR-006 AC-1, NFR-007 AC-1, CON-006_

  - [ ] 20.2 Implement `wop-compute-stack` with least-privilege IAM roles
    - Lambda functions, Lambda Layer (idempotency), API Gateway (HTTPS-only, TLS 1.2+, `/v1/` routes), EventBridge rules (rollover, report), environment variables; separate IAM role per Lambda scoped to specific tables/actions and S3 prefixes; audit/events roles get PutItem only (no Update/Delete); idempotency role gets DeleteItem scoped to IN_FLIGHT only; synth-time guard failing when prod `AUTH_MODE` is empty/synthetic; exclude synthetic-data function from prod
    - _Requirements: SEC-001 AC-4, SEC-002 AC-1, SEC-003 AC-1..AC-4, SEC-004 AC-1..AC-3, SEC-007 AC-1, SEC-007 AC-3, NFR-002 AC-2, NFR-003 AC-1, NFR-010 AC-2, FR-023 AC-3, CON-005_

  - [ ] 20.3 Implement `wop-monitoring-stack`
    - CloudWatch Log Groups with SSM-driven retention, custom metric filters, alarms (high error rate, p95 latency, claim conflict spike, ingestion failure, unhandled error), SNS topics, X-Ray active tracing/sampling
    - _Requirements: NFR-001 AC-3, NFR-002 AC-3, NFR-004 AC-3, NFR-004 AC-4, NFR-009 AC-2_

  - [ ] 20.4 Write CDK synth/assertion tests
    - Assert PITR/encryption/public-access-block settings, PutItem-only audit/events roles, and prod auth-mode synth guard failure
    - _Requirements: SEC-005, SEC-007 AC-3, SEC-001 AC-4_

- [ ] 21. Define the CI pipeline as code
  - [ ] 21.1 Implement pipeline definition (`ci/` pipeline-agnostic config)
    - Stages: lint (flake8/black/mypy, ESLint/Prettier, cdk synth), unit + property tests with 80% coverage gate, build (Lambda bundle, vite build, cdk synth), integration/concurrency/e2e tests; block on any failure; environment-agnostic artifacts
    - _Requirements: NFR-005 AC-5, NFR-006 AC-3, NFR-007 AC-2, NFR-008 AC-1, NFR-008 AC-2_

- [ ] 22. Implement the React + TypeScript frontend
  - [ ] 22.1 Scaffold the SPA and API client
    - Vite + React + TypeScript app; typed API client for `/v1/` endpoints with bearer token and Idempotency-Key headers; auth token handling for synthetic users
    - _Requirements: CON-001, SEC-001 AC-2, FR-020 AC-1_

  - [ ] 22.2 Implement the workload queue view
    - List AVAILABLE WorkUnits with filters (site, work_type, current_scheduled_date), pagination (â‰¤100), and required display fields
    - _Requirements: FR-005 AC-1, FR-005 AC-2, FR-005 AC-4, FR-005 AC-5_

  - [ ] 22.3 Implement work tracking views (claim through complete)
    - UI for claim, start, quantity update, prep-complete, label, tote-assign, ready-to-verify, verify, fail-verify, complete, and exception assignment, driven by state and role
    - _Requirements: FR-006, FR-007, FR-008, FR-009, FR-010, FR-011, FR-012, FR-013, FR-014, FR-015, FR-016_

  - [ ] 22.4 Implement material tracking views
    - Material Runner queue (by status), material-claim and material-deliver forms capturing qty/meters
    - _Requirements: FR-003 AC-5, FR-004 AC-1, FR-004 AC-4_

  - [ ] 22.5 Implement audit history and reports views
    - Audit history view (chronological) for Lead/Admin; weekly report view with separate Technician and Material Runner sections
    - _Requirements: FR-017 AC-3, FR-024 AC-1, FR-024 AC-3, BR-016_

  - [ ]* 22.6 Write frontend component/unit tests
    - Test queue rendering, role-gated actions, and report section separation
    - _Requirements: FR-005 AC-4, FR-019 AC-3, BR-016_

- [ ] 23. Final checkpoint - Ensure all tests pass
  - Ensure all backend, IaC, and frontend tests pass, ask the user if questions arise.

## Notes

- Backend correctness-property tests, transactional/atomic and idempotency tests, ingestion staging/commit tests, concurrency tests, the end-to-end synthetic workflow test, and CDK security/infrastructure assertion tests are REQUIRED for WOP V1 production-engineering validation and are not marked optional. Only frontend component/unit tests (task 22.6) remain marked with `*` as optional and skippable for a faster MVP.
- Each task references specific requirement IDs and/or design correctness properties for traceability.
- All Correctness Properties 1â€“13 are covered by explicit property-based test tasks (2.2, 2.4, 2.7, 2.9, 4.2, 6.3, 9.2, 11.5, 11.6, 15.2).
- INTERNAL-INTEGRATION-TODO items (RATS ingestion schema, Midway auth, retention) are honored: the ingestion schema and Midway provider are stubbed/tagged rather than fabricated.
- Property tests use Hypothesis with â‰¥200 examples (NFR-005 AC-4).

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["2.1", "2.3", "2.5", "2.8", "3.1", "8.1"] },
    { "id": 2, "tasks": ["2.2", "2.4", "2.6", "2.7", "2.9", "3.2", "4.1", "5.1", "8.2"] },
    { "id": 3, "tasks": ["3.3", "4.2", "5.2", "5.3", "6.1", "7.1"] },
    { "id": 4, "tasks": ["5.4", "6.2", "7.2"] },
    { "id": 5, "tasks": ["6.3", "7.3", "9.1"] },
    { "id": 6, "tasks": ["7.4", "9.2", "9.3", "10.1"] },
    { "id": 7, "tasks": ["10.2", "11.1"] },
    { "id": 8, "tasks": ["11.2", "11.3"] },
    { "id": 9, "tasks": ["11.4"] },
    { "id": 10, "tasks": ["11.5", "11.6", "13.1", "14.1", "15.1", "16.1"] },
    { "id": 11, "tasks": ["13.2", "15.2", "16.2", "17.1", "17.2"] },
    { "id": 12, "tasks": ["18.1", "20.1"] },
    { "id": 13, "tasks": ["20.2", "20.3"] },
    { "id": 14, "tasks": ["20.4", "21.1", "22.1"] },
    { "id": 15, "tasks": ["22.2", "22.3", "22.4"] },
    { "id": 16, "tasks": ["22.5"] },
    { "id": 17, "tasks": ["22.6"] }
  ]
}
```
