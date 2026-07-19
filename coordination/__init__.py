"""Multi-agent coordination system for agentic-memory.

Provides file locking, agent messaging, task management, project state,
durability (heartbeats/audit), and cross-DB sync primitives.

Submodules:
    locking        — File lock acquire/release/check with auto-expiry
    messaging      — Inter-agent message queue (send/read/broadcast)
    project_state  — Shared key-value state per project
    hooks          — Integration hooks for save pipeline, search, cron, sessions
    durability     — Crash recovery, heartbeats, audit logging, safety reports
"""

from coordination.locking import acquire_lock, release_lock, check_lock, list_locks
from coordination.messaging import send_message, read_messages, broadcast_message
from coordination.project_state import get_state, set_state, delete_state
from coordination.durability import update_heartbeat, check_agent_alive, get_safety_report
from coordination.hooks import (
    acquire_save_lock,
    release_save_lock,
    claim_pending_tasks,
    get_coordination_context,
    create_coordination_task,
    update_project_activity,
    clear_project_activity,
)

__all__ = [
    "acquire_lock",
    "release_lock",
    "check_lock",
    "list_locks",
    "send_message",
    "read_messages",
    "broadcast_message",
    "get_state",
    "set_state",
    "delete_state",
    "update_heartbeat",
    "check_agent_alive",
    "get_safety_report",
    "acquire_save_lock",
    "release_save_lock",
    "claim_pending_tasks",
    "get_coordination_context",
    "create_coordination_task",
    "update_project_activity",
    "clear_project_activity",
]
