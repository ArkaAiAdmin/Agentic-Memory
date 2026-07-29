/**
 * EmptyState — Unified empty-state display for all panels.
 */
import React from "react";

interface EmptyStateProps {
  icon?: React.ReactNode;
  title: string;
  description?: string;
  action?: { label: string; onClick: () => void };
}

export function EmptyState({ icon, title, description, action }: EmptyStateProps) {
  return (
    <div style={{
      display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center",
      height: "100%", padding: 40, textAlign: "center",
    }}>
      {icon ? (
        <div style={{ fontSize: 32, marginBottom: 12, opacity: 0.5 }}>{icon}</div>
      ) : (
        <svg width="40" height="40" viewBox="0 0 48 48" fill="none" stroke="currentColor"
          strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"
          style={{ opacity: 0.3, marginBottom: 12, color: "var(--text-tertiary)" }}>
          <rect x="8" y="8" width="32" height="32" rx="4" />
          <path d="M16 16h16M16 24h10M16 32h12" />
        </svg>
      )}
      <div style={{ fontSize: 14, color: "var(--text-secondary)", fontWeight: 500, marginBottom: 4 }}>
        {title}
      </div>
      {description && (
        <div style={{ fontSize: 12, color: "var(--text-tertiary)", maxWidth: 280, lineHeight: 1.6 }}>
          {description}
        </div>
      )}
      {action && (
        <button
          onClick={action.onClick}
          style={{
            marginTop: 12, padding: "6px 16px", borderRadius: "var(--radius-md)",
            border: "1px solid var(--accent)", background: "transparent",
            color: "var(--accent)", fontSize: 12, fontWeight: 500, cursor: "pointer",
          }}
        >
          {action.label}
        </button>
      )}
    </div>
  );
}
