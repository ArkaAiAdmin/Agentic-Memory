"""Multi-agent coordination system for agentic-memory.

Provides file locking, agent messaging, task management, project state,
durability (heartbeats/audit), and cross-DB sync primitives.

Submodules:
    locking        — File lock acquire/release/check with auto-expiry and fencing tokens
    messaging      — Inter-agent message queue (send/read/broadcast), dead-letter, subscriptions
    project_state  — Shared key-value state per project
    hooks          — Integration hooks for save pipeline, search, cron, sessions
    durability     — Crash recovery, heartbeats, audit logging, safety reports
"""

from coordination.locking import acquire_lock, acquire_lock_fenced, FencingLock
from coordination.locking import release_lock, renew_lock, verify_lock_fenced
from coordination.locking import check_lock, list_locks, cleanup_expired_locks
from coordination.messaging import (
    send_message, read_messages, broadcast_message,
    get_message_history, get_pending_count, check_messages,
    subscribe, unsubscribe,
    process_dead_letters, get_dead_letters, replay_dead_letter,
    cleanup_dead_letters, cleanup_old_messages,
)
from coordination.project_state import get_state, set_state, delete_state
from coordination.project_state import get_state_keys, get_agent_activity, get_active_files
from coordination.project_state import set_agent_status, get_agent_status
from coordination.durability import (
    update_heartbeat, check_agent_alive, get_safety_report,
    run_durability_maintenance, cleanup_stale_agents, release_stale_locks,
)
from coordination.hooks import (
    acquire_save_lock, verify_save_lock, renew_save_lock,
    release_save_lock, claim_pending_tasks,
    get_coordination_context, create_coordination_task,
    update_project_activity, clear_project_activity,
    queue_lock_conflict_message, send_task_notification,
    create_and_dispatch_task,
)

__all__ = [
    # Locking — fencing
    "acquire_lock", "acquire_lock_fenced", "FencingLock",
    "release_lock", "renew_lock", "verify_lock_fenced",
    "check_lock", "list_locks", "cleanup_expired_locks",
    # Messaging — subscriptions + dead-letter
    "send_message", "read_messages", "broadcast_message",
    "get_message_history", "get_pending_count", "check_messages",
    "subscribe", "unsubscribe",
    "process_dead_letters", "get_dead_letters", "replay_dead_letter",
    "cleanup_dead_letters", "cleanup_old_messages",
    # Project state
    "get_state", "set_state", "delete_state",
    "get_state_keys", "get_agent_activity", "get_active_files",
    "set_agent_status", "get_agent_status",
    # Durability
    "update_heartbeat", "check_agent_alive", "get_safety_report",
    "run_durability_maintenance", "cleanup_stale_agents", "release_stale_locks",
    # Hooks — save pipeline integration
    "acquire_save_lock", "verify_save_lock", "renew_save_lock",
    "release_save_lock", "claim_pending_tasks",
    "get_coordination_context", "create_coordination_task",
    "update_project_activity", "clear_project_activity",
    "queue_lock_conflict_message", "send_task_notification",
    "create_and_dispatch_task",
]
