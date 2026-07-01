#!/usr/bin/env python3
"""Unit tests for frontmatter.py.

Run with:
    ~/.config/agentic-memory/venv/bin/python eval/test_frontmatter.py
"""

import sys
import unittest
from pathlib import Path

INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))

from infra.frontmatter import parse_frontmatter, _coerce


class TestCoerce(unittest.TestCase):
    def test_booleans(self):
        self.assertIs(_coerce("true"), True)
        self.assertIs(_coerce("TRUE"), True)
        self.assertIs(_coerce("yes"), True)
        self.assertIs(_coerce("on"), True)
        self.assertIs(_coerce("1"), True)
        self.assertIs(_coerce("false"), False)
        self.assertIs(_coerce("FALSE"), False)
        self.assertIs(_coerce("no"), False)
        self.assertIs(_coerce("off"), False)
        self.assertIs(_coerce("0"), False)

    def test_list(self):
        self.assertEqual(_coerce("[a, b, c]"), ["a", "b", "c"])
        self.assertEqual(_coerce('["a", "b"]'), ["a", "b"])

    def test_empty_string(self):
        self.assertEqual(_coerce(""), "")

    def test_plain_string(self):
        self.assertEqual(_coerce("hello"), "hello")
        self.assertEqual(_coerce('"quoted"'), "quoted")
        self.assertEqual(_coerce("'single'"), "single")


class TestParseFrontmatter(unittest.TestCase):
    def test_no_frontmatter(self):
        meta, body = parse_frontmatter("just content")
        self.assertEqual(meta, {})
        self.assertEqual(body, "just content")

    def test_empty_frontmatter(self):
        meta, body = parse_frontmatter("---\n---\nbody")
        self.assertEqual(meta, {})
        self.assertEqual(body, "---\n---\nbody")

    def test_simple_key_value(self):
        content = "---\ntitle: hello\nimportance: 3\n---\nbody text"
        meta, body = parse_frontmatter(content)
        self.assertEqual(meta["title"], "hello")
        self.assertTrue(meta["importance"] is True or meta["importance"] == "3")

    def test_boolean_value(self):
        content = "---\npinned: true\n---\nbody"
        meta, body = parse_frontmatter(content)
        self.assertIs(meta["pinned"], True)

    def test_inline_list(self):
        content = "---\ntags: [a, b, c]\n---\nbody"
        meta, body = parse_frontmatter(content)
        self.assertEqual(meta["tags"], ["a", "b", "c"])

    def test_multiline_continuation(self):
        content = "---\ndescription:\n  first line\n  second line\n---\nbody"
        meta, body = parse_frontmatter(content)
        self.assertEqual(meta["description"], "first line second line")

    def test_list_items(self):
        content = "---\ntags:\n  - foo\n  - bar\n---\nbody"
        meta, body = parse_frontmatter(content)
        self.assertEqual(meta["tags"], ["foo", "bar"])

    def test_crlf_line_endings(self):
        content = "---\r\ntitle: hello\r\n---\r\nbody"
        meta, body = parse_frontmatter(content)
        self.assertEqual(meta["title"], "hello")
        self.assertEqual(body, "body")

    def test_bom_stripped(self):
        content = "\ufeff---\ntitle: hello\n---\nbody"
        meta, body = parse_frontmatter(content)
        self.assertEqual(meta["title"], "hello")

    def test_comment_lines_skipped(self):
        content = "---\n# this is a comment\ntitle: hello\n---\nbody"
        meta, body = parse_frontmatter(content)
        self.assertEqual(meta["title"], "hello")

    def test_key_without_value(self):
        content = "---\nkey:\n---\nbody"
        meta, body = parse_frontmatter(content)
        self.assertEqual(meta["key"], "")

    def test_multi_key(self):
        content = "---\na: 1\nb: 2\nc: 3\n---\nbody"
        meta, body = parse_frontmatter(content)
        self.assertIn("a", meta)
        self.assertIn("b", meta)
        self.assertIn("c", meta)

    def test_dashes_in_body_not_confused_with_closer(self):
        content = "---\ntitle: hi\n---\nsome text\n---\nmore text"
        meta, body = parse_frontmatter(content)
        self.assertEqual(meta["title"], "hi")
        self.assertEqual(body, "some text\n---\nmore text")

    def test_preserves_body_newlines(self):
        content = "---\ntitle: hi\n---\nline1\n\nline3\n"
        meta, body = parse_frontmatter(content)
        self.assertEqual(body, "line1\n\nline3\n")

    def test_leading_whitespace_before_opening(self):
        content = "   \n---\ntitle: hi\n---\nbody"
        meta, body = parse_frontmatter(content)
        self.assertEqual(meta["title"], "hi")


if __name__ == "__main__":
    unittest.main(verbosity=2)
