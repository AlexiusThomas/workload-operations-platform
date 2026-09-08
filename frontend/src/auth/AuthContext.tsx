import { createContext, useContext } from "react";
import type { UserContext } from "../types";

export const STORAGE_KEY = "wop.synthetic.user";

export interface AuthState {
  user: UserContext | null;
  signIn: (user: UserContext) => void;
  signOut: () => void;
}

export const AuthCtx = createContext<AuthState | undefined>(undefined);

export function loadStored(): UserContext | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as UserContext) : null;
  } catch {
    return null;
  }
}

// Non-component exports only (hook + context) so react-refresh Fast Refresh is preserved.
// The AuthProvider component lives in AuthProvider.tsx.
export function useAuth(): AuthState {
  const ctx = useContext(AuthCtx);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
