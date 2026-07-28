import { describe, it, expect, vi, beforeEach } from "vitest";
import { MemoryBridgeClient } from "../client.js";
import { memoryEventBus, kgEventBus } from "../events.js";

describe("MemoryBridgeClient", () => {
  let client: MemoryBridgeClient;

  beforeEach(() => {
    client = new MemoryBridgeClient();
  });

  it("should report non-running initially", () => {
    expect(client.isRunning).toBe(false);
  });

  it("should parse line-delimited JSON-RPC responses", () => {
    const handleStdout = (client as any).handleStdout.bind(client);
    let resolvedValue: any = null;

    (client as any).pendingRequests.set(1, {
      resolve: (val: any) => {
        resolvedValue = val;
      },
      reject: vi.fn(),
    });

    handleStdout(JSON.stringify({ jsonrpc: "2.0", id: 1, result: { success: true } }) + "\n");
    expect(resolvedValue).toEqual({ success: true });
  });

  it("should handle JSON-RPC notifications and emit memory events", () => {
    const handleStdout = (client as any).handleStdout.bind(client);
    const listener = vi.fn();
    const unsub = memoryEventBus.on("memory.saved", listener);

    handleStdout(
      JSON.stringify({
        jsonrpc: "2.0",
        method: "notifications/memory/saved",
        params: { note_id: "note-123", category: "lessons" },
      }) + "\n",
    );

    expect(listener).toHaveBeenCalledWith({
      type: "memory.saved",
      noteId: "note-123",
      category: "lessons",
    });

    unsub();
  });

  it("should handle KG notifications and emit KG events", () => {
    const handleStdout = (client as any).handleStdout.bind(client);
    const listener = vi.fn();
    const unsub = kgEventBus.on("entity.created", listener);

    handleStdout(
      JSON.stringify({
        jsonrpc: "2.0",
        method: "notifications/kg/entity_created",
        params: { entity: { id: "e1", name: "TestEntity", entity_type: "Concept" } },
      }) + "\n",
    );

    expect(listener).toHaveBeenCalledWith({
      type: "entity.created",
      entity: { id: "e1", name: "TestEntity", entity_type: "Concept" },
    });

    unsub();
  });

  it("should reject all pending requests on unexpected exit", () => {
    const rejectFn = vi.fn();
    (client as any).pendingRequests.set(100, {
      resolve: vi.fn(),
      reject: rejectFn,
    });

    (client as any).rejectAllPending("Process crashed");
    expect(rejectFn).toHaveBeenCalledWith(expect.any(Error));
    expect((client as any).pendingRequests.size).toBe(0);
  });
});
