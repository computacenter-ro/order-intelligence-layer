"use client";

import { createContext, useCallback, useContext, useMemo, useState } from "react";
import type { ChatContext as ChatScope } from "@/lib/types";

/**
 * One assistant panel, opened from several places.
 *
 * The side-nav "Assistant" item, the alert drawer's "Ask about this" button and
 * the journey view's all open the SAME drawer — so its open/scope state lives
 * here rather than in each caller. Mounted once in ``AppShell``; anything under
 * the shell opens it with :func:`useChat`.
 *
 * Deliberately no persistence (no localStorage): a conversation is scoped to the
 * record you were looking at, and silently resurrecting yesterday's thread about
 * a different order would mislead more than it helps.
 */

interface ChatState {
  open: boolean;
  scope: ChatScope | null;
  scopeLabel?: string;
  /** Open the panel; pass a scope to anchor answers to one record. */
  openChat: (scope?: ChatScope | null, label?: string) => void;
  closeChat: () => void;
}

const Ctx = createContext<ChatState | null>(null);

export function ChatProvider({ children }: { children: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  const [scope, setScope] = useState<ChatScope | null>(null);
  const [scopeLabel, setScopeLabel] = useState<string | undefined>(undefined);

  const openChat = useCallback((next: ChatScope | null = null, label?: string) => {
    setScope(next);
    setScopeLabel(label);
    setOpen(true);
  }, []);

  const closeChat = useCallback(() => setOpen(false), []);

  const value = useMemo(
    () => ({ open, scope, scopeLabel, openChat, closeChat }),
    [open, scope, scopeLabel, openChat, closeChat]
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useChat(): ChatState {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useChat must be used within <ChatProvider>");
  return ctx;
}
