"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.AgentMemory = void 0;
const client_1 = require("./client");
class AgentMemory {
    client;
    agentId;
    constructor(options) {
        if (!options.agentId) {
            throw new Error('agentId is required');
        }
        this.agentId = options.agentId;
        this.client = new client_1.MemoryClient({ baseUrl: options.baseUrl, token: options.token });
    }
    async save(content, options = {}) {
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
    async search(query, options = {}) {
        return this.client.search(query, {
            limit: options.limit || 10,
            tags: [`agent-${this.agentId}`],
        });
    }
}
exports.AgentMemory = AgentMemory;
