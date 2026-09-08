"""CDK application entry point for WOP infrastructure (Task 20).

Environment-agnostic: account/region resolved from the CDK environment at deploy time.
Run ``cdk synth`` (or ``python -m infra.app`` for a bare synth) to produce CloudFormation
without deploying. No AWS credentials, account IDs, or regions are hard-coded here.
"""

from __future__ import annotations

import os

import aws_cdk as cdk

from infra.stacks import ComputeStack, DataStack, MonitoringStack


def build_app(environment: str | None = None, auth_mode: str | None = None) -> cdk.App:
    """Construct the CDK app with all three WOP stacks.

    Args:
        environment: Deployment environment name (dev/staging/prod). Falls back to the
            ``ENVIRONMENT`` env var, then ``dev``.
        auth_mode: Auth mode passed to the compute stack's prod fail-closed guard.

    Returns:
        The configured :class:`aws_cdk.App`.
    """
    app = cdk.App()
    env_name = (
        environment
        or app.node.try_get_context("environment")
        or os.environ.get("ENVIRONMENT", "dev")
    )

    # Account/region supplied by the CDK CLI environment; never hard-coded.
    cdk_env = cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION"),
    )

    data = DataStack(
        app,
        f"wop-data-stack-{env_name}",
        environment=env_name,
        env=cdk_env,
    )
    compute = ComputeStack(
        app,
        f"wop-compute-stack-{env_name}",
        environment=env_name,
        main_table=data.main_table,
        audit_table=data.audit_table,
        events_table=data.events_table,
        idempotency_table=data.idempotency_table,
        import_bucket=data.import_bucket,
        archive_bucket=data.archive_bucket,
        error_bucket=data.error_bucket,
        auth_mode=auth_mode,
        env=cdk_env,
    )
    MonitoringStack(
        app,
        f"wop-monitoring-stack-{env_name}",
        environment=env_name,
        functions=compute.functions,
        env=cdk_env,
    )
    return app


if __name__ == "__main__":
    build_app().synth()
