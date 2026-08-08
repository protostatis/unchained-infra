"""Focused tests for Agent View fidelity-lab style diagnostics."""

import unittest

from fidelity_lab_checker import resolved_style_for_attributes, style_property_names


class TestResolvedSalientStyles(unittest.TestCase):
    def test_combines_inline_and_projected_style_tokens(self):
        attributes = {
            "style": "min-height:900px",
            "data-ucm-cs": "c2 c1 missing",
        }
        salient_styles = (
            '[data-ucm-cs~="c1"]{display:block!important;}'
            '[data-ucm-cs~="c2"]{font-family:Verdana!important;}'
            '[data-ucm-cs~="unused"]{color:red!important;}'
        )

        resolved = resolved_style_for_attributes(attributes, salient_styles)

        self.assertEqual(
            style_property_names(resolved),
            {"min-height", "display", "font-family"},
        )
        self.assertNotIn("color:red", resolved)

    def test_legacy_inline_style_remains_supported(self):
        resolved = resolved_style_for_attributes(
            {"style": "color:rgb(1,2,3);display:flex"},
            "",
        )

        self.assertEqual(style_property_names(resolved), {"color", "display"})


if __name__ == "__main__":
    unittest.main()
