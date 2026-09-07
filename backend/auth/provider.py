"""Authentication abstraction: AuthProvider, UserContext, and concrete providers.

Business logic in every Lambda handler depends only on the :class:`AuthProvider` protocol
(SEC-001 AC-1). The concrete implementation is selected at cold start by
:func:`get_auth_provider` from the ``AUTH_MODE`` value:

- ``synthetic`` -> :class:`SyntheticAuthProvider` (dev/test; static token registry).
- ``midway``    -> :class:`MidwayAuthProvider` (production; INTERNAL-INTEGRATION-TODO DEP-002).

Fail-closed behaviour (SEC-001 AC-4, NFR-007 AC-3): in ``ENVIRONMENT=prod`` an absent or
``synthetic`` auth mode raises :class:`AuthConfigurationError` so a misconfigured Lambda
refuses to serve requests. In dev/staging, an absent mode safely defaults to ``synthetic``.

This module is import-safe without AWS credentials: no environment or AWS access occurs at
import time. ``get_auth_provider`` reads its inputs from explicit arguments.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, Optional, Protocol, runtime_checkable

from backend.observability.errors import AuthConfigurationError, AuthenticationError

# ---------------------------------------------------------------------------
# Role constants (design: Authorization Model role definitions)
# ---------------------------------------------------------------------------
TECHNICIAN = "TECHNICIAN"
MATERIAL_RUNNER = "MATERIAL_RUNNER"
VERIFIER_LEAD = "VERIFIER_LEAD"
MANAGER_ADMIN = "MANAGER_ADMIN"

#: Auth mode values.
AUTH_MODE_SYNTHETIC = "synthetic"
AUTH_MODE_MIDWAY = "midway"

#: Environment names.
ENV_PROD = "prod"


@dataclass(frozen=True)
class UserContext:
    """Server-verified identity of the caller (design: AuthProvider interface).

    Attributes:
        user_id: Unique identifier (e.g. ``"TECH-001"`` in dev, employee id in prod).
        role: One of TECHNICIAN, MATERIAL_RUNNER, VERIFIER_LEAD, MANAGER_ADMIN.
        display_name: Human-readable name for AuditEvent display.
    """

    user_id: str
    role: str
    display_name: str


@runtime_checkable
class AuthProvider(Protocol):
    """The single auth abstraction every handler depends on (SEC-001 AC-1)."""

    def authenticate(self, request_event: Dict) -> UserContext:
        """Extract and validate the bearer token; return a :class:`UserContext`.

        Raises:
            AuthenticationError: If the token is absent or invalid (HTTP 401).
        """
        ...

    def validate_token(self, token: str) -> UserContext:
        """Validate a raw token string and return the encoded :class:`UserContext`."""
        ...

    def get_synthetic_user(self, user_id: str) -> UserContext:
        """Dev/test only: return the :class:`UserContext` for a known synthetic user id."""
        ...


# ---------------------------------------------------------------------------
# Synthetic provider (dev/test) — static token registry (design: SyntheticAuthProvider)
# ---------------------------------------------------------------------------
SYNTHETIC_USERS: Dict[str, UserContext] = {
    "token-tech-001": UserContext("TECH-001", TECHNICIAN, "Technician One"),
    "token-tech-002": UserContext("TECH-002", TECHNICIAN, "Technician Two"),
    "token-runner-001": UserContext("RUNNER-001", MATERIAL_RUNNER, "Runner One"),
    "token-verify-001": UserContext("VERIFY-001", VERIFIER_LEAD, "Lead One"),
    "token-admin-001": UserContext("ADMIN-001", MANAGER_ADMIN, "Admin One"),
}


def _extract_bearer_token(request_event: Dict) -> str:
    """Pull the bearer token out of the ``Authorization`` header (case-insensitive)."""
    headers = request_event.get("headers") or {}
    auth_header = ""
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == "authorization":
            auth_header = value or ""
            break
    return auth_header.removeprefix("Bearer ").strip()


class SyntheticAuthProvider:
    """Dev/test auth provider backed by an in-memory token registry (SEC-001 AC-2).

    Makes no external calls, enabling full end-to-end testing without a real IdP
    (CON-003, FR-023 AC-4).
    """

    def authenticate(self, request_event: Dict) -> UserContext:
        token = _extract_bearer_token(request_event)
        if not token:
            raise AuthenticationError("Missing Authorization header")
        return self.validate_token(token)

    def validate_token(self, token: str) -> UserContext:
        user = SYNTHETIC_USERS.get(token)
        if user is None:
            raise AuthenticationError("Unknown synthetic token")
        return user

    def get_synthetic_user(self, user_id: str) -> UserContext:
        for ctx in SYNTHETIC_USERS.values():
            if ctx.user_id == user_id:
                return ctx
        raise AuthenticationError(f"Synthetic user not found: {user_id}")


# ---------------------------------------------------------------------------
# Production provider (INTERNAL-INTEGRATION-TODO — DEP-002)
# ---------------------------------------------------------------------------
class MidwayAuthProvider:
    """Production auth provider — INTERNAL-INTEGRATION-TODO (DEP-002).

    The Midway integration protocol, token format, and endpoints are unconfirmed
    (OQ-002) and MUST NOT be fabricated. Every method raises ``NotImplementedError`` until
    the integration is confirmed. Swapping this provider in requires no business-logic
    change (SEC-001 AC-3).
    """

    _TODO = (
        "INTERNAL-INTEGRATION-TODO: Midway authentication is not implemented. "
        "See DEP-002 / OQ-002 for the required protocol, token format, and endpoints."
    )

    def authenticate(self, request_event: Dict) -> UserContext:
        raise NotImplementedError(self._TODO)

    def validate_token(self, token: str) -> UserContext:
        raise NotImplementedError(self._TODO)

    def get_synthetic_user(self, user_id: str) -> UserContext:
        raise NotImplementedError("Synthetic users are not available in production auth mode")


def resolve_auth_mode(
    auth_mode: Optional[str] = None,
    environment: Optional[str] = None,
) -> str:
    """Resolve the effective auth mode, failing closed in production (SEC-001 AC-4).

    Args:
        auth_mode: The configured ``AUTH_MODE`` (falls back to the env var if None).
        environment: The ``ENVIRONMENT`` (falls back to the env var, default ``"dev"``).

    Returns:
        The resolved auth mode string (``"synthetic"`` or ``"midway"``).

    Raises:
        AuthConfigurationError: If ``environment == "prod"`` and the mode is absent or
            ``synthetic``. Production must use the approved provider (NFR-007 AC-3).
    """
    mode = auth_mode if auth_mode is not None else os.environ.get("AUTH_MODE")
    env = environment if environment is not None else os.environ.get("ENVIRONMENT", "dev")

    if env == ENV_PROD:
        if not mode or mode == AUTH_MODE_SYNTHETIC:
            raise AuthConfigurationError(
                "AUTH_MODE must be set to the approved provider in production; "
                "synthetic auth is not permitted. See DEP-002."
            )
        return mode

    # dev/staging: default to synthetic when unset.
    return mode or AUTH_MODE_SYNTHETIC


def get_auth_provider(
    mode: Optional[str] = None,
    environment: Optional[str] = None,
) -> AuthProvider:
    """Return the concrete auth provider for the resolved mode (SEC-001 AC-4).

    Args:
        mode: The desired auth mode; when None, resolved from ``AUTH_MODE`` with
            fail-closed production checks via :func:`resolve_auth_mode`.
        environment: The environment name; when None, resolved from ``ENVIRONMENT``.

    Returns:
        A :class:`SyntheticAuthProvider` or :class:`MidwayAuthProvider`.

    Raises:
        AuthConfigurationError: On a fail-closed production misconfiguration, or an
            unknown mode.
    """
    resolved = resolve_auth_mode(auth_mode=mode, environment=environment)
    if resolved == AUTH_MODE_SYNTHETIC:
        return SyntheticAuthProvider()
    if resolved == AUTH_MODE_MIDWAY:
        return MidwayAuthProvider()
    raise AuthConfigurationError(f"Unknown AUTH_MODE: {resolved}")
