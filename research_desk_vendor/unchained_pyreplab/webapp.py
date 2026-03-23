from __future__ import annotations

import html
import hmac
import json
import os
import re
import secrets
import time
import threading
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, quote, unquote, urlparse

from .lab_agent import (
    UNCHAINED_TRIAL_PATHS,
    LabAgentError,
    detect_agent_mode,
    generate_code_turn,
    plan_mission,
    summarize_turn,
)
from .lab_session import discover_pyreplab_bin, get_session
from .reload_control import is_reload_paused, set_reload_paused
from .mcp_client import (
    DEFAULT_AGENT_ENV_PATH,
    DEFAULT_ENDPOINT,
    infer_agents_endpoint,
    infer_api_base,
    parse_env_file,
    resolve_credentials,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
CAPSULES_ROOT = REPO_ROOT / "capsules"
HOT_RELOAD_ROOTS = [REPO_ROOT / "unchained_pyreplab", REPO_ROOT / "tests"]

HANDSHAKE_TTL_SECONDS = 300
HANDSHAKE_TOKEN_TTL_SECONDS = 600
MAX_PENDING_HANDSHAKE_REQUESTS = 50
_HANDSHAKE_REQUESTS: dict[str, dict[str, Any]] = {}
_HANDSHAKE_TOKENS: dict[str, str] = {}
_HANDSHAKE_LOCK = threading.Lock()
_MISSION_ADVANCE_LOCK = threading.Lock()
_MISSION_ADVANCE_STATE_LOCK = threading.Lock()
_MISSION_ADVANCE_ACTIVE_CAPSULE = ""
_MISSION_ADVANCE_ACTIVE_KIND = ""
_SERVER_HOST = "127.0.0.1"
_SERVER_PORT = 8766
_HANDSHAKE_CLEANER_STARTED = False
_HANDSHAKE_CLEANER_LOCK = threading.Lock()
_HANDSHAKE_ALLOWED_ORIGINS: tuple[str, ...] = ()


def _local_base_url() -> str:
    return "http://{host}:{port}".format(host=_SERVER_HOST, port=_SERVER_PORT)


def _local_origin_allowed(origin: str = "", referer: str = "") -> bool:
    base = _local_base_url().rstrip("/")
    return (origin or "").rstrip("/") == base or str(referer or "").startswith(base + "/")


def _handshake_cleaner_loop() -> None:
    failure_count = 0
    while True:
        sleep_seconds = 60 if failure_count <= 0 else min(300, 30 * (2**min(failure_count - 1, 3)))
        time.sleep(sleep_seconds)
        try:
            _cleanup_handshake_requests()
            failure_count = 0
        except Exception:
            failure_count = min(failure_count + 1, 5)
            traceback.print_exc()


def _render_inline(text: str) -> str:
    chunks = text.split("`")
    if len(chunks) == 1:
        return html.escape(text)
    parts: list[str] = []
    for index, chunk in enumerate(chunks):
        if index % 2 == 1:
            parts.append("<code>{text}</code>".format(text=html.escape(chunk)))
        else:
            parts.append(html.escape(chunk))
    return "".join(parts)


def _is_markdown_table_delimiter(line: str) -> bool:
    stripped = line.strip().strip("|").strip()
    if not stripped:
        return False
    cells = [cell.strip() for cell in stripped.split("|")]
    if not cells:
        return False
    return all(re.fullmatch(r":?-{3,}:?", cell or "") for cell in cells)


def _split_markdown_table_row(line: str) -> list[str]:
    stripped = line.strip().strip("|")
    return [cell.strip() for cell in stripped.split("|")]


def _looks_like_ascii_chart_line(line: str) -> bool:
    stripped = line.rstrip()
    if "|" not in stripped:
        return False
    if "#" not in stripped:
        return False
    if "(" not in stripped or ")" not in stripped:
        return False
    return bool(re.search(r"\$\s*\d", stripped))


def _looks_like_markdown_table_output(text: str) -> bool:
    stripped = text.strip()
    return "\n|" in stripped and "\n|---" in stripped


def _split_plain_table_row(line: str) -> list[str]:
    return [cell.strip() for cell in re.split(r"\s{2,}", line.strip()) if cell.strip()]


def _looks_like_plaintext_table_output(text: str) -> bool:
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    if len(lines) < 3:
        return False
    header = _split_plain_table_row(lines[0])
    if len(header) < 2:
        return False
    data_rows = 0
    for line in lines[1:]:
        parts = _split_plain_table_row(line)
        if len(parts) >= 2:
            data_rows += 1
    return data_rows >= 2


def _render_plaintext_table_output(text: str) -> str:
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    header = _split_plain_table_row(lines[0])
    if len(header) < 2:
        return '<pre class="stdout-block">{text}</pre>'.format(text=html.escape(text))
    rows: list[list[str]] = []
    width = len(header)
    for line in lines[1:]:
        parts = _split_plain_table_row(line)
        if len(parts) == width + 1:
            rows.append(parts)
        elif len(parts) == width:
            rows.append(["", *parts])
        else:
            return '<pre class="stdout-block">{text}</pre>'.format(text=html.escape(text))
    head_cells = ["stat", *header]
    head_html = "".join("<th>{cell}</th>".format(cell=_render_inline(cell)) for cell in head_cells)
    body_html = []
    for row in rows:
        normalized = row[: len(head_cells)] + [""] * max(len(head_cells) - len(row), 0)
        cells = "".join("<td>{cell}</td>".format(cell=_render_inline(cell)) for cell in normalized)
        body_html.append("<tr>{cells}</tr>".format(cells=cells))
    return (
        '<div class="table-wrap"><table class="md-table"><thead><tr>{head}</tr></thead>'
        "<tbody>{body}</tbody></table></div>"
    ).format(head=head_html, body="".join(body_html))


def _looks_like_dataframe_info_output(text: str) -> bool:
    stripped = text.strip()
    return stripped.startswith("<class 'pandas.core.frame.DataFrame'>") and "Data columns (total" in stripped


def _render_dataframe_info_output(text: str) -> str:
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    if len(lines) < 3:
        return '<pre class="stdout-block">{text}</pre>'.format(text=html.escape(text))
    title = lines[0]
    range_line = next((line for line in lines if "RangeIndex:" in line or "Index:" in line), "")
    memory_line = next((line for line in lines if line.startswith("memory usage:")), "")
    dtype_line = next((line for line in lines if line.startswith("dtypes:")), "")
    column_rows: list[tuple[str, str, str]] = []
    for line in lines:
        stripped = line.strip()
        if not re.match(r"^\d+\s+", stripped):
            continue
        cleaned = re.sub(r"^\d+\s+", "", stripped)
        parts = _split_plain_table_row(cleaned)
        if len(parts) >= 3:
            column_rows.append((parts[0], parts[1], parts[2]))
    meta_rows = []
    if range_line:
        meta_rows.append('<span class="chip">{value}</span>'.format(value=html.escape(range_line)))
    if dtype_line:
        meta_rows.append('<span class="chip">{value}</span>'.format(value=html.escape(dtype_line)))
    if memory_line:
        meta_rows.append('<span class="chip">{value}</span>'.format(value=html.escape(memory_line)))
    if not column_rows:
        return '<pre class="stdout-block">{text}</pre>'.format(text=html.escape(text))
    body_html = []
    for column, non_null, dtype in column_rows:
        body_html.append(
            "<tr><td>{column}</td><td>{non_null}</td><td>{dtype}</td></tr>".format(
                column=_render_inline(column),
                non_null=_render_inline(non_null),
                dtype=_render_inline(dtype),
            )
        )
    return """
<div class="df-info-card">
  <div class="df-info-title">{title}</div>
  <div class="chips chips-wrap">{meta}</div>
  <div class="table-wrap">
    <table class="md-table">
      <thead><tr><th>column</th><th>non-null</th><th>dtype</th></tr></thead>
      <tbody>{body}</tbody>
    </table>
  </div>
</div>
""".format(title=html.escape(title), meta="".join(meta_rows), body="".join(body_html))


def _render_markdown_table(table_lines: list[str]) -> str:
    clean_lines = [line.strip() for line in table_lines if line.strip()]
    if len(clean_lines) < 2 or not _is_markdown_table_delimiter(clean_lines[1]):
        return "\n".join("<p>{text}</p>".format(text=_render_inline(line)) for line in clean_lines)

    header = _split_markdown_table_row(clean_lines[0])
    body_rows = [_split_markdown_table_row(line) for line in clean_lines[2:]]
    width = len(header)
    if width == 0:
        return ""

    def normalize(row: list[str]) -> list[str]:
        if len(row) < width:
            return row + [""] * (width - len(row))
        return row[:width]

    head_html = "".join("<th>{cell}</th>".format(cell=_render_inline(cell)) for cell in normalize(header))
    row_html: list[str] = []
    for row in body_rows:
        cells = "".join("<td>{cell}</td>".format(cell=_render_inline(cell)) for cell in normalize(row))
        row_html.append("<tr>{cells}</tr>".format(cells=cells))
    return (
        '<div class="table-wrap"><table class="md-table"><thead><tr>{head}</tr></thead>'
        "<tbody>{body}</tbody></table></div>"
    ).format(head=head_html, body="".join(row_html))


def _render_markdown_like(text: str) -> str:
    lines = text.splitlines()
    parts: list[str] = []
    in_list = False
    in_code = False
    code_lines: list[str] = []
    table_lines: list[str] = []
    ascii_lines: list[str] = []

    def flush_list() -> None:
        nonlocal in_list
        if in_list:
            parts.append("</ul>")
            in_list = False

    def flush_code() -> None:
        nonlocal in_code, code_lines
        if in_code:
            parts.append("<pre><code>{code}</code></pre>".format(code=html.escape("\n".join(code_lines))))
            in_code = False
            code_lines = []

    def flush_table() -> None:
        nonlocal table_lines
        if table_lines:
            parts.append(_render_markdown_table(table_lines))
            table_lines = []

    def flush_ascii() -> None:
        nonlocal ascii_lines
        if ascii_lines:
            parts.append(
                '<pre class="ascii-block">{text}</pre>'.format(
                    text=html.escape("\n".join(ascii_lines))
                )
            )
            ascii_lines = []

    for raw_line in lines:
        line = raw_line.rstrip()
        stripped = line.strip()
        if stripped.startswith("```"):
            flush_list()
            flush_table()
            flush_ascii()
            if in_code:
                flush_code()
            else:
                in_code = True
                code_lines = []
            continue
        if in_code:
            code_lines.append(line)
            continue
        if _looks_like_ascii_chart_line(line):
            flush_list()
            flush_table()
            ascii_lines.append(line)
            continue
        if stripped.startswith("|"):
            flush_list()
            flush_ascii()
            table_lines.append(stripped)
            continue
        if not stripped:
            if table_lines:
                continue
            flush_ascii()
            flush_list()
            continue
        flush_table()
        flush_ascii()
        if stripped.startswith("- "):
            if not in_list:
                parts.append("<ul>")
                in_list = True
            parts.append("<li>{text}</li>".format(text=_render_inline(stripped[2:])))
            continue
        flush_list()
        if stripped.startswith("### "):
            parts.append("<h3>{text}</h3>".format(text=_render_inline(stripped[4:])))
        elif stripped.startswith("## "):
            parts.append("<h2>{text}</h2>".format(text=_render_inline(stripped[3:])))
        elif stripped.startswith("# "):
            parts.append("<h1>{text}</h1>".format(text=_render_inline(stripped[2:])))
        else:
            parts.append("<p>{text}</p>".format(text=_render_inline(stripped)))

    flush_list()
    flush_table()
    flush_ascii()
    flush_code()
    return "\n".join(parts)


def _html_page(title: str, body: str) -> str:
    reload_paused = is_reload_paused()
    script = """
(function () {
  const reloadToken = document.documentElement.dataset.reloadToken || '';
  let lastReloadToken = reloadToken;
  let reloadPaused = document.documentElement.dataset.reloadPaused === '1';
  const loadingOverlay = document.getElementById('loading-overlay');
  const loadingTitle = document.getElementById('loading-title');
  const loadingCopy = document.getElementById('loading-copy');
  const reloadToggle = document.querySelector('[data-reload-toggle]');
  const reloadBadge = document.getElementById('reload-state');
  const stageSpotlightTitle = document.getElementById('board-spotlight-title');
  const stageSpotlightCopy = document.getElementById('board-spotlight-copy');
  const stageSpotlightHint = document.getElementById('board-spotlight-hint');
  const stageSpotlightMeterFill = document.getElementById('board-spotlight-meter-fill');

  function setLoading(message) {
    if (!loadingOverlay) {
      return;
    }
    const nextMessage = (message || '').trim() || 'Agent is working';
    if (loadingTitle) {
      loadingTitle.textContent = nextMessage;
    }
    if (loadingCopy) {
      loadingCopy.textContent = 'The system is actively planning or executing the next stage. You do not need to intervene unless you want to redirect it.';
    }
    loadingOverlay.hidden = false;
    document.body.classList.add('loading-active');
  }

  function clearLoading() {
    if (!loadingOverlay) {
      return;
    }
    loadingOverlay.hidden = true;
    document.body.classList.remove('loading-active');
  }

  function renderReloadState() {
    if (reloadToggle) {
      reloadToggle.textContent = reloadPaused ? 'Resume hot reload' : 'Pause hot reload';
      reloadToggle.setAttribute('aria-pressed', reloadPaused ? 'true' : 'false');
      reloadToggle.dataset.state = reloadPaused ? 'paused' : 'live';
    }
    if (reloadBadge) {
      reloadBadge.textContent = reloadPaused ? 'paused while you edit' : 'live';
      reloadBadge.dataset.state = reloadPaused ? 'paused' : 'live';
    }
  }

  function revealManualControls(options) {
    const details = document.getElementById('manual-controls');
    if (!details) {
      return;
    }
    details.open = true;
    const body = details.querySelector('.manual-controls-body');
    const summary = details.querySelector('summary');
    window.requestAnimationFrame(function () {
      details.scrollIntoView({ block: 'center', inline: 'nearest', behavior: (options && options.instant) ? 'auto' : 'smooth' });
      if (body) {
        body.scrollTop = 0;
      }
      if (summary && typeof summary.focus === 'function') {
        summary.focus({ preventScroll: true });
      }
    });
  }

  function dismissPopup(target) {
    if (!target) {
      return;
    }
    target.hidden = true;
  }

  function resolveNamedField(name, source) {
    if (!name) {
      return null;
    }
    const candidates = [];
    if (source && typeof source.closest === 'function') {
      const form = source.closest('form');
      if (form) {
        const scoped = form.querySelector('[name="' + name + '"]');
        if (scoped) {
          candidates.push(scoped);
        }
      }
    }
    document.querySelectorAll('[name="' + name + '"]').forEach(function (node) {
      candidates.push(node);
    });
    for (const field of candidates) {
      if (!field) {
        continue;
      }
      const hiddenByDetails = field.closest && field.closest('details:not([open])');
      if (!hiddenByDetails) {
        return field;
      }
    }
    return candidates.length ? candidates[0] : null;
  }

  function focusNamedField(name, source) {
    if (!name) {
      return;
    }
    const field = resolveNamedField(name, source);
    if (!field || typeof field.focus !== 'function') {
      return;
    }
    let current = field.parentElement;
    const detailsAncestors = [];
    while (current) {
      if (current.tagName === 'DETAILS') {
        detailsAncestors.push(current);
      }
      current = current.parentElement;
    }
    detailsAncestors.reverse().forEach(function (details) {
      details.open = true;
    });
    window.requestAnimationFrame(function () {
      field.focus({ preventScroll: true });
      if (typeof field.select === 'function' && (field.tagName === 'TEXTAREA' || field.tagName === 'INPUT')) {
        field.select();
      }
      field.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'smooth' });
    });
  }

  function fillTargetField(name, value, mode, source) {
    if (!name) {
      return;
    }
    const field = resolveNamedField(name, source);
    if (!field) {
      return;
    }
    const nextValue = (value || '').trim();
    if (!nextValue) {
      return;
    }
    const currentValue = typeof field.value === 'string' ? field.value.trim() : '';
    if (mode === 'append' && currentValue) {
      field.value = currentValue + '\\n' + nextValue;
    } else {
      field.value = nextValue;
    }
    field.dispatchEvent(new Event('input', { bubbles: true }));
    field.dispatchEvent(new Event('change', { bubbles: true }));
    focusNamedField(name, source);
  }

  function setStageFocus(trigger, options) {
    if (!trigger) {
      return;
    }
    const stage = (trigger.dataset && trigger.dataset.stageTrigger ? trigger.dataset.stageTrigger : '').trim();
    if (!stage) {
      return;
    }
    const title = (trigger.dataset && trigger.dataset.stageTitle ? trigger.dataset.stageTitle : stage).trim() || stage;
    const copy = (trigger.dataset && trigger.dataset.stageCopy ? trigger.dataset.stageCopy : '').trim();
    const hint = (trigger.dataset && trigger.dataset.stageHint ? trigger.dataset.stageHint : '').trim();
    const energy = Math.max(8, Math.min(100, parseInt(trigger.dataset.stageEnergy || '0', 10) || 0));
    document.querySelectorAll('[data-stage-trigger]').forEach(function (node) {
      node.classList.toggle('is-active', node.dataset.stageTrigger === stage);
    });
    document.querySelectorAll('[data-stage-section]').forEach(function (section) {
      section.classList.toggle('is-stage-active', section.dataset.stageSection === stage);
    });
    if (stageSpotlightTitle) {
      stageSpotlightTitle.textContent = title;
    }
    if (stageSpotlightCopy) {
      stageSpotlightCopy.textContent = copy || 'Tap another stage to jump through the flow.';
    }
    if (stageSpotlightHint) {
      stageSpotlightHint.textContent = 'Desk energy: ' + String(energy) + '% · ' + (hint || 'Desk is live.');
    }
    if (stageSpotlightMeterFill) {
      stageSpotlightMeterFill.style.width = String(energy) + '%';
    }
    if (options && options.scroll === false) {
      return;
    }
    const section = document.querySelector('[data-stage-section="' + stage + '"]');
    if (!section) {
      return;
    }
    window.requestAnimationFrame(function () {
      section.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'smooth' });
    });
  }

  function loadingLabel(form, submitter, fallback) {
    if (submitter && submitter.dataset && submitter.dataset.loadingLabel) {
      return submitter.dataset.loadingLabel;
    }
    if (form && form.dataset && form.dataset.loadingLabel) {
      return form.dataset.loadingLabel;
    }
    return fallback;
  }

  async function pollReload() {
    try {
      const response = await fetch('/__reload_status', { cache: 'no-store' });
      if (!response.ok) {
        return;
      }
      const payload = await response.json();
      const nextToken = (payload && payload.token ? payload.token : '').trim();
      reloadPaused = !!(payload && payload.paused);
      renderReloadState();
      if (!lastReloadToken) {
        lastReloadToken = nextToken;
        return;
      }
      if (reloadPaused) {
        return;
      }
      if (nextToken && nextToken !== lastReloadToken) {
        window.location.reload();
      }
    } catch (error) {
      return;
    }
  }

  renderReloadState();

  if (reloadToggle) {
    reloadToggle.addEventListener('click', async function () {
      const endpoint = reloadPaused ? '/__reload/resume' : '/__reload/pause';
      try {
        const response = await fetch(endpoint, {
          method: 'POST',
          headers: {
            'Accept': 'application/json',
            'X-Requested-With': 'fetch'
          }
        });
        if (!response.ok) {
          throw new Error('reload toggle failed');
        }
        const payload = await response.json();
        reloadPaused = !!(payload && payload.paused);
        renderReloadState();
      } catch (_error) {
        if (reloadBadge) {
          reloadBadge.textContent = 'toggle failed';
          reloadBadge.dataset.state = 'error';
        }
      }
    });
  }

  window.setInterval(pollReload, 1200);

  if (window.location.hash === '#manual-controls') {
    revealManualControls({ instant: true });
  }

  document.addEventListener('click', function (event) {
    const dismissButton = event.target instanceof Element ? event.target.closest('[data-dismiss-popup]') : null;
    if (dismissButton) {
      event.preventDefault();
      dismissPopup(dismissButton.closest('.mission-popup'));
      return;
    }
    const fillButton = event.target instanceof Element ? event.target.closest('[data-fill-target]') : null;
    if (fillButton) {
      event.preventDefault();
      if (fillButton.dataset.openManualControls === 'true') {
        revealManualControls({ instant: false });
      }
      fillTargetField(
        (fillButton.dataset && fillButton.dataset.fillTarget ? fillButton.dataset.fillTarget : '').trim(),
        fillButton.dataset && fillButton.dataset.fillValue ? fillButton.dataset.fillValue : fillButton.textContent,
        fillButton.dataset && fillButton.dataset.fillMode ? fillButton.dataset.fillMode : 'replace',
        fillButton
      );
      if (fillButton.dataset && fillButton.dataset.focusField) {
        window.setTimeout(function () {
          focusNamedField(fillButton.dataset.focusField, fillButton);
        }, 180);
      }
      return;
    }
    const stageTrigger = event.target instanceof Element ? event.target.closest('[data-stage-trigger]') : null;
    if (stageTrigger) {
      event.preventDefault();
      setStageFocus(stageTrigger, { scroll: stageTrigger.dataset.stageScroll !== 'false' });
      return;
    }
    const manualButton = event.target instanceof Element ? event.target.closest('[data-open-manual-controls]') : null;
    if (manualButton) {
      event.preventDefault();
      dismissPopup(manualButton.closest('.mission-popup'));
      if (history && typeof history.replaceState === 'function') {
        history.replaceState(null, '', '#manual-controls');
      }
      revealManualControls({ instant: false });
      if (manualButton.dataset && manualButton.dataset.focusField) {
        window.setTimeout(function () {
          focusNamedField(manualButton.dataset.focusField);
        }, 180);
      }
      return;
    }
    const autobootLabLink = event.target instanceof Element ? event.target.closest('a[data-autoboot-lab="true"]') : null;
    if (autobootLabLink && window.location.pathname.startsWith('/mission/')) {
      try {
        window.sessionStorage.setItem('research-desk-autoboot:' + autobootLabLink.getAttribute('href'), '1');
      } catch (error) {
        return;
      }
    }
    const anchor = event.target instanceof Element ? event.target.closest('a[href="#manual-controls"]') : null;
    if (!anchor) {
      return;
    }
    event.preventDefault();
    if (history && typeof history.replaceState === 'function') {
      history.replaceState(null, '', '#manual-controls');
    }
    revealManualControls({ instant: false });
  });

  const defaultStageTrigger = document.querySelector('[data-stage-trigger][data-stage-default="true"]') || document.querySelector('[data-stage-trigger]');
  if (defaultStageTrigger) {
    setStageFocus(defaultStageTrigger, { scroll: false });
  }

  const missionAutopilot = document.querySelector('[data-mission-autoplay="true"]');
  const autopilotStatus = document.getElementById('autopilot-status');
  let autopilotCancelled = false;
  let autopilotExecuting = false;
  let autopilotTimer = null;
  let autopilotTickTimer = null;
  let autopilotCancelCopy = 'Autopilot paused. Manual steering is active.';
  let autopilotCancelBound = false;

  function updateAutopilotStatus(message) {
    if (!autopilotStatus || !message) {
      return;
    }
    autopilotStatus.textContent = message;
  }

  function clearAutopilotTimers() {
    if (autopilotTimer) {
      window.clearTimeout(autopilotTimer);
      autopilotTimer = null;
    }
    if (autopilotTickTimer) {
      window.clearInterval(autopilotTickTimer);
      autopilotTickTimer = null;
    }
  }

  function cancelAutopilot(message) {
    if (autopilotExecuting || autopilotCancelled) {
      return;
    }
    autopilotCancelled = true;
    clearAutopilotTimers();
    updateAutopilotStatus(message || autopilotCancelCopy);
  }

  function bindAutopilotCancel(message) {
    if (message) {
      autopilotCancelCopy = message;
    }
    if (autopilotCancelBound) {
      return;
    }
    autopilotCancelBound = true;
    document.addEventListener('pointerdown', function (event) {
      if (autopilotExecuting || autopilotCancelled) {
        return;
      }
      if (!(event.target instanceof Element)) {
        return;
      }
      if (event.target.closest('#loading-overlay')) {
        return;
      }
      cancelAutopilot(autopilotCancelCopy);
    }, true);
    document.addEventListener('keydown', function (event) {
      if (autopilotExecuting || autopilotCancelled) {
        return;
      }
      if (!event.key) {
        return;
      }
      if (event.key === 'Tab' || event.key === 'Shift') {
        return;
      }
      cancelAutopilot(autopilotCancelCopy);
    }, true);
    document.addEventListener('input', function () {
      if (autopilotExecuting || autopilotCancelled) {
        return;
      }
      cancelAutopilot(autopilotCancelCopy);
    }, true);
  }

  function findStageTrigger(name) {
    if (!name) {
      return null;
    }
    const nodes = document.querySelectorAll('[data-stage-trigger]');
    for (const node of nodes) {
      if (node.dataset && node.dataset.stageTrigger === name) {
        return node;
      }
    }
    return null;
  }

  function startAutopilotCountdown(options) {
    const delayMs = Math.max(1200, parseInt(options.delayMs || '0', 10) || 2600);
    let remaining = Math.max(1, Math.ceil(delayMs / 1000));
    const renderStatus = function () {
      if (typeof options.renderStatus === 'function') {
        options.renderStatus(remaining);
      }
    };
    clearAutopilotTimers();
    renderStatus();
    autopilotTickTimer = window.setInterval(function () {
      if (autopilotCancelled || autopilotExecuting) {
        clearAutopilotTimers();
        return;
      }
      remaining -= 1;
      if (remaining > 0) {
        renderStatus();
      }
    }, 1000);
    autopilotTimer = window.setTimeout(function () {
      if (autopilotCancelled || autopilotExecuting) {
        return;
      }
      clearAutopilotTimers();
      autopilotExecuting = true;
      if (typeof options.beforeRun === 'function') {
        options.beforeRun();
      }
      if (typeof options.run === 'function') {
        options.run();
      }
    }, delayMs);
  }

  function queueMissionAutopilot() {
    if (!missionAutopilot || missionAutopilot.dataset.autopilotBlocked === 'true') {
      return;
    }
    const action = missionAutopilot.querySelector('[data-autopilot-action]');
    if (!action) {
      return;
    }
    const label = (action.dataset.autopilotCountdownLabel || action.textContent || 'continue').trim();
    const stageName = (action.dataset.autopilotStage || '').trim();
    const stageTrigger = findStageTrigger(stageName);
    if (stageTrigger) {
      setStageFocus(stageTrigger, { scroll: true });
    }
    bindAutopilotCancel('Autopilot paused. You are steering this stage now.');
    startAutopilotCountdown({
      delayMs: missionAutopilot.dataset.autopilotDelayMs || '2600',
      renderStatus: function (remaining) {
        updateAutopilotStatus('Autopilot moves to ' + label + ' in ' + String(remaining) + '...');
      },
      beforeRun: function () {
        updateAutopilotStatus('Autopilot is moving to ' + label + '.');
        setLoading('Autopilot is moving to ' + label);
      },
      run: function () {
        if (action instanceof HTMLFormElement) {
          const submitButton = action.querySelector('button[type="submit"], button');
          if (typeof action.requestSubmit === 'function') {
            action.requestSubmit(submitButton || undefined);
          } else {
            action.submit();
          }
          return;
        }
        if (action instanceof HTMLAnchorElement) {
          if (action.dataset.autobootLab === 'true' && window.location.pathname.startsWith('/mission/')) {
            try {
              window.sessionStorage.setItem('research-desk-autoboot:' + action.getAttribute('href'), '1');
            } catch (error) {
            }
          }
          window.location.href = action.href;
        }
      }
    });
  }

  document.addEventListener('submit', function (event) {
    const form = event.target;
    if (!form || !(form instanceof HTMLFormElement)) {
      return;
    }
    if (form.dataset.asyncForm === 'true') {
      return;
    }
    setLoading(loadingLabel(form, event.submitter, 'Agent is working'));
  });

  if (missionAutopilot) {
    queueMissionAutopilot();
  }

  const form = document.querySelector('[data-async-form="true"]');
  if (!form) {
    return;
  }

  const transcript = document.getElementById('turn-stream');
  const textarea = form.querySelector('textarea[name="content"]');
  const statusLine = document.getElementById('composer-status');
  const historyKey = 'research-desk-history:' + window.location.pathname;
  let commandHistory = [];
  let historyIndex = -1;
  let historyDraft = '';

  function loadHistory() {
    try {
      const raw = window.localStorage.getItem(historyKey);
      const parsed = raw ? JSON.parse(raw) : [];
      commandHistory = Array.isArray(parsed) ? parsed.filter(function (item) {
        return typeof item === 'string' && item.trim();
      }).slice(-40) : [];
    } catch (error) {
      commandHistory = [];
    }
    historyIndex = commandHistory.length;
  }

  function saveHistory() {
    try {
      window.localStorage.setItem(historyKey, JSON.stringify(commandHistory.slice(-40)));
    } catch (error) {
      return;
    }
  }

  function rememberCommand(value) {
    const clean = (value || '').trim();
    if (!clean) {
      return;
    }
    commandHistory = commandHistory.filter(function (item) {
      return item !== clean;
    });
    commandHistory.push(clean);
    historyIndex = commandHistory.length;
    historyDraft = '';
    saveHistory();
  }

  function applyHistory(index) {
    if (!textarea || !commandHistory.length) {
      return;
    }
    const bounded = Math.max(0, Math.min(index, commandHistory.length));
    historyIndex = bounded;
    if (bounded === commandHistory.length) {
      textarea.value = historyDraft;
    } else {
      textarea.value = commandHistory[bounded];
    }
    window.requestAnimationFrame(function () {
      const length = textarea.value.length;
      textarea.setSelectionRange(length, length);
    });
  }

  function scrollToBottom() {
    if (!transcript) {
      return;
    }
    window.requestAnimationFrame(function () {
      transcript.scrollTop = transcript.scrollHeight;
    });
  }

  function focusLatestTurn(kind) {
    if (!transcript) {
      return;
    }
    let selector = '.turn:last-child';
    if (kind === 'ask') {
      selector = '.turn[data-role="agent"][data-cell-type="markdown"]';
    } else if (kind === 'code') {
      selector = '.turn[data-cell-type="output"]';
    } else if (kind === 'markdown') {
      selector = '.turn[data-role="user"][data-cell-type="markdown"]';
    }
    const matches = transcript.querySelectorAll(selector);
    const target = matches.length ? matches[matches.length - 1] : transcript.lastElementChild;
    if (!target || typeof target.focus !== 'function') {
      return;
    }
    window.requestAnimationFrame(function () {
      target.focus({ preventScroll: true });
      target.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    });
  }

  function setBusy(busy, label) {
    const buttons = form.querySelectorAll('button');
    buttons.forEach(function (button) {
      button.disabled = busy;
    });
    if (!statusLine) {
      return;
    }
    statusLine.textContent = busy ? 'Running ' + label + '...' : 'Ready';
  }

  scrollToBottom();
  loadHistory();
  if (textarea) {
    textarea.focus({ preventScroll: true });
    textarea.addEventListener('keydown', function (event) {
      const selectionStart = textarea.selectionStart || 0;
      const selectionEnd = textarea.selectionEnd || 0;
      const firstBreak = textarea.value.indexOf('\\n');
      const firstLineEnd = firstBreak === -1 ? textarea.value.length : firstBreak;
      const atFirstLine = selectionStart === selectionEnd && selectionStart <= firstLineEnd;
      if (((event.metaKey || event.ctrlKey) && event.key === 'Enter') || (event.shiftKey && event.key === 'Enter')) {
        event.preventDefault();
        const askButton = form.querySelector('button[data-kind="ask"]');
        if (askButton) {
          askButton.click();
        }
        return;
      }
      if (!event.metaKey && !event.ctrlKey && !event.altKey && event.key === 'ArrowUp' && atFirstLine && commandHistory.length) {
        event.preventDefault();
        if (historyIndex === commandHistory.length) {
          historyDraft = textarea.value;
        }
        applyHistory(historyIndex - 1);
        return;
      }
      if (!event.metaKey && !event.ctrlKey && !event.altKey && event.key === 'ArrowDown' && historyIndex < commandHistory.length) {
        event.preventDefault();
        applyHistory(historyIndex + 1);
      }
    });
    textarea.addEventListener('input', function () {
      if (historyIndex !== commandHistory.length) {
        historyIndex = commandHistory.length;
      }
    });
  }

  (function queueCapsuleAutoboot() {
    if (!window.location.pathname.startsWith('/capsule/')) {
      return;
    }
    const autobootReady = form.dataset.autobootReady === 'true';
    const autobootPrompt = (form.dataset.autobootPrompt || '').trim();
    if (!autobootReady || !autobootPrompt) {
      return;
    }
    const storageKey = 'research-desk-autoboot:' + window.location.pathname;
    let shouldAutoboot = '';
    try {
      shouldAutoboot = window.sessionStorage.getItem(storageKey) || '';
      if (shouldAutoboot) {
        window.sessionStorage.removeItem(storageKey);
      }
    } catch (error) {
      shouldAutoboot = '';
    }
    if (!shouldAutoboot) {
      return;
    }
    if (!textarea) {
      return;
    }
    const askButton = form.querySelector('button[data-kind="ask"]');
    if (!askButton) {
      return;
    }
    const labStageTrigger = findStageTrigger('Lab Notes');
    if (labStageTrigger) {
      setStageFocus(labStageTrigger, { scroll: false });
    }
    bindAutopilotCancel('Autopilot paused. You are steering Lab Notes now.');
    startAutopilotCountdown({
      delayMs: '2200',
      renderStatus: function (remaining) {
        if (statusLine) {
          statusLine.textContent = 'Autopilot opens Lab Notes in ' + String(remaining) + '...';
        }
      },
      beforeRun: function () {
        if (statusLine) {
          statusLine.textContent = 'Autopilot is drafting the first notebook turn...';
        }
        textarea.value = autobootPrompt;
        textarea.dispatchEvent(new Event('input', { bubbles: true }));
        textarea.dispatchEvent(new Event('change', { bubbles: true }));
        setLoading('Autopilot is drafting the first notebook turn');
      },
      run: function () {
        askButton.click();
      }
    });
  })();

  form.addEventListener('submit', async function (event) {
    const submitter = event.submitter;
    if (!submitter) {
      return;
    }
    event.preventDefault();

    const kind = submitter.dataset.kind || 'ask';
    if (kind === 'reset' && !window.confirm('Reset the local kernel state for this capsule?')) {
      return;
    }

    const content = textarea ? textarea.value.trim() : '';
    if (!content && kind !== 'reset') {
      if (statusLine) {
        statusLine.textContent = 'Input is empty.';
      }
      return;
    }

    const data = new URLSearchParams();
    if (kind !== 'reset') {
      data.set('content', content);
      if (kind === 'ask' || kind === 'code' || kind === 'markdown') {
        rememberCommand(content);
      }
    }

    setBusy(true, kind);
    setLoading(loadingLabel(form, submitter, 'Agent is working'));
    try {
      const response = await fetch(submitter.formAction, {
        method: 'POST',
        headers: {
          'Accept': 'application/json',
          'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
          'X-Requested-With': 'fetch'
        },
        body: data.toString()
      });
      if (!response.ok) {
        throw new Error('request failed (' + response.status + ')');
      }
      const payload = await response.json();
      if (transcript && typeof payload.turns_html === 'string') {
        transcript.innerHTML = payload.turns_html;
      }
      const panel = document.getElementById('status-panel');
      if (panel && typeof payload.status_html === 'string') {
        panel.outerHTML = payload.status_html;
      }
      if (textarea && (kind === 'ask' || kind === 'markdown' || kind === 'reset')) {
        textarea.value = '';
      }
      if (statusLine) {
        statusLine.textContent = payload.message || 'Ready';
      }
      scrollToBottom();
      if (kind === 'ask' || kind === 'code' || kind === 'markdown' || kind === 'wait') {
        focusLatestTurn(kind);
      } else if (textarea) {
        textarea.focus({ preventScroll: true });
      }
    } catch (error) {
      if (statusLine) {
        statusLine.textContent = error.message || 'request failed';
      }
    } finally {
      setBusy(false, kind);
      clearLoading();
    }
  });
})();
"""
    return """<!doctype html>
<html lang="en" data-reload-token="{reload_token}" data-reload-paused="{reload_paused}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #0a0c0f;
      --panel: rgba(17, 22, 29, 0.92);
      --panel-2: #10151d;
      --ink: #edf2f7;
      --muted: #9da7b7;
      --line: #2a3341;
      --line-strong: #3a4555;
      --accent: #ff6b4a;
      --accent-strong: #ff8768;
      --accent-soft: rgba(255, 107, 74, 0.16);
      --accent-2: #64b4ff;
      --user: #f0c46d;
      --system: #5cd48a;
      --error: #ef7c7c;
      --code: #0d131a;
      --sans: "Space Grotesk", "Avenir Next", "Segoe UI", sans-serif;
      --mono: "IBM Plex Mono", "SF Mono", "Menlo", "Consolas", monospace;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: var(--sans);
      color: var(--ink);
      background:
        radial-gradient(1200px 420px at 12% -8%, rgba(255, 107, 74, 0.15), transparent 58%),
        radial-gradient(900px 340px at 95% 4%, rgba(64, 136, 124, 0.14), transparent 62%),
        linear-gradient(180deg, #121720 0%, #0d1118 44%, var(--bg) 100%);
    }}
    body.loading-active {{
      overflow: hidden;
    }}
    .shell {{
      max-width: 1320px;
      margin: 0 auto;
      min-height: 100vh;
      padding: 18px 18px 24px;
    }}
    .shell-tools {{
      display: flex;
      justify-content: flex-end;
      align-items: center;
      gap: 10px;
      margin-bottom: 12px;
    }}
    h1, h2, h3, p {{ margin: 0; }}
    a {{ color: var(--accent-strong); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    code {{
      color: var(--accent-strong);
      font-family: var(--mono);
    }}
    .topbar {{
      display: flex;
      justify-content: space-between;
      gap: 20px;
      align-items: flex-start;
      margin-bottom: 16px;
      padding: 14px 18px;
      border: 1px solid var(--line);
      border-radius: 18px;
      background: rgba(17, 22, 29, 0.88);
      box-shadow: 0 18px 44px rgba(0, 0, 0, 0.28);
      backdrop-filter: blur(8px);
    }}
    .reload-toggle {{
      border: 1px solid var(--line-strong);
      border-radius: 999px;
      background: rgba(17, 22, 29, 0.88);
      color: var(--ink);
      font-family: var(--mono);
      font-size: 12px;
      padding: 10px 14px;
      cursor: pointer;
      transition: background 120ms ease, border-color 120ms ease, color 120ms ease;
    }}
    .reload-toggle:hover {{
      border-color: rgba(255, 107, 74, 0.38);
      background: rgba(255, 107, 74, 0.08);
    }}
    .reload-toggle[data-state="paused"] {{
      border-color: rgba(240, 196, 109, 0.34);
      background: rgba(240, 196, 109, 0.08);
      color: #f0c46d;
    }}
    .reload-state {{
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      border: 1px solid var(--line);
      padding: 8px 12px;
      color: var(--muted);
      font-family: var(--mono);
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      background: rgba(17, 22, 29, 0.72);
    }}
    .reload-state[data-state="paused"] {{
      color: #f0c46d;
      border-color: rgba(240, 196, 109, 0.24);
      background: rgba(240, 196, 109, 0.08);
    }}
    .reload-state[data-state="error"] {{
      color: var(--error);
      border-color: rgba(239, 124, 124, 0.24);
      background: rgba(239, 124, 124, 0.08);
    }}
    .muted {{ color: var(--muted); }}
    .eyebrow {{
      margin-bottom: 8px;
      color: var(--accent-strong);
      font-family: var(--mono);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }}
    .grid {{
      display: grid;
      grid-template-columns: 286px minmax(0, 1fr);
      gap: 16px;
      align-items: start;
    }}
    .main-column {{
      display: flex;
      flex-direction: column;
      gap: 16px;
      min-width: 0;
    }}
    .panel, .terminal {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 18px;
      box-shadow: 0 18px 44px rgba(0, 0, 0, 0.28);
    }}
    .panel {{
      padding: 18px;
      position: static;
    }}
    .stack {{
      display: flex;
      flex-direction: column;
      gap: 12px;
    }}
    .rail {{
      padding: 16px;
      position: sticky;
      top: 18px;
      align-self: start;
    }}
    .rail-list {{
      list-style: none;
      margin: 16px 0 0;
      padding: 0;
      display: flex;
      flex-direction: column;
      gap: 10px;
    }}
    .rail-item {{
      display: grid;
      grid-template-columns: 12px minmax(0, 1fr);
      gap: 10px;
      align-items: start;
      padding: 10px 0;
      border-top: 1px solid rgba(255, 255, 255, 0.04);
    }}
    .rail-item:first-child {{
      border-top: 0;
      padding-top: 0;
    }}
    .rail-item strong {{
      display: block;
      font-size: 14px;
      margin-bottom: 4px;
    }}
    .rail-item p {{
      color: var(--muted);
      font-size: 13px;
      line-height: 1.45;
    }}
    .rail-dot {{
      width: 10px;
      height: 10px;
      border-radius: 999px;
      margin-top: 4px;
      background: rgba(255, 255, 255, 0.16);
      box-shadow: 0 0 0 3px rgba(255, 255, 255, 0.04);
    }}
    .rail-done .rail-dot {{
      background: #63d99d;
      box-shadow: 0 0 0 3px rgba(99, 217, 157, 0.14);
    }}
    .rail-ready .rail-dot,
    .rail-working .rail-dot,
    .rail-active .rail-dot {{
      background: var(--accent);
      box-shadow: 0 0 0 3px rgba(255, 107, 74, 0.14);
    }}
    .rail-blocked .rail-dot {{
      background: var(--error);
      box-shadow: 0 0 0 3px rgba(239, 124, 124, 0.14);
    }}
    .rail-footer {{
      margin-top: 16px;
      padding-top: 14px;
      border-top: 1px solid var(--line);
    }}
    .autopilot {{
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: flex-start;
      padding: 18px 20px;
      border: 1px solid rgba(255, 107, 74, 0.16);
      border-radius: 18px;
      background:
        linear-gradient(135deg, rgba(255, 107, 74, 0.09), rgba(255, 107, 74, 0.02)),
        rgba(17, 22, 29, 0.94);
      box-shadow: 0 18px 44px rgba(0, 0, 0, 0.22);
    }}
    .autopilot-next {{
      display: flex;
      flex-wrap: wrap;
      justify-content: flex-end;
      gap: 8px;
      align-items: center;
      max-width: 46%;
    }}
    .autopilot-next strong {{
      color: var(--ink);
      font-size: 14px;
      line-height: 1.4;
    }}
    .autopilot-actions {{
      margin-top: 12px;
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      align-items: center;
    }}
    .autopilot-status {{
      margin: 12px 0 0;
      min-height: 1.4em;
    }}
    .reel-panel {{
      padding: 18px;
      overflow: hidden;
    }}
    .scene-dag-shell {{
      overflow-x: auto;
      overflow-y: hidden;
      padding: 4px 2px 10px;
      margin: 0 -2px;
    }}
    .scene-dag {{
      display: grid;
      grid-template-columns: repeat(13, 176px);
      grid-template-rows: auto 20px auto;
      gap: 12px 10px;
      align-items: center;
      width: max-content;
      min-width: max(100%, 176px);
    }}
    .dag-node {{
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 14px;
      background: rgba(16, 21, 29, 0.74);
      min-height: 128px;
    }}
    .dag-node h3 {{
      margin-top: 6px;
      font-size: 15px;
      line-height: 1.3;
    }}
    .dag-node p {{
      margin-top: 8px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.45;
    }}
    .dag-kicker {{
      color: var(--accent-strong);
      font-family: var(--mono);
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }}
    .dag-edge {{
      height: 2px;
      border-radius: 999px;
      background: rgba(255, 255, 255, 0.12);
    }}
    .dag-edge-vertical {{
      width: 2px;
      min-height: 20px;
      justify-self: center;
      border-radius: 999px;
      background: rgba(255, 255, 255, 0.12);
    }}
    .dag-done {{
      border-color: rgba(99, 217, 157, 0.18);
    }}
    .dag-done .dag-kicker {{
      color: #63d99d;
    }}
    .dag-ready, .dag-working, .dag-active {{
      border-color: rgba(255, 107, 74, 0.18);
    }}
    .dag-ready .dag-kicker,
    .dag-working .dag-kicker,
    .dag-active .dag-kicker {{
      color: var(--accent-strong);
    }}
    .dag-blocked {{
      border-color: rgba(239, 124, 124, 0.22);
    }}
    .dag-blocked .dag-kicker {{
      color: var(--error);
    }}
    .dag-muted .dag-kicker {{
      color: var(--muted);
    }}
    .dag-edge-done {{
      background: rgba(99, 217, 157, 0.4);
    }}
    .dag-edge-ready,
    .dag-edge-working,
    .dag-edge-active {{
      background: rgba(255, 107, 74, 0.35);
    }}
    .dag-edge-blocked {{
      background: rgba(239, 124, 124, 0.4);
    }}
    .terminal {{
      display: flex;
      flex-direction: column;
      min-height: calc(100vh - 88px);
      overflow: hidden;
    }}
    .terminal-head {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      padding: 12px 16px;
      border-bottom: 1px solid var(--line);
      background: rgba(255, 107, 74, 0.05);
    }}
    .dataframe-inventory {{
      padding: 16px;
      border-bottom: 1px solid var(--line);
      background: rgba(100, 180, 255, 0.05);
    }}
    .dataframe-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 10px;
      margin-top: 14px;
    }}
    .df-callout {{
      margin-top: 14px;
      padding: 14px;
      border: 1px solid rgba(100, 180, 255, 0.2);
      border-radius: 16px;
      background: rgba(16, 21, 29, 0.72);
    }}
    .df-callout h3 {{
      margin-top: 4px;
      font-size: 18px;
    }}
    .df-callout p {{
      margin-top: 8px;
      color: var(--muted);
      line-height: 1.5;
    }}
    .df-card {{
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 12px;
      background: rgba(16, 21, 29, 0.62);
    }}
    .df-card strong {{
      display: block;
      font-size: 14px;
      margin-bottom: 6px;
    }}
    .df-meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 8px;
    }}
    .df-columns {{
      color: var(--muted);
      font-size: 13px;
      line-height: 1.5;
    }}
    .df-info-preview {{
      margin-top: 12px;
      padding: 12px;
      border-radius: 12px;
      border: 1px solid rgba(255, 255, 255, 0.05);
      background: var(--code);
      color: #d7e0ea;
      font-family: var(--mono);
      font-size: 12px;
      line-height: 1.55;
      white-space: pre-wrap;
      word-break: break-word;
    }}
    .chips {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }}
    .chips-wrap {{
      margin-top: 14px;
    }}
    .chip {{
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 4px 10px;
      font-size: 12px;
      color: var(--muted);
      background: rgba(16, 21, 29, 0.84);
      font-family: var(--mono);
    }}
    .turn-stream {{
      flex: 1;
      overflow: auto;
      padding: 18px;
      display: flex;
      flex-direction: column;
      gap: 12px;
      scroll-behavior: smooth;
    }}
    .turn {{
      border: 1px solid var(--line);
      border-radius: 14px;
      background: rgba(16, 22, 26, 0.86);
      overflow: hidden;
    }}
    .turn-meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      align-items: center;
      padding: 10px 14px;
      border-bottom: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.02);
      font-size: 12px;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }}
    .turn-tag {{
      color: var(--accent);
    }}
    .turn-body {{
      padding: 14px;
      line-height: 1.6;
    }}
    .turn-query .turn-tag {{ color: var(--user); }}
    .turn-code .turn-tag {{ color: var(--accent-2); }}
    .turn-output .turn-tag {{ color: var(--system); }}
    .turn .prompt-line {{
      display: flex;
      gap: 12px;
      align-items: flex-start;
    }}
    .turn .sigil {{
      color: var(--accent);
      font-weight: 700;
    }}
    .turn pre, .turn code {{
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      font: inherit;
    }}
    .turn pre {{
      padding: 12px;
      border-radius: 10px;
      background: var(--code);
      border: 1px solid rgba(255, 255, 255, 0.04);
    }}
    .turn .traceback {{
      margin-top: 10px;
      color: var(--error);
    }}
    .turn details {{
      border: 1px solid var(--line);
      border-radius: 10px;
      background: rgba(255, 255, 255, 0.02);
      padding: 10px 12px;
    }}
    .turn summary {{
      cursor: pointer;
      color: var(--muted);
      user-select: none;
    }}
    .turn summary:hover {{
      color: var(--ink);
    }}
    .turn details[open] summary {{
      margin-bottom: 10px;
    }}
    .markdown p, .markdown li {{
      line-height: 1.6;
    }}
    .markdown p + p {{
      margin-top: 10px;
    }}
    .markdown ul {{
      margin: 10px 0 0;
      padding-left: 20px;
    }}
    .markdown .table-wrap,
    .turn-body .table-wrap {{
      overflow-x: auto;
      margin-top: 10px;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: rgba(12, 17, 20, 0.78);
    }}
    .markdown .ascii-block,
    .turn-body .ascii-block {{
      margin-top: 10px;
      white-space: pre;
      overflow-x: auto;
      word-break: normal;
      line-height: 1.25;
    }}
    .markdown .md-table,
    .turn-body .md-table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }}
    .markdown .md-table thead,
    .turn-body .md-table thead {{
      background: rgba(93, 211, 158, 0.08);
    }}
    .markdown .md-table th,
    .markdown .md-table td,
    .turn-body .md-table th,
    .turn-body .md-table td {{
      padding: 10px 12px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
      white-space: nowrap;
    }}
    .markdown .md-table tbody tr:last-child td,
    .turn-body .md-table tbody tr:last-child td {{
      border-bottom: 0;
    }}
    .stdout-block {{
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      overflow-x: auto;
      font-family: var(--mono);
      font-size: 12px;
      line-height: 1.45;
      color: #d7e0ea;
    }}
    .df-info-card {{
      display: grid;
      gap: 10px;
    }}
    .df-info-title {{
      font-size: 13px;
      color: var(--muted);
      font-family: var(--mono);
    }}
    .empty-state {{
      border: 1px dashed var(--line);
      border-radius: 14px;
      padding: 18px;
      color: var(--muted);
    }}
    .composer {{
      border-top: 1px solid var(--line);
      padding: 14px 16px 16px;
      background: rgba(17, 22, 29, 0.96);
      backdrop-filter: blur(10px);
    }}
    .dock-head {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: flex-start;
      margin-bottom: 10px;
    }}
    .composer-label {{
      display: block;
      margin-bottom: 6px;
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.06em;
    }}
    textarea {{
      width: 100%;
      min-height: 96px;
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 14px;
      font: inherit;
      background: var(--panel-2);
      color: var(--ink);
      resize: vertical;
      outline: none;
    }}
    textarea:focus {{
      border-color: rgba(255, 107, 74, 0.55);
      box-shadow: 0 0 0 3px rgba(255, 107, 74, 0.10);
    }}
    .actions {{
      display: flex;
      gap: 12px;
      margin-top: 12px;
      flex-wrap: wrap;
    }}
    button {{
      border: 1px solid transparent;
      border-radius: 999px;
      padding: 10px 16px;
      font: inherit;
      color: white;
      background: linear-gradient(135deg, var(--accent), var(--accent-strong));
      cursor: pointer;
    }}
    button.secondary {{
      color: var(--ink);
      background: rgba(138, 180, 255, 0.16);
      border-color: rgba(138, 180, 255, 0.24);
    }}
    button.ghost {{
      color: var(--muted);
      background: transparent;
      border-color: var(--line);
    }}
    button:disabled {{
      opacity: 0.7;
      cursor: wait;
    }}
    .composer-status {{
      margin-top: 10px;
      color: var(--muted);
      font-size: 12px;
    }}
    .status-ok {{ color: var(--accent); }}
    .status-error {{ color: var(--error); }}
    .capsules {{
      display: grid;
      gap: 16px;
    }}
    .capsule-card {{
      padding: 18px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 18px;
    }}
    .center-stage {{
      display: flex;
      justify-content: center;
      width: 100%;
      margin: 16px 0;
    }}
    .center-stage > * {{
      width: min(920px, 100%);
    }}
    .mission-layout {{
      display: grid;
      grid-template-columns: minmax(0, 1.35fr) minmax(320px, 0.95fr);
      gap: 16px;
      align-items: start;
    }}
    .mission-panel {{
      width: 100%;
      margin: 0 auto;
      align-self: center;
    }}
    .mission-panel .section-head {{
      justify-content: center;
      text-align: center;
    }}
    .mission-panel .section-head > div {{
      max-width: 760px;
      margin: 0 auto;
    }}
    .mission-panel > p.muted {{
      max-width: 760px;
      margin: 0 auto 14px;
      text-align: center;
    }}
    .mission-panel details,
    .mission-panel .mission-form,
    .mission-panel .advanced-planning {{
      text-align: left;
    }}
    .mission-panel-create {{
      text-align: center;
    }}
    .mission-panel-create .section-head > div {{
      max-width: 720px;
      margin: 0 auto;
    }}
    .mission-panel-create .mission-form {{
      max-width: 760px;
      margin: 0 auto;
    }}
    .mission-panel-create .field {{
      text-align: left;
    }}
    .mission-panel-create .mission-actions {{
      justify-content: center;
    }}
    .launch-input-shell {{
      margin-top: 10px;
      padding: 16px;
      border-radius: 20px;
      border: 1px solid rgba(255, 107, 74, 0.32);
      background:
        radial-gradient(circle at top left, rgba(255, 107, 74, 0.14), transparent 34%),
        linear-gradient(180deg, rgba(13, 18, 25, 0.98), rgba(10, 14, 20, 0.96));
      box-shadow:
        0 18px 44px rgba(0, 0, 0, 0.24),
        0 0 0 1px rgba(255, 107, 74, 0.08);
    }}
    .launch-input-kicker {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      margin-bottom: 10px;
    }}
    .launch-input-kicker strong {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      color: var(--ink);
      font-size: 14px;
    }}
    .launch-input-kicker strong::before {{
      content: "1";
      display: inline-grid;
      place-items: center;
      width: 24px;
      height: 24px;
      border-radius: 999px;
      background: linear-gradient(135deg, var(--accent), var(--accent-strong));
      color: white;
      font-family: var(--mono);
      font-size: 12px;
    }}
    .launch-input-kicker span {{
      color: var(--accent-strong);
      font-family: var(--mono);
      font-size: 11px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }}
    .launch-prompt-field textarea {{
      min-height: 132px;
      border-width: 2px;
      border-color: rgba(255, 107, 74, 0.34);
      background:
        linear-gradient(180deg, rgba(20, 25, 33, 1), rgba(10, 14, 20, 1)),
        var(--panel-2);
      box-shadow:
        inset 0 1px 0 rgba(255, 255, 255, 0.03),
        0 0 0 1px rgba(255, 107, 74, 0.06);
    }}
    .launch-prompt-field textarea::placeholder {{
      color: rgba(237, 242, 247, 0.45);
    }}
    .launch-prompt-field textarea:focus {{
      border-color: rgba(255, 107, 74, 0.72);
      box-shadow:
        0 0 0 4px rgba(255, 107, 74, 0.14),
        0 20px 40px rgba(255, 107, 74, 0.12);
    }}
    .mission-main, .mission-side {{
      display: flex;
      flex-direction: column;
      gap: 16px;
    }}
    .attention-panel {{
      border: 1px solid rgba(255, 107, 74, 0.32);
      background:
        radial-gradient(circle at top left, rgba(255, 107, 74, 0.12), transparent 46%),
        rgba(17, 22, 29, 0.94);
      animation: attentionPulse 2.4s ease-in-out infinite;
    }}
    .attention-panel .actions {{
      justify-content: center;
    }}
    .mission-popup {{
      position: fixed;
      right: 22px;
      bottom: 22px;
      width: min(430px, calc(100vw - 28px));
      z-index: 40;
      padding: 16px 18px;
      border: 1px solid rgba(255, 107, 74, 0.38);
      border-radius: 18px;
      background:
        linear-gradient(135deg, rgba(255, 107, 74, 0.14), rgba(255, 107, 74, 0.03)),
        rgba(14, 18, 25, 0.98);
      box-shadow: 0 18px 44px rgba(0, 0, 0, 0.42);
      backdrop-filter: blur(10px);
      animation: popupIn 180ms ease-out;
    }}
    .mission-popup[hidden] {{
      display: none;
    }}
    .mission-popup h2 {{
      margin-top: 4px;
      font-size: 20px;
      line-height: 1.25;
    }}
    .mission-popup p {{
      margin-top: 10px;
      line-height: 1.5;
    }}
    .mission-popup .chips {{
      margin-top: 12px;
    }}
    .mission-popup .actions {{
      margin-top: 14px;
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
    }}
    .mission-popup .button-link {{
      cursor: pointer;
    }}
    @keyframes popupIn {{
      from {{
        transform: translateY(10px);
        opacity: 0;
      }}
      to {{
        transform: translateY(0);
        opacity: 1;
      }}
    }}
    .loading-overlay {{
      position: fixed;
      inset: 0;
      z-index: 999;
      display: grid;
      place-items: center;
      background: rgba(7, 10, 15, 0.64);
      backdrop-filter: blur(10px);
    }}
    .loading-overlay[hidden] {{
      display: none;
    }}
    .loading-card {{
      width: min(420px, calc(100vw - 32px));
      padding: 22px 22px 18px;
      border: 1px solid rgba(255, 107, 74, 0.32);
      border-radius: 22px;
      background:
        radial-gradient(circle at top left, rgba(255, 107, 74, 0.16), transparent 42%),
        rgba(17, 22, 29, 0.96);
      box-shadow: 0 28px 80px rgba(0, 0, 0, 0.42);
      text-align: left;
    }}
    .loading-spinner {{
      width: 34px;
      height: 34px;
      border-radius: 999px;
      border: 3px solid rgba(255, 255, 255, 0.12);
      border-top-color: var(--accent);
      animation: spin 0.9s linear infinite;
      margin-bottom: 14px;
    }}
    @keyframes spin {{
      to {{ transform: rotate(360deg); }}
    }}
    .loading-card h2 {{
      font-size: 24px;
      margin-bottom: 8px;
    }}
    .loading-card p {{
      color: var(--muted);
      line-height: 1.55;
    }}
    @keyframes attentionPulse {{
      0% {{ transform: translateY(0); box-shadow: 0 18px 44px rgba(0, 0, 0, 0.24); }}
      50% {{ transform: translateY(-2px); box-shadow: 0 22px 50px rgba(255, 107, 74, 0.16); }}
      100% {{ transform: translateY(0); box-shadow: 0 18px 44px rgba(0, 0, 0, 0.24); }}
    }}
    .section-head {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: flex-start;
      margin-bottom: 16px;
    }}
    .mission-form {{
      display: flex;
      flex-direction: column;
      gap: 16px;
    }}
    .advanced-planning {{
      border: 1px solid var(--line);
      border-radius: 14px;
      background: rgba(16, 21, 29, 0.62);
      padding: 14px;
    }}
    .advanced-planning summary {{
      cursor: pointer;
      color: var(--accent-strong);
      font-family: var(--mono);
      list-style: none;
    }}
    .advanced-planning summary::-webkit-details-marker {{
      display: none;
    }}
    .advanced-planning[open] summary {{
      margin-bottom: 10px;
    }}
    .manual-controls-body {{
      display: grid;
      gap: 12px;
    }}
    .mission-panel .advanced-planning[open] {{
      max-height: min(72vh, 760px);
      overflow: hidden;
    }}
    .mission-panel .advanced-planning[open] .manual-controls-body {{
      max-height: calc(min(72vh, 760px) - 74px);
      overflow-y: auto;
      padding-right: 6px;
    }}
    .field-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 14px;
    }}
    .field {{
      display: flex;
      flex-direction: column;
      gap: 8px;
    }}
    .field span {{
      color: var(--muted);
      font-family: var(--mono);
      font-size: 12px;
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }}
    .field-wide {{
      grid-column: 1 / -1;
    }}
    input[type="text"],
    input[type="number"] {{
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 12px 14px;
      font: inherit;
      background: var(--panel-2);
      color: var(--ink);
      outline: none;
    }}
    input[type="text"]:focus,
    input[type="number"]:focus {{
      border-color: rgba(255, 107, 74, 0.55);
      box-shadow: 0 0 0 3px rgba(255, 107, 74, 0.10);
    }}
    .route-list {{
      display: flex;
      flex-direction: column;
      gap: 12px;
    }}
    .route-card {{
      border: 1px solid var(--line);
      border-radius: 14px;
      background: rgba(16, 21, 29, 0.78);
      padding: 14px;
    }}
    .route-select {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      margin-bottom: 10px;
      font-size: 13px;
      color: var(--muted);
    }}
    .route-meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 10px;
    }}
    .route-url {{
      font-family: var(--mono);
      font-size: 13px;
      color: var(--ink);
      word-break: break-word;
    }}
    .mission-process {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
      margin-bottom: 14px;
    }}
    .process-step {{
      border: 1px solid var(--line);
      border-radius: 14px;
      background: rgba(16, 21, 29, 0.58);
      padding: 12px;
    }}
    .process-step strong {{
      display: block;
      margin-bottom: 6px;
      color: var(--ink);
      font-size: 13px;
    }}
    .process-step span {{
      color: var(--muted);
      font-size: 13px;
      line-height: 1.5;
    }}
    .status-list {{
      display: grid;
      gap: 10px;
      margin-top: 14px;
    }}
    .status-row {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      color: var(--muted);
      font-size: 13px;
    }}
    .status-row strong {{
      color: var(--ink);
      font-weight: 600;
    }}
    .status-actions {{
      margin-top: 16px;
      padding-top: 14px;
      border-top: 1px solid var(--line);
    }}
    .status-actions ul {{
      margin: 10px 0 0;
      padding-left: 18px;
      color: var(--muted);
    }}
    .button-link {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border: 1px solid transparent;
      border-radius: 999px;
      padding: 10px 16px;
      font: inherit;
      text-decoration: none;
    }}
    .button-link:hover {{
      text-decoration: none;
    }}
    .button-link.secondary {{
      color: white;
      background: linear-gradient(135deg, var(--accent), var(--accent-strong));
    }}
    .button-link.ghost {{
      color: var(--muted);
      background: transparent;
      border-color: var(--line);
    }}
    .inline-actions {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      margin-top: 12px;
    }}
    .mission-actions {{
      margin-top: 4px;
    }}
    .shape-next-step {{
      margin-top: 16px;
      padding-top: 14px;
      border-top: 1px solid var(--line);
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      flex-wrap: wrap;
    }}
    .shape-next-step p {{
      margin: 0;
      color: var(--muted);
    }}
    .readiness-copy {{
      margin-top: 12px;
    }}
    body::before {{
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      background-image:
        linear-gradient(rgba(100, 180, 255, 0.05) 1px, transparent 1px),
        linear-gradient(90deg, rgba(100, 180, 255, 0.05) 1px, transparent 1px);
      background-size: 48px 48px;
      mask-image: linear-gradient(180deg, rgba(0, 0, 0, 0.72), transparent 92%);
      opacity: 0.38;
    }}
    body::after {{
      content: "";
      position: fixed;
      inset: -12% -10%;
      pointer-events: none;
      background:
        radial-gradient(circle at 18% 18%, rgba(255, 107, 74, 0.16), transparent 26%),
        radial-gradient(circle at 80% 10%, rgba(100, 180, 255, 0.12), transparent 24%),
        radial-gradient(circle at 55% 72%, rgba(92, 212, 138, 0.08), transparent 24%);
      filter: blur(18px);
      animation: glowDrift 16s ease-in-out infinite alternate;
      opacity: 0.8;
    }}
    .shell {{
      position: relative;
      z-index: 1;
    }}
    .topbar,
    .panel,
    .terminal,
    .capsule-card,
    .route-card,
    .df-card,
    .turn {{
      transition:
        transform 180ms ease,
        border-color 180ms ease,
        box-shadow 220ms ease,
        background 220ms ease;
      animation: revealUp 420ms ease both;
    }}
    .panel:hover,
    .capsule-card:hover,
    .route-card:hover,
    .df-card:hover,
    .turn:hover {{
      transform: translateY(-3px);
      border-color: rgba(255, 107, 74, 0.22);
      box-shadow: 0 24px 56px rgba(0, 0, 0, 0.34);
    }}
    .topbar {{
      overflow: hidden;
      position: relative;
    }}
    .topbar::after,
    .mission-panel-create::after,
    .board-spotlight::after,
    .terminal::before {{
      content: "";
      position: absolute;
      inset: 0;
      pointer-events: none;
      background: linear-gradient(120deg, transparent 0%, rgba(255, 255, 255, 0.05) 45%, transparent 60%);
      transform: translateX(-120%);
      animation: shimmerSweep 11s ease-in-out infinite;
    }}
    .mission-panel-create {{
      position: relative;
      overflow: hidden;
      background:
        radial-gradient(circle at top center, rgba(255, 107, 74, 0.12), transparent 30%),
        rgba(17, 22, 29, 0.92);
    }}
    .launch-metrics {{
      display: flex;
      flex-wrap: wrap;
      justify-content: center;
      gap: 10px;
      margin-top: 16px;
    }}
    .spark-strip {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 10px;
      margin-top: 16px;
    }}
    .spark-chip {{
      border: 1px solid rgba(255, 107, 74, 0.2);
      border-radius: 18px;
      padding: 14px 16px;
      background:
        linear-gradient(180deg, rgba(255, 107, 74, 0.14), rgba(100, 180, 255, 0.04)),
        rgba(17, 22, 29, 0.9);
      color: var(--ink);
      font: inherit;
      cursor: pointer;
      text-align: left;
      box-shadow:
        inset 0 0 0 1px rgba(255, 255, 255, 0.03),
        0 12px 28px rgba(0, 0, 0, 0.18);
      min-height: 110px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
    }}
    .spark-chip:hover,
    .spark-chip:focus-visible {{
      border-color: rgba(255, 107, 74, 0.44);
      background:
        linear-gradient(180deg, rgba(255, 107, 74, 0.2), rgba(100, 180, 255, 0.08)),
        rgba(17, 22, 29, 0.96);
      transform: translateY(-3px);
      outline: none;
    }}
    .spark-chip strong {{
      display: block;
      font-size: 13px;
      margin-bottom: 4px;
    }}
    .spark-chip span {{
      display: block;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.4;
    }}
    .spark-chip-action {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      margin-top: 14px;
      color: var(--accent-strong);
      font-family: var(--mono);
      font-size: 11px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }}
    .spark-chip-action::after {{
      content: "Load Prompt";
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 92px;
      border-radius: 999px;
      padding: 6px 10px;
      border: 1px solid rgba(255, 107, 74, 0.28);
      background: rgba(255, 107, 74, 0.1);
    }}
    .capsule-card {{
      position: relative;
      overflow: hidden;
      background:
        linear-gradient(180deg, rgba(17, 22, 29, 0.94), rgba(12, 17, 24, 0.88)),
        var(--panel);
    }}
    .capsule-card::before {{
      content: "";
      position: absolute;
      inset: 0;
      pointer-events: none;
      background: radial-gradient(circle at top right, rgba(255, 107, 74, 0.10), transparent 28%);
    }}
    .capsule-card-head {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: flex-start;
    }}
    .capsule-card-next {{
      max-width: 220px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
      text-align: right;
    }}
    .capsule-meter {{
      position: relative;
      width: 100%;
      height: 10px;
      border-radius: 999px;
      margin: 16px 0 14px;
      background: rgba(255, 255, 255, 0.06);
      overflow: hidden;
    }}
    .capsule-meter span,
    .board-meter-fill {{
      display: block;
      height: 100%;
      border-radius: inherit;
      background: linear-gradient(90deg, rgba(100, 180, 255, 0.86), rgba(255, 107, 74, 0.94));
      box-shadow: 0 0 18px rgba(255, 107, 74, 0.28);
      transition: width 240ms ease;
    }}
    .board-spotlight {{
      position: relative;
      overflow: hidden;
      background:
        radial-gradient(circle at top right, rgba(100, 180, 255, 0.08), transparent 28%),
        linear-gradient(135deg, rgba(255, 107, 74, 0.10), rgba(255, 107, 74, 0.02)),
        rgba(17, 22, 29, 0.94);
    }}
    .board-spotlight-head {{
      display: flex;
      justify-content: space-between;
      gap: 18px;
      align-items: flex-start;
    }}
    .board-meter-shell {{
      min-width: 180px;
      max-width: 220px;
    }}
    .board-meter {{
      height: 12px;
      border-radius: 999px;
      background: rgba(255, 255, 255, 0.08);
      overflow: hidden;
      margin-bottom: 10px;
    }}
    .board-meter-copy {{
      color: var(--muted);
      font-size: 12px;
      line-height: 1.5;
    }}
    .board-stage-copy {{
      margin-top: 8px;
      color: var(--muted);
      line-height: 1.6;
    }}
    .playbook-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 12px;
      margin-top: 12px;
    }}
    .playbook-card {{
      width: 100%;
      border: 1px solid rgba(255, 107, 74, 0.18);
      border-radius: 18px;
      padding: 16px;
      background:
        linear-gradient(180deg, rgba(255, 107, 74, 0.08), rgba(255, 107, 74, 0.02)),
        rgba(16, 21, 29, 0.88);
      color: var(--ink);
      text-align: left;
      cursor: pointer;
    }}
    .playbook-card strong {{
      display: block;
      margin-bottom: 6px;
      font-size: 15px;
    }}
    .playbook-card p {{
      color: var(--muted);
      font-size: 13px;
      line-height: 1.5;
    }}
    .playbook-card small {{
      display: inline-flex;
      margin-top: 10px;
      color: var(--accent-strong);
      font-family: var(--mono);
      font-size: 11px;
      letter-spacing: 0.06em;
      text-transform: uppercase;
    }}
    .playbook-card:hover,
    .playbook-card.is-active {{
      transform: translateY(-2px);
      border-color: rgba(255, 107, 74, 0.44);
      box-shadow: 0 18px 36px rgba(255, 107, 74, 0.14);
    }}
    .stage-trigger {{
      cursor: pointer;
    }}
    .stage-trigger:focus-visible {{
      outline: 2px solid rgba(100, 180, 255, 0.65);
      outline-offset: 2px;
    }}
    .rail-item .stage-trigger {{
      width: 100%;
      display: grid;
      grid-template-columns: 12px minmax(0, 1fr);
      gap: 10px;
      align-items: start;
      background: transparent;
      border: 0;
      padding: 0;
      color: inherit;
      text-align: left;
      font: inherit;
    }}
    .rail-item .stage-trigger.is-active strong,
    .rail-item .stage-trigger.is-active p {{
      color: var(--ink);
    }}
    .rail-item .stage-trigger.is-active .rail-dot {{
      box-shadow: 0 0 0 4px rgba(255, 107, 74, 0.18), 0 0 24px rgba(255, 107, 74, 0.22);
    }}
    .dag-node.stage-trigger {{
      width: 100%;
      text-align: left;
      color: var(--ink);
      font: inherit;
      cursor: pointer;
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 14px;
      background: rgba(16, 21, 29, 0.74);
    }}
    .dag-node.stage-trigger.is-active {{
      border-color: rgba(255, 107, 74, 0.48);
      box-shadow: 0 18px 42px rgba(255, 107, 74, 0.14);
      transform: translateY(-2px) scale(1.01);
    }}
    .stage-section {{
      position: relative;
      scroll-margin-top: 28px;
    }}
    .stage-section.is-stage-active {{
      border-color: rgba(255, 107, 74, 0.4);
      box-shadow: 0 0 0 1px rgba(255, 107, 74, 0.18), 0 24px 54px rgba(0, 0, 0, 0.34);
    }}
    .stage-section.is-stage-active::after {{
      content: "";
      position: absolute;
      inset: 0;
      border-radius: inherit;
      pointer-events: none;
      box-shadow: inset 0 0 0 1px rgba(100, 180, 255, 0.16);
    }}
    .route-card {{
      position: relative;
      overflow: hidden;
    }}
    .route-card:has(input:checked) {{
      border-color: rgba(255, 107, 74, 0.36);
      background:
        linear-gradient(180deg, rgba(255, 107, 74, 0.10), rgba(16, 21, 29, 0.78)),
        rgba(16, 21, 29, 0.78);
      box-shadow: 0 16px 32px rgba(255, 107, 74, 0.10);
    }}
    .route-card:has(input:not(:checked)) {{
      opacity: 0.78;
    }}
    .route-select input {{
      accent-color: var(--accent);
    }}
    .terminal {{
      position: relative;
    }}
    .terminal::before {{
      opacity: 0.4;
    }}
    .composer-sparks {{
      margin-bottom: 14px;
    }}
    .composer textarea,
    .mission-form textarea {{
      background:
        linear-gradient(180deg, rgba(16, 21, 29, 0.98), rgba(11, 16, 22, 0.96)),
        var(--panel-2);
    }}
    .dataframe-shell {{
      border: 1px solid var(--line);
      border-radius: 18px;
      overflow: hidden;
      background: rgba(100, 180, 255, 0.05);
    }}
    .dataframe-shell summary {{
      list-style: none;
      cursor: pointer;
      padding: 16px 18px;
      background: linear-gradient(180deg, rgba(100, 180, 255, 0.08), rgba(100, 180, 255, 0.03));
    }}
    .dataframe-shell summary::-webkit-details-marker {{
      display: none;
    }}
    .dataframe-shell[open] summary {{
      border-bottom: 1px solid var(--line);
    }}
    .dataframe-shell .dataframe-inventory {{
      border-bottom: 0;
      background: transparent;
    }}
    button,
    .button-link {{
      transition: transform 140ms ease, box-shadow 180ms ease, filter 180ms ease, border-color 180ms ease;
    }}
    button:hover,
    .button-link:hover {{
      transform: translateY(-1px);
      box-shadow: 0 10px 24px rgba(255, 107, 74, 0.16);
      filter: saturate(1.08);
    }}
    .button-link.ghost:hover,
    button.ghost:hover {{
      box-shadow: none;
      border-color: rgba(100, 180, 255, 0.22);
      background: rgba(100, 180, 255, 0.08);
    }}
    @keyframes revealUp {{
      from {{
        opacity: 0;
        transform: translateY(14px);
      }}
      to {{
        opacity: 1;
        transform: translateY(0);
      }}
    }}
    @keyframes shimmerSweep {{
      0%, 72%, 100% {{
        transform: translateX(-120%);
      }}
      84% {{
        transform: translateX(120%);
      }}
    }}
    @keyframes glowDrift {{
      from {{
        transform: translate3d(-2%, 0, 0) scale(1);
      }}
      to {{
        transform: translate3d(2%, 2%, 0) scale(1.03);
      }}
    }}
    @media (max-width: 880px) {{
      .grid {{ grid-template-columns: 1fr; }}
      .main-column {{ min-width: 0; }}
      .autopilot {{ flex-direction: column; }}
      .autopilot-next {{ max-width: 100%; justify-content: flex-start; }}
      .mission-layout {{ grid-template-columns: 1fr; }}
      .field-grid {{ grid-template-columns: 1fr; }}
      .mission-process {{ grid-template-columns: 1fr; }}
      .panel {{ position: static; }}
      .terminal {{ min-height: auto; }}
      .turn-stream {{ max-height: none; min-height: 50vh; }}
      .dock-head {{ flex-direction: column; }}
      .board-spotlight-head {{ flex-direction: column; }}
      .board-meter-shell,
      .capsule-card-next {{ max-width: 100%; min-width: 0; text-align: left; }}
      .scene-dag {{
        grid-template-columns: 1fr;
        grid-template-rows: none;
      }}
      .dag-edge,
      .dag-edge-vertical {{
        display: none;
      }}
    }}
  </style>
</head>
<body>
  <div id="loading-overlay" class="loading-overlay" hidden>
    <div class="loading-card" role="status" aria-live="polite" aria-busy="true">
      <div class="loading-spinner" aria-hidden="true"></div>
      <div class="eyebrow">Autopilot</div>
      <h2 id="loading-title">Agent is working</h2>
      <p id="loading-copy">The system is actively planning or executing the next stage. You do not need to intervene unless you want to redirect it.</p>
    </div>
  </div>
  <div class="shell">
    <div class="shell-tools">
      <span id="reload-state" class="reload-state" data-state="{reload_state}">{reload_label}</span>
      <button type="button" class="reload-toggle" data-reload-toggle data-state="{reload_state}" aria-pressed="{reload_pressed}">{reload_toggle}</button>
    </div>
    {body}
  </div>
  <script>{script}</script>
</body>
</html>
""".format(
        title=html.escape(title),
        body=body,
        script=script,
        reload_token=html.escape(_reload_token()),
        reload_paused="1" if reload_paused else "0",
        reload_state="paused" if reload_paused else "live",
        reload_label="paused while you edit" if reload_paused else "live",
        reload_pressed="true" if reload_paused else "false",
        reload_toggle="Resume hot reload" if reload_paused else "Pause hot reload",
    )


def _reload_token() -> str:
    latest_mtime_ns = 0
    file_count = 0
    for root in HOT_RELOAD_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            try:
                stat = path.stat()
            except OSError:
                continue
            file_count += 1
            latest_mtime_ns = max(latest_mtime_ns, getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000)))
    return "{latest}:{count}".format(latest=latest_mtime_ns, count=file_count)


def _reload_status_payload() -> dict[str, Any]:
    return {
        "token": _reload_token(),
        "paused": is_reload_paused(),
    }


def _resolve_capsule_dir(capsule_name: str) -> Path:
    raw_name = unquote(str(capsule_name)).strip().strip("/")
    if not raw_name:
        raise ValueError("Missing capsule name.")
    candidate = (CAPSULES_ROOT / raw_name).resolve()
    root = CAPSULES_ROOT.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("Invalid capsule path.") from exc
    return candidate


def _capsule_dirs() -> list[Path]:
    if not CAPSULES_ROOT.exists():
        return []
    return sorted(
        [
            path
            for path in CAPSULES_ROOT.iterdir()
            if path.is_dir() and not path.name.startswith("_") and (path / "manifest.json").exists()
        ]
    )


def _capsule_updated_at(capsule_dir: Path) -> float:
    latest = 0.0
    for name in (
        "readiness.json",
        "object_manifest.json",
        "capsule_state.json",
        "manifest.json",
        "task_spec.json",
    ):
        path = capsule_dir / name
        try:
            latest = max(latest, path.stat().st_mtime)
        except OSError:
            continue
    if latest > 0:
        return latest
    try:
        return capsule_dir.stat().st_mtime
    except OSError:
        return 0.0


def _capsule_summary_payload(capsule_dir: Path) -> dict[str, Any]:
    from .cli import read_json

    manifest = read_json(capsule_dir / "manifest.json", {})
    capsule_state = read_json(capsule_dir / "capsule_state.json", {})
    readiness = read_json(capsule_dir / "readiness.json", {})
    object_manifest = read_json(capsule_dir / "object_manifest.json", {})
    task_spec = read_json(capsule_dir / "task_spec.json", {})
    source_plan = read_json(capsule_dir / "source_plan.json", {})

    snapshot = {
        "manifest": manifest,
        "capsule_state": capsule_state,
        "readiness": readiness,
        "object_manifest": object_manifest,
        "task_spec": task_spec,
        "source_plan": source_plan,
    }
    primary = _primary_object(snapshot)
    target = _first_target(task_spec)
    return {
        "capsule_name": str(manifest.get("name", capsule_dir.name)).strip() or capsule_dir.name,
        "task": str(manifest.get("task", "")).strip(),
        "stage": str(capsule_state.get("stage", "planning")).strip() or "planning",
        "workflow_status": str(capsule_state.get("status", "planned")).strip() or "planned",
        "readiness_status": str(readiness.get("overall_status", "planned")).strip() or "planned",
        "primary_object_name": str(primary.get("name", target.get("name", ""))).strip(),
        "primary_row_count": int(primary.get("row_count", 0) or 0),
        "primary_object_status": str(primary.get("status", "")).strip(),
        "next_step": _autopilot_next(snapshot),
        "mission_url": "/mission/{name}".format(name=quote(capsule_dir.name)),
        "capsule_url": "/capsule/{name}".format(name=quote(capsule_dir.name)),
        "updated_at_epoch": _capsule_updated_at(capsule_dir),
    }


def _research_desk_capsules_payload(*, limit: int = 12) -> dict[str, Any]:
    capsule_dirs = sorted(_capsule_dirs(), key=_capsule_updated_at, reverse=True)
    summaries = [_capsule_summary_payload(capsule_dir) for capsule_dir in capsule_dirs[: max(0, limit)]]
    return {
        "ok": True,
        "product": "research_desk",
        "capsule_count": len(capsule_dirs),
        "capsules": summaries,
    }


def _research_desk_status_payload() -> dict[str, Any]:
    from .cli import load_setup_config

    config = load_setup_config()
    env_values = parse_env_file(DEFAULT_AGENT_ENV_PATH)
    api_key = (
        str(config.get("env_defaults", {}).get("UNCHAINED_API_KEY", "")).strip()
        or str(env_values.get("UNCHAINED_API_KEY", "")).strip()
    )
    endpoint = (
        os.environ.get("UNCHAINED_MCP_ENDPOINT", "").strip()
        or str(config.get("mcp_endpoint", "")).strip()
        or str(env_values.get("UNCHAINED_MCP_ENDPOINT", "")).strip()
        or DEFAULT_ENDPOINT
    )
    agent_id = (
        os.environ.get("UNCHAINED_AGENT_ID", "").strip()
        or str(env_values.get("UNCHAINED_AGENT_ID", "")).strip()
    )
    agents_endpoint = infer_agents_endpoint(endpoint)
    credential_source = "missing"
    if os.environ.get("UNCHAINED_API_KEY") or os.environ.get("UNCHAINED_AGENT_ID"):
        credential_source = "env"
    elif env_values.get("UNCHAINED_API_KEY") or env_values.get("UNCHAINED_AGENT_ID"):
        credential_source = "agent-env-file"
    elif api_key:
        credential_source = "config"
    agent_resolution = "provided" if agent_id else "missing"
    agent_resolution_error = ""
    # Do not pin the browser-side agent to a stale setup snapshot. If live
    # credential resolution is slow or unavailable, fall back to the local
    # client env values instead of failing the status endpoint.
    try:
        resolved = resolve_credentials(
            api_key=api_key or None,
            agent_id=agent_id or None,
            endpoint=endpoint,
            timeout=3,
        )
    except Exception as exc:
        resolved = None
        if api_key and not agent_id:
            agent_resolution = "error"
            agent_resolution_error = str(exc)
    if resolved is not None:
        api_key = str(resolved.api_key or api_key or "").strip()
        agent_id = str(resolved.agent_id or agent_id or "").strip()
        agents_endpoint = str(resolved.agents_endpoint or agents_endpoint).strip() or agents_endpoint
        resolved_source = str(resolved.source or credential_source).strip() or credential_source
        if credential_source == "config" and resolved_source == "flags":
            resolved_source = "config"
        credential_source = resolved_source
        agent_resolution = str(resolved.agent_resolution or agent_resolution).strip() or agent_resolution
        agent_resolution_error = str(resolved.agent_resolution_error or "").strip()
    pyreplab_bin = (
        os.environ.get("PYREPLAB_BIN", "").strip()
        or str(config.get("pyreplab_bin", "")).strip()
        or str(discover_pyreplab_bin() or "").strip()
    )
    provider = str(config.get("provider", "")).strip() or detect_agent_mode()
    provider_mode = str(config.get("lab_provider_mode", "")).strip() or detect_agent_mode()
    browser_client = str(config.get("browser_client", "")).strip()
    api_base = infer_api_base(endpoint)
    trial_api_base = os.environ.get("UNCHAINED_PYREPLAB_TRIAL_API_BASE", "").strip() or api_base
    trial_route_prefix = str(next(iter(UNCHAINED_TRIAL_PATHS.values()), "/research-desk/agent")).rsplit("/", 1)[0]
    trial_configured = provider == "trial" or provider_mode == "trial"
    trial_enabled = trial_configured or bool(os.environ.get("UNCHAINED_PYREPLAB_TRIAL_ENABLED", "").strip())
    missing: list[str] = []
    if not api_key:
        missing.append("api_key")
    if not agent_id:
        missing.append("agent_id")
    if not pyreplab_bin:
        missing.append("pyreplab")
    if not provider:
        missing.append("provider")
    launch_ready = not missing
    local_base = _local_base_url()
    return {
        "ok": True,
        "product": "research_desk",
        "server_kind": "local",
        "launch_ready": launch_ready,
        "missing": missing,
        "local_urls": {
            "home": local_base + "/",
            "status": local_base + "/web/research-desk/status",
            "capsules": local_base + "/web/research-desk/capsules",
            "mission_status_root": local_base + "/web/research-desk/mission-status",
        },
        "provider": {
            "configured_provider": provider,
            "agent_mode": detect_agent_mode(),
            "lab_provider_mode": provider_mode,
            "browser_client": browser_client,
        },
        "bridge": {
            "mcp_endpoint": endpoint,
            "agents_endpoint": agents_endpoint,
            "agent_id": agent_id,
            "api_key_present": bool(api_key),
            "credential_source": credential_source,
            "agent_resolution": agent_resolution,
            "agent_resolution_error": agent_resolution_error,
            "agent_env_path": str(DEFAULT_AGENT_ENV_PATH),
        },
        "hosted": {
            "api_base": api_base,
            "labs_url": api_base.rstrip("/") + "/labs/research-desk",
            "first_look_url": api_base.rstrip("/") + "/first-look",
        },
        "trial": {
            "enabled": trial_enabled,
            "configured": trial_configured,
            "status": "configured" if trial_configured else ("available" if bool(api_key) else "unavailable"),
            "api_base": trial_api_base,
            "route_prefix": trial_route_prefix,
            "credit_source": "unchained_api_key",
        },
        "pyreplab": {
            "available": bool(pyreplab_bin),
            "path": pyreplab_bin,
        },
        "capsules": {
            "count": len(_capsule_dirs()),
        },
        "handshake": {
            "supported": True,
            "start_url": local_base + "/web/research-desk/handshake/start",
            "status_url": local_base + "/web/research-desk/handshake/status",
            "approval_root": local_base + "/web/research-desk/handshake/approve",
            "actions": {
                "mission_create_url": local_base + "/web/research-desk/actions/mission-create",
                "mission_advance_url": local_base + "/web/research-desk/actions/mission-advance",
            },
            "ttl_seconds": HANDSHAKE_TTL_SECONDS,
            "token_ttl_seconds": HANDSHAKE_TOKEN_TTL_SECONDS,
        },
    }


def _cleanup_handshake_requests() -> None:
    now = time.time()
    with _HANDSHAKE_LOCK:
        expired = [
            key
            for key, value in list(_HANDSHAKE_REQUESTS.items())
            if max(
                float(value.get("expires_at_epoch", 0.0) or 0.0),
                float(value.get("token_expires_at_epoch", 0.0) or 0.0),
            )
            <= now
        ]
        for key in expired:
            entry = _HANDSHAKE_REQUESTS.pop(key, None)
            token = str((entry or {}).get("token", "")).strip()
            if token:
                _HANDSHAKE_TOKENS.pop(token, None)


def _refresh_allowed_handshake_origins() -> set[str]:
    global _HANDSHAKE_ALLOWED_ORIGINS
    endpoint = os.environ.get("UNCHAINED_MCP_ENDPOINT", DEFAULT_ENDPOINT).strip() or DEFAULT_ENDPOINT
    api_base = infer_api_base(endpoint)
    origins = {
        "https://unchainedsky.com",
        "https://www.unchainedsky.com",
        str(api_base).rstrip("/"),
    }
    _HANDSHAKE_ALLOWED_ORIGINS = tuple(sorted(item for item in origins if item))
    return set(_HANDSHAKE_ALLOWED_ORIGINS)


def _allowed_handshake_origins() -> set[str]:
    if not _HANDSHAKE_ALLOWED_ORIGINS:
        return _refresh_allowed_handshake_origins()
    return set(_HANDSHAKE_ALLOWED_ORIGINS)


def _read_only_headers(origin: str = "") -> dict[str, str]:
    allowed = _allowed_handshake_origins()
    headers = {
        "Access-Control-Allow-Origin": origin if origin in allowed else "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Cache-Control": "no-store",
    }
    if origin in allowed:
        headers["Access-Control-Allow-Private-Network"] = "true"
    return headers


def _handshake_headers(origin: str = "", *, include_auth_headers: bool = False) -> dict[str, str]:
    allowed = _allowed_handshake_origins()
    allow_origin = origin if origin in allowed else "null"
    allow_headers = "Content-Type"
    if include_auth_headers:
        allow_headers += ", Authorization, X-Research-Desk-Token"
    headers = {
        "Access-Control-Allow-Origin": allow_origin,
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": allow_headers,
        "Cache-Control": "no-store",
    }
    if allow_origin != "null":
        headers["Access-Control-Allow-Private-Network"] = "true"
    return headers


def _start_handshake_request(origin: str, *, client_label: str = "", requested_scope: str = "mission:create") -> dict[str, Any]:
    _cleanup_handshake_requests()
    with _HANDSHAKE_LOCK:
        pending_count = sum(1 for value in _HANDSHAKE_REQUESTS.values() if str(value.get("status", "")) == "pending")
        if pending_count >= MAX_PENDING_HANDSHAKE_REQUESTS:
            return {"ok": False, "error": "too_many_pending_requests", "retry_after_seconds": HANDSHAKE_TTL_SECONDS}
        request_id = secrets.token_urlsafe(18)
        now = time.time()
        entry = {
            "request_id": request_id,
            "origin": origin,
            "client_label": client_label.strip() or "Unchained",
            "requested_scope": requested_scope.strip() or "mission:create",
            "status": "pending",
            "created_at_epoch": now,
            "expires_at_epoch": now + HANDSHAKE_TTL_SECONDS,
            "approved_at_epoch": 0.0,
            "token": "",
            "csrf_token": secrets.token_urlsafe(18),
            "token_expires_at_epoch": 0.0,
        }
        _HANDSHAKE_REQUESTS[request_id] = entry
    return _handshake_status_payload(request_id)


def _handshake_status_payload(request_id: str) -> dict[str, Any]:
    _cleanup_handshake_requests()
    with _HANDSHAKE_LOCK:
        entry = dict(_HANDSHAKE_REQUESTS.get(request_id, {}))
    if not entry:
        return {"ok": False, "error": "not_found", "request_id": request_id}
    payload = {
        "ok": True,
        "request_id": request_id,
        "status": str(entry.get("status", "pending")),
        "origin": str(entry.get("origin", "")),
        "client_label": str(entry.get("client_label", "")),
        "requested_scope": str(entry.get("requested_scope", "")),
        "created_at_epoch": float(entry.get("created_at_epoch", 0.0) or 0.0),
        "expires_at_epoch": float(entry.get("expires_at_epoch", 0.0) or 0.0),
        "approval_url": _local_base_url() + "/web/research-desk/handshake/approve?request_id={request_id}".format(
            request_id=quote(request_id)
        ),
    }
    token = str(entry.get("token", ""))
    if token and str(entry.get("status", "")) == "approved":
        payload["session_token"] = token
        payload["approved_at_epoch"] = float(entry.get("approved_at_epoch", 0.0) or 0.0)
        payload["token_expires_at_epoch"] = float(entry.get("token_expires_at_epoch", 0.0) or 0.0)
        payload["token_expires_in_seconds"] = max(
            0,
            int(float(entry.get("token_expires_at_epoch", 0.0) or 0.0) - time.time()),
        )
    return payload


def _decide_handshake_request(request_id: str, *, allow: bool) -> dict[str, Any]:
    _cleanup_handshake_requests()
    with _HANDSHAKE_LOCK:
        entry = _HANDSHAKE_REQUESTS.get(request_id)
        if not entry:
            return {"ok": False, "error": "not_found", "request_id": request_id}
        if not allow:
            entry["status"] = "denied"
            entry["token"] = ""
            entry["token_expires_at_epoch"] = 0.0
        else:
            now = time.time()
            entry["status"] = "approved"
            entry["approved_at_epoch"] = now
            entry["token"] = secrets.token_urlsafe(24)
            entry["token_expires_at_epoch"] = now + HANDSHAKE_TOKEN_TTL_SECONDS
            _HANDSHAKE_TOKENS[str(entry["token"])] = request_id
    return _handshake_status_payload(request_id)


def _validate_handshake_approval(request_id: str, csrf_token: str, *, origin: str = "", referer: str = "") -> tuple[bool, dict[str, Any]]:
    _cleanup_handshake_requests()
    if not _local_origin_allowed(origin, referer):
        return False, {"ok": False, "error": "approval_origin_not_allowed", "request_id": request_id}
    with _HANDSHAKE_LOCK:
        entry = _HANDSHAKE_REQUESTS.get(request_id)
        if not entry:
            return False, {"ok": False, "error": "not_found", "request_id": request_id}
        expected = str(entry.get("csrf_token", "")).strip()
    candidate = str(csrf_token).strip()
    if not expected or not candidate or not hmac.compare_digest(candidate, expected):
        return False, {"ok": False, "error": "invalid_csrf", "request_id": request_id}
    return True, {"ok": True}


def _extract_bearer_token(headers: Mapping[str, Any]) -> str:
    auth_header = str(headers.get("Authorization", "")).strip()
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    return str(headers.get("X-Research-Desk-Token", "")).strip()


def _handshake_approval_page(request_id: str) -> str:
    payload = _handshake_status_payload(request_id)
    if not payload.get("ok"):
        body = """
        <section class="panel">
          <h1>Handshake request not found</h1>
          <p>This connection request expired or was already cleared.</p>
          <p><a href="/">Back to Research Desk</a></p>
        </section>
        """
        return _html_page("Research Desk Connect", body)
    status = str(payload.get("status", "pending"))
    if status == "approved":
        body = """
        <section class="panel">
          <h1>Connection approved</h1>
          <p>Hosted Unchained can now continue with the approved local action scope.</p>
          <p><a href="/">Back to Research Desk</a></p>
        </section>
        """
        return _html_page("Research Desk Connect", body)
    if status == "denied":
        body = """
        <section class="panel">
          <h1>Connection denied</h1>
          <p>You denied this hosted connection request.</p>
          <p><a href="/">Back to Research Desk</a></p>
        </section>
        """
        return _html_page("Research Desk Connect", body)
    with _HANDSHAKE_LOCK:
        csrf_token = str((_HANDSHAKE_REQUESTS.get(request_id) or {}).get("csrf_token", ""))
    body = """
    <section class="panel" style="max-width:720px;margin:0 auto">
      <p class="eyebrow">Connect Research Desk</p>
      <h1>Allow hosted Unchained to connect to this local desk?</h1>
      <p class="muted">If you restart the local Research Desk server, this pending approval and any issued token will be invalidated.</p>
      <p>Requester: <code>{origin}</code></p>
      <p>Client: <code>{client}</code></p>
      <p>Requested scope: <code>{scope}</code></p>
      <form method="post" action="/web/research-desk/handshake/approve" style="display:flex;gap:12px;flex-wrap:wrap;margin-top:18px">
        <input type="hidden" name="request_id" value="{request_id}">
        <input type="hidden" name="csrf_token" value="{csrf_token}">
        <button class="primary-button" type="submit" name="decision" value="allow">Allow Connection</button>
        <button class="secondary-button" type="submit" name="decision" value="deny">Deny</button>
      </form>
    </section>
    """.format(
        origin=html.escape(str(payload.get("origin", ""))),
        client=html.escape(str(payload.get("client_label", ""))),
        scope=html.escape(str(payload.get("requested_scope", ""))),
        request_id=html.escape(request_id),
        csrf_token=html.escape(csrf_token),
    )
    return _html_page("Research Desk Connect", body)


def _validate_handshake_token(token: str, *, required_scope: str) -> tuple[bool, dict[str, Any]]:
    _cleanup_handshake_requests()
    if not token:
        return False, {"ok": False, "error": "missing_token"}
    now = time.time()
    with _HANDSHAKE_LOCK:
        request_id = _HANDSHAKE_TOKENS.get(token, "")
        entry = _HANDSHAKE_REQUESTS.get(request_id) if request_id else None
        if entry:
            if str(entry.get("status", "")) != "approved":
                return False, {"ok": False, "error": "token_not_approved"}
            if float(entry.get("token_expires_at_epoch", 0.0) or 0.0) <= now:
                return False, {"ok": False, "error": "token_expired"}
            granted_scope = str(entry.get("requested_scope", "")).strip()
            granted_scopes = {part for part in granted_scope.replace(",", " ").split() if part}
            if required_scope and required_scope not in granted_scopes:
                return False, {"ok": False, "error": "scope_mismatch", "granted_scope": granted_scope}
            return True, {
                "request_id": str(entry.get("request_id", "")),
                "origin": str(entry.get("origin", "")),
                "requested_scope": granted_scope,
                "approved_at_epoch": float(entry.get("approved_at_epoch", 0.0) or 0.0),
                "token_expires_at_epoch": float(entry.get("token_expires_at_epoch", 0.0) or 0.0),
            }

    # Missing token index entries and cleared request ids both resolve to the same invalid-token response.
    return False, {"ok": False, "error": "invalid_token"}


def _research_desk_mission_payload(capsule_name: str) -> dict[str, Any]:
    capsule_dir = _resolve_capsule_dir(capsule_name)
    if not capsule_dir.exists():
        return {"ok": False, "error": "not_found", "capsule_name": capsule_name}
    summary = _capsule_summary_payload(capsule_dir)
    snapshot = _mission_snapshot(capsule_dir)
    readiness = snapshot.get("readiness", {}) or {}
    gather_qa = snapshot.get("gather_qa", {}) or {}
    action_state = _autopilot_action_state(capsule_dir, snapshot)
    blocked, blocked_copy = _autopilot_block_state(snapshot)
    with _MISSION_ADVANCE_STATE_LOCK:
        advance_busy = _MISSION_ADVANCE_ACTIVE_CAPSULE == capsule_dir.name
        active_action_kind = _MISSION_ADVANCE_ACTIVE_KIND if advance_busy else ""
    status_counts = {
        str(key): int(value or 0)
        for key, value in dict(gather_qa.get("status_counts") or {}).items()
        if str(key)
    }
    mission_url_abs = "{base}{path}".format(base=_local_base_url(), path=summary["mission_url"])
    lab_url_abs = "{base}{path}".format(base=_local_base_url(), path=summary["capsule_url"])
    lab_ready = bool(_primary_object_ready_for_lab(snapshot))
    return {
        "ok": True,
        **summary,
        "mission_prompt": str((snapshot.get("manifest", {}) or {}).get("task", "")).strip(),
        "accepted_like_fraction": float(gather_qa.get("accepted_like_fraction", 0.0) or 0.0),
        "reviewed_page_count": int(gather_qa.get("reviewed_page_count", 0) or 0),
        "qa_status_counts": status_counts,
        "mission_url_abs": mission_url_abs,
        "capsule_url_abs": lab_url_abs,
        "lab_url_abs": lab_url_abs,
        "preferred_open_url_abs": lab_url_abs if lab_ready else mission_url_abs,
        "preferred_open_label": "Open Lab Notes" if lab_ready else "Open Mission",
        "green_light": str(readiness.get("overall_status", "planned")).strip() or "planned",
        "autopilot_next_kind": str((action_state or {}).get("kind", "")).strip(),
        "autopilot_next_label": str((action_state or {}).get("action_label", "")).strip(),
        "autopilot_next_stage": str((action_state or {}).get("stage", "")).strip(),
        "can_advance": (
            bool(action_state)
            and not blocked
            and not advance_busy
            and str((action_state or {}).get("kind", "")).strip() in {"scout", "gather"}
        ),
        "advance_busy": advance_busy,
        "active_action_kind": active_action_kind,
        "lab_ready": lab_ready,
        "blocked": bool(blocked),
        "blocked_reason": str(blocked_copy).strip(),
    }


def _create_mission_from_prompt(
    prompt: str,
    *,
    requested_name: str = "",
    source_route: str = "",
    source_session_id: str = "",
) -> dict[str, Any]:
    from .cli import ensure_capsule, read_json, slugify, update_manifest, write_json

    prompt_text = str(prompt).strip()
    if not prompt_text:
        return {"ok": False, "error": "missing_prompt"}
    desired_name = str(requested_name).strip() or prompt_text
    capsule_slug = slugify(desired_name)
    capsule_dir = _resolve_capsule_dir(capsule_slug)
    if capsule_dir.exists():
        manifest = read_json(capsule_dir / "manifest.json", {})
        created = False
    else:
        capsule_dir, manifest = ensure_capsule(desired_name, append=False)
        created = True
    manifest["name"] = capsule_dir.name
    manifest["task"] = prompt_text
    update_manifest(capsule_dir, manifest)
    hosted_context = {
        "source_route": str(source_route).strip(),
        "source_session_id": str(source_session_id).strip(),
        "created_via": "first_look_handoff" if str(source_route).strip() else "hosted_handoff",
        "created_at_epoch": time.time(),
    }
    write_json(capsule_dir / "hosted_context.json", hosted_context)
    return {
        "ok": True,
        "created": created,
        "capsule_name": capsule_dir.name,
        "mission_url": "/mission/{name}".format(name=quote(capsule_dir.name)),
        "capsule_url": "/capsule/{name}".format(name=quote(capsule_dir.name)),
        "mission_status_url": "/web/research-desk/mission-status?capsule_name={name}".format(name=quote(capsule_dir.name)),
    }


def _advance_mission_from_hosted(capsule_name: str) -> dict[str, Any]:
    from .cli import NoveltyStepTimeout, _run_with_timeout, gather_selected_sources, gather_selected_targets

    if not _MISSION_ADVANCE_LOCK.acquire(blocking=False):
        return {"ok": False, "error": "advance_busy", "capsule_name": capsule_name}
    global _MISSION_ADVANCE_ACTIVE_CAPSULE, _MISSION_ADVANCE_ACTIVE_KIND
    active_capsule_name = ""
    try:
        capsule_dir = _resolve_capsule_dir(capsule_name)
        if not capsule_dir.exists():
            return {"ok": False, "error": "not_found", "capsule_name": capsule_name}
        canonical_capsule_name = capsule_dir.name
        active_capsule_name = canonical_capsule_name
        with _MISSION_ADVANCE_STATE_LOCK:
            _MISSION_ADVANCE_ACTIVE_CAPSULE = canonical_capsule_name
            _MISSION_ADVANCE_ACTIVE_KIND = ""
        snapshot = _mission_snapshot(capsule_dir)
        blocked, blocked_copy = _autopilot_block_state(snapshot)
        if blocked:
            return {
                "ok": False,
                "error": "blocked",
                "capsule_name": canonical_capsule_name,
                "message": str(blocked_copy).strip(),
            }
        action_state = _autopilot_action_state(capsule_dir, snapshot)
        if not action_state:
            payload = _research_desk_mission_payload(canonical_capsule_name)
            return {"ok": False, "error": "no_action", "capsule_name": canonical_capsule_name, "mission": payload}
        kind = str(action_state.get("kind", "")).strip()
        if kind == "lab":
            payload = _research_desk_mission_payload(canonical_capsule_name)
            return {
                "ok": True,
                "performed": False,
                "status": "ready_for_lab",
                "capsule_name": canonical_capsule_name,
                "message": "Mission is ready for Lab Notes.",
                "mission": payload,
            }
        with _MISSION_ADVANCE_STATE_LOCK:
            _MISSION_ADVANCE_ACTIVE_KIND = kind
        try:
            if kind == "scout":
                result = _run_with_timeout(
                    180,
                    gather_selected_sources,
                    capsule_dir,
                    source_ids=list(action_state.get("selected_ids", []) or []),
                )
                message = "Scout captured {count} discovery page(s).".format(
                    count=int(result.get("captured_count", 0) or 0)
                )
            elif kind == "gather":
                result = _run_with_timeout(
                    180,
                    gather_selected_targets,
                    capsule_dir,
                    target_ids=list(action_state.get("selected_ids", []) or []),
                )
                message = "Gather captured {count} candidate page(s).".format(
                    count=int(result.get("captured_count", 0) or 0)
                )
            else:
                return {"ok": False, "error": "unsupported_action", "capsule_name": canonical_capsule_name, "kind": kind}
        except NoveltyStepTimeout:
            return {"ok": False, "error": "timeout", "capsule_name": canonical_capsule_name, "kind": kind}
        except SystemExit:
            raise
        except Exception as exc:
            return {
                "ok": False,
                "error": "action_failed",
                "capsule_name": canonical_capsule_name,
                "kind": kind,
                "message": str(exc) or "{kind} failed.".format(kind=kind),
            }
        payload = _research_desk_mission_payload(canonical_capsule_name)
        return {
            "ok": True,
            "performed": True,
            "status": "advanced",
            "kind": kind,
            "capsule_name": canonical_capsule_name,
            "message": message,
            "mission": payload,
        }
    finally:
        with _MISSION_ADVANCE_STATE_LOCK:
            if _MISSION_ADVANCE_ACTIVE_CAPSULE == active_capsule_name:
                _MISSION_ADVANCE_ACTIVE_CAPSULE = ""
                _MISSION_ADVANCE_ACTIVE_KIND = ""
        _MISSION_ADVANCE_LOCK.release()


def _split_lines(text: str) -> list[str]:
    values: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line:
            values.append(line)
    return values


def _split_csv(text: str) -> list[str]:
    values: list[str] = []
    for raw_piece in text.split(","):
        piece = raw_piece.strip()
        if piece:
            values.append(piece)
    return values


def _source_urls_from_plan(source_plan: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for source in source_plan.get("sources", []):
        if not isinstance(source, dict):
            continue
        entrypoint = source.get("entrypoint") or {}
        if entrypoint.get("mode") != "url":
            continue
        value = str(entrypoint.get("value", "")).strip()
        if value and value not in urls:
            urls.append(value)
    return urls


def _first_target(task_spec: dict[str, Any]) -> dict[str, Any]:
    targets = task_spec.get("target_objects") or []
    if targets and isinstance(targets[0], dict):
        return dict(targets[0])
    return {}


def _planner_clarification_state(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    review = snapshot.get("object_decision_review", {}) or {}
    status = str(review.get("status", "")).strip()
    if status not in {"needs_clarification", "replan_recommended"}:
        return None
    return review


def _mission_values(
    manifest: dict[str, Any],
    task_spec: dict[str, Any],
    source_plan: dict[str, Any],
) -> dict[str, str]:
    target = _first_target(task_spec)
    sample_target = target.get("sample_target") or {}
    return {
        "capsule_name": str(manifest.get("name", "")),
        "mission_prompt": str(manifest.get("task", "")).strip(),
        "objective": str(task_spec.get("objective", "")).strip(),
        "questions": "\n".join(str(item).strip() for item in task_spec.get("questions", []) if str(item).strip()),
        "object_name": str(target.get("name", "")).strip(),
        "object_description": str(target.get("description", "")).strip(),
        "object_grain": str(target.get("grain", "")).strip(),
        "primary_key": ", ".join(str(item).strip() for item in target.get("primary_key", []) if str(item).strip()),
        "measures": ", ".join(str(item).strip() for item in target.get("measures", []) if str(item).strip()),
        "dimensions": ", ".join(str(item).strip() for item in target.get("dimensions", []) if str(item).strip()),
        "required_columns": ", ".join(
            str(item).strip() for item in target.get("required_columns", []) if str(item).strip()
        ),
        "min_rows": str(sample_target.get("min_rows", "") or "").strip(),
        "seed_urls": "\n".join(_source_urls_from_plan(source_plan)),
    }


def _spark_chip_html(
    *,
    title: str,
    subtitle: str,
    fill_target: str = "",
    fill_value: str = "",
    fill_mode: str = "replace",
    open_manual: bool = False,
    focus_field: str = "",
) -> str:
    attrs = [
        'type="button"',
        'class="spark-chip"',
    ]
    if fill_target:
        attrs.append('data-fill-target="{target}"'.format(target=html.escape(fill_target)))
        attrs.append('data-fill-value="{value}"'.format(value=html.escape(fill_value)))
        attrs.append('data-fill-mode="{mode}"'.format(mode=html.escape(fill_mode)))
    if open_manual:
        attrs.append('data-open-manual-controls="true"')
    if focus_field:
        attrs.append('data-focus-field="{field}"'.format(field=html.escape(focus_field)))
    return """
<button {attrs}>
  <strong>{title}</strong>
  <span>{subtitle}</span>
  <span class="spark-chip-action">Load prompt</span>
</button>
""".format(
        attrs=" ".join(attrs),
        title=html.escape(title),
        subtitle=html.escape(subtitle),
    )


def _mission_prompt_sparks_html() -> str:
    sparks = [
        (
            "Crypto market odds",
            "Analyze active prediction market events about Bitcoin, Ethereum, and stablecoin regulation and compare the odds.",
        ),
        (
            "NFL market odds",
            "Analyze active prediction market events about NFL free agency, the draft, and quarterback movement and compare the odds.",
        ),
        (
            "AI infrastructure stocks",
            "Find the strongest large-cap stocks for 2026 using AI infrastructure demand, photonics exposure, and balance-sheet quality.",
        ),
        (
            "Cooling neighborhoods",
            "Find neighborhoods in Phoenix where home prices have cooled the most.",
        ),
    ]
    chips = "".join(
        _spark_chip_html(
            title=title,
            subtitle=subtitle,
            fill_target="mission_prompt",
            fill_value=subtitle,
        )
        for title, subtitle in sparks
    )
    return """
  <p class="muted">Pick a spark, then launch. You can still write your own mission from scratch.</p>
  <div class="spark-strip">{chips}</div>
""".format(chips=chips)


def _playbook_card_html(
    *,
    title: str,
    body: str,
    kicker: str,
    focus_field: str,
) -> str:
    return """
<button type="button" class="playbook-card" data-open-manual-controls="true" data-focus-field="{focus_field}">
  <strong>{title}</strong>
  <p>{body}</p>
  <small>{kicker}</small>
</button>
""".format(
        focus_field=html.escape(focus_field),
        title=html.escape(title),
        body=html.escape(body),
        kicker=html.escape(kicker),
    )


def _mission_playbook_html(capsule_name: str) -> str:
    cards = "".join(
        [
            _playbook_card_html(
                title="Sharpen the ask",
                body="Rewrite the mission prompt until the desk has a cleaner target.",
                kicker="focus mission prompt",
                focus_field="mission_prompt",
            ),
            _playbook_card_html(
                title="Retarget the object",
                body="Change the row object or grain when Shape is chasing the wrong thing.",
                kicker="focus row object",
                focus_field="object_name",
            ),
            _playbook_card_html(
                title="Swap in hotter sources",
                body="Feed the planner better seed URLs when the route mix feels weak.",
                kicker="focus seed urls",
                focus_field="seed_urls",
            ),
        ]
    )
    return """
<section class="panel stage-section" data-stage-section="Mission">
  <div class="section-head">
    <div>
      <div class="eyebrow">Fun Fixes</div>
      <h2>Steer the desk instead of opening a giant form</h2>
      <p class="muted">Fast interventions keep the mission playful. The expert editor is still there when you need it.</p>
    </div>
    <a class="button-link ghost" href="/capsule/{name}">Jump to Lab Notes</a>
  </div>
  <div class="playbook-grid">{cards}</div>
</section>
""".format(name=quote(capsule_name), cards=cards)


def _mission_create_form_html() -> str:
    return """
<section class="panel mission-panel mission-panel-create">
  <div class="section-head">
    <div>
      <div class="eyebrow">Mission</div>
      <h2>Start with the question, not the schema.</h2>
      <p class="muted">Describe the work in plain language. The planner will infer the row object, key questions, and first Trail Map.</p>
    </div>
  </div>
  <div class="launch-metrics chips">
    <span class="chip">question first</span>
    <span class="chip">plan drafts itself</span>
    <span class="chip">intervene only when it gets weird</span>
  </div>
  {sparks}
  <form class="mission-form" method="post" action="/mission/create" data-loading-label="Planning Mission">
    <div class="launch-input-shell">
      <div class="launch-input-kicker">
        <strong>Start here</strong>
        <span>first input</span>
      </div>
      <label class="field field-wide launch-prompt-field">
        <span>Mission prompt</span>
        <textarea name="mission_prompt" spellcheck="false" placeholder="Find neighborhoods in Phoenix where home prices have cooled the most."></textarea>
      </label>
    </div>
    <div class="actions mission-actions">
        <button type="submit" data-loading-label="Planning Mission">Launch Mission</button>
    </div>
  </form>
</section>
""".format(sparks=_mission_prompt_sparks_html())


def _advanced_overrides_html(values: dict[str, str]) -> str:
    return """
<details class="advanced-planning">
  <summary>Override generated plan</summary>
  <p class="muted">Only use this if the planner inferred the wrong row object, fields, or source seeds.</p>
  <div class="field-grid">
    <label class="field field-wide">
      <span>Objective</span>
      <textarea name="objective" spellcheck="false" placeholder="What should this mission produce?">{objective}</textarea>
    </label>
    <label class="field field-wide">
      <span>Questions</span>
      <textarea name="questions" spellcheck="false" placeholder="One question per line">{questions}</textarea>
    </label>
    <label class="field">
      <span>Row object</span>
      <input type="text" name="object_name" value="{object_name}" placeholder="listings">
    </label>
    <label class="field">
      <span>Grain</span>
      <input type="text" name="object_grain" value="{object_grain}" placeholder="one listing">
    </label>
    <label class="field">
      <span>Minimum rows</span>
      <input type="number" min="1" step="1" name="min_rows" value="{min_rows}" placeholder="100">
    </label>
    <label class="field field-wide">
      <span>Object description</span>
      <input type="text" name="object_description" value="{object_description}" placeholder="One row per marketplace listing">
    </label>
    <label class="field">
      <span>Primary key</span>
      <input type="text" name="primary_key" value="{primary_key}" placeholder="item_id, listing_url">
    </label>
    <label class="field">
      <span>Measures</span>
      <input type="text" name="measures" value="{measures}" placeholder="price_value">
    </label>
    <label class="field">
      <span>Dimensions</span>
      <input type="text" name="dimensions" value="{dimensions}" placeholder="condition, shipping_text">
    </label>
    <label class="field">
      <span>Required columns</span>
      <input type="text" name="required_columns" value="{required_columns}" placeholder="title_clean, price_value, listing_url">
    </label>
    <label class="field field-wide">
      <span>Seed URLs</span>
      <textarea name="seed_urls" spellcheck="false" placeholder="One URL per line">{seed_urls}</textarea>
    </label>
  </div>
</details>
""".format(
        objective=html.escape(values.get("objective", "")),
        questions=html.escape(values.get("questions", "")),
        object_name=html.escape(values.get("object_name", "")),
        object_grain=html.escape(values.get("object_grain", "")),
        min_rows=html.escape(values.get("min_rows", "")),
        object_description=html.escape(values.get("object_description", "")),
        primary_key=html.escape(values.get("primary_key", "")),
        measures=html.escape(values.get("measures", "")),
        dimensions=html.escape(values.get("dimensions", "")),
        required_columns=html.escape(values.get("required_columns", "")),
        seed_urls=html.escape(values.get("seed_urls", "")),
    )


def _mission_editor_html(
    *,
    values: dict[str, str],
    action: str,
    capsule_name: str,
    open_manual: bool = False,
) -> str:
    mission_prompt = str(values.get("mission_prompt", "")).strip()
    return """
<section class="panel mission-panel stage-section" data-stage-section="Mission">
  <div class="section-head">
    <div>
      <div class="eyebrow">Intervene</div>
      <h2>{heading}</h2>
      <p class="muted">The planner keeps driving by default. Edit the mission only when you want to redirect the object model or the next route.</p>
    </div>
  </div>
  <p class="muted"><code>{mission_prompt}</code></p>
  <details class="advanced-planning" id="manual-controls"{open_attr}>
    <summary>Open manual controls</summary>
    <div class="manual-controls-body">
      <p class="muted">Autopilot is on. Open this only to rewrite the mission or override the generated plan.</p>
      <form class="mission-form" method="post" action="{action}" data-loading-label="Re-planning Mission">
        <label class="field field-wide">
          <span>Mission prompt</span>
          <textarea name="mission_prompt" spellcheck="false" placeholder="Describe the mission in plain language.">{mission_prompt}</textarea>
        </label>
        {advanced}
        <div class="actions mission-actions">
          <button type="submit" data-loading-label="Re-planning Mission">Re-plan Mission</button>
          <a class="button-link ghost" href="/capsule/{name}">Open Lab Notes</a>
        </div>
      </form>
    </div>
  </details>
</section>
""".format(
        heading=html.escape(capsule_name),
        action=html.escape(action),
        mission_prompt=html.escape(mission_prompt),
        advanced=_advanced_overrides_html(values),
        name=quote(capsule_name),
        open_attr=" open" if open_manual else "",
    )


def _mission_summary_html(task_spec: dict[str, Any], source_plan: dict[str, Any]) -> str:
    target = _first_target(task_spec)
    source_budget = dict(source_plan.get("source_budget") or {})
    questions = list(task_spec.get("questions") or [])
    question_items = "".join(
        "<li>{text}</li>".format(text=html.escape(str(question)))
        for question in questions[:4]
        if str(question).strip()
    ) or "<li>No planner questions yet.</li>"
    key_fields = [
        item
        for item in list(target.get("required_columns") or [])[:6]
        if str(item).strip()
    ]
    field_badges = "".join(
        '<span class="chip">{field}</span>'.format(field=html.escape(str(field)))
        for field in key_fields
    ) or '<span class="chip">No required fields yet</span>'
    return """
<section class="panel stage-section" data-stage-section="Mission">
  <div class="section-head">
    <div>
      <div class="eyebrow">Planner Draft</div>
      <h2>{objective}</h2>
      <p class="muted">Generated from the current Mission prompt.</p>
    </div>
  </div>
  <div class="status-list">
    <div class="status-row"><span>row object</span><strong>{object_name}</strong></div>
    <div class="status-row"><span>grain</span><strong>{grain}</strong></div>
    <div class="status-row"><span>minimum rows</span><strong>{min_rows}</strong></div>
    <div class="status-row"><span>scout routes</span><strong>{scout_sources}</strong></div>
    <div class="status-row"><span>scout action budget</span><strong>{scout_budget}</strong></div>
    <div class="status-row"><span>planned gather sources</span><strong>{planned_sources}</strong></div>
    <div class="status-row"><span>target gather sources</span><strong>{recommended_sources}</strong></div>
    <div class="status-row"><span>estimated rows</span><strong>{estimated_rows}</strong></div>
    <div class="status-row"><span>budget basis</span><strong>{budget_basis}</strong></div>
  </div>
  <div class="chips chips-wrap">{field_badges}</div>
  <div class="status-actions">
    <p class="muted">Key questions</p>
    <ul>{question_items}</ul>
  </div>
</section>
""".format(
        objective=html.escape(str(task_spec.get("objective", "Planner draft pending."))),
        object_name=html.escape(str(target.get("name", "unknown"))),
        grain=html.escape(str(target.get("grain", "unknown"))),
        min_rows=html.escape(str((target.get("sample_target") or {}).get("min_rows", "n/a"))),
        scout_sources=html.escape(str(source_budget.get("scout_source_count", 0))),
        scout_budget=html.escape(str(source_budget.get("scout_action_budget", "n/a"))),
        planned_sources=html.escape(str(source_budget.get("planned_source_count", len(source_plan.get("sources", []))))),
        recommended_sources=html.escape(str(source_budget.get("recommended_source_count", len(source_plan.get("sources", []))))),
        estimated_rows=html.escape(
            "{low}-{high}".format(
                low=int(source_budget.get("estimated_rows_low", 0) or 0),
                high=int(source_budget.get("estimated_rows_high", 0) or 0),
            )
        ),
        budget_basis=html.escape(
            "local calibration" if str(source_budget.get("budget_basis", "")) == "local_empirical" else "heuristic"
        ),
        field_badges=field_badges,
        question_items=question_items,
    )


def _trail_map_html(source_plan: dict[str, Any], *, capsule_name: str = "", blocked_reason: str = "") -> str:
    sources = list(source_plan.get("sources", []))
    source_budget = dict(source_plan.get("source_budget") or {})
    if not sources:
        return """
<section class="panel stage-section" data-stage-section="Trail Map">
  <div class="section-head">
    <div>
      <div class="eyebrow">Trail Map</div>
      <h2>No sources planned yet</h2>
      <p class="muted">Add seed URLs or tighten the Mission object model to generate a clearer starting map.</p>
    </div>
  </div>
</section>
"""
    cards: list[str] = []
    for source in sources:
        entrypoint = source.get("entrypoint") or {}
        mode = str(entrypoint.get("mode", "url")).strip() or "url"
        site_hint = str(entrypoint.get("site_hint", "")).strip() or str(
            (source.get("capture_hints") or {}).get("site_hint", "")
        ).strip()
        value = str(entrypoint.get("value", "")).strip()
        mode_label = {
            "url": "URL",
            "query": "Query",
            "site_hint": "Site hint",
        }.get(mode, mode.replace("_", " ").title())
        extra_chips = ""
        if site_hint:
            extra_chips = '<span class="chip">{site_hint}</span>'.format(site_hint=html.escape(site_hint))
        rationale = str(source.get("rationale", "")).strip()
        source_id = str(source.get("source_id", "")).strip()
        yield_estimate = dict(source.get("yield_estimate") or {})
        checkbox_name = "route_{source_id}".format(source_id=source_id)
        checked = " checked" if str(source.get("capture_status", "")) != "captured" else ""
        cards.append(
            """
<article class="route-card">
  <label class="route-select">
    <input type="checkbox" name="{checkbox_name}" value="1"{checked}>
    <span>Include in Gather</span>
  </label>
  <div class="route-meta">
    <span class="chip">{source_type}</span>
    <span class="chip">{capture_status}</span>
    <span class="chip">{mode_label}</span>
    {extra_chips}
  </div>
  <div class="route-url">{value}</div>
  <p class="muted">Targets: {targets}</p>
  <p class="muted">Expected yield: {rows_low}-{rows_high} rows, {scout_low}-{scout_high} scout candidates</p>
  {rationale}
</article>
""".format(
                checkbox_name=html.escape(checkbox_name),
                checked=checked,
                source_type=html.escape(str(source.get("source_type", "seed_url"))),
                capture_status=html.escape(str(source.get("capture_status", "pending"))),
                mode_label=html.escape(mode_label),
                extra_chips=extra_chips,
                value=html.escape(value),
                targets=html.escape(", ".join(str(item) for item in source.get("target_objects", []))),
                rows_low=int(yield_estimate.get("expected_rows_low", 0) or 0),
                rows_high=int(yield_estimate.get("expected_rows_high", 0) or 0),
                scout_low=int(yield_estimate.get("expected_scout_candidates_low", 0) or 0),
                scout_high=int(yield_estimate.get("expected_scout_candidates_high", 0) or 0),
                rationale=(
                    '<p class="muted">{text}</p>'.format(text=html.escape(rationale))
                    if rationale
                    else ""
                ),
            )
        )
    if blocked_reason:
        return """
<section class="panel attention-panel stage-section" data-stage-section="Trail Map">
  <div class="section-head">
    <div>
      <div class="eyebrow">Trail Map</div>
      <h2>Scout is blocked until the Mission is clearer</h2>
      <p class="muted">{blocked_reason}</p>
    </div>
  </div>
  <div class="route-list">{cards}</div>
  <div class="actions mission-actions">
    <button type="button" disabled>Scout blocked</button>
    <a class="button-link secondary" href="#manual-controls">Open manual controls</a>
  </div>
</section>
""".format(
            blocked_reason=html.escape(blocked_reason),
            cards="".join(cards),
        )
    return """
<section class="panel stage-section" data-stage-section="Trail Map">
  <div class="section-head">
    <div>
      <div class="eyebrow">Trail Map</div>
      <h2>Planned sources</h2>
      <p class="muted">This is the first route Gather will use, including search queries when no URLs are known yet.</p>
      <p class="muted">Planner budget: {scout_routes} scout routes with ~{scout_budget} MCP actions, {planned} planned / {target} target gather sources, estimated {rows_low}-{rows_high} rows.</p>
      <p class="muted">Budget basis: {budget_basis}. Matched capsules: {matched_capsules}. Families with calibration: {calibration_families}.</p>
    </div>
  </div>
  <form method="post" action="/mission/{name}/scout" data-loading-label="Scouting selected routes">
    <div class="route-list">{cards}</div>
    <div class="actions mission-actions">
      <button type="submit" data-loading-label="Scouting selected routes">Scout These Routes</button>
      <span class="muted">Uses MCP to capture discovery pages and build scout candidates.</span>
    </div>
  </form>
</section>
""".format(
        cards="".join(cards),
        name=quote(capsule_name),
        planned=int(source_budget.get("planned_source_count", len(sources)) or 0),
        target=int(source_budget.get("recommended_source_count", len(sources)) or 0),
        scout_routes=int(source_budget.get("scout_source_count", 0) or 0),
        scout_budget=int(source_budget.get("scout_action_budget", 0) or 0),
        rows_low=int(source_budget.get("estimated_rows_low", 0) or 0),
        rows_high=int(source_budget.get("estimated_rows_high", 0) or 0),
        budget_basis=html.escape(
            "local calibration" if str(source_budget.get("budget_basis", "")) == "local_empirical" else "heuristic"
        ),
        matched_capsules=int(source_budget.get("matched_capsule_count", 0) or 0),
        calibration_families=html.escape(
            ", ".join(str(item) for item in source_budget.get("calibration_families", []) if str(item).strip()) or "none"
        ),
    )


def _shape_summary_html(object_manifest: dict[str, Any], *, capsule_name: str = "") -> str:
    objects = [
        item
        for item in object_manifest.get("objects", [])
        if isinstance(item, dict) and str(item.get("object_role", "")).strip() != "support"
    ]
    if not objects:
        return """
<section class="panel stage-section" data-stage-section="Shape">
  <div class="section-head">
    <div>
      <div class="eyebrow">Shape</div>
      <h2>No shaped objects yet</h2>
      <p class="muted">Scout and Gather need to produce enough evidence before the primary object is shaped.</p>
    </div>
  </div>
</section>
"""
    cards: list[str] = []
    for item in objects:
        quality = dict(item.get("quality") or {})
        confidence_counts = dict(quality.get("row_confidence_counts") or {})
        confidence_chips = "".join(
            '<span class="chip">{label}: {count}</span>'.format(
                label=html.escape(str(label)),
                count=html.escape(str(count)),
            )
            for label, count in confidence_counts.items()
        ) or '<span class="chip">confidence pending</span>'
        extractor = dict(item.get("extractor") or {})
        extractor_name = str(extractor.get("name", "")).strip() or "pending"
        provenance_state = "yes" if str(item.get("provenance_path", "")).strip() else "no"
        cards.append(
            """
<article class="route-card">
  <div class="route-meta">
    <span class="chip">{role}</span>
    <span class="chip">{rows} rows</span>
    <span class="chip">provenance: {provenance}</span>
  </div>
  <div class="route-url">{name}</div>
  <p class="muted">Extractor: {extractor}</p>
  <div class="route-meta">{confidence_chips}</div>
</article>
""".format(
                role=html.escape(str(item.get("object_role", "primary"))),
                rows=html.escape(str(item.get("row_count", 0))),
                provenance=html.escape(provenance_state),
                name=html.escape(str(item.get("name", ""))),
                extractor=html.escape(extractor_name),
                confidence_chips=confidence_chips,
            )
        )
    next_step = ""
    if capsule_name:
        next_step = """
  <div class="shape-next-step">
    <p>Shape is ready. Continue the investigation in Lab Notes with the structured object already loaded.</p>
    <a class="button-link secondary" href="/capsule/{name}">Explore in Lab Notes</a>
  </div>
""".format(name=quote(capsule_name))
    return """
<section class="panel stage-section" data-stage-section="Shape">
  <div class="section-head">
    <div>
      <div class="eyebrow">Shape</div>
      <h2>Structured objects</h2>
      <p class="muted">This is the current object layer that Green Light evaluates before Lab Notes opens against it.</p>
    </div>
  </div>
  <div class="route-list">{cards}</div>
  {next_step}
</section>
""".format(cards="".join(cards), next_step=next_step)


def _mission_notice_html(message: str) -> str:
    text = str(message).strip()
    if not text:
        return ""
    return """
<section class="panel stage-section" data-stage-section="Mission">
  <div class="section-head">
    <div>
      <div class="eyebrow">Mission Status</div>
      <h2>{message}</h2>
      <p class="muted">The existing capsule was reopened so you can keep working without recreating the Mission from scratch.</p>
    </div>
  </div>
</section>
""".format(message=html.escape(text))


def _mission_snapshot(capsule_dir: Path) -> dict[str, Any]:
    from .cli import read_json, refresh_object_decision_review, sync_task_files

    manifest = read_json(capsule_dir / "manifest.json", {})
    task_spec = read_json(capsule_dir / "task_spec.json", {})
    row_schema = read_json(capsule_dir / "row_schema.json", {})
    object_decision_review = read_json(capsule_dir / "object_decision_review.json", {})
    source_plan = read_json(capsule_dir / "source_plan.json", {})
    capsule_state = read_json(capsule_dir / "capsule_state.json", {})
    readiness = read_json(capsule_dir / "readiness.json", {})
    object_manifest = read_json(capsule_dir / "object_manifest.json", {})
    scout_summary = read_json(capsule_dir / "scout_summary.json", {})
    gather_targets = read_json(capsule_dir / "gather_targets.json", {})
    gather_qa = read_json(capsule_dir / "gather_qa.json", {})
    gather_qa_review = read_json(capsule_dir / "gather_qa_review.json", {})
    source_budget = dict(source_plan.get("source_budget") or {})
    needs_source_plan_refresh = not source_plan or not source_budget
    if manifest and (
        not task_spec
        or not row_schema
        or not object_decision_review
        or needs_source_plan_refresh
        or not capsule_state
    ):
        sync_task_files(capsule_dir, manifest)
        task_spec = read_json(capsule_dir / "task_spec.json", {})
        row_schema = read_json(capsule_dir / "row_schema.json", {})
        object_decision_review = read_json(capsule_dir / "object_decision_review.json", {})
        source_plan = read_json(capsule_dir / "source_plan.json", {})
        capsule_state = read_json(capsule_dir / "capsule_state.json", {})
    if manifest and task_spec and source_plan and (manifest.get("pages") or scout_summary):
        refresh_object_decision_review(capsule_dir, manifest)
        row_schema = read_json(capsule_dir / "row_schema.json", row_schema)
        object_decision_review = read_json(capsule_dir / "object_decision_review.json", object_decision_review)
        scout_summary = read_json(capsule_dir / "scout_summary.json", scout_summary)
    return {
        "manifest": manifest,
        "task_spec": task_spec,
        "row_schema": row_schema,
        "object_decision_review": object_decision_review,
        "source_plan": source_plan,
        "capsule_state": capsule_state,
        "readiness": readiness,
        "object_manifest": object_manifest,
        "scout_summary": scout_summary,
        "gather_targets": gather_targets,
        "gather_qa": gather_qa,
        "gather_qa_review": gather_qa_review,
    }


def _primary_object(snapshot: dict[str, Any]) -> dict[str, Any]:
    object_manifest = snapshot.get("object_manifest", {}) or {}
    for item in object_manifest.get("objects", []):
        if isinstance(item, dict) and str(item.get("object_role", "")).strip() == "primary":
            return item
    return {}


def _captured_page_count(snapshot: dict[str, Any]) -> int:
    manifest = snapshot.get("manifest", {}) or {}
    pages = manifest.get("pages") or []
    if isinstance(pages, list):
        return len(pages)
    return 0


def _workflow_stage_rows(snapshot: dict[str, Any]) -> list[dict[str, str]]:
    manifest = snapshot.get("manifest", {}) or {}
    task_spec = snapshot.get("task_spec", {}) or {}
    source_plan = snapshot.get("source_plan", {}) or {}
    scout_summary = snapshot.get("scout_summary", {}) or {}
    gather_targets = snapshot.get("gather_targets", {}) or {}
    gather_qa = snapshot.get("gather_qa", {}) or {}
    gather_qa_review = snapshot.get("gather_qa_review", {}) or {}
    readiness = snapshot.get("readiness", {}) or {}
    turns = list(snapshot.get("turns", []) or [])
    primary = _primary_object(snapshot)

    target = _first_target(task_spec)
    sources = list(source_plan.get("sources", []) or [])
    candidate_count = int(scout_summary.get("candidate_count", 0) or 0)
    page_count = _captured_page_count(snapshot)
    captured_targets = sum(
        1
        for target_item in gather_targets.get("targets", [])
        if isinstance(target_item, dict) and str(target_item.get("gather_status", "")).strip() == "captured"
    )
    reviewed_pages = int(gather_qa.get("reviewed_page_count", 0) or 0)
    agent_reviewed_pages = int(gather_qa_review.get("reviewed_page_count", 0) or 0)
    row_count = int(primary.get("row_count", 0) or 0)
    readiness_status = str(readiness.get("overall_status", "planned")).strip() or "planned"

    def row(stage: str, status: str, summary: str) -> dict[str, str]:
        return {"stage": stage, "status": status, "summary": summary}

    mission_status = "done" if str(target.get("name", "")).strip() else "waiting"
    trail_status = "done" if sources else "waiting"
    if candidate_count > 0:
        scout_status = "done"
        scout_summary_text = "{count} candidates".format(count=candidate_count)
    elif page_count > 0 or row_count > 0:
        scout_status = "done"
        scout_summary_text = "seed captures already present"
    else:
        scout_status = "ready" if sources else "waiting"
        scout_summary_text = "{count} candidates".format(count=candidate_count)

    if captured_targets > 0:
        gather_status = "done"
        gather_summary_text = "{count} captured".format(count=captured_targets)
    elif page_count > 0 or row_count > 0:
        gather_status = "done"
        gather_summary_text = "{count} pages in capsule".format(count=page_count)
    else:
        gather_status = "ready" if gather_targets.get("targets") else "waiting"
        gather_summary_text = "{count} captured".format(count=captured_targets)

    if reviewed_pages > 0 or agent_reviewed_pages > 0:
        qa_status = "done"
        qa_summary_text = "{count} reviewed".format(count=reviewed_pages + max(agent_reviewed_pages, 0))
    elif page_count > 0 or row_count > 0:
        qa_status = "done"
        qa_summary_text = "review not needed yet"
    else:
        qa_status = "ready" if captured_targets > 0 else "waiting"
        qa_summary_text = "{count} reviewed".format(count=reviewed_pages)
    shape_status = "done" if row_count > 0 else ("ready" if captured_targets > 0 else "waiting")
    if readiness_status == "final_ready":
        green_status = "done"
    elif readiness_status == "exploratory_ready":
        green_status = "working"
    elif readiness_status == "blocked":
        green_status = "blocked"
    else:
        green_status = "waiting"
    notes_status = "active" if turns else ("ready" if row_count > 0 else "waiting")

    return [
        row("Mission", mission_status, str(target.get("name", "not inferred")) or "not inferred"),
        row("Trail Map", trail_status, "{count} routes".format(count=len(sources))),
        row("Scout", scout_status, scout_summary_text),
        row("Gather", gather_status, gather_summary_text),
        row("Gather QA", qa_status, qa_summary_text),
        row("Shape", shape_status, "{count} rows".format(count=row_count)),
        row("Green Light", green_status, readiness_status.replace("_", " ")),
        row("Lab Notes", notes_status, "{count} turns".format(count=len(turns))),
    ]


def _workflow_energy(stage_rows: list[dict[str, str]]) -> int:
    if not stage_rows:
        return 0
    weights = {
        "done": 100,
        "active": 92,
        "working": 82,
        "ready": 68,
        "blocked": 44,
        "waiting": 18,
    }
    total = sum(weights.get(str(row.get("status", "")).strip(), 24) for row in stage_rows)
    return int(round(total / len(stage_rows)))


def _default_stage_name(stage_rows: list[dict[str, str]]) -> str:
    priority = ["blocked", "active", "working", "ready", "waiting", "done"]
    for status in priority:
        for row in stage_rows:
            if str(row.get("status", "")).strip() == status:
                return str(row.get("stage", "")).strip()
    return str(stage_rows[0].get("stage", "")).strip() if stage_rows else "Mission"


def _stage_spotlight_content(stage: str) -> dict[str, str]:
    mapping = {
        "Mission": {
            "title": "Mission draft is the vibe setter.",
            "copy": "If the desk feels vague, fix it here first. A sharper ask makes every later stage feel smarter and faster.",
            "hint": "Punchier questions create better routes.",
        },
        "Trail Map": {
            "title": "Trail Map is the hunt plan.",
            "copy": "This is where the system decides where to look next. Good source variety makes the whole run feel alive.",
            "hint": "Pick routes that look surprising, specific, and evidence-rich.",
        },
        "Scout": {
            "title": "Scout is the discovery reel.",
            "copy": "Skim candidates fast, keep the interesting ones, and let weak pages die early instead of dragging the mission down.",
            "hint": "This is the most game-like part of the desk.",
        },
        "Gather": {
            "title": "Gather is where trust is earned.",
            "copy": "This stage turns promising leads into real pages you can lean on. Strong picks here make Shape feel magical.",
            "hint": "Feed it fewer, better targets.",
        },
        "Gather QA": {
            "title": "Gather QA trims the noise.",
            "copy": "Bad captures are boring. QA should feel like cleaning the signal so the desk gets sharper, not heavier.",
            "hint": "Keep the accepted-like coverage climbing.",
        },
        "Shape": {
            "title": "Shape is the reveal moment.",
            "copy": "This is the payoff. Evidence turns into rows, and the mission starts feeling real instead of hypothetical.",
            "hint": "When this unlocks, celebrate it.",
        },
        "Green Light": {
            "title": "Green Light is the confidence gate.",
            "copy": "This stage should feel dramatic: either the object is trustworthy enough to play with, or the desk tells you exactly what to fix next.",
            "hint": "Blocked is useful if the next move is obvious.",
        },
        "Lab Notes": {
            "title": "Lab Notes should feel like a reel, not a log.",
            "copy": "Questions, runs, and takeaways should read like a sequence of moves you can riff on, not static console output.",
            "hint": "Lead with prompts and highlights, not setup.",
        },
    }
    return mapping.get(stage, mapping["Mission"])


def _stage_trigger_energy(stage: str, status: str) -> int:
    base = {
        "Mission": 16,
        "Trail Map": 28,
        "Scout": 42,
        "Gather": 56,
        "Gather QA": 66,
        "Shape": 78,
        "Green Light": 88,
        "Lab Notes": 96,
    }.get(stage, 22)
    bonus = {
        "done": 6,
        "active": 4,
        "working": 2,
        "ready": 0,
        "blocked": -10,
        "waiting": -14,
    }.get(status, 0)
    return max(8, min(100, base + bonus))


def _board_spotlight_html(snapshot: dict[str, Any]) -> str:
    stage_rows = _workflow_stage_rows(snapshot)
    default_stage = _default_stage_name(stage_rows)
    content = _stage_spotlight_content(default_stage)
    energy = _workflow_energy(stage_rows)
    return """
<section class="panel board-spotlight">
  <div class="board-spotlight-head">
    <div>
      <div class="eyebrow">Desk Spotlight</div>
      <h2 id="board-spotlight-title">{title}</h2>
      <p id="board-spotlight-copy" class="board-stage-copy">{copy}</p>
    </div>
    <div class="board-meter-shell">
      <div class="board-meter">
        <span id="board-spotlight-meter-fill" class="board-meter-fill" style="width: {energy}%"></span>
      </div>
      <p id="board-spotlight-hint" class="board-meter-copy">{hint}</p>
    </div>
  </div>
</section>
""".format(
        title=html.escape(content["title"]),
        copy=html.escape(content["copy"]),
        hint=html.escape("Desk energy: {energy}%".format(energy=energy) + " · " + content["hint"]),
        energy=energy,
    )


def _autopilot_next(snapshot: dict[str, Any]) -> str:
    task_spec = snapshot.get("task_spec", {}) or {}
    source_plan = snapshot.get("source_plan", {}) or {}
    scout_summary = snapshot.get("scout_summary", {}) or {}
    gather_targets = snapshot.get("gather_targets", {}) or {}
    readiness = snapshot.get("readiness", {}) or {}
    primary = _primary_object(snapshot)
    page_count = _captured_page_count(snapshot)

    target = _first_target(task_spec)
    readiness_status = str(readiness.get("overall_status", "")).strip()
    blocked_actions = list(readiness.get("blocked_actions", []) or [])
    if not str(target.get("name", "")).strip():
        return "Infer the row object from the mission."
    if _primary_object_ready_for_lab(snapshot):
        return "Open Lab Notes and analyze the shaped object."
    if not list(source_plan.get("sources", []) or []):
        return "Generate the first Trail Map."
    if int(primary.get("row_count", 0) or 0) > 0:
        if blocked_actions:
            return str(blocked_actions[0])
        return "Open Lab Notes and keep iterating."
    pending_direct_sources = _pending_direct_source_ids(source_plan)
    if int(scout_summary.get("candidate_count", 0) or 0) <= 0 and page_count <= 0:
        if pending_direct_sources:
            return "Gather the direct market sources."
        return "Scout the planned routes."
    captured_targets = sum(
        1
        for target_item in gather_targets.get("targets", [])
        if isinstance(target_item, dict) and str(target_item.get("gather_status", "")).strip() == "captured"
    )
    if captured_targets <= 0 and page_count <= 0:
        return "Gather the strongest candidates."
    if int(primary.get("row_count", 0) or 0) <= 0 and page_count > 0:
        return "Shape the primary object from gathered pages."
    if blocked_actions:
        return str(blocked_actions[0])
    return "Open Lab Notes and keep iterating."


def _primary_object_ready_for_lab(snapshot: dict[str, Any]) -> bool:
    readiness = snapshot.get("readiness", {}) or {}
    primary = _primary_object(snapshot)
    row_count = int(primary.get("row_count", 0) or 0)
    if row_count <= 0:
        return False
    readiness_status = str(readiness.get("overall_status", "")).strip()
    primary_status = str(primary.get("status", "")).strip()
    return readiness_status in {"exploratory_ready", "final_ready"} or primary_status in {
        "exploratory_ready",
        "final_ready",
    }


def _is_search_engine_route_url(url: str) -> bool:
    clean = str(url or "").strip().lower()
    return any(marker in clean for marker in ("google.com/search", "bing.com/search", "duckduckgo.com/?q=", "search.yahoo.com/search"))


def _gather_qa_collapse_state(snapshot: dict[str, Any]) -> dict[str, str] | None:
    gather_qa = snapshot.get("gather_qa", {}) or {}
    primary = _primary_object(snapshot)
    if int(primary.get("row_count", 0) or 0) > 0:
        return None

    reviewed_pages = int(gather_qa.get("reviewed_page_count", 0) or 0)
    if reviewed_pages < 4:
        return None

    accepted_like_fraction = float(gather_qa.get("accepted_like_fraction", 0.0) or 0.0)
    if accepted_like_fraction > 0.25:
        return None

    status_counts = {
        str(key): int(value)
        for key, value in dict(gather_qa.get("status_counts") or {}).items()
        if str(key).strip()
    }
    rejected_like_count = sum(status_counts.get(key, 0) for key in ("blocked", "redirect", "retry"))
    if rejected_like_count < max(3, reviewed_pages - 1):
        return None

    top_reasons = {
        str(reason).strip()
        for reason in dict(gather_qa.get("top_reasons") or {}).keys()
        if str(reason).strip()
    }
    if not top_reasons.intersection({"blocked_page", "schema_page_mismatch", "domain_mismatch", "search_engine_page"}):
        return None

    return {
        "copy": "Autopilot paused because Gather QA is collapsing on blocked or mismatched pages. Tighten the route hints or re-plan the Mission before another wave.",
    }


def _autopilot_action_state(capsule_dir: Path, snapshot: dict[str, Any]) -> dict[str, Any] | None:
    source_plan = snapshot.get("source_plan", {}) or {}
    gather_targets = snapshot.get("gather_targets", {}) or {}

    target_name = quote(capsule_dir.name)
    if _primary_object_ready_for_lab(snapshot):
        return {
            "kind": "lab",
            "href": "/capsule/{name}".format(name=target_name),
            "action_label": "Explore in Lab Notes",
            "countdown_label": "Lab Notes",
            "stage": "Lab Notes",
        }

    direct_source_ids = _pending_direct_source_ids(source_plan)
    if direct_source_ids:
        return {
            "kind": "scout",
            "action": "/mission/{name}/scout".format(name=target_name),
            "hidden_name_prefix": "route_",
            "selected_ids": direct_source_ids,
            "action_label": "Gather Direct Sources",
            "countdown_label": "Gather",
            "stage": "Trail Map",
            "loading_label": "Gathering direct sources",
        }

    route_ids = [
        str(source.get("source_id", "")).strip()
        for source in list(source_plan.get("sources", []) or [])
        if isinstance(source, dict)
        and str(source.get("source_id", "")).strip()
        and str(source.get("capture_status", "")).strip() != "captured"
        and str((source.get("entrypoint") or {}).get("mode", "")).strip().lower() == "query"
    ]
    if route_ids:
        return {
            "kind": "scout",
            "action": "/mission/{name}/scout".format(name=target_name),
            "hidden_name_prefix": "route_",
            "selected_ids": route_ids,
            "action_label": "Scout These Routes",
            "countdown_label": "Scout",
            "stage": "Trail Map",
            "loading_label": "Scouting selected routes",
        }

    target_ids = [
        str(target.get("target_id", "")).strip()
        for target in list(gather_targets.get("targets", []) or [])
        if isinstance(target, dict) and str(target.get("target_id", "")).strip() and str(target.get("gather_status", "")).strip() != "captured"
    ]
    if target_ids:
        return {
            "kind": "gather",
            "action": "/mission/{name}/gather-targets".format(name=target_name),
            "hidden_name_prefix": "target_",
            "selected_ids": target_ids,
            "action_label": "Gather These Hits",
            "countdown_label": "Gather",
            "stage": "Scout",
            "loading_label": "Gathering selected candidates",
        }

    primary = _primary_object(snapshot)
    if int(primary.get("row_count", 0) or 0) > 0:
        return {
            "kind": "lab",
            "href": "/capsule/{name}".format(name=target_name),
            "action_label": "Explore in Lab Notes",
            "countdown_label": "Lab Notes",
            "stage": "Lab Notes",
        }
    return None


def _pending_direct_source_ids(source_plan: dict[str, Any]) -> list[str]:
    direct_ids: list[str] = []
    for source in list(source_plan.get("sources", []) or []):
        if not isinstance(source, dict):
            continue
        source_id = str(source.get("source_id", "")).strip()
        if not source_id:
            continue
        if str(source.get("capture_status", "")).strip() == "captured":
            continue
        entrypoint = dict(source.get("entrypoint") or {})
        mode = str(entrypoint.get("mode", "")).strip().lower()
        if mode == "url" and not _is_search_engine_route_url(str(entrypoint.get("value", "")).strip()):
            direct_ids.append(source_id)
    return direct_ids


def _autopilot_action_html(capsule_dir: Path, snapshot: dict[str, Any], *, page_kind: str = "capsule") -> str:
    action_state = _autopilot_action_state(capsule_dir, snapshot)
    if not action_state:
        return ""
    if str(action_state.get("kind", "")).strip() == "lab":
        extras = []
        if page_kind == "mission":
            extras.append('data-autoboot-lab="true"')
        extras.append('data-autopilot-action="lab"')
        extras.append('data-autopilot-stage="{stage}"'.format(stage=html.escape(str(action_state.get("stage", "")))))
        extras.append(
            'data-autopilot-countdown-label="{label}"'.format(
                label=html.escape(str(action_state.get("countdown_label", "Lab Notes")))
            )
        )
        return '<a class="button-link secondary" href="{href}" {extras}>{label}</a>'.format(
            href=html.escape(str(action_state.get("href", ""))),
            extras=" ".join(extras),
            label=html.escape(str(action_state.get("action_label", "Explore in Lab Notes"))),
        )

    hidden_prefix = str(action_state.get("hidden_name_prefix", "")).strip()
    hidden_inputs = "".join(
        '<input type="hidden" name="{name}{value}" value="1">'.format(
            name=html.escape(hidden_prefix),
            value=html.escape(str(item)),
        )
        for item in list(action_state.get("selected_ids", []) or [])
        if str(item).strip()
    )
    return """
<form
  method="post"
  action="{action}"
  data-loading-label="{loading_label}"
  data-autopilot-action="{kind}"
  data-autopilot-stage="{stage}"
  data-autopilot-countdown-label="{countdown_label}"
>
  {hidden_inputs}
  <button type="submit" data-loading-label="{loading_label}">{label}</button>
</form>
""".format(
        action=html.escape(str(action_state.get("action", ""))),
        loading_label=html.escape(str(action_state.get("loading_label", "Agent is working"))),
        kind=html.escape(str(action_state.get("kind", ""))),
        stage=html.escape(str(action_state.get("stage", ""))),
        countdown_label=html.escape(str(action_state.get("countdown_label", ""))),
        hidden_inputs=hidden_inputs,
        label=html.escape(str(action_state.get("action_label", "Continue"))),
    )


def _autopilot_block_state(snapshot: dict[str, Any], notice: str = "") -> tuple[bool, str]:
    if _planner_clarification_state(snapshot):
        return True, "Autopilot is waiting for a clearer Mission before it moves again."
    if _mission_attention_state(snapshot):
        return True, "Autopilot is paused because this run needs manual intervention."
    qa_collapse = _gather_qa_collapse_state(snapshot)
    if qa_collapse:
        return True, str(qa_collapse.get("copy", "")).strip()
    notice_lower = notice.lower().strip()
    if notice_lower and (
        "timed out" in notice_lower
        or "failed unexpectedly" in notice_lower
        or "request failed" in notice_lower
        or notice_lower.endswith(" failed")
        or notice_lower.endswith(" failed.")
    ):
        return True, "Autopilot paused after a runtime failure. Review the stage and step in if needed."
    return False, ""


def _autopilot_banner_html(
    capsule_dir: Path,
    snapshot: dict[str, Any],
    *,
    page_kind: str = "capsule",
    notice: str = "",
) -> str:
    primary = _primary_object(snapshot)
    target = _first_target(snapshot.get("task_spec", {}) or {})
    min_rows = str((target.get("sample_target") or {}).get("min_rows", "n/a"))
    action_html = _autopilot_action_html(capsule_dir, snapshot, page_kind=page_kind)
    action_state = _autopilot_action_state(capsule_dir, snapshot)
    blocked, blocked_copy = _autopilot_block_state(snapshot, notice)
    if blocked:
        status_copy = blocked_copy
    elif action_state:
        status_copy = "Autopilot pauses for a couple of seconds so you can step in before the next move."
    else:
        status_copy = "Autopilot is waiting for the next usable stage."
    autoplay_attrs = ""
    if page_kind == "mission":
        autoplay_attrs = ' data-mission-autoplay="true" data-autopilot-delay-ms="2600" data-autopilot-blocked="{blocked}"'.format(
            blocked="true" if blocked else "false"
        )
    return """
<section class="autopilot"{autoplay_attrs}>
  <div>
    <div class="eyebrow">Autopilot</div>
    <h2>Show, then move on.</h2>
    <p class="muted">The system keeps going after each draft. Step in only if you want to redirect the plan.</p>
    <div class="autopilot-actions">{action_html}</div>
    <p id="autopilot-status" class="muted autopilot-status">{status_copy}</p>
  </div>
  <div class="autopilot-next">
    <span class="chip">next</span>
    <strong>{next_action}</strong>
    <span class="chip">{object_name}</span>
    <span class="chip">target {min_rows} rows</span>
    <span class="chip">{row_count} shaped</span>
  </div>
</section>
""".format(
        autoplay_attrs=autoplay_attrs,
        action_html=action_html,
        status_copy=html.escape(status_copy),
        next_action=html.escape(_autopilot_next(snapshot)),
        object_name=html.escape(str(primary.get("name", target.get("name", "object"))) or "object"),
        min_rows=html.escape(min_rows),
        row_count=html.escape(str(primary.get("row_count", 0))),
    )


def _mission_attention_state(snapshot: dict[str, Any]) -> dict[str, str] | None:
    source_plan = snapshot.get("source_plan", {}) or {}
    scout_summary = snapshot.get("scout_summary", {}) or {}
    gather_targets = snapshot.get("gather_targets", {}) or {}
    primary = _primary_object(snapshot)
    page_count = _captured_page_count(snapshot)
    route_count = len(list(source_plan.get("sources", []) or []))
    candidate_count = int(scout_summary.get("candidate_count", 0) or 0)
    target_count = len(list(gather_targets.get("targets", []) or []))
    row_count = int(primary.get("row_count", 0) or 0)

    if row_count > 0:
        return None
    if route_count <= 0:
        return {
            "title": "Autopilot needs a stronger Mission.",
            "copy": "The planner did not produce usable Trail Map routes. Rewrite the Mission prompt or add seed URLs in manual controls.",
        }
    if page_count > 0 and candidate_count <= 0 and target_count <= 0:
        return {
            "title": "Scout did not produce gather candidates.",
            "copy": "The current routes landed on pages that were not specific enough for Gather. Tighten the Mission prompt, add seed URLs, or override the row object and route hints.",
        }
    return None


def _mission_attention_html(snapshot: dict[str, Any]) -> str:
    attention = _mission_attention_state(snapshot)
    if not attention:
        return ""
    return """
<section class="panel attention-panel stage-section" data-stage-section="Scout">
  <div class="section-head">
    <div>
      <div class="eyebrow">Intervention Suggested</div>
      <h2>{title}</h2>
      <p class="muted">{copy}</p>
    </div>
  </div>
  <div class="actions mission-actions">
    <a class="button-link secondary" href="#manual-controls">Open manual controls</a>
  </div>
</section>
""".format(title=html.escape(attention["title"]), copy=html.escape(attention["copy"]))


def _planner_clarification_popup_html(snapshot: dict[str, Any]) -> str:
    state = _planner_clarification_state(snapshot)
    if not state:
        return ""
    capsule_name = str((snapshot.get("manifest", {}) or {}).get("name", "")).strip()
    example_items = "".join(
        "<li>{text}</li>".format(text=html.escape(str(item)))
        for item in state.get("examples", [])
        if str(item).strip()
    )
    suggestion_cards = ""
    suggestions = [item for item in state.get("suggestions", []) if isinstance(item, dict)]
    if suggestions and capsule_name:
        cards: list[str] = []
        for suggestion in suggestions[:3]:
            suggestion_id = str(suggestion.get("suggestion_id", "")).strip()
            if not suggestion_id:
                continue
            cards.append(
                """
<form method="post" action="/mission/{name}/apply-plan-suggestion" class="suggestion-card" data-loading-label="Updating the Mission plan">
  <input type="hidden" name="suggestion_id" value="{suggestion_id}">
  <strong>{label}</strong>
  <p class="muted">{description}</p>
  <button type="submit" class="button-link secondary">Use this plan</button>
</form>
""".format(
                    name=quote(capsule_name),
                    suggestion_id=html.escape(suggestion_id),
                    label=html.escape(str(suggestion.get("label", "")).strip() or suggestion_id),
                    description=html.escape(str(suggestion.get("description", "")).strip()),
                )
            )
        if cards:
            suggestion_cards = '<div class="suggestion-grid">{cards}</div>'.format(cards="".join(cards))
    return """
<section class="panel attention-panel stage-section" data-stage-section="Scout">
  <div class="section-head">
    <div>
      <div class="eyebrow">Planner feedback</div>
      <h2>{title}</h2>
      <p class="muted">{copy}</p>
      <p><strong>{question}</strong></p>
      <ul>{examples}</ul>
    </div>
  </div>
  {suggestions}
  <div class="actions mission-actions">
    <a class="button-link secondary" href="#manual-controls">Open manual controls</a>
  </div>
</section>
""".format(
        title=html.escape(str(state.get("title", "")).strip() or "Planner needs one more detail."),
        copy=html.escape(str(state.get("copy", "")).strip() or str(state.get("summary", "")).strip()),
        question=html.escape(
            str(state.get("question", "")).strip() or "What should one row represent in this mission?"
        ),
        examples=example_items or "<li>Add a concrete row object and the fields you care about most.</li>",
        suggestions=suggestion_cards,
    )


def _mission_action_block_notice(capsule_dir: Path) -> str:
    state = _planner_clarification_state(_mission_snapshot(capsule_dir))
    if not state:
        return ""
    title = str(state.get("title", "")).strip()
    summary = str(state.get("summary", "")).strip()
    question = str(state.get("question", "")).strip()
    return " ".join(part for part in [title, summary or question] if part).strip()


def _shape_failure_state(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    task_spec = snapshot.get("task_spec", {}) or {}
    gather_targets = snapshot.get("gather_targets", {}) or {}
    gather_qa = snapshot.get("gather_qa", {}) or {}
    readiness = snapshot.get("readiness", {}) or {}
    primary = _primary_object(snapshot)
    target = _first_target(task_spec)

    row_count = int(primary.get("row_count", 0) or 0)
    if row_count > 0:
        return None

    target_name = str(target.get("name", "")).strip()
    if not target_name:
        return None

    captured_targets = sum(
        1
        for target_item in gather_targets.get("targets", [])
        if isinstance(target_item, dict) and str(target_item.get("gather_status", "")).strip() == "captured"
    )
    reviewed_pages = int(gather_qa.get("reviewed_page_count", 0) or 0)
    gathered_page_count = sum(
        1
        for page in list((snapshot.get("manifest", {}) or {}).get("pages", []) or [])
        if isinstance(page, dict) and str(page.get("gather_target_id", "")).strip()
    )
    if max(captured_targets, reviewed_pages, gathered_page_count) <= 0:
        return None

    blocked_actions = [str(item).strip() for item in readiness.get("blocked_actions", []) if str(item).strip()]
    top_reasons = dict(gather_qa.get("top_reasons") or {})
    accepted_like_fraction = float(gather_qa.get("accepted_like_fraction", 0.0) or 0.0)

    if target_name == "records":
        title = "Shape needs a concrete row object first."
        copy = "Gather captured evidence, but this Mission is still targeting generic records. Re-plan the Mission or set the object manually before Shape can produce a usable table."
    elif any(reason in top_reasons for reason in ("schema_page_mismatch", "domain_mismatch", "search_engine_page")):
        title = "Gather found pages, but they are the wrong kind for `{name}`.".format(name=target_name)
        copy = "The current pages do not match the row object closely enough to shape useful rows. Let the system retry stronger same-family pages, or intervene and tighten the Mission and route hints."
    else:
        title = "Shape could not build `{name}` yet.".format(name=target_name)
        copy = "Gather finished, but the current pages did not shape into the primary object. Tighten the Mission, adjust route hints, or intervene manually before running Gather again."

    reason_bits: list[str] = []
    for key, count in sorted(top_reasons.items(), key=lambda item: int(item[1]), reverse=True)[:2]:
        reason_bits.append("{reason}: {count}".format(reason=str(key).replace("_", " "), count=int(count)))
    if accepted_like_fraction:
        reason_bits.append("accepted-like QA: {value:.0%}".format(value=accepted_like_fraction))

    return {
        "title": title,
        "copy": copy,
        "target_name": target_name,
        "captured_targets": captured_targets,
        "reviewed_pages": reviewed_pages,
        "page_count": gathered_page_count,
        "blocked_action": blocked_actions[0] if blocked_actions else "",
        "reasons": reason_bits,
    }


def _shape_failure_popup_html(snapshot: dict[str, Any]) -> str:
    state = _shape_failure_state(snapshot)
    if not state:
        return ""
    reason_html = "".join(
        '<span class="chip">{text}</span>'.format(text=html.escape(reason))
        for reason in state.get("reasons", [])
        if str(reason).strip()
    )
    blocked_html = ""
    if str(state.get("blocked_action", "")).strip():
        blocked_html = '<p class="muted"><strong>Next fix:</strong> {text}</p>'.format(
            text=html.escape(str(state["blocked_action"]))
        )
    return """
<section id="shape-failure-popup" class="mission-popup" role="status" aria-live="polite">
  <div class="eyebrow">Shape Alert</div>
  <h2>{title}</h2>
  <p class="muted">{copy}</p>
  <div class="chips">
    <span class="chip">{captured} gathered</span>
    <span class="chip">{reviewed} QA reviewed</span>
    <span class="chip">{pages} pages in capsule</span>
    {reason_html}
  </div>
  {blocked_html}
  <div class="actions">
    <button type="button" class="button-link secondary" data-open-manual-controls="true">Open manual controls</button>
    <button type="button" class="button-link ghost" data-dismiss-popup="true">Dismiss</button>
  </div>
</section>
""".format(
        title=html.escape(str(state["title"])),
        copy=html.escape(str(state["copy"])),
        captured=html.escape(str(int(state.get("captured_targets", 0) or 0))),
        reviewed=html.escape(str(int(state.get("reviewed_pages", 0) or 0))),
        pages=html.escape(str(int(state.get("page_count", 0) or 0))),
        reason_html=reason_html,
        blocked_html=blocked_html,
    )


def _stage_rail_html(capsule_dir: Path, snapshot: dict[str, Any]) -> str:
    manifest = snapshot.get("manifest", {}) or {}
    readiness = snapshot.get("readiness", {}) or {}
    capsule_state = snapshot.get("capsule_state", {}) or {}
    kernel_status = snapshot.get("kernel_status", {}) or {}
    agent_mode = str(snapshot.get("agent_mode", "builtin"))

    stage_rows = _workflow_stage_rows(snapshot)
    default_stage = _default_stage_name(stage_rows)
    stage_items_parts: list[str] = []
    for item in stage_rows:
        stage = str(item.get("stage", "")).strip()
        status = str(item.get("status", "")).strip()
        summary = str(item.get("summary", "")).strip()
        spotlight = _stage_spotlight_content(stage)
        stage_items_parts.append(
            """
<li class="rail-item rail-{status}">
  <button
    type="button"
    class="stage-trigger"
    data-stage-trigger="{stage}"
    data-stage-title="{title}"
    data-stage-copy="{copy}"
    data-stage-hint="{hint}"
    data-stage-energy="{energy}"{default_attr}
  >
    <span class="rail-dot"></span>
    <div>
      <strong>{stage}</strong>
      <p>{summary}</p>
    </div>
  </button>
</li>
""".format(
                stage=html.escape(stage),
                status=html.escape(status),
                title=html.escape(spotlight["title"]),
                copy=html.escape(spotlight["copy"]),
                hint=html.escape(spotlight["hint"]),
                energy=html.escape(str(_stage_trigger_energy(stage, status))),
                default_attr=' data-stage-default="true"' if stage == default_stage else "",
                summary=html.escape(summary),
            )
        )
    stage_items = "".join(stage_items_parts)
    return """
<aside id="status-panel" class="panel rail">
  <div class="eyebrow">Research Desk</div>
  <h2>{name}</h2>
  <p class="muted">{task}</p>
  <div class="chips">
    <span class="chip">workflow: {workflow}</span>
    <span class="chip">readiness: {readiness}</span>
    <span class="chip">agent: {agent}</span>
    <span class="chip">kernel: {kernel}</span>
  </div>
  <ol class="rail-list">{stage_items}</ol>
  <div class="rail-footer">
    <p class="muted">Default behavior is to show the draft, keep moving, and let you interrupt only if needed.</p>
  </div>
</aside>
""".format(
        name=html.escape(capsule_dir.name),
        task=html.escape(str(manifest.get("task", ""))),
        workflow=html.escape(str(capsule_state.get("stage", "planning"))),
        readiness=html.escape(str(readiness.get("overall_status", "planned"))),
        agent=html.escape(agent_mode),
        kernel=html.escape(str(kernel_status.get("state", "ready"))),
        stage_items=stage_items,
    )


def _scene_reel_html(snapshot: dict[str, Any]) -> str:
    stage_rows = {item["stage"]: item for item in _workflow_stage_rows(snapshot)}
    default_stage = _default_stage_name(list(stage_rows.values()))

    def node(stage: str, *, title: str | None = None) -> str:
        item = stage_rows.get(stage, {"stage": stage, "status": "waiting", "summary": "pending"})
        status = str(item.get("status", "waiting")).strip() or "waiting"
        summary = str(item.get("summary", "")).strip() or "pending"
        label = title or stage
        kicker = status.replace("_", " ")
        status_class = "dag-muted" if status == "waiting" else "dag-{status}".format(status=status)
        spotlight = _stage_spotlight_content(stage)
        return """
<button
  type="button"
  class="dag-node stage-trigger {status_class}"
  data-stage-trigger="{stage}"
  data-stage-title="{title}"
  data-stage-copy="{copy}"
  data-stage-hint="{hint}"
  data-stage-energy="{energy}"{default_attr}
>
  <div class="dag-kicker">{kicker}</div>
  <h3>{label}</h3>
  <p>{summary}</p>
</button>
""".format(
            status_class=html.escape(status_class),
            stage=html.escape(stage),
            title=html.escape(spotlight["title"]),
            copy=html.escape(spotlight["copy"]),
            hint=html.escape(spotlight["hint"]),
            energy=html.escape(str(_stage_trigger_energy(stage, status))),
            default_attr=' data-stage-default="true"' if stage == default_stage else "",
            kicker=html.escape(kicker),
            label=html.escape(label),
            summary=html.escape(summary),
        )

    def edge(next_stage: str) -> str:
        item = stage_rows.get(next_stage, {"status": "waiting"})
        status = str(item.get("status", "waiting")).strip() or "waiting"
        status_class = "dag-edge"
        if status != "waiting":
            status_class += " dag-edge-{status}".format(status=status)
        return '<div class="{status_class}"></div>'.format(status_class=html.escape(status_class))

    return """
<section class="panel reel-panel">
  <div class="section-head">
    <div>
      <div class="eyebrow">Desk Flow</div>
      <h2>Tap a stage to focus it</h2>
      <p class="muted">Use the rail or the flow to jump through the run. Gather QA branches off capture before the object is shaped.</p>
    </div>
  </div>
  <div class="scene-dag-shell">
    <div class="scene-dag">
      <div style="grid-column: 1; grid-row: 1;">{mission}</div>
      <div style="grid-column: 2; grid-row: 1;">{edge_trail}</div>
      <div style="grid-column: 3; grid-row: 1;">{trail}</div>
      <div style="grid-column: 4; grid-row: 1;">{edge_scout}</div>
      <div style="grid-column: 5; grid-row: 1;">{scout}</div>
      <div style="grid-column: 6; grid-row: 1;">{edge_gather}</div>
      <div style="grid-column: 7; grid-row: 1;">{gather}</div>
      <div class="dag-edge-vertical" style="grid-column: 7; grid-row: 2;"></div>
      <div style="grid-column: 7; grid-row: 3;">{gather_qa}</div>
      <div style="grid-column: 8; grid-row: 1;">{edge_shape}</div>
      <div style="grid-column: 9; grid-row: 1;">{shape}</div>
      <div style="grid-column: 10; grid-row: 1;">{edge_green}</div>
      <div style="grid-column: 11; grid-row: 1;">{green}</div>
      <div style="grid-column: 12; grid-row: 1;">{edge_notes}</div>
      <div style="grid-column: 13; grid-row: 1;">{notes}</div>
    </div>
  </div>
</section>
""".format(
        mission=node("Mission"),
        edge_trail=edge("Trail Map"),
        trail=node("Trail Map"),
        edge_scout=edge("Scout"),
        scout=node("Scout"),
        edge_gather=edge("Gather"),
        gather=node("Gather"),
        gather_qa=node("Gather QA"),
        edge_shape=edge("Shape"),
        shape=node("Shape"),
        edge_green=edge("Green Light"),
        green=node("Green Light"),
        edge_notes=edge("Lab Notes"),
        notes=node("Lab Notes"),
    )


def _index_html() -> str:
    from .cli import read_json

    create_form = _mission_create_form_html()
    buckets: dict[str, list[str]] = {
        "in_flight": [],
        "ready": [],
        "queued": [],
    }
    for capsule_dir in _capsule_dirs():
        manifest = read_json(capsule_dir / "manifest.json", {})
        capsule_state = read_json(capsule_dir / "capsule_state.json", {})
        readiness = read_json(capsule_dir / "readiness.json", {})
        object_manifest = read_json(capsule_dir / "object_manifest.json", {})
        primary = {}
        for item in object_manifest.get("objects", []):
            if isinstance(item, dict) and str(item.get("object_role", "")).strip() == "primary":
                primary = item
                break
        stage = str(capsule_state.get("stage", "planning"))
        status = str(capsule_state.get("status", "planned"))
        readiness_status = str(readiness.get("overall_status", "planned"))
        row_count = int(primary.get("row_count", 0) or 0)
        object_name = str(primary.get("name", "")).strip() or "pending"
        page_count = len(list(manifest.get("pages", []) or []))
        if readiness_status == "final_ready":
            energy = 100
            next_step = "Lab Notes is hot and ready to explore."
        elif row_count > 0:
            energy = 86
            next_step = "Shape landed. Push through Green Light."
        elif page_count > 0:
            energy = 62
            next_step = "Good evidence is in. Next move is Shape."
        elif stage == "analysis":
            energy = 44
            next_step = "Board is moving. Scout and Gather need stronger hits."
        else:
            energy = 24
            next_step = "Fresh mission. Time to draft routes and launch the hunt."
        card = """<article class="capsule-card">
  <div class="capsule-card-head">
    <div>
      <div class="eyebrow">Mission</div>
      <h2><a href="/mission/{name}">{name}</a></h2>
    </div>
    <div class="capsule-card-next">{next_step}</div>
  </div>
  <p>{task}</p>
  <div class="capsule-meter"><span style="width: {energy}%"></span></div>
  <div class="chips chips-wrap">
    <span class="chip">workflow: {stage}</span>
    <span class="chip">readiness: {readiness}</span>
    <span class="chip">object: {object_name}</span>
    <span class="chip">{row_count} rows</span>
  </div>
  <div class="actions inline-actions">
        <a class="button-link secondary" href="/mission/{name}">Mission</a>
        <a class="button-link ghost" href="/capsule/{name}">Lab Notes</a>
      </div>
</article>""".format(
            name=html.escape(capsule_dir.name),
            task=html.escape(str(manifest.get("task", "")) or "(no task)"),
            next_step=html.escape(next_step),
            energy=html.escape(str(energy)),
            stage=html.escape(stage),
            readiness=html.escape(readiness_status or status),
            object_name=html.escape(object_name),
            row_count=html.escape(str(row_count)),
        )
        if readiness_status == "final_ready":
            buckets["ready"].append(card)
        elif stage == "planning":
            buckets["queued"].append(card)
        else:
            buckets["in_flight"].append(card)

    def section(title: str, subtitle: str, items: list[str]) -> str:
        if not items:
            return ""
        return """
<section class="panel stage-section" data-stage-section="Scout">
  <div class="section-head">
    <div>
      <div class="eyebrow">{title}</div>
      <h2>{subtitle}</h2>
    </div>
  </div>
  <div class="capsules">{items}</div>
</section>
""".format(
            title=html.escape(title),
            subtitle=html.escape(subtitle),
            items="".join(items),
        )

    body = """
<div class="topbar">
  <div>
    <div class="eyebrow">Unchained Lab</div>
    <h1>Research Desk for local research.</h1>
    <p class="muted">Start with the question. The system drafts the plan, keeps moving, and lets you step in only when you want to redirect it.</p>
  </div>
</div>
{autopilot}
<div class="center-stage">
  {create_form}
</div>
<section class="panel rail">
  <div class="eyebrow">Flow</div>
  <h2>Show, then move on.</h2>
  <ol class="rail-list">
    <li class="rail-item rail-done"><span class="rail-dot"></span><div><strong>Mission</strong><p>Describe the task in plain language.</p></div></li>
    <li class="rail-item rail-ready"><span class="rail-dot"></span><div><strong>Trail Map</strong><p>The planner drafts sources and action budgets.</p></div></li>
    <li class="rail-item rail-ready"><span class="rail-dot"></span><div><strong>Scout → Gather</strong><p>MCP indexes routes, then captures the strongest pages.</p></div></li>
    <li class="rail-item rail-ready"><span class="rail-dot"></span><div><strong>Shape → Green Light</strong><p>Objects get structured, checked, and opened in Lab Notes.</p></div></li>
  </ol>
</section>
{in_flight}
{ready}
{queued}
<div class="capsules">
  {empty}
</div>
""".format(
        create_form=create_form,
        autopilot="""
<section class="autopilot">
  <div>
    <div class="eyebrow">Autopilot</div>
    <h2>Show, then move on.</h2>
    <p class="muted">Mission drafts are visible, the pipeline keeps moving, and manual controls stay tucked away until you need them.</p>
  </div>
  <div class="autopilot-next">
    <span class="chip">{count} missions</span>
    <span class="chip">{ready} final ready</span>
    <span class="chip">{in_flight} in flight</span>
  </div>
</section>
""".format(
            count=sum(len(items) for items in buckets.values()),
            ready=len(buckets["ready"]),
            in_flight=len(buckets["in_flight"]),
        ),
        in_flight=section("In Flight", "Active missions", buckets["in_flight"]),
        ready=section("Ready", "Ready for Lab Notes", buckets["ready"]),
        queued=section("Queued", "Planning and early setup", buckets["queued"]),
        empty="" if any(buckets.values()) else '<p class="muted">No capsules found.</p>',
    )
    return _html_page("Local Lab", body)


def _mission_page(capsule_dir: Path, *, notice: str = "") -> str:
    snapshot = _mission_snapshot(capsule_dir)
    manifest = snapshot["manifest"]
    task_spec = snapshot["task_spec"]
    source_plan = snapshot["source_plan"]
    capsule_state = snapshot["capsule_state"]
    readiness = snapshot["readiness"]
    object_manifest = snapshot["object_manifest"]
    scout_summary = snapshot["scout_summary"]
    gather_targets = snapshot["gather_targets"]
    gather_qa = snapshot["gather_qa"]
    gather_qa_review = snapshot["gather_qa_review"]
    mission_values = _mission_values(manifest, task_spec, source_plan)
    analysis_object_names = [
        str(item.get("name", "")).strip()
        for item in object_manifest.get("objects", [])
        if isinstance(item, dict)
        and str(item.get("name", "")).strip()
        and str(item.get("object_role", "")).strip() != "support"
    ]
    primary_object = _primary_object(snapshot)
    primary_row_count = int(primary_object.get("row_count", 0) or 0)
    support_object_count = sum(
        1
        for item in object_manifest.get("objects", [])
        if isinstance(item, dict) and str(item.get("object_role", "")).strip() == "support"
    )
    planner_clarification = _planner_clarification_state(snapshot)
    blocked_reason = ""
    if planner_clarification:
        blocked_reason = (
            str(planner_clarification.get("question", "")).strip()
            or str(planner_clarification.get("summary", "")).strip()
        )

    mission_panel = _mission_editor_html(
        values=mission_values,
        action="/mission/{name}".format(name=quote(capsule_dir.name)),
        capsule_name=capsule_dir.name,
        open_manual=_mission_attention_state(snapshot) is not None or planner_clarification is not None,
    )
    mission_attention = _mission_attention_html(snapshot)
    planner_feedback = _planner_clarification_popup_html(snapshot)
    planner_summary = _mission_summary_html(task_spec, source_plan)
    trail_map = _trail_map_html(source_plan, capsule_name=capsule_dir.name, blocked_reason=blocked_reason)
    mission_playbook = _mission_playbook_html(capsule_dir.name)
    scout_panel = _scout_panel_html(
        gather_targets,
        scout_summary,
        gather_qa,
        gather_qa_review,
        capsule_name=capsule_dir.name,
        blocked_reason=blocked_reason,
    )
    shape_summary = _shape_summary_html(object_manifest, capsule_name=capsule_dir.name)
    mission_notice = _mission_notice_html(notice)
    shape_failure_popup = _shape_failure_popup_html(snapshot)
    readiness_items = """
<section class="panel stage-section" data-stage-section="Green Light">
  <div class="section-head">
    <div>
      <div class="eyebrow">Green Light</div>
      <h2>{status}</h2>
      <p class="muted">Stage: {stage} / {workflow_status}</p>
    </div>
  </div>
  <div class="chips">
    <span class="chip">planned sources: {planned_sources}</span>
    <span class="chip">analysis objects: {object_count}</span>
    <span class="chip">support artifacts: {support_object_count}</span>
    <span class="chip">task type: {task_type}</span>
  </div>
  <p class="muted readiness-copy">{object_names}</p>
</section>
""".format(
        status=html.escape(str(readiness.get("overall_status", capsule_state.get("status", "planned")))),
        stage=html.escape(str(capsule_state.get("stage", "planning"))),
        workflow_status=html.escape(str(capsule_state.get("status", "planned"))),
        planned_sources=len(source_plan.get("sources", [])),
        object_count=len(analysis_object_names),
        support_object_count=support_object_count,
        task_type=html.escape(str(task_spec.get("task_type", manifest.get("task_type", "generic")))),
        object_names=html.escape(
            "Objects: " + (", ".join(analysis_object_names) if analysis_object_names else "not shaped yet")
        ),
    )
    shape_main = shape_summary if primary_row_count > 0 else ""
    shape_side = "" if primary_row_count > 0 else shape_summary

    body = """
<div class="topbar">
  <div>
    <div class="eyebrow">Mission</div>
    <h1>{name}</h1>
    <p class="muted">A guided mission surface that shows each draft and keeps moving unless you choose to redirect it.</p>
  </div>
  <div class="actions inline-actions">
    <a class="button-link secondary" href="/">All Missions</a>
    <a class="button-link ghost" href="/capsule/{name}">Open Lab Notes</a>
  </div>
</div>
{mission_notice}
{shape_failure_popup}
<div class="center-stage">
  {mission_panel}
</div>
  <div class="grid">
  {stage_rail}
  <div class="main-column">
    {autopilot}
    {board_spotlight}
    {planner_feedback}
    {mission_attention}
    {mission_playbook}
    {reel}
    <div class="mission-layout">
      <div class="mission-main">
        {planner_summary}
        {trail_map}
        {shape_main}
        {scout_panel}
      </div>
      <div class="mission-side">
        {shape_side}
        {readiness_items}
      </div>
    </div>
  </div>
</div>
""".format(
        name=html.escape(capsule_dir.name),
        mission_notice=mission_notice,
        shape_failure_popup=shape_failure_popup,
        planner_feedback=planner_feedback,
        planner_summary=planner_summary,
        trail_map=trail_map,
        shape_main=shape_main,
        scout_panel=scout_panel,
        shape_side=shape_side,
        readiness_items=readiness_items,
        mission_panel=mission_panel,
        mission_attention=mission_attention,
        stage_rail=_stage_rail_html(capsule_dir, snapshot),
        autopilot=_autopilot_banner_html(capsule_dir, snapshot, page_kind="mission", notice=notice),
        board_spotlight=_board_spotlight_html(snapshot),
        mission_playbook=mission_playbook,
        reel=_scene_reel_html(snapshot),
    )
    return _html_page("Mission / {name}".format(name=capsule_dir.name), body)


def _mission_overrides_from_form(form: dict[str, str]) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    scalar_fields = {
        "objective": "objective",
        "object_name": "name",
        "object_description": "description",
        "object_grain": "grain",
    }
    for field_name, override_name in scalar_fields.items():
        value = str(form.get(field_name, "")).strip()
        if value:
            overrides[override_name] = value

    list_fields = {
        "questions": _split_lines,
        "primary_key": _split_csv,
        "measures": _split_csv,
        "dimensions": _split_csv,
        "required_columns": _split_csv,
    }
    for field_name, parser in list_fields.items():
        values = parser(str(form.get(field_name, "")))
        if values:
            if field_name == "questions":
                overrides["questions"] = values
            else:
                overrides[field_name] = values

    min_rows = str(form.get("min_rows", "")).strip()
    if min_rows:
        overrides["min_rows"] = min_rows
    return overrides


def _merge_mission_plan_and_overrides(
    mission_plan: dict[str, Any],
    manual_overrides: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(mission_plan)
    for key, value in manual_overrides.items():
        if isinstance(value, list):
            if value:
                merged[key] = value
            continue
        text = str(value).strip()
        if text:
            merged[key] = text
    return merged


def _gather_source_ids_from_form(form: dict[str, str], source_plan: dict[str, Any]) -> list[str]:
    selected: list[str] = []
    for key, value in form.items():
        if not key.startswith("route_"):
            continue
        if str(value).strip() not in {"1", "true", "on", "yes"}:
            continue
        source_id = key[len("route_"):].strip()
        if source_id:
            selected.append(source_id)
    if selected:
        return selected
    return [
        str(source.get("source_id", "")).strip()
        for source in source_plan.get("sources", [])
        if isinstance(source, dict)
        and str(source.get("source_id", "")).strip()
        and str(source.get("capture_status", "")).strip() != "captured"
    ]


def _gather_target_ids_from_form(form: dict[str, str], gather_targets: dict[str, Any]) -> list[str]:
    selected: list[str] = []
    for key, value in form.items():
        if not key.startswith("target_"):
            continue
        if str(value).strip() not in {"1", "true", "on", "yes"}:
            continue
        target_id = key[len("target_"):].strip()
        if target_id:
            selected.append(target_id)
    if selected:
        return selected
    return [
        str(target.get("target_id", "")).strip()
        for target in gather_targets.get("targets", [])
        if isinstance(target, dict)
        and str(target.get("target_id", "")).strip()
        and str(target.get("gather_status", "")).strip() != "captured"
    ]


def _compact_review_note(note: Any) -> str:
    text = str(note).strip()
    if not text:
        return ""
    if ":" in text:
        prefix, _remainder = text.split(":", 1)
        prefix = prefix.strip()
        if prefix:
            return prefix
    text = re.sub(r"\s+", " ", text)
    if len(text) > 72:
        return text[:69].rstrip() + "..."
    return text


def _ordered_note_labels(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        item = str(value).strip()
        if not item or item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered


def _scout_panel_html(
    gather_targets: dict[str, Any],
    scout_summary: dict[str, Any],
    gather_qa: dict[str, Any],
    gather_qa_review: dict[str, Any],
    *,
    capsule_name: str,
    blocked_reason: str = "",
) -> str:
    from .cli import DEFAULT_GATHER_PARALLEL_TABS

    targets = list(gather_targets.get("targets", []))
    candidate_count = int(scout_summary.get("candidate_count", 0))
    qa_status_counts = {
        str(key): int(value)
        for key, value in dict(gather_qa.get("status_counts") or {}).items()
        if str(key)
    }
    qa_summary = ""
    if gather_qa:
        qa_chips = "".join(
            '<span class="chip">{label}: {count}</span>'.format(
                label=html.escape(status.replace("_", " ")),
                count=count,
            )
            for status, count in sorted(qa_status_counts.items())
        )
        top_reasons = ", ".join(
            "{reason} ({count})".format(reason=str(reason).replace("_", " "), count=count)
            for reason, count in list(dict(gather_qa.get("top_reasons") or {}).items())[:3]
        )
        qa_summary = """
  <div class="status-actions">
    <p class="muted">Gather QA reviewed {reviewed} page(s). Accepted-like coverage: {accepted_like}.</p>
    <div class="chips chips-wrap">{qa_chips}</div>
    {top_reasons}
  </div>
""".format(
            reviewed=int(gather_qa.get("reviewed_page_count", 0) or 0),
            accepted_like=html.escape(str(gather_qa.get("accepted_like_fraction", 0.0))),
            qa_chips=qa_chips or '<span class="chip">no qa statuses yet</span>',
            top_reasons=(
                '<p class="muted">Top issues: {issues}</p>'.format(issues=html.escape(top_reasons))
                if top_reasons
                else ""
            ),
        )
    review_summary = ""
    if gather_qa_review:
        review_mode = str(gather_qa_review.get("review_mode", "")).strip() or "none"
        review_count = int(gather_qa_review.get("reviewed_page_count", 0) or 0)
        review_note_labels = _ordered_note_labels(
            [
                _compact_review_note(item)
                for item in list(gather_qa_review.get("notes", []))[:3]
                if _compact_review_note(item)
            ]
        )
        review_note_chips = "".join(
            '<span class="chip">{label}</span>'.format(label=html.escape(label))
            for label in review_note_labels
        )
        review_mode_label = review_mode.replace("-", " ")
        review_mode_copy = "Bounded QA agent reviewed {count} ambiguous page(s) in {mode} mode.".format(
            count=review_count,
            mode=review_mode_label,
        )
        if "fallback" in review_mode:
            review_mode_copy = (
                "Bounded QA agent reviewed {count} ambiguous page(s) in {mode} mode after the external review path failed."
            ).format(
                count=review_count,
                mode=review_mode_label,
            )
        review_summary = """
  <div class="status-actions">
    <p class="muted">{copy}</p>
    {notes}
  </div>
""".format(
            copy=html.escape(review_mode_copy),
            notes=(
                '<div class="chips chips-wrap">{notes}</div>'.format(notes=review_note_chips)
                if review_note_chips
                else ""
            ),
        )
    if not targets:
        return """
<section class="panel attention-panel">
  <div class="section-head">
    <div>
      <div class="eyebrow">Scout</div>
      <h2>No gather candidates yet</h2>
      <p class="muted">Run Scout on one or more Trail Map routes first. If this still stays empty after Scout, open manual controls and tighten the Mission or add seed URLs.</p>
    </div>
  </div>
  <div class="actions mission-actions">
    <a class="button-link secondary" href="#manual-controls">Open manual controls</a>
  </div>
  {qa_summary}
  {review_summary}
</section>
""".format(qa_summary=qa_summary, review_summary=review_summary)
    if blocked_reason:
        return """
<section class="panel attention-panel">
  <div class="section-head">
    <div>
      <div class="eyebrow">Scout</div>
      <h2>Gather is blocked until the Mission is clearer</h2>
      <p class="muted">{blocked_reason}</p>
    </div>
  </div>
  {qa_summary}
  {review_summary}
  <div class="actions mission-actions">
    <button type="button" disabled>Gather blocked</button>
    <a class="button-link secondary" href="#manual-controls">Open manual controls</a>
  </div>
</section>
""".format(
            blocked_reason=html.escape(blocked_reason),
            qa_summary=qa_summary,
            review_summary=review_summary,
        )
    cards: list[str] = []
    for target in targets[:10]:
        entrypoint = target.get("entrypoint") or {}
        target_id = str(target.get("target_id", "")).strip()
        cards.append(
            """
<article class="route-card">
  <label class="route-select">
    <input type="checkbox" name="target_{target_id}" value="1"{checked}>
    <span>Include in Gather</span>
  </label>
  <div class="route-meta">
    <span class="chip">{status}</span>
    <span class="chip">{mode}</span>
    <span class="chip">{domain}</span>
    <span class="chip">score {score}</span>
  </div>
  <div class="route-url">{value}</div>
  <p class="muted">{title}</p>
</article>
""".format(
                target_id=html.escape(target_id),
                checked=" checked" if str(target.get("gather_status", "")) != "captured" else "",
                status=html.escape(str(target.get("gather_status", "planned"))),
                mode=html.escape(str(entrypoint.get("mode", "url"))),
                domain=html.escape(str(target.get("domain_hint", "")) or "candidate"),
                score=html.escape(str(target.get("scout_score", 0))),
                value=html.escape(str(entrypoint.get("value", ""))),
                title=html.escape(str(target.get("title", ""))),
            )
        )
    return """
<section class="panel stage-section" data-stage-section="Scout">
  <div class="section-head">
    <div>
      <div class="eyebrow">Scout</div>
      <h2>Gather candidates</h2>
      <p class="muted">Scout indexed {candidate_count} candidate results. Pick the strongest follow-up pages to gather in depth.</p>
    </div>
  </div>
  {qa_summary}
  {review_summary}
  <form method="post" action="/mission/{name}/gather-targets" data-loading-label="Gathering selected candidates">
    <div class="mission-process">
      <div class="process-step">
        <strong>1. Open tabs</strong>
        <span>Gather spins up to {parallel_tabs} browser tabs in parallel for the selected candidates.</span>
      </div>
      <div class="process-step">
        <strong>2. Capture pages</strong>
        <span>Each tab runs MCP capture, DDM, and page text extraction against its own target page.</span>
      </div>
      <div class="process-step">
        <strong>3. Refresh shape</strong>
        <span>When the batch finishes, Shape reruns and Green Light updates against the newly gathered pages.</span>
      </div>
    </div>
    <div class="route-list">{cards}</div>
    <div class="actions mission-actions">
      <button type="submit" data-loading-label="Gathering selected candidates">Gather These Hits</button>
      <span class="muted">Uses MCP to capture detail pages or targeted follow-up searches across up to {parallel_tabs} tabs.</span>
    </div>
  </form>
</section>
""".format(
        candidate_count=candidate_count,
        name=quote(capsule_name),
        cards="".join(cards),
        parallel_tabs=DEFAULT_GATHER_PARALLEL_TABS,
        qa_summary=qa_summary,
        review_summary=review_summary,
    )


def _save_mission(capsule_dir: Path, form: dict[str, str]) -> None:
    from .cli import read_json, refresh_analysis, sync_task_files, update_manifest, write_json
    from .planning import build_object_manifest, build_readiness

    manifest = read_json(capsule_dir / "manifest.json", {})
    prompt = str(form.get("mission_prompt", "")).strip()
    if prompt:
        manifest["task"] = prompt
    manifest["name"] = capsule_dir.name
    update_manifest(capsule_dir, manifest)

    existing_task_spec = read_json(capsule_dir / "task_spec.json", {})
    mission_plan = plan_mission(prompt or str(manifest.get("task", "")), capsule_dir=capsule_dir, existing_task_spec=existing_task_spec)
    manual_overrides = _mission_overrides_from_form(form)
    merged_overrides = _merge_mission_plan_and_overrides(mission_plan, manual_overrides)
    existing_task_spec["mission_overrides"] = merged_overrides
    existing_task_spec["mission_plan"] = mission_plan
    write_json(capsule_dir / "task_spec.json", existing_task_spec)

    source_urls = _split_lines(str(form.get("seed_urls", "")))
    if not source_urls:
        source_urls = [str(url).strip() for url in mission_plan.get("seed_urls", []) if str(url).strip()]
    stage = "analysis" if manifest.get("pages") else "planning"
    status = "exploratory_ready" if manifest.get("pages") else "planned"
    task_spec, _source_plan, _capsule_state = sync_task_files(
        capsule_dir,
        manifest,
        source_urls=source_urls,
        stage=stage,
        status=status,
    )
    if manifest.get("pages"):
        refresh_analysis(capsule_dir, manifest)
        return

    object_manifest = build_object_manifest(capsule_dir, task_spec)
    readiness = build_readiness(task_spec, object_manifest)
    write_json(capsule_dir / "object_manifest.json", object_manifest)
    write_json(capsule_dir / "readiness.json", readiness)


def _apply_plan_suggestion(capsule_dir: Path, suggestion_id: str) -> str:
    from .cli import read_json, refresh_analysis, sync_task_files, update_manifest, write_json
    from .planning import build_object_manifest, build_readiness

    manifest = read_json(capsule_dir / "manifest.json", {})
    task_spec = read_json(capsule_dir / "task_spec.json", {})
    review = read_json(capsule_dir / "object_decision_review.json", {})
    suggestions = [item for item in review.get("suggestions", []) if isinstance(item, dict)]
    selected = next(
        (item for item in suggestions if str(item.get("suggestion_id", "")).strip() == suggestion_id.strip()),
        None,
    )
    if not selected:
        return "Planner suggestion is no longer available."

    existing_overrides = dict(task_spec.get("mission_overrides") or {})
    suggestion_overrides = dict(selected.get("mission_overrides") or {})
    merged_overrides = _merge_mission_plan_and_overrides(existing_overrides, suggestion_overrides)
    task_spec["mission_overrides"] = merged_overrides
    write_json(capsule_dir / "task_spec.json", task_spec)

    manifest["name"] = capsule_dir.name
    update_manifest(capsule_dir, manifest)
    source_urls = [str(url).strip() for url in merged_overrides.get("seed_urls", []) if str(url).strip()]
    stage = "analysis" if manifest.get("pages") else "planning"
    status = "exploratory_ready" if manifest.get("pages") else "planned"
    synced_task_spec, _source_plan, _capsule_state = sync_task_files(
        capsule_dir,
        manifest,
        source_urls=source_urls,
        stage=stage,
        status=status,
    )
    if manifest.get("pages"):
        refresh_analysis(capsule_dir, manifest)
    else:
        object_manifest = build_object_manifest(capsule_dir, synced_task_spec)
        readiness = build_readiness(synced_task_spec, object_manifest)
        write_json(capsule_dir / "object_manifest.json", object_manifest)
        write_json(capsule_dir / "readiness.json", readiness)
    return str(selected.get("label", "")).strip() or "Mission plan updated."


def _capsule_snapshot(capsule_dir: Path, *, refresh: bool) -> dict[str, Any]:
    from .cli import read_json, refresh_analysis

    manifest = read_json(capsule_dir / "manifest.json", {})
    if refresh:
        refresh_analysis(capsule_dir, manifest)
    brief = read_json(capsule_dir / "capture_brief.json", {})
    task_spec = read_json(capsule_dir / "task_spec.json", {})
    source_plan = read_json(capsule_dir / "source_plan.json", {})
    object_manifest = read_json(capsule_dir / "object_manifest.json", {})
    readiness = read_json(capsule_dir / "readiness.json", {})
    capsule_state = read_json(capsule_dir / "capsule_state.json", {})
    session = get_session(capsule_dir)
    turns = session.read_turns()
    return {
        "manifest": manifest,
        "brief": brief,
        "task_spec": task_spec,
        "source_plan": source_plan,
        "object_manifest": object_manifest,
        "readiness": readiness,
        "capsule_state": capsule_state,
        "summary": brief.get("summary", {}),
        "session": session,
        "turns": turns,
        "agent_mode": detect_agent_mode(),
        "kernel_status": session.runtime_status(),
    }


def _render_turn(turn: dict[str, Any]) -> str:
    return _render_turn_with_context(turn, previous_turn=None, next_turn=None)


def _turn_scene_label(turn: dict[str, Any]) -> str:
    cell_type = str(turn.get("cell_type", "markdown"))
    role = str(turn.get("role", "system"))
    content = str(turn.get("content", "")).strip()
    if cell_type == "query":
        return "Question"
    if cell_type == "code":
        return "Planned analysis" if role == "agent" else "Manual code"
    if cell_type == "output":
        return "Execution result"
    if cell_type == "markdown" and role == "agent":
        if content.lower().startswith("## agent error"):
            return "Agent error"
        return "Takeaway"
    if cell_type == "markdown" and role == "user":
        return "Note"
    return cell_type.replace("_", " ").title()


def _render_turn_with_context(
    turn: dict[str, Any],
    *,
    previous_turn: dict[str, Any] | None,
    next_turn: dict[str, Any] | None,
) -> str:
    cell_type = str(turn.get("cell_type", "markdown"))
    role = str(turn.get("role", "system"))
    created_at = html.escape(str(turn.get("created_at", "")))
    tag = html.escape(_turn_scene_label(turn))
    meta = (
        '<div class="turn-meta"><span class="turn-tag">{tag}</span>'
        "<span>{role}</span><span>{created_at}</span></div>"
    ).format(tag=tag, role=html.escape(role), created_at=created_at)

    if cell_type == "markdown":
        body = '<div class="turn-body markdown">{content}</div>'.format(
            content=_render_markdown_like(str(turn.get("content", "")))
        )
    elif cell_type == "query":
        body = '<div class="turn-body"><div class="prompt-line"><span class="sigil">&gt;</span><span>{content}</span></div></div>'.format(
            content=html.escape(str(turn.get("content", "")))
        )
    elif cell_type == "code":
        content = html.escape(str(turn.get("content", "")))
        if role == "agent":
            body = (
                '<div class="turn-body"><details>'
                '<summary>Agent code</summary>'
                '<pre><code>{content}</code></pre>'
                '</details></div>'
            ).format(content=content)
        else:
            body = '<div class="turn-body"><pre><code>{content}</code></pre></div>'.format(content=content)
    else:
        error_block = ""
        if turn.get("error"):
            error_block = '<pre class="traceback">{error}</pre>'.format(
                error=html.escape(str(turn.get("error", "")))
            )
        raw_content = str(turn.get("content", ""))
        content_html = '<pre class="stdout-block">{content}</pre>'.format(content=html.escape(raw_content))
        if _looks_like_markdown_table_output(raw_content):
            content_html = '<div class="markdown">{content}</div>'.format(
                content=_render_markdown_like(raw_content)
            )
        elif _looks_like_dataframe_info_output(raw_content):
            content_html = _render_dataframe_info_output(raw_content)
        elif _looks_like_plaintext_table_output(raw_content):
            content_html = _render_plaintext_table_output(raw_content)
        is_agent_stdout = (
            previous_turn is not None
            and str(previous_turn.get("cell_type", "")) == "code"
            and str(previous_turn.get("role", "")) == "agent"
            and next_turn is not None
            and str(next_turn.get("cell_type", "")) == "markdown"
            and str(next_turn.get("role", "")) == "agent"
        )
        if is_agent_stdout:
            body = (
                '<div class="turn-body"><details>'
                '<summary>Execution output</summary>'
                '<div class="status-{status}">{content}</div>{error}'
                '</details></div>'
            ).format(
                status=html.escape(str(turn.get("status", "ok"))),
                content=content_html,
                error=error_block,
            )
        else:
            body = '<div class="turn-body"><div class="status-{status}">{content}</div>{error}</div>'.format(
                status=html.escape(str(turn.get("status", "ok"))),
                content=content_html,
                error=error_block,
            )
    return '<article class="turn turn-{cell_type}" tabindex="-1" data-cell-type="{cell_type}" data-role="{role}">{meta}{body}</article>'.format(
        cell_type=html.escape(cell_type),
        role=html.escape(role),
        meta=meta,
        body=body,
    )


def _turns_html(turns: list[dict[str, Any]]) -> str:
    if not turns:
        return (
            '<div class="empty-state">No turns yet. Ask a question, run code, or add a note from the composer below.</div>'
        )
    parts: list[str] = []
    for index, turn in enumerate(turns):
        previous_turn = turns[index - 1] if index > 0 else None
        next_turn = turns[index + 1] if index + 1 < len(turns) else None
        parts.append(_render_turn_with_context(turn, previous_turn=previous_turn, next_turn=next_turn))
    return "\n".join(parts)


def _latest_agent_query_and_code(turns: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    code_turn: dict[str, Any] | None = None
    query_turn: dict[str, Any] | None = None
    for turn in reversed(turns):
        if code_turn is None and str(turn.get("cell_type")) == "code" and str(turn.get("role")) == "agent":
            code_turn = turn
            continue
        if code_turn is not None and str(turn.get("cell_type")) == "query" and str(turn.get("role")) == "user":
            query_turn = turn
            break
    return query_turn, code_turn


def _status_panel_html(capsule_dir: Path, snapshot: dict[str, Any]) -> str:
    return _stage_rail_html(capsule_dir, snapshot)


def _capsule_prompt_sparks_html(snapshot: dict[str, Any]) -> str:
    primary = _primary_object(snapshot)
    primary_name = str(primary.get("name", "")).strip()
    row_count = int(primary.get("row_count", 0) or 0)
    lead_frame = "{name}_df".format(name=primary_name) if primary_name and row_count > 0 else "source_df"
    sparks = [
        (
            "Rank the signal",
            "Use {frame} to show the strongest evidence and why it wins.".format(frame=lead_frame),
        ),
        (
            "Show the weak spots",
            "Use gather_qa_df to surface thin evidence, duplicates, or pages that need follow-up.",
        ),
        (
            "Explain the block",
            "Summarize why Shape or Green Light is blocked and what the next best fix is.",
        ),
        (
            "Draft the next move",
            "Give me the next question or code step that will move this mission forward fast.",
        ),
    ]
    chips = "".join(
        _spark_chip_html(
            title=title,
            subtitle=subtitle,
            fill_target="content",
            fill_value=subtitle,
        )
        for title, subtitle in sparks
    )
    return """
  <div class="composer-sparks">
    <label class="composer-label">Hot Prompts</label>
    <div class="spark-strip">{chips}</div>
  </div>
""".format(chips=chips)


def _lab_autoboot_ready(snapshot: dict[str, Any]) -> bool:
    primary = _primary_object(snapshot)
    if int(primary.get("row_count", 0) or 0) <= 0:
        return False
    meaningful_turns = sum(
        1
        for turn in list(snapshot.get("turns", []) or [])
        if isinstance(turn, dict)
        and str(turn.get("role", "")).strip() in {"user", "agent"}
        and str(turn.get("content", "")).strip()
    )
    return meaningful_turns <= 0


def _lab_autoboot_prompt(snapshot: dict[str, Any]) -> str:
    manifest = snapshot.get("manifest", {}) or {}
    primary = _primary_object(snapshot)
    readiness = snapshot.get("readiness", {}) or {}
    task = str(manifest.get("task", "")).strip() or "current mission"
    object_name = str(primary.get("name", "")).strip() or "primary object"
    row_count = int(primary.get("row_count", 0) or 0)
    readiness_status = str(readiness.get("overall_status", "")).strip() or "exploratory_ready"
    return (
        "Use recommended_dataframe to start Lab Notes for this mission. Show a compact dataframe preview, explain the most "
        "important columns, summarize source quality or blockers briefly, and answer the main mission question at a high "
        "level. Mission: {task}. Primary object: {object_name} with {row_count} rows and readiness {readiness_status}."
    ).format(
        task=task,
        object_name=object_name,
        row_count=row_count,
        readiness_status=readiness_status,
    )


def _composer_html(capsule_dir: Path, snapshot: dict[str, Any]) -> str:
    name = html.escape(capsule_dir.name)
    autoboot_ready = "true" if _lab_autoboot_ready(snapshot) else "false"
    autoboot_prompt = html.escape(_lab_autoboot_prompt(snapshot))
    return """
<form class="composer" method="post" data-async-form="true" data-autoboot-ready="{autoboot_ready}" data-autoboot-prompt="{autoboot_prompt}">
  <div class="dock-head">
    <div>
      <label class="composer-label" for="composer-input">Command Dock</label>
      <p class="muted">Drafts are shown first, then the system keeps moving. Step in only if you want to redirect the reel.</p>
    </div>
    <div class="chips">
      <span class="chip">default: ask agent</span>
      <span class="chip">shift+enter</span>
      <span class="chip">autopilot on</span>
    </div>
  </div>
  {sparks}
  <textarea
    id="composer-input"
    name="content"
    spellcheck="false"
    placeholder="Ask, add a note, or run code. The system will show the draft and keep moving."
  ></textarea>
  <div class="actions">
    <button type="submit" formaction="/capsule/{name}/ask" data-kind="ask" data-loading-label="Agent is drafting the next step">Ask</button>
    <button type="submit" formaction="/capsule/{name}/code" data-kind="code" data-loading-label="Running code in pyreplab" class="secondary">Run Code</button>
    <button type="submit" formaction="/capsule/{name}/wait" data-kind="wait" data-loading-label="Waiting for pyreplab to finish" class="secondary">Wait</button>
    <button type="submit" formaction="/capsule/{name}/markdown" data-kind="markdown" data-loading-label="Saving note" class="secondary">Note</button>
    <button type="submit" formaction="/capsule/{name}/reset" data-kind="reset" data-loading-label="Resetting local session" class="ghost">Reset Session</button>
  </div>
  <div id="composer-status" class="composer-status">Ready</div>
</form>
""".format(
        name=name,
        sparks=_capsule_prompt_sparks_html(snapshot),
        autoboot_ready=autoboot_ready,
        autoboot_prompt=autoboot_prompt,
    )


def _capsule_page(capsule_dir: Path) -> str:
    snapshot = _capsule_snapshot(capsule_dir, refresh=True)
    manifest = snapshot["manifest"]
    sidebar = _stage_rail_html(capsule_dir, snapshot)
    terminal = """
<section class="terminal stage-section" data-stage-section="Lab Notes">
  <div class="terminal-head">
    <div>
      <div class="eyebrow">Lab Notes</div>
      <h2><a href="/">Local Lab</a> / {name}</h2>
      <p class="muted">A live reel of questions, drafted analysis, execution, and takeaways backed by pyreplab.</p>
    </div>
    <div class="chips">
      <span class="chip">task: {task_type}</span>
      <span class="chip">capsule: {name}</span>
      <a class="button-link ghost" href="/mission/{name}">Mission</a>
    </div>
  </div>
  {dataframes}
  {reel}
  <div id="turn-stream" class="turn-stream">{turns_html}</div>
  {composer}
</section>
""".format(
        name=html.escape(capsule_dir.name),
        task_type=html.escape(str(manifest.get("task_type", "generic"))),
        dataframes=_lab_dataframe_inventory_html(snapshot),
        reel=_scene_reel_html(snapshot),
        turns_html=_turns_html(snapshot["turns"]),
        composer=_composer_html(capsule_dir, snapshot),
    )

    body = """
<div class="topbar">
  <div>
    <div class="eyebrow">Unchained Lab</div>
    <h1><a href="/">Local Lab</a> / {name}</h1>
    <p class="muted">Research Desk for local analysis. The system shows each draft, keeps moving, and lets you step in only when you want to.</p>
  </div>
</div>
<div class="grid">
  {sidebar}
  <div class="main-column">
    {autopilot}
    {board_spotlight}
    {terminal}
  </div>
</div>
""".format(
        name=html.escape(capsule_dir.name),
        sidebar=sidebar,
        autopilot=_autopilot_banner_html(capsule_dir, snapshot, page_kind="capsule"),
        board_spotlight=_board_spotlight_html(snapshot),
        terminal=terminal,
    )
    return _html_page(capsule_dir.name, body)


def _lab_dataframe_inventory_html(snapshot: dict[str, Any]) -> str:
    session = snapshot["session"]
    try:
        session.ensure_initialized()
    except Exception:
        return ""
    namespace_rows = [row for row in session.namespace_rows() if isinstance(row, dict)]
    dataframe_rows = [
        row for row in namespace_rows
        if str(row.get("name", "")).endswith("_df") or str(row.get("type", "")) == "DataFrame"
    ]
    if not dataframe_rows:
        return ""

    object_manifest = snapshot.get("object_manifest", {}) or {}
    preferred_names: list[str] = []
    primary = _primary_object(snapshot)
    primary_name = str(primary.get("name", "")).strip()
    if primary_name:
        preferred_names.append("{name}_df".format(name=primary_name))
    for item in object_manifest.get("objects", []):
        if not isinstance(item, dict):
            continue
        object_name = str(item.get("name", "")).strip()
        if object_name:
            preferred_names.append("{name}_df".format(name=object_name))
    preferred_names.extend(["source_df", "entity_df", "district_metrics_df", "ranked_districts_df"])
    priority = {name: index for index, name in enumerate(preferred_names)}
    dataframe_rows.sort(
        key=lambda row: (
            priority.get(str(row.get("name", "")), 999),
            -int(row.get("size", 0) or 0),
            str(row.get("name", "")),
        )
    )

    def info_preview(row: dict[str, Any]) -> str:
        name = str(row.get("name", "")).strip() or "frame"
        row_count = int(row.get("size", 0) or 0)
        columns = [str(column) for column in row.get("columns", []) if str(column).strip()]
        dtypes = row.get("dtypes") or {}
        lines = [
            "{name}.info()".format(name=name),
            "RangeIndex: {rows} entries".format(rows=row_count),
            "Data columns (total {count} columns):".format(count=len(columns)),
        ]
        if columns:
            for index, column in enumerate(columns[:8]):
                dtype = str(dtypes.get(column, "unknown"))
                lines.append(" {index}. {column}  {dtype}".format(index=index, column=column, dtype=dtype))
            if len(columns) > 8:
                lines.append(" ... {count} more columns".format(count=len(columns) - 8))
        else:
            lines.append(" (no columns available)")
        return "\n".join(lines)

    primary_row = dataframe_rows[0]
    primary_frame_name = str(primary_row.get("name", "")).strip() or "frame"
    primary_hint = "This is the best first dataframe for the current mission."
    if primary_name and primary_frame_name == "{name}_df".format(name=primary_name):
        primary_hint = (
            "This is the primary shaped object for the mission. Start here unless you are inspecting provenance or pipeline state."
        )

    cards: list[str] = []
    for row in dataframe_rows[1:7]:
        columns = [str(column) for column in row.get("columns", []) if str(column).strip()]
        dtypes = row.get("dtypes") or {}
        dtype_preview = ", ".join(
            "{name}: {dtype}".format(name=column, dtype=dtype)
            for column, dtype in list(dtypes.items())[:4]
        )
        cards.append(
            """
<article class="df-card">
  <strong><code>{name}</code></strong>
  <div class="df-meta">
    <span class="chip">{rows} rows</span>
    <span class="chip">{column_count} cols</span>
  </div>
  <p class="df-columns">{columns}</p>
  {dtypes}
</article>
""".format(
                name=html.escape(str(row.get("name", ""))),
                rows=html.escape(str(int(row.get("size", 0) or 0))),
                column_count=html.escape(str(len(columns))),
                columns=html.escape(", ".join(columns[:8]) or "no columns"),
                dtypes=(
                    '<p class="muted">{text}</p>'.format(text=html.escape(dtype_preview))
                    if dtype_preview
                    else ""
                ),
            )
        )
    remainder = """
  <div class="dataframe-grid">{cards}</div>
""".format(cards="".join(cards)) if cards else ""
    return """
<details class="dataframe-shell stage-section" data-stage-section="Lab Notes">
  <summary>
    <div class="eyebrow">DataFrames</div>
    <h3>Open the data deck when you need it</h3>
    <p class="muted">Lead with the conversation reel. Drop into frames when you need provenance, QA state, or structured objects.</p>
  </summary>
  <section class="dataframe-inventory">
    <div class="df-callout">
      <div class="eyebrow">Recommended first dataframe</div>
      <h3><code>{primary_name}</code></h3>
      <p>{primary_hint}</p>
      <pre class="df-info-preview">{info_preview}</pre>
    </div>
    <div class="chips-wrap chips">
      <span class="chip">first prompt: use <code>{primary_name}</code></span>
      <span class="chip">{primary_rows} rows</span>
      <span class="chip">{primary_cols} cols</span>
    </div>
    <p class="muted" style="margin-top:14px;">Other loaded dataframes</p>
    {remainder}
  </section>
</details>
""".format(
        primary_name=html.escape(primary_frame_name),
        primary_hint=html.escape(primary_hint),
        info_preview=html.escape(info_preview(primary_row)),
        primary_rows=html.escape(str(int(primary_row.get("size", 0) or 0))),
        primary_cols=html.escape(str(len([str(column) for column in primary_row.get("columns", []) if str(column).strip()]))),
        remainder=remainder,
    )


class LabHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            if parsed.path == "/__reload_token":
                self._respond_text(_reload_token())
                return
            if parsed.path == "/__reload_status":
                self._respond_json(_reload_status_payload())
                return
            if parsed.path == "/web/research-desk/status":
                self._respond_json(_research_desk_status_payload(), headers=_read_only_headers(self.headers.get("Origin", "").strip()))
                return
            if parsed.path == "/web/research-desk/capsules":
                raw_limit = str((query.get("limit") or ["12"])[0]).strip()
                try:
                    limit = max(1, min(50, int(raw_limit or "12")))
                except ValueError:
                    limit = 12
                self._respond_json(
                    _research_desk_capsules_payload(limit=limit),
                    headers=_read_only_headers(self.headers.get("Origin", "").strip()),
                )
                return
            if parsed.path == "/web/research-desk/mission-status":
                capsule_name = str((query.get("capsule_name") or [""])[0]).strip()
                payload = _research_desk_mission_payload(capsule_name)
                status = 200 if payload.get("ok") else 404
                self._respond_json(
                    payload,
                    status=status,
                    headers=_read_only_headers(self.headers.get("Origin", "").strip()),
                )
                return
            if parsed.path == "/web/research-desk/handshake/status":
                request_id = str((query.get("request_id") or [""])[0]).strip()
                headers = _handshake_headers(self.headers.get("Origin", "").strip())
                self._respond_json(_handshake_status_payload(request_id), headers=headers)
                return
            if parsed.path == "/web/research-desk/handshake/approve":
                request_id = str((query.get("request_id") or [""])[0]).strip()
                self._respond_html(_handshake_approval_page(request_id))
                return
            if parsed.path == "/":
                self._respond_html(_index_html())
                return
            if parsed.path.startswith("/mission/"):
                capsule_name = unquote(parsed.path[len("/mission/"):]).strip("/")
                if capsule_name == "new":
                    self._redirect("/")
                    return
                capsule_dir = _resolve_capsule_dir(capsule_name)
                if capsule_dir.exists():
                    notice = str((query.get("notice") or [""])[0]).strip()
                    if query.get("reopened") == ["1"] and not notice:
                        notice = "Mission already existed. Reopened the current capsule instead."
                    self._respond_html(_mission_page(capsule_dir, notice=notice))
                    return
            if parsed.path.startswith("/capsule/"):
                capsule_name = unquote(parsed.path[len("/capsule/"):]).strip("/")
                capsule_dir = _resolve_capsule_dir(capsule_name)
                if capsule_dir.exists():
                    self._respond_html(_capsule_page(capsule_dir))
                    return
            self.send_error(HTTPStatus.NOT_FOUND)
        except Exception as exc:  # pragma: no cover - exercised through route behavior tests
            self._handle_route_exception(parsed, exc)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/web/research-desk/handshake/start":
                origin = self.headers.get("Origin", "").strip()
                headers = _handshake_headers(origin)
                if not origin:
                    self._respond_json({"ok": False, "error": "missing_origin"}, status=403, headers=headers)
                    return
                if origin not in _allowed_handshake_origins():
                    self._respond_json({"ok": False, "error": "origin_not_allowed"}, status=403, headers=headers)
                    return
                try:
                    form = self._read_form()
                except Exception:
                    self._respond_json({"ok": False, "error": "invalid_form"}, status=400, headers=headers)
                    return
                payload = _start_handshake_request(
                    origin,
                    client_label=str(form.get("client_label", "")).strip() or "Unchained",
                    requested_scope=str(form.get("requested_scope", "")).strip() or "mission:create",
                )
                status = 200 if payload.get("ok") else 429 if payload.get("error") == "too_many_pending_requests" else 400
                self._respond_json(payload, status=status, headers=headers)
                return
            if parsed.path == "/web/research-desk/handshake/approve":
                try:
                    form = self._read_form()
                except Exception:
                    self._respond_html(_html_page("Research Desk Connect", "<section class=\"panel\"><h1>Approval failed</h1><p>The approval form could not be read.</p><p><a href=\"/\">Back to Research Desk</a></p></section>"), status=400)
                    return
                request_id = str(form.get("request_id", "")).strip()
                csrf_token = str(form.get("csrf_token", "")).strip()
                decision = str(form.get("decision", "")).strip().lower()
                valid_approval, approval_payload = _validate_handshake_approval(
                    request_id,
                    csrf_token,
                    origin=self.headers.get("Origin", "").strip(),
                    referer=self.headers.get("Referer", "").strip(),
                )
                if not valid_approval:
                    self._respond_html(
                        _html_page(
                            "Research Desk Connect",
                            "<section class=\"panel\"><h1>Approval blocked</h1><p>This approval request was rejected because it did not come from the local Research Desk page.</p><p><a href=\"/\">Back to Research Desk</a></p></section>",
                        ),
                        status=403,
                    )
                    return
                payload = _decide_handshake_request(request_id, allow=decision == "allow")
                if not payload.get("ok"):
                    self._respond_html(
                        _html_page(
                            "Research Desk Connect",
                            "<section class=\"panel\"><h1>Approval request unavailable</h1><p>This connection request expired or is no longer valid.</p><p><a href=\"/\">Back to Research Desk</a></p></section>",
                        ),
                        status=404,
                    )
                    return
                self._redirect(
                    "/web/research-desk/handshake/approve?request_id={request_id}".format(request_id=quote(request_id))
                )
                return
            if parsed.path == "/web/research-desk/actions/mission-create":
                origin = self.headers.get("Origin", "").strip()
                headers = _handshake_headers(origin, include_auth_headers=True)
                # Action endpoints allow missing Origin for trusted same-host callers; token validation is still required.
                if origin and origin not in _allowed_handshake_origins():
                    self._respond_json({"ok": False, "error": "origin_not_allowed"}, status=403, headers=headers)
                    return
                token = _extract_bearer_token(self.headers)
                allowed, token_payload = _validate_handshake_token(token, required_scope="mission:create")
                if not allowed:
                    self._respond_json(token_payload, status=403, headers=headers)
                    return
                try:
                    form = self._read_form()
                except Exception:
                    self._respond_json({"ok": False, "error": "invalid_form"}, status=400, headers=headers)
                    return
                created = _create_mission_from_prompt(
                    str(form.get("mission_prompt", "")).strip(),
                    requested_name=str(form.get("capsule_name", "")).strip(),
                    source_route=str(form.get("source_route", "")).strip(),
                    source_session_id=str(form.get("source_session_id", "")).strip(),
                )
                status = 200 if created.get("ok") else 400
                self._respond_json(created, status=status, headers=headers)
                return
            if parsed.path == "/web/research-desk/actions/mission-advance":
                origin = self.headers.get("Origin", "").strip()
                headers = _handshake_headers(origin, include_auth_headers=True)
                # Action endpoints allow missing Origin for trusted same-host callers; token validation is still required.
                if origin and origin not in _allowed_handshake_origins():
                    self._respond_json({"ok": False, "error": "origin_not_allowed"}, status=403, headers=headers)
                    return
                token = _extract_bearer_token(self.headers)
                allowed, token_payload = _validate_handshake_token(token, required_scope="mission:advance")
                if not allowed:
                    self._respond_json(token_payload, status=403, headers=headers)
                    return
                try:
                    form = self._read_form()
                except Exception:
                    self._respond_json({"ok": False, "error": "invalid_form"}, status=400, headers=headers)
                    return
                try:
                    payload = _advance_mission_from_hosted(str(form.get("capsule_name", "")).strip())
                except Exception as exc:
                    self._respond_json(
                        {
                            "ok": False,
                            "error": "advance_failed",
                            "message": str(exc) or exc.__class__.__name__,
                        },
                        status=500,
                        headers=headers,
                    )
                    return
                status = 200 if payload.get("ok") else 400
                if str(payload.get("error", "")).strip() == "advance_busy":
                    status = 429
                self._respond_json(payload, status=status, headers=headers)
                return
            if parsed.path == "/__reload/pause":
                self._respond_json(
                    {
                        "ok": True,
                        "paused": bool(set_reload_paused(True).get("paused", True)),
                        "token": _reload_token(),
                    }
                )
                return
            if parsed.path == "/__reload/resume":
                self._respond_json(
                    {
                        "ok": True,
                        "paused": bool(set_reload_paused(False).get("paused", False)),
                        "token": _reload_token(),
                    }
                )
                return
            if parsed.path == "/mission/create":
                from .cli import ensure_capsule, slugify, update_manifest

                form = self._read_form()
                prompt = str(form.get("mission_prompt", "")).strip()
                if not prompt:
                    self._redirect("/")
                    return
                requested_name = str(form.get("capsule_name", "")).strip() or prompt
                existing_capsule_name = slugify(requested_name)
                try:
                    capsule_dir, manifest = ensure_capsule(requested_name, append=False)
                except SystemExit:
                    self._redirect("/mission/{name}?reopened=1".format(name=quote(existing_capsule_name)))
                    return
                manifest["name"] = capsule_dir.name
                manifest["task"] = prompt
                update_manifest(capsule_dir, manifest)
                _save_mission(capsule_dir, form)
                self._redirect("/mission/{name}".format(name=quote(capsule_dir.name)))
                return
            if parsed.path.startswith("/mission/") and (parsed.path.endswith("/scout") or parsed.path.endswith("/gather")):
                from .cli import NoveltyStepTimeout, _run_with_timeout, gather_selected_sources, read_json

                parts = [part for part in parsed.path.split("/") if part]
                if len(parts) != 3:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                capsule_name = unquote(parts[1])
                capsule_dir = _resolve_capsule_dir(capsule_name)
                if not capsule_dir.exists():
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                blocked_notice = _mission_action_block_notice(capsule_dir)
                if blocked_notice:
                    self._redirect(
                        "/mission/{name}?notice={message}".format(
                            name=quote(capsule_name),
                            message=quote(blocked_notice),
                        )
                    )
                    return
                form = self._read_form()
                source_plan = read_json(capsule_dir / "source_plan.json", {})
                source_ids = _gather_source_ids_from_form(form, source_plan)
                if not source_ids:
                    self._redirect(
                        "/mission/{name}?notice={message}".format(
                            name=quote(capsule_name),
                            message=quote("No planned routes selected for Gather."),
                        )
                    )
                    return
                try:
                    result = _run_with_timeout(180, gather_selected_sources, capsule_dir, source_ids=source_ids)
                    notice = "Scout captured {count} discovery page(s) from {routes} selected route(s).".format(
                        count=int(result.get("captured_count", 0)),
                        routes=len(source_ids),
                    )
                except NoveltyStepTimeout:
                    notice = "Scout timed out before it could finish. Try again or tighten the Mission so the route set is smaller."
                except SystemExit as exc:
                    notice = str(exc) or "Scout failed."
                self._redirect(
                    "/mission/{name}?notice={message}".format(
                        name=quote(capsule_name),
                        message=quote(notice),
                    )
                )
                return
            if parsed.path.startswith("/mission/") and parsed.path.endswith("/apply-plan-suggestion"):
                parts = [part for part in parsed.path.split("/") if part]
                if len(parts) != 3:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                capsule_name = unquote(parts[1])
                capsule_dir = _resolve_capsule_dir(capsule_name)
                if not capsule_dir.exists():
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                form = self._read_form()
                suggestion_id = str(form.get("suggestion_id", "")).strip()
                if not suggestion_id:
                    self._redirect(
                        "/mission/{name}?notice={message}".format(
                            name=quote(capsule_name),
                            message=quote("Planner suggestion was missing."),
                        )
                    )
                    return
                notice = _apply_plan_suggestion(capsule_dir, suggestion_id)
                self._redirect(
                    "/mission/{name}?notice={message}".format(
                        name=quote(capsule_name),
                        message=quote(notice),
                    )
                )
                return
            if parsed.path.startswith("/mission/") and parsed.path.endswith("/gather-targets"):
                from .cli import NoveltyStepTimeout, _run_with_timeout, gather_selected_targets, read_json

                parts = [part for part in parsed.path.split("/") if part]
                if len(parts) != 3:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                capsule_name = unquote(parts[1])
                capsule_dir = _resolve_capsule_dir(capsule_name)
                if not capsule_dir.exists():
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                blocked_notice = _mission_action_block_notice(capsule_dir)
                if blocked_notice:
                    self._redirect(
                        "/mission/{name}?notice={message}".format(
                            name=quote(capsule_name),
                            message=quote(blocked_notice),
                        )
                    )
                    return
                form = self._read_form()
                gather_targets = read_json(capsule_dir / "gather_targets.json", {})
                target_ids = _gather_target_ids_from_form(form, gather_targets)
                if not target_ids:
                    self._redirect(
                        "/mission/{name}?notice={message}".format(
                            name=quote(capsule_name),
                            message=quote("No scout candidates selected for Gather."),
                        )
                    )
                    return
                try:
                    result = _run_with_timeout(180, gather_selected_targets, capsule_dir, target_ids=target_ids)
                    snapshot = _mission_snapshot(capsule_dir)
                    primary_object = _primary_object(snapshot)
                    primary_row_count = int(primary_object.get("row_count", 0) or 0)
                    recovery_suffix = ""
                    if bool(result.get("auto_recovery_wave_run")):
                        recovery_count = int(result.get("auto_recovery_wave_count", 0) or 0)
                        recovery_target_count = len(list(result.get("auto_recovery_target_ids", []) or []))
                        recovery_suffix = " The system also ran {wave_count} recovery wave(s) on {count} stronger follow-up target(s).".format(
                            wave_count=recovery_count,
                            count=recovery_target_count,
                        )
                    row_progress_suffix = ""
                    row_target = int(result.get("row_target", 0) or 0)
                    row_progress_start = int(result.get("row_progress_start", primary_row_count) or 0)
                    row_progress_end = int(result.get("row_progress_end", primary_row_count) or 0)
                    if row_target > 0:
                        row_progress_suffix = " Rows: {start} -> {end} / {target}.".format(
                            start=row_progress_start,
                            end=row_progress_end,
                            target=row_target,
                        )
                    if primary_row_count <= 0:
                        target_name = str(_first_target(snapshot.get("task_spec", {}) or {}).get("name", "object")).strip() or "object"
                        notice = "Gather finished, but Shape could not build `{name}` yet.{rows}{suffix}".format(
                            name=target_name,
                            rows=row_progress_suffix,
                            suffix=recovery_suffix,
                        )
                    else:
                        notice = "Gather captured {count} candidate page(s).{rows}{suffix}".format(
                            count=int(result.get("captured_count", 0)),
                            rows=row_progress_suffix,
                            suffix=recovery_suffix,
                        )
                except NoveltyStepTimeout:
                    notice = "Gather timed out before it could finish. Try again or narrow the candidate set."
                except SystemExit as exc:
                    notice = str(exc) or "Gather failed."
                self._redirect(
                    "/mission/{name}?notice={message}".format(
                        name=quote(capsule_name),
                        message=quote(notice),
                    )
                )
                return
            if parsed.path.startswith("/mission/"):
                capsule_name = unquote(parsed.path[len("/mission/"):]).strip("/")
                capsule_dir = _resolve_capsule_dir(capsule_name)
                if not capsule_dir.exists():
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                _save_mission(capsule_dir, self._read_form())
                self._redirect("/mission/{name}".format(name=quote(capsule_name)))
                return
            if not parsed.path.startswith("/capsule/"):
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) < 3:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            capsule_name = unquote(parts[1])
            action = parts[2]
            capsule_dir = _resolve_capsule_dir(capsule_name)
            if not capsule_dir.exists():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            session = get_session(capsule_dir)
            form = self._read_form()
            content = form.get("content", "").strip()

            if action == "markdown":
                if content:
                    session.append_markdown(content, role="user")
                self._finish_action(capsule_dir, capsule_name, message="Markdown turn added.")
                return
            if action == "ask":
                if content:
                    session.append_query(content, role="user")
                    try:
                        generated = generate_code_turn(session, content)
                        code = str(generated.get("code", "")).rstrip()
                        if not code:
                            raise LabAgentError("agent returned no code")
                        execution = session.execute_code(
                            code,
                            role="agent",
                            metadata={
                                "title": str(generated.get("title", "")),
                                "intent": str(generated.get("intent", "")),
                                "notes": str(generated.get("notes", "")),
                            },
                        )
                        summarized = summarize_turn(
                            session,
                            query=content,
                            generated=generated,
                            execution=execution,
                        )
                        markdown = str(summarized.get("markdown", "")).strip()
                        if markdown:
                            session.append_markdown(markdown, role="agent")
                    except LabAgentError as exc:
                        session.append_markdown(
                            "## Agent Error\n\n{message}".format(message=str(exc)),
                            role="agent",
                        )
                    except Exception as exc:
                        session.append_markdown(
                            "## System Error\n\nThe notebook turn failed unexpectedly.\n\n```text\n{message}\n```".format(
                                message=str(exc)
                            ),
                            role="agent",
                        )
                self._finish_action(capsule_dir, capsule_name, message="Ask turn completed.")
                return
            if action == "code":
                if content:
                    session.execute_code(content)
                self._finish_action(capsule_dir, capsule_name, message="Code cell executed.")
                return
            if action == "wait":
                wait_result = session.wait_for_pending()
                if wait_result.get("appended"):
                    turns = session.read_turns()
                    query_turn, code_turn = _latest_agent_query_and_code(turns)
                    if query_turn and code_turn:
                        summarized = summarize_turn(
                            session,
                            query=str(query_turn.get("content", "")),
                            generated={
                                "title": str(code_turn.get("title", "")) or "Result",
                                "intent": str(code_turn.get("intent", "")),
                                "code": str(code_turn.get("content", "")),
                            },
                            execution=wait_result,
                        )
                        markdown = str(summarized.get("markdown", "")).strip()
                        if markdown:
                            session.append_markdown(markdown, role="agent")
                message_map = {
                    "ok": "Kernel output received.",
                    "error": "Kernel command failed.",
                    "running": "pyreplab is still running.",
                    "idle": "No pending pyreplab command.",
                }
                self._finish_action(
                    capsule_dir,
                    capsule_name,
                    message=message_map.get(str(wait_result.get("status", "")), "Wait check completed."),
                )
                return
            if action == "reset":
                session.reset()
                self._finish_action(capsule_dir, capsule_name, message="Session reset.")
                return
            self.send_error(HTTPStatus.NOT_FOUND)
        except Exception as exc:  # pragma: no cover - exercised through route behavior tests
            self._handle_route_exception(parsed, exc)

    def do_OPTIONS(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path.startswith("/web/research-desk/"):
            self.send_response(HTTPStatus.NO_CONTENT)
            headers = (
                _handshake_headers(
                    self.headers.get("Origin", "").strip(),
                    include_auth_headers=parsed.path.startswith("/web/research-desk/actions/"),
                )
                if parsed.path.startswith("/web/research-desk/handshake/") or parsed.path.startswith("/web/research-desk/actions/")
                else _read_only_headers(self.headers.get("Origin", "").strip())
            )
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return

    def _read_form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8", "replace")
        parsed = parse_qs(raw, keep_blank_values=True)
        return {key: values[0] if values else "" for key, values in parsed.items()}

    def _respond_html(self, body: str, status: int = 200) -> None:
        payload = body.encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return

    def _respond_json(self, payload: dict[str, Any], status: int = 200, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return

    def _respond_text(self, body: str, status: int = 200) -> None:
        payload = body.encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return

    def _wants_json(self) -> bool:
        accept = self.headers.get("Accept", "")
        requested_with = self.headers.get("X-Requested-With", "")
        return "application/json" in accept or requested_with.lower() == "fetch"

    def _handle_route_exception(self, parsed, exc: Exception) -> None:
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            return
        traceback.print_exc()
        message = "{kind} failed unexpectedly: {detail}".format(
            kind=self._route_label(parsed.path),
            detail=str(exc) or exc.__class__.__name__,
        )
        if parsed.path.startswith("/mission/"):
            capsule_name = self._capsule_name_from_path(parsed.path, prefix="/mission/")
            if capsule_name:
                self._redirect(
                    "/mission/{name}?notice={message}".format(
                        name=quote(capsule_name),
                        message=quote(message),
                    )
                )
                return
        if parsed.path.startswith("/capsule/"):
            capsule_name = self._capsule_name_from_path(parsed.path, prefix="/capsule/")
            if capsule_name:
                try:
                    capsule_dir = _resolve_capsule_dir(capsule_name)
                except ValueError:
                    capsule_dir = None
                if capsule_dir and capsule_dir.exists():
                    session = get_session(capsule_dir)
                    session.append_markdown("## System Error\n\n{message}".format(message=message), role="agent")
                    if self._wants_json():
                        snapshot = _capsule_snapshot(capsule_dir, refresh=False)
                        self._respond_json(
                            {
                                "message": message,
                                "turns_html": _turns_html(snapshot["turns"]),
                                "status_html": _status_panel_html(capsule_dir, snapshot),
                            },
                            status=500,
                        )
                    else:
                        self._redirect("/capsule/{name}".format(name=quote(capsule_name)))
                    return
        self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, explain=message)

    def _capsule_name_from_path(self, path: str, *, prefix: str) -> str:
        remainder = unquote(path[len(prefix):]).strip("/")
        if not remainder:
            return ""
        return remainder.split("/", 1)[0].strip()

    def _route_label(self, path: str) -> str:
        if path.endswith("/gather-targets"):
            return "Gather"
        if path.endswith("/scout") or path.endswith("/gather"):
            return "Scout"
        if path.endswith("/ask"):
            return "Ask"
        if path.endswith("/code"):
            return "Run Code"
        if path.endswith("/wait"):
            return "Wait"
        if path.endswith("/reset"):
            return "Reset"
        if path == "/mission/create" or path.startswith("/mission/"):
            return "Mission"
        return "Request"

    def _finish_action(self, capsule_dir: Path, capsule_name: str, *, message: str) -> None:
        if self._wants_json():
            snapshot = _capsule_snapshot(capsule_dir, refresh=False)
            self._respond_json(
                {
                    "message": message,
                    "turns_html": _turns_html(snapshot["turns"]),
                    "status_html": _status_panel_html(capsule_dir, snapshot),
                }
            )
            return
        self._redirect("/capsule/{name}".format(name=quote(capsule_name)))

    def _redirect(self, location: str) -> None:
        try:
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", location)
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return


def run_server(host: str = "127.0.0.1", port: int = 8766) -> None:
    global _SERVER_HOST, _SERVER_PORT, _HANDSHAKE_CLEANER_STARTED
    _SERVER_HOST = host
    _SERVER_PORT = port
    _refresh_allowed_handshake_origins()
    with _HANDSHAKE_CLEANER_LOCK:
        if not _HANDSHAKE_CLEANER_STARTED:
            cleaner = threading.Thread(target=_handshake_cleaner_loop, name="research-desk-handshake-cleaner", daemon=True)
            cleaner.start()
            _HANDSHAKE_CLEANER_STARTED = True
    httpd = ThreadingHTTPServer((host, port), LabHandler)
    print("local_lab=http://{host}:{port}".format(host=host, port=port))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
