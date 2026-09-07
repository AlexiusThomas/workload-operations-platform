import { useState } from "react";
import { useParams } from "react-router-dom";
import { api } from "../api/client";
import { ApiError } from "../api/errors";
import { useAuth } from "../auth/AuthContext";
import { useAsync } from "../hooks/useAsync";
import { EmptyState, ErrorBanner, ErrorState, Loading } from "../components/StateViews";
import { can, ownsOrLeadAdmin, ownsStrict, type Operation } from "../auth/permissions";
import type { Role, WorkUnit, WorkUnitState } from "../types";

interface ActionDef {
  action: string; // path segment
  label: string;
  op: Operation;
  fromStates: WorkUnitState[];
  ownership: "none" | "strict" | "ownerOrLeadAdmin";
}

// State + role driven action set. State membership mirrors the backend state machine; role
// gating mirrors RBAC_MATRIX; ownership mirrors the require_* helpers. Actions the user is
// not authorized for are hidden entirely.
const ACTIONS: ActionDef[] = [
  { action: "claim", label: "Claim", op: "claim", fromStates: ["AVAILABLE", "REWORK_REQUIRED"], ownership: "none" },
  { action: "start", label: "Start", op: "start", fromStates: ["CLAIMED"], ownership: "strict" },
  { action: "prep-complete", label: "Prep complete", op: "prep_complete", fromStates: ["IN_PROGRESS"], ownership: "strict" },
  { action: "label", label: "Label", op: "label", fromStates: ["PREP_COMPLETE"], ownership: "ownerOrLeadAdmin" },
  { action: "tote-assign", label: "Assign tote", op: "tote_assign", fromStates: ["LABELED"], ownership: "ownerOrLeadAdmin" },
  { action: "ready-to-verify", label: "Ready to verify", op: "ready_to_verify", fromStates: ["TOTE_ASSIGNED"], ownership: "ownerOrLeadAdmin" },
  { action: "verify", label: "Verify", op: "verify", fromStates: ["READY_TO_VERIFY"], ownership: "none" },
  { action: "fail-verify", label: "Fail (rework)", op: "fail_verify", fromStates: ["READY_TO_VERIFY"], ownership: "none" },
  { action: "complete", label: "Complete", op: "complete", fromStates: ["VERIFIED"], ownership: "none" },
  { action: "assign", label: "Assign to technician", op: "assign", fromStates: ["AVAILABLE", "CLAIMED"], ownership: "none" },
];

function allowed(a: ActionDef, role: Role, userId: string, wu: WorkUnit): boolean {
  if (!a.fromStates.includes(wu.state)) return false;
  if (!can(role, a.op)) return false;
  if (a.ownership === "strict") return ownsStrict(role, userId, wu.claimed_by);
  if (a.ownership === "ownerOrLeadAdmin") return ownsOrLeadAdmin(role, userId, wu.claimed_by);
  return true;
}

export function WorkUnitPage() {
  const { id = "" } = useParams();
  const { user } = useAuth();
  const token = user!.token;
  const { data: wu, loading, error, reload } = useAsync(() => api.getWorkUnit(token, id), [id]);

  const [actionError, setActionError] = useState<unknown>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [qty, setQty] = useState<string>("");
  const [tote, setTote] = useState<string>("");
  const [failReason, setFailReason] = useState<string>("");
  const [assignTech, setAssignTech] = useState<string>("");

  if (loading) return <Loading label="Loading work unit…" />;
  if (error) return <ErrorState error={error} onRetry={reload} />;
  if (!wu) return <EmptyState message="Work unit not found." />;

  const role = user!.role;
  const userId = user!.user_id;

  const run = async (fn: () => Promise<unknown>, key: string) => {
    setActionError(null);
    setBusy(key);
    try {
      await fn();
      reload();
    } catch (e) {
      // 409 conflicts and validation errors surface as a banner; never crash the page.
      setActionError(e);
    } finally {
      setBusy(null);
    }
  };

  const canQuantity =
    can(role, "quantity") && wu.state === "IN_PROGRESS" && ownsStrict(role, userId, wu.claimed_by);

  return (
    <main className="page work-unit-page">
      <h1>Work Unit {wu.work_unit_id}</h1>
      {actionError instanceof ApiError && actionError.isConflict && (
        <ErrorBanner error={actionError} onDismiss={() => setActionError(null)} />
      )}
      {actionError != null && !(actionError instanceof ApiError && actionError.isConflict) && (
        <ErrorBanner error={actionError} onDismiss={() => setActionError(null)} />
      )}

      <dl className="detail-grid">
        <dt>Status</dt><dd><span className={`status status--${wu.state}`}>{wu.state}</span></dd>
        <dt>Package</dt><dd>{wu.work_package_id}</dd>
        <dt>Site / Type</dt><dd>{wu.site} / {wu.work_type}</dd>
        <dt>Claimed by</dt><dd>{wu.claimed_by ?? "—"}</dd>
        <dt>Quantity</dt><dd>{wu.completed_qty} / {wu.required_qty}</dd>
        <dt>Tote</dt><dd>{wu.tote_id ?? "—"}</dd>
        <dt>Rework count</dt><dd>{wu.rework_count}</dd>
        <dt>Scheduled</dt><dd>{wu.current_scheduled_date}</dd>
      </dl>

      <section className="actions">
        <h2>Actions</h2>

        {canQuantity && (
          <form
            className="action-form"
            data-tour="action-quantity"
            onSubmit={(e) => {
              e.preventDefault();
              const n = Number(qty);
              if (!Number.isInteger(n) || n < 0 || n > wu.required_qty) return;
              run(() => api.updateQuantity(token, wu.work_unit_id, n), "quantity");
            }}
          >
            <label>
              Completed qty
              <input
                type="number" min={0} max={wu.required_qty} value={qty}
                onChange={(e) => setQty(e.target.value)}
              />
            </label>
            <button type="submit" disabled={busy === "quantity" || qty === "" || Number(qty) > wu.required_qty}>
              Save quantity
            </button>
          </form>
        )}

        <div className="action-buttons">
          {ACTIONS.filter((a) => allowed(a, role, userId, wu)).map((a) => {
            if (a.action === "tote-assign") {
              return (
                <form
                  key={a.action}
                  className="action-form"
                  data-tour="action-tote-assign"
                  onSubmit={(e) => {
                    e.preventDefault();
                    if (!tote.trim()) return;
                    run(() => api.workUnitAction(token, wu.work_unit_id, "tote-assign", { tote_id: tote.trim() }), a.action);
                  }}
                >
                  <label>Tote id<input value={tote} onChange={(e) => setTote(e.target.value)} /></label>
                  <button type="submit" disabled={busy === a.action || !tote.trim()}>{a.label}</button>
                </form>
              );
            }
            if (a.action === "fail-verify") {
              return (
                <form
                  key={a.action}
                  className="action-form"
                  data-tour="action-fail-verify"
                  onSubmit={(e) => {
                    e.preventDefault();
                    if (!failReason.trim()) return;
                    run(() => api.workUnitAction(token, wu.work_unit_id, "fail-verify", { failure_reason: failReason.trim() }), a.action);
                  }}
                >
                  <label>Failure reason<input value={failReason} onChange={(e) => setFailReason(e.target.value)} /></label>
                  <button type="submit" disabled={busy === a.action || !failReason.trim()}>{a.label}</button>
                </form>
              );
            }
            if (a.action === "assign") {
              return (
                <form
                  key={a.action}
                  className="action-form"
                  data-tour="action-assign"
                  onSubmit={(e) => {
                    e.preventDefault();
                    if (!assignTech.trim()) return;
                    run(() => api.workUnitAction(token, wu.work_unit_id, "assign", { technician_id: assignTech.trim() }), a.action);
                  }}
                >
                  <label>Technician id<input value={assignTech} onChange={(e) => setAssignTech(e.target.value)} /></label>
                  <button type="submit" disabled={busy === a.action || !assignTech.trim()}>{a.label}</button>
                </form>
              );
            }
            return (
              <button
                key={a.action}
                type="button"
                data-tour={`action-${a.action}`}
                disabled={busy === a.action}
                onClick={() => run(() => api.workUnitAction(token, wu.work_unit_id, a.action), a.action)}
              >
                {a.label}
              </button>
            );
          })}
        </div>
        {ACTIONS.filter((a) => allowed(a, role, userId, wu)).length === 0 && (
          <EmptyState message="No actions are available to you for this status." />
        )}
      </section>
    </main>
  );
}
