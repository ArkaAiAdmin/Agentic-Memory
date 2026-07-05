import { MemoryResult, SearchResult, MemoryOptions, KGEntity, KGRelation, Stats } from './types';

export class MemoryClient {
  private baseUrl: string;
  private token: string;

  constructor(options: { baseUrl?: string; token?: string } = {}) {
    this.baseUrl = (options.baseUrl || 'http://127.0.0.1:9878').replace(/\/$/, '');
    this.token = options.token || '';
  }

  private async request<T>(path: string, method: string = 'GET', body?: any): Promise<T> {
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
    };
    if (this.token) {
      headers['Authorization'] = `Bearer ${this.token}`;
    }

    const response = await fetch(`${this.baseUrl}${path}`, {
      method,
      headers,
      body: body ? JSON.stringify(body) : undefined,
    });

    if (!response.ok) {
      const errText = await response.text();
      let errMsg = response.statusText;
      try {
        const parsed = JSON.parse(errText);
        errMsg = parsed.error || errMsg;
      } catch {}
      throw new Error(`HTTP ${response.status}: ${errMsg}`);
    }

    return response.json() as Promise<T>;
  }

  async add(content: string, options: MemoryOptions = {}): Promise<string> {
    const res = await this.request<{ id: string }>('/api/v1/memories', 'POST', {
      content,
      tags: options.tags || [],
      category: options.category || 'sdk',
      is_global: options.isGlobal !== false,
      pinned: !!options.pinned,
    });
    return res.id;
  }

  async search(query: string, options: { limit?: number; rerank?: boolean } = {}): Promise<SearchResult[]> {
    const res = await this.request<{ results: SearchResult[] }>('/api/v1/memories/search', 'POST', {
      query,
      limit: options.limit || 10,
      rerank: options.rerank !== false,
    });
    return res.results;
  }

  async get(noteId: string): Promise<MemoryResult> {
    return this.request<MemoryResult>(`/api/v1/memories/${noteId}`, 'GET');
  }

  async delete(noteId: string): Promise<boolean> {
    const res = await this.request<{ success: boolean }>(`/api/v1/memories/${noteId}`, 'DELETE');
    return res.success;
  }

  async list(options: { limit?: number; offset?: number } = {}): Promise<MemoryResult[]> {
    const params = new URLSearchParams();
    if (options.limit) params.append('limit', options.limit.toString());
    if (options.offset) params.append('offset', options.offset.toString());
    const queryStr = params.toString() ? `?${params.toString()}` : '';
    const data = await this.request<{ memories: MemoryResult[] }>(`/api/v1/memories${queryStr}`, 'GET');
    return data.memories;
  }

  async clear(): Promise<number> {
    const res = await this.request<{ cleared: number }>('/api/v1/memories/clear', 'POST');
    return res.cleared;
  }

  async stats(): Promise<Stats> {
    return this.request<Stats>('/api/v1/memories/stats', 'GET');
  }

  // Knowledge Graph Sub-Client
  readonly kg = {
    getNodes: async (options: { limit?: number } = {}): Promise<KGEntity[]> => {
      const params = options.limit ? `?limit=${options.limit}` : '';
      const res = await this.request<{ nodes: KGEntity[] }>(`/api/v1/kg/nodes${params}`, 'GET');
      return res.nodes;
    },
    getEdges: async (options: { limit?: number } = {}): Promise<KGRelation[]> => {
      const params = options.limit ? `?limit=${options.limit}` : '';
      const res = await this.request<{ edges: KGRelation[] }>(`/api/v1/kg/edges${params}`, 'GET');
      return res.edges;
    }
  };

  // Maintenance Sub-Client
  readonly maintenance = {
    rebuild: async (): Promise<boolean> => {
      const res = await this.request<{ success: boolean }>('/api/v1/maintenance/rebuild', 'POST');
      return res.success;
    },
    compact: async (): Promise<boolean> => {
      const res = await this.request<{ success: boolean }>('/api/v1/maintenance/compact', 'POST');
      return res.success;
    },
    integrity: async (): Promise<{ success: boolean; errors: string[] }> => {
      return this.request<{ success: boolean; errors: string[] }>('/api/v1/maintenance/integrity', 'POST');
    }
  };
}
