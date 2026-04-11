"""Utilities for assembling inline HTML templates safely.

The frontend still ships inline HTML strings, but these helpers make the
assembly flow explicit and fail loudly when a base template drifts.
"""

from __future__ import annotations

from dataclasses import dataclass


class TemplateTransformError(RuntimeError):
    """Raised when a template transform can no longer find its source marker."""


@dataclass(frozen=True)
class TemplateReplacement:
    """A checked string replacement applied to an inline HTML template."""

    needle: str
    replacement: str
    label: str
    expected_count: int = 1


def apply_template_replacements(
    html: str,
    replacements: tuple[TemplateReplacement, ...],
    *,
    template_name: str,
) -> str:
    """Apply checked replacements so template drift fails loudly."""
    rendered = html
    for spec in replacements:
        actual_count = rendered.count(spec.needle)
        if actual_count != spec.expected_count:
            raise TemplateTransformError(
                f"{template_name}: expected {spec.expected_count} match(es) for "
                f"{spec.label!r}, found {actual_count}"
            )
        rendered = rendered.replace(spec.needle, spec.replacement, spec.expected_count)
    return rendered

