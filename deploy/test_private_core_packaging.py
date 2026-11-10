"""Every overlay-managed runtime file must actually be copied into the image.

install_private_core.sh overlays files into the build context; the Dockerfile
copies an explicit list of them into the image. A file present in one and
missing from the other is silently absent from the running container: importing
it fails only when a caller first uses the feature, which for a disabled feature
may be never.

That happened: jev_client.py was added to the overlay, the deploy logged
"installed jev_client.py", and the container did not have it. The engine's lazy
import turned that into a contained error rather than a crash, which is exactly
why nothing noticed.

The parsers are deliberately permissive about formatting and strict about
coverage: they tolerate inline lists, line continuations, comments, COPY flags
and multi-source COPY lines, and they fail loudly if they stop finding a
plausible number of entries — a silent regex regression would turn the guard
into a no-op.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OVERLAY = ROOT / "tools" / "install_private_core.sh"
DOCKERFILE = ROOT / "Dockerfile"

# Overlaid for the working tree/operator, deliberately not baked into the image.
NON_RUNTIME_FILES = {"CLAUDE.md", "LABEL_RESOLUTION.md"}

# Floors that make a parser regression fail instead of vacuously passing.
MIN_OVERLAY_ENTRIES = 9
MIN_DOCKERFILE_COPIES = 20


def overlay_files() -> list[str]:
    """Entries in install_private_core.sh's `for f in ...; do` list."""
    text = OVERLAY.read_text(encoding="utf-8")
    match = re.search(r"for\s+f\s+in\s+(.*?)\bdo\b", text, re.S)
    if not match:
        raise AssertionError(
            "could not find the overlay file list; update this parser rather "
            "than deleting it")
    entries: list[str] = []
    for line in match.group(1).splitlines():
        line = line.split("#", 1)[0].replace("\\", " ")
        entries.extend(token for token in line.split() if token)
    return entries


def dockerfile_copies() -> set[str]:
    """Every `unchained/<name>` source the Dockerfile copies.

    Handles COPY flags and multi-source forms; a destination directory other
    than "." still counts, because what matters is that the file enters the
    image at all.
    """
    copies: set[str] = set()
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.upper().startswith("COPY "):
            continue
        parts = [p for p in stripped.split()[1:] if not p.startswith("--")]
        if len(parts) < 2:
            continue
        for source in parts[:-1]:          # last token is the destination
            if source.startswith("unchained/"):
                copies.add(source.split("/", 1)[1])
    return copies


class PrivateCorePackagingTests(unittest.TestCase):
    def test_parsers_find_a_plausible_number_of_entries(self):
        self.assertGreaterEqual(len(overlay_files()), MIN_OVERLAY_ENTRIES)
        self.assertGreaterEqual(len(dockerfile_copies()), MIN_DOCKERFILE_COPIES)

    def test_every_runtime_overlay_file_is_copied_into_the_image(self):
        missing = sorted(
            name for name in overlay_files()
            if name not in NON_RUNTIME_FILES
            and name not in dockerfile_copies()
        )
        self.assertEqual(
            missing, [],
            "overlay installs these into the build context but the Dockerfile "
            "never copies them into the image: " + ", ".join(missing),
        )

    def test_assessment_client_is_packaged(self):
        # Named explicitly so deleting the guard above cannot quietly drop the
        # module that motivated it.
        self.assertIn("jev_client.py", overlay_files())
        self.assertIn("jev_client.py", dockerfile_copies())


if __name__ == "__main__":
    unittest.main()
