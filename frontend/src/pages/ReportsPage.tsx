import { useState } from "react";
import { api } from "../api/client";
import { ApiError, friendlyMessage } from "../api/errors";
import { useAuth } from "../auth/AuthContext";
import { EmptyState, Loading } from "../components/StateViews";
import { can } from "../auth/permissions";
import type { WeeklyReport } from "../types";

// Reporting is historical/derived data only (from immutable events). It is never shown as
// editable operational state. Technician and Material Runner sections are always separate
// (BR-016, FR-024 AC-3).
export function ReportsPage() {
  const { user } = useAuth();
  const token = user!.token;
  const role = user!.role;
  const [weekKey, setWeekKey] = useState("");
  const [report, setReport] = useState<WeeklyReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const WEEK_RE = /^\d{4}-W\d{2}$/; // ISO week key, mirrors backend validation

  const load = async (trigger: boolean) => {
    if (!WEEK_RE.test(weekKey)) {
      setError(new ApiError("VALIDATION_ERROR", "Week must look like 2024-W03", 400));
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const r = trigger
        ? await api.triggerWeeklyReport(token, weekKey)
        : await api.getWeeklyReport(token, weekKey);
      setReport(r);
    } catch (e) {
      setError(e);
    } finally {
      setLoading(false);
    }
  };

  return (
    <main className="page reports-page" data-tour="report-view">
      <h1>Weekly Report</h1>
      <form className="filters" onSubmit={(e) => { e.preventDefault(); load(false); }}>
        <label>Week key<input value={weekKey} onChange={(e) => setWeekKey(e.target.value)} placeholder="2024-W03" /></label>
        <button type="submit" disabled={loading}>View report</button>
        {can(role, "trigger_report") && (
          <button type="button" data-tour="report-trigger" disabled={loading} onClick={() => load(true)}>
            Generate on demand
          </button>
        )}
      </form>

      {error != null && <p className="banner banner--error" role="alert">{friendlyMessage(error)}</p>}
      {loading && <Loading label="Loading report…" />}
      {!loading && !report && !error && <EmptyState message="Enter a week key to view its report." />}

      {report && (
        <div className="report">
          <h2>Week {report.week_key}</h2>

          <section>
            <h3>Verified units by work type</h3>
            <ul>{Object.entries(report.verified_units_by_work_type).map(([k, v]) => (<li key={k}>{k}: {v}</li>))}</ul>
            <h3>Verified units by site</h3>
            <ul>{Object.entries(report.verified_units_by_site).map(([k, v]) => (<li key={k}>{k}: {v}</li>))}</ul>
            <h3>Meters by material type</h3>
            <ul>{Object.entries(report.total_meters_by_material_type).map(([k, v]) => (<li key={k}>{k}: {v}</li>))}</ul>
          </section>

          <section className="report-section report-section--technician" data-tour="report-technician-section">
            <h3>Technician Production</h3>
            {report.technician_production.length === 0 ? (
              <EmptyState message="No technician production this week." />
            ) : (
              <table>
                <thead><tr><th>Technician</th><th>Units verified</th><th>Total qty</th></tr></thead>
                <tbody>
                  {report.technician_production.map((t) => (
                    <tr key={t.technician_id}>
                      <td>{t.technician_id}</td><td>{t.verified_unit_count}</td><td>{t.total_verified_qty}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>

          <section className="report-section report-section--runner" data-tour="report-runner-section">
            <h3>Material Runner Deliveries</h3>
            {report.material_runner_deliveries.length === 0 ? (
              <EmptyState message="No material deliveries this week." />
            ) : (
              <table>
                <thead><tr><th>Runner</th><th>Runs completed</th><th>Meters delivered</th><th>Packages supplied</th></tr></thead>
                <tbody>
                  {report.material_runner_deliveries.map((r) => (
                    <tr key={r.runner_id}>
                      <td>{r.runner_id}</td><td>{r.material_runs_completed}</td>
                      <td>{r.total_meters_delivered}</td><td>{r.work_packages_supplied}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>
        </div>
      )}
    </main>
  );
}
