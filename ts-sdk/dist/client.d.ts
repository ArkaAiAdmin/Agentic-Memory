import { MemoryResult, SearchResult, MemoryOptions, SearchOptions, KGEntity, KGRelation, Stats } from './types';
export declare class MemoryClient {
    private baseUrl;
    private token;
    constructor(options?: {
        baseUrl?: string;
        token?: string;
    });
    private request;
    add(content: string, options?: MemoryOptions): Promise<string>;
    search(query: string, options?: SearchOptions): Promise<SearchResult[]>;
    get(noteId: string): Promise<MemoryResult>;
    delete(noteId: string): Promise<boolean>;
    list(options?: {
        limit?: number;
        offset?: number;
    }): Promise<MemoryResult[]>;
    clear(): Promise<number>;
    stats(): Promise<Stats>;
    readonly kg: {
        getNodes: (options?: {
            limit?: number;
        }) => Promise<KGEntity[]>;
        getEdges: (options?: {
            limit?: number;
        }) => Promise<KGRelation[]>;
    };
    readonly maintenance: {
        rebuild: () => Promise<boolean>;
        compact: () => Promise<boolean>;
        integrity: () => Promise<{
            success: boolean;
            errors: string[];
        }>;
    };
}
