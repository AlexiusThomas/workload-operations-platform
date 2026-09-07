// Domain types mirroring the backend API contracts (backend/handlers/*).
// Do not invent fields; these match the handler response bodies exactly.

export type Role =
  | "TECHNICIAN"
  | "MATERIAL_RUNNER"
  | "VERIFIER_LEAD"
  | "MANAGER_ADMIN";

// WorkUnit states — backend/domain/state_machine.py ALL_STATES.
export type WorkUnitState =
  | "AVAILABLE"
  | "CLAIMED"
  | "IN_PROGRESS"
  | "PREP_COMPLETE"
  | "LABELED"
  | "TOTE_ASSIGNED"
  | "READY_TO_VERIFY"
  | "VERIFIED"
  | "COMPLETE"
  | "REWORK_REQUIRED";

// MaterialRequirement statuses — backend/handlers/material.py.
export type MaterialStatus =
  | "MATERIAL_REQUIRED"
  | "MATERIAL_CLAIMED"
  | "MATERIAL_DELIVERED"
  | "READY_FOR_PREP";

export interface UserContext {
  user_id: string;
  role: Role;
  display_name: string;
  token: string;
}

export interface WorkUnit {
  work_unit_id: string;
  work_package_id: string;
  site: string;
  work_type: string;
  state: WorkUnitState;
  claimed_by: string | null;
  required_qty: number;
  completed_qty: number;
  tote_id: string | null;
  rework_count: number;
  current_scheduled_date: string;
  original_scheduled_date: string;
  rollover_count: number;
  production_event_id?: string;
}

export interface MaterialRequirement {
  material_req_id: string;
  work_package_id: string;
  work_unit_id: string | null;
  material_type: string;
  cable_length_m: number;
  qty_required: number;
  total_meters_required: number;
  qty_delivered: number;
  meters_delivered: number;
  runner_id: string | null;
  status: MaterialStatus;
}

export interface AuditEvent {
  audit_event_id: string;
  entity_type: string;
  entity_id: string;
  action_type: string;
  actor_id: string;
  actor_role: string;
  before_state: string | null;
  after_state: string | null;
  metadata: Record<string, unknown> | null;
  timestamp: string;
  correlation_id: string | null;
}

export interface WeeklyReport {
  week_key: string;
  verified_units_by_work_type: Record<string, number>;
  verified_units_by_site: Record<string, number>;
  total_meters_by_material_type: Record<string, number>;
  technician_production: Array<{
    technician_id: string;
    verified_units_by_work_type: Record<string, number>;
    total_verified_qty: number;
    verified_unit_count: number;
  }>;
  material_runner_deliveries: Array<{
    runner_id: string;
    material_runs_completed: number;
    total_meters_delivered: number;
    work_packages_supplied: number;
  }>;
}

export interface Paginated<T> {
  items: T[];
  next_token: string | null;
  count: number;
}

// Standard error envelope — backend/observability/errors.py error_body().
export interface ApiErrorBody {
  error: {
    code: string;
    message: string;
    request_id: string | null;
    timestamp: string;
  };
}
