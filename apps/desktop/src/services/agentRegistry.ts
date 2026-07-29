/**
 * Agent Registry Service
 *
 * Manages active agent identities in the IDE and synchronizes with
 * the kernel's agent registry via memory_maintenance("agent_list").
 */

import { memoryBridge } from "@ami/memory-bridge";

export interface AgentIdentity {
  id: string;
  name: string;
  role: string;
  color: string;
  status: "idle" | "busy" | "offline";
  createdAt: number;
  lastActive: number;
  metadata?: Record<string, unknown>;
}

type RegistryListener = (agents: AgentIdentity[]) => void;

class AgentRegistryService {
  private agents: Map<string, AgentIdentity> = new Map();
  private listeners: Set<RegistryListener> = new Set();
  private activeAgentId: string = "default";
  private syncInterval: ReturnType<typeof setInterval> | null = null;

  constructor() {
    // Register default primary agent
    this.registerLocal({
      id: "default",
      name: "Primary Agent",
      role: "General Assistant",
      color: "var(--accent)",
      status: "idle",
      createdAt: Date.now(),
      lastActive: Date.now(),
    });
  }

  /** Initialize service and start kernel synchronization loop */
  async init(): Promise<void> {
    await this.syncFromKernel();
    if (!this.syncInterval) {
      this.syncInterval = setInterval(() => this.syncFromKernel(), 30_000);
    }
  }

  /** Stop sync loop */
  stop(): void {
    if (this.syncInterval) {
      clearInterval(this.syncInterval);
      this.syncInterval = null;
    }
  }

  /** Subscribe to agent registry changes */
  subscribe(listener: RegistryListener): () => void {
    this.listeners.add(listener);
    listener(this.getAll());
    return () => this.listeners.delete(listener);
  }

  private notify(): void {
    const list = this.getAll();
    for (const listener of this.listeners) {
      listener(list);
    }
  }

  /** Get all registered agents */
  getAll(): AgentIdentity[] {
    return Array.from(this.agents.values()).sort((a, b) => b.lastActive - a.lastActive);
  }

  /** Get active agent identity for current tab/context */
  getActiveAgentId(): string {
    return this.activeAgentId;
  }

  /** Set active agent ID */
  setActiveAgentId(id: string): void {
    if (this.agents.has(id)) {
      this.activeAgentId = id;
    }
  }

  /** Get agent identity by ID */
  get(id: string): AgentIdentity | undefined {
    return this.agents.get(id);
  }

  /** Register or update a local agent identity */
  registerLocal(identity: Partial<AgentIdentity> & { id: string }): AgentIdentity {
    const existing = this.agents.get(identity.id);
    const updated: AgentIdentity = {
      id: identity.id,
      name: identity.name ?? existing?.name ?? identity.id,
      role: identity.role ?? existing?.role ?? "Assistant",
      color: identity.color ?? existing?.color ?? "#8b5cf6",
      status: identity.status ?? existing?.status ?? "idle",
      createdAt: existing?.createdAt ?? Date.now(),
      lastActive: Date.now(),
      metadata: identity.metadata ?? existing?.metadata ?? {},
    };

    this.agents.set(identity.id, updated);
    this.notify();

    // Async sync to kernel if bridge is running
    // Note: kernel only stores display_name/parent_agent/namespace; role/color are local UI state
    if (memoryBridge.isRunning) {
      memoryBridge.initAgent(updated.id, {
        displayName: updated.name,
      }).catch(err => console.error("[AgentRegistry] Kernel init failed:", err));
    }

    return updated;
  }

  /** Update agent runtime status */
  setStatus(id: string, status: "idle" | "busy" | "offline"): void {
    const agent = this.agents.get(id);
    if (agent) {
      agent.status = status;
      agent.lastActive = Date.now();
      this.notify();
    }
  }

  /** Create a new named agent identity */
  async createAgent(name: string, role = "Assistant", color = "#10b981"): Promise<AgentIdentity> {
    const id = `agent-${name.toLowerCase().replace(/[^a-z0-9]/g, "-")}-${Date.now().toString(36)}`;
    const identity = this.registerLocal({
      id,
      name,
      role,
      color,
      status: "idle",
    });
    return identity;
  }

  /** Sync active agents list, publish heartbeat, and poll inbox from Python kernel */
  async syncFromKernel(): Promise<void> {
    if (!memoryBridge.isRunning) return;
    try {
      // 1. Fetch agent list from kernel
      const res = await memoryBridge.listAgents();
      if (res && Array.isArray(res.agents)) {
        for (const kAgent of res.agents) {
          if (!this.agents.has(kAgent.agent_id)) {
            this.agents.set(kAgent.agent_id, {
              id: kAgent.agent_id,
              name: kAgent.metadata?.name ?? kAgent.agent_id,
              role: kAgent.metadata?.role ?? "Kernel Agent",
              color: kAgent.metadata?.color ?? "#6366f1",
              status: "idle",
              createdAt: kAgent.created_at ? new Date(kAgent.created_at).getTime() : Date.now(),
              lastActive: kAgent.last_active ? new Date(kAgent.last_active).getTime() : Date.now(),
            });
          }
        }
        this.notify();
      }

      // 2. Publish heartbeat for active agent (Gap I)
      const active = this.get(this.activeAgentId);
      if (active) {
        active.lastActive = Date.now();
        await memoryBridge.initAgent(active.id, {
          displayName: active.name,
        });
      }

      // 3. Poll inbox for unread inter-agent messages (Gap H)
      await this.pollInbox();
    } catch (err) {
      console.warn("[AgentRegistry] Sync from kernel skipped:", err);
    }
  }

  /** Poll inbox for unread inter-agent messages via memory_coordinate */
  async pollInbox(): Promise<void> {
    if (!memoryBridge.isRunning) return;
    try {
      const msgs = await memoryBridge.coordinateMessage("read_messages");
      if (msgs && Array.isArray(msgs) && msgs.length > 0) {
        console.log(`[AgentRegistry] Received ${msgs.length} unread agent message(s):`, msgs);
        for (const msg of msgs) {
          // Notify listeners or log event
          console.log(`[AgentMessage] From ${msg.from_agent || "unknown"}: [${msg.message_type}]`, msg.payload);
        }
      }
    } catch (err) {
      console.warn("[AgentRegistry] Inbox poll failed:", err);
    }
  }
}

export const agentRegistry = new AgentRegistryService();
