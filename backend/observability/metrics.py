"""Custom CloudWatch metric emitters for WOP (NFR-004 AC-4).

All metrics are published to the ``WOP/Operations`` namespace using the metric names
enumerated in the design's Observability Strategy section. The emitter is safely
no-op/injectable so unit tests never require AWS credentials or network access:

- The boto3 CloudWatch client is created lazily on first use (never at import time).
- Tests may call :func:`set_client` to inject a fake client, or :func:`disable` to make
  every emit a silent no-op.
- Any error from ``put_metric_data`` is swallowed and logged; metric emission must never
  break a business operation.

This module is import-safe without AWS credentials.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from backend.observability import logger

#: CloudWatch namespace for all WOP custom metrics (design: CloudWatch Custom Metrics).
NAMESPACE = "WOP/Operations"

# ---------------------------------------------------------------------------
# Metric names (design: CloudWatch Custom Metrics table)
# ---------------------------------------------------------------------------
INGESTION_SUCCESS = "wop.ingestion.success_count"
INGESTION_FAILURE = "wop.ingestion.failure_count"
CLAIM_CONFLICT = "wop.claim.conflict_count"
MATERIAL_CLAIM_CONFLICT = "wop.material_claim.conflict_count"
MATERIAL_DELIVERY = "wop.material_delivery.count"
VERIFICATION_PASS = "wop.verification.pass_count"
VERIFICATION_FAIL = "wop.verification.fail_count"
ROLLOVER_UNIT = "wop.rollover.unit_count"
REPORT_GENERATION = "wop.report.generation_count"
UNHANDLED_ERROR = "wop.unhandled_error"


def _state_transition_metric(transition_name: str) -> str:
    """Return the per-transition metric name, e.g. ``wop.state_transition.CLAIMED.count``."""
    return f"wop.state_transition.{transition_name}.count"


# ---------------------------------------------------------------------------
# Injectable client management (keeps unit tests hermetic)
# ---------------------------------------------------------------------------
_client: Optional[Any] = None
_enabled: bool = True


def set_client(client: Optional[Any]) -> None:
    """Inject a CloudWatch client (or a test fake). Passing None clears the cached client."""
    global _client
    _client = client


def disable() -> None:
    """Disable metric emission entirely (used by unit tests). Emits become no-ops."""
    global _enabled
    _enabled = False


def enable() -> None:
    """Re-enable metric emission after :func:`disable`."""
    global _enabled
    _enabled = True


def _get_client() -> Optional[Any]:
    """Return the CloudWatch client, creating it lazily via boto3 on first use.

    Returns None if boto3 or the client cannot be constructed (e.g. no credentials in a
    local test run); callers treat a None client as a no-op.
    """
    global _client
    if _client is not None:
        return _client
    try:
        import boto3  # imported lazily so the module stays import-safe without AWS

        _client = boto3.client("cloudwatch")
    except Exception as exc:  # pragma: no cover - defensive; no AWS in unit tests
        logger.warning("metrics_client_init_failed", error=str(exc))
        _client = None
    return _client


def emit(
    metric_name: str,
    value: float = 1.0,
    unit: str = "Count",
    dimensions: Optional[Dict[str, str]] = None,
) -> None:
    """Emit a single custom metric data point to the ``WOP/Operations`` namespace.

    Emission is best-effort: when disabled, or when no client is available, or when the
    CloudWatch call raises, the function returns without raising. Metric failures must
    never interrupt a business operation (design: Observability Strategy).

    Args:
        metric_name: One of the module-level metric name constants.
        value: The metric value (defaults to 1.0 for count metrics).
        unit: CloudWatch unit (defaults to ``"Count"``).
        dimensions: Optional dimension name/value map.
    """
    if not _enabled:
        return
    client = _get_client()
    if client is None:
        return

    metric_datum: Dict[str, Any] = {
        "MetricName": metric_name,
        "Value": value,
        "Unit": unit,
    }
    if dimensions:
        metric_datum["Dimensions"] = [
            {"Name": name, "Value": val} for name, val in dimensions.items()
        ]
    try:
        client.put_metric_data(Namespace=NAMESPACE, MetricData=[metric_datum])
    except Exception as exc:  # pragma: no cover - defensive; swallow metric errors
        logger.warning("metric_emit_failed", error=str(exc), action=metric_name)


def increment(metric_name: str, dimensions: Optional[Dict[str, str]] = None) -> None:
    """Emit a count metric with value 1.0 (convenience wrapper over :func:`emit`)."""
    emit(metric_name, value=1.0, unit="Count", dimensions=dimensions)


def state_transition(transition_name: str) -> None:
    """Emit the per-transition count metric for ``transition_name``."""
    increment(_state_transition_metric(transition_name))


def emit_batch(data: List[Dict[str, Any]]) -> None:
    """Emit multiple metric data points in a single CloudWatch call (best-effort)."""
    if not _enabled or not data:
        return
    client = _get_client()
    if client is None:
        return
    try:
        client.put_metric_data(Namespace=NAMESPACE, MetricData=data)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("metric_batch_emit_failed", error=str(exc))
