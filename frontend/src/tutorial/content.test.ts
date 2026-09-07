import { describe, expect, it } from "vitest";
import { ROLE_TUTORIALS } from "./content";

describe("role tutorials", () => {
  it("provides a flow for every actual role", () => {
    for (const role of ["TECHNICIAN", "MATERIAL_RUNNER", "VERIFIER_LEAD", "MANAGER_ADMIN"] as const) {
      expect(ROLE_TUTORIALS[role].steps.length).toBeGreaterThan(0);
    }
  });

  it("never tells a Material Runner to verify or complete work", () => {
    const bodies = ROLE_TUTORIALS.MATERIAL_RUNNER.steps.map((s) => s.body.toLowerCase());
    expect(bodies.some((b) => b.includes("verify") || b.includes("complete"))).toBe(false);
  });

  it("never tells a Technician to verify work", () => {
    const titles = ROLE_TUTORIALS.TECHNICIAN.steps.map((s) => s.title.toLowerCase());
    expect(titles.some((t) => t.includes("verify") && !t.includes("send to verify"))).toBe(false);
  });
});
