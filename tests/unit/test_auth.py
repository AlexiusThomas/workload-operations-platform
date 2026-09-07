"""Unit tests for the authentication abstraction and RBAC/ownership (Task 5.4).

Covers synthetic token resolution, fail-closed production behaviour, role checks per the
RBAC matrix, and Ownership Rule 4 (Technician vs Lead/Admin) for label / tote-assign /
ready-to-verify (SEC-001 AC-2, AC-4, FR-019 AC-3, SEC-002 AC-2).
"""

from __future__ import annotations

import pytest

from backend.auth import middleware, provider
from backend.auth.provider import (
    MANAGER_ADMIN,
    MATERIAL_RUNNER,
    TECHNICIAN,
    VERIFIER_LEAD,
    UserContext,
)
from backend.observability.errors import (
    AuthConfigurationError,
    AuthenticationError,
    AuthorizationError,
)


# ---------------------------------------------------------------------------
# SyntheticAuthProvider
# ---------------------------------------------------------------------------
def test_synthetic_token_resolves_to_user_context() -> None:
    p = provider.SyntheticAuthProvider()
    event = {"headers": {"Authorization": "Bearer token-tech-001"}}
    ctx = p.authenticate(event)
    assert ctx == UserContext("TECH-001", TECHNICIAN, "Technician One")


def test_synthetic_authorization_header_case_insensitive() -> None:
    p = provider.SyntheticAuthProvider()
    ctx = p.authenticate({"headers": {"authorization": "Bearer token-admin-001"}})
    assert ctx.user_id == "ADMIN-001"
    assert ctx.role == MANAGER_ADMIN


def test_synthetic_missing_token_raises() -> None:
    p = provider.SyntheticAuthProvider()
    with pytest.raises(AuthenticationError):
        p.authenticate({"headers": {}})


def test_synthetic_unknown_token_raises() -> None:
    p = provider.SyntheticAuthProvider()
    with pytest.raises(AuthenticationError):
        p.validate_token("token-does-not-exist")


def test_get_synthetic_user_by_id() -> None:
    p = provider.SyntheticAuthProvider()
    ctx = p.get_synthetic_user("RUNNER-001")
    assert ctx.role == MATERIAL_RUNNER


# ---------------------------------------------------------------------------
# Fail-closed production behaviour
# ---------------------------------------------------------------------------
def test_prod_without_auth_mode_fails_closed() -> None:
    with pytest.raises(AuthConfigurationError):
        provider.get_auth_provider(mode=None, environment="prod")


def test_prod_with_synthetic_fails_closed() -> None:
    with pytest.raises(AuthConfigurationError):
        provider.get_auth_provider(mode="synthetic", environment="prod")


def test_dev_defaults_to_synthetic() -> None:
    p = provider.get_auth_provider(mode=None, environment="dev")
    assert isinstance(p, provider.SyntheticAuthProvider)


def test_prod_with_midway_returns_midway_stub() -> None:
    p = provider.get_auth_provider(mode="midway", environment="prod")
    assert isinstance(p, provider.MidwayAuthProvider)


def test_midway_authenticate_not_implemented() -> None:
    p = provider.MidwayAuthProvider()
    with pytest.raises(NotImplementedError):
        p.authenticate({"headers": {"Authorization": "Bearer x"}})


def test_unknown_mode_raises() -> None:
    with pytest.raises(AuthConfigurationError):
        provider.get_auth_provider(mode="ldap", environment="dev")


# ---------------------------------------------------------------------------
# RBAC matrix
# ---------------------------------------------------------------------------
_TECH = UserContext("TECH-001", TECHNICIAN, "Tech One")
_RUNNER = UserContext("RUNNER-001", MATERIAL_RUNNER, "Runner One")
_LEAD = UserContext("VERIFY-001", VERIFIER_LEAD, "Lead One")
_ADMIN = UserContext("ADMIN-001", MANAGER_ADMIN, "Admin One")


@pytest.mark.parametrize(
    "operation, allowed, denied",
    [
        ("verify", [_LEAD, _ADMIN], [_TECH, _RUNNER]),
        ("create_work_package", [_ADMIN], [_TECH, _RUNNER, _LEAD]),
        ("claim", [_TECH, _LEAD, _ADMIN], [_RUNNER]),
        ("rework_claim", [_TECH], [_RUNNER, _LEAD, _ADMIN]),
        ("material_claim", [_RUNNER, _ADMIN], [_TECH, _LEAD]),
        ("view_audit", [_LEAD, _ADMIN], [_TECH, _RUNNER]),
        ("trigger_report", [_ADMIN], [_TECH, _RUNNER, _LEAD]),
        ("start", [_TECH], [_RUNNER, _LEAD, _ADMIN]),
    ],
)
def test_rbac_matrix(operation, allowed, denied) -> None:
    for ctx in allowed:
        middleware.authorize_operation(ctx, operation)  # no raise
    for ctx in denied:
        with pytest.raises(AuthorizationError):
            middleware.authorize_operation(ctx, operation)


def test_authorize_unknown_operation_raises() -> None:
    with pytest.raises(AuthorizationError):
        middleware.authorize_operation(_ADMIN, "does_not_exist")


# ---------------------------------------------------------------------------
# Ownership Rule 1 (start/quantity/prep-complete) — strict owner only
# ---------------------------------------------------------------------------
def test_require_owner_allows_claiming_technician() -> None:
    middleware.require_owner(_TECH, claimed_by="TECH-001")  # no raise


def test_require_owner_rejects_non_owner() -> None:
    with pytest.raises(AuthorizationError):
        middleware.require_owner(_TECH, claimed_by="TECH-002")


def test_require_owner_rejects_unclaimed() -> None:
    with pytest.raises(AuthorizationError):
        middleware.require_owner(_TECH, claimed_by=None)


# ---------------------------------------------------------------------------
# Ownership Rule 4 (label/tote/ready-to-verify) — owner or Lead/Admin bypass
# ---------------------------------------------------------------------------
def test_rule4_technician_owner_allowed() -> None:
    middleware.require_owner_or_lead_admin(_TECH, claimed_by="TECH-001")  # no raise


def test_rule4_technician_non_owner_denied() -> None:
    with pytest.raises(AuthorizationError):
        middleware.require_owner_or_lead_admin(_TECH, claimed_by="TECH-002")


def test_rule4_lead_bypasses_ownership() -> None:
    # Lead may act on a WorkUnit claimed by any Technician.
    middleware.require_owner_or_lead_admin(_LEAD, claimed_by="TECH-002")  # no raise


def test_rule4_admin_bypasses_ownership_even_unclaimed() -> None:
    middleware.require_owner_or_lead_admin(_ADMIN, claimed_by=None)  # no raise


# ---------------------------------------------------------------------------
# Ownership Rule 2 (material-deliver) — runner match, Admin bypass
# ---------------------------------------------------------------------------
def test_runner_match_allows_claiming_runner() -> None:
    middleware.require_runner_match(_RUNNER, runner_id="RUNNER-001")  # no raise


def test_runner_match_rejects_other_runner() -> None:
    other = UserContext("RUNNER-002", MATERIAL_RUNNER, "Runner Two")
    with pytest.raises(AuthorizationError):
        middleware.require_runner_match(other, runner_id="RUNNER-001")


def test_runner_match_admin_bypass() -> None:
    middleware.require_runner_match(_ADMIN, runner_id="RUNNER-001")  # no raise


# ---------------------------------------------------------------------------
# Identity mismatch warning (FR-018 AC-3) — logged, never raised
# ---------------------------------------------------------------------------
def test_identity_mismatch_logs_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def fake_warning(message, **fields):
        captured["message"] = message
        captured.update(fields)
        return fields

    monkeypatch.setattr(middleware.logger, "warning", fake_warning)
    middleware.check_identity_mismatch(_TECH, client_supplied_id="TECH-999")
    assert captured["message"] == middleware.IDENTITY_MISMATCH_WARNING
    assert captured["client_supplied_identity"] == "TECH-999"
    assert captured["session_identity"] == "TECH-001"


def test_identity_match_no_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    called = {"count": 0}
    monkeypatch.setattr(
        middleware.logger, "warning", lambda *a, **k: called.__setitem__("count", 1)
    )
    middleware.check_identity_mismatch(_TECH, client_supplied_id="TECH-001")
    assert called["count"] == 0
