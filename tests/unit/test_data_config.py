"""Unit tests for data-layer configuration resolution (Task 7.1, NFR-007 AC-3)."""

from __future__ import annotations

import pytest

from backend.data import config
from backend.observability.errors import MissingConfigurationError


def test_require_returns_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(config.ENV_MAIN_TABLE, "wop-main")
    assert config.require(config.ENV_MAIN_TABLE) == "wop-main"


def test_require_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(config.ENV_MAIN_TABLE, raising=False)
    with pytest.raises(MissingConfigurationError):
        config.require(config.ENV_MAIN_TABLE)


def test_require_empty_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(config.ENV_AUDIT_TABLE, "")
    with pytest.raises(MissingConfigurationError):
        config.require(config.ENV_AUDIT_TABLE)


def test_load_config_resolves_all(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(config.ENV_MAIN_TABLE, "main")
    monkeypatch.setenv(config.ENV_AUDIT_TABLE, "audit")
    monkeypatch.setenv(config.ENV_EVENTS_TABLE, "events")
    monkeypatch.setenv(config.ENV_IDEMPOTENCY_TABLE, "idem")
    monkeypatch.setenv(config.ENV_IMPORT_BUCKET, "import")
    monkeypatch.setenv(config.ENV_ARCHIVE_BUCKET, "archive")
    monkeypatch.setenv(config.ENV_ERROR_BUCKET, "error")

    cfg = config.load_config()
    assert cfg.main_table == "main"
    assert cfg.audit_table == "audit"
    assert cfg.events_table == "events"
    assert cfg.idempotency_table == "idem"
    assert cfg.import_bucket == "import"
    assert cfg.archive_bucket == "archive"
    assert cfg.error_bucket == "error"


def test_load_config_missing_one_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(config.ENV_MAIN_TABLE, "main")
    monkeypatch.delenv(config.ENV_AUDIT_TABLE, raising=False)
    with pytest.raises(MissingConfigurationError):
        config.load_config()
