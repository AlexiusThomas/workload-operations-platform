import type { ApiErrorBody } from "../types";

// Error codes mirror backend/observability/errors.py.
export type ApiErrorCode =
  | "UNAUTHENTICATED"
  | "AUTHORIZATION_DENIED"
  | "VALIDATION_ERROR"
  | "INVALID_STATE_TRANSITION"
  | "CLAIM_CONFLICT"
  | "MATERIAL_CLAIM_CONFLICT"
  | "IDEMPOTENCY_CONFLICT"
  | "RESOURCE_NOT_FOUND"
  | "QUANTITY_EXCEEDED"
  | "TOTE_REQUIRED"
  | "INCOMPLETE_WORK"
  | "INGESTION_SCHEMA_ERROR"
  | "INTERNAL_ERROR"
  | "NETWORK_ERROR";

export class ApiError extends Error {
  readonly code: ApiErrorCode;
  readonly status: number;
  readonly requestId: string | null;

  constructor(code: ApiErrorCode, message: string, status: number, requestId: string | null = null) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
    this.requestId = requestId;
  }

  // True for 409 conflicts (claim/material-claim/idempotency).
  get isConflict(): boolean {
    return this.status === 409;
  }
}

// Plain-language messages so validation/conflict/error responses never crash the UI and
// are understandable. Keyed by the stable backend error code.
const FRIENDLY: Record<string, string> = {
  UNAUTHENTICATED: "Your session is not valid. Please sign in again.",
  AUTHORIZATION_DENIED: "You do not have permission to perform this action.",
  VALIDATION_ERROR: "Some details are invalid. Please review and try again.",
  INVALID_STATE_TRANSITION: "This action is not allowed for the item's current status.",
  CLAIM_CONFLICT: "Someone else already claimed this work unit. Refresh to see the latest status.",
  MATERIAL_CLAIM_CONFLICT: "This material requirement was just claimed by someone else. Refresh to continue.",
  IDEMPOTENCY_CONFLICT: "This request is already being processed. Please wait a moment.",
  RESOURCE_NOT_FOUND: "That item could not be found. It may have been removed.",
  QUANTITY_EXCEEDED: "The completed quantity cannot exceed the required quantity.",
  TOTE_REQUIRED: "A tote must be assigned before this step.",
  INCOMPLETE_WORK: "All required quantity must be completed before preparation can be marked complete.",
  INGESTION_SCHEMA_ERROR: "The uploaded workload file did not match the expected format.",
  INTERNAL_ERROR: "Something went wrong on our side. Please try again shortly.",
  NETWORK_ERROR: "Could not reach the server. Check your connection and try again.",
};

export function friendlyMessage(err: unknown): string {
  if (err instanceof ApiError) {
    return FRIENDLY[err.code] ?? err.message ?? FRIENDLY.INTERNAL_ERROR;
  }
  return FRIENDLY.INTERNAL_ERROR;
}

export function toApiError(status: number, body: unknown): ApiError {
  const parsed = body as Partial<ApiErrorBody> | undefined;
  const err = parsed?.error;
  const code = (err?.code as ApiErrorCode) ?? "INTERNAL_ERROR";
  const message = err?.message ?? "Unexpected error";
  return new ApiError(code, message, status, err?.request_id ?? null);
}
