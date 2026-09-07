"""S3-triggered workload ingestion handler — two-phase staging/commit (Task 9.1).

Implements ``wop-ingestion-handler`` per the design's *Event Write Atomicity → Ingestion*
section. Ingestion is idempotent (keyed on the S3 object key, FR-001 AC-5, FR-020 AC-7) and
never leaves partially-ingested WorkPackages visible in the operational queue
(FR-001 AC-7, NFR-009 AC-4):

Phase 1 (Staging)
    The WorkPackage is created with ``state=INGESTING`` and ``committed=false``; its
    WorkUnits are created with ``state=STAGING``. Staged entities are invisible to the
    queue: GSI-1 is queried on ``state=AVAILABLE`` and staged units carry no ``sk_gsi1``
    key (sparse index), and the queue additionally suppresses any WorkUnit whose parent
    WorkPackage has ``committed=false`` (the visibility gate).

Phase 2 (Commit)
    Each STAGING WorkUnit is transitioned to ``AVAILABLE`` (canonical + projection +
    ``INGESTION_WORK_UNIT_COMMITTED`` audit). Only after every WorkUnit commit succeeds is
    a final atomic write executed that sets ``committed=true`` and ``state=AVAILABLE`` on
    the WorkPackage plus an ``INGESTION_COMMITTED`` audit — the single package-level
    visibility gate.

On any failure the WorkPackage is set to ``state=INGESTION_ERROR``, the S3 file is moved to
the error prefix, and an ``INGESTION_COMMIT_FAILED`` audit is written; no WorkUnit is left
visible in the operational queue.

INTERNAL-INTEGRATION-TODO (DEP-001): the real RATS workload file schema/format is
unconfirmed and MUST NOT be fabricated. Parsing is delegated to a replaceable
:class:`WorkloadFileParser` adapter; a synthetic JSON implementation
(:class:`SyntheticJsonWorkloadParser`) is provided for dev/test only.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Protocol, Sequence
from uuid import uuid4

from botocore.exceptions import ClientError

from backend.data import repositories as repo
from backend.data import tables
from backend.idempotency import layer as idempotency
from backend.observability import errors, logger, metrics

# ---------------------------------------------------------------------------
# Non-operational ingestion sentinel states (design: two-phase staging/commit).
# These are NOT part of the WorkUnit state machine and never appear in the queue.
# ---------------------------------------------------------------------------
WP_STATE_INGESTING = "INGESTING"
WP_STATE_AVAILABLE = "AVAILABLE"
WP_STATE_INGESTION_ERROR = "INGESTION_ERROR"
WU_STATE_STAGING = "STAGING"
WU_STATE_AVAILABLE = "AVAILABLE"

SYSTEM_ACTOR = "SYSTEM"
SYSTEM_ROLE = "SYSTEM"


def _iso_now() -> str:
    """Return the current UTC time as an ISO-8601 millisecond string (``...Z``)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _sk_gsi1(site: str, work_type: str, scheduled_date: str) -> str:
    """Build the GSI-1 composite sort key ``site#work_type#current_scheduled_date``."""
    return f"{site}#{work_type}#{scheduled_date}"


# ---------------------------------------------------------------------------
# Workload file parser adapter (INTERNAL-INTEGRATION-TODO — DEP-001)
# ---------------------------------------------------------------------------
class WorkloadValidationError(errors.IngestionSchemaError):
    """A workload file failed schema validation (FR-001 AC-4). Maps to INGESTION_SCHEMA_ERROR."""


class ParsedWorkPackage:
    """A parsed WorkPackage plus its WorkUnits, produced by a :class:`WorkloadFileParser`.

    This is a schema-neutral intermediate representation. The concrete file format is a
    replaceable concern (DEP-001); parsers translate whatever the upstream produces into
    this shape so the handler never depends on the real RATS schema.
    """

    def __init__(
        self,
        *,
        work_package_id: str,
        site: str,
        rack_position: str,
        work_type: str,
        scheduled_date: str,
        work_units: List[Dict[str, Any]],
    ) -> None:
        self.work_package_id = work_package_id
        self.site = site
        self.rack_position = rack_position
        self.work_type = work_type
        self.scheduled_date = scheduled_date
        self.work_units = work_units


class WorkloadFileParser(Protocol):
    """Replaceable adapter that parses a raw workload file body into WorkPackages.

    INTERNAL-INTEGRATION-TODO (DEP-001): the real RATS parser implements this protocol once
    the file schema/format is confirmed. The handler depends only on this interface.
    """

    def parse(self, raw_body: str) -> List[ParsedWorkPackage]:
        """Parse ``raw_body`` and return the WorkPackages it describes.

        Raises:
            WorkloadValidationError: If the file is malformed or fails schema validation.
        """
        ...


class SyntheticJsonWorkloadParser:
    """Dev/test parser for a documented synthetic JSON workload format.

    The synthetic format (NOT the real RATS schema) is a JSON object::

        {"work_packages": [
            {"work_package_id": "...", "site": "SITE-A", "rack_position": "RACK-001",
             "work_type": "Fiber", "scheduled_date": "2024-01-15",
             "work_units": [{"work_unit_id": "...", "required_qty": 10,
                             "work_type": "Fiber"}]}
        ]}

    ``work_package_id`` / ``work_unit_id`` are optional; when omitted a UUID is generated.
    """

    _REQUIRED_WP_FIELDS = ("site", "rack_position", "work_type", "scheduled_date")

    def parse(self, raw_body: str) -> List[ParsedWorkPackage]:
        import json

        try:
            document = json.loads(raw_body)
        except (ValueError, TypeError) as exc:
            raise WorkloadValidationError(f"workload file is not valid JSON: {exc}") from exc

        if not isinstance(document, dict) or not isinstance(document.get("work_packages"), list):
            raise WorkloadValidationError(
                "workload file must be a JSON object with a 'work_packages' array"
            )

        parsed: List[ParsedWorkPackage] = []
        for entry in document["work_packages"]:
            if not isinstance(entry, dict):
                raise WorkloadValidationError("each work_packages entry must be an object")
            for field in self._REQUIRED_WP_FIELDS:
                if not entry.get(field):
                    raise WorkloadValidationError(
                        f"work_package is missing mandatory attribute '{field}'"
                    )
            raw_units = entry.get("work_units")
            if not isinstance(raw_units, list) or not raw_units:
                raise WorkloadValidationError("work_package must contain a non-empty work_units")

            units: List[Dict[str, Any]] = []
            for raw_unit in raw_units:
                if not isinstance(raw_unit, dict):
                    raise WorkloadValidationError("each work_unit must be an object")
                required_qty = raw_unit.get("required_qty")
                if not isinstance(required_qty, int) or required_qty <= 0:
                    raise WorkloadValidationError("work_unit required_qty must be a positive int")
                units.append(
                    {
                        "work_unit_id": raw_unit.get("work_unit_id") or str(uuid4()),
                        "required_qty": required_qty,
                        "work_type": raw_unit.get("work_type") or entry["work_type"],
                    }
                )

            parsed.append(
                ParsedWorkPackage(
                    work_package_id=entry.get("work_package_id") or str(uuid4()),
                    site=entry["site"],
                    rack_position=entry["rack_position"],
                    work_type=entry["work_type"],
                    scheduled_date=entry["scheduled_date"],
                    work_units=units,
                )
            )
        return parsed


#: Default parser used when the handler is not given an explicit one. Replace with the real
#: RATS parser once DEP-001 is confirmed (INTERNAL-INTEGRATION-TODO).
DEFAULT_PARSER: WorkloadFileParser = SyntheticJsonWorkloadParser()


# ---------------------------------------------------------------------------
# Entity builders
# ---------------------------------------------------------------------------
def _build_work_package_item(parsed: ParsedWorkPackage) -> Dict[str, Any]:
    """Build the WorkPackage canonical item in the INGESTING/uncommitted state."""
    ts = _iso_now()
    return {
        "work_package_id": parsed.work_package_id,
        "site": parsed.site,
        "rack_position": parsed.rack_position,
        "work_type": parsed.work_type,
        "original_scheduled_date": parsed.scheduled_date,
        "current_scheduled_date": parsed.scheduled_date,
        "rollover_count": 0,
        "committed": False,
        "state": WP_STATE_INGESTING,
        "created_at": ts,
        "updated_at": ts,
    }


def _build_staged_work_unit_item(
    parsed_unit: Dict[str, Any], parsed_wp: ParsedWorkPackage
) -> Dict[str, Any]:
    """Build a WorkUnit canonical item in the STAGING state (no GSI-1 key — sparse)."""
    ts = _iso_now()
    return {
        "work_unit_id": parsed_unit["work_unit_id"],
        "work_package_id": parsed_wp.work_package_id,
        "site": parsed_wp.site,
        "work_type": parsed_unit["work_type"],
        "state": WU_STATE_STAGING,
        "claimed_by": None,
        "required_qty": parsed_unit["required_qty"],
        "completed_qty": 0,
        "tote_id": None,
        "rework_count": 0,
        "current_scheduled_date": parsed_wp.scheduled_date,
        "original_scheduled_date": parsed_wp.scheduled_date,
        "rollover_count": 0,
        "version": 0,
        "created_at": ts,
        "updated_at": ts,
    }


# ---------------------------------------------------------------------------
# S3 helpers
# ---------------------------------------------------------------------------
def _s3_client() -> Any:
    """Return the boto3 S3 client via the shared DynamoDB resource's session (lazy)."""
    import boto3

    return boto3.client("s3")


def _extract_s3_records(event: Dict[str, Any]) -> List[Dict[str, str]]:
    """Extract ``{"bucket", "key"}`` records from an S3 ObjectCreated event."""
    records: List[Dict[str, str]] = []
    for record in event.get("Records", []):
        s3 = record.get("s3", {})
        bucket = s3.get("bucket", {}).get("name")
        key = s3.get("object", {}).get("key")
        if bucket and key:
            records.append({"bucket": bucket, "key": key})
    return records


def _move_object_to_error(bucket: str, key: str, s3_client: Any) -> None:
    """Copy the source object to the error bucket and delete the original (best-effort)."""
    error_bucket = tables._get_config().error_bucket  # type: ignore[attr-defined]
    try:
        s3_client.copy_object(
            Bucket=error_bucket,
            Key=key,
            CopySource={"Bucket": bucket, "Key": key},
        )
        s3_client.delete_object(Bucket=bucket, Key=key)
    except ClientError as exc:  # pragma: no cover - defensive; do not mask ingestion error
        logger.error("ingestion_error_move_failed", error=str(exc), entity_id=key)


def _move_object_to_archive(bucket: str, key: str, s3_client: Any) -> None:
    """Copy the source object to the archive bucket and delete the original (best-effort)."""
    archive_bucket = tables._get_config().archive_bucket  # type: ignore[attr-defined]
    try:
        s3_client.copy_object(
            Bucket=archive_bucket,
            Key=key,
            CopySource={"Bucket": bucket, "Key": key},
        )
        s3_client.delete_object(Bucket=bucket, Key=key)
    except ClientError as exc:  # pragma: no cover - defensive
        logger.error("ingestion_archive_move_failed", error=str(exc), entity_id=key)


# ---------------------------------------------------------------------------
# Two-phase ingestion of a single parsed WorkPackage
# ---------------------------------------------------------------------------
def _ingest_work_package(parsed: ParsedWorkPackage, correlation_id: str) -> str:
    """Stage then commit one WorkPackage. Returns the outcome: ``committed`` / ``duplicate``.

    Raises:
        Exception: Propagated on an unrecoverable commit failure after the WorkPackage has
            been set to INGESTION_ERROR by the caller's error path.
    """
    existing = repo.get_work_package(parsed.work_package_id)
    if existing is not None:
        # Duplicate WorkPackage id — treat as a no-op (FR-001 AC-3).
        logger.info(
            "ingestion_duplicate_work_package",
            correlation_id=correlation_id,
            entity_type=repo.ENTITY_WORK_PACKAGE,
            entity_id=parsed.work_package_id,
        )
        try:
            repo.update_work_package_fields(
                parsed.work_package_id,
                fields={},
                action_type="WORK_PACKAGE_INGESTION_DUPLICATE",
                actor_id=SYSTEM_ACTOR,
                actor_role=SYSTEM_ROLE,
                before_state=existing.get("state"),
                after_state=existing.get("state"),
                correlation_id=correlation_id,
            )
        except ClientError:  # pragma: no cover - audit best-effort on duplicate
            pass
        return "duplicate"

    wp_item = _build_work_package_item(parsed)
    staged_units = [_build_staged_work_unit_item(unit, parsed) for unit in parsed.work_units]

    # Phase 1: staging.
    repo.stage_work_package_with_units(
        wp_item,
        staged_units,
        actor_id=SYSTEM_ACTOR,
        actor_role=SYSTEM_ROLE,
        correlation_id=correlation_id,
    )

    # Phase 2: commit each staged WorkUnit to AVAILABLE, then set the visibility gate.
    for staged in staged_units:
        available_unit = dict(staged)
        available_unit["sk_gsi1"] = _sk_gsi1(
            parsed.site, staged["work_type"], parsed.scheduled_date
        )
        repo.commit_staged_work_unit(
            available_unit,
            actor_id=SYSTEM_ACTOR,
            actor_role=SYSTEM_ROLE,
            correlation_id=correlation_id,
        )

    # Final visibility gate: WorkPackage committed=true + state=AVAILABLE.
    repo.update_work_package_fields(
        parsed.work_package_id,
        fields={"committed": True, "state": WP_STATE_AVAILABLE},
        action_type="INGESTION_COMMITTED",
        actor_id=SYSTEM_ACTOR,
        actor_role=SYSTEM_ROLE,
        before_state=WP_STATE_INGESTING,
        after_state=WP_STATE_AVAILABLE,
        correlation_id=correlation_id,
    )
    return "committed"


def _mark_ingestion_error(work_package_id: str, correlation_id: str, reason: str) -> None:
    """Set a WorkPackage to INGESTION_ERROR with an INGESTION_COMMIT_FAILED audit."""
    try:
        repo.update_work_package_fields(
            work_package_id,
            fields={"committed": False, "state": WP_STATE_INGESTION_ERROR},
            action_type="INGESTION_COMMIT_FAILED",
            actor_id=SYSTEM_ACTOR,
            actor_role=SYSTEM_ROLE,
            before_state=WP_STATE_INGESTING,
            after_state=WP_STATE_INGESTION_ERROR,
            correlation_id=correlation_id,
            metadata={"reason": reason},
        )
    except ClientError as exc:  # pragma: no cover - defensive
        logger.error(
            "ingestion_error_mark_failed",
            correlation_id=correlation_id,
            entity_id=work_package_id,
            error=str(exc),
        )


def process_object(
    bucket: str,
    key: str,
    *,
    correlation_id: str,
    parser: WorkloadFileParser,
    s3_client: Any,
) -> Dict[str, Any]:
    """Process a single S3 workload object end-to-end (idempotent on the S3 key).

    Returns a summary dict describing the outcome for the object.
    """
    idempotency_key = f"s3://{bucket}/{key}"
    fingerprint = idempotency.compute_fingerprint("ingestion", idempotency_key, {"key": key})

    check = idempotency.check_idempotency(idempotency_key, fingerprint)
    if check.decision == idempotency.DECISION_REPLAY:
        logger.info("ingestion_replayed", correlation_id=correlation_id, entity_id=key)
        return {"key": key, "status": "replayed"}
    if check.decision == idempotency.DECISION_IN_FLIGHT:
        # Another invocation is processing the same object; skip to avoid duplicate effects.
        logger.info("ingestion_in_flight_skip", correlation_id=correlation_id, entity_id=key)
        return {"key": key, "status": "in_flight"}

    if not idempotency.acquire_lock(idempotency_key, "ingestion", idempotency_key, fingerprint):
        logger.info("ingestion_lock_contended", correlation_id=correlation_id, entity_id=key)
        return {"key": key, "status": "in_flight"}

    logger.info("ingestion_started", correlation_id=correlation_id, entity_id=key)

    raw_body = s3_client.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")

    try:
        parsed_packages = parser.parse(raw_body)
    except WorkloadValidationError as exc:
        # Malformed / schema failure (FR-001 AC-4): move to error prefix, record, and stop.
        logger.warning(
            "ingestion_schema_error",
            correlation_id=correlation_id,
            entity_id=key,
            error=str(exc),
        )
        metrics.increment(metrics.INGESTION_FAILURE)
        _move_object_to_error(bucket, key, s3_client)
        body = f'{{"key": "{key}", "status": "schema_error"}}'
        idempotency.store_idempotency_result(idempotency_key, 400, body)
        return {"key": key, "status": "schema_error", "error": str(exc)}

    outcomes: List[Dict[str, str]] = []
    for parsed in parsed_packages:
        try:
            result = _ingest_work_package(parsed, correlation_id)
            outcomes.append({"work_package_id": parsed.work_package_id, "status": result})
        except Exception as exc:  # noqa: BLE001 - convert to INGESTION_ERROR + error prefix
            logger.error(
                "ingestion_commit_failed",
                correlation_id=correlation_id,
                entity_id=parsed.work_package_id,
                error=str(exc),
            )
            metrics.increment(metrics.INGESTION_FAILURE)
            _mark_ingestion_error(parsed.work_package_id, correlation_id, str(exc))
            _move_object_to_error(bucket, key, s3_client)
            body = f'{{"key": "{key}", "status": "ingestion_error"}}'
            idempotency.store_idempotency_result(idempotency_key, 500, body)
            return {"key": key, "status": "ingestion_error", "outcomes": outcomes}

    metrics.increment(metrics.INGESTION_SUCCESS)
    _move_object_to_archive(bucket, key, s3_client)
    logger.info("ingestion_completed", correlation_id=correlation_id, entity_id=key)
    import json

    body = json.dumps({"key": key, "status": "committed", "outcomes": outcomes})
    idempotency.store_idempotency_result(idempotency_key, 200, body)
    return {"key": key, "status": "committed", "outcomes": outcomes}


def handle(
    event: Dict[str, Any],
    context: Any = None,
    *,
    correlation_id: Optional[str] = None,
    parser: Optional[WorkloadFileParser] = None,
    s3_client: Optional[Any] = None,
) -> Dict[str, Any]:
    """Handle an S3 ObjectCreated event, ingesting every referenced workload file.

    This is the testable core. It is not wrapped by the HTTP ``error_handler`` because the
    ingestion trigger is S3, not API Gateway; failures are recorded as AuditEvents and the
    file is moved to the error prefix rather than surfaced as an HTTP error.

    Args:
        event: The S3 event.
        context: Unused Lambda context.
        correlation_id: Optional correlation id (generated when absent).
        parser: Optional workload parser (defaults to the synthetic JSON parser).
        s3_client: Optional S3 client (defaults to a lazily-created boto3 client).

    Returns:
        A summary dict with a ``results`` list, one entry per processed object.
    """
    correlation_id = correlation_id or str(uuid4())
    active_parser = parser or DEFAULT_PARSER
    active_s3 = s3_client or _s3_client()

    idempotency.set_table(tables.idempotency_table())

    results: List[Dict[str, Any]] = []
    for record in _extract_s3_records(event):
        results.append(
            process_object(
                record["bucket"],
                record["key"],
                correlation_id=correlation_id,
                parser=active_parser,
                s3_client=active_s3,
            )
        )
    return {"results": results}


def suppress_uncommitted(
    work_units: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Filter out WorkUnits whose parent WorkPackage has ``committed=false`` (visibility gate).

    Used by the queue handler so units already flipped to AVAILABLE during a Phase 2 commit
    (or orphaned under an INGESTION_ERROR package) never appear until the whole WorkPackage
    is committed (design: package-level visibility gate). A single BatchGet per distinct
    parent is performed via repository reads.
    """
    committed_cache: Dict[str, bool] = {}
    visible: List[Dict[str, Any]] = []
    for unit in work_units:
        work_package_id = unit.get("work_package_id")
        if work_package_id is None:
            visible.append(unit)
            continue
        if work_package_id not in committed_cache:
            parent = repo.get_work_package(str(work_package_id))
            committed_cache[work_package_id] = bool(parent and parent.get("committed"))
        if committed_cache[work_package_id]:
            visible.append(unit)
    return visible
