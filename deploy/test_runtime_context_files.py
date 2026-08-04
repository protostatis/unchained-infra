"""Tests deploy coverage for root-level runtime Dockerfile inputs."""

from __future__ import annotations

import re
import shlex
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "deploy" / "runtime_context_files.sh"


def _manifest_array(name: str) -> set[str]:
    script = f'source "$1"; printf "%s\\n" "${{{name}[@]}}"'
    output = subprocess.check_output(
        ["bash", "-c", script, "runtime-context-test", str(MANIFEST)],
        cwd=ROOT,
        text=True,
    )
    return {line for line in output.splitlines() if line}


def _root_unchained_copy_sources(dockerfile: Path) -> set[str]:
    sources: set[str] = set()
    for line in dockerfile.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\s*COPY(?:\s+--\S+)*\s+(\S+)\s+\S+\s*$", line)
        if not match:
            continue
        source = match.group(1)
        if not source.startswith("unchained/"):
            continue
        relative = source.removeprefix("unchained/")
        if relative.endswith("/") or "/" in relative:
            continue
        sources.add(relative)
    return sources


def _logical_instructions(dockerfile: Path) -> list[str]:
    text = dockerfile.read_text(encoding="utf-8")
    return [
        line.strip()
        for line in re.sub(r"\\\s*\n", " ", text).splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


class RuntimeContextFilesTests(unittest.TestCase):
    def test_manifest_covers_root_unchained_dockerfile_copies(self) -> None:
        runtime_files = _manifest_array("UNCHAINED_RUNTIME_FILES")
        dockerfiles = {
            relative
            for relative in _manifest_array("TOP_LEVEL_CONTEXT_FILES")
            if Path(relative).name.startswith("Dockerfile")
        }
        self.assertTrue(dockerfiles)

        required: set[str] = set()
        for relative in dockerfiles:
            required.update(_root_unchained_copy_sources(ROOT / relative))

        self.assertEqual(required - runtime_files, set())

    def test_manifest_runtime_files_exist(self) -> None:
        missing = {
            relative
            for relative in _manifest_array("UNCHAINED_RUNTIME_FILES")
            if not (ROOT / "unchained" / relative).is_file()
        }
        self.assertEqual(missing, set())

    def test_host_runtime_files_are_manifested_and_deployed_transactionally(self) -> None:
        host_files = _manifest_array("HOST_RUNTIME_FILES")
        self.assertEqual(
            host_files,
            {
                "terminal_runtime_reconciler.py",
                "terminal-runtime-reconciler.service",
            },
        )
        self.assertTrue(all((ROOT / "deploy" / name).is_file() for name in host_files))
        deploy = (ROOT / "deploy.sh").read_text(encoding="utf-8")
        self.assertIn("upload_host_runtime_files()", deploy)
        self.assertIn('upload_host_runtime_files\n', deploy)
        for name in host_files:
            self.assertIn(f'deploy/{name}', deploy)

    def test_pip_requirements_do_not_become_shell_redirections(self) -> None:
        failures: list[str] = []
        for relative in _manifest_array("TOP_LEVEL_CONTEXT_FILES"):
            if not Path(relative).name.startswith("Dockerfile"):
                continue
            for instruction in _logical_instructions(ROOT / relative):
                if not instruction.startswith("RUN ") or "pip install" not in instruction:
                    continue
                lexer = shlex.shlex(
                    instruction.removeprefix("RUN "),
                    posix=True,
                    punctuation_chars="<>",
                )
                lexer.whitespace_split = True
                lexer.commenters = ""
                redirections = [
                    token for token in lexer if token and set(token) <= {"<", ">"}
                ]
                if redirections:
                    failures.append(f"{relative}: {redirections}")

        self.assertEqual(failures, [])

    def test_deploy_snapshots_auth_database_before_mutation(self) -> None:
        deploy = (ROOT / "deploy.sh").read_text(encoding="utf-8")
        snapshot_start = deploy.index("snapshot_remote_release() {")
        snapshot_end = deploy.index("release_remote_rollback_images() {", snapshot_start)
        snapshot = deploy[snapshot_start:snapshot_end]

        self.assertIn('source.backup(target)', snapshot)
        self.assertIn('target.execute("PRAGMA quick_check")', snapshot)
        self.assertIn('"$backup_dir/auth.db.backup"', snapshot)
        self.assertIn('sha256sum "$backup_dir/auth.db.backup"', snapshot)

        snapshot_call = deploy.index("\nsnapshot_remote_release\n")
        mutation = deploy.index("\nDEPLOY_MUTATED=true\n", snapshot_call)
        self.assertLess(snapshot_call, mutation)


if __name__ == "__main__":
    unittest.main()
