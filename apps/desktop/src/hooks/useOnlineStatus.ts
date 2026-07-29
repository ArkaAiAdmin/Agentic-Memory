/**
 * Offline / Connection Monitor
 *
 * Tracks browser online/offline status and exposes it via a hook so the UI
 * can show a banner when the network is down.
 */

import { useEffect, useState } from "react";

let listeners = new Set<(online: boolean) => void>();
let currentOnline = typeof navigator !== "undefined" ? navigator.onLine : true;

function notify() {
  for (const fn of listeners) fn(currentOnline);
}

if (typeof window !== "undefined") {
  window.addEventListener("online", () => { currentOnline = true; notify(); });
  window.addEventListener("offline", () => { currentOnline = false; notify(); });
}

export function useOnlineStatus(): boolean {
  const [online, setOnline] = useState(currentOnline);

  useEffect(() => {
    const fn = (v: boolean) => setOnline(v);
    listeners.add(fn);
    return () => { listeners.delete(fn); };
  }, []);

  return online;
}

export function isOnline(): boolean {
  return currentOnline;
}
