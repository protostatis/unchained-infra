#!/bin/bash

# Verify that a production deployment is built from one reproducible public
# source revision. Private-core and generated runtime overlays are applied only
# after this guard succeeds.
verify_deploy_source() {
    local repo_dir="$1"
    local deploy_revision="${2:-}"
    local worktree_status head_revision origin_main_result origin_main_revision

    if [[ ! "$deploy_revision" =~ ^[0-9a-f]{40}$ ]]; then
        echo "ERROR: DEPLOY_REVISION must be set to an explicit 40-character lowercase Git SHA" >&2
        return 1
    fi

    if ! git -C "$repo_dir" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        echo "ERROR: deployment source is not a Git worktree: $repo_dir" >&2
        return 1
    fi

    if ! worktree_status="$(
        git -C "$repo_dir" status --porcelain=v1 --untracked-files=all
    )"; then
        echo "ERROR: could not inspect deployment source worktree status" >&2
        return 1
    fi
    if [[ -n "$worktree_status" ]]; then
        echo "ERROR: deployment source worktree is dirty; commit or remove every change before deploying" >&2
        printf '%s\n' "$worktree_status" >&2
        return 1
    fi

    if ! head_revision="$(git -C "$repo_dir" rev-parse HEAD)"; then
        echo "ERROR: could not resolve deployment worktree HEAD" >&2
        return 1
    fi
    if [[ "$deploy_revision" != "$head_revision" ]]; then
        echo "ERROR: DEPLOY_REVISION does not match the deployment worktree HEAD" >&2
        echo "    DEPLOY_REVISION: $deploy_revision" >&2
        echo "    HEAD:            $head_revision" >&2
        return 1
    fi

    if ! origin_main_result="$(
        git -C "$repo_dir" ls-remote --exit-code origin refs/heads/main
    )"; then
        echo "ERROR: could not query origin/main for deployment verification" >&2
        return 1
    fi
    if ! read -r origin_main_revision _ <<<"$origin_main_result"; then
        echo "ERROR: could not parse origin/main revision" >&2
        return 1
    fi
    if [[ ! "$origin_main_revision" =~ ^[0-9a-f]{40}$ ]]; then
        echo "ERROR: origin/main did not report a valid 40-character Git SHA" >&2
        return 1
    fi
    if [[ "$deploy_revision" != "$origin_main_revision" ]]; then
        echo "ERROR: DEPLOY_REVISION is not the current origin/main revision" >&2
        echo "    DEPLOY_REVISION: $deploy_revision" >&2
        echo "    origin/main:     $origin_main_revision" >&2
        return 1
    fi
}
