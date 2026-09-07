import { describe, expect, it } from "vitest";
import { can, ownsOrLeadAdmin, ownsStrict } from "./permissions";

describe("RBAC permissions (mirror backend RBAC_MATRIX)", () => {
  it("only Technician can start/quantity/prep_complete", () => {
    expect(can("TECHNICIAN", "start")).toBe(true);
    expect(can("VERIFIER_LEAD", "start")).toBe(false);
    expect(can("MATERIAL_RUNNER", "quantity")).toBe(false);
    expect(can("MANAGER_ADMIN", "prep_complete")).toBe(false);
  });

  it("only Lead/Admin can verify, complete, view audit and reports", () => {
    for (const op of ["verify", "complete", "view_audit", "view_report"] as const) {
      expect(can("VERIFIER_LEAD", op)).toBe(true);
      expect(can("MANAGER_ADMIN", op)).toBe(true);
      expect(can("TECHNICIAN", op)).toBe(false);
      expect(can("MATERIAL_RUNNER", op)).toBe(false);
    }
  });

  it("only Runner/Admin can claim and deliver material", () => {
    expect(can("MATERIAL_RUNNER", "material_claim")).toBe(true);
    expect(can("MANAGER_ADMIN", "material_deliver")).toBe(true);
    expect(can("TECHNICIAN", "material_claim")).toBe(false);
    expect(can("VERIFIER_LEAD", "material_deliver")).toBe(false);
  });

  it("only Admin can trigger a report or create work packages", () => {
    expect(can("MANAGER_ADMIN", "trigger_report")).toBe(true);
    expect(can("VERIFIER_LEAD", "trigger_report")).toBe(false);
    expect(can("MANAGER_ADMIN", "create_work_package")).toBe(true);
    expect(can("TECHNICIAN", "create_work_package")).toBe(false);
  });

  it("ownership rules: strict owner vs lead/admin bypass", () => {
    expect(ownsStrict("TECHNICIAN", "TECH-001", "TECH-001")).toBe(true);
    expect(ownsStrict("TECHNICIAN", "TECH-001", "TECH-002")).toBe(false);
    expect(ownsOrLeadAdmin("VERIFIER_LEAD", "VERIFY-001", "TECH-002")).toBe(true);
    expect(ownsOrLeadAdmin("TECHNICIAN", "TECH-001", "TECH-002")).toBe(false);
  });
});
