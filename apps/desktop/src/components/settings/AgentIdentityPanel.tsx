import React, { useEffect, useState } from "react";
import { agentRegistry, type AgentIdentity } from "../../services/agentRegistry";
import { Badge } from "../ui";

export function AgentIdentityPanel() {
  const [agents, setAgents] = useState<AgentIdentity[]>([]);
  const [activeId, setActiveId] = useState<string>("default");
  const [newName, setNewName] = useState("");
  const [newRole, setNewRole] = useState("Researcher");
  const [newColor, setNewColor] = useState("#10b981");
  const [isCreating, setIsCreating] = useState(false);

  useEffect(() => {
    agentRegistry.init().catch(console.error);
    const unsub = agentRegistry.subscribe((updated) => {
      setAgents(updated);
      setActiveId(agentRegistry.getActiveAgentId());
    });
    return unsub;
  }, []);

  const handleSelectAgent = (id: string) => {
    agentRegistry.setActiveAgentId(id);
    setActiveId(id);
  };

  const handleCreateAgent = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newName.trim()) return;

    try {
      const created = await agentRegistry.createAgent(newName.trim(), newRole, newColor);
      handleSelectAgent(created.id);
      setNewName("");
      setIsCreating(false);
    } catch (err) {
      console.error("Failed to create agent identity:", err);
    }
  };

  return (
    <div>
      {/* Header row */}
      <div style={{
        display: "flex",
        justifyContent: "space-between",
        alignItems: "center",
        marginBottom: 14,
      }}>
        <div>
          <div style={{
            fontSize: 12,
            color: "var(--text-secondary)",
            fontWeight: 500,
          }}>
            Manage active identities and namespaces for multi-agent execution.
          </div>
        </div>
        <button
          onClick={() => setIsCreating(!isCreating)}
          style={{
            padding: "6px 14px",
            borderRadius: "var(--radius-sm)",
            background: isCreating ? "transparent" : "var(--accent)",
            color: isCreating ? "var(--text-secondary)" : "var(--accent-text)",
            border: isCreating
              ? "1px solid var(--border-default)"
              : "none",
            cursor: "pointer",
            fontSize: 12,
            fontWeight: 500,
            transition: "all 0.15s ease",
          }}
        >
          {isCreating ? "Cancel" : "+ New Agent"}
        </button>
      </div>

      {/* Create agent form */}
      {isCreating && (
        <form
          onSubmit={handleCreateAgent}
          style={{
            background: "var(--bg-tertiary)",
            padding: 16,
            borderRadius: "var(--radius-md)",
            marginBottom: 14,
            border: "1px solid var(--border-default)",
            display: "flex",
            flexDirection: "column",
            gap: 12,
          }}
        >
          <div>
            <label style={labelStyle}>Agent Name</label>
            <input
              type="text"
              placeholder="e.g. Code Reviewer, Security Auditor"
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              style={inputStyle}
              required
            />
          </div>

          <div style={{ display: "flex", gap: 12 }}>
            <div style={{ flex: 1 }}>
              <label style={labelStyle}>Role / Scope</label>
              <input
                type="text"
                placeholder="e.g. Refactoring Specialist"
                value={newRole}
                onChange={(e) => setNewRole(e.target.value)}
                style={inputStyle}
              />
            </div>
            <div>
              <label style={labelStyle}>Badge Color</label>
              <input
                type="color"
                value={newColor}
                onChange={(e) => setNewColor(e.target.value)}
                style={{
                  width: 42,
                  height: 34,
                  padding: 2,
                  borderRadius: "var(--radius-sm)",
                  border: "1px solid var(--border-default)",
                  background: "var(--bg-tertiary)",
                  cursor: "pointer",
                }}
              />
            </div>
          </div>

          <button
            type="submit"
            style={{
              padding: "8px 14px",
              borderRadius: "var(--radius-md)",
              background: "var(--accent)",
              color: "var(--accent-text)",
              border: "none",
              cursor: "pointer",
              fontWeight: 600,
              fontSize: 12,
              transition: "opacity 0.15s",
            }}
          >
            Create Agent Identity
          </button>
        </form>
      )}

      {/* Agent cards */}
      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        {agents.map((agent) => {
          const isActive = agent.id === activeId;
          return (
            <div
              key={agent.id}
              onClick={() => handleSelectAgent(agent.id)}
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                padding: "10px 14px",
                borderRadius: "var(--radius-md)",
                border: isActive
                  ? `2px solid ${agent.color}`
                  : "1px solid var(--border-default)",
                background: isActive
                  ? "var(--accent-muted)"
                  : "var(--bg-tertiary)",
                cursor: "pointer",
                transition: "all 0.15s ease",
              }}
              onMouseEnter={(e) => {
                if (!isActive) {
                  (e.currentTarget as HTMLDivElement).style.background = "var(--bg-hover)";
                  (e.currentTarget as HTMLDivElement).style.borderColor = agent.color;
                }
              }}
              onMouseLeave={(e) => {
                if (!isActive) {
                  (e.currentTarget as HTMLDivElement).style.background = "var(--bg-tertiary)";
                  (e.currentTarget as HTMLDivElement).style.borderColor = "";
                }
              }}
            >
              <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                {/* Color dot */}
                <span
                  style={{
                    width: 10,
                    height: 10,
                    borderRadius: "50%",
                    backgroundColor: agent.color,
                    display: "inline-block",
                    boxShadow: isActive ? `0 0 8px ${agent.color}60` : "none",
                    flexShrink: 0,
                  }}
                />
                <div>
                  <div style={{
                    fontWeight: 600,
                    fontSize: 13,
                    color: "var(--text-primary)",
                    display: "flex",
                    alignItems: "center",
                    gap: 6,
                  }}>
                    {agent.name}
                    {isActive && (
                      <span style={{
                        fontSize: 10,
                        fontWeight: 500,
                        color: agent.color,
                        background: `${agent.color}18`,
                        padding: "1px 6px",
                        borderRadius: "var(--radius-xs)",
                      }}>
                        Active
                      </span>
                    )}
                  </div>
                  <div style={{
                    fontSize: 11,
                    color: "var(--text-tertiary)",
                    marginTop: 2,
                  }}>
                    <code style={{
                      fontSize: 10,
                      padding: "1px 4px",
                      borderRadius: 3,
                      background: "var(--bg-hover)",
                      color: "var(--text-secondary)",
                      fontFamily: "var(--font-mono)",
                    }}>{agent.id}</code>
                    <span style={{ margin: "0 6px", opacity: 0.5 }}>·</span>
                    {agent.role}
                  </div>
                </div>
              </div>

              <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <Badge variant={agent.status === "busy" ? "warning" : "success"}>
                  {agent.status}
                </Badge>
              </div>
            </div>
          );
        })}

        {agents.length === 0 && (
          <div style={{
            textAlign: "center",
            padding: "20px 0",
            fontSize: 12,
            color: "var(--text-tertiary)",
          }}>
            No agent identities registered. Click &ldquo;+ New Agent&rdquo; to create one.
          </div>
        )}
      </div>
    </div>
  );
}

// ── Shared inline styles matching the SettingsPanel design system ────────

const labelStyle: React.CSSProperties = {
  display: "block",
  fontSize: 12,
  color: "var(--text-secondary)",
  marginBottom: 6,
  fontWeight: 500,
};

const inputStyle: React.CSSProperties = {
  width: "100%",
  padding: "8px 12px",
  borderRadius: "var(--radius-sm)",
  border: "1px solid var(--border-default)",
  background: "var(--bg-tertiary)",
  color: "var(--text-primary)",
  fontSize: 12,
  boxSizing: "border-box" as const,
  outline: "none",
};
