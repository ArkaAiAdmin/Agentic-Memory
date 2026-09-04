import { MemoryEvent } from './types';

export class StreamingClient {
  private wsUrl: string;
  private token: string;
  private ws: any = null;
  private listeners: Set<(event: MemoryEvent) => void> = new Set();
  private isClosedIntentional: boolean = false;
  private reconnectAttempts: number = 0;
  private maxReconnectAttempts: number = 10;
  private reconnectInterval: number = 1000;

  constructor(options: { baseUrl?: string; token?: string } = {}) {
    const base = options.baseUrl || 'http://127.0.0.1:9879';
    this.wsUrl = base.replace(/^http/, 'ws').replace(/\/$/, '') + '/ws';
    this.token = options.token || '';
  }

  connect(): void {
    this.isClosedIntentional = false;
    
    // Resolve WebSocket constructor (Browser + Node support)
    let WSConstructor: any;
    if (typeof WebSocket !== 'undefined') {
      WSConstructor = WebSocket;
    } else {
      try {
        WSConstructor = require('ws');
      } catch {
        throw new Error('WebSocket implementation missing. Install "ws" package for Node environment.');
      }
    }

    const url = this.token ? `${this.wsUrl}?token=${encodeURIComponent(this.token)}` : this.wsUrl;
    const connectionOptions = typeof window === 'undefined' && this.token ? {
      headers: { 'Authorization': `Bearer ${this.token}` }
    } : undefined;

    this.ws = new WSConstructor(url, undefined, connectionOptions);

    this.ws.onopen = () => {
      this.reconnectAttempts = 0;
      this.reconnectInterval = 1000;
      logger.info('WebSocket streaming connected');
    };

    this.ws.onmessage = (event: any) => {
      try {
        const rawData = typeof event.data === 'string' ? event.data : event.data.toString();
        const msg = JSON.parse(rawData);
        if (msg.event === 'memory_event' && msg.data) {
          const ev: MemoryEvent = msg.data;
          for (const listener of this.listeners) {
            listener(ev);
          }
        }
      } catch (err) {
        logger.warning('Failed to parse WebSocket event payload', err);
      }
    };

    this.ws.onclose = () => {
      logger.info('WebSocket connection closed');
      if (!this.isClosedIntentional) {
        this.attemptReconnect();
      }
    };

    this.ws.onerror = (err: any) => {
      logger.warning('WebSocket streaming error', err);
    };
  }

  private attemptReconnect(): void {
    if (this.reconnectAttempts >= this.maxReconnectAttempts) {
      logger.warning('Max WebSocket reconnect attempts reached');
      return;
    }

    this.reconnectAttempts++;
    const delay = this.reconnectInterval * Math.pow(2, this.reconnectAttempts - 1);
    logger.info(`Reconnecting in ${delay}ms (attempt ${this.reconnectAttempts})...`);
    
    setTimeout(() => {
      this.connect();
    }, delay);
  }

  subscribe(callback: (event: MemoryEvent) => void): () => void {
    this.listeners.add(callback);
    return () => {
      this.listeners.delete(callback);
    };
  }

  close(): void {
    this.isClosedIntentional = true;
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
  }
}

const logger = {
  info: (msg: string) => console.log(`[AgenticMemory SDK] INFO: ${msg}`),
  warning: (msg: string, ...args: any[]) => console.warn(`[AgenticMemory SDK] WARN: ${msg}`, ...args)
};
