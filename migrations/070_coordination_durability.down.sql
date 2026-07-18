-- Migration 070 down: Remove coordination durability tables

DROP TABLE IF EXISTS agent_heartbeats;
DROP TABLE IF EXISTS coordination_audit;
