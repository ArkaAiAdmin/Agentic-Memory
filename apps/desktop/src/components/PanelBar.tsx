/**
 * PanelBar — Vertical icon strip on the right edge.
 * State-based tooltips for reliable visibility.
 */

import React, { useState, useRef, useEffect } from "react";
import { useAppStore, type RightPanelTab } from "../stores/appStore";

interface PanelItem {
  id: RightPanelTab;
  icon: React.ReactNode;
  label: string;
}

const PANEL_ITEMS: PanelItem[] = [
  { id: "chat", icon: <ChatIcon />, label: "Chat" },
  { id: "composer", icon: <ComposerIcon />, label: "Composer" },
  { id: "git", icon: <GitIcon />, label: "Git" },
  { id: "tasks", icon: <TasksIcon />, label: "Tasks" },
  { id: "goals", icon: <GoalsIcon />, label: "Goals" },
  { id: "worktrees", icon: <TreesIcon />, label: "Trees" },
  { id: "memory", icon: <MemoryIcon />, label: "Memory" },
  { id: "beliefs", icon: <BeliefsIcon />, label: "Beliefs" },
  { id: "skills", icon: <SkillsIcon />, label: "Skills" },
  { id: "workers", icon: <WorkersIcon />, label: "Workers" },
];

export function PanelBar() {
  const { rightPanelTab, setRightPanelTab } = useAppStore();
  const [hovered, setHovered] = useState<string | null>(null);
  const [tooltipPos, setTooltipPos] = useState<{ top: number; left: number } | null>(null);
  const hoveredRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    if (hovered && hoveredRef.current) {
      const rect = hoveredRef.current.getBoundingClientRect();
      setTooltipPos({
        top: rect.top + rect.height / 2,
        left: rect.left - 8,
      });
    } else {
      setTooltipPos(null);
    }
  }, [hovered]);

  return (
    <div style={{
      width: 40,
      minWidth: 40,
      background: "var(--bg-secondary)",
      borderLeft: "1px solid var(--border-default)",
      display: "flex",
      flexDirection: "column",
      alignItems: "center",
      paddingTop: 8,
      gap: 2,
      flexShrink: 0,
      position: "relative",
    }}>
      {PANEL_ITEMS.map((item) => {
        const isActive = rightPanelTab === item.id;
        const isHovered = hovered === item.id;
        return (
          <div key={item.id}>
            <button
              ref={isHovered ? hoveredRef : undefined}
              onClick={() => setRightPanelTab(item.id)}
              onMouseEnter={() => setHovered(item.id)}
              onMouseLeave={() => setHovered(null)}
              aria-label={item.label}
              aria-pressed={isActive || undefined}
              role="tab"
              aria-selected={isActive}
              style={{
                width: 34,
                height: 34,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                background: isHovered ? "var(--bg-hover)" : "transparent",
                border: "none",
                borderLeft: isActive ? "2px solid var(--accent)" : "2px solid transparent",
                color: isActive ? "var(--text-primary)" : "var(--text-tertiary)",
                cursor: "pointer",
                borderRadius: 0,
                transition: "color 0.12s, background 0.12s",
              }}
            >
              {item.icon}
            </button>
            {/* Tooltip — rendered as fixed overlay to avoid clipping by ResizablePane overflow:hidden */}
            {isHovered && tooltipPos && (
              <div style={{
                position: "fixed",
                top: tooltipPos.top,
                left: tooltipPos.left,
                transform: "translate(-100%, -50%)",
                padding: "4px 8px",
                borderRadius: "var(--radius-sm)",
                background: "var(--bg-elevated)",
                border: "1px solid var(--border-default)",
                boxShadow: "var(--shadow-md)",
                fontSize: 11,
                color: "var(--text-primary)",
                whiteSpace: "nowrap",
                pointerEvents: "none",
                zIndex: 9999,
              }}>
                {item.label}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ── Icons ────────────────────────────────────────────────────────────────

const s: React.CSSProperties = { width: 16, height: 16 };

function ChatIcon() {
  return <svg style={s} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M2 2h12a1 1 0 0 1 1 1v8a1 1 0 0 1-1 1H5l-3 3V3a1 1 0 0 1 1-1z" /></svg>;
}
function ComposerIcon() {
  return <svg style={s} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M10 1H4a1 1 0 0 0-1 1v11a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1V5l-4-4z" /><path d="M9 1v4h4" /></svg>;
}
function GitIcon() {
  return <svg style={s} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><circle cx="5" cy="4" r="1.5" /><circle cx="5" cy="12" r="1.5" /><circle cx="11" cy="8" r="1.5" /><path d="M5 5.5V10.5M6.5 4L9.5 8" /></svg>;
}
function TasksIcon() {
  return <svg style={s} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M3 8h10M3 4h10M3 12h6" /></svg>;
}
function GoalsIcon() {
  return <svg style={s} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><circle cx="8" cy="8" r="6" /><circle cx="8" cy="8" r="3" /><circle cx="8" cy="8" r="0.5" fill="currentColor" /></svg>;
}
function TreesIcon() {
  return <svg style={s} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M3 14h4M9 14h4M5 14V8M11 14V8M8 8V4M5 8L8 5l3 3" /></svg>;
}
function MemoryIcon() {
  return <svg style={s} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><circle cx="8" cy="8" r="6" /><path d="M8 4v4l3 2" /></svg>;
}
function BeliefsIcon() {
  return <svg style={s} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M4 8l3 3 5-5" /><circle cx="8" cy="8" r="6" /></svg>;
}
function SkillsIcon() {
  return <svg style={s} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><rect x="2" y="2" width="12" height="12" rx="2" /><path d="M6 6h4M6 8h4M6 10h2" /></svg>;
}
function WorkersIcon() {
  return <svg style={s} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><circle cx="8" cy="8" r="2" /><path d="M8 2v2M8 12v2M2 8h2M12 8h2M3.8 3.8l1.4 1.4M10.8 10.8l1.4 1.4M3.8 12.2l1.4-1.4M10.8 5.2l1.4-1.4" /></svg>;
}
