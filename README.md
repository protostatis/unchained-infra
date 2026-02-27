# unchained-infra

Public infrastructure/control-plane repository for Unchained.

Private intelligence/core modules (`cdp`, `ddm`, `intel`) are **not** stored here.
They live in:
- `protostatis/unchained-core-private` (private)

## CI Modes

- Public CI always runs in this repo.
- Private-integrated CI runs when `PRIVATE_CORE_REPO_PAT` is configured, and overlays private files during workflow execution.

See [SPLIT_REPO_SETUP.md](./SPLIT_REPO_SETUP.md) for setup and GitHub Actions links.
