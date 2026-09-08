import { useMemo, useState, type ReactNode } from "react";
import type { UserContext } from "../types";
import { AuthCtx, STORAGE_KEY, loadStored, type AuthState } from "./AuthContext";

// Component-only module (satisfies react-refresh/only-export-components).
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
