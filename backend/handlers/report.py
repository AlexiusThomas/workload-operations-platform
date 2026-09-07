"""Weekly production summary report handler — ``wop-report-handler`` (Task 16.1).

Implements the report endpoints from the design's API Design section and the EventBridge
weekly-schedule entrypoint:

- ``GET  /v1/reports/weekly?week_key=YYYY-Wnn`` — LEAD/ADMIN read (``view_report``).
- ``POST /v1/reports/weekly`` (body ``{"week_key": "..."}``) — ADMIN on-demand trigger
  (``trigger_report``).
- EventBridge weekly schedule — invoked via :func:`handle` with a ``week_key`` in the event.

All report figures are derived **exclusively** from immutable ProductionEvents and
MaterialEvents; no figure is read from mutable WorkUnit or MaterialRequirement state
(FR-024 AC-2, BR-016). Because the underlying events are immutable and append-only, the
report is deterministic given the same week (FR-024 AC-4).

Technician production figures and Material Runner figures appear in **separate** response
sections (``technician_production`` and ``material_runner_deliveries``) and are never
aggregated into a single combined productivity metric (BR-011, BR-016, FR-024 AC-3).

Report contents (FR-024 AC-1):
    - verified WorkUnit counts by Work_Type and by site (from ProductionEvents),
    - total meters verified by material type,
    - verified quantities by Technician (from ProductionEvents),
    - material runs completed and meters delivered by Material_Runner (from MaterialEvents).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from backend.auth import middleware as auth
from backend.data import repositories as repo
from backend.handlers import common
from backend.handlers.middleware import error_handler
from backend.observability import errors, metrics


def _num(value: Any) -> float:
    """Coerce a DynamoDB numeric (Decimal/int/float/None) to float for aggregation."""
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def _int(value: Any) -> int:
    """Coerce a DynamoDB numeric to int for count/quantity aggregation."""
    if value is None:
        return 0
    if isinstance(value, Decimal):
        return int(value)
    return int(value)


def _fetch_all(query: Any, week_key: str) -> List[Dict[str, Any]]:
    """Page through a week-scoped event query and return every item.

    ``query`` is one of the repository week queries (``query_..._by_week``) taking a
    ``next_token`` keyword. Reports must cover the whole week, so all pages are drained.
    """
    items: List[Dict[str, Any]] = []
    next_token: Optional[str] = None
    while True:
        page, next_token = query(week_key, next_token=next_token)
        items.extend(page)
        if not next_token:
            break
    return items


def _aggregate_technician_production(
    production_events: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Dict[str, int], Dict[str, int]]:
    """Aggregate ProductionEvents into the Technician section plus by-work_type / by-site.

    Returns:
        A tuple of (per-technician rows, verified counts by work_type, verified counts by
        site). Rows are sorted by technician_id for deterministic output (FR-024 AC-4).
    """
    per_tech: Dict[str, Dict[str, Any]] = {}
    by_work_type: Dict[str, int] = {}
    by_site: Dict[str, int] = {}

    for event in production_events:
        technician_id = str(event.get("technician_id"))
        work_type = str(event.get("work_type"))
        site = str(event.get("site"))
        qty = _int(event.get("completed_qty"))

        by_work_type[work_type] = by_work_type.get(work_type, 0) + 1
        by_site[site] = by_site.get(site, 0) + 1

        tech = per_tech.setdefault(
            technician_id,
            {
                "technician_id": technician_id,
                "verified_units_by_work_type": {},
                "total_verified_qty": 0,
                "verified_unit_count": 0,
            },
        )
        units_by_type = tech["verified_units_by_work_type"]
        units_by_type[work_type] = units_by_type.get(work_type, 0) + 1
        tech["total_verified_qty"] += qty
        tech["verified_unit_count"] += 1

    rows = [per_tech[key] for key in sorted(per_tech)]
    return rows, by_work_type, by_site


def _aggregate_material_runner(
    material_events: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Dict[str, float]]:
    """Aggregate MaterialEvents into the Material Runner section plus meters by material type.

    Returns:
        A tuple of (per-runner rows, total meters delivered by material type). Rows are
        sorted by runner_id for deterministic output (FR-024 AC-4).
    """
    per_runner: Dict[str, Dict[str, Any]] = {}
    meters_by_material_type: Dict[str, float] = {}

    for event in material_events:
        runner_id = str(event.get("runner_id"))
        material_type = str(event.get("material_type"))
        meters = _num(event.get("meters_delivered"))
        work_package_id = event.get("work_package_id")

        meters_by_material_type[material_type] = (
            meters_by_material_type.get(material_type, 0.0) + meters
        )

        runner = per_runner.setdefault(
            runner_id,
            {
                "runner_id": runner_id,
                "material_runs_completed": 0,
                "total_meters_delivered": 0.0,
                "_work_packages": set(),
            },
        )
        runner["material_runs_completed"] += 1
        runner["total_meters_delivered"] += meters
        if work_package_id is not None:
            runner["_work_packages"].add(str(work_package_id))

    rows: List[Dict[str, Any]] = []
    for key in sorted(per_runner):
        runner = per_runner[key]
        rows.append(
            {
                "runner_id": runner["runner_id"],
                "material_runs_completed": runner["material_runs_completed"],
                "total_meters_delivered": runner["total_meters_delivered"],
                "work_packages_supplied": len(runner["_work_packages"]),
            }
        )
    return rows, meters_by_material_type


def generate_weekly_report(week_key: str) -> Dict[str, Any]:
    """Build the weekly production summary for ``week_key`` from immutable events only.

    Reads ProductionEvents and MaterialEvents for the week (never mutable state) and returns
    a report with strictly separate Technician and Material Runner sections (BR-011, BR-016).
    Deterministic for a fixed set of events (FR-024 AC-4).
    """
    production_events = _fetch_all(repo.query_production_events_by_week, week_key)
    material_events = _fetch_all(repo.query_material_events_by_week, week_key)

    tech_rows, verified_by_work_type, verified_by_site = _aggregate_technician_production(
        production_events
    )
    runner_rows, meters_by_material_type = _aggregate_material_runner(material_events)

    return {
        "week_key": week_key,
        "verified_units_by_work_type": verified_by_work_type,
        "verified_units_by_site": verified_by_site,
        "total_meters_by_material_type": meters_by_material_type,
        # Separate sections — never combined into one productivity metric (BR-016).
        "technician_production": tech_rows,
        "material_runner_deliveries": runner_rows,
    }


def _require_week_key(week_key: Optional[str]) -> str:
    """Return a non-empty ``week_key`` or raise a validation error."""
    if not week_key:
        raise errors.ValidationError("week_key is required")
    return week_key


def _get(event: Dict[str, Any], correlation_id: str) -> Dict[str, Any]:
    """Handle ``GET /v1/reports/weekly`` (LEAD/ADMIN)."""
    user_ctx = common.authenticate(event)
    auth.authorize_operation(user_ctx, "view_report")
    week_key = _require_week_key(common.query_param(event, "week_key"))
    report = generate_weekly_report(week_key)
    return common.success(200, report, correlation_id)


def _post(event: Dict[str, Any], correlation_id: str) -> Dict[str, Any]:
    """Handle ``POST /v1/reports/weekly`` on-demand trigger (ADMIN)."""
    user_ctx = common.authenticate(event)
    auth.authorize_operation(user_ctx, "trigger_report")
    body = common.parse_body(event)
    week_key = _require_week_key(body.get("week_key"))
    metrics.increment(metrics.REPORT_GENERATION)
    report = generate_weekly_report(week_key)
    return common.success(200, report, correlation_id)


@error_handler
def handle(
    event: Dict[str, Any], context: Any = None, *, correlation_id: str = ""
) -> Dict[str, Any]:
    """Route a report request by HTTP method, or run the EventBridge weekly schedule.

    An event without an ``httpMethod`` is treated as the EventBridge weekly trigger: the
    report is generated for the ``week_key`` carried on the event (FR-024 AC-5).
    """
    method = event.get("httpMethod")
    if method is None:
        # EventBridge scheduled invocation — generate for the supplied week_key.
        correlation_id = correlation_id or str(uuid4())
        week_key = _require_week_key(event.get("week_key"))
        metrics.increment(metrics.REPORT_GENERATION)
        return generate_weekly_report(week_key)

    method = method.upper()
    if method == "POST":
        return _post(event, correlation_id)
    if method == "GET":
        return _get(event, correlation_id)
    raise errors.ValidationError(f"unsupported report route: {method}")
