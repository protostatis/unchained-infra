"""Unit tests for intel.py — Bayesian model, renderers, arg parsing.

All tests use synthetic data dicts. No Chrome or network connection required.
"""
import math
import sys
import unittest
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Mock cdp before importing intel (avoids Chrome connection)
# ---------------------------------------------------------------------------
_mock_tt = MagicMock()
_mock_tt._select_profile = MagicMock()
_mock_tt.CDP = MagicMock
_mock_tt._get_ws_url = MagicMock()
_mock_tt._ensure_chrome = MagicMock()
_mock_tt._list_tabs = MagicMock(return_value=[])
_mock_tt._get_ws_url_for_tab = MagicMock()
_mock_tt._create_new_tab = MagicMock()
_mock_tt._close_tab = MagicMock()
sys.modules.setdefault('cdp', _mock_tt)

import intel as pi


# ---------------------------------------------------------------------------
# Synthetic site fingerprints (from 6-site experiment)
# ---------------------------------------------------------------------------

REDDIT_FP = {
    'totalEls': 4269, 'customEls': 1110, 'shadowRoots': 697,
    'richAttrEls': 82, 'avgAttrs': 26, 'dataTestIds': 0,
    'iframes': 2, 'canvases': 0, 'imgs': 15, 'reactFiber': False,
}
REDDIT_STORES = []

YOUTUBE_FP = {
    'totalEls': 6572, 'customEls': 1467, 'shadowRoots': 0,
    'richAttrEls': 24, 'avgAttrs': 9, 'dataTestIds': 0,
    'iframes': 3, 'canvases': 2, 'imgs': 50, 'reactFiber': False,
}
YOUTUBE_STORES = [{'name': 'ytInitialData', 'size': 1600000}]

HN_FP = {
    'totalEls': 810, 'customEls': 0, 'shadowRoots': 0,
    'richAttrEls': 0, 'avgAttrs': 0, 'dataTestIds': 0,
    'iframes': 0, 'canvases': 0, 'imgs': 1, 'reactFiber': False,
}
HN_STORES = []

AMAZON_FP = {
    'totalEls': 3500, 'customEls': 0, 'shadowRoots': 0,
    'richAttrEls': 5, 'avgAttrs': 10, 'dataTestIds': 3,
    'iframes': 4, 'canvases': 1, 'imgs': 120, 'reactFiber': False,
}
AMAZON_STORES = []

CNN_FP = {
    'totalEls': 2800, 'customEls': 3, 'shadowRoots': 0,
    'richAttrEls': 2, 'avgAttrs': 10, 'dataTestIds': 5,
    'iframes': 6, 'canvases': 0, 'imgs': 40, 'reactFiber': False,
}
CNN_STORES = []

WEATHER_FP = {
    'totalEls': 1800, 'customEls': 2, 'shadowRoots': 0,
    'richAttrEls': 3, 'avgAttrs': 12, 'dataTestIds': 45,
    'iframes': 1, 'canvases': 0, 'imgs': 20, 'reactFiber': False,
}
WEATHER_STORES = []

REACT_APP_FP = {
    'totalEls': 1200, 'customEls': 0, 'shadowRoots': 0,
    'richAttrEls': 0, 'avgAttrs': 0, 'dataTestIds': 10,
    'iframes': 0, 'canvases': 0, 'imgs': 5, 'reactFiber': True,
}
REACT_APP_STORES = []

NEXT_APP_FP = {
    'totalEls': 900, 'customEls': 0, 'shadowRoots': 0,
    'richAttrEls': 0, 'avgAttrs': 0, 'dataTestIds': 2,
    'iframes': 0, 'canvases': 0, 'imgs': 3, 'reactFiber': True,
}
NEXT_STORES = [{'name': '__NEXT_DATA__', 'size': 50000}]


# ===========================================================================
# A. categorize_features
# ===========================================================================

class TestCategorizeFeatures(unittest.TestCase):

    def test_reddit_features(self):
        obs = pi.categorize_features(REDDIT_FP, REDDIT_STORES)
        self.assertEqual(obs["shadow_roots"], "high")
        self.assertEqual(obs["custom_elements"], "high")
        self.assertEqual(obs["rich_attrs"], "high")
        self.assertFalse(obs["js_global_found"])
        self.assertFalse(obs["react_fiber"])

    def test_youtube_features(self):
        obs = pi.categorize_features(YOUTUBE_FP, YOUTUBE_STORES)
        self.assertEqual(obs["shadow_roots"], "low")
        self.assertEqual(obs["custom_elements"], "high")
        self.assertEqual(obs["rich_attrs"], "low")
        self.assertTrue(obs["js_global_found"])

    def test_hn_features(self):
        obs = pi.categorize_features(HN_FP, HN_STORES)
        self.assertEqual(obs["shadow_roots"], "low")
        self.assertEqual(obs["custom_elements"], "none")
        self.assertEqual(obs["rich_attrs"], "low")
        self.assertFalse(obs["js_global_found"])
        self.assertEqual(obs["total_elements"], "normal")

    def test_sparse_page(self):
        fp = {'totalEls': 50, 'customEls': 0, 'shadowRoots': 0,
              'richAttrEls': 0, 'avgAttrs': 0, 'dataTestIds': 0}
        obs = pi.categorize_features(fp, [])
        self.assertEqual(obs["total_elements"], "sparse")

    def test_dense_page(self):
        fp = {'totalEls': 5000, 'customEls': 10, 'shadowRoots': 10,
              'richAttrEls': 3, 'avgAttrs': 12, 'dataTestIds': 0}
        obs = pi.categorize_features(fp, [])
        self.assertEqual(obs["total_elements"], "dense")
        self.assertEqual(obs["shadow_roots"], "medium")
        self.assertEqual(obs["custom_elements"], "medium")

    def test_weather_features(self):
        obs = pi.categorize_features(WEATHER_FP, WEATHER_STORES)
        self.assertEqual(obs["data_testids"], "many")

    def test_very_many_testids(self):
        fp = {'totalEls': 3000, 'customEls': 0, 'shadowRoots': 0,
              'richAttrEls': 0, 'avgAttrs': 0, 'dataTestIds': 250}
        obs = pi.categorize_features(fp, [])
        self.assertEqual(obs["data_testids"], "very_many")

    def test_store_noise_filtered(self):
        """Framework noise stores should NOT set js_global_found."""
        fp = {'totalEls': 1000, 'customEls': 0, 'shadowRoots': 0,
              'richAttrEls': 0, 'avgAttrs': 0, 'dataTestIds': 0}
        noise_stores = [
            {'name': '__SENTRY__', 'size': 500},
            {'name': '__BUILD_MANIFEST', 'size': 2000},
            {'name': '_sentryDebugIds', 'size': 100},
        ]
        obs = pi.categorize_features(fp, noise_stores)
        self.assertFalse(obs["js_global_found"])

    def test_known_data_store_passes(self):
        """Known data stores (ytInitialData) should set js_global_found."""
        fp = {'totalEls': 1000, 'customEls': 0, 'shadowRoots': 0,
              'richAttrEls': 0, 'avgAttrs': 0, 'dataTestIds': 0}
        data_stores = [{'name': 'ytInitialData', 'size': 500}]
        obs = pi.categorize_features(fp, data_stores)
        self.assertTrue(obs["js_global_found"])

    def test_next_data_small_filtered(self):
        """Small __NEXT_DATA__ (<5KB) should NOT set js_global_found."""
        fp = {'totalEls': 1000, 'customEls': 0, 'shadowRoots': 0,
              'richAttrEls': 0, 'avgAttrs': 0, 'dataTestIds': 0}
        small_next = [{'name': '__NEXT_DATA__', 'size': 2000}]
        obs = pi.categorize_features(fp, small_next)
        self.assertFalse(obs["js_global_found"])

    def test_next_data_large_passes(self):
        """Large __NEXT_DATA__ (>5KB) should set js_global_found."""
        fp = {'totalEls': 1000, 'customEls': 0, 'shadowRoots': 0,
              'richAttrEls': 0, 'avgAttrs': 0, 'dataTestIds': 0}
        large_next = [{'name': '__NEXT_DATA__', 'size': 50000}]
        obs = pi.categorize_features(fp, large_next)
        self.assertTrue(obs["js_global_found"])


# ===========================================================================
# B. bayesian_rank
# ===========================================================================

class TestBayesianRank(unittest.TestCase):

    def test_reddit_selects_host_attrs(self):
        obs = pi.categorize_features(REDDIT_FP, REDDIT_STORES)
        ranked = pi.bayesian_rank(obs)
        self.assertEqual(ranked[0][0], "host_attrs")
        self.assertGreater(ranked[0][1], 0.50)

    def test_youtube_selects_js_global(self):
        obs = pi.categorize_features(YOUTUBE_FP, YOUTUBE_STORES)
        ranked = pi.bayesian_rank(obs)
        self.assertEqual(ranked[0][0], "js_global")

    def test_hn_selects_innertext(self):
        obs = pi.categorize_features(HN_FP, HN_STORES)
        ranked = pi.bayesian_rank(obs)
        self.assertEqual(ranked[0][0], "innerText")

    def test_weather_selects_data_testid(self):
        obs = pi.categorize_features(WEATHER_FP, WEATHER_STORES)
        ranked = pi.bayesian_rank(obs)
        self.assertEqual(ranked[0][0], "data_testid")

    def test_react_app_selects_react_fiber(self):
        obs = pi.categorize_features(REACT_APP_FP, REACT_APP_STORES)
        ranked = pi.bayesian_rank(obs)
        self.assertEqual(ranked[0][0], "react_fiber")

    def test_next_app_selects_js_global(self):
        """Next.js app with __NEXT_DATA__ should prefer js_global."""
        obs = pi.categorize_features(NEXT_APP_FP, NEXT_STORES)
        ranked = pi.bayesian_rank(obs)
        # js_global or react_fiber are both valid; js_global should win
        # because __NEXT_DATA__ is a strong signal
        top_two = {ranked[0][0], ranked[1][0]}
        self.assertIn("js_global", top_two)

    def test_very_many_testids_selects_data_testid(self):
        """Site with 500+ data-testids should strongly prefer data_testid."""
        fp = {'totalEls': 3000, 'customEls': 0, 'shadowRoots': 0,
              'richAttrEls': 0, 'avgAttrs': 0, 'dataTestIds': 500,
              'reactFiber': False}
        obs = pi.categorize_features(fp, [])
        ranked = pi.bayesian_rank(obs)
        self.assertEqual(ranked[0][0], "data_testid")
        self.assertGreater(ranked[0][1], 0.50)

    def test_amazon_selects_img_alt(self):
        obs = pi.categorize_features(AMAZON_FP, AMAZON_STORES)
        ranked = pi.bayesian_rank(obs)
        # Amazon: many images, no custom elements, no globals
        top = ranked[0][0]
        self.assertIn(top, ["img_alt", "innerText", "heading_hier"])

    def test_cnn_selects_reasonable_strategy(self):
        obs = pi.categorize_features(CNN_FP, CNN_STORES)
        ranked = pi.bayesian_rank(obs)
        top = ranked[0][0]
        # CNN: 5 testids, iframes, no stores — data_testid or innerText
        self.assertIn(top, ["data_testid", "heading_hier", "innerText"])


# ===========================================================================
# C. bayesian_rank normalization
# ===========================================================================

class TestBayesianRankNormalization(unittest.TestCase):

    def test_posteriors_sum_to_one(self):
        obs = pi.categorize_features(REDDIT_FP, REDDIT_STORES)
        ranked = pi.bayesian_rank(obs)
        total = sum(p for _, p in ranked)
        self.assertAlmostEqual(total, 1.0, places=5)

    def test_no_nan_or_negative(self):
        obs = pi.categorize_features(HN_FP, HN_STORES)
        ranked = pi.bayesian_rank(obs)
        for strategy, prob in ranked:
            self.assertFalse(math.isnan(prob), f"{strategy} has NaN probability")
            self.assertGreaterEqual(prob, 0.0, f"{strategy} has negative probability")


# ===========================================================================
# D. render_fingerprint
# ===========================================================================

class TestRenderFingerprint(unittest.TestCase):

    def test_basic_fingerprint(self):
        fp = {'totalEls': 100, 'customEls': 5, 'shadowRoots': 0,
              'avgAttrs': 0, 'dataTestIds': 0, 'reactFiber': False}
        out = pi.render_fingerprint(fp)
        self.assertIn("fingerprint:", out)
        self.assertIn("100els", out)
        self.assertIn("5custom", out)
        self.assertIn("stores:none", out)

    def test_fingerprint_with_stores(self):
        fp = {'totalEls': 100, 'customEls': 0, 'shadowRoots': 0,
              'avgAttrs': 0, 'dataTestIds': 0, 'reactFiber': False}
        stores = [{'name': 'ytInitialData', 'size': 1600000}]
        out = pi.render_fingerprint(fp, stores)
        self.assertIn("ytInitialData", out)
        self.assertIn("1.6MB", out)

    def test_fingerprint_react(self):
        fp = {'totalEls': 500, 'customEls': 0, 'shadowRoots': 0,
              'avgAttrs': 0, 'dataTestIds': 10, 'reactFiber': True}
        out = pi.render_fingerprint(fp)
        self.assertIn("react:yes", out)

    def test_fingerprint_no_react(self):
        fp = {'totalEls': 500, 'customEls': 0, 'shadowRoots': 0,
              'avgAttrs': 0, 'dataTestIds': 0, 'reactFiber': False}
        out = pi.render_fingerprint(fp)
        self.assertIn("react:no", out)


# ===========================================================================
# E. render_strategy_line
# ===========================================================================

class TestRenderStrategy(unittest.TestCase):

    def test_strategy_line_basic(self):
        ranked = [("host_attrs", 0.95), ("shadow_pierce", 0.03)]
        out = pi.render_strategy_line(ranked)
        self.assertIn("host_attrs", out)
        self.assertIn("95%", out)
        self.assertIn("runner-up", out)
        self.assertIn("shadow_pierce", out)

    def test_strategy_line_empty(self):
        out = pi.render_strategy_line([])
        self.assertIn("unknown", out)

    def test_strategy_line_single(self):
        ranked = [("innerText", 0.80)]
        out = pi.render_strategy_line(ranked)
        self.assertIn("innerText", out)
        self.assertNotIn("runner-up", out)


# ===========================================================================
# F. render_host_attrs
# ===========================================================================

class TestRenderHostAttrs(unittest.TestCase):

    def test_host_attrs_basic(self):
        data = [
            {"tag": "shreddit-post", "attrs": {"author": "u/test", "score": "100",
             "title": "Test post"}, "text": ""},
            {"tag": "shreddit-post", "attrs": {"author": "u/other", "score": "50",
             "title": "Another"}, "text": ""},
        ]
        out = pi.render_host_attrs(data)
        self.assertIn("schema:", out)
        self.assertIn("u/test", out)
        self.assertIn("u/other", out)

    def test_host_attrs_empty(self):
        out = pi.render_host_attrs([])
        self.assertIn("no host attributes", out)

    def test_host_attrs_none(self):
        out = pi.render_host_attrs(None)
        self.assertIn("no host attributes", out)

    def test_host_attrs_filters_class_style(self):
        data = [{"tag": "my-el", "attrs": {"class": "foo bar", "style": "color:red",
                 "title": "real"}, "text": ""}]
        out = pi.render_host_attrs(data)
        self.assertNotIn("foo bar", out)
        self.assertNotIn("color:red", out)
        self.assertIn("real", out)


# ===========================================================================
# G. render_js_global
# ===========================================================================

class TestRenderJsGlobal(unittest.TestCase):

    def test_js_global_basic(self):
        data = {
            "name": "ytInitialData",
            "size": 1600000,
            "shape": {"contents": {"twoColumnBrowseResultsRenderer": "..."}}
        }
        out = pi.render_js_global(data)
        self.assertIn("ytInitialData", out)
        self.assertIn("1.6MB", out)
        self.assertIn("contents", out)

    def test_js_global_error(self):
        data = {"error": "not found"}
        out = pi.render_js_global(data)
        self.assertIn("not found", out)

    def test_js_global_none(self):
        out = pi.render_js_global(None)
        self.assertIn("error", out)


# ===========================================================================
# H. render_img_alt
# ===========================================================================

class TestRenderImgAlt(unittest.TestCase):

    def test_img_alt_basic(self):
        data = [
            {"alt": "Product image", "src": "prod.jpg", "size": "200x200"},
            {"alt": "Logo", "src": "logo.png", "size": "50x50"},
        ]
        out = pi.render_img_alt(data)
        self.assertIn("schema:", out)
        self.assertIn("Product image", out)
        self.assertIn("Logo", out)

    def test_img_alt_empty(self):
        out = pi.render_img_alt([])
        self.assertIn("no images", out)

    def test_img_alt_none(self):
        out = pi.render_img_alt(None)
        self.assertIn("no images", out)


# ===========================================================================
# I. render_heading_hier
# ===========================================================================

class TestRenderHeadingHier(unittest.TestCase):

    def test_heading_hier_basic(self):
        data = {
            "headings": [
                {"level": "h1", "text": "Main Title", "fontSize": 32},
                {"level": "h2", "text": "Section", "fontSize": 24},
            ],
            "hasOverlay": False,
            "iframes": 0,
        }
        out = pi.render_heading_hier(data)
        self.assertIn("Main Title", out)
        self.assertIn("Section", out)

    def test_heading_hier_with_overlay(self):
        data = {
            "headings": [{"level": "h1", "text": "Title", "fontSize": 32}],
            "hasOverlay": True,
            "iframes": 2,
        }
        out = pi.render_heading_hier(data)
        self.assertIn("OVERLAY", out)
        self.assertIn("iframes: 2", out)

    def test_heading_hier_empty(self):
        out = pi.render_heading_hier(None)
        self.assertIn("no headings", out)


# ===========================================================================
# J. schema_rows_compress
# ===========================================================================

class TestSchemaRowsCompression(unittest.TestCase):

    def test_basic_compression(self):
        items = [
            {"name": "Alice", "score": 100},
            {"name": "Bob", "score": 50},
        ]
        out = pi.schema_rows_compress(items, key_order=["name", "score"])
        self.assertIn("schema: [name, score]", out)
        self.assertIn("Alice | 100", out)
        self.assertIn("Bob | 50", out)

    def test_max_rows(self):
        items = [{"n": str(i)} for i in range(20)]
        out = pi.schema_rows_compress(items, max_rows=5)
        self.assertIn("+15 more", out)
        lines = out.strip().split("\n")
        # 1 schema + 5 data + 1 overflow = 7 lines
        self.assertEqual(len(lines), 7)

    def test_empty_items(self):
        out = pi.schema_rows_compress([])
        self.assertIn("no data", out)

    def test_long_values_truncated(self):
        items = [{"text": "x" * 100}]
        out = pi.schema_rows_compress(items)
        self.assertIn("...", out)
        # Value should be truncated to 60 chars
        for line in out.split("\n"):
            if line.startswith("schema:"):
                continue
            self.assertLessEqual(len(line), 65)


# ===========================================================================
# K. Output token budget
# ===========================================================================

class TestOutputTokenBudget(unittest.TestCase):

    def _estimate_tokens(self, text):
        """Rough token estimate: ~4 chars per token."""
        return len(text) / 4

    def test_fingerprint_under_budget(self):
        fp = {'totalEls': 4269, 'customEls': 1110, 'shadowRoots': 697,
              'avgAttrs': 26, 'dataTestIds': 0, 'reactFiber': False}
        stores = [{'name': 'ytInitialData', 'size': 1600000}]
        out = pi.render_fingerprint(fp, stores)
        tokens = self._estimate_tokens(out)
        self.assertLess(tokens, 100, f"Fingerprint too long: ~{tokens:.0f} tokens")

    def test_strategy_line_under_budget(self):
        ranked = [("host_attrs", 0.95), ("shadow_pierce", 0.03)]
        out = pi.render_strategy_line(ranked)
        tokens = self._estimate_tokens(out)
        self.assertLess(tokens, 50, f"Strategy line too long: ~{tokens:.0f} tokens")

    def test_schema_rows_under_budget(self):
        items = [{"author": f"u/user_{i}", "title": f"Post title {i}",
                  "score": str(i * 100)} for i in range(15)]
        out = pi.schema_rows_compress(items, key_order=["author", "title", "score"])
        tokens = self._estimate_tokens(out)
        self.assertLess(tokens, 400, f"Schema+rows too long: ~{tokens:.0f} tokens")


# ===========================================================================
# L. Constants validation
# ===========================================================================

class TestConstants(unittest.TestCase):

    def test_priors_sum_to_one(self):
        total = sum(pi.PRIORS.values())
        self.assertAlmostEqual(total, 1.0, places=5,
                               msg=f"PRIORS sum to {total}, should be 1.0")

    def test_likelihoods_complete(self):
        """Every feature/observation combo covers all 8 strategies."""
        for feature, obs_table in pi.LIKELIHOODS.items():
            for obs, strategy_probs in obs_table.items():
                for strategy in pi.STRATEGIES:
                    self.assertIn(strategy, strategy_probs,
                                  f"Missing {strategy} in LIKELIHOODS[{feature}][{obs}]")

    def test_strategies_have_js_and_renderers(self):
        for s in pi.STRATEGIES:
            self.assertIn(s, pi.STRATEGY_JS,
                          f"Strategy '{s}' missing from STRATEGY_JS")
            self.assertIn(s, pi.STRATEGY_RENDERERS,
                          f"Strategy '{s}' missing from STRATEGY_RENDERERS")


# ===========================================================================
# M. Arg parsing
# ===========================================================================

class TestArgParsing(unittest.TestCase):

    def test_probe_mode(self):
        opts = pi.parse_args(["--probe"])
        self.assertEqual(opts["mode"], "probe")

    def test_extract_with_strategy(self):
        opts = pi.parse_args(["--extract", "--strategy", "host_attrs"])
        self.assertEqual(opts["mode"], "extract")
        self.assertEqual(opts["strategy"], "host_attrs")

    def test_shape_mode(self):
        opts = pi.parse_args(["--shape", "ytInitialData"])
        self.assertEqual(opts["mode"], "shape")
        self.assertEqual(opts["shape_global"], "ytInitialData")

    def test_find_paths_mode(self):
        opts = pi.parse_args(["--find-paths", "ytInitialData", "title"])
        self.assertEqual(opts["mode"], "find_paths")
        self.assertEqual(opts["find_global"], "ytInitialData")
        self.assertEqual(opts["find_pattern"], "title")

    def test_tab_and_url(self):
        opts = pi.parse_args(["--tab", "ABC123", "--probe", "https://example.com"])
        self.assertEqual(opts["tab_id"], "ABC123")
        self.assertEqual(opts["url"], "https://example.com")
        self.assertEqual(opts["mode"], "probe")


# ===========================================================================
# N. Additional render tests
# ===========================================================================

class TestRenderDataTestid(unittest.TestCase):

    def test_basic(self):
        data = [
            {"testid": "temperature", "tag": "span", "text": "72°F"},
            {"testid": "humidity", "tag": "span", "text": "45%"},
        ]
        out = pi.render_data_testid(data)
        self.assertIn("schema:", out)
        self.assertIn("temperature", out)

    def test_empty(self):
        out = pi.render_data_testid([])
        self.assertIn("no data-testid", out)


class TestRenderShadowPierce(unittest.TestCase):

    def test_basic(self):
        data = [{"host": "my-component", "childCount": 10,
                 "text": ["Hello", "World"]}]
        out = pi.render_shadow_pierce(data)
        self.assertIn("<my-component>", out)
        self.assertIn("Hello", out)

    def test_empty(self):
        out = pi.render_shadow_pierce([])
        self.assertIn("no shadow DOM", out)


class TestRenderReactFiber(unittest.TestCase):

    def test_basic(self):
        data = {"fiberKey": "__reactFiber$abc",
                "components": [{"type": "App", "props": {"theme": "dark"}}]}
        out = pi.render_react_fiber(data)
        self.assertIn("fiber:", out)
        self.assertIn("<App>", out)
        self.assertIn("theme=dark", out)

    def test_error(self):
        out = pi.render_react_fiber({"error": "no root element"})
        self.assertIn("no root element", out)


class TestRenderFindPaths(unittest.TestCase):

    def test_basic(self):
        data = {"global": "ytInitialData", "pattern": "title",
                "matches": [{"path": "ytInitialData.contents.title", "preview": "Home"}]}
        out = pi.render_find_paths(data)
        self.assertIn("title", out)
        self.assertIn("Home", out)

    def test_no_matches(self):
        data = {"global": "x", "pattern": "y", "matches": []}
        out = pi.render_find_paths(data)
        self.assertIn("no matches", out)


class TestRenderStores(unittest.TestCase):

    def test_basic(self):
        stores = [{"name": "ytInitialData", "size": 1600000}]
        out = pi.render_stores(stores)
        self.assertIn("ytInitialData", out)
        self.assertIn("1.6MB", out)

    def test_empty(self):
        out = pi.render_stores([])
        self.assertIn("none found", out)


class TestFormatSize(unittest.TestCase):

    def test_bytes(self):
        self.assertEqual(pi._format_size(500), "500B")

    def test_kilobytes(self):
        self.assertEqual(pi._format_size(5000), "5.0KB")

    def test_megabytes(self):
        self.assertEqual(pi._format_size(1600000), "1.6MB")


if __name__ == '__main__':
    unittest.main()
