"use client";

import { useEffect, useRef, useState } from "react";
import type { WsEvent } from "@/lib/types";

// Configured at BUILD time (Next inlines NEXT_PUBLIC_*; see dashboard/Dockerfile).
// Either a relative path ("/ws", the production default behind the reverse proxy)
// or an absolute ws(s):// URL (split-origin / local dev against :8000).
const WS_URL_CONFIG = process.env.NEXT_PUBLIC_WS_URL ?? "ws://localhost:8000/ws";

/**
 * Resolve the configured WS URL to an absolute ws(s):// URL.
 *
 * `new WebSocket()` REQUIRES an absolute URL with a ws:// or wss:// scheme — it
 * throws SyntaxError on a relative path, unlike `fetch`, which happily resolves
 * one against the page origin. So a relative config value has to be resolved
 * here, against `window.location`.
 *
 * Resolving at call time (not module scope) is deliberate: `window` does not
 * exist while Next pre-renders on the server, so reading it at import would
 * break the build/SSR pass.
 *
 * Deriving the scheme from `window.location.protocol` is what makes one image
 * work on both http:// and https:// — an https:// page MUST use wss:// (a
 * browser blocks mixed-content ws:// from a secure page), and that is exactly
 * the production case behind TLS.
 */
function resolveWsUrl(): string {
  if (/^wss?:\/\//i.test(WS_URL_CONFIG)) return WS_URL_CONFIG; // already absolute
  const { protocol, host } = window.location;
  const scheme = protocol === "https:" ? "wss:" : "ws:";
  const path = WS_URL_CONFIG.startsWith("/") ? WS_URL_CONFIG : `/${WS_URL_CONFIG}`;
  return `${scheme}//${host}${path}`;
}

export type ConnectionStatus = "live" | "reconnecting" | "disconnected";

const INITIAL_RETRY_DELAY_MS = 1000;
const MAX_RETRY_DELAY_MS = 15000;

export function useWebSocket(onEvent: (event: WsEvent) => void): ConnectionStatus {
  const [status, setStatus] = useState<ConnectionStatus>("disconnected");
  const onEventRef = useRef(onEvent);
  useEffect(() => {
    onEventRef.current = onEvent;
  });

  useEffect(() => {
    let active = true;
    let socket: WebSocket | null = null;
    let retryDelay = INITIAL_RETRY_DELAY_MS;
    let retryTimeout: ReturnType<typeof setTimeout> | null = null;

    function connect() {
      socket = new WebSocket(resolveWsUrl());

      socket.onopen = () => {
        if (!active) return;
        retryDelay = INITIAL_RETRY_DELAY_MS;
        setStatus("live");
      };

      socket.onmessage = (event) => {
        if (!active) return;
        try {
          onEventRef.current(JSON.parse(event.data) as WsEvent);
        } catch {
          // malformed frame — drop it, never let one bad message kill the socket
        }
      };

      socket.onclose = () => {
        if (!active) return;
        setStatus("reconnecting");
        retryTimeout = setTimeout(connect, retryDelay);
        retryDelay = Math.min(retryDelay * 2, MAX_RETRY_DELAY_MS);
      };

      socket.onerror = () => {
        socket?.close();
      };
    }

    connect();

    return () => {
      active = false;
      if (retryTimeout) clearTimeout(retryTimeout);
      socket?.close();
    };
  }, []);

  return status;
}
