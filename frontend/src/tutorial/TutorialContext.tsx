import { createContext, useContext } from "react";

export const SEEN_PREFIX = "wop.tutorial.seen.";

export interface TutorialState {
  open: boolean;
  start: () => void;
  close: () => void;
  markSeen: (userId: string) => void;
  hasSeen: (userId: string) => boolean;
}

export const TutorialCtx = createContext<TutorialState | undefined>(undefined);

// Non-component exports only (hook + context). The Provider lives in TutorialProvider.tsx.
export function useTutorial(): TutorialState {
  const ctx = useContext(TutorialCtx);
  if (!ctx) throw new Error("useTutorial must be used within TutorialProvider");
  return ctx;
}
