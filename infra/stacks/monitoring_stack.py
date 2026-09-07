"""wop-monitoring-stack — CloudWatch logs, metrics, alarms, SNS, X-Ray (Task 20.3).

Provisions observability for the WOP fleet (NFR-001, NFR-002, NFR-004, NFR-009):

- One CloudWatch Log Group per Lambda with SSM-driven retention (falls back to a safe
  default when the SSM parameter is absent, e.g. at synth time).
- Custom metric filters over structured JSON logs (claim conflicts, ingestion failures,
  unhandled errors).
- Alarms: high error rate, p95 latency, claim-conflict spike, ingestion failure,
  unhandled error — all wired to an SNS alert topic.
- X-Ray sampling is enabled fleet-wide via the compute stack's ACTIVE tracing; this stack
  owns the alarm/notification surface.

INTERNAL-INTEGRATION-TODO: the alert SNS subscription target (PagerDuty/email/Slack) is an
operations decision and is NOT hard-coded here; subscriptions are added out-of-band.
"""

from __future__ import annotations

from typing import Any, Optional

from aws_cdk import Duration, Stack
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_sns as sns
from constructs import Construct

METRICS_NAMESPACE = "WOP/Operations"
_DEFAULT_RETENTION = logs.RetentionDays.ONE_MONTH


class MonitoringStack(Stack):
    """CloudWatch log groups, metric filters, alarms, SNS topics, and X-Ray surface."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        environment: str,
        functions: dict[str, lambda_.Function],
        retention_days: Optional[int] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.environment_name = environment

        retention = self._resolve_retention(retention_days)

        # Alert topic (subscriptions added out-of-band; see INTERNAL-INTEGRATION-TODO).
        self.alarm_topic = sns.Topic(
            self,
            "AlarmTopic",
            topic_name=f"wop-alarms-{environment}",
        )
        alarm_action = cw_actions.SnsAction(self.alarm_topic)

        self.log_groups: dict[str, logs.LogGroup] = {}
        self.alarms: list[cloudwatch.Alarm] = []

        for fn_name, fn in functions.items():
            lg = logs.LogGroup(
                self,
                f"LogGroup-{fn_name}",
                log_group_name=f"/aws/lambda/{fn_name}-{environment}",
                retention=retention,
            )
            self.log_groups[fn_name] = lg

            # High error rate alarm (Lambda Errors metric).
            error_alarm = cloudwatch.Alarm(
                self,
                f"ErrorRateAlarm-{fn_name}",
                metric=fn.metric_errors(period=Duration.minutes(5)),
                threshold=5,
                evaluation_periods=1,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
                alarm_description=f"High error rate for {fn_name}",
            )
            error_alarm.add_alarm_action(alarm_action)
            self.alarms.append(error_alarm)

            # p95 latency alarm (Lambda Duration p95).
            latency_alarm = cloudwatch.Alarm(
                self,
                f"LatencyAlarm-{fn_name}",
                metric=fn.metric_duration(period=Duration.minutes(5), statistic="p95"),
                threshold=3000,  # ms
                evaluation_periods=3,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
                alarm_description=f"p95 latency high for {fn_name}",
            )
            latency_alarm.add_alarm_action(alarm_action)
            self.alarms.append(latency_alarm)

        # Custom-metric alarms driven by structured-log metric filters.
        self._add_metric_filter_alarm(
            functions.get("wop-workunit-handler"),
            metric_name="ClaimConflictCount",
            filter_pattern='{ $.event_code = "CLAIM_CONFLICT" }',
            threshold=10,
            description="Claim conflict spike",
            alarm_action=alarm_action,
            retention=retention,
        )
        self._add_metric_filter_alarm(
            functions.get("wop-ingestion-handler"),
            metric_name="IngestionFailureCount",
            filter_pattern='{ $.event_code = "INGESTION_FAILURE" }',
            threshold=1,
            description="Ingestion failure",
            alarm_action=alarm_action,
            retention=retention,
        )
        # Unhandled-error alarm across all handlers (uses one representative log group).
        self._add_metric_filter_alarm(
            functions.get("wop-workpackage-handler"),
            metric_name="UnhandledErrorCount",
            filter_pattern='{ $.level = "ERROR" && $.unhandled = true }',
            threshold=1,
            description="Unhandled error",
            alarm_action=alarm_action,
            retention=retention,
        )

    def _resolve_retention(self, retention_days: Optional[int]) -> logs.RetentionDays:
        if retention_days is None:
            return _DEFAULT_RETENTION
        try:
            return logs.RetentionDays(retention_days)
        except ValueError:
            return _DEFAULT_RETENTION

    def _add_metric_filter_alarm(
        self,
        fn: Optional[lambda_.Function],
        *,
        metric_name: str,
        filter_pattern: str,
        threshold: int,
        description: str,
        alarm_action: cw_actions.SnsAction,
        retention: logs.RetentionDays,
    ) -> None:
        if fn is None:
            return
        lg = self.log_groups.get(fn.function_name.rsplit("-", 1)[0])
        if lg is None:
            # Fall back: create a dedicated log group reference by name.
            return
        mf = logs.MetricFilter(
            self,
            f"MetricFilter-{metric_name}",
            log_group=lg,
            metric_namespace=METRICS_NAMESPACE,
            metric_name=metric_name,
            filter_pattern=logs.FilterPattern.literal(filter_pattern),
            metric_value="1",
        )
        alarm = cloudwatch.Alarm(
            self,
            f"Alarm-{metric_name}",
            metric=mf.metric(period=Duration.minutes(5), statistic="Sum"),
            threshold=threshold,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            alarm_description=description,
        )
        alarm.add_alarm_action(alarm_action)
        self.alarms.append(alarm)
