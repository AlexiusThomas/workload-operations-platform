"""wop-data-stack — DynamoDB tables, S3 buckets, and SSM parameters (Task 20.1).

The DynamoDB schema mirrors the authoritative design (DynamoDB Table Design and Access
Patterns) and the integration-test fixtures in ``tests/integration/conftest.py``:

- main table: pk/sk + GSI-1 state-site-worktype-date-index, GSI-2 work-package-children-index,
  GSI-3 material-status-index.
- audit table: pk/sk only (no GSIs) — PutItem-only IAM enforced in the compute stack.
- events table: pk/sk + GSI-4 production-week-index, GSI-5 material-week-index,
  GSI-6 technician-week-index, GSI-7 runner-week-index.
- idempotency table: pk only + TTL attribute ``ttl``.

Security posture:
- AWS-managed encryption at rest on every table (SEC-005 AC-1/AC-2).
- Point-in-time recovery on all tables except idempotency (transient records) (NFR-006 AC-1).
- S3 buckets: SSE, block-all-public-access, enforce-SSL (SEC-005 AC-3, NFR-007 AC-1).

Table and bucket names are published to SSM Parameter Store so the compute stack and the
running Lambdas resolve them at deploy/cold-start without hard-coding (NFR-007 AC-2, CON-006).

INTERNAL-INTEGRATION-TODO (OQ-007): event-table TTL/retention is unconfirmed by
Compliance/Operations; no TTL is set on audit/events tables until the requirement is known.
"""

from __future__ import annotations

from typing import Any

from aws_cdk import RemovalPolicy, Stack
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_ssm as ssm
from constructs import Construct

# SSM parameter path prefix (environment-scoped). No account/region embedded.
SSM_PREFIX = "/wop"


def _param_path(environment: str, name: str) -> str:
    return f"{SSM_PREFIX}/{environment}/{name}"


class DataStack(Stack):
    """DynamoDB tables, S3 buckets, and SSM parameters for WOP."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        environment: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.environment_name = environment

        # Non-prod stacks are destroyable to keep dev/test iteration cheap; prod
        # retains data on stack deletion to avoid accidental data loss.
        is_prod = environment == "prod"
        removal_policy = RemovalPolicy.RETAIN if is_prod else RemovalPolicy.DESTROY

        # -------------------------------------------------------------------
        # DynamoDB tables
        # -------------------------------------------------------------------
        self.main_table = dynamodb.Table(
            self,
            "MainTable",
            table_name=f"wop-main-table-{environment}",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            point_in_time_recovery=True,
            removal_policy=removal_policy,
        )
        self.main_table.add_global_secondary_index(
            index_name="state-site-worktype-date-index",
            partition_key=dynamodb.Attribute(name="state", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk_gsi1", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )
        self.main_table.add_global_secondary_index(
            index_name="work-package-children-index",
            partition_key=dynamodb.Attribute(
                name="work_package_id", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )
        self.main_table.add_global_secondary_index(
            index_name="material-status-index",
            partition_key=dynamodb.Attribute(name="status", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="work_package_id", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        self.audit_table = dynamodb.Table(
            self,
            "AuditTable",
            table_name=f"wop-audit-table-{environment}",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            point_in_time_recovery=True,
            removal_policy=RemovalPolicy.RETAIN if is_prod else RemovalPolicy.DESTROY,
        )

        self.events_table = dynamodb.Table(
            self,
            "EventsTable",
            table_name=f"wop-events-table-{environment}",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            point_in_time_recovery=True,
            removal_policy=RemovalPolicy.RETAIN if is_prod else RemovalPolicy.DESTROY,
        )
        # GSI-4 / GSI-5 share the event_type#week_key / timestamp key layout but are
        # distinct indexes so production and material queries scale independently.
        for index_name in ("production-week-index", "material-week-index"):
            self.events_table.add_global_secondary_index(
                index_name=index_name,
                partition_key=dynamodb.Attribute(
                    name="event_type#week_key", type=dynamodb.AttributeType.STRING
                ),
                sort_key=dynamodb.Attribute(name="timestamp", type=dynamodb.AttributeType.STRING),
                projection_type=dynamodb.ProjectionType.ALL,
            )
        self.events_table.add_global_secondary_index(
            index_name="technician-week-index",
            partition_key=dynamodb.Attribute(
                name="technician_id", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="week_key#timestamp", type=dynamodb.AttributeType.STRING
            ),
            projection_type=dynamodb.ProjectionType.ALL,
        )
        self.events_table.add_global_secondary_index(
            index_name="runner-week-index",
            partition_key=dynamodb.Attribute(name="runner_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(
                name="week_key#timestamp", type=dynamodb.AttributeType.STRING
            ),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        # Idempotency table: single-key, TTL-driven cleanup, no PITR (transient records).
        self.idempotency_table = dynamodb.Table(
            self,
            "IdempotencyTable",
            table_name=f"wop-idempotency-table-{environment}",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            point_in_time_recovery=False,
            time_to_live_attribute="ttl",
            removal_policy=RemovalPolicy.DESTROY,
        )

        # -------------------------------------------------------------------
        # S3 buckets (import / archive / error) — encrypted, private, TLS-only.
        # Bucket names are NOT hard-coded to a global name; CDK auto-generates a
        # unique physical name so there is no account/region collision risk.
        # -------------------------------------------------------------------
        common_bucket_kwargs = dict(
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=is_prod,
            removal_policy=RemovalPolicy.RETAIN if is_prod else RemovalPolicy.DESTROY,
            auto_delete_objects=not is_prod,
        )
        self.import_bucket = s3.Bucket(self, "ImportBucket", **common_bucket_kwargs)
        self.archive_bucket = s3.Bucket(self, "ArchiveBucket", **common_bucket_kwargs)
        self.error_bucket = s3.Bucket(self, "ErrorBucket", **common_bucket_kwargs)

        # -------------------------------------------------------------------
        # SSM parameters — names the compute stack + Lambdas resolve at runtime.
        # -------------------------------------------------------------------
        self._publish_ssm(environment)

    def _publish_ssm(self, environment: str) -> None:
        """Publish table/bucket names to SSM Parameter Store (NFR-007 AC-2)."""
        params = {
            "main-table-name": self.main_table.table_name,
            "audit-table-name": self.audit_table.table_name,
            "events-table-name": self.events_table.table_name,
            "idempotency-table-name": self.idempotency_table.table_name,
            "import-bucket-name": self.import_bucket.bucket_name,
            "archive-bucket-name": self.archive_bucket.bucket_name,
            "error-bucket-name": self.error_bucket.bucket_name,
        }
        for name, value in params.items():
            ssm.StringParameter(
                self,
                f"Param-{name}",
                parameter_name=_param_path(environment, name),
                string_value=value,
            )
