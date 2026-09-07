"""Configuration resolution for the data layer (NFR-007 AC-1, AC-2, AC-3).

Table and bucket names are externalised as environment variables (populated from SSM
Parameter Store at deploy time — design: Environment-Specific Configuration). Nothing is
hard-coded in application source (NFR-007 AC-2). Resolution happens inside functions, not
at import time, so the module is import-safe without AWS or a configured environment.

If a required configuration value is absent, :func:`require` raises
:class:`~backend.observability.errors.MissingConfigurationError` with a descriptive
message so the Lambda fails fast at cold start (NFR-007 AC-3).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from backend.observability.errors import MissingConfigurationError

# ---------------------------------------------------------------------------
# Environment variable names (populated from SSM at deploy; design SSM table)
# ---------------------------------------------------------------------------
ENV_MAIN_TABLE = "WOP_MAIN_TABLE_NAME"
ENV_AUDIT_TABLE = "WOP_AUDIT_TABLE_NAME"
ENV_EVENTS_TABLE = "WOP_EVENTS_TABLE_NAME"
ENV_IDEMPOTENCY_TABLE = "WOP_IDEMPOTENCY_TABLE_NAME"
ENV_IMPORT_BUCKET = "WOP_IMPORT_BUCKET_NAME"
ENV_ARCHIVE_BUCKET = "WOP_ARCHIVE_BUCKET_NAME"
ENV_ERROR_BUCKET = "WOP_ERROR_BUCKET_NAME"


def require(env_var: str) -> str:
    """Return the value of a required environment variable.

    Args:
        env_var: The environment variable name.

    Returns:
        The configured value.

    Raises:
        MissingConfigurationError: If the variable is absent or empty (NFR-007 AC-3).
    """
    value = os.environ.get(env_var)
    if not value:
        raise MissingConfigurationError(
            f"Required configuration '{env_var}' is not set. "
            "It must be provided via environment variable (sourced from SSM at deploy)."
        )
    return value


@dataclass(frozen=True)
class DataConfig:
    """Resolved data-layer configuration (table and bucket names)."""

    main_table: str
    audit_table: str
    events_table: str
    idempotency_table: str
    import_bucket: str
    archive_bucket: str
    error_bucket: str


def load_config() -> DataConfig:
    """Resolve all data-layer configuration from the environment.

    Raises:
        MissingConfigurationError: If any required value is absent (NFR-007 AC-3).
    """
    return DataConfig(
        main_table=require(ENV_MAIN_TABLE),
        audit_table=require(ENV_AUDIT_TABLE),
        events_table=require(ENV_EVENTS_TABLE),
        idempotency_table=require(ENV_IDEMPOTENCY_TABLE),
        import_bucket=require(ENV_IMPORT_BUCKET),
        archive_bucket=require(ENV_ARCHIVE_BUCKET),
        error_bucket=require(ENV_ERROR_BUCKET),
    )
