import { useNavigate } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { SYNTHETIC_USERS } from "../auth/synthetic";

// Development sign-in using synthetic users only (SEC-001 AC-2). Production auth (Midway,
// DEP-002) is intentionally NOT implemented; this preserves the auth-provider boundary.
export function LoginPage() {
  const { signIn } = useAuth();
  const navigate = useNavigate();

  return (
    <main className="page login-page">
      <h1>Workload Operations Platform</h1>
      <p className="notice">
        Development sign-in (synthetic users). Production authentication is provided by the
        approved identity provider and is not available here.
      </p>
      <ul className="user-list">
        {SYNTHETIC_USERS.map((u) => (
          <li key={u.token}>
            <button
              type="button"
              data-tour={`login-${u.role}`}
              onClick={() => {
                signIn(u);
                navigate("/queue");
              }}
            >
              <strong>{u.display_name}</strong>
              <span className="role-badge">{u.role.replace("_", " ")}</span>
            </button>
          </li>
        ))}
      </ul>
    </main>
  );
}
