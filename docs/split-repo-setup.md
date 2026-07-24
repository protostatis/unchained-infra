# Split Repo Setup

This repository is the public infrastructure/control-plane repo.
Private intelligence code lives in:

- `protostatis/unchained-core-private` (private)

## GitHub Actions Links

- Public CI workflow:
  - https://github.com/protostatis/unchained-infra/actions/workflows/ci.yml
- Private core smoke workflow:
  - https://github.com/protostatis/unchained-core-private/actions/workflows/private-core-smoke.yml

## Required Secrets (in `unchained-infra`)

1. `PRIVATE_CORE_REPO_PAT`
- Fine-grained PAT with read access to `protostatis/unchained-core-private`.
- Used by Actions checkout to pull private core files.

2. Production Environment secrets
- `EC2_SSH_KEY`
- `EC2_HOST`
- `SSH_KNOWN_HOSTS` — verified `known_hosts` entry for the deployment host.

The GitHub `production` Environment must allow only protected branches and
require an explicit deployment approval. Do not use runtime `ssh-keyscan` in
CI; update `SSH_KNOWN_HOSTS` deliberately after independently verifying a host
key rotation.

## How it Works

1. Pull requests run public-safe checks without access to private-core
   credentials.
2. Protected `main` runs also require `PRIVATE_CORE_REPO_PAT` and run
   private-integrated tests:
- checks out private repo into `private-core/`
- overlays proprietary files into `unchained/` via `tools/install_private_core.sh`
- runs integrated tests

3. A passing `main` revision enters the `production` Environment gate. Once
   approved, the same CI workflow checks that the candidate is still current
   `main`, overlays private core, and invokes `./deploy.sh`.
4. `deploy.sh` holds a remote deployment lock, snapshots the previous release,
   validates container and public HTTP health, and rolls back source plus
   containers if the health gate fails.

## Local Remotes (from monorepo)

- `origin-public` -> `git@github.com:protostatis/unchained-infra.git`
- `origin-private` -> `git@github.com:protostatis/unchained-core-private.git`
