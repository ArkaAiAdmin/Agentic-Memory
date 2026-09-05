"""UI-state inventory: materialized view over structured agent observations.

Web-agent trajectories emit step-level rows of the form:

    [traj] Step N Dropdown / Menu / Values: a, b, c ...
    [traj] Step N Observation: ...

Lexical retrieval routinely misses these answers because query wording and
observation vocabulary diverge.  This module materializes the dropdown-value
rows — together with the nearest observation as context — into the
``ui_state_inventory`` table (migration 079) so state/option questions can be
answered by lookup.  Build runs at consolidation time; rebuilds are
idempotent keyed on ``source_memory``.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

_DROPDOWN_RE = re.compile(r"\[(\w+)\] Step (\d+) Dropdown / Menu / Values:\s*(.+)")
_OBSERVATION_RE = re.compile(r"\[(\w+)\] Step (\d+) Observation:\s*(.+)")


def build_ui_state_inventory(db, tenant_id: str | None = None) -> int:
    """(Re)build ui_state_inventory from fact-row observations.

    Idempotent: existing ``source_memory`` ids are replaced, not duplicated.
    Returns the number of rows in the view after the build.
    """
    if tenant_id:
        rows = db.execute(
            "SELECT id, content, tenant_id FROM memories "
            "WHERE content LIKE '%Dropdown / Menu / Values:%' AND deleted_at IS NULL AND tenant_id = ?",
            (tenant_id,),
        ).fetchall()
        obs_rows = db.execute(
            "SELECT id, content, tenant_id FROM memories "
            "WHERE content LIKE '%Observation:%' AND deleted_at IS NULL AND tenant_id = ?",
            (tenant_id,),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT id, content, tenant_id FROM memories "
            "WHERE content LIKE '%Dropdown / Menu / Values:%' AND deleted_at IS NULL"
        ).fetchall()
        obs_rows = db.execute(
            "SELECT id, content, tenant_id FROM memories "
            "WHERE content LIKE '%Observation:%' AND deleted_at IS NULL"
        ).fetchall()

    obs_map: dict[tuple[str, int], str] = {}
    for rid, content, _t in obs_rows:
        m = _OBSERVATION_RE.match(content.strip())
        if m:
            obs_map[(m.group(1), int(m.group(2)))] = m.group(3)[:400]

    parsed = []
    for rid, content, row_tenant_id in rows:
        m = _DROPDOWN_RE.match(content.strip())
        if not m:
            continue
        traj, step, vals = m.group(1), int(m.group(2)), m.group(3)
        ctx = obs_map.get((traj, step - 1), "") or obs_map.get((traj, step), "")
        effective_tenant = tenant_id or row_tenant_id or "default"
        parsed.append((traj, step, vals[:2000], ctx, rid, effective_tenant))

    if tenant_id:
        db.execute("DELETE FROM ui_state_inventory WHERE tenant_id = ?", (tenant_id,))
    else:
        db.execute("DELETE FROM ui_state_inventory")

    db.executemany(
        "INSERT INTO ui_state_inventory (traj_id, step, vals, ctx, source_memory, tenant_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        parsed,
    )
    # Repopulate the query-side FTS index (migration 079).
    db.execute("DELETE FROM ui_state_inventory_fts")
    db.execute(
        "INSERT INTO ui_state_inventory_fts (rowid, vals, ctx, traj_id, step) "
        "SELECT id, vals, ctx, traj_id, step FROM ui_state_inventory"
    )

    # Materialize synthesized entries as FIRST-CLASS memory rows. Downstream
    # consumers (eval harness, output builders) resolve content from the
    # memories table by id — side-channel rows are invisible to them. As real
    # rows they are FTS-indexed like any other document.
    #
    # Pollution guard: raw dropdown dumps are dominated by generic UI chrome
    # ("Search, Choose search context" x900+) that floods FTS as distractors.
    # Materialize ONE document per UNIQUE vals string and drop boilerplate —
    # vals appearing in many trajectories are chrome, not environment
    # knowledge.
    _BOILERPLATE_MAX_OCCURRENCES = 50
    from collections import Counter

    _vals_freq = Counter(p[2].strip().lower() for p in parsed)
    seen_vals: set[str] = set()
    docs = []
    for (traj, step, vals, ctx, src_rid, doc_tenant) in parsed:
        key = vals.strip().lower()
        if key in seen_vals or _vals_freq[key] > _BOILERPLATE_MAX_OCCURRENCES:
            continue
        seen_vals.add(key)
        docs.append(
            (
                f"uistate_{traj}_{step}",
                f"UI State [{traj} step {step}] values: {vals} | context: {ctx}",
                src_rid,
            )
        )

    # Resync FTS BEFORE inserting: databases rebuilt by later migrations
    # (e.g. 078) can carry stale FTS entries whose rowids collide with
    # newly auto-assigned memories.rowids; such an insert fails with a bare
    # "constraint failed" inside the memories_ai trigger. A contentful-FTS5
    # 'rebuild' replays from the shadow content table and PRESERVES stale
    # rows, so delete orphans directly.
    db.execute(
        "DELETE FROM memories_fts WHERE rowid NOT IN (SELECT rowid FROM memories)"
    )

    if tenant_id:
        db.execute("DELETE FROM memories WHERE id LIKE 'uistate_%' AND tenant_id = ?", (tenant_id,))
    else:
        db.execute("DELETE FROM memories WHERE id LIKE 'uistate_%'")

    db.executemany(
        "INSERT INTO memories (id, content, source_file, tags, created_at, updated_at, "
        "observed_at, pinned, importance, category, tenant_id) "
        "VALUES (?, ?, 'ui_state/inventory', '[]', datetime('now'), datetime('now'), "
        "        datetime('now'), 0, 3, 'sessions', (SELECT tenant_id FROM memories WHERE id = ?))",
        docs,
    )
    # Triggers (memories_ai) maintain FTS for the new rows; no post-rebuild
    # needed.
    db.commit()
    logger.info(
        "ui_state_inventory built: %d entries (+%d synthesized memory rows)",
        len(parsed), len(parsed),
    )
    return len(parsed)


def lookup_ui_state(
    db,
    query_tokens: list[str],
    limit: int = 5,
    idf: dict[str, float] | None = None,
    tenant_id: str | None = None,
) -> list[dict]:
    """Route a query to the most relevant inventory entries.

    Two-stage: FTS5 narrows 45k rows to candidates on distinctive tokens,
    then IDF-weighted overlap over (vals + ctx) ranks them.  ``idf`` maps
    token -> weight; when omitted, plain overlap counts are used.

    Returns list of dicts: {traj_id, step, vals, ctx, score}.
    """
    if not query_tokens:
        return []
    _idf = idf or {}
    distinctive = sorted(set(query_tokens), key=lambda t: -_idf.get(t, 1.0))[:8]
    if not distinctive:
        return []
    match_q = " OR ".join(f'"{t}"' for t in distinctive)
    try:
        if tenant_id:
            cand_rows = db.execute(
                """
                SELECT i.traj_id, i.step, i.vals, i.ctx
                FROM ui_state_inventory_fts f
                JOIN ui_state_inventory i ON i.id = f.rowid
                WHERE ui_state_inventory_fts MATCH ? AND i.tenant_id = ?
                LIMIT 800
                """,
                (match_q, tenant_id),
            ).fetchall()
        else:
            cand_rows = db.execute(
                """
                SELECT i.traj_id, i.step, i.vals, i.ctx
                FROM ui_state_inventory_fts f
                JOIN ui_state_inventory i ON i.id = f.rowid
                WHERE ui_state_inventory_fts MATCH ?
                LIMIT 800
                """,
                (match_q,),
            ).fetchall()
    except sqlite3.OperationalError as exc:
        logger.debug("ui_state FTS unavailable: %s", exc)
        return []
    if not cand_rows:
        return []

    qset = set(query_tokens)
    scored = []
    for traj, step, vals, ctx in cand_rows:
        etoks = set(re.findall(r"[a-z]{4,}", (vals + " " + ctx).lower()))
        if _idf:
            score = sum(_idf.get(t, 0.0) for t in qset & etoks)
        else:
            score = float(len(qset & etoks))
        if score > 0:
            scored.append({"traj_id": traj, "step": step, "vals": vals, "ctx": ctx, "score": score})
    scored.sort(key=lambda x: -x["score"])
    return scored[:limit]


def compute_inventory_idf(db, tenant_id: str | None = None) -> dict[str, float]:
    """Precompute IDF weights over inventory entry tokens."""
    import math
    from collections import Counter

    df: Counter = Counter()
    n = 0
    if tenant_id:
        query = "SELECT vals, ctx FROM ui_state_inventory WHERE tenant_id = ?"
        params = (tenant_id,)
    else:
        query = "SELECT vals, ctx FROM ui_state_inventory"
        params = ()
    for (vals, ctx) in db.execute(query, params):
        df.update(set(re.findall(r"[a-z]{4,}", (vals + " " + ctx).lower())))
        n += 1
    if n == 0:
        return {}
    return {t: math.log(n / (1 + c)) for t, c in df.items()}


def ensure_view(db_path: Path) -> None:
    """Build the inventory for ``db_path`` if not yet populated (best-effort)."""
    try:
        from infra.db import open_db
        with open_db(db_path, timeout=30.0) as db:
            row = db.execute("SELECT COUNT(*) FROM ui_state_inventory").fetchone()
            if row and row[0] == 0:
                build_ui_state_inventory(db)
    except Exception as exc:
        logger.warning("ensure_view failed: %s", exc)
