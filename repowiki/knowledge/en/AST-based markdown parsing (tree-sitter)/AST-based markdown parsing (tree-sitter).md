---
kind: external_dependency
name: AST-based markdown parsing (tree-sitter)
slug: tree-sitter
category: external_dependency
category_hints:
    - vendor_identity
scope:
    - '**'
---

tree-sitter and tree-sitter-markdown are parsed as core dependencies (not optional) and used in `skill_extractor.py` to perform AST-level checks on saved markdown content. This is distinct from plain text reading — the parser walks the markdown AST to detect structural patterns (headings, links, code blocks) for skill extraction.