"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.MemoryClient = void 0;
class MemoryClient {
    baseUrl;
    token;
    constructor(options = {}) {
        this.baseUrl = (options.baseUrl || 'http://127.0.0.1:9879').replace(/\/$/, '');
        this.token = options.token || '';
    }
    async request(path, method = 'GET', body) {
        const headers = {
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
            }
            catch { }
            throw new Error(`HTTP ${response.status}: ${errMsg}`);
        }
        return response.json();
    }
    async add(content, options = {}) {
        const res = await this.request('/api/v1/memories', 'POST', {
            content,
            tags: options.tags || [],
            category: options.category || 'sdk',
            is_global: options.isGlobal !== false,
            pinned: !!options.pinned,
        });
        return res.id;
    }
    async search(query, options = {}) {
        const res = await this.request('/api/v1/memories/search', 'POST', {
            query,
            limit: options.limit || 10,
            rerank: options.rerank !== false,
            tags: options.tags,
        });
        return res.results;
    }
    async get(noteId) {
        return this.request(`/api/v1/memories/${noteId}`, 'GET');
    }
    async delete(noteId) {
        const res = await this.request(`/api/v1/memories/${noteId}`, 'DELETE');
        return res.success;
    }
    async list(options = {}) {
        const params = new URLSearchParams();
        if (options.limit)
            params.append('limit', options.limit.toString());
        if (options.offset)
            params.append('offset', options.offset.toString());
        const queryStr = params.toString() ? `?${params.toString()}` : '';
        const data = await this.request(`/api/v1/memories${queryStr}`, 'GET');
        return data.memories;
    }
    async clear() {
        const res = await this.request('/api/v1/memories/clear', 'POST');
        return res.cleared;
    }
    async stats() {
        return this.request('/api/v1/memories/stats', 'GET');
    }
    // Knowledge Graph Sub-Client
    kg = {
        getNodes: async (options = {}) => {
            const params = options.limit ? `?limit=${options.limit}` : '';
            const res = await this.request(`/api/v1/kg/nodes${params}`, 'GET');
            return res.nodes;
        },
        getEdges: async (options = {}) => {
            const params = options.limit ? `?limit=${options.limit}` : '';
            const res = await this.request(`/api/v1/kg/edges${params}`, 'GET');
            return res.edges;
        }
    };
    // Maintenance Sub-Client
    maintenance = {
        rebuild: async () => {
            const res = await this.request('/api/v1/maintenance/rebuild', 'POST');
            return res.success;
        },
        compact: async () => {
            const res = await this.request('/api/v1/maintenance/compact', 'POST');
            return res.success;
        },
        integrity: async () => {
            return this.request('/api/v1/maintenance/integrity', 'POST');
        }
    };
}
exports.MemoryClient = MemoryClient;
