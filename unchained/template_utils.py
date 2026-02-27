"""Shared helpers for HTML template rendering."""

from __future__ import annotations


def inject_google_client_id(template_html: str, google_client_id: str) -> str:
    """Replace the Google client placeholder in a template."""
    return template_html.replace("__GOOGLE_CLIENT_ID__", google_client_id)
