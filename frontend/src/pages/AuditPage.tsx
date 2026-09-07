import { useState } from "react";
import { api } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { useAsync } from "../hooks/useAsync";
import { EmptyState, ErrorState, Loading } from "../components/StateViews";

const ENTITY_TYPES = ["work-unit", "work-package", "material-requirement"];

// Read-only historical view. Audit events are immutable and are never presented as editable
// operational state (no action controls here).
export function AuditPage() {
  const { user } = useAuth();
  const token = user!.token;
  const [entityType, setEntityType] = useState("work-unit");
  const [entityId, setEntityId] = useState("");
  const [submitted, setSubmitted] = useState<{ t: string; id: string } | null>(null);

  const { data, loading, error, reload } = useAsync(
    () => (submitted ? api.getAudit(token, submitted.t, submitted.id) : Promise.resolve(null)),
    [submitted?.t, submitted?.id],
  );

  return (
    <main className="page audit-page" data-tour="audit-view">
      <h1>Audit History</h1>
      <form
        className="filters"
        onSubmit={(e) => {
          e.preventDefault();
          if (entityId.trim()) setSubmitted({ t: entityType, id: entityId.trim() });
        }}
      >
        <label>Entity type
          <select value={entityType} onChange={(e) => setEntityType(e.target.value)}>
            {ENTITY_TYPES.map((t) => (<option key={t} value={t}>{t}</option>))}
          </select>
        </label>
        <label>Entity id<input value={entityId} onChange={(e) => setEntityId(e.target.value)} /></label>
        <button type="submit" disabled={!entityId.trim()}>View history</button>
      </form>

      {!submitted && <EmptyState message="Enter an entity id to view its chronological audit history." />}
      {submitted && loading && <Loading label="Loading audit history…" />}
      {submitted && error != null && <ErrorState error={error} onRetry={reload} />}
      {submitted && !loading && !error && data && data.items.length === 0 && (
        <EmptyState message="No audit events found for that entity." />
      )}
      {submitted && !loading && !error && data && data.items.length > 0 && (
        <ol className="audit-timeline">
          {data.items.map((ev) => (
            <li key={ev.audit_event_id}>
              <time>{ev.timestamp}</time>
              <span className="audit-action">{ev.action_type}</span>
              <span className="audit-transition">{ev.before_state ?? "—"} → {ev.after_state ?? "—"}</span>
              <span className="audit-actor">{ev.actor_id} ({ev.actor_role})</span>
            </li>
          ))}
        </ol>
      )}
    </main>
  );
}
