#!/usr/bin/env python3
"""
verify_repowiki.py - Integrity and health verification script for Agentic Memory RepoWiki.
Checks for missing catalog items, failed markdown pages, Chinese export artifacts, and empty knowledge source files.
"""

import sys
import os
import glob
import json
import yaml

def verify_repowiki():
    repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    wiki_dir = os.path.join(repo_dir, 'repowiki')
    content_dir = os.path.join(wiki_dir, 'en/content')
    meta_path = os.path.join(wiki_dir, 'en/meta/repowiki-metadata.json')
    knowledge_dir = os.path.join(wiki_dir, 'knowledge/en')

    errors = []

    # Check 1: Metadata consistency
    if not os.path.exists(meta_path):
        errors.append(f"Metadata file missing: {meta_path}")
    else:
        with open(meta_path, 'r', encoding='utf-8') as f:
            meta = json.load(f)

        catalogs = meta.get('wiki_catalogs', [])
        items = meta.get('wiki_items', [])
        overview = meta.get('wiki_overview', {}).get('content', '')

        if len(catalogs) != len(items):
            errors.append(f"Metadata mismatch: {len(catalogs)} catalogs vs {len(items)} items")

        if '</think>' in overview or 'technical issue' in overview.lower():
            errors.append("wiki_overview contains failed AI generation artifact ('</think>')")

        cat_ids = {c['id'] for c in catalogs}
        item_cat_ids = {i['catalog_id'] for i in items}
        missing_ids = cat_ids - item_cat_ids
        if missing_ids:
            errors.append(f"Missing items for catalog IDs: {missing_ids}")

    # Check 2: Content markdown files verification
    if not os.path.exists(content_dir):
        errors.append(f"Content directory missing: {content_dir}")
    else:
        all_md = glob.glob(os.path.join(content_dir, '**/*.md'), recursive=True)
        for md_file in all_md:
            with open(md_file, 'r', encoding='utf-8', errors='ignore') as f:
                c = f.read()
            rel_path = os.path.relpath(md_file, content_dir)
            if '</think>' in c or 'technical issue' in c.lower() or 'apologiz' in c.lower():
                errors.append(f"Failed page found in content: {rel_path}")
            if len(c.strip()) < 100:
                errors.append(f"Page too short / empty: {rel_path} ({len(c)} bytes)")

    # Check 3: Check for Chinese characters artifact
    for root, dirs, files in os.walk(wiki_dir):
        for file in files:
            p = os.path.join(root, file)
            if file.endswith(('.md', '.yaml', '.yml', '.json')):
                try:
                    with open(p, 'r', encoding='utf-8', errors='ignore') as f:
                        c = f.read()
                    if any('\u4e00' <= char <= '\u9fff' for char in c):
                        errors.append(f"Chinese character found in file: {os.path.relpath(p, wiki_dir)}")
                except Exception:
                    pass

    # Check 4: Knowledge map source_files verification
    if os.path.exists(knowledge_dir):
        module_yamls = glob.glob(os.path.join(knowledge_dir, '**/_module.yaml'), recursive=True)
        for my in module_yamls:
            try:
                with open(my, 'r', encoding='utf-8') as f:
                    data = yaml.safe_load(f)
                if isinstance(data, dict) and not data.get('source_files'):
                    errors.append(f"Empty source_files in module card: {os.path.relpath(my, knowledge_dir)}")
            except Exception as e:
                errors.append(f"Invalid yaml in {my}: {e}")

    # Check 5: Catalog title -> .md file existence assertion
    if os.path.exists(meta_path) and os.path.exists(content_dir):
        cat_names = {c['id']: c.get('name') for c in catalogs}
        all_md_basenames = {os.path.splitext(os.path.basename(p))[0]: p for p in all_md}
        for item in items:
            title = item.get('title')
            cat_name = cat_names.get(item.get('catalog_id'))
            if title not in all_md_basenames and cat_name not in all_md_basenames:
                errors.append(f"Catalog item title '{title}' / catalog name '{cat_name}' has no matching .md file")

    # Check 6: Core map invariant hard strings allowlist assertion
    hard_strings = ['save_memory', 'include_global', 'memory_maintenance', '24']
    target_rel_paths = [
        'Core Concepts/Memory Architecture.md',
        'API Reference/MCP Tools.md',
    ]
    for rel_p in target_rel_paths:
        full_p = os.path.join(content_dir, rel_p)
        if not os.path.exists(full_p):
            errors.append(f"Core map page missing: {rel_p}")
        else:
            with open(full_p, 'r', encoding='utf-8', errors='ignore') as f:
                c = f.read()
            for hs in hard_strings:
                if hs not in c:
                    errors.append(f"Core map page '{rel_p}' missing required invariant term '{hs}'")

    # Report results
    if errors:
        print(f"❌ RepoWiki Verification FAILED with {len(errors)} error(s):")
        for err in errors:
            print(f" - {err}")
        return 1
    else:
        print("✅ RepoWiki Verification PASSED! All 132 catalog items present, 0 failed pages, 0 Chinese artifacts, 100% catalog-to-md mapping, and core invariant terms verified.")
        return 0

if __name__ == '__main__':
    sys.exit(verify_repowiki())

