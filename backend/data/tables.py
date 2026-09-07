"""DynamoDB table client accessors for the WOP multi-table design (design: DynamoDB Table Design).

Provides lazily-constructed boto3 DynamoDB resource and per-table ``Table`` accessors for:
``wop-main-table``, ``wop-audit-table``, ``wop-events-table``, ``wop-idempotency-table``.

The boto3 resource and table names are resolved on first use, never at import time, so this
module is import-safe without AWS credentials. Tests can inject a resource and/or explicit
table names via :func:`configure` (e.g. a moto-emulated resource).
"""

from __future__ import annotations

from typing import Any, Optional

from backend.data import config

# Cached boto3 resource and resolved table names (populated lazily / via configure()).
_resource: Optional[Any] = None
_config: Optional[config.DataConfig] = None


def configure(
    resource: Optional[Any] = None, data_config: Optional[config.DataConfig] = None
) -> None:
    """Inject a DynamoDB resource and/or resolved config (used by tests and cold-start wiring).

    Args:
        resource: A boto3 ``dynamodb`` resource (or a moto-emulated one). None clears it.
        data_config: A resolved :class:`~backend.data.config.DataConfig`. None clears it.
    """
    global _resource, _config
    _resource = resource
    _config = data_config


def _get_resource() -> Any:
    """Return the DynamoDB resource, creating it lazily via boto3 on first use."""
    global _resource
    if _resource is None:
        import boto3  # lazy import keeps the module import-safe without AWS

        _resource = boto3.resource("dynamodb")
    return _resource


def _get_config() -> config.DataConfig:
    """Return the resolved data config, loading from the environment on first use."""
    global _config
    if _config is None:
        _config = config.load_config()
    return _config


def main_table() -> Any:
    """Return the ``wop-main-table`` Table resource."""
    return _get_resource().Table(_get_config().main_table)


def audit_table() -> Any:
    """Return the ``wop-audit-table`` Table resource."""
    return _get_resource().Table(_get_config().audit_table)


def events_table() -> Any:
    """Return the ``wop-events-table`` Table resource."""
    return _get_resource().Table(_get_config().events_table)


def idempotency_table() -> Any:
    """Return the ``wop-idempotency-table`` Table resource."""
    return _get_resource().Table(_get_config().idempotency_table)
