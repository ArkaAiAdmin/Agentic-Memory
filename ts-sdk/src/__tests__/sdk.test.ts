import { describe, it, expect, beforeEach, vi } from 'vitest'
import { MemoryClient } from '../client';
import { AgentMemory } from '../agent';
import { StreamingClient } from '../websocket';

describe('MemoryClient', () => {
  let client: MemoryClient;

  beforeEach(() => {
    client = new MemoryClient({ baseUrl: 'http://127.0.0.1:9878', token: 'test-token' });
    global.fetch = vi.fn() as any;
  });

  it('should call fetch with correct arguments on add()', async () => {
    const mockResponse = { id: 'sdk/test-123' };
    (global.fetch as any).mockResolvedValue({
      ok: true,
      json: async () => mockResponse,
    });

    const noteId = await client.add('User prefers spaces', { tags: ['pref'], category: 'preferences' });
    expect(noteId).toBe('sdk/test-123');
    expect(global.fetch).toHaveBeenCalledWith('http://127.0.0.1:9878/api/v1/memories', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer test-token',
      },
      body: JSON.stringify({
        content: 'User prefers spaces',
        tags: ['pref'],
        category: 'preferences',
        is_global: true,
        pinned: false,
      }),
    });
  });

  it('should call fetch on search()', async () => {
    const mockResponse = {
      results: [
        { id: '123', content: 'test content', score: 0.9, tags: ['test'] }
      ]
    };
    (global.fetch as any).mockResolvedValue({
      ok: true,
      json: async () => mockResponse,
    });

    const results = await client.search('query', { limit: 5, rerank: true });
    expect(results).toHaveLength(1);
    expect(results[0].content).toBe('test content');
  });

  it('should call fetch on delete()', async () => {
    (global.fetch as any).mockResolvedValue({
      ok: true,
      json: async () => ({ success: true }),
    });

    const success = await client.delete('sdk/test-123');
    expect(success).toBe(true);
  });

  it('should support sub-clients: kg and maintenance', async () => {
    (global.fetch as any).mockImplementation((url: string) => {
      if (url.includes('/api/v1/kg/nodes')) {
        return Promise.resolve({
          ok: true,
          json: async () => ({ nodes: [{ id: 'n1', name: 'entity1', type: 'person' }] }),
        });
      }
      if (url.includes('/api/v1/maintenance/rebuild')) {
        return Promise.resolve({
          ok: true,
          json: async () => ({ success: true }),
        });
      }
      return Promise.reject(new Error('Unknown url'));
    });

    const nodes = await client.kg.getNodes({ limit: 5 });
    expect(nodes).toHaveLength(1);
    expect(nodes[0].name).toBe('entity1');

    const success = await client.maintenance.rebuild();
    expect(success).toBe(true);
  });
});

describe('AgentMemory namespace isolation', () => {
  let agentMemory: AgentMemory;

  beforeEach(() => {
    agentMemory = new AgentMemory({ agentId: 'alpha-agent', baseUrl: 'http://127.0.0.1:9878', token: 'test' });
    global.fetch = vi.fn() as any;
  });

  it('should force agent namespace tag on save', async () => {
    (global.fetch as any).mockResolvedValue({
      ok: true,
      json: async () => ({ id: 'agents/test-note-1' }),
    });

    const noteId = await agentMemory.save('Project guidelines');
    expect(noteId).toBe('agents/test-note-1');
    const fetchArgs = (global.fetch as any).mock.calls[0];
    const body = JSON.parse(fetchArgs[1].body);
    expect(body.tags).toContain('agent-alpha-agent');
    expect(body.category).toBe('agents');
    expect(body.is_global).toBe(false);
  });

  it('should pass agent tag to server for namespace scoping', async () => {
    const mockResponse = {
      results: [
        { id: '1', content: 'Agent specific info', tags: ['agent-alpha-agent'] },
      ]
    };
    (global.fetch as any).mockResolvedValue({
      ok: true,
      json: async () => mockResponse,
    });

    const results = await agentMemory.search('info');
    expect(results).toHaveLength(1);
    expect(results[0].content).toBe('Agent specific info');

    const fetchArgs = (global.fetch as any).mock.calls[0];
    const body = JSON.parse(fetchArgs[1].body);
    expect(body.tags).toEqual(['agent-alpha-agent']);
  });
});

describe('StreamingClient WebSocket', () => {
  let client: StreamingClient;
  let mockWS: any;

  beforeEach(() => {
    mockWS = {
      onopen: null,
      onmessage: null,
      onclose: null,
      onerror: null,
      close: vi.fn(),
    };
    (global as any).WebSocket = vi.fn().mockImplementation(function () { return mockWS; });
    client = new StreamingClient({ baseUrl: 'http://127.0.0.1:9878', token: 'test-token' });
  });

  it('should connect and receive events', () => {
    client.connect();
    expect(global.WebSocket).toHaveBeenCalled();

    const mockCallback = vi.fn();
    const unsubscribe = client.subscribe(mockCallback);

    if (mockWS.onopen) mockWS.onopen();

    const eventPayload = {
      event: 'memory_event',
      data: {
        id: 1,
        event_type: 'memory_added',
        note_id: '123',
        payload: { content: 'test' },
        created_at: '2026-07-05T12:00:00Z',
      },
    };
    if (mockWS.onmessage) {
      mockWS.onmessage({ data: JSON.stringify(eventPayload) });
    }

    expect(mockCallback).toHaveBeenCalledWith(eventPayload.data);

    unsubscribe();
    client.close();
    expect(mockWS.close).toHaveBeenCalled();
  });
});
