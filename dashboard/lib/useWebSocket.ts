"use client";

import { useEffect, useRef, useState } from "react";
import type { WsEvent } from "@/lib/types";

const WS_URL = process.env.NEXT_PUBLIC_WS_URL ?? "ws://localhost:8000/ws";

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
      socket = new WebSocket(WS_URL);

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
