ALTER TABLE memory_skills ADD COLUMN hit_vector TEXT DEFAULT '{}';
ALTER TABLE memory_skills ADD COLUMN last_used_vector TEXT DEFAULT '{}';
ALTER TABLE memory_skills ADD COLUMN logical_clock INTEGER DEFAULT 0;
