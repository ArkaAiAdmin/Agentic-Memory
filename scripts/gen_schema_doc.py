#!/usr/bin/env python3
"""Generate docs/reference/schema.md from migration SQL files.

Usage:
    python scripts/gen_schema_doc.py          # write in-place
    python scripts/gen_schema_doc.py --check   # exit 1 on mismatch
    python scripts/gen_schema_doc.py --stdout  # print to stdout
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

from _docgen_markers import assemble, extract_manual  # noqa: E402


# ---------------------------------------------------------------------------
# SQL parsing helpers
# ---------------------------------------------------------------------------


def _parse_create_table(sql: str) -> dict | None:
    """Extract table name and columns from a CREATE TABLE statement."""
    m = re.search(
        r"CREATE\s+(?:VIRTUAL\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:`?\w+`?\.)?(?:`(\w+)`|(\w+))\s*\(",
        sql,
        re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return None
    table_name = m.group(1) or m.group(2)
    # Extract body between outer parentheses
    start = m.end()
    depth = 1
    i = start
    while i < len(sql) and depth > 0:
        if sql[i] == "(":
            depth += 1
        elif sql[i] == ")":
            depth -= 1
        i += 1
    body = sql[start : i - 1]

    columns: list[dict[str, Any]] = []
    indices: list[dict[str, Any]] = []
    constraints: list[str] = []
    is_fts = "VIRTUAL TABLE" in sql and "fts5" in sql

    for line in body.split(","):
        line = line.strip()
        if not line:
            continue
        # Index definition
        idx_m = re.match(
            r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)",
            line,
            re.IGNORECASE,
        )
        if idx_m:
            indices.append({"name": idx_m.group(1), "definition": line[:80]})
            continue
        # Column definition: name type [constraints]
        col_m = re.match(
            r"(?:`(\w+)`|(\w+))\s+("
            r"INTEGER|TEXT|REAL|BLOB|NUMERIC|BOOL|BOOLEAN|FLOAT|DOUBLE|"
            r"INT\b|BIGINT|SMALLINT|TINYINT|VARCHAR\s*\([^)]*\)|CHAR\s*\([^)]*\)"
            r")(.*)",
            line,
            re.IGNORECASE,
        )
        if col_m:
            col_name = col_m.group(1) or col_m.group(2)
            col_type = col_m.group(3).strip()
            col_rest = col_m.group(4).strip() if (col_m.lastindex or 0) >= 4 else ""
            col: dict[str, Any] = {"name": col_name, "type": col_type}
            if "NOT NULL" in col_rest.upper():
                col["not_null"] = True
            if "PRIMARY KEY" in col_rest.upper():
                col["pk"] = True
            if "DEFAULT" in col_rest.upper():
                d = re.search(r"DEFAULT\s+(\S+)", col_rest, re.IGNORECASE)
                if d:
                    col["default"] = d.group(1)
            if "UNIQUE" in col_rest.upper():
                col["unique"] = True
            if "CHECK" in col_rest.upper():
                constraints.append(f"{col_name} CHECK{col_rest[col_rest.upper().find('CHECK'):]}")
            if "REFERENCES" in col_rest.upper():
                fk = re.search(r"REFERENCES\s+(\w+)\s*\((\w+)\)", col_rest, re.IGNORECASE)
                if fk:
                    col["fk"] = f"{fk.group(1)}({fk.group(2)})"
            columns.append(col)
            continue
        # Inline CHECK constraint
        if re.match(r"CHECK\s*\(", line, re.IGNORECASE):
            constraints.append(line)
            continue
        # Inline FOREIGN KEY
        if re.match(r"FOREIGN\s+KEY\s*\(", line, re.IGNORECASE):
            constraints.append(line)
            continue
        # Inline PRIMARY KEY (composite)
        if re.match(r"PRIMARY\s+KEY\s*\(", line, re.IGNORECASE):
            constraints.append(line)
            continue
        # Inline UNIQUE (composite)
        if re.match(r"UNIQUE\s*\(", line, re.IGNORECASE):
            constraints.append(line)
            continue

    result: dict[str, Any] = {
        "name": table_name,
        "columns": columns,
        "constraints": constraints,
        "is_fts": is_fts,
    }
    if indices:
        result["indices"] = indices
    return result


def _parse_alter_table(sql: str) -> dict | None:
    """Extract ALTER TABLE ... ADD COLUMN statements."""
    m = re.search(
        r"ALTER\s+TABLE\s+(?:`?(\w+)`?|(\w+))\s+ADD\s+(?:COLUMN\s+)?(.+)",
        sql,
        re.IGNORECASE,
    )
    if not m:
        return None
    table_name = m.group(1) or m.group(2)
    col_def = m.group(3).strip()
    col_m = re.match(
        r"(?:`(\w+)`|(\w+))\s+("
        r"INTEGER|TEXT|REAL|BLOB|NUMERIC|BOOL|BOOLEAN|FLOAT|DOUBLE|"
        r"INT\b|BIGINT|SMALLINT|TINYINT|VARCHAR\s*\([^)]*\)|CHAR\s*\([^)]*\)"
        r")",
        col_def,
        re.IGNORECASE,
    )
    if col_m:
        col_name = col_m.group(1) or col_m.group(2)
        col_type = col_m.group(3).strip()
        return {"table": table_name, "column": col_name, "type": col_type, "full": col_def}
    return {"table": table_name, "full": col_def}


def _parse_file(sql: str) -> dict[str, Any]:
    """Parse a SQL migration file.

    Returns {description, creates: [{table, cols, ...}], alters: [{...}], standalone_indices: [{...}]}.
    """
    desc_match = re.search(r"--\s*(?:Migration\s+\d+[:\s]+)?(.+)", sql)
    description = desc_match.group(1).strip() if desc_match else ""

    creates: list[dict] = []
    alters: list[dict] = []
    standalone_indices: list[dict] = []

    # Split top-level statements
    statements = re.split(r";\s*(?:--[^\n]*\n)*", sql)

    for stmt in statements:
        stmt = stmt.strip()
        if not stmt:
            continue
        # CREATE TABLE
        ct = _parse_create_table(stmt)
        if ct:
            creates.append(ct)
            continue
        # ALTER TABLE
        at = _parse_alter_table(stmt)
        if at:
            alters.append(at)
            continue
        # Standalone CREATE INDEX (not inside a CREATE TABLE)
        if re.match(r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+", stmt, re.IGNORECASE):
            idx_m = re.search(
                r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)",
                stmt,
                re.IGNORECASE,
            )
            if idx_m:
                standalone_indices.append({"name": idx_m.group(1), "definition": stmt[:80]})

    return {
        "description": description,
        "creates": creates,
        "alters": alters,
        "indices": standalone_indices,
    }


# ---------------------------------------------------------------------------
# Markdown generation
# ---------------------------------------------------------------------------


def _table_header(doc_meta: dict) -> str:
    version = doc_meta.get("schema_version", 37)
    migrations = doc_meta.get("num_migrations", 38)
    tables = doc_meta.get("num_tables_visible", 49)
    return (
        f"# Database Schema\n\n"
        f"Agentic Memory uses SQLite with FTS5 for full-text search. "
        f"Schema version **{version}** (defined in `migration_runner.py`; "
        f"{migrations} migrations, ~{tables} user-visible tables; "
        f"~62 total including FTS5 virtual tables).\n\n"
        f"*This file is generated by `scripts/gen_schema_doc.py`. "
        f"Run it to sync from live code.*\n"
    )


def _create_table_sql(table: dict) -> str:
    lines = [f"CREATE TABLE {table['name']} ("]
    col_lines = []
    for col in table["columns"]:
        parts = [f"    {col['name']}"]
        col_type = col["type"]
        # Clean up type
        col_type = re.sub(r"\s+", " ", col_type).strip()
        parts.append(col_type)
        if col.get("pk"):
            parts.append("PRIMARY KEY")
        if col.get("not_null"):
            parts.append("NOT NULL")
        if col.get("unique"):
            parts.append("UNIQUE")
        if col.get("default") is not None:
            parts.append(f"DEFAULT {col['default']}")
        if col.get("fk"):
            parts.append(f"REFERENCES {col['fk']}")
        col_lines.append(" ".join(parts))
    for c in table.get("constraints", []):
        col_lines.append(f"    {c}")
    lines.append(",\n".join(col_lines))
    lines.append(");")
    return "\n".join(lines)


def _render_columns_table(columns: list[dict]) -> str:
    if not columns:
        return ""
    rows = ["| Column | Type | Constraints |", "|--------|------|-------------|"]
    for col in columns:
        constraints = []
        if col.get("pk"):
            constraints.append("PK")
        if col.get("not_null"):
            constraints.append("NOT NULL")
        if col.get("unique"):
            constraints.append("UNIQUE")
        if col.get("default") is not None:
            constraints.append(f"DEFAULT {col['default']}")
        if col.get("fk"):
            constraints.append(f"FK→{col['fk']}")
        con_str = ", ".join(constraints) if constraints else ""
        # Clean up type
        col_type = re.sub(r"\s+", " ", col["type"]).strip()
        rows.append(f"| `{col['name']}` | {col_type} | {con_str} |")
    return "\n".join(rows)


def _render_migration_entry(num: int, filename: str, parsed: dict) -> str:
    lines = [f"### Migration {num}: `{filename}`"]
    if parsed["description"]:
        lines.append("")
        lines.append(parsed["description"])
    lines.append("")

    for table in parsed["creates"]:
        lines.append("")
        lines.append(f"#### `{table['name']}`")
        if table["is_fts"]:
            lines.append("")
            lines.append("FTS5 virtual table for full-text search.")
        lines.append("")
        lines.append("```sql")
        lines.append(_create_table_sql(table))
        lines.append("```")
        if table["columns"]:
            lines.append("")
            lines.append(_render_columns_table(table["columns"]))
        if table.get("indices"):
            lines.append("")
            lines.append("**Indices:**")
            for idx in table["indices"]:
                lines.append(f"- `{idx['name']}`")
        lines.append("")

    for alter in parsed["alters"]:
        lines.append("")
        lines.append(f"**ALTER TABLE `{alter['table']}`:**")
        lines.append(f"- Added column `{alter.get('column', '?')}` ({alter.get('type', '?')})")
        lines.append("")

    for idx in parsed.get("indices", []):
        lines.append("")
        lines.append(f"**Index:** `{idx['name']}`")
        lines.append("")

    return "\n".join(lines)


def _render_migration_row(num: int, filename: str, desc: str) -> str:
    desc_short = desc.split("\n")[0].strip() if desc else ""
    return f"| {num} | `{filename}` | {desc_short} |"


def generate_doc() -> str:
    # Read _meta.json
    meta_path = ROOT / "docs/_meta.json"
    doc_meta: dict = {}
    if meta_path.exists():
        doc_meta = json.loads(meta_path.read_text())

    # Read migration files sorted by number
    migrations_dir = ROOT / "migrations"
    sql_files = sorted(migrations_dir.glob("*.sql"))
    # Deduplicate: only keep .sql files (not .down.sql)
    sql_files = [f for f in sql_files if not f.name.endswith(".down.sql")]

    parsed_migrations: list[dict] = []
    for fpath in sql_files:
        sql = fpath.read_text(encoding="utf-8")
        parsed = _parse_file(sql)

        # Extract migration number from filename
        num_m = re.match(r"(\d+)_", fpath.name)
        num = int(num_m.group(1)) if num_m else 0

        parsed_migrations.append({
            "num": num,
            "filename": fpath.name,
            "parsed": parsed,
        })

    # Build document
    doc = _table_header(doc_meta)
    doc += "\n"

    # Migration history table
    doc += "## Migration History\n\n"
    doc += "| # | File | Description |\n"
    doc += "|---|------|-------------|\n"
    for m in parsed_migrations:
        doc += _render_migration_row(m["num"], m["filename"], m["parsed"]["description"]) + "\n"
    doc += "\n"

    # Detailed migration sections
    doc += "## Detailed Migration Breakdown\n\n"
    for m in parsed_migrations:
        doc += _render_migration_entry(m["num"], m["filename"], m["parsed"]) + "\n\n---\n\n"

    doc += (
        "\n---\n*This file is generated by `scripts/gen_schema_doc.py`. "
        "Do not edit directly; run the script and review the diff.*\n"
    )

    return doc


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    core = generate_doc()
    target = ROOT / "docs/reference/schema.md"
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    manual = extract_manual(existing)
    doc = assemble(core, manual)

    if "--stdout" in sys.argv:
        print(doc)
        return 0

    if "--check" in sys.argv:
        if existing.strip() == doc.strip():
            print("✅ docs/reference/schema.md is in sync with live code.")
            return 0
        print("❌ docs/reference/schema.md has drifted from live code.")
        print("   Run: python scripts/gen_schema_doc.py")
        existing_lines = existing.splitlines()
        new_lines = doc.splitlines()
        for i, (a, b) in enumerate(zip(existing_lines, new_lines)):
            if a != b:
                print(f"  Line {i+1}:")
                print(f"    - {a}")
                print(f"    + {b}")
                if i > 20:
                    print("  ... (truncated)")
                    break
        if len(existing_lines) != len(new_lines):
            print(f"  Line count: existing={len(existing_lines)} new={len(new_lines)}")
        return 1

    target.write_text(doc, encoding="utf-8")
    print(f"Written: {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
