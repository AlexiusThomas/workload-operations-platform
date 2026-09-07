import type { Role } from "../types";

// Structured tutorial content (maintainable data, not scattered strings). Each role's flow
// lists ONLY actions that role is authorized to perform per backend RBAC_MATRIX. A flow is
// included for a role only if the workflow is supported by that role's permissions.

export interface TutorialStep {
  title: string;
  body: string;
  // CSS selector or route the step highlights (actual controls/screens used by the role).
  target?: string;
  route?: string;
}

export interface RoleTutorial {
  role: Role;
  intro: string;
  steps: TutorialStep[];
}

// Plain-language explanation of workload statuses (WorkUnit state machine) — shared reference.
export const WORKUNIT_STATUS_GUIDE: Array<{ status: string; meaning: string }> = [
  { status: "AVAILABLE", meaning: "Unclaimed work waiting in the queue." },
  { status: "CLAIMED", meaning: "Picked up by a Technician; not started yet." },
  { status: "IN_PROGRESS", meaning: "Being worked on; quantities can be recorded." },
  { status: "PREP_COMPLETE", meaning: "All required quantity finished; ready to label." },
  { status: "LABELED", meaning: "Labeled; ready for a tote to be assigned." },
  { status: "TOTE_ASSIGNED", meaning: "A tote is recorded; ready to send for verification." },
  { status: "READY_TO_VERIFY", meaning: "Awaiting a Verifier Lead's check." },
  { status: "VERIFIED", meaning: "Passed verification; a production record was created." },
  { status: "COMPLETE", meaning: "Finished and closed." },
  { status: "REWORK_REQUIRED", meaning: "Verification failed; needs rework (progress is kept)." },
];

// Plain-language explanation of material statuses (MaterialRequirement).
export const MATERIAL_STATUS_GUIDE: Array<{ status: string; meaning: string }> = [
  { status: "MATERIAL_REQUIRED", meaning: "Material still needs to be claimed by a Runner." },
  { status: "MATERIAL_CLAIMED", meaning: "A Runner claimed it and will deliver it." },
  { status: "MATERIAL_DELIVERED", meaning: "Delivered; quantity and meters recorded." },
  { status: "READY_FOR_PREP", meaning: "All material for the scope is delivered; prep can proceed." },
];

// Common validation / conflict / error responses in plain language (shared reference).
export const ERROR_GUIDE: Array<{ code: string; meaning: string }> = [
  { code: "CLAIM_CONFLICT", meaning: "Someone else claimed the item first. Refresh and pick another." },
  { code: "MATERIAL_CLAIM_CONFLICT", meaning: "The material was just claimed by someone else." },
  { code: "QUANTITY_EXCEEDED", meaning: "Completed quantity cannot be more than required." },
  { code: "TOTE_REQUIRED", meaning: "Assign a tote before marking ready to verify." },
  { code: "INCOMPLETE_WORK", meaning: "Finish all required quantity before prep complete." },
  { code: "INVALID_STATE_TRANSITION", meaning: "That step is not allowed for the current status." },
  { code: "AUTHORIZATION_DENIED", meaning: "Your role cannot perform that action." },
];

// Per-role guided workflows. Actions listed match the RBAC matrix for each role exactly.
export const ROLE_TUTORIALS: Record<Role, RoleTutorial> = {
  TECHNICIAN: {
    role: "TECHNICIAN",
    intro:
      "As a Technician you pull work from the queue and move a work unit through the build steps.",
    steps: [
      { title: "Find work", body: "Open the Queue and filter by site, work type, or date.", route: "/queue", target: "[data-tour='queue-filters']" },
      { title: "Claim a work unit", body: "Claim an AVAILABLE unit to start. If it was just taken, you'll see a claim-conflict message — pick another.", target: "[data-tour='action-claim']" },
      { title: "Start work", body: "Start your claimed unit to move it to IN_PROGRESS.", target: "[data-tour='action-start']" },
      { title: "Record quantity", body: "Enter completed quantity as you go. It cannot exceed the required quantity.", target: "[data-tour='action-quantity']" },
      { title: "Prep complete", body: "When all required quantity is done, mark preparation complete.", target: "[data-tour='action-prep-complete']" },
      { title: "Label & tote", body: "Label the unit, then assign a tote id (required before verification).", target: "[data-tour='action-label']" },
      { title: "Send to verify", body: "Mark ready to verify once a tote is recorded.", target: "[data-tour='action-ready-to-verify']" },
      { title: "Rework", body: "If a Lead sends it back (REWORK_REQUIRED), re-claim it — your completed quantity is preserved.", target: "[data-tour='action-claim']" },
    ],
  },
  MATERIAL_RUNNER: {
    role: "MATERIAL_RUNNER",
    intro:
      "As a Material Runner you claim material requirements and record deliveries.",
    steps: [
      { title: "Open the material queue", body: "View requirements by status, starting with MATERIAL_REQUIRED.", route: "/materials", target: "[data-tour='material-status-filter']" },
      { title: "Claim material", body: "Claim a requirement to reserve it. If it was just claimed, you'll see a conflict message.", target: "[data-tour='action-material-claim']" },
      { title: "Deliver material", body: "Record quantity delivered and meters delivered. When the whole scope is delivered it becomes READY_FOR_PREP.", target: "[data-tour='action-material-deliver']" },
    ],
  },
  VERIFIER_LEAD: {
    role: "VERIFIER_LEAD",
    intro:
      "As a Verifier Lead you verify finished work, send back rework, handle exceptions, and review audit and reports.",
    steps: [
      { title: "Review ready work", body: "Filter the queue to READY_TO_VERIFY to find units awaiting your check.", route: "/queue", target: "[data-tour='queue-state-filter']" },
      { title: "Verify or fail", body: "Verify passing work (creates a production record) or fail it with a reason to send it back for rework.", target: "[data-tour='action-verify']" },
      { title: "Complete", body: "Complete a VERIFIED unit to close it.", target: "[data-tour='action-complete']" },
      { title: "Exception assignment", body: "Reassign an AVAILABLE or CLAIMED unit to a specific technician when needed.", target: "[data-tour='action-assign']" },
      { title: "Audit history", body: "Open Audit to review the chronological history of any item.", route: "/audit", target: "[data-tour='audit-view']" },
      { title: "Weekly report", body: "Open Reports to view weekly results — Technician and Material Runner sections are shown separately.", route: "/reports", target: "[data-tour='report-view']" },
    ],
  },
  MANAGER_ADMIN: {
    role: "MANAGER_ADMIN",
    intro:
      "As a Manager Admin you have full operational access and can trigger on-demand reports.",
    steps: [
      { title: "Full queue access", body: "You can claim, assign, label, tote, verify, and complete work units.", route: "/queue", target: "[data-tour='queue-filters']" },
      { title: "Material oversight", body: "You can claim and deliver material requirements as needed.", route: "/materials", target: "[data-tour='material-status-filter']" },
      { title: "Audit history", body: "Review the chronological audit trail for any entity.", route: "/audit", target: "[data-tour='audit-view']" },
      { title: "Trigger a report", body: "Generate a weekly report on demand; sections stay separate for Technicians and Runners.", route: "/reports", target: "[data-tour='report-trigger']" },
    ],
  },
};
