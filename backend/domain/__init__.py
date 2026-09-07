"""Pure domain layer for the Workload Operations Platform.

Contains the WorkUnit state machine, quantity validation, WorkPackage aggregate state
derivation, and reporting/material helpers. All modules are pure: no I/O, no AWS/DynamoDB
calls, no network access, and no environment reads.

Modules:
    - ``state_machine``: WorkUnit valid-transition table and ``apply_transition``.
    - ``quantity``: ``validate_quantity_update`` (completed <= required, required > 0).
    - ``aggregate_state``: ``compute_work_package_state`` (priority-ordered derivation).
    - ``reporting``: ``compute_week_key`` (ISO ``YYYY-WNN``).
    - ``material``: ``create_material_requirement`` (immutable total_meters_required).
"""
