import type { ReactNode } from "react";
import { friendlyMessage } from "../api/errors";

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="state state--loading" role="status" aria-live="polite">
      {label}
    </div>
  );
}

export function EmptyState({ message, children }: { message: string; children?: ReactNode }) {
  return (
    <div className="state state--empty">
      <p>{message}</p>
      {children}
    </div>
  );
}

export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  return (
    <div className="state state--error" role="alert">
      <p>{friendlyMessage(error)}</p>
      {onRetry && (
        <button type="button" onClick={onRetry}>
          Try again
        </button>
      )}
    </div>
  );
}

// Inline banner for action-level errors (e.g. 409 claim conflict) that must not crash the page.
export function ErrorBanner({ error, onDismiss }: { error: unknown; onDismiss?: () => void }) {
  return (
    <div className="banner banner--error" role="alert">
      <span>{friendlyMessage(error)}</span>
      {onDismiss && (
        <button type="button" aria-label="Dismiss" onClick={onDismiss}>
          ×
        </button>
      )}
    </div>
  );
}
