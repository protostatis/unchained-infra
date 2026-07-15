"""Focused tests for inline template transform helpers."""

from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path

from web_app.template_transforms import (
    TemplateReplacement,
    TemplateTransformError,
    apply_template_replacements,
)


class TestTemplateTransforms(unittest.TestCase):
    def test_apply_template_replacements_replaces_expected_marker(self):
        html = "<head></head>"
        rendered = apply_template_replacements(
            html,
            (
                TemplateReplacement(
                    "</head>",
                    "<style id='x'></style></head>",
                    "style injection",
                ),
            ),
            template_name="sample template",
        )
        self.assertEqual(rendered, "<head><style id='x'></style></head>")

    def test_apply_template_replacements_raises_on_marker_drift(self):
        with self.assertRaises(TemplateTransformError) as ctx:
            apply_template_replacements(
                "<head></head>",
                (
                    TemplateReplacement(
                        "<body>",
                        "<body class='x'>",
                        "missing body marker",
                    ),
                ),
                template_name="sample template",
            )
        self.assertIn("missing body marker", str(ctx.exception))
        self.assertIn("sample template", str(ctx.exception))

    def test_chat_templates_import_with_sidebar_and_theme_applied(self):
        from web_app import templates

        self.assertIn('id="modern-chat-theme"', templates.CHAT_CODEX_HTML)
        self.assertIn('id="playful-chat-skin"', templates.TRIAL_CHAT_HTML)
        self.assertIn('id="playful-chat-skin"', templates.CLAUDE_CHAT_HTML)
        self.assertIn("Chat 1", templates.TRIAL_CHAT_HTML)
        self.assertIn("Lane A", templates.CLAUDE_CHAT_HTML)
        self.assertIn('id="dev-login-btn"', templates.TRIAL_CHAT_HTML)
        self.assertIn("maybeShowDevLogin", templates.TRIAL_CHAT_HTML)
        self.assertIn('class="topbar-new"', templates.TRIAL_CHAT_HTML)
        self.assertIn('class="topbar-new"', templates.CLAUDE_CHAT_HTML)
        self.assertIn("text-decoration:none!important", templates.TRIAL_CHAT_HTML)
        self.assertIn('id="sidebar"', templates.CHAT_CODEX_HTML)
        self.assertIn("codex-sdk:codex-mini-latest", templates.CHAT_CODEX_HTML)
        self.assertIn("claude-sdk:claude-sonnet-4-6", templates.CHAT_CLAUDE_SDK_HTML)

    def test_agent_view_chat_state_and_response_reveal_across_lanes(self):
        from web_app import templates

        lane_templates = {
            "Claude CLI": templates.CLAUDE_CHAT_HTML,
            "Claude SDK": templates.CHAT_CLAUDE_SDK_HTML,
            "Codex": templates.CHAT_CODEX_HTML,
        }
        for lane, html in lane_templates.items():
            with self.subTest(lane=lane):
                self.assertEqual(html.count('id="agent-view"'), 1)
                self.assertEqual(html.count('id="agent-view-chat-controls"'), 1)
                self.assertEqual(html.count('id="chat-card-expand"'), 1)
                self.assertEqual(html.count('id="chat-card-exit"'), 1)
                self.assertEqual(html.count('id="chat-card-minimize"'), 1)
                self.assertEqual(html.count('id="agent-view-chat-restore"'), 1)
                self.assertEqual(html.count('id="msginput"'), 1)
                self.assertEqual(html.count("appendText(bubble, evt.data)"), 1)
                self.assertIn("function setAgentViewChatState(mode, surface)", html)
                self.assertIn("function maybeRevealAgentResponse()", html)
                self.assertIn("beginAgentViewResponseTurn()", html)
                self.assertIn(
                    "if (agentViewChatMode === 'fullscreen') { exitAgentViewFullscreen(); return; }",
                    html,
                )
                self.assertIn("setAgentViewChatState('docked', 'chat');", html)
                self.assertNotIn('id="topbar-chat-size"', html)
                self.assertNotIn('id="av-chat-size"', html)
                self.assertNotIn('id="chat-card-size"', html)
                self.assertNotIn("_chatSizeCycle", html)
                self.assertNotIn("av-fullscreen-minimize", html)

    def test_agent_view_controls_are_buttons_not_filtered_nav_links(self):
        from web_app import templates

        html = templates.CHAT_CLAUDE_SDK_HTML
        controls = html.split('id="agent-view-chat-controls"', 1)[1].split(
            '<a href="#" onclick="doDisconnect();return false">', 1
        )[0]
        self.assertIn('<button type="button" id="chat-card-expand"', controls)
        self.assertIn('<button type="button" id="chat-card-exit"', controls)
        self.assertIn('<button type="button" id="chat-card-minimize"', controls)
        self.assertNotIn("<a ", controls)
        self.assertEqual(controls.count('<svg viewBox="0 0 16 16"'), 4)
        self.assertIn('id="chat-card-history"', controls)
        self.assertIn('title="Chat history"', controls)
        self.assertIn('title="Expand chat"', controls)
        self.assertIn('title="Default chat size"', controls)
        self.assertIn('title="Minimize chat"', controls)
        self.assertIn("body.agent-view-open #agent-view-chat-controls{display:inline-flex}", html)
        self.assertIn("body.agent-view-open.agent-view-chat-expanded #chat-card-exit{display:inline-flex}", html)
        self.assertIn(
            "body.agent-view-open #main #chat .bubble{min-width:0;max-width:100%;flex-shrink:0}",
            html,
        )

    def test_mobile_chat_controls_are_right_aligned_and_responsive(self):
        from web_app import templates

        html = templates.CHAT_CLAUDE_SDK_HTML
        local_html = templates.CLAUDE_CHAT_HTML
        self.assertIn(
            'role="group" aria-label="Same conversation controlling browser preview"',
            html,
        )
        self.assertIn(
            "body.agent-view-open #main #topbar .nav{margin-left:auto!important;width:100%!important;max-width:none!important;gap:6px!important;flex-wrap:nowrap!important;justify-content:flex-end!important;overflow:visible!important}",
            html,
        )
        self.assertIn(".chat-size-btn{width:44px;min-width:44px;height:44px}", html)
        self.assertIn(".agent-view-foot,.agent-view-chat-context{display:none}", html)
        self.assertIn(
            "body.agent-view-open.chat-minimized .agent-view-chat-restore{display:none!important}",
            html,
        )
        self.assertIn(
            "body.agent-view-open.agent-view-chat-expanded #main #inputbar{margin-bottom:max(8px,env(safe-area-inset-bottom))!important}",
            html,
        )
        self.assertIn(
            "height:min(62dvh,560px,calc(100dvh - var(--agent-view-mobile-chat-top,0px) - 10px))!important",
            html,
        )
        self.assertIn("function positionAgentViewMobileChat(renderedHeight)", html)
        self.assertIn("agent-view-browser-positioned", html)
        self.assertIn(
            "@media(min-width:761px) and (max-width:900px) and (max-height:500px)",
            html,
        )
        self.assertIn(
            "grid-template-columns:max-content minmax(0,1fr)!important",
            local_html,
        )
        self.assertIn('placeholder="Ask anything..."', local_html)
        self.assertNotIn('placeholder="Ask the agent anything..."', local_html)

    def test_agent_view_uses_browser_preview_copy_without_exposing_diagnostics(self):
        from web_app import templates

        lane_templates = {
            "Claude CLI": templates.CLAUDE_CHAT_HTML,
            "Claude SDK": templates.CHAT_CLAUDE_SDK_HTML,
            "Codex": templates.CHAT_CODEX_HTML,
        }
        for lane, html in lane_templates.items():
            with self.subTest(lane=lane):
                self.assertIn(">Browser Preview</a>", html)
                self.assertIn("<strong>Browser Preview</strong>", html)
                self.assertIn("<strong>Same conversation</strong>", html)
                self.assertIn("Controls this preview", html)
                self.assertIn("Current page", html)
                self.assertNotIn("Shared semantic browser", html)
                self.assertNotIn("semantic://", html)
                self.assertIn(".agent-view-foot{display:none", html)
                self.assertIn('id="agent-view-fidelity"', html)
                self.assertIn("fidelityEl.textContent", html)
                self.assertIn("setAgentViewState", html)

    def test_closed_mobile_sidebar_is_removed_from_focus_order(self):
        from web_app import templates

        runtime = templates._SIDEBAR_JS
        self.assertIn("function syncSidebarInteractivity()", runtime)
        self.assertIn("sidebar.toggleAttribute('inert', hidden);", runtime)
        self.assertIn("sidebar.setAttribute('aria-hidden', String(hidden));", runtime)
        self.assertIn("window.addEventListener('resize', syncSidebarInteractivity);", runtime)

    def test_agent_view_mobile_response_reveal_is_once_per_turn(self):
        from web_app import templates

        runtime = templates._AGENT_VIEW_JS
        self.assertIn("agentViewResponseRevealPending = true", runtime)
        self.assertIn("agentViewResponseRevealDone = false", runtime)
        self.assertIn(
            "if (!agentViewResponseRevealPending || agentViewResponseRevealDone) return;",
            runtime,
        )
        self.assertIn("if (!_agentViewIsMobile()", runtime)
        self.assertIn("if (agentViewChatMode === 'minimized') {", runtime)
        self.assertIn("agentViewResponseRevealDone = true;\n    return;", runtime)
        self.assertIn("setAgentViewChatState('docked', 'chat');", runtime)
        self.assertIn(
            "} else if (evt.type === 'done') {\n            completeAgentShellTurn();\n            maybeRevealAgentResponse();",
            templates.CHAT_CODEX_HTML,
        )

    def test_agent_task_shell_is_default_with_legacy_escape_and_adaptive(self):
        from web_app import templates

        html = templates.CLAUDE_CHAT_HTML
        runtime = templates._AGENT_VIEW_JS
        self.assertIn("const AGENT_SHELL_DEFAULT = 'task';", runtime)
        self.assertIn("value === 'task' || value === 'legacy'", runtime)
        self.assertIn("function setAgentShellConnectionLayout(browserAvailable)", runtime)
        self.assertIn("agent-shell-chat-only", runtime)
        self.assertIn("agentShellBrowserUsedThisTurn", runtime)
        self.assertIn("agent-shell-text-turn", runtime)
        self.assertNotIn('id="agent-shell-modes"', html)
        self.assertNotIn('data-shell-view=', html)
        self.assertIn('id="chat-card-history"', html)
        self.assertIn('aria-controls="sidebar"', html)
        self.assertIn('id="agent-shell-history-scrim"', html)
        self.assertIn("function toggleAgentShellHistory(forceOpen)", html)
        self.assertIn('id="lane-picker-toggle"', html)
        self.assertIn('id="lane-picker-current"', html)
        self.assertIn('<span class="lane-picker-kicker">Thread</span>', html)
        self.assertIn("Select chat thread. Current:", runtime)
        self.assertIn("visible.textContent = threadName", runtime)
        self.assertIn("function toggleAgentLanePicker(forceOpen)", runtime)
        self.assertIn("function arrangeAgentChatToolbar()", runtime)
        self.assertIn("group.setAttribute('aria-label', 'Chat navigation')", runtime)
        self.assertIn("group.appendChild(picker);", runtime)
        self.assertIn("group.appendChild(newChat);", runtime)
        self.assertIn("group.appendChild(history);", runtime)
        self.assertIn("#agent-chat-primary-tools{gap:8px}", html)
        self.assertIn("#slotbar:hover>button:not(#lane-picker-toggle)", html)
        self.assertIn("width:36px!important", html)
        self.assertIn("#slot2{left:40px!important}", html)
        self.assertIn('class="agent-shell-trace"', html)
        self.assertIn("body.agent-shell-task.agent-view-open #agent-view", html)
        self.assertIn("body.agent-shell-task.agent-view-open.chat-minimized #agent-view{right:0}", html)
        self.assertIn("translateX(28px) scale(.965)", html)
        self.assertIn("transition:right 240ms", html)
        self.assertIn("width:calc(100vw - 24px)!important", html)
        self.assertIn("function positionAgentShellHistory()", html)
        self.assertIn("body.agent-view-open.agent-shell-history-open #sidebar", html)
        self.assertIn("!document.body.classList.contains('agent-view-open')", html)
        self.assertIn('id="sidebar-history-search" type="search"', html)
        self.assertIn('aria-label="Search chat history"', html)
        self.assertIn("Loading chats...", html)
        self.assertIn("No matching chats", html)
        self.assertIn("translateY(-8px) scale(.98)", html)
        self.assertIn("body.agent-shell-task.agent-view-open #main #slotbar{display:flex!important;", html)
        self.assertIn('id="agent-view" aria-label="Browser Preview" aria-hidden="true" tabindex="-1"', html)
        self.assertIn("confirmation.classList.contains('open')", runtime)
        self.assertIn("agentViewReturnFocus", runtime)
        self.assertIn("typeof globalThis._setActiveSlotSession === 'function'", runtime)
        self.assertIn("if (agentShellTaskEnabled && mobile)", runtime)
        self.assertIn("body.agent-shell-task .agent-view-chat-toggle{display:inline-flex!important}", html)
        self.assertIn("agent-view-browser-positioned #app-shell #main", html)
        self.assertIn("#main #topbar{z-index:20;overflow:visible!important}", html)
        self.assertIn("agent-view-chat-expanded #main #modelrow", html)
        self.assertIn("display:grid!important;grid-template-columns:max-content minmax(0,1fr)!important", html)
        self.assertIn("const mobileExpanded = open && _agentViewIsMobile()", runtime)
        self.assertIn("body.agent-shell-task.agent-view-open #chat-card-minimize{display:inline-flex!important}", html)
        self.assertIn("grid-template-columns:minmax(0,1fr) auto!important", html)
        self.assertIn("#main #topbar .nav{width:auto!important;min-width:94px!important", html)
        self.assertIn("completeAgentShellTurn('cancelled');", html)
        self.assertIn("completeAgentShellTurn('error');", html)
        self.assertIn("} finally {\n    completeAgentShellTurn('error');", html)
        self.assertIn("Task ended with an error", runtime)
        self.assertIn("@media(min-width:761px) and (hover:none)", html)
        self.assertIn("transition:none!important", html)

    def test_default_task_shell_arranges_thread_toolbar_and_keeps_legacy_override(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is required for Agent Task Shell runtime checks")

        from web_app import templates

        runtime = templates._AGENT_VIEW_JS

        def function_source(name, next_name):
            start = runtime.index(f"function {name}(")
            end = runtime.index(f"\nfunction {next_name}(", start)
            return runtime[start:end]

        mode_source = function_source("agentShellModeFromLocation", "setAgentShellConnectionLayout")
        arrange_source = function_source("arrangeAgentChatToolbar", "syncAgentLanePicker")
        initialize_source = function_source("initializeAgentShellExperiment", "maybeInitializeAgentShell")
        harness = r"""
class FakeClassList {
  constructor() { this.values = new Set(); }
  contains(value) { return this.values.has(value); }
  toggle(value, force) {
    const enabled = typeof force === 'boolean' ? force : !this.values.has(value);
    if (enabled) this.values.add(value); else this.values.delete(value);
    return enabled;
  }
}
class FakeElement {
  constructor(id) { this.id = id; this.children = []; this.parentElement = null; this.attrs = {}; }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  appendChild(child) {
    if (child.parentElement) child.parentElement.children = child.parentElement.children.filter(item => item !== child);
    child.parentElement = this;
    this.children.push(child);
    return child;
  }
  insertBefore(child, before) {
    if (child.parentElement) child.parentElement.children = child.parentElement.children.filter(item => item !== child);
    child.parentElement = this;
    const index = this.children.indexOf(before);
    this.children.splice(index < 0 ? this.children.length : index, 0, child);
    return child;
  }
  querySelector(selector) { return selector === '.nav' ? nav : null; }
}
const body = {classList: new FakeClassList()};
const topbar = new FakeElement('topbar');
const nav = new FakeElement('nav');
const picker = new FakeElement('slotbar');
const newChat = new FakeElement('new-chat');
const history = new FakeElement('chat-card-history');
topbar.appendChild(nav);
const elements = new Map([
  ['slotbar', picker],
  ['chat-card-history', history],
]);
globalThis.document = {
  body,
  querySelector(selector) {
    if (selector === '#main #topbar') return topbar;
    if (selector === '#main .topbar-new') return newChat;
    return null;
  },
  createElement() { return new FakeElement(''); },
  getElementById(id) { return elements.get(id) || null; },
};
globalThis.location = {search: ''};
const AGENT_SHELL_DEFAULT = 'task';
let agentShellTaskEnabled = false;
function setAgentShellPhase() {}
"""
        checks = r"""
function expect(condition, message) { if (!condition) throw new Error(message); }
expect(agentShellModeFromLocation() === 'task', 'task shell is not the default');
location.search = '?provider=opencode-cli&shell=legacy';
expect(agentShellModeFromLocation() === 'legacy', 'explicit legacy override was ignored');
initializeAgentShellExperiment();
expect(!agentShellTaskEnabled, 'legacy override enabled the task shell');
expect(!body.classList.contains('agent-shell-task'), 'legacy override kept the task shell body class');
expect(!topbar.children.some(child => child.id === 'agent-chat-primary-tools'), 'legacy override created the task toolbar');
location.search = '?provider=opencode-cli';
initializeAgentShellExperiment();
expect(agentShellTaskEnabled, 'default route did not enable task shell');
expect(body.classList.contains('agent-shell-task'), 'task shell body class is missing');
const group = topbar.children.find(child => child.id === 'agent-chat-primary-tools');
expect(!!group, 'chat navigation toolbar was not created');
expect(group.children[0] === picker, 'thread picker is not first in the toolbar');
expect(group.children[1] === newChat, 'New Chat is not second in the toolbar');
expect(group.children[2] === history, 'History is not third in the toolbar');
"""
        result = subprocess.run(
            [node],
            input=harness + mode_source + arrange_source + initialize_source + checks,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_agent_view_session_hook_is_optional_for_codex_template(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is required for Agent View runtime checks")

        from web_app import templates

        runtime = templates._AGENT_VIEW_JS
        start = runtime.index("const _agentViewSetActiveSlotSession")
        end = runtime.index("\ndocument.addEventListener('click'", start)
        hook_source = runtime[start:end]
        harness = r"""
let agentViewBoundSessionId = '';
globalThis.document = {body: {classList: {contains() { return false; }}}};
globalThis.setTimeout = function(callback) { callback(); };
function syncAgentLanePicker() {}
function refreshAgentView() {}
"""
        checks = r"""
if (typeof globalThis._setActiveSlotSession !== 'function') {
  throw new Error('optional session hook was not installed');
}
globalThis._setActiveSlotSession('s-codex-test');
"""
        result = subprocess.run(
            [node],
            input=harness + hook_source + checks,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_trial_thread_picker_preserves_rich_slot_labels(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is required for Thread picker runtime checks")

        from web_app import templates

        runtime = templates._AGENT_VIEW_JS
        start = runtime.index("function syncAgentLanePicker()")
        end = runtime.index("\nfunction toggleAgentLanePicker", start)
        sync_source = runtime[start:end]
        harness = r"""
class FakeClassList {
  constructor(active) { this.active = active; }
  contains(value) { return value === 'active' && this.active; }
}
class FakeChild {
  constructor(className, text) { this.className = className; this.textContent = text; this.attrs = {}; }
  setAttribute(name, value) { this.attrs[name] = String(value); }
}
class FakeButton {
  constructor(id, active) {
    this.id = id;
    this.classList = new FakeClassList(active);
    this.dataset = {};
    this.attrs = {};
    this.children = [new FakeChild('slot-name', 'Chat ' + id.slice(4)), new FakeChild('slot-preview', 'No task yet')];
  }
  querySelector(selector) {
    if (selector === '.slot-name') return this.children.find(child => child.className === 'slot-name') || null;
    if (selector === '.slot-preview') return this.children.find(child => child.className === 'slot-preview') || null;
    if (selector === '.lane-option-label') return this.children.find(child => child.className === 'lane-option-label') || null;
    return null;
  }
  appendChild(child) { this.children.push(child); return child; }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  get textContent() { return this.children.map(child => child.textContent).join(' '); }
  set textContent(value) { this.children = [new FakeChild('', String(value))]; }
}
const buttons = [new FakeButton('slot1', true), new FakeButton('slot2', false), new FakeButton('slot3', false)];
const picker = {querySelector(selector) { return selector === 'button[id^="slot"].active' ? buttons[0] : null; }};
const current = {textContent: ''};
const toggle = {attrs: {}, title: '', setAttribute(name, value) { this.attrs[name] = String(value); }};
const elements = new Map([['slotbar', picker], ['lane-picker-current', current], ['lane-picker-toggle', toggle]]);
buttons.forEach(button => elements.set(button.id, button));
globalThis.document = {
  getElementById(id) { return elements.get(id) || null; },
  createElement() { return new FakeChild('', ''); },
};
"""
        checks = r"""
const nameNode = buttons[0].querySelector('.slot-name');
const previewNode = buttons[0].querySelector('.slot-preview');
syncAgentLanePicker();
if (buttons[0].querySelector('.slot-name') !== nameNode || buttons[0].querySelector('.slot-preview') !== previewNode) {
  throw new Error('rich trial slot labels were destroyed');
}
if (buttons[0].querySelector('.lane-option-label').textContent !== '1') throw new Error('compact label missing');
previewNode.textContent = 'Latest browser task';
syncAgentLanePicker();
if (buttons[0].dataset.laneDetail !== 'Latest browser task') throw new Error('preview detail did not refresh');
if (!buttons[0].attrs['aria-label'].includes('Latest browser task')) throw new Error('accessible label is stale');
if (current.textContent !== '1') throw new Error('active Thread number is wrong');
"""
        result = subprocess.run(
            [node],
            input=harness + sync_source + checks,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_agent_view_state_transitions_execute_consistently(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is required for Agent View runtime checks")

        from web_app import templates

        harness = r"""
class FakeClassList {
  constructor() { this.values = new Set(); }
  add(...values) { values.forEach(value => this.values.add(value)); }
  remove(...values) { values.forEach(value => this.values.delete(value)); }
  contains(value) { return this.values.has(value); }
  toggle(value, force) {
    const enabled = typeof force === 'boolean' ? force : !this.values.has(value);
    if (enabled) this.values.add(value); else this.values.delete(value);
    return enabled;
  }
}
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    id,
    classList: new FakeClassList(),
    attrs: {},
    textContent: '',
    scrollTop: 0,
    scrollHeight: 100,
    setAttribute(name, value) { this.attrs[name] = String(value); },
  });
  return elements.get(id);
}
const body = {classList: new FakeClassList(), dataset: {}, style: {removeProperty() {}}};
globalThis.document = {
  body,
  getElementById: element,
  querySelectorAll() { return []; },
  addEventListener() {},
};
let mobile = true;
globalThis.window = {
  matchMedia() { return {matches: mobile}; },
  addEventListener() {},
};
globalThis.requestAnimationFrame = function() {};
globalThis.location = {search: '?shell=legacy', protocol: 'https:', host: 'example.test'};
const sessionValues = new Map();
globalThis.sessionStorage = {
  getItem(key) { return sessionValues.get(key) || null; },
  setItem(key, value) { sessionValues.set(key, String(value)); },
  removeItem(key) { sessionValues.delete(key); },
};
globalThis._setActiveSlotSession = function() {};
globalThis.addUserBubble = function() {};
globalThis.appendText = function() {};
let sending = false;
"""
        checks = r"""
function expect(condition, message) { if (!condition) throw new Error(message); }
openAgentView();
expect(body.classList.contains('agent-view-open'), 'Agent View did not open');
expect(!body.classList.contains('agent-view-chat-open'), 'mobile should open on browser surface');

toggleAgentViewChat(true);
expect(body.classList.contains('agent-view-chat-open'), 'Chat did not open');
expandAgentViewChat();
expect(body.classList.contains('agent-view-chat-expanded'), 'Chat did not expand');
exitAgentViewFullscreen();
expect(!body.classList.contains('agent-view-chat-expanded'), 'Fullscreen exit did not dock chat');
expect(body.classList.contains('agent-view-chat-open'), 'Fullscreen exit hid transcript');

minimizeAgentViewChat();
expect(body.classList.contains('chat-minimized'), 'Chat did not minimize');
beginAgentViewResponseTurn();
maybeRevealAgentResponse();
expect(body.classList.contains('chat-minimized'), 'Response reveal overrode explicit minimize');
restoreAgentViewChat();
expect(!body.classList.contains('chat-minimized'), 'Restore left chat minimized');
expect(body.classList.contains('agent-view-chat-open'), 'Restore did not open transcript');

setAgentViewChatState('docked', 'browser');
beginAgentViewResponseTurn();
maybeRevealAgentResponse();
expect(body.classList.contains('agent-view-chat-open'), 'First mobile response did not reveal transcript');
setAgentViewChatState('docked', 'browser');
maybeRevealAgentResponse();
expect(!body.classList.contains('agent-view-chat-open'), 'Later response chunk ignored Browser choice');

closeAgentView();
beginAgentViewResponseTurn();
maybeRevealAgentResponse();
expect(!body.classList.contains('agent-view-open'), 'Response opened Agent View before browser activity');
openAgentView();
maybeRevealAgentResponse();
expect(body.classList.contains('agent-view-chat-open'), 'Response pending before browser activity was lost');

closeAgentView();
location.search = '?shell=task';
sessionId = 's-test-task';
initializeAgentShellExperiment();
expect(body.classList.contains('agent-shell-task'), 'Task shell flag did not activate');
expect(!body.classList.contains('agent-view-open'), 'Task shell opened before readiness');
maybeInitializeAgentShell({chat_connected:true, bridge_connected:true});
expect(body.classList.contains('agent-view-open'), 'Ready task shell did not default to Agent View');
expect(!body.classList.contains('agent-shell-chat-only'), 'Connected shell incorrectly used chat-only layout');

beginAgentViewResponseTurn();
appendText({}, 'text-only answer');
expect(body.classList.contains('agent-view-chat-expanded'), 'Text-only Auto turn did not expand chat');
ensureAgentViewForBrowserActivity('ddm');
expect(body.classList.contains('agent-view-chat-expanded'), 'Non-navigation work collapsed fullscreen chat');
expect(agentShellBrowserUsedThisTurn, 'Non-navigation work was not tracked as browser activity');
ensureAgentViewForBrowserActivity('navigate');
expect(!body.classList.contains('agent-view-chat-expanded'), 'Navigation did not restore the browser surface');
expect(!body.classList.contains('agent-view-chat-open'), 'Mobile navigation left the chat surface open');

expandAgentViewChat();
beginAgentViewResponseTurn();
expect(body.classList.contains('agent-view-chat-expanded'), 'Starting a turn collapsed fullscreen chat');
ensureAgentViewForBrowserActivity('click');
appendText({}, 'browser result');
expect(body.classList.contains('agent-view-chat-expanded'), 'Non-navigation response reveal collapsed fullscreen chat');

minimizeAgentViewChat();
expect(body.classList.contains('chat-minimized'), 'Minimizing chat did not release the browser stage');
restoreAgentViewChat();
expect(!body.classList.contains('chat-minimized'), 'Restoring chat kept the browser stage expanded');

closeAgentView();
maybeInitializeAgentShell({chat_connected:true, bridge_connected:true});
expect(body.classList.contains('agent-view-open'), 'Adaptive task shell did not recover after a status refresh');

location.search = '?shell=task';
initializeAgentShellExperiment();
maybeInitializeAgentShell({chat_connected:false, bridge_connected:false});
expect(body.classList.contains('agent-view-open'), 'Offline state left the adaptive shell');
expect(body.classList.contains('agent-shell-chat-only'), 'Offline state did not morph into chat workspace');
"""
        result = subprocess.run(
            [node],
            input=harness + templates._AGENT_VIEW_JS + checks,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_agent_view_preview_scaling_coalesces_and_pauses_during_pinch(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is required for Agent View runtime checks")

        from web_app import templates

        source = templates._AGENT_VIEW_JS
        start = source.index("let agentViewSemanticScaleScheduled")
        end = source.index("function renderAgentViewSemanticSnapshot", start)
        scale_source = source[start:end]
        harness = r"""
const frames = [{style:{}}, {style:{}}];
const canvas = {
  clientWidth: 320,
  clientHeight: 560,
  classList: {contains() { return false; }},
};
const image = {naturalWidth: 0, naturalHeight: 0};
const rafQueue = [];
const timers = new Map();
let nextTimer = 1;
globalThis.requestAnimationFrame = function(callback) { rafQueue.push(callback); return rafQueue.length; };
globalThis.setTimeout = function(callback, delay) { const id = nextTimer++; timers.set(id, {callback, delay}); return id; };
globalThis.clearTimeout = function(id) { timers.delete(id); };
globalThis.window = {
  visualViewport: {scale: 2},
  matchMedia() { return {matches: true}; },
};
globalThis.document = {
  getElementById(id) { return id === 'agent-view-canvas' ? canvas : (id === 'agent-view-image' ? image : null); },
  querySelectorAll(selector) { return selector === '.agent-view-semantic-frame' ? frames : []; },
};
let agentViewSnapshot = {viewport: {width: 1280, height: 720}};
function positionAgentViewMobileChat() {}
function flushRaf() { while (rafQueue.length) rafQueue.shift()(); }
function runTimers(delay) {
  [...timers.entries()].filter(([, timer]) => timer.delay === delay).forEach(([id, timer]) => {
    timers.delete(id);
    timer.callback();
  });
}
function expect(condition, message) { if (!condition) throw new Error(message); }
"""
        checks = r"""
for (let index = 0; index < 10; index += 1) handleAgentViewViewportResize();
expect(rafQueue.length === 1, 'resize work was not coalesced');
flushRaf();
expect(!frames[0].style.width, 'pinch zoom rewrote iframe styles');

runTimers(180);
flushRaf();
expect(frames[0].style.width === '1280px', 'settled zoom left the iframe unpositioned');

window.visualViewport.scale = 1;
handleAgentViewViewportResize();
flushRaf();
expect(frames[0].style.width === '1280px', 'scaling did not recover after pinch');
expect(!/NaN|Infinity/.test(JSON.stringify(frames)), 'valid scaling produced invalid CSS');

agentViewSnapshot = {viewport: {width: Infinity, height: NaN}};
scheduleAgentViewSemanticFrameScale(false);
flushRaf();
expect(frames[0].style.width === '1280px' && frames[0].style.height === '720px', 'invalid dimensions did not use fallbacks');

agentViewSnapshot = {viewport: {width: 100000, height: 100000}};
scheduleAgentViewSemanticFrameScale(false);
flushRaf();
expect(frames[0].style.width === '8192px' && frames[0].style.height === '8192px', 'dimensions were not bounded');
expect(!/NaN|Infinity/.test(JSON.stringify(frames)), 'bounded scaling produced invalid CSS');

const transform = frames[0].style.transform;
canvas.clientWidth = 0;
scheduleAgentViewSemanticFrameScale(false);
flushRaf();
expect(frames[0].style.transform === transform, 'zero-size canvas rewrote iframe styles');
"""
        result = subprocess.run(
            [node],
            input=harness + scale_source + checks,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_agent_view_reports_only_active_stylesheet_replay_failures(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is required for Agent View runtime checks")

        from web_app import templates

        source = templates._AGENT_VIEW_JS
        start = source.index("function agentViewStylesheetReplayStatus")
        end = source.index("function agentViewFindTarget", start)
        fidelity_source = source[start:end]
        harness = r"""
const fidelityElement = {textContent:'', title:''};
globalThis.document = {
  getElementById(id) { return id === 'agent-view-fidelity' ? fidelityElement : null; },
};
function link({sheet=null, disabled=false, alternate=false, media=''}) {
  return {
    sheet,
    disabled,
    relList: {contains(value) { return value === 'alternate' && alternate; }},
    getAttribute(name) { return name === 'media' ? media : ''; },
  };
}
const links = [
  link({sheet:{}}),
  link({sheet:null}),
  link({sheet:null, disabled:true}),
  link({sheet:null, alternate:true}),
  link({sheet:null, media:'print'}),
];
const frame = {
  contentDocument: {querySelectorAll() { return links; }},
  contentWindow: {matchMedia(media) { return {matches:media !== 'print'}; }},
};
function expect(condition, message) { if (!condition) throw new Error(message); }
"""
        checks = r"""
const replay = agentViewStylesheetReplayStatus(frame, true);
expect(replay.total === 2, 'inactive stylesheets were counted');
expect(replay.failed === 1, 'active replay failure was not counted');
agentViewUpdateFidelity({fidelity:{
  sourceInlineStyleSheets:4,
  capturedInlineStyleSheets:3,
  sourceStyleSheetLinks:2,
  omittedInlineStyleSheets:1,
  capturedHeadBytes:2048,
  capturedBodyBytes:4096,
  criticalStyleBytes:1024,
  headTruncated:true,
  truncationStage:'head-budget',
}}, frame, true);
expect(fidelityElement.textContent.includes('stylesheet replays failed'), 'replay failure was not surfaced');
expect(fidelityElement.title.includes('inline styles 3/4'), 'style counts were not included in diagnostics');
expect(fidelityElement.title.includes('computed fallback 1 KB'), 'computed fallback bytes were not included');
"""
        result = subprocess.run(
            [node],
            input=harness + fidelity_source + checks,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_trial_uses_full_width_shell_without_removing_other_sidebars(self):
        from web_app import templates

        trial = templates.TRIAL_CHAT_HTML
        self.assertIn('id="app-shell"', trial)
        self.assertNotIn('<aside id="sidebar">', trial)
        self.assertNotIn('id="sidebar-history"', trial)
        self.assertNotIn('id="sidebar-toggle"', trial)
        self.assertNotIn("function loadSidebarHistory()", trial)
        self.assertIn('class="topbar-new"', trial)
        self.assertIn('id="full-width-chat-shell"', trial)
        self.assertIn('<div id="slotbar" role="group" aria-label="Chat sessions">', trial)
        self.assertEqual(trial.count('id="lane-picker-toggle"'), 1)
        self.assertIn('id="chat-card-history" class="chat-size-btn" aria-label="Open chat archives"', trial)
        self.assertIn('onclick="openArchives()"', trial)
        self.assertIn('ensureAgentViewForBrowserActivity(name)', trial)

        for html in (
            templates.CLAUDE_CHAT_HTML,
            templates.CHAT_GEMINI_HTML,
            templates.CHAT_CLAUDE_SDK_HTML,
            templates.CHAT_CODEX_HTML,
        ):
            with self.subTest(title=html[html.index("<title>"):html.index("</title>")]):
                self.assertIn('<aside id="sidebar">', html)
                self.assertIn('id="sidebar-history"', html)
                self.assertIn('id="sidebar-toggle"', html)
                self.assertIn("function loadSidebarHistory()", html)
                self.assertNotIn('id="full-width-chat-shell"', html)

    def test_landing_signin_targets_last_provider_route(self):
        from web_app import templates

        self.assertIn('id="landing-auth-link"', templates.LANDING_HTML)
        self.assertIn("normalizeLandingRoute", templates.LANDING_HTML)
        self.assertIn("provider=codex-cli", templates.LANDING_HTML)
        self.assertIn("Codex CLI", templates.LANDING_HTML)
        self.assertIn("provider=opencode-cli", templates.LANDING_HTML)
        self.assertIn("OpenCode CLI", templates.LANDING_HTML)

    def test_chat_and_setup_pages_remember_provider_routes(self):
        from web_app import templates

        self.assertIn("_rememberLastAppRoute", templates.CLAUDE_CHAT_HTML)
        self.assertIn("/local?provider=opencode-cli", templates.CLAUDE_CHAT_HTML)
        self.assertIn("updateOpenCodeModelOptions", templates.CLAUDE_CHAT_HTML)
        self.assertIn("opencode_models", templates.CLAUDE_CHAT_HTML)
        self.assertIn("/local?provider=codex-cli", templates.CHAT_CODEX_HTML)
        self.assertIn("rememberSetupRoute", templates.SETUP_HTML)
        self.assertIn("unchained_last_route", templates.SETUP_HTML)

    def test_opencode_lane_state_uses_stable_provider_route(self):
        from web_app import templates

        html = templates.CLAUDE_CHAT_HTML
        self.assertIn("function currentLaneModel()", html)
        self.assertIn("? 'opencode-cli:' : model", html)
        self.assertEqual(html.count("model: currentLaneModel()"), 4)
        self.assertIn("const model = currentModel();", html)
        self.assertIn("model: model,", html)

    def test_opencode_model_refresh_preserves_concrete_selection(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is required for OpenCode model runtime checks")

        from web_app import templates

        html = templates.CLAUDE_CHAT_HTML
        start = html.index("function updateOpenCodeModelOptions(models)")
        end = html.index("\n\nlet activeSlot", start)
        function_source = html[start:end]
        harness = r"""
const select = {
  value: 'opencode-cli:openai/gpt-5.3-codex-spark',
  options: [],
  appendChild(option) { this.options.push(option); },
  set innerHTML(value) { this.options = []; this.value = ''; },
};
globalThis.document = {
  getElementById(id) { return id === 'modelsel' ? select : null; },
  createElement() { return {value: '', textContent: ''}; },
};
globalThis.localStorage = {
  getItem() { return 'opencode-cli:openai/gpt-5.3-codex-spark'; },
};
function _wantsOpenCodeModelOptions() { return true; }
function _opencodeOptionLabel(modelId) { return modelId; }
let _openCodeModelOptionsSignature = '';
"""
        checks = r"""
updateOpenCodeModelOptions(['anthropic/claude-sonnet-4-6']);
if (select.value !== 'opencode-cli:openai/gpt-5.3-codex-spark') {
  throw new Error('concrete model selection was replaced: ' + select.value);
}
if (!select.options.some(option => option.value === select.value)) {
  throw new Error('preserved concrete model was not added as an option');
}
"""
        result = subprocess.run(
            [node],
            input=harness + function_source + checks,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_local_new_chat_waits_for_authoritative_agent_ack(self):
        from web_app import templates

        html = templates.CLAUDE_CHAT_HTML
        start = html.index("async function doNewChat()")
        end = html.index("\nasync function openArchives()", start)
        new_chat = html[start:end]

        self.assertIn("if (localNewChatPending) return;", new_chat)
        self.assertNotIn("if (sending) return;", new_chat)
        acknowledged = new_chat.index("data.ok !== true")
        stream_abort = new_chat.index("if (_cancelCtrl) _cancelCtrl.abort();")
        transcript_reset = new_chat.index("document.getElementById('chat').innerHTML = '';")
        self.assertLess(acknowledged, transcript_reset)
        self.assertLess(stream_abort, transcript_reset)
        self.assertIn("Could not safely start a new chat.", new_chat)

    def test_ux_contract_copy_and_demo_routes(self):
        from web_app import templates

        self.assertIn("You navigate. The agent drives.", templates.LANDING_HTML)
        self.assertIn("font-family:'Familjen Grotesk',system-ui,sans-serif", templates.LANDING_HTML)
        self.assertNotIn("Instrument Serif", templates.LANDING_HTML)
        self.assertIn("Chrome profile", templates.LANDING_HTML)
        self.assertIn("Product preview", templates.LANDING_HTML)
        self.assertIn('aria-live="polite"', templates.LANDING_HTML)
        self.assertIn("prefers-reduced-motion:reduce", templates.LANDING_HTML)
        self.assertIn("See what you can <em>hand off</em>", templates.LANDING_HTML)
        self.assertIn("workflow-preview", templates.LANDING_HTML)
        self.assertIn("SCENARIOS", templates.LANDING_HTML)
        self.assertIn("Shortlist apartments", templates.LANDING_HTML)
        self.assertIn("Prepared result", templates.LANDING_HTML)
        self.assertIn("decision-tree", templates.LANDING_HTML)
        self.assertIn("Watch it work first", templates.LANDING_HTML)
        self.assertIn("Start a guided trial", templates.LANDING_HTML)
        self.assertIn("Recommended", templates.LANDING_HTML)
        self.assertIn("Built for browser work", templates.LANDING_HTML)
        self.assertIn('href="/demo" class="cta-btn">Try live demo', templates.LANDING_HTML)
        self.assertIn('href="/trial" class="signin" id="landing-auth-link">Start free trial</a>', templates.LANDING_HTML)
        self.assertIn("Start free trial", templates.LANDING_HTML)
        self.assertIn("Guided bridge install", templates.LANDING_HTML)
        self.assertIn("Work from a selected Chrome profile", templates.LANDING_HTML)
        self.assertIn("rec-cta", templates.LANDING_HTML)
        self.assertIn('href="/unbrowser"', templates.LANDING_HTML)
        self.assertIn('href="/chrome-tax"', templates.LANDING_HTML)
        self.assertNotIn("Drive my browser", templates.LANDING_HTML)
        self.assertNotIn("narrative rhythm", templates.LANDING_HTML)
        self.assertNotIn("Sign in / Sign up", templates.LANDING_HTML)

    def test_auth_setup_trust_and_accessibility_copy(self):
        from web_app import templates

        self.assertNotIn("Signing up as: <strong>Trial</strong>", templates.TRIAL_CHAT_HTML)
        self.assertNotIn("Signing up as: <strong>Claude</strong>", templates.CLAUDE_CHAT_HTML)
        self.assertIn("Mode: <strong>Your Chrome + free models</strong>", templates.TRIAL_CHAT_HTML)
        self.assertIn("Mode: <strong>Local CLI + your Chrome</strong>", templates.CLAUDE_CHAT_HTML)
        self.assertIn("Unchained Chat", templates.CLAUDE_CHAT_HTML)
        self.assertIn('role="dialog" aria-modal="true"', templates.FIRST_LOOK_PREVIEW_HTML)
        self.assertIn("showSampleRun", templates.FIRST_LOOK_PREVIEW_HTML)
        self.assertIn("Task for the shared browser", templates.FIRST_LOOK_PREVIEW_HTML)
        self.assertIn("Shared browser is reconnecting. Try again in a moment.", templates.FIRST_LOOK_PREVIEW_HTML)
        self.assertNotIn("Shared browser is unavailable right now.", templates.FIRST_LOOK_PREVIEW_HTML)
        self.assertIn("https://unchainedsky.com/install.sh", templates.MCP_PAGE_HTML)
        self.assertNotIn("https://api.unchainedsky.com/install.sh", templates.MCP_PAGE_HTML)
        self.assertIn("API key handling", templates.SETUP_HTML)
        self.assertIn("Your Chrome profile stays on your machine", templates.INSTALL_ONBOARD_HTML)

    def test_new_chat_transaction_storage_and_guest_failure_ordering(self):
        from web_app import templates

        trial = templates.TRIAL_CHAT_HTML
        self.assertIn("localStorage.getItem(_newChatStateKey())", trial)
        self.assertIn("localStorage.setItem(_newChatStateKey()", trial)
        self.assertIn("localStorage.removeItem(_newChatStateKey())", trial)
        self.assertIn("recoveringSource", trial)
        self.assertIn("Another tab is finishing New Chat", trial)
        self.assertIn("localStorage.setItem(_sessionStoreKey(), sid)", trial)
        self.assertIn("localStorage.setItem(_slotStateKey()", trial)

        guest = templates.FIRST_LOOK_PREVIEW_HTML
        self.assertIn('id="new-chat-feedback" role="status" aria-live="polite"', guest)
        self.assertIn("if (sending || guestNewChatPending) return;", guest)
        self.assertIn("request_id: guestNewChatRequestId()", guest)
        self.assertIn("localStorage.setItem(key, JSON.stringify(pending))", guest)
        self.assertIn("function syncGuestSessionFromStorage()", guest)
        self.assertIn("window.location.reload();", guest)
        self.assertIn("if (!syncGuestSessionFromStorage()) return;", guest)
        self.assertIn("data.request_id !== pending.request_id", guest)
        self.assertIn("data.previous_session_id !== previousSessionId", guest)
        self.assertIn("data.ok !== true || data.guest !== true", guest)
        self.assertIn("nextSessionId === previousSessionId", guest)
        self.assertIn("agentId !== previousAgentId || sessionId !== previousSessionId", guest)
        self.assertIn("r.status === 429", guest)
        self.assertIn("Your current chat is unchanged.", guest)

        new_chat = guest[guest.index("async function doNewChat()") : guest.index("function hideHints()")]
        parsed = new_chat.index("data = await r.json();")
        validated = new_chat.index("if (!data || data.ok !== true")
        adopted = new_chat.index("sessionId = nextSessionId;")
        transcript_reset = new_chat.index("chat.innerHTML =")
        preview_reset = new_chat.index("resetPreview();")
        draft_reset = new_chat.index("document.getElementById('msginput').value = '';")
        cleared = new_chat.rindex("clearGuestNewChatRequest(pending);")
        self.assertLess(parsed, validated)
        self.assertLess(validated, adopted)
        self.assertLess(adopted, transcript_reset)
        self.assertLess(transcript_reset, preview_reset)
        self.assertLess(preview_reset, draft_reset)
        self.assertLess(draft_reset, cleared)

    def test_signed_in_chat_templates_recover_active_turns_but_guest_preview_does_not(self):
        from web_app import templates

        script_tag = (
            '<script id="signed-chat-reconnect-runtime" '
            'src="/web/static/signed-chat-reconnect.js"></script>'
        )
        signed_in_templates = {
            "Trial": templates.TRIAL_CHAT_HTML,
            "Gemini": templates.CHAT_GEMINI_HTML,
            "Claude SDK": templates.CHAT_CLAUDE_SDK_HTML,
            "Codex": templates.CHAT_CODEX_HTML,
            "Local CLI": templates.CLAUDE_CHAT_HTML,
        }
        for lane, html in signed_in_templates.items():
            with self.subTest(lane=lane):
                self.assertEqual(html.count(script_tag), 1)
                self.assertEqual(html.count("chatReconnectFetch('/web/chat', {"), 1)
                self.assertEqual(html.count("chatReconnectFetch('/web/chat/cancel', {"), 1)
                self.assertEqual(html.count("chatReconnectFetch("), 2)
                self.assertNotIn("window.fetch =", html)
                self.assertNotIn("unchained_chat_active_turn_v1", html)
                self.assertEqual(html.count("checkSession();"), 1)
                self.assertIn(
                    script_tag + "\n<script>checkSession();</script>\n</body>",
                    html,
                )

        guest_preview = templates.FIRST_LOOK_PREVIEW_HTML
        self.assertNotIn(script_tag, guest_preview)
        self.assertNotIn("chatReconnectFetch", guest_preview)
        self.assertIn("fetch('/web/chat', {", guest_preview)
        self.assertIn("fetch('/web/chat/cancel', {", guest_preview)

        asset = (
            Path(__file__).with_name("web_app")
            / "static"
            / "signed-chat-reconnect.js"
        ).read_text(encoding="utf-8")
        active_index = asset.find("/web/chat/active")
        events_index = asset.find("/web/chat/events")
        self.assertNotEqual(active_index, -1)
        self.assertNotEqual(events_index, -1)
        self.assertLess(active_index, events_index)
        self.assertIn("window.chatReconnectFetch = function", asset)
        self.assertIn("function randomRequestId()", asset)
        self.assertIn("headers.set('X-Request-ID', reqId)", asset)
        self.assertIn("preserveRejectedDraft();", asset)
        self.assertIn("finishTurn('error', false);", asset)
        self.assertNotIn("else setTimeout(reconcileTurn, 0);", asset)
        mismatch = asset[asset.index("if (data.req_id && data.req_id !== reconnect.reqId)") :]
        mismatch = mismatch[:mismatch.index("updateActivity(data);")]
        self.assertIn("finishTurn('error', false);", mismatch)
        self.assertNotIn("restoreActiveTurn()", mismatch)
        self.assertNotIn("window.fetch =", asset)
        self.assertNotIn("localStorage", asset)
        self.assertNotIn("unchained_chat_active_turn_v1", asset)
        self.assertNotIn("<script", asset)

    def test_mcp_api_key_instructions_are_local_and_do_not_autofill(self):
        from agent_package import _WINDOWS_INSTALLER_TEMPLATE, _generate_public_install_script
        from web_app import templates

        html = templates.MCP_PAGE_HTML
        self.assertIn(
            'INSTALL_DIR="$HOME/unchained-agent"',
            _generate_public_install_script("https://unchainedsky.com"),
        )
        self.assertIn('$installDir = Join-Path $HOME "unchained-agent"', _WINDOWS_INSTALLER_TEMPLATE)
        self.assertIn("~/unchained-agent/.env", html)
        self.assertIn(r"%USERPROFILE%\unchained-agent\.env", html)
        self.assertIn('id="load-key-posix"', html)
        self.assertIn('id="load-key-windows"', html)
        self.assertIn("Select-Object -First 1", html)
        self.assertIn("Claude Desktop JSON does not expand shell variables", html)
        self.assertIn("If it cannot expand environment variables", html)
        self.assertIn("YOUR_UNCHAINED_API_KEY", html)
        self.assertNotIn("Sign in to auto-fill your API key", html)
        self.assertNotIn("YOUR_API_KEY", html)
        self.assertNotIn("/auth/me", html)
        self.assertNotIn("me.api_key", html)
        self.assertNotIn("copySnippet", html)
        self.assertNotIn("fillKey", html)

    def test_mcp_shell_copy_text_uses_loaded_environment_variable(self):
        from web_app import templates

        html = templates.MCP_PAGE_HTML

        def copied_text(snippet_id: str) -> str:
            match = re.search(
                rf'<pre class="code-block" id="{snippet_id}">(.*?)</pre>',
                html,
                re.DOTALL,
            )
            self.assertIsNotNone(match, f"missing copied snippet {snippet_id}")
            self.assertIn(f"copyCode('{snippet_id}',this)", html)
            return match.group(1)

        self.assertEqual(
            copied_text("snippet-claude-code"),
            """claude mcp add unchainedsky \\
  https://api.unchainedsky.com/mcp \\
  -t http \\
  -H \"Authorization: Bearer $UNCHAINED_API_KEY\"""",
        )
        self.assertEqual(
            copied_text("snippet-claude-code-windows"),
            """claude mcp add unchainedsky `
  https://api.unchainedsky.com/mcp `
  -t http `
  -H \"Authorization: Bearer $env:UNCHAINED_API_KEY\"""",
        )
        self.assertEqual(
            copied_text("snippet-agent-lookup"),
            'curl -s -H "Authorization: Bearer $UNCHAINED_API_KEY" '
            "https://api.unchainedsky.com/api/agents | python3 -m json.tool",
        )
        self.assertEqual(
            copied_text("snippet-agent-lookup-windows"),
            'Invoke-RestMethod -Headers @{ Authorization = "Bearer '
            '$env:UNCHAINED_API_KEY" } https://api.unchainedsky.com/api/agents '
            "| ConvertTo-Json -Depth 5",
        )
        self.assertIn("Bearer YOUR_UNCHAINED_API_KEY", copied_text("snippet-claude-desktop"))
        self.assertIn("Bearer YOUR_UNCHAINED_API_KEY", copied_text("snippet-other"))


if __name__ == "__main__":
    unittest.main()
