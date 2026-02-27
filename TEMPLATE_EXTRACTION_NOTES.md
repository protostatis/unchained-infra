# Template Extraction Notes (PR6)

Initial dedupe implemented:

- Centralized Google client placeholder injection in `unchained/template_utils.py`.
- Updated page handlers in `unchained/web.py` to call the shared helper.

Next extraction steps:

1. Move large inline template constants from `web.py` into `unchained/templates/`.
2. Introduce a tiny loader/cache for template files.
3. Pull repeated chat-page JS blocks into shared partials.
4. Keep route paths and DOM IDs stable while moving source location.
