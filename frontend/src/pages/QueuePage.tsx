import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { useAsync } from "../hooks/useAsync";
import { EmptyState, ErrorState, Loading } from "../components/StateViews";
import type { WorkUnitState } from "../types";
import { can } from "../auth/permissions";

const STATES: WorkUnitState[] = [
  "AVAILABLE", "CLAIMED", "IN_PROGRESS", "PREP_COMPLETE", "LABELED",
  "TOTE_ASSIGNED", "READY_TO_VERIFY", "VERIFIED", "COMPLETE", "REWORK_REQUIRED",
];

export function QueuePage() {
  const { user } = useAuth();
  const [state, setState] = useState<WorkUnitState>("AVAILABLE");
  const [site, setSite] = useState("");
  const [workType, setWorkType] = useState("");
  const [date, setDate] = useState("");

  const token = user!.token;
  const { data, loading, error, reload } = useAsync(
    () =>
      api.listWorkUnits(token, {
        state,
        site: site || undefined,
        work_type: workType || undefined,
        current_scheduled_date: date || undefined,
      }),
    [state, site, workType, date],
  );

  return (
    <main className="page queue-page">
      <h1>Work Queue</h1>

      <form className="filters" data-tour="queue-filters" onSubmit={(e) => e.preventDefault()}>
        <label>
          Status
          <select
            data-tour="queue-state-filter"
            value={state}
            onChange={(e) => setState(e.target.value as WorkUnitState)}
          >
            {STATES.map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
        </label>
        <label>Site<input value={site} onChange={(e) => setSite(e.target.value)} placeholder="e.g. SITE-A" /></label>
        <label>Work type<input value={workType} onChange={(e) => setWorkType(e.target.value)} placeholder="e.g. Fiber" /></label>
        <label>Date<input type="date" value={date} onChange={(e) => setDate(e.target.value)} /></label>
      </form>

      {can(user!.role, "view_stale") && (
        <p><Link to="/stale">View stale active work</Link></p>
      )}

      {loading && <Loading label="Loading work units…" />}
      {error != null && <ErrorState error={error} onRetry={reload} />}
      {!loading && !error && data && data.items.length === 0 && (
        <EmptyState message="No work units match these filters." />
      )}
      {!loading && !error && data && data.items.length > 0 && (
        <table className="work-table">
          <thead>
            <tr>
              <th>Work Unit</th><th>Package</th><th>Site</th><th>Type</th>
              <th>Status</th><th>Qty</th><th>Scheduled</th><th></th>
            </tr>
          </thead>
          <tbody>
            {data.items.map((wu) => (
              <tr key={wu.work_unit_id}>
                <td>{wu.work_unit_id}</td>
                <td>{wu.work_package_id}</td>
                <td>{wu.site}</td>
                <td>{wu.work_type}</td>
                <td><span className={`status status--${wu.state}`}>{wu.state}</span></td>
                <td>{wu.completed_qty}/{wu.required_qty}</td>
                <td>{wu.current_scheduled_date}</td>
                <td><Link to={`/work-units/${wu.work_unit_id}`}>Open</Link></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {data?.next_token && <p className="pagination-note">More results available (showing up to 100).</p>}
    </main>
  );
}
