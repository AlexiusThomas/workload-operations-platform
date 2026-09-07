import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { useTutorial } from "./TutorialContext";
import {
  ROLE_TUTORIALS,
  WORKUNIT_STATUS_GUIDE,
  MATERIAL_STATUS_GUIDE,
  ERROR_GUIDE,
} from "./content";

// Role-adaptive, replayable guided tutorial. Steps are derived from the authenticated user's
// role and show ONLY actions that role can perform (content lists are RBAC-aligned).
export function Tutorial() {
  const { user } = useAuth();
  const { open, close, markSeen } = useTutorial();
  const navigate = useNavigate();
  const [index, setIndex] = useState(0);

  useEffect(() => {
    if (open) setIndex(0);
  }, [open]);

  if (!open || !user) return null;

  const flow = ROLE_TUTORIALS[user.role];
  const step = flow.steps[index];
  const isLast = index === flow.steps.length - 1;

  const go = (delta: number) => {
    const next = index + delta;
    if (next < 0 || next >= flow.steps.length) return;
    const target = flow.steps[next];
    if (target.route) navigate(target.route);
    setIndex(next);
  };

  const finish = () => {
    markSeen(user.user_id);
    close();
  };

  return (
    <div className="tutorial-overlay" role="dialog" aria-modal="true" aria-label="Guided tutorial">
      <div className="tutorial-card" data-tour="tutorial-card">
        <header>
          <h2>{flow.role.replace("_", " ")} — Getting started</h2>
          <button type="button" aria-label="Close tutorial" onClick={finish}>×</button>
        </header>

        {index === 0 && <p className="tutorial-intro">{flow.intro}</p>}

        <section className="tutorial-step">
          <p className="tutorial-progress">
            Step {index + 1} of {flow.steps.length}
          </p>
          <h3>{step.title}</h3>
          <p>{step.body}</p>
          {step.target && <p className="tutorial-target">Look for: <code>{step.target}</code></p>}
        </section>

        {isLast && (
          <details className="tutorial-reference">
            <summary>Status &amp; error reference</summary>
            <h4>Work statuses</h4>
            <ul>
              {WORKUNIT_STATUS_GUIDE.map((s) => (
                <li key={s.status}><strong>{s.status}</strong>: {s.meaning}</li>
              ))}
            </ul>
            <h4>Material statuses</h4>
            <ul>
              {MATERIAL_STATUS_GUIDE.map((s) => (
                <li key={s.status}><strong>{s.status}</strong>: {s.meaning}</li>
              ))}
            </ul>
            <h4>Common messages</h4>
            <ul>
              {ERROR_GUIDE.map((e) => (
                <li key={e.code}><strong>{e.code}</strong>: {e.meaning}</li>
              ))}
            </ul>
          </details>
        )}

        <footer className="tutorial-nav">
          <button type="button" onClick={() => go(-1)} disabled={index === 0}>Back</button>
          {isLast ? (
            <button type="button" onClick={finish}>Done</button>
          ) : (
            <button type="button" onClick={() => go(1)}>Next</button>
          )}
        </footer>
      </div>
    </div>
  );
}
