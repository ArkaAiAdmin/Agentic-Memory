import { MemoryClient } from './client';
import { SearchResult, MemoryOptions } from './types';

export class AgentMemory {
  private client: MemoryClient;
  private agentId: string;

  constructor(options: { agentId: string; baseUrl?: string; token?: string }) {
    if (!options.agentId) {
      throw new Error('agentId is required');
    }
    this.agentId = options.agentId;
    this.client = new MemoryClient({ baseUrl: options.baseUrl, token: options.token });
  }

  async save(content: string, options: MemoryOptions = {}): Promise<string> {
    const tags = options.tags || [];
    if (!tags.includes(`agent-${this.agentId}`)) {
      tags.push(`agent-${this.agentId}`);
    }
    return this.client.add(content, {
      ...options,
      tags,
      category: options.category || 'agents',
      isGlobal: options.isGlobal ?? false,
    });
  }

  async search(query: string, options: { limit?: number } = {}): Promise<SearchResult[]> {
    const results = await this.client.search(query, {
      limit: options.limit || 10,
    });
    return results.filter(r => r.tags?.includes(`agent-${this.agentId}`));
  }
}
