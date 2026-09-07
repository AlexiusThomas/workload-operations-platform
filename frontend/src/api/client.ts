import type {
  AuditEvent,
  MaterialRequirement,
  Paginated,
  WeeklyReport,
  WorkUnit,
} from "../types";
import { ApiError, toApiError } from "./errors";

// Base URL is injected at build/deploy time (environment-agnostic). No endpoint is hardcoded.
const BASE_URL: string =
  (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, "") ?? "";

function uuid(): string {
  // Client-generated Idempotency-Key for mutating requests (FR-020 AC-1).
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return "idem-" + Date.now() + "-" + Math.random().toString(16).slice(2);
}

interface RequestOptions {
  method?: string;
  body?: unknown;
  token: string;
  idempotent?: boolean;
  query?: Record<string, string | undefined>;
}

function buildQuery(query?: Record<string, string | undefined>): string {
  if (!query) return "";
  const parts = Object.entries(query)
    .filter(([, v]) => v !== undefined && v !== "")
    .map(([k, v]) => encodeURIComponent(k) + "=" + encodeURIComponent(v as string));
  return parts.length ? "?" + parts.join("&") : "";
}

async function request<T>(path: string, opts: RequestOptions): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Authorization: "Bearer " + opts.token,
  };
  const method = opts.method ?? "GET";
  if (opts.idempotent && method !== "GET") {
    headers["Idempotency-Key"] = uuid();
  }

  let resp: Response;
  try {
    resp = await fetch(BASE_URL + path + buildQuery(opts.query), {
      method,
      headers,
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
    });
  } catch {
    throw new ApiError("NETWORK_ERROR", "Network request failed", 0);
  }

  const text = await resp.text();
  const data = text ? safeParse(text) : undefined;
  if (!resp.ok) {
    throw toApiError(resp.status, data);
  }
  return data as T;
}

function safeParse(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return undefined;
  }
}

// ---- Endpoint methods (paths match backend API Design / handler routers) ----
export const api = {
  // Work queue + units
  listWorkUnits(token: string, q: Record<string, string | undefined>): Promise<Paginated<WorkUnit>> {
    return request("/v1/work-units", { token, query: q });
  },
  getWorkUnit(token: string, id: string): Promise<WorkUnit> {
    return request(`/v1/work-units/${id}`, { token });
  },
  listStale(token: string): Promise<{ items: WorkUnit[]; count: number }> {
    return request("/v1/work-units/stale", { token });
  },
  workUnitAction(
    token: string,
    id: string,
    action: string,
    body?: unknown,
    method: string = "POST",
  ): Promise<WorkUnit> {
    return request(`/v1/work-units/${id}/${action}`, {
      token,
      method,
      body,
      idempotent: true,
    });
  },
  updateQuantity(token: string, id: string, completed_qty: number): Promise<WorkUnit> {
    return request(`/v1/work-units/${id}/quantity`, {
      token,
      method: "PATCH",
      body: { completed_qty },
      idempotent: true,
    });
  },

  // Material requirements
  listMaterialByStatus(
    token: string,
    status: string,
    next_token?: string,
  ): Promise<Paginated<MaterialRequirement>> {
    return request("/v1/material-requirements", { token, query: { status, next_token } });
  },
  materialAction(
    token: string,
    id: string,
    action: "material-claim" | "material-deliver",
    body?: unknown,
  ): Promise<unknown> {
    return request(`/v1/material-requirements/${id}/${action}`, {
      token,
      method: "POST",
      body,
      idempotent: true,
    });
  },

  // Audit + reports (read-only; historical, never editable operational state)
  getAudit(
    token: string,
    entityType: string,
    entityId: string,
  ): Promise<Paginated<AuditEvent>> {
    return request(`/v1/audit/${entityType}/${entityId}`, { token });
  },
  getWeeklyReport(token: string, week_key: string): Promise<WeeklyReport> {
    return request("/v1/reports/weekly", { token, query: { week_key } });
  },
  triggerWeeklyReport(token: string, week_key: string): Promise<WeeklyReport> {
    return request("/v1/reports/weekly", { token, method: "POST", body: { week_key }, idempotent: true });
  },
};
