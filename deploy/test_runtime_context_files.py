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
                "browser_terminal_canary_preflight.sh",
                "terminal_runtime_reconciler.py",
                "terminal-runtime-reconciler.service",
                "preserve_hosted_conversations.py",
            },
        )
        self.assertTrue(all((ROOT / "deploy" / name).is_file() for name in host_files))
        deploy = (ROOT / "deploy.sh").read_text(encoding="utf-8")
        self.assertIn("upload_host_runtime_files()", deploy)
        self.assertIn('upload_host_runtime_files\n', deploy)
        for name in host_files:
            self.assertIn(f'deploy/{name}', deploy)

    def test_web_recreate_preserves_legacy_hosted_data_before_replacement(self) -> None:
        deploy = (ROOT / "deploy.sh").read_text(encoding="utf-8")
        preserve = deploy.index('preservation_tool="$remote_dir/deploy/preserve_hosted_conversations.py"')
        stop = deploy.index('docker compose stop --timeout 30 web', preserve)
        snapshot = deploy.index(
            'docker cp -a "$web_container:$legacy_source/." -', stop
        )
        run = deploy.index('docker run -i --rm --network none --read-only', snapshot)
        recreate = deploy.index('docker compose up -d --no-deps --no-build --force-recreate "$service"')
        self.assertLess(preserve, stop)
        self.assertLess(stop, snapshot)
        self.assertLess(snapshot, run)
        self.assertLess(run, recreate)
        self.assertIn('if [[ ! "$web_container" =~ ^[0-9a-f]{12,64}$ ]]; then', deploy)
        self.assertIn('if [[ ! -f "$preservation_tool" ]]; then', deploy)
        self.assertIn('"$legacy_source" != /*', deploy)
        self.assertIn('"$legacy_source" == *:*', deploy)
        self.assertIn('web_user="$(docker exec "$web_container" python3 -c', deploy)
        self.assertIn('data_volume="$(docker inspect --format', deploy)
        self.assertIn('web_image="$(docker inspect --format', deploy)
        self.assertIn('data_mount="type=volume,src=$data_volume,dst=/data"', deploy)
        self.assertIn('--network none', deploy)
        self.assertIn('--user "$web_user"', deploy)
        self.assertIn('--destination-dir /data/hosted-conversations', deploy)
        # Recovery keeps compose's lifecycle bookkeeping coherent.
        self.assertIn('docker compose start web', deploy)
        self.assertNotIn('docker start "$web_container"', deploy)
        # The stopped tree is streamed through a tar pipe: extraction runs as
        # the web UID inside the helper, so there is no container-to-container
        # copy and no ownership-preserving copy back into a container (both are
        # unsupported by Docker).
        self.assertIn('helper_mount="type=bind,src=$preservation_tool,', deploy)
        self.assertIn('--mount "$helper_mount"', deploy)
        self.assertIn('tar -xf - -C /tmp/legacy', deploy)
        self.assertIn('--source-dir /tmp/legacy', deploy)
        self.assertNotIn("docker create --name", deploy)
        # Transcripts must never be staged on the host filesystem.
        self.assertNotIn('mktemp -d /tmp/hosted-conversation-migration', deploy)
        self.assertNotIn('chmod -R a+rX', deploy)
        self.assertNotIn('--volumes-from "$web_container"', deploy)
        self.assertIn('UNCHAINED_HOSTED_DATA_DIR=/data/hosted-conversations', (ROOT / "docker-compose.yml").read_text())

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
