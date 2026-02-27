"""Unit tests for ddm.py pure-Python renderers.

All tests use synthetic data dicts matching the DOM walker output format.
No Chrome or network connection required.
"""
import sys
import unittest
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Mock cdp before importing ddm (avoids Chrome connection)
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

import ddm as dd


# ---------------------------------------------------------------------------
# Helpers: synthetic data builders
# ---------------------------------------------------------------------------

def _data(elements=None, vw=1920, vh=1080):
    """Build a minimal DOM walker data dict."""
    elems = elements or []
    return {'vw': vw, 'vh': vh, 'count': len(elems), 'elements': elems, 'shadow': 0}


def _el(x, y, w, h, k=None, interactive=False, label='', name='', input_type='', domain=''):
    """Build a single element dict matching the DOM walker output."""
    entry = {'x': x, 'y': y, 'w': w, 'h': h}
    if k:
        entry['k'] = k
    if interactive:
        entry['i'] = True
    if label:
        entry['l'] = label
    if name:
        entry['n'] = name
    if input_type:
        entry['it'] = input_type
    if domain:
        entry['d'] = domain
    return entry


# ===========================================================================
# A. _density_char
# ===========================================================================

class TestDensityChar(unittest.TestCase):

    def test_density_char_zero(self):
        self.assertEqual(dd._density_char(0), ' ')

    def test_density_char_one(self):
        self.assertEqual(dd._density_char(1), '.')

    def test_density_char_mid(self):
        self.assertEqual(dd._density_char(2), ':')
        self.assertEqual(dd._density_char(3), ':')

    def test_density_char_high(self):
        self.assertEqual(dd._density_char(4), '#')
        self.assertEqual(dd._density_char(7), '#')

    def test_density_char_overflow(self):
        self.assertEqual(dd._density_char(8), '@')
        self.assertEqual(dd._density_char(100), '@')

    def test_density_char_blocks_mode(self):
        self.assertEqual(dd._density_char(0, blocks=True), ' ')
        self.assertEqual(dd._density_char(1, blocks=True), '░')
        self.assertEqual(dd._density_char(2, blocks=True), '▒')
        self.assertEqual(dd._density_char(3, blocks=True), '▒')
        self.assertEqual(dd._density_char(4, blocks=True), '▓')
        self.assertEqual(dd._density_char(7, blocks=True), '▓')
        self.assertEqual(dd._density_char(8, blocks=True), '█')


# ===========================================================================
# B. _rle_row
# ===========================================================================

class TestRleRow(unittest.TestCase):

    def test_rle_empty(self):
        self.assertEqual(dd._rle_row([]), '')

    def test_rle_no_runs(self):
        self.assertEqual(dd._rle_row(['a', 'b', 'c']), 'abc')

    def test_rle_all_same(self):
        self.assertEqual(dd._rle_row(['a'] * 5), 'a5')

    def test_rle_mixed(self):
        row = ['.'] * 3 + ['#'] * 2 + ['@']
        self.assertEqual(dd._rle_row(row), '.3#2@')


# ===========================================================================
# C. _PRIORITY / _BLOCK_TYPES completeness
# ===========================================================================

class TestPriorityBlockTypes(unittest.TestCase):

    def test_priority_has_all_kinds(self):
        for kind in ('B', 'F', 'L', 'I', 'T', 'C', 'IF'):
            self.assertIn(kind, dd._PRIORITY, f"'{kind}' missing from _PRIORITY")

    def test_block_types_has_all_kinds(self):
        for kind in ('B', 'F', 'L', 'I', 'T', 'C', 'IF'):
            self.assertIn(kind, dd._BLOCK_TYPES, f"'{kind}' missing from _BLOCK_TYPES")

    def test_kind_char_has_all_kinds(self):
        for kind in ('B', 'F', 'L', 'I', 'T', 'C', 'IF'):
            self.assertIn(kind, dd._KIND_CHAR, f"'{kind}' missing from _KIND_CHAR")

    def test_kind_char_all_single_char(self):
        for kind, char in dd._KIND_CHAR.items():
            self.assertEqual(len(char), 1,
                             f"_KIND_CHAR['{kind}'] = '{char}' is not single char")


# ===========================================================================
# D. render_density_map
# ===========================================================================

class TestRenderDensityMap(unittest.TestCase):

    def test_density_map_header(self):
        data = _data(vw=100, vh=50)
        out = dd.render_density_map(data, title="Test", url="http://x.com", tab="T1")
        self.assertIn("=== DOM Density Map ===", out)
        self.assertIn("Page: Test", out)
        self.assertIn("URL: http://x.com", out)
        self.assertIn("Tab: T1", out)

    def test_density_map_single_button(self):
        btn = _el(50, 25, 10, 10, k='B', interactive=True, label='Click')
        data = _data([btn], vw=100, vh=50)
        out = dd.render_density_map(data, max_cols=100)
        # Button should render as 'B' in text mode
        self.assertIn('B', out)

    def test_density_map_interactive_list(self):
        btn = _el(50, 25, 10, 10, k='B', interactive=True, label='Submit')
        data = _data([btn], vw=100, vh=50)
        out = dd.render_density_map(data, max_cols=100)
        self.assertIn('--- Interactive', out)
        self.assertIn('Submit', out)

    def test_density_map_cell_cap(self):
        # Very large grid should be capped
        data = _data(vw=1920, vh=10800)  # would be 1920x10800 cells without cap
        out = dd.render_density_map(data, max_cols=1920)
        # Should not crash; grid capped to MAX_GRID_CELLS
        self.assertIn("=== DOM Density Map ===", out)

    def test_density_map_empty_page(self):
        data = _data(vw=100, vh=50)
        out = dd.render_density_map(data, max_cols=100)
        self.assertIn('Elements: 0 visible, 0 interactive', out)
        self.assertNotIn('--- Interactive', out)

    def test_density_map_iframe_renders_as_X(self):
        iframe = _el(10, 10, 20, 20, k='IF')
        data = _data([iframe], vw=100, vh=50)
        out = dd.render_density_map(data, max_cols=100)
        # IF should render as 'X' in text mode (not the 2-char 'IF')
        lines = out.split('\n')
        grid_lines = [l for l in lines if len(l) == 100]
        found_x = any('X' in line for line in grid_lines)
        self.assertTrue(found_x, "IF element should render as 'X' in text mode grid")


# ===========================================================================
# E. render_sparse_map
# ===========================================================================

class TestRenderSparseMap(unittest.TestCase):

    def test_sparse_map_rle_output(self):
        btn = _el(0, 0, 100, 10, k='B', interactive=True, label='Go')
        data = _data([btn], vw=100, vh=50)
        out = dd.render_sparse_map(data, max_cols=100)
        self.assertIn('RLE:', out)

    def test_sparse_map_row_dedup(self):
        # Single tall element should create identical rows that get deduped
        el = _el(0, 0, 100, 50, k='T')
        data = _data([el], vw=100, vh=50)
        out = dd.render_sparse_map(data, max_cols=10)
        # Should see "x" multiplier for repeated rows
        has_dedup = 'x' in out or '-' in out
        self.assertTrue(has_dedup, "Identical consecutive rows should be collapsed")

    def test_sparse_map_empty_rows(self):
        # Element only at bottom
        el = _el(0, 40, 10, 10, k='B', interactive=True, label='X')
        data = _data([el], vw=100, vh=50)
        out = dd.render_sparse_map(data, max_cols=10)
        self.assertIn('(empty)', out)

    def test_sparse_map_interactive_list(self):
        btn = _el(50, 25, 10, 10, k='B', interactive=True, label='OK')
        data = _data([btn], vw=100, vh=50)
        out = dd.render_sparse_map(data, max_cols=100)
        self.assertIn('--- Interactive', out)
        self.assertIn('OK', out)


# ===========================================================================
# F. render_interactive_only
# ===========================================================================

class TestRenderInteractiveOnly(unittest.TestCase):

    def test_interactive_only_filters_non_interactive(self):
        btn = _el(10, 10, 20, 20, k='B', interactive=True, label='Go')
        txt = _el(50, 10, 20, 20, k='T')  # not interactive
        data = _data([btn, txt], vw=100, vh=50)
        out = dd.render_interactive_only(data)
        self.assertIn('Go', out)
        # Non-interactive elements have no label in the output
        self.assertIn('1 interactive', out)

    def test_interactive_only_sort_order(self):
        b1 = _el(50, 100, 10, 10, k='B', interactive=True, label='Bottom')
        b2 = _el(50, 10, 10, 10, k='B', interactive=True, label='Top')
        data = _data([b1, b2], vw=200, vh=200)
        out = dd.render_interactive_only(data)
        top_pos = out.index('Top')
        bottom_pos = out.index('Bottom')
        self.assertLess(top_pos, bottom_pos, "Should be sorted by py")

    def test_interactive_only_cap_50(self):
        elems = [_el(10, i * 10, 10, 8, k='B', interactive=True, label=f'B{i}')
                 for i in range(60)]
        data = _data(elems, vw=200, vh=800)
        out = dd.render_interactive_only(data)
        self.assertIn('50 interactive', out)


# ===========================================================================
# G. _extract_interactive
# ===========================================================================

class TestExtractInteractive(unittest.TestCase):

    def test_extract_interactive_filters_junk(self):
        btn = _el(10, 10, 20, 20, k='L', interactive=True, label='privacy')
        data = _data([btn])
        result = dd._extract_interactive(data, filter_junk=True)
        self.assertEqual(len(result), 0)

    def test_extract_interactive_strips_pua(self):
        # PUA char (U+E000) + normal text
        btn = _el(10, 10, 20, 20, k='B', interactive=True, label='\ue000Search')
        data = _data([btn])
        result = dd._extract_interactive(data)
        self.assertEqual(result[0]['label'], 'Search')

    def test_extract_interactive_empty_label_skipped(self):
        btn = _el(10, 10, 20, 20, k='B', interactive=True, label='\ue000')
        data = _data([btn])
        result = dd._extract_interactive(data)
        self.assertEqual(len(result), 0, "Element with PUA-only label should be skipped")

    def test_extract_interactive_no_filter(self):
        btn = _el(10, 10, 20, 20, k='L', interactive=True, label='privacy')
        data = _data([btn])
        result = dd._extract_interactive(data, filter_junk=False)
        self.assertEqual(len(result), 1)


# ===========================================================================
# H. _is_junk / _strip_pua
# ===========================================================================

class TestJunkAndPua(unittest.TestCase):

    def test_is_junk_known_labels(self):
        for label in ('terms', 'privacy', 'dismiss', 'get app'):
            self.assertTrue(dd._is_junk(label), f"'{label}' should be junk")

    def test_is_junk_prefix_match(self):
        self.assertTrue(dd._is_junk('1 mi north of downtown'))
        self.assertTrue(dd._is_junk('500 ft ahead'))

    def test_is_junk_normal_label(self):
        self.assertFalse(dd._is_junk('Search'))
        self.assertFalse(dd._is_junk('Sign in'))

    def test_strip_pua_removes_icons(self):
        self.assertEqual(dd._strip_pua('\ue000Hello\ue001'), 'Hello')
        self.assertEqual(dd._strip_pua('Normal text'), 'Normal text')

    def test_strip_pua_empty(self):
        self.assertEqual(dd._strip_pua('\ue000\ue001'), '')


# ===========================================================================
# I. _label_short
# ===========================================================================

class TestLabelShort(unittest.TestCase):

    def test_label_short_under_limit(self):
        self.assertEqual(dd._label_short('Hello', max_len=25), 'Hello')

    def test_label_short_over_limit(self):
        result = dd._label_short('This is a very long label text here', max_len=15)
        self.assertLessEqual(len(result), 15)

    def test_label_short_empty(self):
        self.assertEqual(dd._label_short(''), '_')
        self.assertEqual(dd._label_short('   '), '_')

    def test_label_short_word_boundary(self):
        # "Hello World Foo" with max_len=13 -> "Hello World" at word boundary
        result = dd._label_short('Hello World Foo', max_len=13)
        self.assertEqual(result, 'Hello World')

    def test_label_short_no_good_word_boundary(self):
        # Single long word — can't cut at word boundary, takes first max_len chars
        result = dd._label_short('Superlongword rest', max_len=10)
        self.assertEqual(len(result), 10)


# ===========================================================================
# J. LLM renderers
# ===========================================================================

class TestLlmCompact(unittest.TestCase):

    def test_llm_compact_format(self):
        btn = _el(50, 25, 10, 10, k='B', interactive=True, label='Click')
        data = _data([btn], vw=100, vh=50)
        out = dd.render_llm_compact(data, title='Test')
        self.assertIn('Test', out)
        self.assertIn('Click', out)
        self.assertIn(',', out)  # coordinates

    def test_llm_input_prefix(self):
        field = _el(50, 25, 10, 10, k='F', interactive=True, label='Email')
        data = _data([field], vw=100, vh=50)
        out = dd.render_llm_compact(data)
        self.assertIn('>Email', out)


class TestLlmLine(unittest.TestCase):

    def test_llm_line_format(self):
        btn = _el(50, 25, 10, 10, k='B', interactive=True, label='Go')
        data = _data([btn], vw=100, vh=50)
        out = dd.render_llm_line(data, title='Page')
        self.assertIn('@', out)  # @ delimiter
        self.assertIn('Go', out)


class TestLlm2Pass(unittest.TestCase):

    def test_llm_2pass_format(self):
        b1 = _el(50, 25, 10, 10, k='B', interactive=True, label='Submit')
        b2 = _el(80, 25, 10, 10, k='B', interactive=True, label='Cancel')
        data = _data([b1, b2], vw=100, vh=50)
        out = dd.render_llm_2pass(data, title='My Page')
        self.assertIn('|', out)  # pipe delimiter between elements
        self.assertIn('Submit', out)
        self.assertIn('Cancel', out)

    def test_llm_2pass_includes_title(self):
        btn = _el(50, 25, 10, 10, k='B', interactive=True, label='OK')
        data = _data([btn], vw=100, vh=50)
        out = dd.render_llm_2pass(data, title='Dashboard')
        self.assertIn('Dashboard', out)

    def test_llm_2pass_includes_tab(self):
        btn = _el(50, 25, 10, 10, k='B', interactive=True, label='OK')
        data = _data([btn], vw=100, vh=50)
        out = dd.render_llm_2pass(data, title='Dashboard', tab='TAB123')
        self.assertIn('Tab: TAB123', out)

    def test_llm_2pass_no_tab_when_empty(self):
        btn = _el(50, 25, 10, 10, k='B', interactive=True, label='OK')
        data = _data([btn], vw=100, vh=50)
        out = dd.render_llm_2pass(data, title='Dashboard', tab='')
        self.assertNotIn('Tab:', out)


class TestLlmGroup(unittest.TestCase):

    def test_llm_group_bands(self):
        b1 = _el(10, 10, 10, 10, k='B', interactive=True, label='Top')
        b2 = _el(10, 200, 10, 10, k='B', interactive=True, label='Bottom')
        data = _data([b1, b2], vw=300, vh=300)
        out = dd.render_llm_group(data, title='Page')
        # Two bands because Y difference > Y_BAND_THRESHOLD
        y_lines = [l for l in out.split('\n') if l.startswith('[y')]
        self.assertEqual(len(y_lines), 2)

    def test_llm_group_empty(self):
        data = _data(vw=100, vh=50)
        out = dd.render_llm_group(data, title='Empty')
        self.assertIn('(empty)', out)


class TestLlmResolve(unittest.TestCase):

    def test_llm_resolve_found(self):
        btn = _el(50, 25, 10, 10, k='B', interactive=True, label='Search')
        data = _data([btn], vw=100, vh=50)
        out = dd.render_llm_resolve(data, query='search')
        self.assertIn('Search', out)
        self.assertIn(',', out)  # coordinates
        self.assertNotEqual(out, 'not found')

    def test_llm_resolve_not_found(self):
        data = _data(vw=100, vh=50)
        out = dd.render_llm_resolve(data, query='nonexistent')
        self.assertEqual(out, 'not found')


# ===========================================================================
# K. render_forms
# ===========================================================================

class TestRenderForms(unittest.TestCase):

    def test_render_forms_basic(self):
        forms = [{
            'index': 0,
            'action': 'https://example.com/search',
            'method': 'GET',
            'fields': [
                {'name': 'q', 'tag': 'input', 'type': 'text', 'label': 'Search'},
            ],
            'submits': ['Search'],
        }]
        out = dd.render_forms(forms, title='Example', url='https://example.com')
        self.assertIn('=== Page Forms', out)
        self.assertIn('Page: Example', out)
        self.assertIn('q: text', out)
        self.assertIn('submit: Search', out)

    def test_render_forms_json(self):
        # render_forms doesn't do JSON itself — that's handled by the caller
        # but we test that the text output is well-formed
        forms = []
        out = dd.render_forms(forms)
        self.assertIn('No forms found', out)

    def test_render_forms_with_tab(self):
        forms = []
        out = dd.render_forms(forms, tab='TAB_XYZ')
        self.assertIn('Tab: TAB_XYZ', out)

    def test_render_forms_hidden_fields(self):
        forms = [{
            'index': 0,
            'fields': [
                {'name': 'csrf', 'tag': 'input', 'type': 'hidden', 'hidden': True},
                {'name': 'q', 'tag': 'input', 'type': 'text'},
            ],
            'submits': ['Go'],
        }]
        out = dd.render_forms(forms)
        self.assertIn('hidden: csrf', out)


# ===========================================================================
# L. render_elements_at
# ===========================================================================

class TestRenderElementsAt(unittest.TestCase):

    def test_elements_at_basic(self):
        elements = [
            {'tag': 'button', 'text': 'Submit', 'cls': 'btn primary',
             'rect': {'x': 10, 'y': 20, 'w': 80, 'h': 30}},
            {'tag': 'div', 'rect': {'x': 0, 'y': 0, 'w': 200, 'h': 100}},
        ]
        out = dd.render_elements_at(elements, 50, 35)
        self.assertIn('=== Elements at px(50,35)', out)
        self.assertIn('<button>', out)
        self.assertIn('text: "Submit"', out)
        self.assertIn('class: btn primary', out)

    def test_elements_at_with_tab(self):
        elements = [{'tag': 'div', 'rect': {'x': 0, 'y': 0, 'w': 100, 'h': 100}}]
        out = dd.render_elements_at(elements, 50, 50, tab='TAB_9')
        self.assertIn('Tab: TAB_9', out)

    def test_elements_at_with_attributes(self):
        elements = [{
            'tag': 'a', 'href': 'https://example.com', 'aria': 'Home',
            'role': 'link', 'data_e2e': 'nav-home', 'clickable': True,
            'rect': {'x': 0, 'y': 0, 'w': 50, 'h': 20},
        }]
        out = dd.render_elements_at(elements, 25, 10)
        self.assertIn('href: https://example.com', out)
        self.assertIn('aria-label: "Home"', out)
        self.assertIn('role="link"', out)
        self.assertIn('data-e2e="nav-home"', out)
        self.assertIn('cursor: pointer', out)


# ===========================================================================
# M. Helper summaries
# ===========================================================================

class TestHelperSummaries(unittest.TestCase):

    def test_canvas_summary(self):
        canvas = _el(0, 0, 1920, 1080, k='C')
        data = _data([canvas], vw=1920, vh=1080)
        out = dd._canvas_summary(data)
        self.assertIn('Canvas:', out)
        self.assertIn('full', out)  # covers >80% of viewport

    def test_canvas_summary_empty(self):
        data = _data()
        self.assertEqual(dd._canvas_summary(data), '')

    def test_iframe_summary(self):
        iframe = _el(10, 10, 300, 200, k='IF', domain='youtube.com')
        data = _data([iframe])
        out = dd._iframe_summary(data)
        self.assertIn('Iframe:', out)
        self.assertIn('youtube.com', out)

    def test_iframe_summary_empty(self):
        data = _data()
        self.assertEqual(dd._iframe_summary(data), '')

    def test_shadow_summary(self):
        data = _data()
        data['shadow'] = 5
        out = dd._shadow_summary(data)
        self.assertIn('Shadow:', out)
        self.assertIn('5 hosts', out)

    def test_shadow_summary_zero(self):
        data = _data()
        self.assertEqual(dd._shadow_summary(data), '')

    def test_page_hints_combined(self):
        canvas = _el(0, 0, 1920, 1080, k='C')
        iframe = _el(10, 10, 300, 200, k='IF', domain='ads.com')
        data = _data([canvas, iframe], vw=1920, vh=1080)
        data['shadow'] = 3
        out = dd._page_hints(data)
        self.assertIn('Canvas:', out)
        self.assertIn('Iframe:', out)
        self.assertIn('Shadow:', out)

    def test_page_hints_empty(self):
        data = _data()
        self.assertEqual(dd._page_hints(data), '')


# ===========================================================================
# N. _build_grid (new shared function)
# ===========================================================================

class TestBuildGrid(unittest.TestCase):

    def test_build_grid_basic(self):
        btn = _el(50, 25, 10, 10, k='B', interactive=True, label='Go')
        data = _data([btn], vw=100, vh=50)
        density, kinds, interactive, cols, rows, cell_px = dd._build_grid(data, max_cols=100)
        self.assertEqual(cols, 100)
        self.assertGreater(rows, 0)
        self.assertEqual(len(interactive), 1)
        self.assertEqual(interactive[0]['label'], 'Go')

    def test_build_grid_cell_cap(self):
        data = _data(vw=1920, vh=10800)
        density, kinds, interactive, cols, rows, cell_px = dd._build_grid(data, max_cols=1920)
        self.assertLessEqual(cols * rows, dd.MAX_GRID_CELLS)

    def test_build_grid_interactive_cap(self):
        elems = [_el(10, i * 10, 10, 8, k='B', interactive=True, label=f'B{i}')
                 for i in range(60)]
        data = _data(elems, vw=200, vh=800)
        _, _, interactive, _, _, _ = dd._build_grid(data)
        self.assertLessEqual(len(interactive), dd.MAX_INTERACTIVE)


# ===========================================================================
# O. Module constants
# ===========================================================================

class TestModuleConstants(unittest.TestCase):

    def test_constants_exist(self):
        self.assertEqual(dd.MAX_DOM_ELEMENTS, 2000)
        self.assertEqual(dd.MAX_INTERACTIVE, 50)
        self.assertEqual(dd.MAX_GRID_CELLS, 16000)
        self.assertEqual(dd.DEFAULT_COLS, 160)
        self.assertEqual(dd.LABEL_TRUNC_SHORT, 25)
        self.assertEqual(dd.Y_BAND_THRESHOLD, 50)


# ===========================================================================
# P. render_text
# ===========================================================================

class TestRenderText(unittest.TestCase):

    def test_render_text_basic(self):
        out = dd.render_text("Hello world", title="Test", url="http://x.com", tab="T1")
        self.assertIn("=== Page Text ===", out)
        self.assertIn("Page: Test", out)
        self.assertIn("URL: http://x.com", out)
        self.assertIn("Tab: T1", out)
        self.assertIn("Hello world", out)

    def test_render_text_find_hit(self):
        text = "A" * 300 + "KEYWORD" + "B" * 300
        out = dd.render_text(text, find="KEYWORD")
        self.assertIn('Find: "KEYWORD"', out)
        self.assertIn("KEYWORD", out)

    def test_render_text_find_miss(self):
        out = dd.render_text("no match here", find="zebra")
        self.assertIn("not found", out)

    def test_render_text_empty(self):
        out = dd.render_text("")
        self.assertIn("Length: 0", out)

    def test_render_text_length(self):
        out = dd.render_text("x" * 100)
        self.assertIn("Length: 100", out)


class TestFindNearbyInteractives(unittest.TestCase):
    """Tests for _find_nearby_interactives()."""

    def _make_interactives(self, *py_values):
        return [{'kind': 'B', 'label': f'Btn{i}', 'px': 100, 'py': py}
                for i, py in enumerate(py_values)]

    def test_empty_list(self):
        result = dd._find_nearby_interactives([], 300)
        self.assertEqual(result, [])

    def test_all_in_range(self):
        items = self._make_interactives(290, 310, 300)
        result = dd._find_nearby_interactives(items, 300, max_dist=100)
        self.assertEqual(len(result), 3)

    def test_none_in_range(self):
        items = self._make_interactives(100, 600)
        result = dd._find_nearby_interactives(items, 350, max_dist=50)
        self.assertEqual(result, [])

    def test_sorted_by_distance(self):
        items = self._make_interactives(200, 305, 295, 400)
        result = dd._find_nearby_interactives(items, 300, max_dist=100)
        # 295 (dist=5), 305 (dist=5), 200 excluded (dist=100 is boundary)
        # dist 200->300 = 100 which is equal to max_dist, so included
        distances = [abs(el['py'] - 300) for el in result]
        self.assertEqual(distances, sorted(distances))

    def test_capped_at_3(self):
        items = self._make_interactives(298, 299, 300, 301, 302)
        result = dd._find_nearby_interactives(items, 300, max_dist=100)
        self.assertEqual(len(result), 3)


class TestRenderFind(unittest.TestCase):
    """Tests for render_find()."""

    def _match(self, px=400, py=300, tag='span', ctx='...some text...', below=False):
        return {'px': px, 'py': py, 'tag': tag, 'ctx': ctx, 'below': below}

    def test_basic_match_with_coords(self):
        matches = [self._match(px=450, py=280, tag='span', ctx='...the price is $29.99...')]
        out = dd.render_find(matches, None, title="Test Page", tab="T1", keyword="price")
        self.assertIn('=== Find: "price" ===', out)
        self.assertIn("Tab: T1", out)
        self.assertIn("Page: Test Page", out)
        self.assertIn("Matches: 1", out)
        self.assertIn("[1] (450,280) <span>", out)
        self.assertIn("$29.99", out)

    def test_multiple_matches(self):
        matches = [
            self._match(px=100, py=200, ctx='first match'),
            self._match(px=300, py=400, ctx='second match'),
            self._match(px=500, py=600, ctx='third match'),
        ]
        out = dd.render_find(matches, None, keyword="test")
        self.assertIn("Matches: 3", out)
        self.assertIn("[1]", out)
        self.assertIn("[2]", out)
        self.assertIn("[3]", out)

    def test_below_fold_marker(self):
        matches = [self._match(py=1200, below=True)]
        out = dd.render_find(matches, None, keyword="test")
        self.assertIn("[below fold]", out)

    def test_no_below_fold_when_false(self):
        matches = [self._match(below=False)]
        out = dd.render_find(matches, None, keyword="test")
        self.assertNotIn("[below fold]", out)

    def test_nearby_interactives_shown(self):
        matches = [self._match(px=400, py=300)]
        dom = _data([
            _el(380, 290, 80, 30, k='B', interactive=True, label='Add to Cart'),
            _el(500, 280, 60, 30, k='L', interactive=True, label='Buy Now'),
        ])
        out = dd.render_find(matches, dom, keyword="price")
        self.assertIn("->", out)
        self.assertIn("Add to Cart", out)
        self.assertIn("Buy Now", out)

    def test_no_matches(self):
        out = dd.render_find([], None, keyword="missing")
        self.assertIn("Matches: 0", out)
        self.assertNotIn("[1]", out)

    def test_no_dom_data_graceful_fallback(self):
        matches = [self._match()]
        out = dd.render_find(matches, None, keyword="test")
        self.assertIn("[1]", out)
        # No crash, no -> line since no interactives
        self.assertNotIn("->", out)

    def test_nearby_capped_at_3(self):
        matches = [self._match(px=400, py=300)]
        dom = _data([
            _el(380, 290, 80, 30, k='B', interactive=True, label='Btn1'),
            _el(400, 295, 60, 30, k='B', interactive=True, label='Btn2'),
            _el(420, 300, 60, 30, k='L', interactive=True, label='Link1'),
            _el(440, 305, 60, 30, k='L', interactive=True, label='Link2'),
            _el(460, 310, 60, 30, k='B', interactive=True, label='Btn3'),
        ])
        out = dd.render_find(matches, dom, keyword="test")
        # Count the number of elements in the -> line
        arrow_lines = [l for l in out.split('\n') if l.strip().startswith('->')]
        self.assertEqual(len(arrow_lines), 1)
        # Should have exactly 3 items (pipe-separated)
        parts = arrow_lines[0].split('|')
        self.assertEqual(len(parts), 3)


if __name__ == '__main__':
    unittest.main()
