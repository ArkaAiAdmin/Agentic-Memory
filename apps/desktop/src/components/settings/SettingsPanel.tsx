import React, { useEffect, useRef, useState } from "react";
import { useAppStore } from "../../stores/appStore";
import { agentService } from "../../services/agentService";
import { getApiKey, setApiKey, deleteApiKey, hasApiKey } from "../../services/secretStore";
import { setCompletionConfig } from "../../services/completion";
import { THEMES, applyTheme, getThemeById } from "../../services/themes";
import { PROVIDER_DEFAULTS, type ProviderConfig, getFetchImpl } from "@ami/llm";
import { connectServer, disconnectServer, getAllStatuses, onMcpStatusChange } from "../../services/mcpClient";
import { Badge } from "../ui";
import { AgentIdentityPanel } from "./AgentIdentityPanel";

const PROVIDER_LABELS: Record<ProviderConfig["type"], string> = {
  openai: "OpenAI", anthropic: "Anthropic", google: "Google Gemini",
  lmstudio: "LM Studio (local)", ollama: "Ollama (local)", litellm: "LiteLLM Proxy",
};
const PROVIDER_TYPES = Object.keys(PROVIDER_LABELS) as ProviderConfig["type"][];
const LOCAL_PROVIDERS = new Set<ProviderConfig["type"]>(["lmstudio", "ollama"]);

export function SettingsPanel({ onClose }: { onClose: () => void }) {
  const { providerConfig, setProviderConfig, theme, setTheme, autocompleteEnabled, autocompleteModel, setAutocompleteEnabled, setAutocompleteModel, toolApprovalEnabled, setToolApprovalEnabled, mcpServers, addMcpServer, removeMcpServer, toggleMcpServer, setHasCompletedOnboarding } = useAppStore();

  const [type, setType] = useState<ProviderConfig["type"]>(providerConfig.type);
  const [model, setModel] = useState(providerConfig.model || PROVIDER_DEFAULTS[providerConfig.type].defaultModel);
  const [baseUrl, setBaseUrl] = useState(providerConfig.baseUrl ?? "");
  const [apiKey, setApiKeyInput] = useState("");
  const [keyStored, setKeyStored] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [mcpStatuses, setMcpStatuses] = useState<Array<{ id: string; connected: boolean; error?: string; tools: number }>>([]);

  useEffect(() => {
    // Sync external MCP status into React state — standard pub/sub pattern
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setMcpStatuses(getAllStatuses().map((s) => ({ id: s.id, connected: s.connected, error: s.error, tools: s.tools.length })));
    const unsub = onMcpStatusChange((statuses) => {
      setMcpStatuses(statuses.map((s) => ({ id: s.id, connected: s.connected, error: s.error, tools: s.tools.length })));
    });
    return unsub;
  }, []);

  useEffect(() => {
    let cancelled = false;
    // Sync API key input state when provider type changes
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setApiKeyInput("");
    hasApiKey(type).then((e) => { if (!cancelled) setKeyStored(e); }).catch(() => { if (!cancelled) setKeyStored(false); });
    return () => { cancelled = true; };
  }, [type]);

  const handleTypeChange = (next: ProviderConfig["type"]) => {
    setType(next);
    setModel(PROVIDER_DEFAULTS[next].defaultModel);
    setBaseUrl("");
    setError(null);
  };

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  const handleSave = async () => {
    setSaving(true); setError(null);
    try {
      const trimmedModel = model.trim() || PROVIDER_DEFAULTS[type].defaultModel;
      const next: ProviderConfig = { type, model: trimmedModel, ...(baseUrl.trim() ? { baseUrl: baseUrl.trim() } : {}) };
      setProviderConfig(next);
      if (apiKey.trim()) await setApiKey(type, apiKey.trim());
      const effectiveKey = apiKey.trim() || (await getApiKey(type)) || undefined;
      await agentService.setProvider({ ...next, apiKey: effectiveKey });
      setCompletionConfig({ enabled: autocompleteEnabled, model: autocompleteModel || "gpt-4o-mini" });
      onClose();
    } catch (err) { setError(err instanceof Error ? err.message : "Failed to save"); }
    finally { setSaving(false); }
  };

  const needsKey = !LOCAL_PROVIDERS.has(type);

  const handleBackdropClick = (e: React.MouseEvent) => {
    if (e.target === e.currentTarget) onClose();
  };

  return (
    <div style={{
      position: "fixed", inset: 0, zIndex: 1000,
      background: "rgba(0,0,0,0.6)", display: "flex", alignItems: "center", justifyContent: "center",
      backdropFilter: "blur(4px)", animation: "fadeIn 0.15s ease",
    }} role="dialog" aria-modal="true" aria-label="Settings" onClick={handleBackdropClick}>
      <div style={{
        background: "var(--bg-elevated)", borderRadius: "var(--radius-xl)", padding: 28,
        width: 540, maxWidth: "92vw", maxHeight: "85vh", overflow: "auto",
        boxShadow: "var(--shadow-lg)", border: "1px solid var(--border-default)",
        animation: "scaleIn 0.2s ease",
      }} onClick={(e) => e.stopPropagation()}>
        <h2 style={{ margin: "0 0 24px", fontSize: 18, fontWeight: 600, color: "var(--text-primary)", letterSpacing: -0.3 }}>Settings</h2>

        {/* Theme */}
        <Section title="Theme">
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            {THEMES.map((t) => (
              <button key={t.id} onClick={() => { setTheme(t.id); applyTheme(t); }} style={{
                padding: "10px 14px", borderRadius: "var(--radius-md)",
                border: `2px solid ${theme === t.id ? t.accent : "var(--border-default)"}`,
                background: t.bgSecondary, cursor: "pointer", display: "flex", alignItems: "center", gap: 8,
                fontSize: 12, color: t.textPrimary, fontWeight: 500, transition: "all 0.15s",
              }}>
                <div style={{ display: "flex", gap: 3 }}>
                  {[t.accent, t.success, t.error].map((c, i) => (
                    <div key={i} style={{ width: 10, height: 10, borderRadius: 3, background: c }} />
                  ))}
                </div>
                {t.name}
              </button>
            ))}
          </div>
          <AccentColorPicker />
        </Section>

        {/* Agent Identities */}
        <Section title="Agent Identities">
          <AgentIdentityPanel />
        </Section>

        {/* Provider */}
        <Section title="Provider">
          <Label>Provider</Label>
          <select value={type} onChange={(e) => handleTypeChange(e.target.value as ProviderConfig["type"])} style={inputStyle} aria-label="Provider">
            {PROVIDER_TYPES.map((t) => <option key={t} value={t}>{PROVIDER_LABELS[t]}</option>)}
          </select>
        </Section>

        <Section title="Model">
          <Label>Model</Label>
          <div style={{ display: "flex", gap: 8 }}>
            <input type="text" value={model} onChange={(e) => setModel(e.target.value)} placeholder={PROVIDER_DEFAULTS[type].defaultModel} style={{ ...inputStyle, flex: 1 }} aria-label="Model" />
            {LOCAL_PROVIDERS.has(type) && (
              <button onClick={async () => {
                try {
                  const bUrl = baseUrl.trim() || PROVIDER_DEFAULTS[type].baseUrl;
                  const fetchFn = getFetchImpl();
                  const res = await fetchFn(`${bUrl}/models`, { method: "GET", headers: {} });
                  if (res.ok) {
                    const data = await res.json();
                    const models = data.data?.map((m: any) => m.id) ?? [];
                    if (models.length > 0) setModel(models[0]);
                    else setError("No models loaded. Load a model in LM Studio first.");
                  } else { setError(`Failed to fetch models: ${res.status}`); }
                } catch (err) { setError(`Cannot reach ${type}. ${err instanceof Error ? err.message : "Is it running?"}\nTip: Enable CORS in LM Studio settings, or run via \`cargo tauri dev\`.`); }
              }} style={clearBtn}>Fetch Models</button>
            )}
          </div>
        </Section>

        <Section title="Base URL">
          <Label>Base URL <span style={{ opacity: 0.5 }}>(optional)</span></Label>
          <input type="text" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder={PROVIDER_DEFAULTS[type].baseUrl} style={inputStyle} aria-label="Base URL" />
        </Section>

        {needsKey && (
          <Section title="API Key">
            <Label>API Key {keyStored && <span style={{ color: "var(--success)" }}>• stored in keychain</span>}</Label>
            <div style={{ display: "flex", gap: 8 }}>
              <input type="password" value={apiKey} onChange={(e) => setApiKeyInput(e.target.value)} onKeyDown={(e) => e.key === "Enter" && handleSave()}
                placeholder={keyStored ? "•••••••• (leave blank to keep)" : "Enter API key"} style={{ ...inputStyle, flex: 1 }} autoComplete="off" aria-label="API Key" />
              {keyStored && <button onClick={async () => { await deleteApiKey(type); setKeyStored(false); setApiKeyInput(""); }} style={clearBtn}>Clear</button>}
            </div>
            <div style={{ fontSize: 11, color: "var(--text-tertiary)", marginTop: 4 }}>Stored in OS keychain, never in browser storage.</div>
          </Section>
        )}

        {/* Test Connection (local providers) */}
        {LOCAL_PROVIDERS.has(type) && (
          <Section title="Connection Test">
            <TestConnectionButton type={type} baseUrl={baseUrl.trim() || PROVIDER_DEFAULTS[type].baseUrl} />
          </Section>
        )}

        {error && (
          <div style={{ color: "var(--error)", fontSize: 12, marginBottom: 12, padding: "8px 12px", background: "var(--error-muted)", borderRadius: "var(--radius-sm)" }}>{error}</div>
        )}

        {/* Autocomplete */}
        <Section title="Autocomplete">
          <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer", fontSize: 12, color: "var(--text-secondary)" }}>
            <input type="checkbox" checked={autocompleteEnabled} onChange={(e) => setAutocompleteEnabled(e.target.checked)} style={{ width: 16, height: 16 }} />
            Enable inline completions (ghost text)
          </label>
          {autocompleteEnabled && (
            <div style={{ marginTop: 8 }}>
              <Label>Completion Model</Label>
              <input type="text" value={autocompleteModel} onChange={(e) => setAutocompleteModel(e.target.value)} placeholder="gpt-4o-mini" style={inputStyle} aria-label="Completion Model" />
              <div style={{ fontSize: 11, color: "var(--text-tertiary)", marginTop: 4 }}>Fast, cheap model. Local models tried first.</div>
            </div>
          )}
        </Section>

        {/* Tool Approval */}
        <Section title="Tool Approval">
          <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer", fontSize: 12, color: "var(--text-secondary)" }}>
            <input type="checkbox" checked={toolApprovalEnabled} onChange={(e) => setToolApprovalEnabled(e.target.checked)} style={{ width: 16, height: 16 }} />
            Require approval for mutating tools
          </label>
          <div style={{ fontSize: 11, color: "var(--text-tertiary)", marginTop: 4 }}>Agent asks before writing files or running commands.</div>
        </Section>

        {/* MCP Servers */}
        <Section title="MCP Servers">
          <div style={{ fontSize: 11, color: "var(--text-tertiary)", marginBottom: 8 }}>External tools for the agent.</div>
          {mcpServers.map((s) => {
            const status = mcpStatuses.find((st) => st.id === s.id);
            return (
              <div key={s.id} style={{ display: "flex", alignItems: "center", gap: 8, padding: "4px 0", fontSize: 12 }}>
                <input type="checkbox" checked={s.enabled} onChange={() => toggleMcpServer(s.id)} style={{ width: 14, height: 14 }} />
                <span style={{ color: "var(--text-primary)", flex: 1 }}>{s.name}</span>
                <span style={{ fontSize: 10, color: "var(--text-tertiary)" }}>{s.transport === "stdio" ? (s.command || "n/a") : (s.url || "n/a")}</span>
                <span style={{ fontSize: 10, color: "var(--text-tertiary)" }}>{s.transport}</span>
                {status && (
                  <Badge variant={status.connected ? "success" : status.error ? "error" : "neutral"}>
                    {status.connected ? `connected (${status.tools})` : status.error || "disconnected"}
                  </Badge>
                )}
                {!status?.connected ? (
                  <button onClick={() => connectServer(s.id)} style={{ padding: "2px 8px", borderRadius: "var(--radius-xs)", border: "1px solid var(--border-default)", background: "transparent", color: "var(--accent)", fontSize: 10, cursor: "pointer" }}>Connect</button>
                ) : (
                  <button onClick={() => disconnectServer(s.id)} style={{ padding: "2px 8px", borderRadius: "var(--radius-xs)", border: "1px solid var(--border-default)", background: "transparent", color: "var(--error)", fontSize: 10, cursor: "pointer" }}>Disconnect</button>
                )}
                <button onClick={() => { disconnectServer(s.id).then(() => removeMcpServer(s.id)); }} style={{ padding: "2px 6px", borderRadius: "var(--radius-xs)", border: "none", background: "transparent", color: "var(--error)", fontSize: 10, cursor: "pointer" }}>x</button>
              </div>
            );
          })}
          <AddMcpServerForm onAdd={(server) => addMcpServer({ ...server, id: `mcp-${Date.now()}` })} />
        </Section>

        {/* Reset Onboarding */}
        <div style={{ borderTop: "1px solid var(--border-default)", paddingTop: 16, marginTop: 16 }}>
          <button onClick={() => { setHasCompletedOnboarding(false); onClose(); }} style={{ ...addBtn, color: "var(--error)", border: "1px solid var(--error)" }}>
            Reset Onboarding Wizard
          </button>
          <div style={{ fontSize: 11, color: "var(--text-tertiary)", marginTop: 4 }}>Re-run first-time setup.</div>
        </div>

        {/* Save/Cancel */}
        <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 24 }}>
          <button onClick={onClose} style={cancelBtn}>Cancel</button>
          <button onClick={handleSave} disabled={saving} style={{ ...saveBtn, opacity: saving ? 0.7 : 1 }}>
            {saving ? "Saving..." : "Save"}
          </button>
        </div>
      </div>
    </div>
  );
}

function TestConnectionButton({ type, baseUrl }: { type: string; baseUrl: string }) {
  const [status, setStatus] = useState<"idle" | "testing" | "ok" | "fail">("idle");
  const [info, setInfo] = useState("");

  const test = async () => {
    setStatus("testing");
    setInfo("");
    try {
      const fetchFn = getFetchImpl();
      const res = await fetchFn(`${baseUrl}/models`, { method: "GET", headers: {} });
      if (res.ok) {
        const data = await res.json();
        const models = data.data?.map((m: any) => m.id) ?? [];
        setStatus("ok");
        setInfo(models.length > 0 ? `Connected. Models: ${models.join(", ")}` : "Connected but no models loaded.");
      } else {
        setStatus("fail");
        setInfo(`HTTP ${res.status}: ${res.statusText}`);
      }
    } catch (err) {
      console.error("Connection test failed:", err);
      setStatus("fail");
      setInfo(`Cannot connect to ${baseUrl}. Is ${type} running?\nIf CORS error: enable CORS in LM Studio settings or run via \`cargo tauri dev\`.`);
    }
  };

  return (
    <div>
      <button onClick={test} disabled={status === "testing"} style={addBtn}>
        {status === "testing" ? "Testing..." : "Test Connection"}
      </button>
      {status === "ok" && (
        <div style={{ marginTop: 6, fontSize: 11, color: "var(--success)", display: "flex", alignItems: "center", gap: 6 }}>
          <Badge variant="success" dot>Connected</Badge>
          <span>{info}</span>
        </div>
      )}
      {status === "fail" && (
        <div style={{ marginTop: 6, fontSize: 11, color: "var(--error)" }}>
          <Badge variant="error" dot>Failed</Badge> {info}
        </div>
      )}
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 20 }}>
      <h3 style={{ margin: "0 0 10px", fontSize: 13, fontWeight: 600, color: "var(--text-primary)" }}>{title}</h3>
      {children}
    </div>
  );
}

function Label({ children }: { children: React.ReactNode }) {
  return <label style={{ display: "block", fontSize: 12, color: "var(--text-secondary)", marginBottom: 6, fontWeight: 500 }}>{children}</label>;
}

const ACCENT_PRESETS = [
  { name: "Violet", hex: "#8b5cf6" },
  { name: "Blue", hex: "#2563eb" },
  { name: "Emerald", hex: "#10b981" },
  { name: "Pink", hex: "#e879f9" },
  { name: "Cyan", hex: "#0ea5e9" },
  { name: "Amber", hex: "#f59e0b" },
  { name: "Orange", hex: "#ea580c" },
  { name: "Rose", hex: "#e11d48" },
];

/* ---- Color picker helpers ---- */
function _hexToRgb(hex: string): [number, number, number] {
  const h = hex.replace("#", "");
  return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
}
function _rgbToHex(r: number, g: number, b: number): string {
  return "#" + [r, g, b].map((c) => Math.round(c).toString(16).padStart(2, "0")).join("");
}
function _hsvToRgb(h: number, s: number, v: number): [number, number, number] {
  const i = Math.floor(h * 6), f = h * 6 - i, p = v * (1 - s), q = v * (1 - f * s), t = v * (1 - (1 - f) * s);
  return i % 6 === 0 ? [v, t, p] : i % 6 === 1 ? [q, v, p] : i % 6 === 2 ? [p, v, t] : i % 6 === 3 ? [p, q, v] : i % 6 === 4 ? [t, p, v] : [v, p, q];
}
function _hexToHsv(hex: string): [number, number, number] {
  const [r, g, b] = _hexToRgb(hex);
  const mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn, rr = r / 255, gg = g / 255, bb = b / 255;
  let h = 0;
  if (mx !== mn) h = mx === r ? ((gg - bb) / d + (gg < bb ? 6 : 0)) / 6 : mx === g ? ((bb - rr) / d + 2) / 6 : ((rr - gg) / d + 4) / 6;
  return [h, mx === 0 ? 0 : d / mx / 255, mx / 255];
}

const _SV_SIZE = 180, _HUE_H = 14;

function AccentColorPicker() {
  const activeThemeId = useAppStore((s) => s.theme);
  const [activeAccent, setActiveAccent] = useState<string>(() => {
    return document.documentElement.style.getPropertyValue("--accent").trim() || "";
  });
  const [pickerOpen, setPickerOpen] = useState(false);
  const pickerRef = useRef<HTMLDivElement>(null);
  const svRef = useRef<HTMLDivElement>(null);
  const hueRef = useRef<HTMLDivElement>(null);

  const [h, setH] = useState(0.75);
  const [s, setS] = useState(0.6);
  const [v, setV] = useState(0.96);

  useEffect(() => {
    const current = document.documentElement.style.getPropertyValue("--accent").trim();
    // Sync CSS custom property to React state — legitimate external-system sync
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setActiveAccent((prev) => prev === current ? prev : current);
  }, [activeThemeId]);

  useEffect(() => {
    if (!pickerOpen) return;
    const handler = (e: MouseEvent) => {
      if (pickerRef.current && !pickerRef.current.contains(e.target as Node)) setPickerOpen(false);
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [pickerOpen]);

  const applyAccentColor = (hex: string) => {
    setActiveAccent(hex);
    document.documentElement.style.setProperty("--accent", hex);
    document.documentElement.style.setProperty("--accent-hover", hex);
    document.documentElement.style.setProperty("--accent-muted", `${hex}22`);
    document.documentElement.style.setProperty("--border-active", hex);
    document.documentElement.style.setProperty("--accent-glow", `0 0 20px ${hex}40`);
  };

  const resetToDefault = () => {
    document.documentElement.style.removeProperty("--accent");
    document.documentElement.style.removeProperty("--accent-hover");
    document.documentElement.style.removeProperty("--accent-muted");
    document.documentElement.style.removeProperty("--border-active");
    document.documentElement.style.removeProperty("--accent-glow");
    setActiveAccent("");
  };

  const currentThemeObj = getThemeById(activeThemeId);
  const curColor = activeAccent || currentThemeObj.accent;

  const commit = (hh: number, ss: number, vv: number) => {
    const [r, g, b] = _hsvToRgb(hh, ss, vv);
    applyAccentColor(_rgbToHex(r * 255, g * 255, b * 255));
  };

  const openPicker = () => {
    if (!pickerOpen) {
      try { const [ph, ps, pv] = _hexToHsv(curColor.startsWith("#") ? curColor : "#8b5cf6"); setH(ph); setS(ps); setV(pv); }
      catch { setH(0.75); setS(0.6); setV(0.96); }
    }
    setPickerOpen(!pickerOpen);
  };

  const hueColor = _rgbToHex(..._hsvToRgb(h, 1, 1).map((c) => c * 255) as [number, number, number]);

  return (
    <div style={{ marginTop: 14 }}>
      <Label>Accent Color</Label>
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
        {ACCENT_PRESETS.map((preset) => {
          const isSelected = curColor.toLowerCase() === preset.hex.toLowerCase();
          return (
            <button
              key={preset.hex}
              onClick={() => applyAccentColor(preset.hex)}
              title={preset.name}
              style={{
                width: 24,
                height: 24,
                borderRadius: "50%",
                background: preset.hex,
                border: isSelected ? "2px solid var(--text-primary)" : "2px solid transparent",
                boxShadow: isSelected ? `0 0 10px ${preset.hex}` : "none",
                cursor: "pointer",
                padding: 0,
                outline: "none",
                transition: "all 0.15s ease",
                transform: isSelected ? "scale(1.15)" : "scale(1)",
              }}
            />
          );
        })}

        {/* Custom color trigger — pure div, no border, overflow hidden */}
        <div ref={pickerRef} style={{ position: "relative", display: "inline-block" }}>
          <div
            onClick={openPicker}
            title="Custom color"
            style={{
              width: 24,
              height: 24,
              borderRadius: 24,
              overflow: "hidden",
              background: "conic-gradient(from 0deg, #ff0000, #ffff00, #00ff00, #00ffff, #0000ff, #ff00ff, #ff0000)",
              boxShadow: pickerOpen ? `0 0 10px ${curColor}` : "none",
              cursor: "pointer",
              transition: "all 0.15s ease",
              transform: pickerOpen ? "scale(1.15)" : "scale(1)",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              flexShrink: 0,
            }}
          >
            <div
              style={{
                width: 8,
                height: 8,
                borderRadius: 8,
                background: curColor.startsWith("#") ? curColor : "#fff",
                flexShrink: 0,
              }}
            />
          </div>

          {/* Popover */}
          {pickerOpen && (
            <div
              style={{
                position: "absolute",
                top: 32,
                left: 0,
                zIndex: 1000,
                background: "var(--bg-secondary)",
                border: "1px solid var(--border-default)",
                borderRadius: "var(--radius-md)",
                padding: 10,
                boxShadow: "0 8px 32px rgba(0,0,0,0.4)",
                display: "flex",
                flexDirection: "column",
                gap: 8,
              }}
            >
              {/* Saturation / Value square */}
              <div
                ref={svRef}
                onClick={(e) => {
                  const rect = (e.currentTarget as HTMLDivElement).getBoundingClientRect();
                  const sx = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
                  const sy = Math.max(0, Math.min(1, (e.clientY - rect.top) / rect.height));
                  setS(sx); setV(1 - sy);
                  commit(h, sx, 1 - sy);
                }}
                onMouseDown={() => {
                  const onMove = (ev: MouseEvent) => {
                    const rect = (svRef.current as HTMLDivElement).getBoundingClientRect();
                    const sx = Math.max(0, Math.min(1, (ev.clientX - rect.left) / rect.width));
                    const sy = Math.max(0, Math.min(1, (ev.clientY - rect.top) / rect.height));
                    setS(sx); setV(1 - sy);
                    commit(h, sx, 1 - sy);
                  };
                  document.addEventListener("mousemove", onMove);
                  document.addEventListener("mouseup", () => document.removeEventListener("mousemove", onMove), { once: true });
                }}
                style={{
                  width: _SV_SIZE,
                  height: _SV_SIZE,
                  borderRadius: "var(--radius-sm)",
                  cursor: "crosshair",
                  background: `linear-gradient(to top, #000, transparent), linear-gradient(to right, #fff, ${hueColor})`,
                  position: "relative",
                }}
              >
                <div style={{
                  position: "absolute", left: `${s * 100}%`, top: `${(1 - v) * 100}%`,
                  width: 10, height: 10, borderRadius: "50%",
                  border: "2px solid #fff", boxShadow: "0 0 4px rgba(0,0,0,0.6)",
                  transform: "translate(-50%, -50%)", pointerEvents: "none",
                }} />
              </div>

              {/* Hue strip */}
              <div
                ref={hueRef}
                onClick={(e) => {
                  const rect = (e.currentTarget as HTMLDivElement).getBoundingClientRect();
                  const hx = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
                  setH(hx);
                  commit(hx, s, v);
                }}
                onMouseDown={() => {
                  const onMove = (ev: MouseEvent) => {
                    const rect = (hueRef.current as HTMLDivElement).getBoundingClientRect();
                    const hx = Math.max(0, Math.min(1, (ev.clientX - rect.left) / rect.width));
                    setH(hx);
                    commit(hx, s, v);
                  };
                  document.addEventListener("mousemove", onMove);
                  document.addEventListener("mouseup", () => document.removeEventListener("mousemove", onMove), { once: true });
                }}
                style={{
                  width: _SV_SIZE,
                  height: _HUE_H,
                  borderRadius: "var(--radius-sm)",
                  cursor: "crosshair",
                  background: "linear-gradient(to right, #f00 0%, #ff0 17%, #0f0 33%, #0ff 50%, #00f 67%, #f0f 83%, #f00 100%)",
                  position: "relative",
                }}
              >
                <div style={{
                  position: "absolute", left: `${h * 100}%`, top: "50%",
                  transform: "translate(-50%, -50%)",
                  width: 8, height: _HUE_H + 4, borderRadius: 2,
                  border: "2px solid #fff", boxShadow: "0 0 4px rgba(0,0,0,0.5)",
                  pointerEvents: "none",
                }} />
              </div>

              {/* Preview + hex */}
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <div style={{ width: 20, height: 20, borderRadius: "var(--radius-sm)", background: curColor, border: "1px solid var(--border-default)", flexShrink: 0 }} />
                <span style={{ fontSize: 12, fontFamily: "monospace", color: "var(--text-secondary)" }}>{curColor.toUpperCase()}</span>
              </div>
            </div>
          )}
        </div>

        {/* Reset button if custom accent is set */}
        {activeAccent && (
          <button
            onClick={resetToDefault}
            style={{
              ...smallBtn,
              fontSize: 11,
              padding: "3px 8px",
            }}
          >
            Reset
          </button>
        )}
      </div>
    </div>
  );
}

function AddMcpServerForm({ onAdd }: { onAdd: (server: { name: string; transport: "stdio" | "sse"; command?: string; args?: string[]; url?: string; enabled: boolean }) => void }) {
  const [expanded, setExpanded] = useState(false);
  const [name, setName] = useState("");
  const [transport, setTransport] = useState<"stdio" | "sse">("stdio");
  const [command, setCommand] = useState("");
  const [args, setArgs] = useState("");
  const [url, setUrl] = useState("");

  if (!expanded) {
    return (
      <button onClick={() => setExpanded(true)} style={addBtn}>+ Add Server</button>
    );
  }

  const handleAdd = () => {
    if (!name.trim()) return;
    onAdd({
      name: name.trim(),
      transport,
      command: transport === "stdio" ? (command.trim() || undefined) : undefined,
      args: transport === "stdio" && args.trim() ? args.split(",").map((a) => a.trim()).filter(Boolean) : undefined,
      url: transport === "sse" ? (url.trim() || undefined) : undefined,
      enabled: true,
    });
    setName(""); setCommand(""); setArgs(""); setUrl("");
    setExpanded(false);
  };

  return (
    <div style={{ padding: "8px 0", display: "flex", flexDirection: "column", gap: 6 }}>
      <input type="text" value={name} onChange={(e) => setName(e.target.value)} placeholder="Server name" style={smallInput} autoFocus />
      <select value={transport} onChange={(e) => setTransport(e.target.value as "stdio" | "sse")} style={smallInput}>
        <option value="stdio">stdio (command)</option>
        <option value="sse">sse (URL)</option>
      </select>
      {transport === "stdio" ? (
        <>
          <input type="text" value={command} onChange={(e) => setCommand(e.target.value)} placeholder="Command (e.g. npx @modelcontextprotocol/server-filesystem)" style={smallInput} />
          <input type="text" value={args} onChange={(e) => setArgs(e.target.value)} placeholder="Args (comma-separated)" style={smallInput} />
        </>
      ) : (
        <input type="text" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://example.com/sse" style={smallInput} />
      )}
      <div style={{ display: "flex", gap: 6 }}>
        <button onClick={handleAdd} disabled={!name.trim()} style={{ ...smallBtn, background: "var(--accent)", color: "var(--accent-text)", border: "none", opacity: name.trim() ? 1 : 0.5 }}>Add</button>
        <button onClick={() => setExpanded(false)} style={smallBtn}>Cancel</button>
      </div>
    </div>
  );
}

const smallInput: React.CSSProperties = {
  padding: "6px 10px", borderRadius: "var(--radius-sm)", border: "1px solid var(--border-default)",
  background: "var(--bg-tertiary)", color: "var(--text-primary)", fontSize: 12, outline: "none", width: "100%", boxSizing: "border-box",
};
const smallBtn: React.CSSProperties = {
  padding: "4px 10px", borderRadius: "var(--radius-sm)", border: "1px solid var(--border-default)",
  background: "var(--bg-tertiary)", color: "var(--text-secondary)", cursor: "pointer", fontSize: 12,
};

const inputStyle: React.CSSProperties = {
  width: "100%", padding: "10px 14px", borderRadius: "var(--radius-md)",
  border: "1px solid var(--border-default)", background: "var(--bg-tertiary)",
  color: "var(--text-primary)", fontSize: 13, boxSizing: "border-box",
};
const clearBtn: React.CSSProperties = {
  padding: "10px 14px", borderRadius: "var(--radius-md)", border: "1px solid var(--border-default)",
  background: "var(--bg-tertiary)", color: "var(--text-secondary)", cursor: "pointer", fontSize: 12, fontWeight: 500,
};
const addBtn: React.CSSProperties = {
  padding: "6px 14px", borderRadius: "var(--radius-sm)", border: "1px solid var(--border-default)",
  background: "transparent", color: "var(--text-secondary)", cursor: "pointer", fontSize: 12, fontWeight: 500, marginTop: 8,
};
const cancelBtn: React.CSSProperties = {
  padding: "10px 20px", borderRadius: "var(--radius-md)", border: "1px solid var(--border-default)",
  background: "transparent", color: "var(--text-secondary)", cursor: "pointer", fontSize: 13, fontWeight: 500,
};
const saveBtn: React.CSSProperties = {
  padding: "10px 24px", borderRadius: "var(--radius-md)", border: "none",
  background: "var(--accent)", color: "var(--accent-text)", cursor: "pointer", fontSize: 13, fontWeight: 600,
};
