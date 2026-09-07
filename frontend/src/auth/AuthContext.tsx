import { createContext, useContext, useMemo, useState, type ReactNode } from "react";
import type { UserContext } from "../types";

const STORAGE_KEY = "wop.synthetic.user";

interface AuthState {
  user: UserContext | null;
  signIn: (user: UserContext) => void;
  signOut: () => void;
}

const AuthCtx = createContext<AuthState | undefined>(undefined);

function loadStored(): UserContext | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as UserContext) : null;
  } catch {
    return null;
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<UserContext | null>(loadStored);

  const value = useMemo<AuthState>(
    () => ({
      user,
      signIn: (u) => {
        setUser(u);
        try {
          localStorage.setItem(STORAGE_KEY, JSON.stringify(u));
        } catch {
          /* storage unavailable; keep in-memory only */
        }
      },
      signOut: () => {
        setUser(null);
        try {
          localStorage.removeItem(STORAGE_KEY);
        } catch {
          /* ignore */
        }
      },
    }),
    [user],
  );

  return <AuthCtx.Provider value={value}>{children}</AuthCtx.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthCtx);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
