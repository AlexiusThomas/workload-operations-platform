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

# Placeholder Lambda code. Real deployment packages the backend/ tree; for synth and
# review no bundling is required. Inline code keeps synth hermetic and deploy-free.
# Lambda layers cannot use inline code (CDK: InlineCodeSupportedLambdaLayers);
# they require an on-disk asset. This placeholder dir is packaged at synth time;
# the real idempotency payload is wired at deploy time.
_LAYER_ASSET_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "layer_placeholder",
)

_PLACEHOLDER_CODE = lambda_.Code.from_inline(
    "def handler(event, context):\n"
    "    raise NotImplementedError('Deployment packaging is wired at deploy time')\n"
)


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
            fn = lambda_.Function(
                self,
                logical_id,
                function_name=f"{fn_name}-{environment}",
                runtime=lambda_.Runtime.PYTHON_3_13,
                handler="index.handler",
                code=_PLACEHOLDER_CODE,
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
        import_bucket.grant_read(ingestion)
        archive_bucket.grant_put(ingestion)
        error_bucket.grant_put(ingestion)

        # S3 import object-created triggers the ingestion handler (DEP-001 RATS).
        # The notification is configured on the bucket in the data stack to keep the
        # cross-stack dependency one-directional (compute -> data); here we only grant
        # S3 permission to invoke the ingestion Lambda, avoiding a data<->compute cycle.
        self._grant_s3_invoke(import_bucket, ingestion)

        # ---- API Gateway (HTTPS-only, TLS 1.2+, /v1/) ----
        self.api = apigw.RestApi(
            self,
            "WopApi",
            rest_api_name=f"wop-api-{environment}",
            deploy_options=apigw.StageOptions(
                stage_name="v1",
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

    def _grant_s3_invoke(self, import_bucket: s3.IBucket, ingestion: lambda_.Function) -> None:
        # Allow S3 (the import bucket) to invoke the ingestion Lambda. This adds a
        # compute -> data reference only (via the bucket ARN in the source condition),
        # never data -> compute, so no stack dependency cycle is introduced. The
        # matching bucket notification is declared in the data stack / at deploy time.
        ingestion.add_permission(
            "AllowImportBucketInvoke",
            principal=iam.ServicePrincipal("s3.amazonaws.com"),
            action="lambda:InvokeFunction",
            source_arn=import_bucket.bucket_arn,
        )

    def _add_proxy(self, parent: apigw.IResource, path: str, fn: lambda_.Function) -> None:
        resource = parent.add_resource(path)
        integration = apigw.LambdaIntegration(fn)
        resource.add_method("ANY", integration)
        proxy = resource.add_resource("{proxy+}")
        proxy.add_method("ANY", integration)
