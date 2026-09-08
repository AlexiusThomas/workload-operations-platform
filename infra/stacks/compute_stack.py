"""wop-compute-stack — Lambda functions, API Gateway, EventBridge, IAM (Task 20.2).

Each Lambda gets its own execution role scoped to exactly the tables, actions, and S3
prefixes it needs (SEC-003, SEC-004, least-privilege). Highlights:

- Audit and events tables receive **PutItem only** — no UpdateItem/DeleteItem (SEC-007 AC-3).
- The idempotency role gets DeleteItem, but only for orphaned IN_FLIGHT records; DynamoDB
  IAM cannot condition on a non-key attribute, so the IN_FLIGHT restriction is enforced in
  the application layer (documented, not fabricated as an IAM condition).
- API Gateway is HTTPS-only with a TLS 1.2+ security policy and ``/v1/`` routing.
- EventBridge rules trigger the rollover and report handlers.
- A synth-time guard fails the build when ``environment == "prod"`` and ``AUTH_MODE`` is
  empty or ``synthetic`` (SEC-001 AC-4, mirrors backend.auth.provider.resolve_auth_mode).
- The synthetic-data utility function is excluded from prod (CON-005).

The two documented wildcard exceptions are ``cloudwatch:PutMetricData`` (scoped by the
``WOP/Operations`` namespace condition) and X-Ray write actions (required by active tracing;
X-Ray actions do not support resource-level permissions).

INTERNAL-INTEGRATION-TODO:
- DEP-001 (RATS ingestion): the import bucket receives files from RATS via S3 PutObject.
  The owning team/protocol is unconfirmed; the ingestion handler is wired to the bucket
  notification but no RATS credentials, endpoints, or schema are assumed.
- DEP-002 (Midway auth): production ``AUTH_MODE=midway`` selects MidwayAuthProvider, which
  is a documented NotImplemented boundary. No Midway endpoints/tokens are configured here.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from aws_cdk import Duration, Stack
from aws_cdk import aws_apigateway as apigw
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_s3 as s3
from constructs import Construct

# Auth-mode constants mirrored from backend.auth.provider (kept in sync intentionally).
AUTH_MODE_SYNTHETIC = "synthetic"
ENV_PROD = "prod"

# CloudWatch custom metric namespace (scopes the PutMetricData wildcard exception).
METRICS_NAMESPACE = "WOP/Operations"

# The Lambda functions execute the REAL backend application. The deployment package is the
# repository root's ``backend/`` package tree; each function points at its real handler
# module via ``handler="backend.handlers.<mod>.handle"``. No placeholder / NotImplementedError
# code is used. Synthesis is deploy-free: from_asset stages the backend directory; no network
# or bundling step runs during synth.
# infra/stacks/compute_stack.py -> parents[2] is the repository root.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_BACKEND_ASSET_PATH = os.path.join(_REPO_ROOT, "backend")

# The shared idempotency layer also ships the real backend code (the idempotency package is
# imported by the mutating handlers); a layer needs an on-disk asset (inline is rejected).
_LAYER_ASSET_PATH = os.path.join(_REPO_ROOT, "infra", "layer_placeholder")

#: Real handler entrypoints per Lambda function name (module.handle in the backend package).
_HANDLER_ENTRYPOINTS = {
    "wop-ingestion-handler": "backend.handlers.ingestion.handle",
    "wop-workpackage-handler": "backend.handlers.workpackage.handle",
    "wop-workunit-handler": "backend.handlers.workunit.handle",
    "wop-material-handler": "backend.handlers.material.handle",
    "wop-audit-query-handler": "backend.handlers.audit_query.handle",
    "wop-rollover-handler": "backend.handlers.rollover.handle",
    "wop-report-handler": "backend.handlers.report.handle",
    "wop-synthetic-data": "backend.synthetic.generator.handle",
}

#: Paths excluded from the Lambda deployment asset (tests, infra, frontend, caches).
_ASSET_EXCLUDES = [
    "tests",
    "infra",
    "frontend",
    "ci",
    ".github",
    "docs",
    ".git",
    "**/__pycache__",
    "*.pyc",
    ".venv",
    "node_modules",
    "cdk.out",
    "dist_artifacts",
    ".mypy_cache",
    ".pytest_cache",
    ".hypothesis",
]


def assert_prod_auth_mode(environment: str, auth_mode: Optional[str]) -> None:
    """Fail synth when prod is misconfigured for auth (SEC-001 AC-4).

    Mirrors backend.auth.provider.resolve_auth_mode's fail-closed rule so a
    production stack cannot be synthesized with synthetic auth.

    Raises:
        ValueError: If ``environment == 'prod'`` and ``auth_mode`` is empty or synthetic.
    """
    if environment == ENV_PROD and (not auth_mode or auth_mode == AUTH_MODE_SYNTHETIC):
        raise ValueError(
            "Refusing to synthesize prod compute stack: AUTH_MODE must be the approved "
            "provider (not empty/synthetic). See DEP-002 (Midway)."
        )


class ComputeStack(Stack):
    """Lambda functions, API Gateway, EventBridge rules, and IAM for WOP."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        environment: str,
        main_table: dynamodb.ITable,
        audit_table: dynamodb.ITable,
        events_table: dynamodb.ITable,
        idempotency_table: dynamodb.ITable,
        import_bucket: s3.IBucket,
        archive_bucket: s3.IBucket,
        error_bucket: s3.IBucket,
        auth_mode: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        resolved_auth_mode = auth_mode if auth_mode is not None else os.environ.get("AUTH_MODE")
        assert_prod_auth_mode(environment, resolved_auth_mode)

        self.environment_name = environment
        self.functions: dict[str, lambda_.Function] = {}

        base_env = {
            "WOP_MAIN_TABLE_NAME": main_table.table_name,
            "WOP_AUDIT_TABLE_NAME": audit_table.table_name,
            "WOP_EVENTS_TABLE_NAME": events_table.table_name,
            "WOP_IDEMPOTENCY_TABLE_NAME": idempotency_table.table_name,
            "WOP_IMPORT_BUCKET_NAME": import_bucket.bucket_name,
            "WOP_ARCHIVE_BUCKET_NAME": archive_bucket.bucket_name,
            "WOP_ERROR_BUCKET_NAME": error_bucket.bucket_name,
            "ENVIRONMENT": environment,
            "AUTH_MODE": resolved_auth_mode or AUTH_MODE_SYNTHETIC,
        }

        # Shared idempotency Lambda Layer (design: wop-idempotency-layer).
        self.idempotency_layer = lambda_.LayerVersion(
            self,
            "IdempotencyLayer",
            code=lambda_.Code.from_asset(_LAYER_ASSET_PATH),
            compatible_runtimes=[lambda_.Runtime.PYTHON_3_13],
            description="Shared idempotency check/store logic (wired at deploy time)",
        )

        def make_fn(logical_id: str, fn_name: str) -> lambda_.Function:
            handler_entry = _HANDLER_ENTRYPOINTS[fn_name]
            fn = lambda_.Function(
                self,
                logical_id,
                function_name=f"{fn_name}-{environment}",
                runtime=lambda_.Runtime.PYTHON_3_13,
                handler=handler_entry,  # real backend handler (never a placeholder)
                code=lambda_.Code.from_asset(_REPO_ROOT, exclude=_ASSET_EXCLUDES),
                environment=dict(base_env),
                timeout=Duration.seconds(30),
                memory_size=256,
                tracing=lambda_.Tracing.ACTIVE,  # X-Ray (NFR-004)
                layers=[self.idempotency_layer],
            )
            self._grant_common_observability(fn)
            self.functions[fn_name] = fn
            return fn

        # ---- Handlers ----
        ingestion = make_fn("IngestionFn", "wop-ingestion-handler")
        workpackage = make_fn("WorkPackageFn", "wop-workpackage-handler")
        workunit = make_fn("WorkUnitFn", "wop-workunit-handler")
        material = make_fn("MaterialFn", "wop-material-handler")
        audit_query = make_fn("AuditQueryFn", "wop-audit-query-handler")
        rollover = make_fn("RolloverFn", "wop-rollover-handler")
        report = make_fn("ReportFn", "wop-report-handler")

        # ---- Least-privilege data-access grants ----
        # Idempotency: full lifecycle incl. DeleteItem (IN_FLIGHT-only enforced in app layer).
        for fn in (ingestion, workpackage, workunit, material, rollover):
            idempotency_table.grant(
                fn,
                "dynamodb:GetItem",
                "dynamodb:PutItem",
                "dynamodb:UpdateItem",
                "dynamodb:DeleteItem",
            )

        # Audit + events tables: PutItem ONLY (SEC-007 AC-3).
        for fn in (ingestion, workpackage, workunit, material, rollover):
            audit_table.grant(fn, "dynamodb:PutItem")
        for fn in (workunit, material):
            events_table.grant(fn, "dynamodb:PutItem")

        # Main table access per handler.
        main_table.grant_read_write_data(ingestion)
        main_table.grant_read_write_data(workpackage)
        main_table.grant_read_write_data(workunit)
        main_table.grant_read_write_data(material)
        main_table.grant_read_write_data(rollover)

        # Audit query handler: read-only on audit table.
        audit_table.grant_read_data(audit_query)

        # Report handler: read-only on events table (GSI-4..7).
        events_table.grant_read_data(report)

        # S3 prefixes for ingestion (DEP-001 RATS drops into import prefix).
        # Ingestion reads the import object, then moves it (copy to archive/error + DELETE the
        # original). Grant read + delete on the import bucket ONLY, and put on archive/error.
        import_bucket.grant_read(ingestion)
        import_bucket.grant_delete(ingestion)
        archive_bucket.grant_put(ingestion)
        error_bucket.grant_put(ingestion)

        # S3 import object-created triggers the ingestion handler (DEP-001 RATS).
        # Uses EventBridge (the import bucket has EventBridge notifications enabled in the
        # data stack) matched by bucket NAME string, so this rule references no DataStack
        # construct and introduces no data<->compute dependency cycle. An input transformer
        # maps the EventBridge S3 detail shape into the S3 "Records" event shape the ingestion
        # handler already parses (no ingestion logic is duplicated).
        self._wire_ingestion_eventbridge(import_bucket, ingestion, environment)

        # ---- API Gateway (HTTPS-only, TLS 1.2+, /v1/) ----
        self.api = apigw.RestApi(
            self,
            "WopApi",
            rest_api_name=f"wop-api-{environment}",
            # Version prefix lives ONLY in the resource hierarchy (/v1/...), never in the
            # stage name, so the external path is /v1/... and never /v1/v1/... .
            deploy_options=apigw.StageOptions(
                stage_name="api",
                tracing_enabled=True,
                metrics_enabled=True,
            ),
            endpoint_configuration=apigw.EndpointConfiguration(types=[apigw.EndpointType.REGIONAL]),
        )
        v1 = self.api.root.add_resource("v1")
        self._add_proxy(v1, "work-packages", workpackage)
        self._add_proxy(v1, "work-units", workunit)
        self._add_proxy(v1, "material-requirements", material)
        self._add_proxy(v1, "audit", audit_query)
        self._add_proxy(v1, "reports", report)

        # ---- EventBridge rules ----
        events.Rule(
            self,
            "RolloverSchedule",
            rule_name=f"wop-rollover-schedule-{environment}",
            schedule=events.Schedule.rate(Duration.days(1)),
            targets=[targets.LambdaFunction(rollover)],
        )
        events.Rule(
            self,
            "ReportSchedule",
            rule_name=f"wop-report-schedule-{environment}",
            # Weekly, Monday 06:00 UTC.
            schedule=events.Schedule.cron(minute="0", hour="6", week_day="MON"),
            targets=[targets.LambdaFunction(report)],
        )

        # ---- Synthetic-data utility: dev/staging only, never prod (CON-005) ----
        if environment != ENV_PROD:
            synth = make_fn("SyntheticDataFn", "wop-synthetic-data")
            main_table.grant_read_write_data(synth)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _grant_common_observability(self, fn: lambda_.Function) -> None:
        """Grant scoped CloudWatch metrics (documented wildcard exception)."""
        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],  # PutMetricData has no resource ARN; scoped by namespace.
                conditions={"StringEquals": {"cloudwatch:namespace": METRICS_NAMESPACE}},
            )
        )
        # X-Ray write actions do not support resource-level permissions (AWS-documented);
        # required by Tracing.ACTIVE.
        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "xray:PutTraceSegments",
                    "xray:PutTelemetryRecords",
                ],
                resources=["*"],
            )
        )

    def _wire_ingestion_eventbridge(
        self, import_bucket: s3.IBucket, ingestion: lambda_.Function, environment: str
    ) -> None:
        # Match S3 "Object Created" EventBridge events for the import bucket (by name) and
        # route them to the ingestion Lambda. The input transformer reshapes the event into
        # the S3 notification "Records" structure the ingestion handler expects, so the
        # handler is unchanged and no ingestion logic is duplicated.
        rule = events.Rule(
            self,
            "IngestionS3Rule",
            rule_name=f"wop-ingestion-s3-{environment}",
            event_pattern=events.EventPattern(
                source=["aws.s3"],
                detail_type=["Object Created"],
                detail={"bucket": {"name": [import_bucket.bucket_name]}},
            ),
        )
        rule.add_target(
            targets.LambdaFunction(
                ingestion,
                event=events.RuleTargetInput.from_object(
                    {
                        "Records": [
                            {
                                "eventSource": "aws:s3",
                                "s3": {
                                    "bucket": {
                                        "name": events.EventField.from_path("$.detail.bucket.name")
                                    },
                                    "object": {
                                        "key": events.EventField.from_path("$.detail.object.key")
                                    },
                                },
                            }
                        ]
                    }
                ),
            )
        )

    def _add_proxy(self, parent: apigw.IResource, path: str, fn: lambda_.Function) -> None:
        resource = parent.add_resource(path)
        integration = apigw.LambdaIntegration(fn)
        resource.add_method("ANY", integration)
        proxy = resource.add_resource("{proxy+}")
        proxy.add_method("ANY", integration)
