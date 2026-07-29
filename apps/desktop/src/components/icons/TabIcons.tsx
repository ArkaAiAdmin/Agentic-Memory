/**
 * TabIcons — Monochrome SVG icons for panel tabs.
 * Black/white, clean, professional — no colored emojis.
 */

import React from "react";

const iconStyle: React.CSSProperties = { width: 14, height: 14, flexShrink: 0 };

export function ChatIcon() {
  return (
    <svg style={iconStyle} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M2 3h12a1 1 0 0 1 1 1v7a1 1 0 0 1-1 1H5l-3 3V4a1 1 0 0 1 1-1z" />
    </svg>
  );
}

export function ComposerIcon() {
  return (
    <svg style={iconStyle} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M11 1.5H4a1 1 0 0 0-1 1v11a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1V5l-4-3.5z" />
      <path d="M10 1.5V5h3.5" />
      <path d="M6 8h4M6 10.5h2.5" />
    </svg>
  );
}

export function GitIcon() {
  return (
    <svg style={iconStyle} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="5" cy="4" r="1.5" />
      <circle cx="5" cy="12" r="1.5" />
      <circle cx="11" cy="8" r="1.5" />
      <path d="M5 5.5V10.5" />
      <path d="M6.5 4L9.5 8" />
    </svg>
  );
}

export function TasksIcon() {
  return (
    <svg style={iconStyle} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M3 8h10M3 4h10M3 12h6" />
    </svg>
  );
}

export function GoalsIcon() {
  return (
    <svg style={iconStyle} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="8" cy="8" r="6" />
      <circle cx="8" cy="8" r="3" />
      <circle cx="8" cy="8" r="0.5" fill="currentColor" />
    </svg>
  );
}

export function TreesIcon() {
  return (
    <svg style={iconStyle} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M8 1v14M4 5l4-4 4 4M4 11l4 4 4-4" />
    </svg>
  );
}

export function MemoryIcon() {
  return (
    <svg style={iconStyle} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="8" cy="8" r="6" />
      <path d="M8 4v4l3 2" />
    </svg>
  );
}

export function BeliefsIcon() {
  return (
    <svg style={iconStyle} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M8 2l2 4h4l-3.5 3 1.5 4.5L8 11l-4 3.5 1.5-4.5L2 6h4z" />
    </svg>
  );
}

export function SkillsIcon() {
  return (
    <svg style={iconStyle} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M8 1l2 5h5l-4 3.5 1.5 5L8 12l-4.5 3.5L5 10.5 1 6h5z" />
    </svg>
  );
}

export function WorkersIcon() {
  return (
    <svg style={iconStyle} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="8" cy="8" r="2" />
      <path d="M8 2v2M8 12v2M2 8h2M12 8h2M3.8 3.8l1.4 1.4M10.8 10.8l1.4 1.4M3.8 12.2l1.4-1.4M10.8 5.2l1.4-1.4" />
    </svg>
  );
}
