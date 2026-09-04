"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.StreamingClient = void 0;
class StreamingClient {
    wsUrl;
    token;
    ws = null;
    listeners = new Set();
    isClosedIntentional = false;
    reconnectAttempts = 0;
    maxReconnectAttempts = 10;
    reconnectInterval = 1000;
    constructor(options = {}) {
        const base = options.baseUrl || 'http://127.0.0.1:9879';
        this.wsUrl = base.replace(/^http/, 'ws').replace(/\/$/, '') + '/ws';
        this.token = options.token || '';
    }
    connect() {
        this.isClosedIntentional = false;
        // Resolve WebSocket constructor (Browser + Node support)
        let WSConstructor;
        if (typeof WebSocket !== 'undefined') {
            WSConstructor = WebSocket;
        }
        else {
            try {
                WSConstructor = require('ws');
            }
            catch {
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
        this.ws.onmessage = (event) => {
            try {
                const rawData = typeof event.data === 'string' ? event.data : event.data.toString();
                const msg = JSON.parse(rawData);
                if (msg.event === 'memory_event' && msg.data) {
                    const ev = msg.data;
                    for (const listener of this.listeners) {
                        listener(ev);
                    }
                }
            }
            catch (err) {
                logger.warning('Failed to parse WebSocket event payload', err);
            }
        };
        this.ws.onclose = () => {
            logger.info('WebSocket connection closed');
            if (!this.isClosedIntentional) {
                this.attemptReconnect();
            }
        };
        this.ws.onerror = (err) => {
            logger.warning('WebSocket streaming error', err);
        };
    }
    attemptReconnect() {
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
    subscribe(callback) {
        this.listeners.add(callback);
        return () => {
            this.listeners.delete(callback);
        };
    }
    close() {
        this.isClosedIntentional = true;
        if (this.ws) {
            this.ws.close();
            this.ws = null;
        }
    }
}
exports.StreamingClient = StreamingClient;
const logger = {
    info: (msg) => console.log(`[AgenticMemory SDK] INFO: ${msg}`),
    warning: (msg, ...args) => console.warn(`[AgenticMemory SDK] WARN: ${msg}`, ...args)
};
