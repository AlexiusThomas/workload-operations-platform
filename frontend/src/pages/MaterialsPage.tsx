import { useState } from "react";
import { api } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { useAsync } from "../hooks/useAsync";
import { EmptyState, ErrorBanner, ErrorState, Loading } from "../components/StateViews";
import { can } from "../auth/permissions";
import type { MaterialRequirement, MaterialStatus } from "../types";

const STATUSES: MaterialStatus[] = [
  "MATERIAL_REQUIRED", "MATERIAL_CLAIMED", "MATERIAL_DELIVERED", "READY_FOR_PREP",
];

export function MaterialsPage() {
  const { user } = useAuth();
  const token = user!.token;
  const role = user!.role;
  const [status, setStatus] = useState<MaterialStatus>("MATERIAL_REQUIRED");
  const [actionError, setActionError] = useState<unknown>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [deliver, setDeliver] = useState<{ id: string; qty: string; meters: string } | null>(null);

  const { data, loading, error, reload } = useAsync(
    () => api.listMaterialByStatus(token, status),
    [status],
  );

  const run = async (fn: () => Promise<unknown>, key: string) => {
    setActionError(null);
    setBusy(key);
    try {
      await fn();
      setDeliver(null);
      reload();
    } catch (e) {
      setActionError(e);
    } finally {
      setBusy(null);
    }
  };

  return (
    <main className="page materials-page">
      <h1>Material Requirements</h1>
      {actionError != null && <ErrorBanner error={actionError} onDismiss={() => setActionError(null)} />}

      <label className="filters" data-tour="material-status-filter">
        Status
        <select value={status} onChange={(e) => setStatus(e.target.value as MaterialStatus)}>
          {STATUSES.map((s) => (<option key={s} value={s}>{s}</option>))}
        </select>
      </label>

      {loading && <Loading label="Loading material requirements…" />}
      {error != null && <ErrorState error={error} onRetry={reload} />}
      {!loading && !error && data && data.items.length === 0 && (
        <EmptyState message="No material requirements match this status." />
      )}
      {!loading && !error && data && data.items.length > 0 && (
        <table className="material-table">
          <thead>
            <tr>
              <th>Requirement</th><th>Type</th><th>Qty</th><th>Meters</th>
              <th>Status</th><th>Runner</th><th></th>
            </tr>
          </thead>
          <tbody>
            {data.items.map((mr: MaterialRequirement) => (
              <tr key={mr.material_req_id}>
                <td>{mr.material_req_id}</td>
                <td>{mr.material_type}</td>
                <td>{mr.qty_delivered}/{mr.qty_required}</td>
                <td>{mr.meters_delivered}/{mr.total_meters_required}</td>
                <td><span className={`status status--${mr.status}`}>{mr.status}</span></td>
                <td>{mr.runner_id ?? "—"}</td>
                <td>
                  {mr.status === "MATERIAL_REQUIRED" && can(role, "material_claim") && (
                    <button
                      type="button"
                      data-tour="action-material-claim"
                      disabled={busy === mr.material_req_id}
                      onClick={() => run(() => api.materialAction(token, mr.material_req_id, "material-claim"), mr.material_req_id)}
                    >
                      Claim
                    </button>
                  )}
                  {mr.status === "MATERIAL_CLAIMED" && can(role, "material_deliver") && (
                    <button
                      type="button"
                      data-tour="action-material-deliver"
                      onClick={() => setDeliver({ id: mr.material_req_id, qty: String(mr.qty_required), meters: String(mr.total_meters_required) })}
                    >
                      Deliver
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {deliver && (
        <form
          className="deliver-form"
          onSubmit={(e) => {
            e.preventDefault();
            const q = Number(deliver.qty);
            const m = Number(deliver.meters);
            if (!Number.isInteger(q) || q <= 0 || !(m > 0)) return;
            run(
              () => api.materialAction(token, deliver.id, "material-deliver", { qty_delivered: q, meters_delivered: m }),
              deliver.id,
            );
          }}
        >
          <h2>Record delivery for {deliver.id}</h2>
          <label>Qty delivered
            <input type="number" min={1} value={deliver.qty} onChange={(e) => setDeliver({ ...deliver, qty: e.target.value })} />
          </label>
          <label>Meters delivered
            <input type="number" min={0} step="0.01" value={deliver.meters} onChange={(e) => setDeliver({ ...deliver, meters: e.target.value })} />
          </label>
          <button type="submit" disabled={busy === deliver.id}>Submit delivery</button>
          <button type="button" onClick={() => setDeliver(null)}>Cancel</button>
        </form>
      )}
    </main>
  );
}
