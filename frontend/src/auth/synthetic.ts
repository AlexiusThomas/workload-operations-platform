import type { UserContext } from "../types";

// Development/synthetic users ONLY. These mirror backend/auth/provider.py SYNTHETIC_USERS
// and exist solely to exercise the SyntheticAuthProvider in dev/test (SEC-001 AC-2, CON-003).
// Production authentication (Midway, DEP-002) is NOT implemented here; the auth-provider
// boundary is preserved. No production credentials, tokens, or secrets are hardcoded.
export const SYNTHETIC_USERS: UserContext[] = [
  { token: "token-tech-001", user_id: "TECH-001", role: "TECHNICIAN", display_name: "Technician One" },
  { token: "token-tech-002", user_id: "TECH-002", role: "TECHNICIAN", display_name: "Technician Two" },
  { token: "token-runner-001", user_id: "RUNNER-001", role: "MATERIAL_RUNNER", display_name: "Runner One" },
  { token: "token-verify-001", user_id: "VERIFY-001", role: "VERIFIER_LEAD", display_name: "Lead One" },
  { token: "token-admin-001", user_id: "ADMIN-001", role: "MANAGER_ADMIN", display_name: "Admin One" },
];
