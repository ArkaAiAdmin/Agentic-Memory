import React, { useEffect, useRef, useCallback, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { WebLinksAddon } from "@xterm/addon-web-links";
import "@xterm/xterm/css/xterm.css";
import { terminal as terminalIpc } from "../../ipc/client";
import { useAppStore } from "../../stores/appStore";
import { getThemeById } from "../../services/themes";

interface TerminalTab {
  id: string;
  ptyId: string | null;
  term: Terminal;
  fitAddon: FitAddon;
  unsubOutput: (() => void) | null;
  unsubExit: (() => void) | null;
  divRef: HTMLDivElement | null;
}

export function TerminalPanel() {
  const containerRef = useRef<HTMLDivElement>(null);
  const tabsRef = useRef<Map<string, TerminalTab>>(new Map());
  const terminalRefs = useRef<Map<string, HTMLDivElement>>(new Map());
  const [activeTabId, setActiveTabId] = useState<string | null>(null);
  const [tabList, setTabList] = useState<string[]>([]);
  const theme = useAppStore((s) => s.theme);

  const getTerminalTheme = useCallback(() => {
    const palette = getThemeById(theme);
    return {
      background: palette.terminalBg,
      foreground: palette.terminalFg,
      cursor: palette.editorCursor,
      selectionBackground: palette.editorSelection,
      black: palette.terminalBlack,
      red: palette.terminalRed,
      green: palette.terminalGreen,
      yellow: palette.terminalYellow,
      blue: palette.terminalBlue,
      magenta: palette.terminalMagenta,
      cyan: palette.terminalCyan,
      white: palette.terminalWhite,
      brightBlack: palette.terminalBlack,
      brightRed: palette.terminalRed,
      brightGreen: palette.terminalGreen,
      brightYellow: palette.terminalYellow,
      brightBlue: palette.terminalBlue,
      brightMagenta: palette.terminalMagenta,
      brightCyan: palette.terminalCyan,
      brightWhite: palette.terminalWhite,
    };
  }, [theme]);

  const createTab = useCallback(async () => {
    if (!containerRef.current) return;

    const id = `term-${Date.now()}`;
    const termDiv = document.createElement("div");
    termDiv.style.height = "100%";
    termDiv.style.display = "none";
    containerRef.current.appendChild(termDiv);
    terminalRefs.current.set(id, termDiv);

    const fitAddon = new FitAddon();
    const webLinksAddon = new WebLinksAddon();

    const term = new Terminal({
      cursorBlink: true,
      cursorStyle: "bar",
      fontSize: 12,
      fontFamily: "var(--font-mono)",
      lineHeight: 1.4,
      theme: getTerminalTheme(),
      allowProposedApi: true,
      scrollback: 10000,
    });

    term.loadAddon(fitAddon);
    term.loadAddon(webLinksAddon);
    term.open(termDiv);

    requestAnimationFrame(() => { try { fitAddon.fit(); } catch { /* ignore fit errors */ } });

    const tab: TerminalTab = { id, ptyId: null, term, fitAddon, unsubOutput: null, unsubExit: null, divRef: termDiv };
    tabsRef.current.set(id, tab);
    setTabList((prev) => [...prev, id]);
    setActiveTabId(id);
    termDiv.style.display = "block";

    try {
      const ptyId = await terminalIpc.create("/", 80, 24);
      tab.ptyId = ptyId;
      tab.unsubOutput = await terminalIpc.onOutput((data) => { if (data.ptyId === ptyId) term.write(data.data); });
      tab.unsubExit = await terminalIpc.onExit((data) => { if (data.ptyId === ptyId) term.write(`\r\n[exited ${data.exitCode}]\r\n`); });
      term.onData((data) => { if (tab.ptyId) terminalIpc.write(tab.ptyId, data).catch(() => {}); });
      term.onResize(({ cols, rows }) => { if (tab.ptyId) terminalIpc.resize(tab.ptyId, cols, rows).catch(() => {}); });
    } catch (err) {
      term.write(`\r\n[error: ${err}]\r\n`);
    }
  }, [getTerminalTheme]);

  useEffect(() => {
    createTab();
    return () => {
      // eslint-disable-next-line react-hooks/exhaustive-deps
      for (const [, tab] of tabsRef.current) {
        tab.unsubOutput?.();
        tab.unsubExit?.();
        tab.term.dispose();
        if (tab.ptyId) terminalIpc.destroy(tab.ptyId).catch(() => {});
      }
      tabsRef.current.clear();
      // eslint-disable-next-line react-hooks/exhaustive-deps
      terminalRefs.current.clear();
    };
  }, [createTab]);

  // Update terminal theme when theme changes
  useEffect(() => {
    for (const [, tab] of tabsRef.current) {
      // eslint-disable-next-line react-hooks/immutability
      tab.term.options.theme = getTerminalTheme();
    }
  }, [theme, getTerminalTheme]);

  const switchTab = useCallback((id: string) => {
    for (const [, div] of terminalRefs.current) {
      // eslint-disable-next-line react-hooks/immutability
      div.style.display = "none";
    }
    const activeTab = tabsRef.current.get(id);
    if (activeTab) {
      const el = terminalRefs.current.get(id);
      if (el) el.style.display = "block";
      requestAnimationFrame(() => activeTab.fitAddon.fit());
    }
    setActiveTabId(id);
  }, []);

  const closeTab = useCallback((id: string) => {
    const tab = tabsRef.current.get(id);
    if (tab) {
      tab.unsubOutput?.();
      tab.unsubExit?.();
      tab.term.dispose();
      if (tab.ptyId) terminalIpc.destroy(tab.ptyId).catch(() => {});
      const div = terminalRefs.current.get(id);
      if (div) div.remove();
      terminalRefs.current.delete(id);
      tabsRef.current.delete(id);
      setTabList((prev) => prev.filter((t) => t !== id));
      if (activeTabId === id) {
        const remaining = Array.from(tabsRef.current.keys());
        if (remaining.length > 0) switchTab(remaining[remaining.length - 1]);
        else setActiveTabId(null);
      }
    }
  }, [activeTabId, switchTab]);

  useEffect(() => {
    const handleResize = () => {
      if (activeTabId) {
        const tab = tabsRef.current.get(activeTabId);
        if (tab) requestAnimationFrame(() => tab.fitAddon.fit());
      }
    };
    window.addEventListener("resize", handleResize);
    return () => window.removeEventListener("resize", handleResize);
  }, [activeTabId]);

  return (
    <div style={{ height: "100%", display: "flex", flexDirection: "column", background: "var(--bg-primary)", overflow: "hidden" }}>
      <div style={{
        display: "flex", alignItems: "center", background: "var(--bg-secondary)",
        borderBottom: "1px solid var(--border-default)", minHeight: 32, flexShrink: 0,
      }} role="tablist" aria-label="Terminal tabs">
        <div style={{ display: "flex", overflow: "auto", flex: 1 }}>
          {tabList.map((id, i) => (
            <div key={id} onClick={() => switchTab(id)} style={{
              padding: "5px 12px", fontSize: 11, cursor: "pointer",
              color: activeTabId === id ? "var(--text-primary)" : "var(--text-tertiary)",
              background: activeTabId === id ? "var(--bg-tertiary)" : "transparent",
              borderBottom: activeTabId === id ? "2px solid var(--accent)" : "2px solid transparent",
              display: "flex", alignItems: "center", gap: 6, whiteSpace: "nowrap", transition: "all 0.1s",
            }} role="tab" aria-selected={activeTabId === id} tabIndex={activeTabId === id ? 0 : -1}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  switchTab(id);
                }
              }}>
              <span style={{ fontSize: 10 }}>▸</span>
              <span>Terminal {i + 1}</span>
              {tabList.length > 1 && (
                <span onClick={(e) => { e.stopPropagation(); closeTab(id); }}
                  style={{ fontSize: 10, color: "var(--text-tertiary)", cursor: "pointer", padding: "0 2px", borderRadius: "var(--radius-xs)" }}
                  onMouseEnter={(e) => e.currentTarget.style.color = "var(--error)"}
                  onMouseLeave={(e) => e.currentTarget.style.color = "var(--text-tertiary)"}
                >×</span>
              )}
            </div>
          ))}
        </div>
        <button onClick={() => createTab()} style={{
          background: "none", border: "none", color: "var(--text-tertiary)",
          cursor: "pointer", padding: "4px 10px", fontSize: 14, borderRadius: "var(--radius-sm)",
        }}
        onMouseEnter={(e) => e.currentTarget.style.color = "var(--text-primary)"}
        onMouseLeave={(e) => e.currentTarget.style.color = "var(--text-tertiary)"}
        title="New Terminal">+</button>
      </div>
      <div ref={containerRef} style={{ flex: 1, overflow: "hidden", padding: "4px 0" }} />
    </div>
  );
}
