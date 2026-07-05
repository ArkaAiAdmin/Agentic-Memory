export interface MemoryOptions {
  tags?: string[];
  category?: string;
  isGlobal?: boolean;
  pinned?: boolean;
}

export interface MemoryResult {
  id: string;
  content: string;
  tags?: string[];
  category?: string;
  created_at?: string;
  updated_at?: string;
  deleted_at?: string;
}

export interface SearchResult {
  id: string;
  content: string;
  score: number;
  tags?: string[];
}

export interface KGEntity {
  id: string;
  name: string;
  type: string;
  properties?: Record<string, any>;
}

export interface KGRelation {
  source: string;
  target: string;
  relation: string;
  weight?: number;
  properties?: Record<string, any>;
}

export interface Stats {
  memories: number;
  vector_keys: number;
  chunks: number;
  facts: number;
  entities: number;
  relations: number;
}

export interface MemoryEvent {
  id: number;
  event_type: 'memory_added' | 'memory_updated' | 'memory_deleted';
  note_id: string;
  payload: {
    id: string;
    content: string;
    tags?: string[];
    category?: string;
    created_at?: string;
    updated_at?: string;
    deleted_at?: string;
  };
  created_at: string;
}
