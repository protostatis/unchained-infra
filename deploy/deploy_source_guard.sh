#!/bin/bash

# Verify that a production deployment is built from one reproducible public
# source revision. Private-core and generated runtime overlays are applied only
# after this guard succeeds.
verify_deploy_source() {
    local repo_dir="$1"
    local deploy_revision="${2:-}"
    local worktree_status head_revision origin_main_revision

    if [[ ! "$deploy_revision" =~ ^[0-9a-f]{40}$ ]]; then
        echo "ERROR: DEPLOY_REVISION must be set to an explicit 40-character lowercase Git SHA" >&2
        return 1
    fi

    if ! git -C "$repo_dir" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        echo "ERROR: deployment source is not a Git worktree: $repo_dir" >&2
        return 1
    fi

    worktree_status="$(git -C "$repo_dir" status --porcelain=v1 --untracked-files=all)"
    if [[ -n "$worktree_status" ]]; then
        echo "ERROR: deployment source worktree is dirty; commit or remove every change before deploying" >&2
        printf '%s\n' "$worktree_status" >&2
        return 1
    fi

    head_revision="$(git -C "$repo_dir" rev-parse HEAD)"
    if [[ "$deploy_revision" != "$head_revision" ]]; then
        echo "ERROR: DEPLOY_REVISION does not match the deployment worktree HEAD" >&2
        echo "    DEPLOY_REVISION: $deploy_revision" >&2
        echo "    HEAD:            $head_revision" >&2
        return 1
    fi

    if ! git -C "$repo_dir" fetch --no-tags --depth=1 origin \
        +refs/heads/main:refs/remotes/origin/main; then
        echo "ERROR: could not refresh origin/main for deployment verification" >&2
        return 1
    fi
    origin_main_revision="$(git -C "$repo_dir" rev-parse refs/remotes/origin/main)"
    if [[ "$deploy_revision" != "$origin_main_revision" ]]; then
        echo "ERROR: DEPLOY_REVISION is not the current origin/main revision" >&2
        echo "    DEPLOY_REVISION: $deploy_revision" >&2
        echo "    origin/main:     $origin_main_revision" >&2
        return 1
    fi
}
