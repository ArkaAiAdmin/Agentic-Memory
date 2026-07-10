#!/usr/bin/env python3
"""Generate docs/reference/configuration.md from infra/config.py + memory.toml.

Usage:
    python scripts/gen_config_doc.py          # write in-place
    python scripts/gen_config_doc.py --check   # exit 1 on mismatch
    python scripts/gen_config_doc.py --stdout  # print to stdout
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

from _docgen_markers import assemble, extract_manual  # noqa: E402


# ---------------------------------------------------------------------------
# Config class discovery via AST
# ---------------------------------------------------------------------------


def _find_config_classes(path: Path) -> list[dict[str, Any]]:
    """Parse infra/config.py and find all dataclass config classes with fields."""
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    classes: list[dict[str, Any]] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        # Check if it's a frozen dataclass
        has_dataclass_decorator = False
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call):
                if getattr(dec.func, "id", "") == "dataclass":
                    has_dataclass_decorator = True
            elif isinstance(dec, ast.Name) and dec.id == "dataclass":
                has_dataclass_decorator = True

        if not has_dataclass_decorator:
            # Check for @dataclass(frozen=True) or similar
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call):
                    func = dec.func
                    if isinstance(func, ast.Name) and func.id == "dataclass":
                        has_dataclass_decorator = True
                        break

        if not has_dataclass_decorator:
            continue

        fields: list[dict[str, Any]] = []
        for item in node.body:
            if isinstance(item, ast.AnnAssign) and item.target:
                field_name = item.target.id if isinstance(item.target, ast.Name) else ""
                if not field_name or field_name.startswith("_"):
                    continue

                # Type annotation
                type_str = ""
                if item.annotation:
                    try:
                        type_str = ast.unparse(item.annotation)
                    except Exception:
                        type_str = "?"

                # Default value
                default_str = ""
                if item.value:
                    try:
                        if isinstance(item.value, ast.Constant):
                            default_str = repr(item.value.value)
                        elif isinstance(item.value, ast.Call) and isinstance(item.value.func, ast.Attribute) and item.value.func.attr == "field":
                            # Extract default from field(default_factory=...)
                            for kw in item.value.keywords:
                                if kw.arg == "default":
                                    try:
                                        default_str = ast.unparse(kw.value)
                                    except Exception:
                                        default_str = "..."
                                elif kw.arg == "default_factory":
                                    default_str = "<factory>"
                            if not default_str:
                                default_str = "<field>"
                        else:
                            default_str = ast.unparse(item.value)
                    except Exception:
                        default_str = "?"

                fields.append({
                    "name": field_name,
                    "type": type_str,
                    "default": default_str,
                })

        if not fields and node.name == "MemoryConfig":
            # MemoryConfig is special - fields are in __init__
            fields = _find_memory_config_fields(text)

        classes.append({
            "name": node.name,
            "fields": fields,
        })

    return classes


def _find_memory_config_fields(text: str) -> list[dict[str, Any]]:
    """Extract MemoryConfig's nested config fields from its __init__."""
    fields = []
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "MemoryConfig":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                    # Look for _SECTION_CLS dict
                    for sub_node in ast.walk(item):
                        if isinstance(sub_node, ast.Dict) and any(
                            isinstance(k, ast.Constant) and isinstance(k.value, str) and k.value != "_FEATURE_FLAGS"
                            for k in sub_node.keys
                        ):
                            for k, v in zip(sub_node.keys, sub_node.values):
                                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                                    val_name = ""
                                    if isinstance(v, ast.Name):
                                        val_name = v.id
                                    elif isinstance(v, ast.Attribute):
                                        val_name = v.attr
                                    fields.append({
                                        "name": k.value,
                                        "type": val_name,
                                        "default": val_name,
                                    })
    return fields


# ---------------------------------------------------------------------------
# TOML parsing
# ---------------------------------------------------------------------------


def _parse_toml_basic(path: Path) -> dict[str, Any]:
    """Read and parse memory.toml, returning section: {key: value}."""
    try:
        import tomllib  # Python 3.11+
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (ImportError, Exception):
        pass
    try:
        import tomli
        data = tomli.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (ImportError, Exception):
        pass
    # Fallback: simple line-based parser
    return _parse_toml_simple(path)


def _parse_toml_simple(path: Path) -> dict[str, Any]:
    """Simple TOML parser for basic key-value pairs."""
    result: dict[str, Any] = {}
    current_section = ""
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        sec_m = re.match(r"\[(\w+)\]", line)
        if sec_m:
            current_section = sec_m.group(1)
            result[current_section] = {}
            continue
        # Subsection: [[section.key]]
        sub_m = re.match(r"\[\[(\w+)\.(\w+)\]\]", line)
        if sub_m:
            if sub_m.group(1) not in result:
                result[sub_m.group(1)] = {}
            result[sub_m.group(1)][sub_m.group(2)] = result.get(sub_m.group(1), {}).get(
                sub_m.group(2), []
            )
            current_section = sub_m.group(1)
            continue
        sub_m2 = re.match(r"\[(\w+)\.(\w+)\]", line)
        if sub_m2:
            section = sub_m2.group(1)
            if section not in result:
                result[section] = {}
            current_section = section
            continue
        kv_m = re.match(r"(\w[\w_]*)\s*=\s*(.+)", line)
        if kv_m and current_section:
            key = kv_m.group(1)
            val = kv_m.group(2).strip()
            # Parse quoted strings, booleans, numbers
            if val.startswith('"') and val.endswith('"'):
                val = val[1:-1]
            elif val in ("true", "True"):
                val = True
            elif val in ("false", "False"):
                val = False
            else:
                try:
                    if "." in val:
                        val = float(val)
                    else:
                        val = int(val)
                except ValueError:
                    pass
            if isinstance(result.get(current_section), dict):
                result[current_section][key] = val
    return result


# ---------------------------------------------------------------------------
# Env var discovery from config.py
# ---------------------------------------------------------------------------


def _find_env_var_mappings(path: Path) -> dict[str, dict[str, str]]:
    """Parse config.py _build_config_from_toml to find MEMORY_* → TOML path mappings.

    Returns {section_name: {field: {env_var, toml_path, default}}}.
    """
    text = path.read_text(encoding="utf-8")
    mappings: dict[str, Any] = {}

    current_section_raw = ""

    # Find all `section = SectionConfig(` blocks, then walk to find _b() calls
    for sec_m in re.finditer(r"(\w+)\s*=\s*(\w+)Config\s*\(", text):
        section_name = sec_m.group(1)

        # Find the closing paren of this section block
        blk_start = sec_m.start()
        depth = 1
        i = sec_m.end()
        while i < len(text) and depth > 0:
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
            i += 1
        block = text[blk_start:i]
        current_section_raw = section_name

        # Find all _b() calls within this section block
        for b_m in re.finditer(r"(\w+)\s*=\s*_b\(", block):
            b_start = b_m.start()
            b_end_local = b_m.end()
            bd = 1
            j = b_end_local
            while j < len(block) and bd > 0:
                if block[j] == "(":
                    bd += 1
                elif block[j] == ")":
                    bd -= 1
                j += 1
            call_text = block[b_start:j]

            # Parse the _b() positional args
            inner = call_text[call_text.index("_b(") + 3 : -1].strip()
            args = []
            ad = 0
            cur = ""
            for ch in inner:
                if ch == "," and ad == 0:
                    args.append(cur.strip())
                    cur = ""
                else:
                    if ch in "([":
                        ad += 1
                    elif ch in ")]":
                        ad -= 1
                    cur += ch
            args.append(cur.strip())

            if len(args) >= 3:
                field = b_m.group(1)
                env_var = args[0].strip('"\'\\n ')
                toml_path = args[1].strip('"\'\\n ')
                default_str = args[2].strip('"\'\\n ')
                sec_lower = current_section_raw.lower()
                if sec_lower not in mappings:
                    mappings[sec_lower] = {}
                mappings[sec_lower][field] = {
                    "env_var": env_var,
                    "toml_path": toml_path,
                    "default": default_str,
                }

    return mappings


# ---------------------------------------------------------------------------
# Markdown generation
# ---------------------------------------------------------------------------


def _group_by_section(all_fields: list[dict], env_mappings: dict) -> dict[str, list[dict]]:
    """Group fields by section."""
    sections: dict[str, list[dict]] = {}
    for field in all_fields:
        section = field.get("section", "other")
        if section not in sections:
            sections[section] = []
        sections[section].append(field)
    return sections


def _build_env_table(
    section_name: str,
    mappings: dict[str, dict],
    toml_data: dict,
) -> str:
    """Build an environment variable table for a given config section."""
    section_mappings = mappings.get(section_name, {})
    if not section_mappings:
        return ""

    rows = ["| Variable | Default | Description |", "|----------|---------|-------------|"]
    for field_name, info in sorted(section_mappings.items()):
        env_var = info["env_var"]
        default = info["default"]
        # Look up TOML value
        toml_path = info["toml_path"]
        toml_val = _resolve_toml_value(toml_data, toml_path)
        desc = _build_desc(field_name, env_var, toml_path, toml_val)
        rows.append(f"| `{env_var}` | `{default}` | {desc} |")

    return "\n".join(rows)


def _resolve_toml_value(data: dict, path: str) -> Any:
    """Resolve a dotted TOML path like 'search.temporal_half_life'."""
    parts = path.split(".")
    cur: Any = data
    for part in parts:
        if isinstance(cur, dict):
            cur = cur.get(part, None)
        else:
            return None
    return cur


def _build_desc(field_name: str, env_var: str, toml_path: str, toml_val: Any) -> str:
    """Build a description string for a config field."""
    parts = []
    if toml_val is not None:
        parts.append(f"TOML: `{toml_path}={toml_val}`")
    return "; ".join(parts) if parts else ""


def generate_doc() -> str:
    config_py = ROOT / "infra/config.py"
    toml_path = ROOT / "memory.toml"
    meta_path = ROOT / "docs/_meta.json"

    # Discover env -> TOML mappings from config.py
    env_mappings = _find_env_var_mappings(config_py)

    # Parse TOML
    toml_data = _parse_toml_basic(toml_path)

    # Read meta
    doc_meta = {}
    if meta_path.exists():
        doc_meta = json.loads(meta_path.read_text())
    schema_version = doc_meta.get("schema_version", 37)

    header = (
        "# Configuration Reference\n\n"
        f"Agentic Memory is configured via environment variables or `memory.toml`. "
        f"(Schema version: {schema_version})\n\n"
        "*This file is generated by `scripts/gen_config_doc.py`. "
        "Run it to sync from live code.*\n"
    )

    # Build sections in display order
    sections = [
        ("core", "Core"),
        ("features", "Features"),
        ("search", "Search"),
        ("cache", "Cache"),
        ("sync", "Multi-Agent Sync"),
        ("api", "API Server"),
        ("write", "Write Pipeline"),
        ("embedding", "Embedding"),
        ("auto_save", "Auto-Save"),
        ("llm", "LLM"),
        ("quality_gates", "Quality Gates"),
        ("user_profile", "User Profile"),
        ("kg", "Knowledge Graph"),
        ("hybrid", "Hybrid Search"),
        ("rerank", "Rerank"),
        ("health_check", "Health Check"),
        ("recall", "Recall"),
        ("sharing", "Memory Sharing"),
    ]

    body_parts = []
    for sec_key, sec_title in sections:
        if sec_key in env_mappings:
            table = _build_env_table(sec_key, env_mappings, toml_data)
            if table:
                body_parts.append(f"## {sec_title}\n\n{table}\n")

    # TOML config section
    body_parts.append("## memory.toml\n\n")
    body_parts.append(f"Located at `{toml_path}`:\n\n")
    body_parts.append("```toml\n")
    body_parts.append(toml_path.read_text(encoding="utf-8"))
    if not toml_path.read_text(encoding="utf-8").endswith("\n"):
        body_parts.append("\n")
    body_parts.append("```\n")

    footer = "\n---\n*This file is generated by `scripts/gen_config_doc.py`. "
    footer += "Do not edit directly; run the script and review the diff.*\n"

    return header + "\n" + "\n".join(body_parts) + "\n" + footer


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    core = generate_doc()
    target = ROOT / "docs/reference/configuration.md"
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    manual = extract_manual(existing)
    doc = assemble(core, manual)

    if "--stdout" in sys.argv:
        print(doc)
        return 0

    if "--check" in sys.argv:
        if existing.strip() == doc.strip():
            print("✅ docs/reference/configuration.md is in sync with live code.")
            return 0
        print("❌ docs/reference/configuration.md has drifted from live code.")
        print("   Run: python scripts/gen_config_doc.py")
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
