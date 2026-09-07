"""Authorization middleware: authentication, RBAC, and ownership checks.

Provides :func:`authenticate` (resolves a :class:`~backend.auth.provider.UserContext` from
the request via the injected :class:`~backend.auth.provider.AuthProvider`) and
:func:`authorize` (enforces the design's RBAC matrix server-side, SEC-002 AC-1).

Identity is resolved exclusively from server-side session context (FR-018 AC-1, BR-008).
Ownership checks implement the design's Ownership Rules, including Rule 4: for
label / tote-assign / ready-to-verify a Technician must be the claiming Technician, while
Verifier_Lead and Manager_Admin bypass the ownership check.

FR-018 AC-3 note: No V1 endpoint accepts a client-supplied *actor* identity — the actor is
always resolved server-side from the authenticated bearer token via :func:`authenticate`.
The only client-supplied identity in the V1 API is the assignment *target*
(``technician_id`` on ``POST /assign``), which is not an actor identity. There is therefore
no client-vs-session actor mismatch to detect in V1, and no IDENTITY_MISMATCH_WARNING is
emitted. If a future endpoint accepts a client-supplied actor identity, add an explicit
mismatch check at that call site so the server-resolved identity remains authoritative.

This module is import-safe without AWS credentials.
"""

from __future__ import annotations

from typing import Dict, Iterable, Optional

from backend.auth.provider import (
    MANAGER_ADMIN,
    MATERIAL_RUNNER,
    TECHNICIAN,
    VERIFIER_LEAD,
    AuthProvider,
    UserContext,
)
from backend.observability.errors import AuthorizationError

#: Roles that bypass WorkUnit ownership checks for label/tote/ready-to-verify (Rule 4).
_OWNERSHIP_BYPASS_ROLES = frozenset({VERIFIER_LEAD, MANAGER_ADMIN})


def authenticate(event: Dict, provider: AuthProvider) -> UserContext:
    """Resolve the caller's :class:`UserContext` from the request (FR-018 AC-1).

    Args:
        event: The Lambda/API Gateway event.
        provider: The configured auth provider (injected; never global state).

    Returns:
        The authenticated :class:`UserContext`.

    Raises:
        AuthenticationError: If the token is absent or invalid (HTTP 401).
    """
    return provider.authenticate(event)


def authorize(user_ctx: UserContext, required_roles: Iterable[str]) -> None:
    """Enforce role-based access control server-side (FR-019 AC-1, SEC-002 AC-1).

    Args:
        user_ctx: The authenticated user.
        required_roles: The roles permitted to perform the action.

    Raises:
        AuthorizationError: If the user's role is not in ``required_roles`` (HTTP 403,
            no resource information — SEC-002 AC-3).
    """
    if user_ctx.role not in set(required_roles):
        raise AuthorizationError("role not permitted for this action")


def require_owner(user_ctx: UserContext, claimed_by: Optional[str]) -> None:
    """Enforce strict ownership: the actor must be the claiming Technician.

    Applies to start / update-quantity / prep-complete (Ownership Rule 1). No role bypass.

    Raises:
        AuthorizationError: If ``user_ctx.user_id != claimed_by``.
    """
    if claimed_by is None or user_ctx.user_id != claimed_by:
        raise AuthorizationError("actor is not the claiming Technician")


def require_owner_or_lead_admin(user_ctx: UserContext, claimed_by: Optional[str]) -> None:
    """Enforce Ownership Rule 4 for label / tote-assign / ready-to-verify.

    Technician actors must be the claiming Technician (``claimed_by``). Verifier_Lead and
    Manager_Admin bypass the ownership check entirely.

    Raises:
        AuthorizationError: If a Technician actor does not own the WorkUnit.
    """
    if user_ctx.role in _OWNERSHIP_BYPASS_ROLES:
        return
    require_owner(user_ctx, claimed_by)


def require_runner_match(user_ctx: UserContext, runner_id: Optional[str]) -> None:
    """Enforce Ownership Rule 2: the delivering runner must match the claiming runner.

    Manager_Admin bypasses the match (mirrors the RBAC matrix material-deliver row).

    Raises:
        AuthorizationError: If a Material_Runner actor is not the runner who claimed.
    """
    if user_ctx.role == MANAGER_ADMIN:
        return
    if runner_id is None or user_ctx.user_id != runner_id:
        raise AuthorizationError("actor is not the claiming Material Runner")


# ---------------------------------------------------------------------------
# RBAC matrix (design: Authorization Model → RBAC Matrix). Maps a logical operation
# name to the set of roles permitted to invoke it. Ownership is enforced separately by
# the require_* helpers above after the role check passes.
# ---------------------------------------------------------------------------
_ALL_ROLES = frozenset({TECHNICIAN, MATERIAL_RUNNER, VERIFIER_LEAD, MANAGER_ADMIN})

RBAC_MATRIX: Dict[str, frozenset] = {
    "view_queue": _ALL_ROLES,
    "get_work_unit": _ALL_ROLES,
    "get_work_package": _ALL_ROLES,
    "view_material_requirements": _ALL_ROLES,
    "create_work_package": frozenset({MANAGER_ADMIN}),
    "create_material_requirement": frozenset({MANAGER_ADMIN}),
    "claim": frozenset({TECHNICIAN, VERIFIER_LEAD, MANAGER_ADMIN}),
    "rework_claim": frozenset({TECHNICIAN}),
    "start": frozenset({TECHNICIAN}),
    "quantity": frozenset({TECHNICIAN}),
    "prep_complete": frozenset({TECHNICIAN}),
    "label": frozenset({TECHNICIAN, VERIFIER_LEAD, MANAGER_ADMIN}),
    "tote_assign": frozenset({TECHNICIAN, VERIFIER_LEAD, MANAGER_ADMIN}),
    "ready_to_verify": frozenset({TECHNICIAN, VERIFIER_LEAD, MANAGER_ADMIN}),
    "verify": frozenset({VERIFIER_LEAD, MANAGER_ADMIN}),
    "fail_verify": frozenset({VERIFIER_LEAD, MANAGER_ADMIN}),
    "complete": frozenset({VERIFIER_LEAD, MANAGER_ADMIN}),
    "assign": frozenset({VERIFIER_LEAD, MANAGER_ADMIN}),
    "material_claim": frozenset({MATERIAL_RUNNER, MANAGER_ADMIN}),
    "material_deliver": frozenset({MATERIAL_RUNNER, MANAGER_ADMIN}),
    "view_audit": frozenset({VERIFIER_LEAD, MANAGER_ADMIN}),
    "trigger_report": frozenset({MANAGER_ADMIN}),
    "view_report": frozenset({VERIFIER_LEAD, MANAGER_ADMIN}),
    "ingest": frozenset({MANAGER_ADMIN}),
    "view_stale": frozenset({VERIFIER_LEAD, MANAGER_ADMIN}),
}


def authorize_operation(user_ctx: UserContext, operation: str) -> None:
    """Authorize a named operation against the RBAC matrix.

    Args:
        user_ctx: The authenticated user.
        operation: A key in :data:`RBAC_MATRIX`.

    Raises:
        AuthorizationError: If the operation is unknown or the role is not permitted.
    """
    permitted = RBAC_MATRIX.get(operation)
    if permitted is None:
        raise AuthorizationError("unknown operation")
    authorize(user_ctx, permitted)
