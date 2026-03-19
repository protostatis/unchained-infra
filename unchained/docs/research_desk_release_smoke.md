# Research Desk Release Smoke

Use this when validating the `first-look -> /labs/research-desk -> local Research Desk` integration before a deploy.

## Automated checks

Run:

```bash
HOSTED_BASE=https://unchainedsky.com \
LOCAL_BASE=http://127.0.0.1:8766 \
./scripts/research_desk_release_smoke.sh
```

The script writes a JSON artifact under `benchmark/results/` and checks:

- hosted `/labs/research-desk` renders the connect/create/advance controls
- hosted `/first-look` still links into `/labs/research-desk`
- local `/web/research-desk/status` reports the launch URL
- local handshake and action URLs are present

## Manual browser pass

After the script passes:

1. Open `https://unchainedsky.com/first-look`.
2. Trigger `Continue in Research Desk`.
3. On `/labs/research-desk`, click `Connect to Local Desk`.
4. Approve the localhost request in the local Research Desk tab.
5. Click `Create Mission in Local Desk`.
6. Click `Run Next Step`.
7. Verify the watch card updates and, once ready, the preferred CTA points to `Lab Notes`.

## Notes

- The automated script is a release smoke, not a full browser E2E test.
- The browser pass is still required because the localhost approval flow and Mission actions are interactive.
- If the script fails, inspect the generated JSON artifact first:
  - `checks` shows which hosted/local surface failed
  - `local_summary` shows the provider, bridge agent, and capsule count the script observed
