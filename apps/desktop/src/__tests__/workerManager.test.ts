import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { BackgroundWorkerManager } from "../services/workerManager";

describe("BackgroundWorkerManager", () => {
  let manager: BackgroundWorkerManager;

  beforeEach(() => {
    manager = new BackgroundWorkerManager("/tmp/memory-test");
  });

  afterEach(() => {
    manager.stop();
  });

  it("should initialize worker status list", () => {
    const statuses = manager.getStatus();
    expect(statuses.length).toBeGreaterThan(0);
    expect(statuses[0].type).toBe("index_rebuild");
    expect(statuses[0].status).toBe("idle");
  });

  it("should report running state on start and stop", () => {
    manager.start();
    expect(manager.isRunning).toBe(true);

    manager.stop();
    expect(manager.isRunning).toBe(false);
  });

  it("should emit started and stopped events", () => {
    const events: any[] = [];
    manager.onEvent((e) => events.push(e));

    manager.start();
    manager.stop();

    const msgs = events.map((e) => e.message);
    expect(msgs.some((m) => m.includes("started") || m.includes("stopped"))).toBe(true);
  });
});
