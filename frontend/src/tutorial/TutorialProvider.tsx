import { useCallback, useMemo, useState, type ReactNode } from "react";
import { SEEN_PREFIX, TutorialCtx, type TutorialState } from "./TutorialContext";

// Component-only module (satisfies react-refresh/only-export-components).
export function TutorialProvider({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState(false);

  const hasSeen = useCallback((userId: string) => {
    try {
      return localStorage.getItem(SEEN_PREFIX + userId) === "1";
    } catch {
      return false;
    }
  }, []);

  const markSeen = useCallback((userId: string) => {
    try {
      localStorage.setItem(SEEN_PREFIX + userId, "1");
    } catch {
      /* ignore */
    }
  }, []);

  const value = useMemo<TutorialState>(
    () => ({
      open,
      start: () => setOpen(true),
      close: () => setOpen(false),
      markSeen,
      hasSeen,
    }),
    [open, markSeen, hasSeen],
  );

  return <TutorialCtx.Provider value={value}>{children}</TutorialCtx.Provider>;
}
