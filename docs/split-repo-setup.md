# Split Repo Setup

This repository is the public infrastructure/control-plane repo.
Private intelligence code lives in:

- `protostatis/unchained-core-private` (private)

## GitHub Actions Links

- Public CI workflow:
  - https://github.com/protostatis/unchained-infra/actions/workflows/ci.yml
- Public deploy workflow:
  - https://github.com/protostatis/unchained-infra/actions/workflows/deploy.yml
- Private core smoke workflow:
  - https://github.com/protostatis/unchained-core-private/actions/workflows/private-core-smoke.yml

## Required Secrets (in `unchained-infra`)

1. `PRIVATE_CORE_REPO_PAT`
- Fine-grained PAT with read access to `protostatis/unchained-core-private`.
- Used by Actions checkout to pull private core files.

2. Deploy secrets
- `EC2_SSH_KEY`
- `EC2_HOST`

## How it Works

1. Public CI runs public-safe checks by default.
2. If `PRIVATE_CORE_REPO_PAT` is present, CI also runs private-integrated tests:
- checks out private repo into `private-core/`
- overlays proprietary files into `unchained/` via `tools/install_private_core.sh`
- runs integrated tests

3. Deploy workflow uses the same overlay step before shipping code to EC2.

## Local Remotes (from monorepo)

- `origin-public` -> `git@github.com:protostatis/unchained-infra.git`
- `origin-private` -> `git@github.com:protostatis/unchained-core-private.git`
