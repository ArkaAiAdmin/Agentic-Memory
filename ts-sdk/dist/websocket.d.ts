import { MemoryEvent } from './types';
export declare class StreamingClient {
    private wsUrl;
    private token;
    private ws;
    private listeners;
    private isClosedIntentional;
    private reconnectAttempts;
    private maxReconnectAttempts;
    private reconnectInterval;
    constructor(options?: {
        baseUrl?: string;
        token?: string;
    });
    connect(): void;
    private attemptReconnect;
    subscribe(callback: (event: MemoryEvent) => void): () => void;
    close(): void;
}
