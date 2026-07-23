/**
 * Memory Event Bus
 *
 * Provides a pub/sub mechanism for memory system events.
 * The bridge emits events when memories are saved, searched,
 * KG entities are created, contradictions detected, etc.
 */

import type { MemoryEvent, KGEvent, Unsubscribe } from "@ami/shared";

type EventHandler<T> = (event: T) => void;

class EventBus<T extends { type: string }> {
  private handlers: Map<string, Set<EventHandler<T>>> = new Map();

  on(eventType: string, handler: EventHandler<T>): Unsubscribe {
    if (!this.handlers.has(eventType)) {
      this.handlers.set(eventType, new Set());
    }
    this.handlers.get(eventType)!.add(handler);

    return () => {
      this.handlers.get(eventType)?.delete(handler);
    };
  }

  onAny(handler: EventHandler<T>): Unsubscribe {
    return this.on("*", handler);
  }

  emit(event: T): void {
    // Fire specific handlers
    const specific = this.handlers.get(event.type);
    if (specific) {
      for (const handler of specific) {
        try {
          handler(event);
        } catch (err) {
          console.error(`[EventBus] Handler error for ${event.type}:`, err);
        }
      }
    }

    // Fire wildcard handlers
    const wildcard = this.handlers.get("*");
    if (wildcard) {
      for (const handler of wildcard) {
        try {
          handler(event);
        } catch (err) {
          console.error(`[EventBus] Wildcard handler error:`, err);
        }
      }
    }
  }

  removeAll(): void {
    this.handlers.clear();
  }
}

export const memoryEventBus = new EventBus<MemoryEvent>();
export const kgEventBus = new EventBus<KGEvent>();
