/**
 * ActivityBar — Vertical icon strip on the left edge.
 *
 * 52px bar with icons that toggle panels.
 * Redesigned with pill-style active indicator and grouped items.
 */

import React from "react";
import { useAppStore } from "../stores/appStore";

interface ActivityItem {
  id: string;
  icon: React.ReactNode;
  label: string;
  action: () => void;
  isActive?: boolean;
  badge?: number;
}

export function ActivityBar({
  onOpenProject,
  onOpenSettings,
  onOpenPalette,
}: {
  onOpenProject?: () => void;
  onOpenSettings?: () => void;
  onOpenPalette?: () => void;
}) {
  const {
    sidebarOpen,
    toggleSidebar,
    terminalOpen,
    toggleTerminal,
    rightPanelTab,
    setRightPanelTab,
    memoryHealth,
  } = useAppStore();

  const items: ActivityItem[] = [
    {
      id: "open",
      label: "Open Project",
      icon: <OpenIcon />,
      action: () => onOpenProject?.(),
    },
    {
      id: "explorer",
      label: "Explorer",
      icon: <ExplorerIcon />,
      action: toggleSidebar,
      isActive: sidebarOpen,
    },
    {
      id: "search",
      label: "Search Files",
      icon: <SearchIcon />,
      action: () => onOpenPalette?.(),
    },
    {
      id: "terminal",
      label: "Terminal",
      icon: <TerminalIcon />,
      action: toggleTerminal,
      isActive: terminalOpen,
    },
  ];

  const panelItems: ActivityItem[] = [
    {
      id: "chat",
      label: "Chat",
      icon: <ChatIcon />,
      action: () => setRightPanelTab("chat"),
      isActive: rightPanelTab === "chat",
    },
    {
      id: "git",
      label: "Git",
      icon: <GitIcon />,
      action: () => setRightPanelTab("git"),
      isActive: rightPanelTab === "git",
    },
    {
      id: "memory",
      label: "Memory",
      icon: <MemoryIcon />,
      action: () => setRightPanelTab("memory"),
      isActive: rightPanelTab === "memory",
      badge: memoryHealth === "unhealthy" || memoryHealth === "degraded" ? 1 : undefined,
    },
  ];

  const bottomItems: ActivityItem[] = [
    {
      id: "settings",
      label: "Settings",
      icon: <SettingsIcon />,
      action: () => onOpenSettings?.(),
    },
  ];

  return (
    <div style={{
      width: 52,
      minWidth: 52,
      background: "var(--bg-secondary)",
      borderRight: "1px solid var(--border-default)",
      display: "flex",
      flexDirection: "column",
      alignItems: "center",
      paddingTop: 10,
      paddingBottom: 10,
      gap: 2,
      flexShrink: 0,
    }}>
      {/* Top items */}
      {items.map((item) => (
        <BarItem key={item.id} item={item} />
      ))}

      {/* Divider */}
      <div style={{ width: 24, height: 1, background: "var(--border-subtle)", margin: "6px 0" }} />

      {/* Panel switchers */}
      {panelItems.map((item) => (
        <BarItem key={item.id} item={item} />
      ))}

      {/* Spacer */}
      <div style={{ flex: 1 }} />

      {/* Bottom items */}
      {bottomItems.map((item) => (
        <BarItem key={item.id} item={item} />
      ))}
    </div>
  );
}

function BarItem({ item }: { item: ActivityItem }) {
  return (
    <div style={{ position: "relative", marginBottom: 2 }} className="activity-bar-item">
      <button
        onClick={item.action}
        title={item.label}
        aria-label={item.label}
        aria-pressed={item.isActive || undefined}
        style={{
          width: 38,
          height: 38,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          background: item.isActive ? "var(--bg-hover)" : "transparent",
          border: "none",
          borderRadius: "var(--radius-md)",
          color: item.isActive ? "var(--text-primary)" : "var(--text-tertiary)",
          cursor: "pointer",
          position: "relative",
          transition: "all 0.15s ease",
        }}
        onMouseEnter={(e) => {
          if (!item.isActive) {
            e.currentTarget.style.color = "var(--text-secondary)";
            e.currentTarget.style.background = "var(--bg-hover)";
          }
        }}
        onMouseLeave={(e) => {
          if (!item.isActive) {
            e.currentTarget.style.color = "var(--text-tertiary)";
            e.currentTarget.style.background = "transparent";
          }
        }}
      >
        {item.icon}
        {item.badge && item.badge > 0 && (
          <span style={{
            position: "absolute", top: 6, right: 6,
            width: 7, height: 7, borderRadius: "50%", background: "var(--error)",
            border: "1.5px solid var(--bg-secondary)",
          }} />
        )}
      </button>
      {/* Active indicator pill */}
      {item.isActive && (
        <div style={{
          position: "absolute", left: 0, top: "50%", transform: "translateY(-50%)",
          width: 3, height: 18, borderRadius: 3,
          background: "var(--accent)",
        }} />
      )}
    </div>
  );
}

// ── Monochrome SVG Icons (18x18) ────────────────────────────────────────

const iconStyle: React.CSSProperties = { width: 18, height: 18 };

function OpenIcon() {
  return (
    <svg style={iconStyle} viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M2 5h5l2 2h7v7H2V5z" />
      <path d="M9 9v4M7 11h4" />
    </svg>
  );
}

function ExplorerIcon() {
  return (
    <svg style={iconStyle} viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M2 4h5l2 2h7v8H2V4z" />
    </svg>
  );
}

function SearchIcon() {
  return (
    <svg style={iconStyle} viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="8" cy="8" r="5" />
      <path d="M12 12l4 4" />
    </svg>
  );
}

function ChatIcon() {
  return (
    <svg style={iconStyle} viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M3 3h12a1 1 0 0 1 1 1v8a1 1 0 0 1-1 1H6l-3 3V4a1 1 0 0 1 1-1z" />
    </svg>
  );
}

function TerminalIcon() {
  return (
    <svg style={iconStyle} viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <rect x="2" y="3" width="14" height="12" rx="1.5" />
      <path d="M5 8l3 2.5L5 13" />
      <path d="M9 13h4" />
    </svg>
  );
}

function GitIcon() {
  return (
    <svg style={iconStyle} viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="5.5" cy="4.5" r="1.5" />
      <circle cx="5.5" cy="13.5" r="1.5" />
      <circle cx="12.5" cy="9" r="1.5" />
      <path d="M5.5 6v7" />
      <path d="M7 4.5l4 4.5" />
    </svg>
  );
}

function MemoryIcon() {
  return (
    <svg style={iconStyle} viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="9" cy="9" r="6.5" />
      <path d="M9 5v4l3 2" />
    </svg>
  );
}

function SettingsIcon() {
  return (
    <svg style={iconStyle} viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M7 2h4l.5 1.5c.4.2.8.5 1.1.8l1.4-.6 2 3.5-1.2 1c.1.3.1.6.1.9s0 .6-.1.9l1.2 1-2 3.5-1.4-.6c-.3.3-.7.6-1.1.8L11 16H7l-.5-1.5c-.4-.2-.8-.5-1.1-.8l-1.4.6-2-3.5 1.2-1c-.1-.3-.1-.6-.1-.9s0-.6.1-.9l-1.2-1 2-3.5 1.4.6c.3-.3.7-.6 1.1-.8L7 2z" />
      <circle cx="9" cy="9" r="2" />
    </svg>
  );
}
