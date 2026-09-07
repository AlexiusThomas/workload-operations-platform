import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";

const SEEN_PREFIX = "wop.tutorial.seen.";

interface TutorialState {
  open: boolean;
  start: () => void;
  close: () => void;
  markSeen: (userId: string) => void;
  hasSeen: (userId: string) => boolean;
}

const Ctx = createContext<TutorialState | undefined>(undefined);

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

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useTutorial(): TutorialState {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useTutorial must be used within TutorialProvider");
  return ctx;
}
