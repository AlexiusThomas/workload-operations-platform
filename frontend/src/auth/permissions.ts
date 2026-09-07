import type { Role } from "../types";

// Operations mirror backend/auth/middleware.py RBAC_MATRIX keys. Do not invent operations.
export type Operation =
  | "view_queue"
  | "get_work_unit"
  | "get_work_package"
  | "view_material_requirements"
  | "create_work_package"
  | "create_material_requirement"
  | "claim"
  | "rework_claim"
  | "start"
  | "quantity"
  | "prep_complete"
  | "label"
  | "tote_assign"
  | "ready_to_verify"
  | "verify"
  | "fail_verify"
  | "complete"
  | "assign"
  | "material_claim"
  | "material_deliver"
  | "view_audit"
  | "trigger_report"
  | "view_report"
  | "ingest"
  | "view_stale";

const ALL: Role[] = ["TECHNICIAN", "MATERIAL_RUNNER", "VERIFIER_LEAD", "MANAGER_ADMIN"];

// Exact mirror of backend RBAC_MATRIX (backend/auth/middleware.py). The backend remains the
// authoritative enforcer; this is a UI-affordance gate to hide/disable unauthorized actions.
export const RBAC_MATRIX: Record<Operation, Role[]> = {
  view_queue: ALL,
  get_work_unit: ALL,
  get_work_package: ALL,
  view_material_requirements: ALL,
  create_work_package: ["MANAGER_ADMIN"],
  create_material_requirement: ["MANAGER_ADMIN"],
  claim: ["TECHNICIAN", "VERIFIER_LEAD", "MANAGER_ADMIN"],
  rework_claim: ["TECHNICIAN"],
  start: ["TECHNICIAN"],
  quantity: ["TECHNICIAN"],
  prep_complete: ["TECHNICIAN"],
  label: ["TECHNICIAN", "VERIFIER_LEAD", "MANAGER_ADMIN"],
  tote_assign: ["TECHNICIAN", "VERIFIER_LEAD", "MANAGER_ADMIN"],
  ready_to_verify: ["TECHNICIAN", "VERIFIER_LEAD", "MANAGER_ADMIN"],
  verify: ["VERIFIER_LEAD", "MANAGER_ADMIN"],
  fail_verify: ["VERIFIER_LEAD", "MANAGER_ADMIN"],
  complete: ["VERIFIER_LEAD", "MANAGER_ADMIN"],
  assign: ["VERIFIER_LEAD", "MANAGER_ADMIN"],
  material_claim: ["MATERIAL_RUNNER", "MANAGER_ADMIN"],
  material_deliver: ["MATERIAL_RUNNER", "MANAGER_ADMIN"],
  view_audit: ["VERIFIER_LEAD", "MANAGER_ADMIN"],
  trigger_report: ["MANAGER_ADMIN"],
  view_report: ["VERIFIER_LEAD", "MANAGER_ADMIN"],
  ingest: ["MANAGER_ADMIN"],
  view_stale: ["VERIFIER_LEAD", "MANAGER_ADMIN"],
};

export function can(role: Role, op: Operation): boolean {
  return RBAC_MATRIX[op].includes(role);
}

// Ownership Rule 1 (start/quantity/prep-complete): actor must be the claiming Technician.
export function ownsStrict(_role: Role, userId: string, claimedBy: string | null): boolean {
  return claimedBy != null && userId === claimedBy;
}

// Ownership Rule 4 (label/tote-assign/ready-to-verify): owner OR Lead/Admin bypass.
export function ownsOrLeadAdmin(role: Role, userId: string, claimedBy: string | null): boolean {
  if (role === "VERIFIER_LEAD" || role === "MANAGER_ADMIN") return true;
  return ownsStrict(role, userId, claimedBy);
}
