import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { useTutorial } from "../tutorial/TutorialContext";
import { can } from "../auth/permissions";

export function NavBar() {
  const { user, signOut } = useAuth();
  const { start } = useTutorial();
  const navigate = useNavigate();
  if (!user) return null;

  return (
    <nav className="navbar">
      <Link to="/queue" className="brand">WOP</Link>
      <div className="nav-links">
        <Link to="/queue">Queue</Link>
        {can(user.role, "view_material_requirements") && <Link to="/materials">Materials</Link>}
        {can(user.role, "view_audit") && <Link to="/audit">Audit</Link>}
        {can(user.role, "view_report") && <Link to="/reports">Reports</Link>}
      </div>
      <div className="nav-user">
        <span className="role-badge">{user.display_name} · {user.role.replace("_", " ")}</span>
        <button type="button" data-tour="help-tutorial" onClick={start}>Help / Tutorial</button>
        <button type="button" onClick={() => { signOut(); navigate("/"); }}>Sign out</button>
      </div>
    </nav>
  );
}
