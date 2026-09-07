"""WOP CDK stacks (Task 20).

Three stacks compose the WOP infrastructure:

- :class:`~infra.stacks.data_stack.DataStack` — DynamoDB tables, S3 buckets, SSM params.
- :class:`~infra.stacks.compute_stack.ComputeStack` — Lambda functions, API Gateway,
  EventBridge rules, and least-privilege IAM roles.
- :class:`~infra.stacks.monitoring_stack.MonitoringStack` — CloudWatch log groups,
  metric filters, alarms, SNS topics, and X-Ray.

All stacks are environment-agnostic: account and region are supplied by the CDK
environment at deploy time (never hard-coded). Resource names are derived from an
``environment`` string (e.g. ``dev``/``staging``/``prod``) passed by the app.
"""

from infra.stacks.compute_stack import ComputeStack
from infra.stacks.data_stack import DataStack
from infra.stacks.monitoring_stack import MonitoringStack

__all__ = ["DataStack", "ComputeStack", "MonitoringStack"]
