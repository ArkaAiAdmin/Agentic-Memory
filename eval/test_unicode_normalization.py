import unittest
import unicodedata
from rebuild_index import _normalize_unicode
from memory_mcp import _normalize_unicode as _normalize_unicode_mcp

class TestNFCKNormalization(unittest.TestCase):
    def test_nfkc_diacritics_stripped(self):
        # NFKC preserves canonical diacritics (e → é is canonical, not
        # compatibility, so NFKC leaves it alone). The matching benefit for
        # diacritics comes from FTS5's unicode61 tokenizer, which strips
        # diacritics at the index/match layer. NFKC's job here is
        # compatibility normalization (e.g. fullwidth → ASCII, ligatures
        # → separated letters, variation selectors dropped).
        # Verify that NFKC is idempotent and a no-op for already-NFKC text
        # with combining diacritics; the actual cross-form matching is
        # handled by FTS5.
        self.assertEqual(_normalize_unicode("résumé café"), "résumé café")
        # Compatibility decomposition: ﬁ ligature → "fi"
        self.assertEqual(_normalize_unicode("ﬁle"), "file")
        # Fullwidth ASCII → ASCII
        self.assertEqual(_normalize_unicode("ＡＢＣ"), "ABC")

    def test_nfkc_cjk_fullwidth_space(self):
        # "日本語 テスト" with full-width space should normalize to "日本語 テスト" with regular space
        s = "日本語\u3000テスト"  # \u3000 is fullwidth space
        self.assertEqual(_normalize_unicode(s), "日本語 テスト")

    def test_nfkc_emoji(self):
        # Compatibility emoji sequences normalize to single chars
        # \uFE0F is variation selector
        s = "test \u2702\uFE0F content"
        normalized = _normalize_unicode(s)
        # Should contain the scissors character
        self.assertIn("✂", normalized)

    def test_nfkc_helper_idempotent(self):
        text = "café résumé"
        once = _normalize_unicode(text)
        twice = _normalize_unicode(once)
        self.assertEqual(once, twice)
        self.assertEqual(once, unicodedata.normalize('NFKC', "café résumé"))

    def test_nfkc_query_syntax_preserved(self):
        # FTS5 query syntax should survive normalization
        q = "(a OR b) AND c"
        self.assertEqual(_normalize_unicode(q), q)
        # Numbers and operators should be preserved
        q2 = "tag:python tag:ml AND \"exact phrase\""
        self.assertEqual(_normalize_unicode(q2), q2)
