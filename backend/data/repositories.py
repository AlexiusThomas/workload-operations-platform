"""DynamoDB repositories: dual-write transactions and GSI-backed queries (Task 7.2, 7.3).

Implements the design's data access with strict guarantees:

- **Atomic dual-write** (Denormalized Projection Strategy): every WorkUnit /
  MaterialRequirement mutation writes the canonical item, the denormalized projection under
  the parent WorkPackage PK, and an immutable AuditEvent in a single ``TransactWriteItems``
  call (FR-017 AC-5, NFR-009 AC-1). Verification adds a ProductionEvent (4th item); material
  delivery adds a MaterialEvent (4th item).
- **AuditEvent key layout** ``PK=AUDIT#{entity_type}#{entity_id}``, ``SK=TS#{timestamp}#{uuid}``
  giving chronological ordering (design: Audit and Event Model).
- **Query access patterns 1-14** from the Access Pattern Summary, with native
  ``LastEvaluatedKey`` pagination exposed as a base64 ``next_token`` capped at 100 items
  (FR-005 AC-5, NFR-010 AC-1).

The module uses the low-level DynamoDB client (via ``table.meta.client``) for
``transact_write_items`` to match the design's transaction composition exactly, and the
Table resource for reads/queries. It is import-safe without AWS: all clients are obtained
through :mod:`backend.data.tables`, which resolves lazily.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple
from uuid import uuid4

from boto3.dynamodb.conditions import Key

from backend.data import tables

# ---------------------------------------------------------------------------
# Key prefixes and constants (design: wop-main-table / audit / events layouts)
# ---------------------------------------------------------------------------
METADATA_SK = "METADATA"

ENTITY_WORK_PACKAGE = "WORK_PACKAGE"
ENTITY_WORK_UNIT = "WORK_UNIT"
ENTITY_MATERIAL_REQUIREMENT = "MATERIAL_REQUIREMENT"

#: Max items returned per page (FR-005 AC-5).
MAX_PAGE_SIZE = 100

#: Non-terminal active WorkUnit states used for the stale-active-work query (FR-022 AC-7).
NON_TERMINAL_ACTIVE_STATES: Tuple[str, ...] = (
    "CLAIMED",
    "IN_PROGRESS",
    "PREP_COMPLETE",
    "LABELED",
    "TOTE_ASSIGNED",
    "READY_TO_VERIFY",
    "REWORK_REQUIRED",
)


def iso_now() -> str:
    """Return the current UTC time as an ISO-8601 millisecond string (``...Z``)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _to_dynamo(value: Any) -> Any:
    """Recursively convert Python ``float`` to :class:`~decimal.Decimal` for DynamoDB.

    The boto3 DynamoDB resource rejects native ``float`` (it requires ``Decimal``). Decimal
    attributes (e.g. cable_length_m, meters_delivered — design MaterialRequirement/MaterialEvent)
    are normalised here so callers may pass plain floats. ``bool`` is left untouched.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        # Round-trip through str to avoid binary-float artefacts in the stored Decimal.
        return Decimal(str(value))
    if isinstance(value, dict):
        return {key: _to_dynamo(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_dynamo(item) for item in value]
    return value


def wp_pk(work_package_id: str) -> str:
    """Partition key for a WorkPackage canonical item / children collection."""
    return f"WP#{work_package_id}"


def wu_pk(work_unit_id: str) -> str:
    """Partition key for a WorkUnit canonical item."""
    return f"WU#{work_unit_id}"


def mr_pk(material_req_id: str) -> str:
    """Partition key for a MaterialRequirement canonical item."""
    return f"MR#{material_req_id}"


def wu_child_sk(work_unit_id: str) -> str:
    """Sort key for a WorkUnit projection under its parent WorkPackage PK."""
    return f"WU#{work_unit_id}"


def mr_child_sk(material_req_id: str) -> str:
    """Sort key for a MaterialRequirement projection under its parent WorkPackage PK."""
    return f"MR#{material_req_id}"


def audit_pk(entity_type: str, entity_id: str) -> str:
    """Partition key for an AuditEvent (design: AuditEvent key layout)."""
    return f"AUDIT#{entity_type}#{entity_id}"


def audit_sk(timestamp: str) -> str:
    """Chronological sort key ``TS#{timestamp}#{uuid}`` for an AuditEvent."""
    return f"TS#{timestamp}#{uuid4()}"


# ---------------------------------------------------------------------------
# Pagination helpers
# ---------------------------------------------------------------------------
def encode_token(last_evaluated_key: Optional[Dict[str, Any]]) -> Optional[str]:
    """Encode a DynamoDB ``LastEvaluatedKey`` as a base64 ``next_token`` (or None)."""
    if not last_evaluated_key:
        return None
    raw = json.dumps(last_evaluated_key, default=str, sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_token(next_token: Optional[str]) -> Optional[Dict[str, Any]]:
    """Decode a base64 ``next_token`` back into a DynamoDB ``ExclusiveStartKey`` (or None)."""
    if not next_token:
        return None
    raw = base64.urlsafe_b64decode(next_token.encode("ascii"))
    decoded: Dict[str, Any] = json.loads(raw.decode("utf-8"))
    return decoded


def _page_limit(limit: Optional[int]) -> int:
    """Clamp a requested page size to ``[1, MAX_PAGE_SIZE]``."""
    if limit is None or limit > MAX_PAGE_SIZE:
        return MAX_PAGE_SIZE
    return max(1, limit)


# ---------------------------------------------------------------------------
# Audit event item builder (used inside every write transaction)
# ---------------------------------------------------------------------------
def build_audit_event(
    *,
    entity_type: str,
    entity_id: str,
    action_type: str,
    actor_id: str,
    actor_role: str,
    before_state: Optional[str],
    after_state: Optional[str],
    correlation_id: Optional[str],
    timestamp: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build an AuditEvent item for the audit table (chronological SK, immutable)."""
    item: Dict[str, Any] = {
        "pk": audit_pk(entity_type, entity_id),
        "sk": audit_sk(timestamp),
        "audit_event_id": str(uuid4()),
        "entity_type": entity_type,
        "entity_id": entity_id,
        "action_type": action_type,
        "actor_id": actor_id,
        "actor_role": actor_role,
        "before_state": before_state,
        "after_state": after_state,
        "metadata": metadata or {},
        "timestamp": timestamp,
        "correlation_id": correlation_id,
    }
    return item


# ---------------------------------------------------------------------------
# WorkUnit projection helper
# ---------------------------------------------------------------------------
def work_unit_projection(work_unit: Dict[str, Any]) -> Dict[str, Any]:
    """Return the projected attribute subset stored under the WorkPackage PK for a WorkUnit.

    Projected attributes (design: Denormalized Projection Strategy): state, site,
    work_type, current_scheduled_date, claimed_by, completed_qty, required_qty, tote_id,
    updated_at — plus the projection key and work_package_id for GSI-2.
    """
    work_package_id = work_unit["work_package_id"]
    work_unit_id = work_unit["work_unit_id"]
    return {
        "pk": wp_pk(work_package_id),
        "sk": wu_child_sk(work_unit_id),
        "work_package_id": work_package_id,
        "work_unit_id": work_unit_id,
        "entity_type": ENTITY_WORK_UNIT,
        "state": work_unit.get("state"),
        "site": work_unit.get("site"),
        "work_type": work_unit.get("work_type"),
        "current_scheduled_date": work_unit.get("current_scheduled_date"),
        "claimed_by": work_unit.get("claimed_by"),
        "completed_qty": work_unit.get("completed_qty"),
        "required_qty": work_unit.get("required_qty"),
        "tote_id": work_unit.get("tote_id"),
        "updated_at": work_unit.get("updated_at"),
    }


def material_requirement_projection(material_req: Dict[str, Any]) -> Dict[str, Any]:
    """Return the projected MaterialRequirement attributes stored under the WorkPackage PK.

    Note: the projection intentionally does NOT carry the ``status`` attribute that GSI-3
    (material-status-index) partitions on. Only the canonical item (SK=METADATA) participates
    in the Material Runner status queue; the projection carries ``proj_status`` for
    list-by-package reads so it never double-appears in the status queue.
    """
    work_package_id = material_req["work_package_id"]
    material_req_id = material_req["material_req_id"]
    return {
        "pk": wp_pk(work_package_id),
        "sk": mr_child_sk(material_req_id),
        "work_package_id": work_package_id,
        "material_req_id": material_req_id,
        "entity_type": ENTITY_MATERIAL_REQUIREMENT,
        "proj_status": material_req.get("status"),
        "material_type": material_req.get("material_type"),
        "qty_required": material_req.get("qty_required"),
        "qty_delivered": material_req.get("qty_delivered"),
        "meters_delivered": material_req.get("meters_delivered"),
        "last_updated_at": material_req.get("last_updated_at"),
    }


# ---------------------------------------------------------------------------
# Reads (Access Patterns 1, 2, 4, 7)
# ---------------------------------------------------------------------------
def get_work_package(work_package_id: str) -> Optional[Dict[str, Any]]:
    """Access Pattern 1: get a WorkPackage canonical item by id."""
    resp = tables.main_table().get_item(Key={"pk": wp_pk(work_package_id), "sk": METADATA_SK})
    item: Optional[Dict[str, Any]] = resp.get("Item")
    return item


def get_work_unit(work_unit_id: str) -> Optional[Dict[str, Any]]:
    """Access Pattern 2: get a WorkUnit canonical item by id."""
    resp = tables.main_table().get_item(Key={"pk": wu_pk(work_unit_id), "sk": METADATA_SK})
    item: Optional[Dict[str, Any]] = resp.get("Item")
    return item


def get_material_requirement(material_req_id: str) -> Optional[Dict[str, Any]]:
    """Get a MaterialRequirement canonical item by id."""
    resp = tables.main_table().get_item(Key={"pk": mr_pk(material_req_id), "sk": METADATA_SK})
    item: Optional[Dict[str, Any]] = resp.get("Item")
    return item


def list_work_units_by_package(work_package_id: str) -> List[Dict[str, Any]]:
    """Access Pattern 4: list WorkUnit projections for a WorkPackage (query on parent PK)."""
    resp = tables.main_table().query(
        KeyConditionExpression=Key("pk").eq(wp_pk(work_package_id)) & Key("sk").begins_with("WU#")
    )
    return list(resp.get("Items", []))


def list_material_requirements_by_package(work_package_id: str) -> List[Dict[str, Any]]:
    """Access Pattern 7: list MaterialRequirement projections for a WorkPackage."""
    resp = tables.main_table().query(
        KeyConditionExpression=Key("pk").eq(wp_pk(work_package_id)) & Key("sk").begins_with("MR#")
    )
    return list(resp.get("Items", []))


# ---------------------------------------------------------------------------
# GSI-backed paginated queries (Access Patterns 3, 5, 8, 6, 9-12, 14)
# ---------------------------------------------------------------------------
def _paginated_query(
    table: Any,
    *,
    index_name: Optional[str],
    key_condition: Any,
    next_token: Optional[str],
    limit: Optional[int],
    scan_forward: bool = True,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Run a paginated DynamoDB query and return (items, next_token)."""
    kwargs: Dict[str, Any] = {
        "KeyConditionExpression": key_condition,
        "Limit": _page_limit(limit),
        "ScanIndexForward": scan_forward,
    }
    if index_name:
        kwargs["IndexName"] = index_name
    start_key = decode_token(next_token)
    if start_key:
        kwargs["ExclusiveStartKey"] = start_key
    resp = table.query(**kwargs)
    return list(resp.get("Items", [])), encode_token(resp.get("LastEvaluatedKey"))


def query_work_units_by_state(
    state: str,
    *,
    site: Optional[str] = None,
    work_type: Optional[str] = None,
    current_scheduled_date: Optional[str] = None,
    next_token: Optional[str] = None,
    limit: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Access Patterns 3 & 5: queue by state, optionally filtered by site/work_type/date.

    Uses GSI-1 (``state-site-worktype-date-index``): PK=state, SK composite
    ``site#work_type#current_scheduled_date`` supporting ``begins_with`` narrowing
    (FR-005 AC-1, AC-2, AC-5).
    """
    key_condition = Key("state").eq(state)
    # Build a left-anchored SK prefix from the provided filters (in composite order).
    prefix_parts: List[str] = []
    if site is not None:
        prefix_parts.append(site)
        if work_type is not None:
            prefix_parts.append(work_type)
            if current_scheduled_date is not None:
                prefix_parts.append(current_scheduled_date)
    if prefix_parts:
        prefix = "#".join(prefix_parts)
        key_condition = key_condition & Key("sk_gsi1").begins_with(prefix)
    return _paginated_query(
        tables.main_table(),
        index_name="state-site-worktype-date-index",
        key_condition=key_condition,
        next_token=next_token,
        limit=limit,
    )


def query_material_requirements_by_status(
    status: str,
    *,
    next_token: Optional[str] = None,
    limit: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Access Pattern 8: list MaterialRequirements by status (GSI-3 material-status-index)."""
    return _paginated_query(
        tables.main_table(),
        index_name="material-status-index",
        key_condition=Key("status").eq(status),
        next_token=next_token,
        limit=limit,
    )


def query_audit_events(
    entity_type: str,
    entity_id: str,
    *,
    next_token: Optional[str] = None,
    limit: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Access Pattern 6: list AuditEvents for an entity in chronological order (ascending SK)."""
    return _paginated_query(
        tables.audit_table(),
        index_name=None,
        key_condition=Key("pk").eq(audit_pk(entity_type, entity_id)),
        next_token=next_token,
        limit=limit,
        scan_forward=True,
    )


def query_production_events_by_week(
    week_key: str,
    *,
    next_token: Optional[str] = None,
    limit: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Access Pattern 9: list ProductionEvents by week_key (GSI-4 production-week-index)."""
    return _paginated_query(
        tables.events_table(),
        index_name="production-week-index",
        key_condition=Key("event_type#week_key").eq(f"PE#{week_key}"),
        next_token=next_token,
        limit=limit,
    )


def query_material_events_by_week(
    week_key: str,
    *,
    next_token: Optional[str] = None,
    limit: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Access Pattern 10: list MaterialEvents by week_key (GSI-5 material-week-index)."""
    return _paginated_query(
        tables.events_table(),
        index_name="material-week-index",
        key_condition=Key("event_type#week_key").eq(f"ME#{week_key}"),
        next_token=next_token,
        limit=limit,
    )


def query_production_events_by_technician_week(
    technician_id: str,
    week_key: str,
    *,
    next_token: Optional[str] = None,
    limit: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Access Pattern 11: ProductionEvents by technician + week (GSI-6 technician-week-index)."""
    return _paginated_query(
        tables.events_table(),
        index_name="technician-week-index",
        key_condition=Key("technician_id").eq(technician_id)
        & Key("week_key#timestamp").begins_with(week_key),
        next_token=next_token,
        limit=limit,
    )


def query_material_events_by_runner_week(
    runner_id: str,
    week_key: str,
    *,
    next_token: Optional[str] = None,
    limit: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Access Pattern 12: MaterialEvents by runner + week (GSI-7 runner-week-index)."""
    return _paginated_query(
        tables.events_table(),
        index_name="runner-week-index",
        key_condition=Key("runner_id").eq(runner_id)
        & Key("week_key#timestamp").begins_with(week_key),
        next_token=next_token,
        limit=limit,
    )


def query_stale_active_work(
    today: str,
    *,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Access Pattern 14: stale active WorkUnits past their current_scheduled_date.

    Queries GSI-1 once per non-terminal active state and merges results in-process; DynamoDB
    cannot inequality-filter a composite SK across all positions at once (design note). The
    date filter is applied in-process against ``current_scheduled_date < today``
    (FR-022 AC-7). Bounded at V1 volumes (AS-002).
    """
    merged: List[Dict[str, Any]] = []
    for state in NON_TERMINAL_ACTIVE_STATES:
        items, _ = _paginated_query(
            tables.main_table(),
            index_name="state-site-worktype-date-index",
            key_condition=Key("state").eq(state),
            next_token=None,
            limit=limit,
        )
        for item in items:
            scheduled = item.get("current_scheduled_date")
            if scheduled is not None and str(scheduled) < today:
                merged.append(item)
    return merged


# ---------------------------------------------------------------------------
# Transactional writes (Task 7.2)
# ---------------------------------------------------------------------------
def _put(table_name: str, item: Dict[str, Any], condition: Optional[str] = None) -> Dict[str, Any]:
    """Build a TransactWriteItems Put action for the resource-style transact API."""
    put: Dict[str, Any] = {"TableName": table_name, "Item": _to_dynamo(item)}
    if condition:
        put["ConditionExpression"] = condition
    return {"Put": put}


def _transact_write(items: Sequence[Dict[str, Any]]) -> None:
    """Execute a TransactWriteItems call via the main table's low-level client.

    The resource-level ``transact_write_items`` accepts native Python types (no
    ``{"S": ...}`` wrapping), matching the item dicts built above.
    """
    client = tables.main_table().meta.client
    client.transact_write_items(TransactItems=list(items))


def create_work_package(
    work_package: Dict[str, Any],
    *,
    actor_id: str,
    actor_role: str,
    correlation_id: Optional[str],
    action_type: str = "WORK_PACKAGE_CREATED",
) -> None:
    """Create a WorkPackage canonical item + AuditEvent atomically (FR-002 AC-6, FR-017 AC-5)."""
    ts = iso_now()
    canonical = dict(work_package)
    canonical.setdefault("pk", wp_pk(work_package["work_package_id"]))
    canonical.setdefault("sk", METADATA_SK)

    audit = build_audit_event(
        entity_type=ENTITY_WORK_PACKAGE,
        entity_id=work_package["work_package_id"],
        action_type=action_type,
        actor_id=actor_id,
        actor_role=actor_role,
        before_state=None,
        after_state=canonical.get("state"),
        correlation_id=correlation_id,
        timestamp=ts,
    )
    _transact_write(
        [
            _put(tables.main_table().table_name, canonical, "attribute_not_exists(pk)"),
            _put(tables.audit_table().table_name, audit, "attribute_not_exists(pk)"),
        ]
    )


def create_work_package_with_units(
    work_package: Dict[str, Any],
    work_units: Sequence[Dict[str, Any]],
    *,
    actor_id: str,
    actor_role: str,
    correlation_id: Optional[str],
) -> None:
    """Create a WorkPackage and its WorkUnits with one AuditEvent per created entity.

    Used by the manual ``POST /v1/work-packages`` admin path (FR-002 AC-5, AC-6). The
    WorkPackage canonical + audit and each WorkUnit's canonical + projection + audit are all
    written in a single TransactWriteItems so creation is atomic (FR-017 AC-5). All Puts use
    ``attribute_not_exists(pk)`` so a repeated create with the same ids fails cleanly.
    """
    ts = iso_now()
    wp = dict(work_package)
    wp.setdefault("pk", wp_pk(wp["work_package_id"]))
    wp.setdefault("sk", METADATA_SK)

    transact_items: List[Dict[str, Any]] = [
        _put(tables.main_table().table_name, wp, "attribute_not_exists(pk)"),
        _put(
            tables.audit_table().table_name,
            build_audit_event(
                entity_type=ENTITY_WORK_PACKAGE,
                entity_id=wp["work_package_id"],
                action_type="WORK_PACKAGE_CREATED",
                actor_id=actor_id,
                actor_role=actor_role,
                before_state=None,
                after_state=wp.get("state"),
                correlation_id=correlation_id,
                timestamp=ts,
            ),
            "attribute_not_exists(pk)",
        ),
    ]
    for work_unit in work_units:
        canonical = dict(work_unit)
        canonical.setdefault("pk", wu_pk(canonical["work_unit_id"]))
        canonical.setdefault("sk", METADATA_SK)
        transact_items.append(
            _put(tables.main_table().table_name, canonical, "attribute_not_exists(pk)")
        )
        transact_items.append(_put(tables.main_table().table_name, work_unit_projection(canonical)))
        transact_items.append(
            _put(
                tables.audit_table().table_name,
                build_audit_event(
                    entity_type=ENTITY_WORK_UNIT,
                    entity_id=canonical["work_unit_id"],
                    action_type="WORK_UNIT_CREATED",
                    actor_id=actor_id,
                    actor_role=actor_role,
                    before_state=None,
                    after_state=canonical.get("state"),
                    correlation_id=correlation_id,
                    timestamp=ts,
                ),
                "attribute_not_exists(pk)",
            )
        )
    _transact_write(transact_items)


def list_work_packages(
    *,
    site: Optional[str] = None,
    work_type: Optional[str] = None,
    current_scheduled_date: Optional[str] = None,
    next_token: Optional[str] = None,
    limit: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """List WorkPackage canonical items with optional filters and pagination.

    V1 has no dedicated list-all-packages GSI (the Access Pattern Summary is queue-first);
    this uses a paginated scan restricted to canonical WorkPackage items (SK=METADATA,
    PK begins with ``WP#``). Bounded at V1 volumes (AS-002). Filters are applied server-side.
    """
    from boto3.dynamodb.conditions import Attr

    filter_expr = Attr("sk").eq(METADATA_SK) & Attr("pk").begins_with("WP#")
    if site is not None:
        filter_expr = filter_expr & Attr("site").eq(site)
    if work_type is not None:
        filter_expr = filter_expr & Attr("work_type").eq(work_type)
    if current_scheduled_date is not None:
        filter_expr = filter_expr & Attr("current_scheduled_date").eq(current_scheduled_date)

    kwargs: Dict[str, Any] = {"FilterExpression": filter_expr, "Limit": _page_limit(limit)}
    start_key = decode_token(next_token)
    if start_key:
        kwargs["ExclusiveStartKey"] = start_key
    resp = tables.main_table().scan(**kwargs)
    return list(resp.get("Items", [])), encode_token(resp.get("LastEvaluatedKey"))


def write_work_unit_transition(
    canonical_item: Dict[str, Any],
    *,
    action_type: str,
    actor_id: str,
    actor_role: str,
    before_state: Optional[str],
    after_state: Optional[str],
    correlation_id: Optional[str],
    condition_expression: Optional[str] = None,
    expression_attribute_names: Optional[Dict[str, str]] = None,
    expression_attribute_values: Optional[Dict[str, Any]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    production_event: Optional[Dict[str, Any]] = None,
) -> None:
    """Write a WorkUnit state transition atomically (design: Concurrency and Atomic Operations).

    Always writes three items — canonical WorkUnit Put, denormalized projection Put, and an
    immutable AuditEvent Put — in a single TransactWriteItems (FR-017 AC-5, NFR-009 AC-1,
    BR-003 preservation is the caller's responsibility via ``canonical_item``). When
    ``production_event`` is provided (verification), a 4th ProductionEvent Put is added.

    The canonical Put may carry a ConditionExpression (e.g. ``state = AVAILABLE``) to make
    the transition a conditional write; a failed condition raises the underlying
    ``TransactionCanceledException`` for the caller to translate into a 409 (FR-006 AC-2).
    """
    ts = canonical_item.get("updated_at") or iso_now()
    canonical = dict(canonical_item)
    canonical.setdefault("pk", wu_pk(canonical_item["work_unit_id"]))
    canonical.setdefault("sk", METADATA_SK)

    canonical_put: Dict[str, Any] = {
        "TableName": tables.main_table().table_name,
        "Item": _to_dynamo(canonical),
    }
    if condition_expression:
        canonical_put["ConditionExpression"] = condition_expression
    if expression_attribute_names:
        canonical_put["ExpressionAttributeNames"] = expression_attribute_names
    if expression_attribute_values:
        canonical_put["ExpressionAttributeValues"] = _to_dynamo(expression_attribute_values)

    projection = work_unit_projection(canonical)
    audit = build_audit_event(
        entity_type=ENTITY_WORK_UNIT,
        entity_id=canonical_item["work_unit_id"],
        action_type=action_type,
        actor_id=actor_id,
        actor_role=actor_role,
        before_state=before_state,
        after_state=after_state,
        correlation_id=correlation_id,
        timestamp=ts,
        metadata=metadata,
    )

    transact_items: List[Dict[str, Any]] = [
        {"Put": canonical_put},
        _put(tables.main_table().table_name, projection),
        _put(tables.audit_table().table_name, audit, "attribute_not_exists(pk)"),
    ]
    if production_event is not None:
        pe = dict(production_event)
        pe.setdefault("pk", f"PE#{pe['production_event_id']}")
        pe.setdefault("sk", METADATA_SK)
        transact_items.append(_put(tables.events_table().table_name, pe))

    _transact_write(transact_items)


def update_work_package_fields(
    work_package_id: str,
    *,
    fields: Dict[str, Any],
    action_type: str,
    actor_id: str,
    actor_role: str,
    before_state: Optional[str],
    after_state: Optional[str],
    correlation_id: Optional[str],
    condition_expression: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """Atomically update selected WorkPackage attributes and write an AuditEvent.

    Used by ingestion Phase 2 to flip the WorkPackage ``committed`` flag and ``state`` as
    the package-level visibility gate, and by the failure path to set ``INGESTION_ERROR``
    (design: Event Write Atomicity → Ingestion). The canonical Update and the AuditEvent
    Put are written in a single TransactWriteItems (FR-017 AC-5).

    Args:
        work_package_id: The WorkPackage id.
        fields: Attribute name -> value pairs to SET on the canonical item.
        action_type: The AuditEvent action_type.
        actor_id: Server-verified actor id (``SYSTEM`` for ingestion).
        actor_role: Actor role.
        before_state / after_state: Recorded on the AuditEvent.
        correlation_id: Request correlation id.
        condition_expression: Optional condition on the canonical Update (raw expression).
        metadata: Optional AuditEvent metadata.
    """
    ts = iso_now()
    fields = dict(fields)
    fields.setdefault("updated_at", ts)

    set_clauses: List[str] = []
    names: Dict[str, str] = {}
    values: Dict[str, Any] = {}
    for idx, (attr, value) in enumerate(fields.items()):
        name_token = f"#f{idx}"
        value_token = f":v{idx}"
        names[name_token] = attr
        values[value_token] = value
        set_clauses.append(f"{name_token} = {value_token}")
    update_expression = "SET " + ", ".join(set_clauses)

    update_action: Dict[str, Any] = {
        "TableName": tables.main_table().table_name,
        "Key": {"pk": wp_pk(work_package_id), "sk": METADATA_SK},
        "UpdateExpression": update_expression,
        "ExpressionAttributeNames": names,
        "ExpressionAttributeValues": _to_dynamo(values),
    }
    if condition_expression:
        update_action["ConditionExpression"] = condition_expression

    audit = build_audit_event(
        entity_type=ENTITY_WORK_PACKAGE,
        entity_id=work_package_id,
        action_type=action_type,
        actor_id=actor_id,
        actor_role=actor_role,
        before_state=before_state,
        after_state=after_state,
        correlation_id=correlation_id,
        timestamp=ts,
        metadata=metadata,
    )
    _transact_write(
        [
            {"Update": update_action},
            _put(tables.audit_table().table_name, audit, "attribute_not_exists(pk)"),
        ]
    )


def stage_work_package_with_units(
    work_package: Dict[str, Any],
    work_units: Sequence[Dict[str, Any]],
    *,
    actor_id: str,
    actor_role: str,
    correlation_id: Optional[str],
    batch_size: int = 25,
) -> None:
    """Ingestion Phase 1: stage a WorkPackage (INGESTING) and its WorkUnits (STAGING).

    Writes the WorkPackage canonical item plus a creation AuditEvent, then each WorkUnit's
    canonical item + denormalized projection + creation AuditEvent, all via
    ``TransactWriteItems`` batches of at most ``batch_size`` DynamoDB actions
    (design: two-phase staging/commit). Staged entities are invisible to the operational
    queue because GSI-1 is queried on ``state=AVAILABLE`` and the ``committed`` flag on the
    WorkPackage is ``false`` (FR-001 AC-1, AC-7, FR-002 AC-1, AC-2, AC-6).

    Each transaction Put uses ``attribute_not_exists(pk)`` so re-staging an existing
    WorkPackage id fails the condition (idempotent no-op handled by the caller).
    """
    ts = iso_now()
    wp = dict(work_package)
    wp.setdefault("pk", wp_pk(wp["work_package_id"]))
    wp.setdefault("sk", METADATA_SK)

    wp_audit = build_audit_event(
        entity_type=ENTITY_WORK_PACKAGE,
        entity_id=wp["work_package_id"],
        action_type="INGESTION_WORK_PACKAGE_STAGED",
        actor_id=actor_id,
        actor_role=actor_role,
        before_state=None,
        after_state=wp.get("state"),
        correlation_id=correlation_id,
        timestamp=ts,
    )

    # First transaction: WorkPackage canonical + its staging AuditEvent.
    _transact_write(
        [
            _put(tables.main_table().table_name, wp, "attribute_not_exists(pk)"),
            _put(tables.audit_table().table_name, wp_audit, "attribute_not_exists(pk)"),
        ]
    )

    # Then WorkUnits in batches. Each WorkUnit contributes 3 actions
    # (canonical + projection + AuditEvent); pack whole units into a batch so a WorkUnit is
    # never split across two transactions.
    units_per_batch = max(1, batch_size // 3)
    batch: List[Dict[str, Any]] = []
    for work_unit in work_units:
        canonical = dict(work_unit)
        canonical.setdefault("pk", wu_pk(canonical["work_unit_id"]))
        canonical.setdefault("sk", METADATA_SK)
        projection = work_unit_projection(canonical)
        wu_audit = build_audit_event(
            entity_type=ENTITY_WORK_UNIT,
            entity_id=canonical["work_unit_id"],
            action_type="WORK_UNIT_CREATED",
            actor_id=actor_id,
            actor_role=actor_role,
            before_state=None,
            after_state=canonical.get("state"),
            correlation_id=correlation_id,
            timestamp=ts,
        )
        batch.extend(
            [
                _put(tables.main_table().table_name, canonical, "attribute_not_exists(pk)"),
                _put(tables.main_table().table_name, projection),
                _put(tables.audit_table().table_name, wu_audit, "attribute_not_exists(pk)"),
            ]
        )
        if len(batch) >= units_per_batch * 3:
            _transact_write(batch)
            batch = []
    if batch:
        _transact_write(batch)


def commit_staged_work_unit(
    work_unit: Dict[str, Any],
    *,
    actor_id: str,
    actor_role: str,
    correlation_id: Optional[str],
) -> None:
    """Ingestion Phase 2: transition one STAGING WorkUnit to AVAILABLE atomically.

    Rewrites the canonical WorkUnit (now ``state=AVAILABLE`` with its GSI-1 ``sk_gsi1``
    populated) + denormalized projection + ``INGESTION_WORK_UNIT_COMMITTED`` AuditEvent in a
    single TransactWriteItems, conditional on the canonical item still being ``STAGING``
    (design: two-phase commit). The WorkUnit remains suppressed from the queue until the
    parent WorkPackage ``committed`` flag is set (visibility gate).
    """
    canonical = dict(work_unit)
    canonical["state"] = "AVAILABLE"
    canonical["updated_at"] = iso_now()
    write_work_unit_transition(
        canonical,
        action_type="INGESTION_WORK_UNIT_COMMITTED",
        actor_id=actor_id,
        actor_role=actor_role,
        before_state="STAGING",
        after_state="AVAILABLE",
        correlation_id=correlation_id,
        condition_expression="#state = :staging",
        expression_attribute_names={"#state": "state"},
        expression_attribute_values={":staging": "STAGING"},
    )


def write_rollover_transition(
    canonical_item: Dict[str, Any],
    *,
    previous_date: str,
    new_date: str,
    new_rollover_count: int,
    actor_id: str,
    actor_role: str,
    correlation_id: Optional[str],
) -> None:
    """Roll one AVAILABLE WorkUnit forward atomically (design: Rollover TransactWriteItems).

    Writes three items in a single ``TransactWriteItems`` (FR-017 AC-5): the canonical
    WorkUnit (with ``current_scheduled_date`` advanced, ``rollover_count`` incremented, and
    the refreshed GSI-1 key), the denormalized projection under the parent WorkPackage PK,
    and an immutable ``WORK_UNIT_ROLLED_OVER`` AuditEvent.

    The canonical Put carries a ConditionExpression requiring the WorkUnit still be
    ``state=AVAILABLE`` AND ``current_scheduled_date = previous_date``. If the unit has been
    claimed since it was queried (BR-014), or the date was already advanced by a prior run
    of the same cycle (FR-022 AC-8 idempotency guard), the condition fails and DynamoDB
    raises ``TransactionCanceledException`` for the caller to skip silently.

    The state is unchanged (AVAILABLE → AVAILABLE); ``completed_qty`` and
    ``original_scheduled_date`` are preserved by the caller supplying an unmodified
    ``canonical_item`` copy (FR-022 AC-4, AC-5, BR-015). Actor is ``SYSTEM``.
    """
    ts = iso_now()
    canonical = dict(canonical_item)
    canonical.setdefault("pk", wu_pk(canonical_item["work_unit_id"]))
    canonical.setdefault("sk", METADATA_SK)
    canonical["current_scheduled_date"] = new_date
    canonical["rollover_count"] = new_rollover_count
    canonical["updated_at"] = ts
    # Refresh the GSI-1 composite sort key so the queue reflects the new scheduled date.
    canonical["sk_gsi1"] = f"{canonical.get('site')}#{canonical.get('work_type')}#{new_date}"

    canonical_put: Dict[str, Any] = {
        "TableName": tables.main_table().table_name,
        "Item": _to_dynamo(canonical),
        "ConditionExpression": (
            "#state = :available AND current_scheduled_date = :expected_old_date"
        ),
        "ExpressionAttributeNames": {"#state": "state"},
        "ExpressionAttributeValues": _to_dynamo(
            {":available": "AVAILABLE", ":expected_old_date": previous_date}
        ),
    }

    projection = work_unit_projection(canonical)
    projection["current_scheduled_date"] = new_date
    audit = build_audit_event(
        entity_type=ENTITY_WORK_UNIT,
        entity_id=canonical_item["work_unit_id"],
        action_type="WORK_UNIT_ROLLED_OVER",
        actor_id=actor_id,
        actor_role=actor_role,
        before_state="AVAILABLE",
        after_state="AVAILABLE",
        correlation_id=correlation_id,
        timestamp=ts,
        metadata={
            "previous_date": previous_date,
            "new_date": new_date,
            "new_rollover_count": new_rollover_count,
        },
    )

    _transact_write(
        [
            {"Put": canonical_put},
            _put(tables.main_table().table_name, projection),
            _put(tables.audit_table().table_name, audit, "attribute_not_exists(pk)"),
        ]
    )


def write_material_transition(
    canonical_item: Dict[str, Any],
    *,
    action_type: str,
    actor_id: str,
    actor_role: str,
    before_state: Optional[str],
    after_state: Optional[str],
    correlation_id: Optional[str],
    condition_expression: Optional[str] = None,
    expression_attribute_names: Optional[Dict[str, str]] = None,
    expression_attribute_values: Optional[Dict[str, Any]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    material_event: Optional[Dict[str, Any]] = None,
) -> None:
    """Write a MaterialRequirement status transition atomically (design: material claim/deliver).

    Always writes canonical + projection + AuditEvent; material delivery adds a 4th
    MaterialEvent Put (FR-004 AC-4). Conditional writes behave as in
    :func:`write_work_unit_transition`.
    """
    ts = canonical_item.get("last_updated_at") or iso_now()
    canonical = dict(canonical_item)
    canonical.setdefault("pk", mr_pk(canonical_item["material_req_id"]))
    canonical.setdefault("sk", METADATA_SK)

    canonical_put: Dict[str, Any] = {
        "TableName": tables.main_table().table_name,
        "Item": _to_dynamo(canonical),
    }
    if condition_expression:
        canonical_put["ConditionExpression"] = condition_expression
    if expression_attribute_names:
        canonical_put["ExpressionAttributeNames"] = expression_attribute_names
    if expression_attribute_values:
        canonical_put["ExpressionAttributeValues"] = _to_dynamo(expression_attribute_values)

    projection = material_requirement_projection(canonical)
    audit = build_audit_event(
        entity_type=ENTITY_MATERIAL_REQUIREMENT,
        entity_id=canonical_item["material_req_id"],
        action_type=action_type,
        actor_id=actor_id,
        actor_role=actor_role,
        before_state=before_state,
        after_state=after_state,
        correlation_id=correlation_id,
        timestamp=ts,
        metadata=metadata,
    )

    transact_items: List[Dict[str, Any]] = [
        {"Put": canonical_put},
        _put(tables.main_table().table_name, projection),
        _put(tables.audit_table().table_name, audit, "attribute_not_exists(pk)"),
    ]
    if material_event is not None:
        me = dict(material_event)
        me.setdefault("pk", f"ME#{me['material_event_id']}")
        me.setdefault("sk", METADATA_SK)
        transact_items.append(_put(tables.events_table().table_name, me))

    _transact_write(transact_items)
