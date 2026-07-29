/**
 * Theme System — 2026 Premium Edition
 *
 * 5 handcrafted themes with glass-morphism support, gradient accents,
 * and vibrant palettes designed for modern desktop IDEs.
 */

export interface ThemePalette {
  // ── Base ──────────────────────────────────────────────
  name: string;
  id: string;
  appearance: "dark" | "light";

  // ── Backgrounds ───────────────────────────────────────
  bgPrimary: string;
  bgSecondary: string;
  bgTertiary: string;
  bgElevated: string;
  bgOverlay: string;
  bgHover: string;
  bgGlass: string;

  // ── Borders ───────────────────────────────────────────
  borderDefault: string;
  borderSubtle: string;
  borderActive: string;
  borderGlow: string;

  // ── Text ──────────────────────────────────────────────
  textPrimary: string;
  textSecondary: string;
  textTertiary: string;
  textInverse: string;

  // ── Accent ────────────────────────────────────────────
  accent: string;
  accentHover: string;
  accentMuted: string;
  accentText: string;
  accentGradient: string;
  accentGlow: string;

  // ── Semantic ──────────────────────────────────────────
  success: string;
  successMuted: string;
  warning: string;
  warningMuted: string;
  error: string;
  errorMuted: string;
  info: string;
  infoMuted: string;

  // ── Sheen/Glass ───────────────────────────────────────
  sheenSubtle: string;
  sheenBorder: string;

  // ── Titlebar ──────────────────────────────────────────
  titlebarBg: string;

  // ── Editor ────────────────────────────────────────────
  editorBg: string;
  editorLineHighlight: string;
  editorSelection: string;
  editorCursor: string;
  editorLineNumber: string;

  // ── Syntax ────────────────────────────────────────────
  syntaxKeyword: string;
  syntaxString: string;
  syntaxNumber: string;
  syntaxFunction: string;
  syntaxComment: string;
  syntaxType: string;
  syntaxVariable: string;
  syntaxOperator: string;

  // ── Terminal ──────────────────────────────────────────
  terminalBg: string;
  terminalFg: string;
  terminalBlack: string;
  terminalRed: string;
  terminalGreen: string;
  terminalYellow: string;
  terminalBlue: string;
  terminalMagenta: string;
  terminalCyan: string;
  terminalWhite: string;
}

// ── Obsidian ─────────────────────────────────────────────────────────────
// Deep charcoal with electric violet accents, glass depth
const OBSIDIAN: ThemePalette = {
  name: "Obsidian",
  id: "obsidian",
  appearance: "dark",

  bgPrimary: "#0a0b10",
  bgSecondary: "#12141d",
  bgTertiary: "#191c28",
  bgElevated: "#1f2233",
  bgOverlay: "rgba(10, 11, 16, 0.88)",
  bgHover: "rgba(255, 255, 255, 0.035)",
  bgGlass: "rgba(255, 255, 255, 0.025)",

  borderDefault: "rgba(255, 255, 255, 0.06)",
  borderSubtle: "rgba(255, 255, 255, 0.03)",
  borderActive: "#8b5cf6",
  borderGlow: "rgba(139, 92, 246, 0.4)",

  textPrimary: "#f1f3f8",
  textSecondary: "#8b93a8",
  textTertiary: "#4e5468",
  textInverse: "#0a0b10",

  accent: "#8b5cf6",
  accentHover: "#a78bfa",
  accentMuted: "rgba(139, 92, 246, 0.12)",
  accentText: "#ffffff",
  accentGradient: "linear-gradient(135deg, #8b5cf6, #6366f1)",
  accentGlow: "0 0 24px rgba(139, 92, 246, 0.25)",

  success: "#34d399",
  successMuted: "rgba(52, 211, 153, 0.1)",
  warning: "#fbbf24",
  warningMuted: "rgba(251, 191, 36, 0.1)",
  error: "#f87171",
  errorMuted: "rgba(248, 113, 113, 0.1)",
  info: "#60a5fa",
  infoMuted: "rgba(96, 165, 250, 0.1)",

  sheenSubtle: "linear-gradient(135deg, rgba(255,255,255,0.03) 0%, transparent 60%)",
  sheenBorder: "linear-gradient(135deg, rgba(255,255,255,0.08), rgba(255,255,255,0.02))",

  titlebarBg: "rgba(12, 13, 19, 0.92)",

  editorBg: "#0a0b10",
  editorLineHighlight: "#141720",
  editorSelection: "rgba(139, 92, 246, 0.18)",
  editorCursor: "#8b5cf6",
  editorLineNumber: "#3d4255",

  syntaxKeyword: "#c084fc",
  syntaxString: "#34d399",
  syntaxNumber: "#fbbf24",
  syntaxFunction: "#60a5fa",
  syntaxComment: "#4e5468",
  syntaxType: "#2dd4bf",
  syntaxVariable: "#f1f3f8",
  syntaxOperator: "#8b93a8",

  terminalBg: "#08090d",
  terminalFg: "#f1f3f8",
  terminalBlack: "#08090d",
  terminalRed: "#f87171",
  terminalGreen: "#34d399",
  terminalYellow: "#fbbf24",
  terminalBlue: "#60a5fa",
  terminalMagenta: "#a78bfa",
  terminalCyan: "#22d3ee",
  terminalWhite: "#f1f3f8",
};

// ── Aurora ───────────────────────────────────────────────────────────────
// Warm light theme with luminous blue-teal accents and paper-white depth
const AURORA: ThemePalette = {
  name: "Aurora",
  id: "aurora",
  appearance: "light",

  bgPrimary: "#fafbfd",
  bgSecondary: "#f3f5f9",
  bgTertiary: "#e8ecf4",
  bgElevated: "#ffffff",
  bgOverlay: "rgba(250, 251, 253, 0.92)",
  bgHover: "rgba(0, 0, 0, 0.025)",
  bgGlass: "rgba(255, 255, 255, 0.7)",

  borderDefault: "rgba(0, 0, 0, 0.08)",
  borderSubtle: "rgba(0, 0, 0, 0.04)",
  borderActive: "#2563eb",
  borderGlow: "rgba(37, 99, 235, 0.3)",

  textPrimary: "#111827",
  textSecondary: "#4b5563",
  textTertiary: "#9ca3af",
  textInverse: "#ffffff",

  accent: "#2563eb",
  accentHover: "#3b82f6",
  accentMuted: "rgba(37, 99, 235, 0.08)",
  accentText: "#ffffff",
  accentGradient: "linear-gradient(135deg, #2563eb, #0891b2)",
  accentGlow: "0 0 20px rgba(37, 99, 235, 0.15)",

  success: "#059669",
  successMuted: "rgba(5, 150, 105, 0.08)",
  warning: "#d97706",
  warningMuted: "rgba(217, 119, 6, 0.08)",
  error: "#dc2626",
  errorMuted: "rgba(220, 38, 38, 0.08)",
  info: "#2563eb",
  infoMuted: "rgba(37, 99, 235, 0.08)",

  sheenSubtle: "linear-gradient(135deg, rgba(255,255,255,0.8) 0%, transparent 60%)",
  sheenBorder: "linear-gradient(135deg, rgba(255,255,255,1), rgba(255,255,255,0.4))",

  titlebarBg: "rgba(243, 245, 249, 0.92)",

  editorBg: "#ffffff",
  editorLineHighlight: "#f8f9fc",
  editorSelection: "rgba(37, 99, 235, 0.1)",
  editorCursor: "#2563eb",
  editorLineNumber: "#9ca3af",

  syntaxKeyword: "#7c3aed",
  syntaxString: "#059669",
  syntaxNumber: "#d97706",
  syntaxFunction: "#2563eb",
  syntaxComment: "#9ca3af",
  syntaxType: "#0891b2",
  syntaxVariable: "#111827",
  syntaxOperator: "#4b5563",

  terminalBg: "#111827",
  terminalFg: "#e5e7eb",
  terminalBlack: "#111827",
  terminalRed: "#ef4444",
  terminalGreen: "#10b981",
  terminalYellow: "#fbbf24",
  terminalBlue: "#3b82f6",
  terminalMagenta: "#a855f7",
  terminalCyan: "#06b6d4",
  terminalWhite: "#f9fafb",
};

// ── Noir ─────────────────────────────────────────────────────────────────
// Pure black OLED with emerald green accent — minimal, high-contrast
const NOIR: ThemePalette = {
  name: "Noir",
  id: "noir",
  appearance: "dark",

  bgPrimary: "#000000",
  bgSecondary: "#0a0a0a",
  bgTertiary: "#141414",
  bgElevated: "#1a1a1a",
  bgOverlay: "rgba(0, 0, 0, 0.9)",
  bgHover: "rgba(255, 255, 255, 0.04)",
  bgGlass: "rgba(255, 255, 255, 0.02)",

  borderDefault: "rgba(255, 255, 255, 0.08)",
  borderSubtle: "rgba(255, 255, 255, 0.04)",
  borderActive: "#10b981",
  borderGlow: "rgba(16, 185, 129, 0.4)",

  textPrimary: "#e5e5e5",
  textSecondary: "#737373",
  textTertiary: "#404040",
  textInverse: "#000000",

  accent: "#10b981",
  accentHover: "#34d399",
  accentMuted: "rgba(16, 185, 129, 0.1)",
  accentText: "#000000",
  accentGradient: "linear-gradient(135deg, #10b981, #06b6d4)",
  accentGlow: "0 0 24px rgba(16, 185, 129, 0.2)",

  success: "#10b981",
  successMuted: "rgba(16, 185, 129, 0.1)",
  warning: "#eab308",
  warningMuted: "rgba(234, 179, 8, 0.1)",
  error: "#ef4444",
  errorMuted: "rgba(239, 68, 68, 0.1)",
  info: "#3b82f6",
  infoMuted: "rgba(59, 130, 246, 0.1)",

  sheenSubtle: "linear-gradient(135deg, rgba(255,255,255,0.02) 0%, transparent 50%)",
  sheenBorder: "linear-gradient(135deg, rgba(255,255,255,0.06), rgba(255,255,255,0.01))",

  titlebarBg: "rgba(0, 0, 0, 0.95)",

  editorBg: "#000000",
  editorLineHighlight: "#0d0d0d",
  editorSelection: "rgba(16, 185, 129, 0.15)",
  editorCursor: "#10b981",
  editorLineNumber: "#333333",

  syntaxKeyword: "#a78bfa",
  syntaxString: "#34d399",
  syntaxNumber: "#fbbf24",
  syntaxFunction: "#60a5fa",
  syntaxComment: "#404040",
  syntaxType: "#22d3ee",
  syntaxVariable: "#e5e5e5",
  syntaxOperator: "#737373",

  terminalBg: "#000000",
  terminalFg: "#e5e5e5",
  terminalBlack: "#000000",
  terminalRed: "#ef4444",
  terminalGreen: "#10b981",
  terminalYellow: "#eab308",
  terminalBlue: "#3b82f6",
  terminalMagenta: "#a855f7",
  terminalCyan: "#06b6d4",
  terminalWhite: "#e5e5e5",
};

// ── Nebula ───────────────────────────────────────────────────────────────
// Deep space purples with warm pink/amber — cinematic, rich
const NEBULA: ThemePalette = {
  name: "Nebula",
  id: "nebula",
  appearance: "dark",

  bgPrimary: "#0d0b14",
  bgSecondary: "#14111f",
  bgTertiary: "#1c1829",
  bgElevated: "#251f35",
  bgOverlay: "rgba(13, 11, 20, 0.9)",
  bgHover: "rgba(255, 255, 255, 0.035)",
  bgGlass: "rgba(255, 255, 255, 0.02)",

  borderDefault: "rgba(255, 255, 255, 0.06)",
  borderSubtle: "rgba(255, 255, 255, 0.03)",
  borderActive: "#e879f9",
  borderGlow: "rgba(232, 121, 249, 0.35)",

  textPrimary: "#f0e6ff",
  textSecondary: "#9b8ab8",
  textTertiary: "#5c4d6e",
  textInverse: "#0d0b14",

  accent: "#e879f9",
  accentHover: "#f0abfc",
  accentMuted: "rgba(232, 121, 249, 0.12)",
  accentText: "#0d0b14",
  accentGradient: "linear-gradient(135deg, #e879f9, #f97316)",
  accentGlow: "0 0 28px rgba(232, 121, 249, 0.25)",

  success: "#4ade80",
  successMuted: "rgba(74, 222, 128, 0.1)",
  warning: "#fb923c",
  warningMuted: "rgba(251, 146, 60, 0.1)",
  error: "#fb7185",
  errorMuted: "rgba(251, 113, 133, 0.1)",
  info: "#818cf8",
  infoMuted: "rgba(129, 140, 248, 0.1)",

  sheenSubtle: "linear-gradient(135deg, rgba(232,121,249,0.03) 0%, transparent 60%)",
  sheenBorder: "linear-gradient(135deg, rgba(255,255,255,0.06), rgba(232,121,249,0.03))",

  titlebarBg: "rgba(13, 11, 20, 0.94)",

  editorBg: "#0d0b14",
  editorLineHighlight: "#16121f",
  editorSelection: "rgba(232, 121, 249, 0.15)",
  editorCursor: "#e879f9",
  editorLineNumber: "#3d3550",

  syntaxKeyword: "#e879f9",
  syntaxString: "#4ade80",
  syntaxNumber: "#fb923c",
  syntaxFunction: "#818cf8",
  syntaxComment: "#5c4d6e",
  syntaxType: "#22d3ee",
  syntaxVariable: "#f0e6ff",
  syntaxOperator: "#9b8ab8",

  terminalBg: "#09070f",
  terminalFg: "#f0e6ff",
  terminalBlack: "#09070f",
  terminalRed: "#fb7185",
  terminalGreen: "#4ade80",
  terminalYellow: "#fb923c",
  terminalBlue: "#818cf8",
  terminalMagenta: "#e879f9",
  terminalCyan: "#22d3ee",
  terminalWhite: "#f0e6ff",
};

// ── Frost ────────────────────────────────────────────────────────────────
// Cool blue-gray light theme with icy clarity and crisp shadows
const FROST: ThemePalette = {
  name: "Frost",
  id: "frost",
  appearance: "light",

  bgPrimary: "#f8fafc",
  bgSecondary: "#f1f5f9",
  bgTertiary: "#e2e8f0",
  bgElevated: "#ffffff",
  bgOverlay: "rgba(248, 250, 252, 0.92)",
  bgHover: "rgba(0, 0, 0, 0.02)",
  bgGlass: "rgba(255, 255, 255, 0.75)",

  borderDefault: "rgba(0, 0, 0, 0.06)",
  borderSubtle: "rgba(0, 0, 0, 0.03)",
  borderActive: "#0ea5e9",
  borderGlow: "rgba(14, 165, 233, 0.3)",

  textPrimary: "#0f172a",
  textSecondary: "#475569",
  textTertiary: "#94a3b8",
  textInverse: "#ffffff",

  accent: "#0ea5e9",
  accentHover: "#38bdf8",
  accentMuted: "rgba(14, 165, 233, 0.08)",
  accentText: "#ffffff",
  accentGradient: "linear-gradient(135deg, #0ea5e9, #6366f1)",
  accentGlow: "0 0 20px rgba(14, 165, 233, 0.15)",

  success: "#059669",
  successMuted: "rgba(5, 150, 105, 0.08)",
  warning: "#d97706",
  warningMuted: "rgba(217, 119, 6, 0.08)",
  error: "#dc2626",
  errorMuted: "rgba(220, 38, 38, 0.08)",
  info: "#0ea5e9",
  infoMuted: "rgba(14, 165, 233, 0.08)",

  sheenSubtle: "linear-gradient(135deg, rgba(255,255,255,0.9) 0%, transparent 60%)",
  sheenBorder: "linear-gradient(135deg, rgba(255,255,255,1), rgba(255,255,255,0.5))",

  titlebarBg: "rgba(241, 245, 249, 0.92)",

  editorBg: "#ffffff",
  editorLineHighlight: "#f8fafc",
  editorSelection: "rgba(14, 165, 233, 0.1)",
  editorCursor: "#0ea5e9",
  editorLineNumber: "#94a3b8",

  syntaxKeyword: "#7c3aed",
  syntaxString: "#059669",
  syntaxNumber: "#d97706",
  syntaxFunction: "#0ea5e9",
  syntaxComment: "#94a3b8",
  syntaxType: "#0891b2",
  syntaxVariable: "#0f172a",
  syntaxOperator: "#475569",

  terminalBg: "#0f172a",
  terminalFg: "#e2e8f0",
  terminalBlack: "#0f172a",
  terminalRed: "#ef4444",
  terminalGreen: "#10b981",
  terminalYellow: "#fbbf24",
  terminalBlue: "#38bdf8",
  terminalMagenta: "#a855f7",
  terminalCyan: "#22d3ee",
  terminalWhite: "#f8fafc",
};

// ── Ember ─────────────────────────────────────────────────────────────────
// Deep warm charcoal with molten amber/orange — cozy, cinematic
const EMBER: ThemePalette = {
  name: "Ember",
  id: "ember",
  appearance: "dark",

  bgPrimary: "#0d0906",
  bgSecondary: "#151009",
  bgTertiary: "#1e1610",
  bgElevated: "#271d14",
  bgOverlay: "rgba(13, 9, 6, 0.9)",
  bgHover: "rgba(255, 180, 80, 0.04)",
  bgGlass: "rgba(255, 160, 60, 0.02)",

  borderDefault: "rgba(255, 180, 100, 0.08)",
  borderSubtle: "rgba(255, 160, 80, 0.04)",
  borderActive: "#f59e0b",
  borderGlow: "rgba(245, 158, 11, 0.35)",

  textPrimary: "#faf0e4",
  textSecondary: "#b8977a",
  textTertiary: "#6b5340",
  textInverse: "#0d0906",

  accent: "#f59e0b",
  accentHover: "#fbbf24",
  accentMuted: "rgba(245, 158, 11, 0.12)",
  accentText: "#0d0906",
  accentGradient: "linear-gradient(135deg, #f59e0b, #ea580c)",
  accentGlow: "0 0 24px rgba(245, 158, 11, 0.22)",

  success: "#4ade80",
  successMuted: "rgba(74, 222, 128, 0.1)",
  warning: "#fbbf24",
  warningMuted: "rgba(251, 191, 36, 0.1)",
  error: "#f87171",
  errorMuted: "rgba(248, 113, 113, 0.1)",
  info: "#60a5fa",
  infoMuted: "rgba(96, 165, 250, 0.1)",

  sheenSubtle: "linear-gradient(135deg, rgba(255,180,80,0.03) 0%, transparent 55%)",
  sheenBorder: "linear-gradient(135deg, rgba(255,180,80,0.08), rgba(255,140,50,0.02))",

  titlebarBg: "rgba(13, 9, 6, 0.94)",

  editorBg: "#0d0906",
  editorLineHighlight: "#17100a",
  editorSelection: "rgba(245, 158, 11, 0.15)",
  editorCursor: "#f59e0b",
  editorLineNumber: "#4a3828",

  syntaxKeyword: "#fbbf24",
  syntaxString: "#4ade80",
  syntaxNumber: "#f97316",
  syntaxFunction: "#60a5fa",
  syntaxComment: "#6b5340",
  syntaxType: "#2dd4bf",
  syntaxVariable: "#faf0e4",
  syntaxOperator: "#b8977a",

  terminalBg: "#0a0704",
  terminalFg: "#faf0e4",
  terminalBlack: "#0a0704",
  terminalRed: "#f87171",
  terminalGreen: "#4ade80",
  terminalYellow: "#fbbf24",
  terminalBlue: "#60a5fa",
  terminalMagenta: "#f97316",
  terminalCyan: "#2dd4bf",
  terminalWhite: "#faf0e4",
};

// ── Solstice ──────────────────────────────────────────────────────────────
// Warm paper-white with burnt terracotta and amber — afternoon sun
const SOLSTICE: ThemePalette = {
  name: "Solstice",
  id: "solstice",
  appearance: "light",

  bgPrimary: "#fdf9f5",
  bgSecondary: "#f7f0e8",
  bgTertiary: "#ede3d6",
  bgElevated: "#ffffff",
  bgOverlay: "rgba(253, 249, 245, 0.92)",
  bgHover: "rgba(180, 80, 20, 0.03)",
  bgGlass: "rgba(255, 255, 255, 0.72)",

  borderDefault: "rgba(120, 60, 20, 0.1)",
  borderSubtle: "rgba(120, 60, 20, 0.05)",
  borderActive: "#ea580c",
  borderGlow: "rgba(234, 88, 12, 0.25)",

  textPrimary: "#1c1108",
  textSecondary: "#6b4f38",
  textTertiary: "#a3886e",
  textInverse: "#ffffff",

  accent: "#ea580c",
  accentHover: "#f97316",
  accentMuted: "rgba(234, 88, 12, 0.08)",
  accentText: "#ffffff",
  accentGradient: "linear-gradient(135deg, #ea580c, #dc2626)",
  accentGlow: "0 0 20px rgba(234, 88, 12, 0.14)",

  success: "#059669",
  successMuted: "rgba(5, 150, 105, 0.08)",
  warning: "#d97706",
  warningMuted: "rgba(217, 119, 6, 0.08)",
  error: "#dc2626",
  errorMuted: "rgba(220, 38, 38, 0.08)",
  info: "#2563eb",
  infoMuted: "rgba(37, 99, 235, 0.08)",

  sheenSubtle: "linear-gradient(135deg, rgba(255,255,255,0.85) 0%, transparent 55%)",
  sheenBorder: "linear-gradient(135deg, rgba(255,255,255,1), rgba(255,220,180,0.4))",

  titlebarBg: "rgba(247, 240, 232, 0.92)",

  editorBg: "#ffffff",
  editorLineHighlight: "#fdf8f3",
  editorSelection: "rgba(234, 88, 12, 0.1)",
  editorCursor: "#ea580c",
  editorLineNumber: "#a3886e",

  syntaxKeyword: "#b91c1c",
  syntaxString: "#059669",
  syntaxNumber: "#d97706",
  syntaxFunction: "#ea580c",
  syntaxComment: "#a3886e",
  syntaxType: "#0891b2",
  syntaxVariable: "#1c1108",
  syntaxOperator: "#6b4f38",

  terminalBg: "#1c1108",
  terminalFg: "#faf0e4",
  terminalBlack: "#1c1108",
  terminalRed: "#ef4444",
  terminalGreen: "#10b981",
  terminalYellow: "#fbbf24",
  terminalBlue: "#3b82f6",
  terminalMagenta: "#f97316",
  terminalCyan: "#06b6d4",
  terminalWhite: "#fdf9f5",
};

// ── Crimson ───────────────────────────────────────────────────────────────
// Inky black with deep rose/red — cinematic, dramatic, brooding
const CRIMSON: ThemePalette = {
  name: "Crimson",
  id: "crimson",
  appearance: "dark",

  bgPrimary: "#0a0507",
  bgSecondary: "#12090d",
  bgTertiary: "#1c0f14",
  bgElevated: "#26141b",
  bgOverlay: "rgba(10, 5, 7, 0.9)",
  bgHover: "rgba(255, 80, 100, 0.04)",
  bgGlass: "rgba(255, 60, 80, 0.02)",

  borderDefault: "rgba(255, 100, 120, 0.08)",
  borderSubtle: "rgba(255, 80, 100, 0.04)",
  borderActive: "#e11d48",
  borderGlow: "rgba(225, 29, 72, 0.35)",

  textPrimary: "#fae8ec",
  textSecondary: "#b07a86",
  textTertiary: "#6b4450",
  textInverse: "#0a0507",

  accent: "#e11d48",
  accentHover: "#fb7185",
  accentMuted: "rgba(225, 29, 72, 0.12)",
  accentText: "#ffffff",
  accentGradient: "linear-gradient(135deg, #e11d48, #9f1239)",
  accentGlow: "0 0 24px rgba(225, 29, 72, 0.22)",

  success: "#34d399",
  successMuted: "rgba(52, 211, 153, 0.1)",
  warning: "#fbbf24",
  warningMuted: "rgba(251, 191, 36, 0.1)",
  error: "#f87171",
  errorMuted: "rgba(248, 113, 113, 0.12)",
  info: "#60a5fa",
  infoMuted: "rgba(96, 165, 250, 0.1)",

  sheenSubtle: "linear-gradient(135deg, rgba(255,80,100,0.03) 0%, transparent 55%)",
  sheenBorder: "linear-gradient(135deg, rgba(255,100,120,0.06), rgba(255,60,80,0.02))",

  titlebarBg: "rgba(10, 5, 7, 0.94)",

  editorBg: "#0a0507",
  editorLineHighlight: "#14080c",
  editorSelection: "rgba(225, 29, 72, 0.15)",
  editorCursor: "#e11d48",
  editorLineNumber: "#4a2a33",

  syntaxKeyword: "#fb7185",
  syntaxString: "#34d399",
  syntaxNumber: "#fbbf24",
  syntaxFunction: "#60a5fa",
  syntaxComment: "#6b4450",
  syntaxType: "#2dd4bf",
  syntaxVariable: "#fae8ec",
  syntaxOperator: "#b07a86",

  terminalBg: "#070304",
  terminalFg: "#fae8ec",
  terminalBlack: "#070304",
  terminalRed: "#fb7185",
  terminalGreen: "#34d399",
  terminalYellow: "#fbbf24",
  terminalBlue: "#60a5fa",
  terminalMagenta: "#e11d48",
  terminalCyan: "#22d3ee",
  terminalWhite: "#fae8ec",
};

// ── Blush ─────────────────────────────────────────────────────────────────
// Soft warm white with rose/coral — gentle, refined, airy
const BLUSH: ThemePalette = {
  name: "Blush",
  id: "blush",
  appearance: "light",

  bgPrimary: "#fdf7f8",
  bgSecondary: "#f9f0f2",
  bgTertiary: "#f0e3e6",
  bgElevated: "#ffffff",
  bgOverlay: "rgba(253, 247, 248, 0.92)",
  bgHover: "rgba(200, 40, 60, 0.03)",
  bgGlass: "rgba(255, 255, 255, 0.72)",

  borderDefault: "rgba(160, 50, 70, 0.09)",
  borderSubtle: "rgba(160, 50, 70, 0.04)",
  borderActive: "#e11d48",
  borderGlow: "rgba(225, 29, 72, 0.22)",

  textPrimary: "#1a0810",
  textSecondary: "#6b3a4a",
  textTertiary: "#a37080",
  textInverse: "#ffffff",

  accent: "#e11d48",
  accentHover: "#f43f5e",
  accentMuted: "rgba(225, 29, 72, 0.07)",
  accentText: "#ffffff",
  accentGradient: "linear-gradient(135deg, #e11d48, #f97316)",
  accentGlow: "0 0 20px rgba(225, 29, 72, 0.12)",

  success: "#059669",
  successMuted: "rgba(5, 150, 105, 0.08)",
  warning: "#d97706",
  warningMuted: "rgba(217, 119, 6, 0.08)",
  error: "#dc2626",
  errorMuted: "rgba(220, 38, 38, 0.08)",
  info: "#2563eb",
  infoMuted: "rgba(37, 99, 235, 0.08)",

  sheenSubtle: "linear-gradient(135deg, rgba(255,255,255,0.85) 0%, transparent 55%)",
  sheenBorder: "linear-gradient(135deg, rgba(255,255,255,1), rgba(255,210,220,0.4))",

  titlebarBg: "rgba(249, 240, 242, 0.92)",

  editorBg: "#ffffff",
  editorLineHighlight: "#fdf5f7",
  editorSelection: "rgba(225, 29, 72, 0.08)",
  editorCursor: "#e11d48",
  editorLineNumber: "#a37080",

  syntaxKeyword: "#be185d",
  syntaxString: "#059669",
  syntaxNumber: "#d97706",
  syntaxFunction: "#e11d48",
  syntaxComment: "#a37080",
  syntaxType: "#0891b2",
  syntaxVariable: "#1a0810",
  syntaxOperator: "#6b3a4a",

  terminalBg: "#1a0810",
  terminalFg: "#fae8ec",
  terminalBlack: "#1a0810",
  terminalRed: "#ef4444",
  terminalGreen: "#10b981",
  terminalYellow: "#fbbf24",
  terminalBlue: "#3b82f6",
  terminalMagenta: "#f43f5e",
  terminalCyan: "#06b6d4",
  terminalWhite: "#fdf7f8",
};

// ── Export ────────────────────────────────────────────────────────────────

export const THEMES: ThemePalette[] = [
  OBSIDIAN,
  AURORA,
  NOIR,
  NEBULA,
  FROST,
  EMBER,
  SOLSTICE,
  CRIMSON,
  BLUSH,
];

export function getThemeById(id: string): ThemePalette {
  return THEMES.find((t) => t.id === id) ?? OBSIDIAN;
}

const THEME_TRANSITION_MS = 400;
let themeTransitionTimeout: ReturnType<typeof setTimeout> | null = null;

/**
 * Apply a theme to the document by setting CSS custom properties.
 * Uses smooth transitions between themes for a dynamic feel.
 */
export function applyTheme(theme: ThemePalette): void {
  const root = document.documentElement;

  // Enable transition animation
  root.classList.add("theme-transitioning");

  const entries = Object.entries(theme) as [string, string][];
  for (const [key, value] of entries) {
    if (typeof value === "string" && key !== "name" && key !== "id" && key !== "appearance") {
      // Convert camelCase to kebab-case CSS variable
      const cssVar = key.replace(/([A-Z])/g, "-$1").toLowerCase();
      root.style.setProperty(`--${cssVar}`, value);
    }
  }

  // Set appearance class for light/dark specific overrides
  root.setAttribute("data-theme", theme.id);
  root.setAttribute("data-appearance", theme.appearance);

  // Remove transition class after animation completes
  if (themeTransitionTimeout) {
    clearTimeout(themeTransitionTimeout);
  }
  themeTransitionTimeout = setTimeout(() => {
    root.classList.remove("theme-transitioning");
    themeTransitionTimeout = null;
  }, THEME_TRANSITION_MS);
}
