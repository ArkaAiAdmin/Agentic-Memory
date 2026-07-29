/**
 * OnboardingWizard — First-run setup flow.
 *
 * 4 steps: Welcome → Theme → Provider → Project
 * Sleek, animated, no generic colors or icons.
 */

import React, { useState, useCallback } from "react";
import { useAppStore } from "../../stores/appStore";
import { THEMES, applyTheme, type ThemePalette } from "../../services/themes";
import { agentService } from "../../services/agentService";
import { setApiKey, getApiKey } from "../../services/secretStore";
import { PROVIDER_DEFAULTS, type ProviderConfig } from "@ami/llm";

interface Props {
  onComplete: () => void;
}

type Step = "welcome" | "theme" | "provider" | "project";

const PROVIDER_LABELS: Record<ProviderConfig["type"], string> = {
  openai: "OpenAI",
  anthropic: "Anthropic",
  google: "Google Gemini",
  lmstudio: "LM Studio",
  ollama: "Ollama",
  litellm: "LiteLLM",
};

const LOCAL_PROVIDERS = new Set<ProviderConfig["type"]>(["lmstudio", "ollama"]);

export function OnboardingWizard({ onComplete }: Props) {
  const [step, setStep] = useState<Step>("welcome");
  const [selectedTheme, setSelectedTheme] = useState<string>("obsidian");
  const [providerType, setProviderType] = useState<ProviderConfig["type"]>("openai");
  const [providerModel, setProviderModel] = useState("gpt-4o");
  const [apiKey, setApiKeyInput] = useState("");
  const [projectPath, setProjectPath] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const { setTheme, setProviderConfig, setActiveProject, setHasCompletedOnboarding, addProject } = useAppStore();

  const handleThemeSelect = useCallback((id: string) => {
    setSelectedTheme(id);
    const theme = THEMES.find((t) => t.id === id);
    if (theme) {
      applyTheme(theme);
      setTheme(id);
    }
  }, [setTheme]);

  const handleFinish = useCallback(async () => {
    setSaving(true);
    setError(null);
    try {
      // Save provider config
      const config: ProviderConfig = {
        type: providerType,
        model: providerModel || PROVIDER_DEFAULTS[providerType].defaultModel,
      };
      setProviderConfig(config);

      // Save API key if provided
      if (apiKey.trim() && !LOCAL_PROVIDERS.has(providerType)) {
        await setApiKey(providerType, apiKey.trim());
      }

      // Set project
      if (projectPath.trim()) {
        setActiveProject(projectPath.trim());
        addProject({
          root: projectPath.trim(),
          name: projectPath.split("/").pop() ?? "Project",
          files: [],
        });
      }

      // Fire-and-forget agent initialization — don't block onboarding on it.
      // The useAgent hook will retry initialization when the user first chats.
      (async () => {
        try {
          const effectiveKey = apiKey.trim() || (await getApiKey(providerType)) || undefined;
          await agentService.setProvider({ ...config, apiKey: effectiveKey });
          await agentService.initialize();
        } catch (initErr) {
          console.warn("[Onboarding] Agent init deferred:", initErr);
        }
      })();

      setHasCompletedOnboarding(true);
      onComplete();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Setup failed");
    } finally {
      setSaving(false);
    }
  }, [providerType, providerModel, apiKey, projectPath, setProviderConfig, setActiveProject, addProject, setHasCompletedOnboarding, onComplete]);

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Setup Wizard"
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 10000,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        background: "var(--bg-primary, #0a0b10)",
      }}
    >
      <div
        style={{
          width: 640,
          maxWidth: "90vw",
          maxHeight: "85vh",
          overflow: "auto",
          borderRadius: 20,
          border: "1px solid var(--border-default, rgba(255,255,255,0.06))",
          background: "var(--bg-secondary, #12141d)",
          backdropFilter: "blur(20px) saturate(1.3)",
          boxShadow: "0 24px 80px rgba(0,0,0,0.6), inset 0 1px 0 rgba(255,255,255,0.04)",
        }}
      >
        {/* Progress bar */}
        <div style={{ padding: "20px 32px 0" }}>
          <div style={{ display: "flex", gap: 6 }}>
            {(["welcome", "theme", "provider", "project"] as Step[]).map((s, i) => (
              <div
                key={s}
                style={{
                  flex: 1,
                  height: 3,
                  borderRadius: 2,
                  background:
                    i <= ["welcome", "theme", "provider", "project"].indexOf(step)
                      ? "var(--accent, #8b5cf6)"
                      : "var(--border-subtle, rgba(255,255,255,0.03))",
                  transition: "background 0.3s",
                }}
              />
            ))}
          </div>
        </div>

        {/* Content */}
        <div style={{ padding: "24px 32px 32px" }} key={step}>
          <div style={{ animation: "fadeIn 0.25s ease" }}>
          {step === "welcome" && (
            <WelcomeStep onNext={() => setStep("theme")} />
          )}
          {step === "theme" && (
            <ThemeStep
              selected={selectedTheme}
              onSelect={handleThemeSelect}
              onNext={() => setStep("provider")}
              onBack={() => setStep("welcome")}
            />
          )}
          {step === "provider" && (
            <ProviderStep
              type={providerType}
              model={providerModel}
              apiKey={apiKey}
              error={error}
              onTypeChange={(t) => { setProviderType(t); setProviderModel(PROVIDER_DEFAULTS[t].defaultModel); }}
              onModelChange={setProviderModel}
              onApiKeyChange={setApiKeyInput}
              onNext={() => setStep("project")}
              onBack={() => setStep("theme")}
            />
          )}
          {step === "project" && (
            <ProjectStep
              path={projectPath}
              onPathChange={setProjectPath}
              onFinish={handleFinish}
              onBack={() => setStep("provider")}
              saving={saving}
            />
          )}
          </div>
        </div>
      </div>
    </div>
  );
}

// ── Step: Welcome ────────────────────────────────────────────────────────

function WelcomeStep({ onNext }: { onNext: () => void }) {
  return (
    <div style={{ textAlign: "center" }}>
      {/* Logo mark */}
      <div
        style={{
          width: 72,
          height: 72,
          borderRadius: 20,
          background: "var(--accent-gradient, linear-gradient(135deg, #8b5cf6, #6366f1))",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          margin: "0 auto 24px",
          boxShadow: "var(--accent-glow, 0 0 24px rgba(139, 92, 246, 0.25)), 0 8px 32px rgba(139, 92, 246, 0.3)",
        }}
      >
        <svg width="36" height="36" viewBox="0 0 36 36" fill="none">
          <path d="M18 4L4 12v12l14 8 14-8V12L18 4z" stroke="white" strokeWidth="2" fill="none"/>
          <path d="M4 12l14 8m0 0l14-8m-14 8v12" stroke="white" strokeWidth="2" opacity="0.5"/>
          <circle cx="18" cy="18" r="4" fill="white"/>
        </svg>
      </div>

      <h1
        style={{
          fontSize: 28,
          fontWeight: 700,
          color: "var(--text-primary, #f1f3f8)",
          margin: "0 0 8px",
          letterSpacing: -0.5,
        }}
      >
        Agentic Memory IDE
      </h1>
      <p
        style={{
          fontSize: 15,
          color: "var(--text-secondary, #8b93a8)",
          margin: "0 0 32px",
          lineHeight: 1.6,
        }}
      >
        The memory-first coding agent.<br />
        Every action builds context. Every session learns.
      </p>

      <button
        onClick={onNext}
        style={{
          padding: "12px 32px",
          borderRadius: 10,
          border: "none",
          background: "var(--accent-gradient, linear-gradient(135deg, #8b5cf6, #6366f1))",
          color: "var(--accent-text, #fff)",
          fontSize: 15,
          fontWeight: 600,
          cursor: "pointer",
          boxShadow: "var(--accent-glow, 0 0 24px rgba(139, 92, 246, 0.25)), 0 4px 16px rgba(139, 92, 246, 0.3)",
          transition: "transform 0.2s cubic-bezier(0.34, 1.56, 0.64, 1), box-shadow 0.2s",
        }}
        onMouseEnter={(e) => {
          e.currentTarget.style.transform = "translateY(-2px) scale(1.02)";
          e.currentTarget.style.boxShadow = "0 0 32px rgba(139, 92, 246, 0.4), 0 8px 24px rgba(139, 92, 246, 0.3)";
        }}
        onMouseLeave={(e) => {
          e.currentTarget.style.transform = "translateY(0) scale(1)";
          e.currentTarget.style.boxShadow = "0 0 24px rgba(139, 92, 246, 0.25), 0 4px 16px rgba(139, 92, 246, 0.3)"
        }}
      >
        Get Started
      </button>
    </div>
  );
}

// ── Step: Theme ──────────────────────────────────────────────────────────

function ThemeStep({
  selected,
  onSelect,
  onNext,
  onBack,
}: {
  selected: string;
  onSelect: (id: string) => void;
  onNext: () => void;
  onBack: () => void;
}) {
  return (
    <div>
      <h2
        style={{
          fontSize: 20,
          fontWeight: 600,
          color: "var(--text-primary, #f1f3f8)",
          margin: "0 0 4px",
        }}
      >
        Choose your look
      </h2>
      <p style={{ fontSize: 13, color: "var(--text-secondary, #8b93a8)", margin: "0 0 20px" }}>
        Pick a theme. You can change this later in Settings.
      </p>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 10, marginBottom: 24 }}>
        {THEMES.map((theme) => (
          <ThemeCard
            key={theme.id}
            theme={theme}
            isSelected={selected === theme.id}
            onSelect={() => onSelect(theme.id)}
          />
        ))}
      </div>

      <StepNav onBack={onBack} onNext={onNext} />
    </div>
  );
}

function ThemeCard({
  theme,
  isSelected,
  onSelect,
}: {
  theme: ThemePalette;
  isSelected: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      onClick={onSelect}
      style={{
        padding: 14,
        borderRadius: 12,
        border: `2px solid ${isSelected ? theme.accent : theme.borderDefault}`,
        background: theme.bgSecondary,
        cursor: "pointer",
        textAlign: "left",
        transition: "border-color 0.2s, transform 0.15s",
        transform: isSelected ? "scale(1.02)" : "scale(1)",
      }}
    >
      {/* Color preview dots */}
      <div style={{ display: "flex", gap: 5, marginBottom: 10 }}>
        {[theme.accent, theme.success, theme.warning, theme.error, theme.syntaxKeyword].map(
          (color, i) => (
            <div
              key={i}
              style={{
                width: 16,
                height: 16,
                borderRadius: 4,
                background: color,
              }}
            />
          ),
        )}
      </div>

      <div
        style={{
          fontSize: 13,
          fontWeight: 600,
          color: theme.textPrimary,
          marginBottom: 2,
        }}
      >
        {theme.name}
      </div>
      <div style={{ fontSize: 11, color: theme.textTertiary }}>
        {theme.appearance === "dark" ? "Dark" : "Light"}
      </div>

      {isSelected && (
        <div
          style={{
            marginTop: 8,
            width: 18,
            height: 18,
            borderRadius: 4,
            background: theme.accent,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
          }}
        >
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
            <path d="M2 6l3 3 5-5" stroke={theme.accentText} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
          </svg>
        </div>
      )}
    </button>
  );
}

// ── Step: Provider ───────────────────────────────────────────────────────

function ProviderStep({
  type,
  model,
  apiKey,
  error,
  onTypeChange,
  onModelChange,
  onApiKeyChange,
  onNext,
  onBack,
}: {
  type: ProviderConfig["type"];
  model: string;
  apiKey: string;
  error: string | null;
  onTypeChange: (t: ProviderConfig["type"]) => void;
  onModelChange: (m: string) => void;
  onApiKeyChange: (k: string) => void;
  onNext: () => void;
  onBack: () => void;
}) {
  const isLocal = LOCAL_PROVIDERS.has(type);

  return (
    <div>
      <h2
        style={{
          fontSize: 20,
          fontWeight: 600,
          color: "var(--text-primary, #f1f3f8)",
          margin: "0 0 4px",
        }}
      >
        Connect your AI
      </h2>
      <p style={{ fontSize: 13, color: "var(--text-secondary, #8b93a8)", margin: "0 0 20px" }}>
        Choose a provider and enter your API key.
      </p>

      {/* Provider grid */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 8, marginBottom: 16 }}>
        {(Object.keys(PROVIDER_LABELS) as ProviderConfig["type"][]).map((t) => (
          <button
            key={t}
            onClick={() => onTypeChange(t)}
            style={{
              padding: "10px 8px",
              borderRadius: 8,
              border: `1.5px solid ${type === t ? "var(--accent, #8b5cf6)" : "var(--border-default, rgba(255,255,255,0.06))"}`,
              background: type === t ? "var(--accent-muted, rgba(139,92,246,0.12))" : "var(--bg-tertiary, #191c28)",
              color: "var(--text-primary, #f1f3f8)",
              fontSize: 12,
              fontWeight: 500,
              cursor: "pointer",
              transition: "all 0.15s",
            }}
          >
            {PROVIDER_LABELS[t]}
          </button>
        ))}
      </div>

      {/* Model */}
      <div style={{ marginBottom: 12 }}>
        <label style={labelStyle}>Model</label>
        <input
          type="text"
          value={model}
          onChange={(e) => onModelChange(e.target.value)}
          placeholder={PROVIDER_DEFAULTS[type].defaultModel}
          aria-label="Model"
          style={inputStyle}
        />
      </div>

      {/* API key */}
      {!isLocal && (
        <div style={{ marginBottom: 12 }}>
          <label style={labelStyle}>API Key</label>
          <input
            type="password"
            value={apiKey}
            onChange={(e) => onApiKeyChange(e.target.value)}
            placeholder="sk-..."
            aria-label="API Key"
            style={inputStyle}
            autoComplete="off"
          />
          <div style={{ fontSize: 11, color: "var(--text-tertiary, #4e5468)", marginTop: 4 }}>
            Stored in your OS keychain, never in the app.
          </div>
        </div>
      )}

      {isLocal && (
        <div
          style={{
            padding: "10px 14px",
            borderRadius: 8,
            background: "var(--info-muted, rgba(59,130,246,0.15))",
            fontSize: 12,
            color: "var(--info, #3b82f6)",
            marginBottom: 12,
          }}
        >
          {type === "ollama"
            ? "Make sure Ollama is running: ollama serve"
            : (
              <>
                <strong>LM Studio setup:</strong> Open LM Studio, load a model, and start the server on localhost:1234.
                <br />
                No API key needed — the IDE connects directly to your local server.
              </>
            )}
        </div>
      )}

      {error && (
        <div
          style={{
            padding: "8px 12px",
            borderRadius: 8,
            background: "var(--error-muted, rgba(239,68,68,0.15))",
            color: "var(--error, #ef4444)",
            fontSize: 12,
            marginBottom: 12,
          }}
        >
          {error}
        </div>
      )}

      <StepNav onBack={onBack} onNext={onNext} />
    </div>
  );
}

// ── Step: Project ────────────────────────────────────────────────────────

function ProjectStep({
  path,
  onPathChange,
  onFinish,
  onBack,
  saving,
}: {
  path: string;
  onPathChange: (p: string) => void;
  onFinish: () => void;
  onBack: () => void;
  saving: boolean;
}) {
  return (
    <div>
      <h2
        style={{
          fontSize: 20,
          fontWeight: 600,
          color: "var(--text-primary, #f1f3f8)",
          margin: "0 0 4px",
        }}
      >
        Open a project
      </h2>
      <p style={{ fontSize: 13, color: "var(--text-secondary, #8b93a8)", margin: "0 0 20px" }}>
        Point to a directory to start coding. You can skip this and open one later.
      </p>

      <div style={{ marginBottom: 20 }}>
        <label style={labelStyle}>Project path</label>
        <input
          type="text"
          value={path}
          onChange={(e) => onPathChange(e.target.value)}
          placeholder="/Users/you/projects/my-app"
          aria-label="Project path"
          style={inputStyle}
        />
      </div>

      <div style={{ display: "flex", justifyContent: "flex-end", gap: 10 }}>
        <button
          onClick={onBack}
          aria-label="Previous step"
          style={{
            padding: "10px 20px",
            borderRadius: 8,
            border: "1px solid var(--border-default, rgba(255,255,255,0.06))",
            background: "transparent",
            color: "var(--text-secondary, #8b93a8)",
            fontSize: 13,
            cursor: "pointer",
            transition: "border-color 0.2s, color 0.2s",
          }}
        >
          Back
        </button>
        <button
          onClick={onFinish}
          disabled={saving}
          style={{
            padding: "10px 24px",
            borderRadius: 8,
            border: "none",
            background: "var(--accent-gradient, linear-gradient(135deg, #8b5cf6, #6366f1))",
            color: "var(--accent-text, #fff)",
            fontSize: 13,
            fontWeight: 600,
            cursor: saving ? "wait" : "pointer",
            opacity: saving ? 0.7 : 1,
            boxShadow: "var(--accent-glow, 0 0 24px rgba(139, 92, 246, 0.25)), 0 4px 16px rgba(139, 92, 246, 0.3)",
            transition: "transform 0.2s, box-shadow 0.2s, opacity 0.2s",
          }}
        >
          {saving ? "Setting up..." : "Start Coding"}
        </button>
      </div>
    </div>
  );
}

// ── Shared ───────────────────────────────────────────────────────────────

function StepNav({ onBack, onNext }: { onBack: () => void; onNext: () => void }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between" }}>
      <button
        onClick={onBack}
        aria-label="Previous step"
        style={{
          padding: "10px 20px",
          borderRadius: 8,
          border: "1px solid var(--border-default, rgba(255,255,255,0.06))",
          background: "transparent",
          color: "var(--text-secondary, #8b93a8)",
          fontSize: 13,
          cursor: "pointer",
          transition: "border-color 0.2s, color 0.2s",
        }}
      >
        Back
      </button>
      <button
        onClick={onNext}
        aria-label="Next step"
        style={{
          padding: "10px 24px",
          borderRadius: 8,
          border: "none",
          background: "var(--accent-gradient, linear-gradient(135deg, #8b5cf6, #6366f1))",
          color: "var(--accent-text, #fff)",
          fontSize: 13,
          fontWeight: 600,
          cursor: "pointer",
          boxShadow: "var(--accent-glow, 0 0 24px rgba(139, 92, 246, 0.25)), 0 4px 16px rgba(139, 92, 246, 0.3)",
          transition: "transform 0.2s, box-shadow 0.2s",
        }}
      >
        Continue
      </button>
    </div>
  );
}

const labelStyle: React.CSSProperties = {
  display: "block",
  fontSize: 12,
  color: "var(--text-secondary, #8b93a8)",
  marginBottom: 6,
  fontWeight: 500,
};

const inputStyle: React.CSSProperties = {
  width: "100%",
  padding: "10px 14px",
  borderRadius: 8,
  border: "1px solid var(--border-default, rgba(255,255,255,0.06))",
  background: "var(--bg-tertiary, #191c28)",
  color: "var(--text-primary, #f1f3f8)",
  fontSize: 13,
  outline: "none",
  boxSizing: "border-box",
  transition: "border-color 0.2s, box-shadow 0.2s",
};
