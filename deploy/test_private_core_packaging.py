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
DEPLOY = ROOT / "deploy.sh"

# Overlaid for the working tree/operator, deliberately not baked into the image.
NON_RUNTIME_FILES = {"CLAUDE.md", "LABEL_RESOLUTION.md"}

# Floors that make a parser regression fail instead of vacuously passing.
MIN_OVERLAY_ENTRIES = 9
MIN_OVERLAY_LOOPS = 2
MIN_DOCKERFILE_COPIES = 20
MIN_RESTORE_ENTRIES = 12


def overlay_file_loops() -> list[list[str]]:
    """File entries from every ``for f in ...; do`` loop in the overlay script."""
    text = OVERLAY.read_text(encoding="utf-8")
    loops: list[list[str]] = []
    for match in re.finditer(r"for\s+f\s+in\s+(.*?)\bdo\b", text, re.S):
        entries: list[str] = []
        for line in match.group(1).splitlines():
            line = line.split("#", 1)[0].replace("\\", " ")
            entries.extend(
                token for token in (t.strip(";") for t in line.split()) if token
            )
        loops.append(entries)
    return loops


def overlay_files() -> list[str]:
    """Entries in install_private_core.sh's main `for f in ...; do` list."""
    loops = overlay_file_loops()
    if not loops:
        raise AssertionError(
            "could not find the overlay file list; update this parser rather "
            "than deleting it")
    return loops[0]


def restore_files() -> set[str]:
    """Entries in deploy.sh's ``overlay_files=(...)`` restore list."""
    text = DEPLOY.read_text(encoding="utf-8")
    match = re.search(r"overlay_files=\((.*?)\)", text, re.S)
    if not match:
        raise AssertionError(
            "could not find deploy.sh's overlay_files restore list; update "
            "this parser rather than deleting it")
    return set(re.findall(r'"([^"]+)"', match.group(1)))


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
        self.assertGreaterEqual(len(overlay_file_loops()), MIN_OVERLAY_LOOPS)
        self.assertGreaterEqual(len(dockerfile_copies()), MIN_DOCKERFILE_COPIES)
        self.assertGreaterEqual(len(restore_files()), MIN_RESTORE_ENTRIES)

    def test_every_overlaid_file_is_restored_after_deploy(self):
        # deploy.sh restores the working tree after the overlay; a file that
        # is installed but not restored leaves proprietary code sitting in the
        # public checkout. jev_client.py was missing from the restore list.
        expected = {
            f"unchained/{name}"
            for loop in overlay_file_loops()
            for name in loop
        }
        missing = sorted(expected - restore_files())
        self.assertEqual(
            missing, [],
            "install_private_core.sh overlays these but deploy.sh never "
            "restores them: " + ", ".join(missing),
        )

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
