FROM python:3.13-slim

WORKDIR /app

RUN useradd -m -u 10001 -s /bin/bash unchained \
    && mkdir -p /app /data \
    && chown -R unchained:unchained /app /data

# Copy all unchained modules
COPY unchained/relay.py .
COPY unchained/rate_limit.py .
COPY unchained/auth.py .
COPY unchained/analytics.py .
COPY unchained/credit.py .
COPY unchained/hosted_conversations.py .
COPY unchained/cloud_tools.py .
COPY unchained/private_core_client.py .
COPY unchained/private_core_contracts.py .
COPY unchained/private_core_server.py .
COPY unchained/private_core_engine.py .
COPY unchained/editable_helpers.js .
COPY unchained/challenge_detection.py .
COPY unchained/domain_policy.py .
COPY unchained/api.py .
COPY unchained/mcp_server.py .
COPY unchained/orchestrator.py .
COPY unchained/cdp.py .
COPY unchained/ddm.py .
COPY unchained/intel.py .
COPY unchained/web.py .
COPY unchained/web_state.py .
COPY unchained/overlay_js.py .
COPY unchained/cdp_tool_packaged.py .
COPY unchained/analytics.py .
COPY unchained/published_results.py .
COPY unchained/provision_helpers.py .
COPY unchained/template_utils.py .
COPY unchained/agent_package.py .
COPY unchained/chat_event_transport.py .
COPY unchained/chrome_bridge.py .
COPY unchained/chat_agent_cli.py .
COPY unchained/chat_agent_openrouter.py .
COPY unchained/chat_agent_gemini.py .
COPY unchained/chat_agent_codex.py .
COPY unchained/chat_agent_sdk.py .
COPY unchained/context_compact.py .
COPY unchained/scheduler_agent.py .
COPY unchained/scheduler_tool.py .
COPY unchained/signup_agent.py .
COPY unchained/nudge.py .
COPY unchained/reflex.py .
COPY unchained/scheduled_tasks.py .
COPY unchained/web_app/ web_app/
COPY unchained/benchmark/__init__.py benchmark/__init__.py
COPY unchained/benchmark/progress_critic.py benchmark/progress_critic.py
COPY unchained/benchmark/intermediate_goal.py benchmark/intermediate_goal.py
COPY unchained/pyproject.toml .
COPY unchained/CLAUDE.md .
COPY unchained/scheduled_jobs.example.json .
COPY unchained/favicon.svg .
COPY unchained/og-image.png .
COPY unchained/installers/ installers/

# Install all dependencies
RUN pip install --no-cache-dir \
    websockets>=16.0 \
    httpx>=0.28.1 \
    aiohttp>=3.11 \
    anthropic>=0.49 \
    fastmcp>=2.0 \
    PyJWT>=2.0 \
    cryptography>=42.0 \
    pypdf>=4.0 \
    pyunbrowser==0.0.18

# Rhythm — event-driven SPA automation (copied by deploy.sh when available)
COPY --chown=unchained:unchained rhythm/ rhythm/

COPY research_desk_vendor/ research_desk_vendor/

ENV PYTHONUNBUFFERED=1
ENV HOME=/home/unchained

# Relay on 8765, MCP on 8766, Web UI on 8080, Private core on 8770
EXPOSE 8765 8766 8080 8770

USER 10001:10001

# Default: run relay with integrated API
CMD ["python", "relay.py", "--host", "0.0.0.0"]
