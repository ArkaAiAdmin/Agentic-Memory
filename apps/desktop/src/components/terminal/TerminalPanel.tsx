import React, { useEffect, useRef, useCallback, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { WebLinksAddon } from "@xterm/addon-web-links";
import "@xterm/xterm/css/xterm.css";
import { terminal as terminalIpc } from "../../ipc/client";

interface TerminalTab {
  id: string;
  ptyId: string | null;
  term: Terminal;
  fitAddon: FitAddon;
}

export function TerminalPanel() {
  const containerRef = useRef<HTMLDivElement>(null);
  const tabsRef = useRef<Map<string, TerminalTab>>(new Map());
  const [activeTabId, setActiveTabId] = useState<string | null>(null);
  const [tabList, setTabList] = useState<string[]>([]);

  // Initialize default terminal tab
  useEffect(() => {
    createTab();
    return () => {
      // Cleanup all terminals on unmount
      for (const [id, tab] of tabsRef.current) {
        tab.term.dispose();
        if (tab.ptyId) {
          terminalIpc.destroy(tab.ptyId).catch(() => {});
        }
      }
      tabsRef.current.clear();
    };
  }, []);

  const createTab = useCallback(async () => {
    if (!containerRef.current) return;

    const id = `term-${Date.now()}`;
    const termDiv = document.createElement("div");
    termDiv.id = `terminal-${id}`;
    termDiv.style.height = "100%";
    termDiv.style.display = id === activeTabId ? "block" : "none";
    containerRef.current.appendChild(termDiv);

    const fitAddon = new FitAddon();
    const webLinksAddon = new WebLinksAddon();

    const term = new Terminal({
      cursorBlink: true,
      cursorStyle: "bar",
      fontSize: 13,
      fontFamily: "'JetBrains Mono', 'Fira Code', 'Cascadia Code', monospace",
      theme: {
        background: "#0d1117",
        foreground: "#c9d1d9",
        cursor: "#58a6ff",
        selectionBackground: "#264f78",
        black: "#0d1117",
        red: "#ff7b72",
        green: "#3fb950",
        yellow: "#d29922",
        blue: "#58a6ff",
        magenta: "#bc8cff",
        cyan: "#39c5cf",
        white: "#c9d1d9",
        brightBlack: "#484f58",
        brightRed: "#ffa198",
        brightGreen: "#56d364",
        brightYellow: "#e3b341",
        brightBlue: "#79c0ff",
        brightMagenta: "#d2a8ff",
        brightCyan: "#56d4dd",
        brightWhite: "#f0f6fc",
      },
      allowProposedApi: true,
      scrollback: 10000,
    });

    term.loadAddon(fitAddon);
    term.loadAddon(webLinksAddon);
    term.open(termDiv);

    // Fit after a short delay to ensure container is sized
    requestAnimationFrame(() => {
      try { fitAddon.fit(); } catch {}
    });

    const tab: TerminalTab = { id, ptyId: null, term, fitAddon };
    tabsRef.current.set(id, tab);
    setTabList((prev) => [...prev, id]);
    setActiveTabId(id);

    // Create PTY in Tauri backend
    try {
      const cwd = "/"; // Default to root; in production, use project root
      const ptyId = await terminalIpc.create(cwd, 80, 24);
      tab.ptyId = ptyId;

      // Listen for PTY output
      terminalIpc.onOutput((data) => {
        if (data.ptyId === ptyId) {
          term.write(data.data);
        }
      });

      // Listen for PTY exit
      terminalIpc.onExit((data) => {
        if (data.ptyId === ptyId) {
          term.write(`\r\n[Process exited with code ${data.exitCode}]\r\n`);
        }
      });

      // Send terminal input to PTY
      term.onData((data) => {
        if (tab.ptyId) {
          terminalIpc.write(tab.ptyId, data).catch(() => {});
        }
      });

      // Handle resize
      term.onResize(({ cols, rows }) => {
        if (tab.ptyId) {
          terminalIpc.resize(tab.ptyId, cols, rows).catch(() => {});
        }
      });
    } catch (err) {
      term.write(`\r\n[Failed to create PTY: ${err}]\r\n`);
    }
  }, [activeTabId]);

  const switchTab = useCallback((id: string) => {
    // Hide all tabs
    for (const [tabId, tab] of tabsRef.current) {
      const el = document.getElementById(`terminal-${tabId}`);
      if (el) el.style.display = "none";
    }

    // Show selected tab
    const activeTab = tabsRef.current.get(id);
    if (activeTab) {
      const el = document.getElementById(`terminal-${id}`);
      if (el) el.style.display = "block";
      requestAnimationFrame(() => activeTab.fitAddon.fit());
    }

    setActiveTabId(id);
  }, []);

  const closeTab = useCallback(
    (id: string) => {
      const tab = tabsRef.current.get(id);
      if (tab) {
        tab.term.dispose();
        if (tab.ptyId) {
          terminalIpc.destroy(tab.ptyId).catch(() => {});
        }
        const el = document.getElementById(`terminal-${id}`);
        if (el) el.remove();
        tabsRef.current.delete(id);
        setTabList((prev) => prev.filter((t) => t !== id));

        // Switch to another tab if closing active
        if (activeTabId === id) {
          const remaining = Array.from(tabsRef.current.keys());
          if (remaining.length > 0) {
            switchTab(remaining[remaining.length - 1]);
          } else {
            setActiveTabId(null);
          }
        }
      }
    },
    [activeTabId, switchTab],
  );

  // Fit on resize
  useEffect(() => {
    const handleResize = () => {
      if (activeTabId) {
        const tab = tabsRef.current.get(activeTabId);
        if (tab) {
          requestAnimationFrame(() => tab.fitAddon.fit());
        }
      }
    };
    window.addEventListener("resize", handleResize);
    return () => window.removeEventListener("resize", handleResize);
  }, [activeTabId]);

  return (
    <div
      style={{
        height: "100%",
        display: "flex",
        flexDirection: "column",
        background: "#0d1117",
      }}
    >
      {/* Tab bar */}
      <div
        style={{
          display: "flex",
          background: "#161b22",
          borderBottom: "1px solid #21262d",
          alignItems: "center",
        }}
      >
        <div style={{ display: "flex", overflow: "auto", flex: 1 }}>
          {tabList.map((id, i) => (
            <div
              key={id}
              onClick={() => switchTab(id)}
              style={{
                padding: "4px 12px",
                fontSize: 11,
                cursor: "pointer",
                color: activeTabId === id ? "#c9d1d9" : "#666",
                background: activeTabId === id ? "#0d1117" : "transparent",
                borderBottom:
                  activeTabId === id ? "2px solid #58a6ff" : "2px solid transparent",
                display: "flex",
                alignItems: "center",
                gap: 6,
                whiteSpace: "nowrap",
              }}
            >
              <span>Terminal {i + 1}</span>
              {tabList.length > 1 && (
                <span
                  onClick={(e) => {
                    e.stopPropagation();
                    closeTab(id);
                  }}
                  style={{
                    fontSize: 10,
                    color: "#666",
                    cursor: "pointer",
                    padding: "0 2px",
                  }}
                >
                  ×
                </span>
              )}
            </div>
          ))}
        </div>
        <button
          onClick={() => createTab()}
          style={{
            background: "none",
            border: "none",
            color: "#666",
            cursor: "pointer",
            padding: "4px 8px",
            fontSize: 14,
          }}
          title="New Terminal"
        >
          +
        </button>
      </div>

      {/* Terminal container */}
      <div ref={containerRef} style={{ flex: 1, overflow: "hidden" }} />
    </div>
  );
}
