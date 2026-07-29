/**
 * useHotkeys — Global keyboard shortcut handler.
 *
 * Registers keybindings from the command registry and dispatches
 * to the appropriate command when triggered.
 */

import { useEffect } from "react";
import { commandRegistry } from "../services/commands";

/** Parse a keybinding string like "Cmd+K" or "Ctrl+Shift+P" into components. */
function parseKeybinding(binding: string): {
  key: string;
  ctrl: boolean;
  meta: boolean;
  shift: boolean;
  alt: boolean;
} {
  const parts = binding.split("+").map((p) => p.trim().toLowerCase());
  const key = parts.find(
    (p) =>
      p !== "ctrl" &&
      p !== "cmd" &&
      p !== "meta" &&
      p !== "shift" &&
      p !== "alt",
  ) ?? "";
  return {
    key,
    ctrl: parts.includes("ctrl"),
    meta: parts.includes("cmd") || parts.includes("meta"),
    shift: parts.includes("shift"),
    alt: parts.includes("alt"),
  };
}

function matchKeybinding(
  event: KeyboardEvent,
  parsed: ReturnType<typeof parseKeybinding>,
): boolean {
  const isMac = (navigator as any).userAgentData?.platform?.includes("Mac") ?? navigator.platform.includes("Mac");
  const ctrlMatch = parsed.ctrl ? (isMac ? event.metaKey : event.ctrlKey) : true;
  const metaMatch = parsed.meta ? (isMac ? event.metaKey : event.ctrlKey) : true;
  const shiftMatch = parsed.shift ? event.shiftKey : !event.shiftKey;
  const altMatch = parsed.alt ? event.altKey : !event.altKey;

  const eventKey = event.key.toLowerCase();
  const keyMatch = eventKey === parsed.key || event.code.toLowerCase() === `key${parsed.key}`;

  return keyMatch && ctrlMatch && metaMatch && shiftMatch && altMatch;
}

/**
 * Hook that registers all command keybindings as global event listeners.
 * Mount once at the App level.
 */
export function useHotkeys() {
  useEffect(() => {
    function handleKeyDown(event: KeyboardEvent) {
      // Don't intercept when typing in inputs/textareas
      const tag = (event.target as HTMLElement)?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA") return;

      const bindings = commandRegistry.getKeybindings();
      for (const { keybinding, commandId } of bindings) {
        const parsed = parseKeybinding(keybinding);
        if (matchKeybinding(event, parsed)) {
          event.preventDefault();
          event.stopPropagation();
          const cmd = commandRegistry.get(commandId);
          if (cmd) {
            cmd.run();
          }
          return;
        }
      }
    }

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, []);
}
