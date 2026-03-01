# Unchained Roadmap

> Your browser. Your data. No walls.

## Where We Are

Unchained is a **working product** with real users. The core loop works end-to-end: user logs in, installs the agent via `curl | bash`, starts it, and controls their own Chrome through a chat UI on their phone. The technology layer — CDP tunneling, DOM Density Map, Bayesian page intelligence — is production-tested and solid.

**What's shipped:**
- Chrome CDP tunnel (agent ↔ relay ↔ orchestrator)
- DOM Density Map with 7 output modes (~500 tokens per page)
- Page intelligence with 8 Bayesian extraction strategies
- Chat UI with SSE streaming and session persistence
- Google OAuth sign-in
- One-liner install (`curl | bash`) with auto-update
- Daemon mode for background agent operation
- Multi-tab browser control
- Local chat history that survives restarts
- Docker Compose deployment with TLS (Caddy)
- MCP server for self-drive users
- WebMCP tool discovery for in-page APIs

---

## Phase 0: Top Priority

*Critical items to address immediately.*

- [ ] **Proper CI/CD** — Automated build, test, and deploy pipeline. Run the full test suite on every push, gate merges on passing tests, and automate deployments to staging/production. Replace manual deploy steps with a repeatable pipeline (GitHub Actions or equivalent).
- [ ] **Alternative LLM integration & Codex CLI** — Make the orchestrator model-agnostic in practice, not just in theory. Add support for OpenAI (including Codex CLI auto-setup/provisioning), Gemini, and open-weight models (Llama, Mistral) alongside Claude. Abstract the LLM interface so swapping providers is a config change, not a code rewrite. For Codex CLI: auto-provision API keys, configure the local agent to use Codex as the backing model, and integrate into the onboarding flow so users can pick their preferred LLM at install time.
- [ ] **Official Benchmark Tasks and Eval** — Define a standardized set of browser automation tasks (navigation, form filling, data extraction, multi-step workflows) with ground-truth expected outputs. Build an eval harness that runs these tasks end-to-end and scores accuracy, token efficiency, and latency. Use this to measure regressions and compare LLM backends.
- [ ] **Context compacting logic** — Implement intelligent conversation history compression to keep long-running agent sessions within context window limits. Summarize older tool outputs and DDM results while preserving key facts (URLs visited, data extracted, current task state). Prevent context overflow from killing multi-step workflows.
- [ ] **Codebase refactor for context efficiency** — `web.py` is a monolith with 4 near-identical HTML templates inlined as Python strings, making every small bug fix require an LLM to load ~8K lines of context. Extract templates into separate files, deduplicate shared JS (cancel logic, session management, SSE streaming), and split route handlers into focused modules. Goal: an LLM debugging a cancel button bug shouldn't need to read the entire web server.

---

## Phase 1: Harden & Observe

*Get visibility into what's happening before scaling.*

- [ ] **Monitoring** — Structured logging, request metrics, agent connection tracking. Know when things break before users tell us.
- [ ] **Error tracking** — Sentry or equivalent. Every unhandled exception should create an alert, not vanish into container logs.
- [ ] **Rate limiting** — Per-user request limits on API endpoints. Prevent abuse and runaway agents.
- [ ] **Database migration** — SQLite → PostgreSQL. Current SQLite works for single-instance but blocks horizontal scaling and concurrent writes.
- [ ] **Test coverage for web/API layer** — Orchestrator, web endpoints, and MCP server have zero automated tests. Core tools (DDM, intel, tunnel) are well-tested.
- [ ] **Health checks** — Proper `/health` endpoints for each service. Container orchestration needs to know when a service is actually ready vs just running.

## Phase 2: Multi-User Reality

*Go from "works for a few users" to "works for many users."*

- [ ] **Usage metering** — Track API calls, tokens consumed, browser time per user. Required for any billing model.
- [ ] **Billing integration** — Stripe. Free tier + paid plans. Gate Mode A (managed orchestration) behind subscription.
- [ ] **Admin dashboard** — User list, connection status, usage stats, key management. Currently all done via CLI.
- [ ] **Onboarding flow** — Guide new users through Chrome setup, agent install, first chat. The curl|bash installer is a good start but Chrome debugging mode still requires manual flags.
- [ ] **Email notifications** — Welcome email, agent offline alerts, usage approaching limits.
- [ ] **Session management** — Multiple concurrent sessions per user. Currently one chat session per agent.

## Phase 3: Scale

*Handle growth without rewriting.*

- [ ] **Horizontal scaling** — Multiple relay instances behind a load balancer. Agent-to-relay affinity via consistent hashing.
- [ ] **Multi-region** — US East is live. Add US West, EU, Asia-Pacific. Agents connect to nearest relay.
- [ ] **CDN for static assets** — Chat UI, landing page, favicons served from edge. Currently everything goes through the single EC2 instance.
- [ ] **Connection pooling** — Relay currently holds one WebSocket per agent. Pool and multiplex for efficiency at scale.
- [ ] **Backup & recovery** — Automated database backups, point-in-time recovery. Disaster recovery runbook.

## Phase 4: Product Evolution

*New capabilities that expand what users can do.*

- [ ] **Browser extension** — One-click agent setup instead of command-line install. Enables Chrome debugging mode automatically, connects to relay, no terminal needed.
- [ ] **Scheduled tasks & local job triggers** — Run predefined agent jobs on a schedule using local services (cron, launchd, systemd timers) to trigger tasks via the agent's API. Users define reusable task templates ("check this page," "export this report," "fill this form") and schedule them locally — no cloud scheduler needed. The local agent exposes a trigger endpoint that accepts a task definition and executes it against the user's own browser session.
- [ ] **Workflow builder** — Visual editor for multi-step automations. DDM-first methodology encoded as reusable templates.
- [ ] **Team workspaces** — Shared browser sessions, collaborative agent control. One user browses, team sees the output.
- [ ] **API marketplace** — Users publish discovered app APIs (WebMCP) for others to use. Community-driven tool library.
- [ ] **Enterprise** — SSO/SAML, audit logs, data residency controls, dedicated relay instances.

## Phase 5: Platform

*From tool to ecosystem.*

- [ ] **Agent SDK** — Let developers build custom agents on the Unchained transport layer. DDM and intel as library APIs, not just CLI tools.
- [ ] **Plugin system** — Third-party extensions for specific domains (e-commerce monitoring, social media management, data entry).
- [ ] **Mobile app** — Native iOS/Android for agent control. The chat UI is already mobile-first; a native app adds push notifications, background keep-alive, biometric auth.
- [ ] **Self-hosted option** — Docker image that enterprises run on their own infra. All data stays in their network.

---

## Benchmarks

*Tracking agent capability against real-world challenges.*

| Benchmark | Best | Model | Notes |
|-----------|------|-------|-------|
| [neal.fun/not-a-robot](https://neal.fun/not-a-robot/) | Level 9 / 48 | — | CAPTCHA gauntlet: checkbox, image selection, puzzles, AI face detection. Tests vision + interaction. |

---

## Principles

1. **User's browser, always.** We never run headless Chrome in the cloud. The user's credentials, cookies, and IP are theirs. This is the moat.
2. **DDM over screenshots.** 500 tokens beats 2,100 tokens. Structured understanding beats pixel matching. Always.
3. **Ship, then harden.** Get the feature working end-to-end, deploy it, test it live, then add tests and monitoring. Velocity matters more than coverage at this stage.
4. **No vendor lock-in.** The agent runs Claude today, but the transport layer (CDP tunnel, DDM, intel) is model-agnostic. Swap the LLM without touching the browser stack.
5. **Simple until proven otherwise.** SQLite before Postgres. Single instance before Kubernetes. Bash scripts before CI pipelines. Upgrade when the current solution actually breaks, not when it theoretically might.
