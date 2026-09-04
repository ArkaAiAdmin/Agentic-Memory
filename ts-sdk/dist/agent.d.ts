import { SearchResult, MemoryOptions } from './types';
export declare class AgentMemory {
    private client;
    private agentId;
    constructor(options: {
        agentId: string;
        baseUrl?: string;
        token?: string;
    });
    save(content: string, options?: MemoryOptions): Promise<string>;
    search(query: string, options?: {
        limit?: number;
    }): Promise<SearchResult[]>;
}
