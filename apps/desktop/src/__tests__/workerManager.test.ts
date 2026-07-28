import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
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

  it("should emit events on start and stop", () => {
    const events: any[] = [];
    manager.onEvent((e) => events.push(e));

    manager.start();
    expect(manager.isRunning).toBe(true);
    expect(events.some((e) => e.message.includes("started"))).toBe(true);

    manager.stop();
    expect(manager.isRunning).toBe(false);
  });
});
