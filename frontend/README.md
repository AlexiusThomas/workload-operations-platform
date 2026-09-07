# frontend/ - WOP React + TypeScript SPA (Task 22)

Single-page application for the Workload Operations Platform, built with React 18 +
TypeScript + Vite. Consumes the existing backend `/v1/` API contracts only; no endpoints
or response formats are invented.

## Run
- Requires Node.js 22 (matches CI). `npm ci` then `npm run dev`.
- Set `VITE_API_BASE_URL` (see `.env.example`) to point at the API. Nothing is hardcoded.

## Scripts (used by CI, Task 21)
- `npm run lint` - ESLint (TypeScript + React).
- `npm run build` - type-check + Vite production build.
- `npm run typecheck` - `tsc --noEmit`.
- `npm run test` - Vitest component/unit tests.

## Screens
- Login (synthetic users, dev only), Work Queue, Work Unit detail/actions,
  Material Requirements, Audit history (Lead/Admin), Weekly Reports (Lead/Admin).

## Authentication
Development uses synthetic users only (SyntheticAuthProvider parity). Production
authentication (Midway, DEP-002) is NOT implemented; the auth-provider boundary is preserved.

## Role-based tutorial
A replayable, role-adaptive guided tutorial (Help / Tutorial control; shown on first use)
derives its steps from the actual RBAC model in `src/auth/permissions.ts`. It only shows
actions each role is authorized to perform.

> Frontend hosting (Harmony Console, DEP-006) remains INTERNAL-INTEGRATION-TODO.
