"""Semantic browser mirroring for an already-resolved agent tab.

Capture behavior started from unchained-mirror-demo PR #2, merge commit
84351eca7f69f7d5e9b5eb078bfc35c1dd335273, and includes production protocol
hardening for capture epochs, nested scroll state, and safe form fidelity.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Callable
from typing import Any, Literal, TypedDict

import cloud_tools


MIRROR_CHUNK_CHARS = 128 * 1024
MAX_MIRROR_PAYLOAD_CHARS = 4 * 1024 * 1024
DEFAULT_POLL_INTERVAL = 0.5
MAX_CONSECUTIVE_CAPTURE_ERRORS = 3

# Each Agent View WebSocket connection gets its own isolated mirror state in the
# source Chrome tab.  Without per-connection isolation, every new connection
# runs INSTALL_MIRROR_EXPRESSION which *disposes* the previous mirror and
# installs a fresh one with a new captureEpoch.  When two clients (phone +
# desktop) are open on the same chat session, this creates an install war:
# each client keeps disposing the other's mirror, scroll actions fail with
# stale-document (epoch mismatch), the source never scrolls, and the next
# snapshot shows scrollY=0 — the "page resets to top after a second" bug.
#
# Per-connection isolation is achieved by giving each stream a unique mirror
# key (Symbol.for key).  The default key preserves backward compatibility for
# any caller that does not opt in.
DEFAULT_MIRROR_KEY = "unchained.mirror.capture.v1"
_MIRROR_KEY_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _validate_mirror_key(key: str) -> str:
    """Validate a mirror symbol key to prevent JS injection in CDP expressions."""
    key = str(key or DEFAULT_MIRROR_KEY)
    if not _MIRROR_KEY_RE.fullmatch(key):
        raise ValueError("invalid mirror key")
    return key


class SemanticMirrorSnapshotEvent(TypedDict):
    type: Literal["snapshot"]
    snapshot: dict[str, Any]
    resync: bool


class SemanticMirrorPatchEvent(TypedDict):
    type: Literal["patch"]
    patch: dict[str, Any]


SemanticMirrorEvent = SemanticMirrorSnapshotEvent | SemanticMirrorPatchEvent


class SemanticActionInput(TypedDict, total=False):
    targetId: str
    kind: Literal["click", "input", "change", "key", "scroll"]
    value: str
    checked: bool
    key: str
    x: float
    y: float
    fx: float
    fy: float


# Vendored from mirrorCapture.ts at the baseline merge commit cited above.
#
# The templates below hardcode DEFAULT_MIRROR_KEY as the Symbol.for argument.
# build_install_mirror_expression() / build_drain_mirror_expression() swap in a
# per-connection key so that concurrent Agent View clients each get an isolated
# mirror state without disposing each other.
_INSTALL_MIRROR_TEMPLATE = r"""(() => {
  'use strict';

  const STATE_KEY = Symbol.for('unchained.mirror.capture.v1');
  const previous = window[STATE_KEY];
  if (previous && typeof previous.dispose === 'function') previous.dispose();

  const MAX_IDS = 100000;
  const MAX_NODES = 30000;
  const MAX_CAPTURE_BYTES = 2 * 1024 * 1024;
  const MAX_PATCH_BYTES = 1024 * 1024;
  const MAX_FRAGMENT_BYTES = 384 * 1024;
  const MAX_TEXT_LENGTH = 256 * 1024;
  const MAX_VALUE_LENGTH = 16384;
  const MAX_RECORDS = 2000;
  const MAX_DIRTY = 750;
  const MAX_SCROLL_POSITIONS = 500;
  const INLINE_RESULT_CHARS = 192 * 1024;
  const VIEWPORT_BUDGET_RESERVE = 0.4;
  const OMITTED_TAGS = new Set([
    'script', 'noscript', 'base', 'object', 'embed', 'applet', 'frame',
    'frameset', 'iframe', 'portal'
  ]);
  const ACTIVE_ATTRIBUTES = new Set([
    'action', 'autofocus', 'autoplay', 'contenteditable', 'download',
    'draggable', 'form', 'formaction', 'formenctype', 'formmethod',
    'formnovalidate', 'formtarget', 'method', 'ping', 'popovertarget',
    'popovertargetaction', 'srcdoc'
  ]);
  const URL_ATTRIBUTES = new Set([
    'href', 'src', 'poster', 'cite', 'background', 'xlink:href'
  ]);
  const ACTIVE_LINK_RELS = new Set([
    'dns-prefetch', 'modulepreload', 'preconnect', 'prefetch', 'preload', 'prerender'
  ]);
  const SENSITIVE_PATTERN = /(?:^|[^a-z0-9])(?:cc-?(?:number|name|csc|cvc|cvv|exp|expiry)|card-?(?:number|holder|csc|cvc|cvv|exp|expiry)|credit-?card|security-?code|one-?time-?(?:code|password)|verification-?code|passcode|otp)(?:$|[^a-z0-9])/i;

  const state = {
    url: location.href,
    captureEpoch: typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
      ? crypto.randomUUID()
      : Date.now().toString(36) + '-' + Math.random().toString(36).slice(2),
    seq: 0,
    nextId: 1,
    nodeToId: new WeakMap(),
    idToNode: new Map(),
    replaceRoots: new Set(),
    attributeDirty: new Set(),
    textDirty: new Map(),
    removedDirty: new Map(),
    stateDirty: new Set(),
    scrollDirty: new Map(),
    allowedValueIds: new Set(),
    observer: null,
    observedRoots: new WeakSet(),
    recordCount: 0,
    overflow: false,
    disposed: false,
    headId: '',
    outbound: ''
  };
  window[STATE_KEY] = state;

  function byteLength(value) {
    return new TextEncoder().encode(value).length;
  }

  function stringifyWithSize(payload) {
    payload.rawBytes = 0;
    let encoded = '';
    for (let index = 0; index < 4; index += 1) {
      encoded = JSON.stringify(payload);
      const size = byteLength(encoded);
      if (size === payload.rawBytes) return encoded;
      payload.rawBytes = size;
    }
    return JSON.stringify(payload);
  }

  function prepareOutbound(encoded) {
    if (encoded.length <= INLINE_RESULT_CHARS) return encoded;
    state.outbound = encoded;
    return JSON.stringify({ __unchainedMirrorChunks: 1, length: encoded.length });
  }

  function hashString(value) {
    let hash = 2166136261;
    for (let index = 0; index < value.length; index += 1) {
      hash ^= value.charCodeAt(index);
      hash = Math.imul(hash, 16777619);
    }
    return 'fnv1a-' + (hash >>> 0).toString(16).padStart(8, '0');
  }

  function parentElementAcrossShadow(node) {
    if (!node) return null;
    if (node.parentElement) return node.parentElement;
    const root = typeof node.getRootNode === 'function' ? node.getRootNode() : null;
    return root && root.host && root.host.nodeType === 1 ? root.host : null;
  }

  function isComposedAncestor(ancestor, node) {
    for (let current = node; current; current = parentElementAcrossShadow(current)) {
      if (current === ancestor) return true;
    }
    return false;
  }

  function idFor(node) {
    let id = state.nodeToId.get(node);
    if (id) {
      state.idToNode.set(id, node);
      return id;
    }
    if (state.nextId > MAX_IDS) {
      state.overflow = true;
      return '';
    }
    id = 'ucm-' + state.nextId.toString(36);
    state.nextId += 1;
    state.nodeToId.set(node, id);
    state.idToNode.set(id, node);
    return id;
  }

  function fieldDescriptor(element) {
    return [
      element.getAttribute('type') || '',
      element.getAttribute('name') || '',
      element.getAttribute('id') || '',
      element.getAttribute('autocomplete') || '',
      element.getAttribute('placeholder') || '',
      element.getAttribute('aria-label') || '',
      element.getAttribute('inputmode') || ''
    ].join(' ');
  }

  function isSensitive(element) {
    if (!element || element.nodeType !== 1) return false;
    const tag = element.localName;
    if (tag !== 'input' && tag !== 'textarea' && tag !== 'select') return false;
    const type = (element.getAttribute('type') || '').toLowerCase();
    if (type === 'password' || type === 'hidden' || type === 'file' || element.hasAttribute('hidden')) return true;
    const autocomplete = (element.getAttribute('autocomplete') || '').toLowerCase();
    if (autocomplete === 'current-password' || autocomplete === 'new-password' ||
        autocomplete === 'one-time-code' || autocomplete.startsWith('cc-')) return true;
    const descriptor = fieldDescriptor(element);
    const compact = descriptor.toLowerCase().replace(/[^a-z0-9]/g, '');
    return SENSITIVE_PATTERN.test(descriptor) ||
      /(?:cardnumber|creditcard|securitycode|onetimecode|verificationcode|passcode|cvc|cvv|csc|otp)/.test(compact);
  }

  function canExposeValue(element) {
    if (!element || element.nodeType !== 1 || isSensitive(element)) return false;
    const tag = element.localName;
    if (tag !== 'input' && tag !== 'textarea') return false;
    const type = (element.getAttribute('type') || 'text').toLowerCase();
    if (/^(?:email|tel|password|file|hidden)$/.test(type)) return false;
    const autocomplete = (element.getAttribute('autocomplete') || '').toLowerCase();
    if (autocomplete && /(?:name|email|tel|street|address|postal|country|username|webauthn|password|cc-|one-time-code)/.test(autocomplete)) return false;
    const descriptor = fieldDescriptor(element).toLowerCase();
    if (/(?:first|last|full|family|given).?name|e-?mail|phone|mobile|street|address|postal|zip.?code|social.?security|passport|license/.test(descriptor)) return false;
    const role = (element.getAttribute('role') || '').toLowerCase();
    return type === 'search' || role === 'searchbox' || role === 'combobox';
  }

  function isCrossOriginFrame(element) {
    const raw = element.getAttribute('src');
    if (raw && raw !== 'about:blank' && !raw.startsWith('#')) {
      try {
        if (new URL(raw, document.baseURI).origin !== location.origin) return true;
      } catch (_error) {
        return true;
      }
    }
    try {
      if (element.contentDocument) return element.contentDocument.location.origin !== location.origin;
    } catch (_error) {
      return true;
    }
    return false;
  }

  function shouldOmitElement(element, fidelity) {
    const tag = element.localName;
    if (isSensitive(element)) {
      if (fidelity) fidelity.omittedSensitiveFields += 1;
      const type = (element.getAttribute('type') || '').toLowerCase();
      if (tag === 'input' && type === 'hidden') return true;
    }
    if (tag === 'meta' && (element.getAttribute('http-equiv') || '').toLowerCase() === 'refresh') return true;
    if (tag === 'link') {
      const rels = (element.getAttribute('rel') || '').toLowerCase().split(/\s+/).filter(Boolean);
      if (rels.some((rel) => ACTIVE_LINK_RELS.has(rel))) return true;
    }
    if (tag === 'iframe' || tag === 'frame') {
      if (fidelity && isCrossOriginFrame(element)) fidelity.crossOriginFrames += 1;
      return true;
    }
    return OMITTED_TAGS.has(tag);
  }

  function safeUrl(raw) {
    const value = String(raw || '').trim();
    if (!value) return '';
    if (value.startsWith('#')) return value;
    try {
      const url = new URL(value, document.baseURI);
      if (url.protocol !== 'http:' && url.protocol !== 'https:') return '';
      url.username = '';
      url.password = '';
      return url.href;
    } catch (_error) {
      return '';
    }
  }

  function safeSrcset(raw) {
    const candidates = String(raw || '').split(',');
    const safe = [];
    for (const candidate of candidates) {
      const parts = candidate.trim().split(/\s+/);
      const url = safeUrl(parts.shift() || '');
      if (!url) continue;
      const descriptor = parts.join(' ');
      if (descriptor && !/^(?:\d+(?:\.\d+)?x|\d+w)$/.test(descriptor)) continue;
      safe.push(url + (descriptor ? ' ' + descriptor : ''));
    }
    return safe.join(', ');
  }

  function safeCss(css) {
    let value = String(css || '');
    value = value.replace(/url\(\s*(['"]?)(.*?)\1\s*\)/gi, (_match, _quote, raw) => {
      const url = safeUrl(raw);
      return url ? 'url("' + url.replace(/"/g, '%22') + '")' : 'url("")';
    });
    value = value.replace(/@import\s+(['"])(.*?)\1/gi, (_match, _quote, raw) => {
      const url = safeUrl(raw);
      return url ? '@import url("' + url.replace(/"/g, '%22') + '")' : '';
    });
    value = value.replace(/(?:expression\s*\(|-moz-binding\s*:|behavior\s*:)/gi, 'ucm-blocked:');
    return value;
  }

  function safeRawText(text) {
    return String(text || '').replace(/&/g, '&amp;').replace(/</g, '&lt;');
  }

  function spend(budget, value, overhead, priority = true) {
    const cost = byteLength(value) + overhead;
    if (!priority && Number.isFinite(budget.nonPriorityLimit) &&
        budget.nonPriorityBytes + cost > budget.nonPriorityLimit) {
      budget.truncated = true;
      return false;
    }
    if (budget.bytes + cost > budget.limit) {
      budget.truncated = true;
      return false;
    }
    budget.bytes += cost;
    if (!priority && Number.isFinite(budget.nonPriorityLimit)) budget.nonPriorityBytes += cost;
    return true;
  }

  function setSafeAttribute(clone, attribute, value, budget, priority) {
    if (!spend(budget, attribute.name + value, 6, priority)) return;
    try {
      if (attribute.namespaceURI) clone.setAttributeNS(attribute.namespaceURI, attribute.name, value);
      else clone.setAttribute(attribute.name, value);
    } catch (_error) {
      // Invalid namespaced attributes are omitted rather than breaking capture.
    }
  }

  function populateSafeAttributes(source, clone, budget, priority) {
    for (const attribute of Array.from(source.attributes || [])) {
      const name = attribute.name.toLowerCase();
      if (name === 'data-ucm-id' || name.startsWith('on') || ACTIVE_ATTRIBUTES.has(name)) continue;
      if (name === 'value' && (source.localName === 'input' || source.localName === 'option')) continue;
      if ((name === 'checked' && source.localName === 'input') ||
          (name === 'selected' && source.localName === 'option')) continue;

      let value = attribute.value;
      if (URL_ATTRIBUTES.has(name)) value = safeUrl(value);
      else if (name === 'srcset' || name === 'imagesrcset') value = safeSrcset(value);
      else if (name === 'style') value = safeCss(value);
      if ((URL_ATTRIBUTES.has(name) || name === 'srcset' || name === 'imagesrcset') && !value) continue;
      setSafeAttribute(clone, attribute, value, budget, priority);
    }

    if (source.localName === 'input') {
      const type = (source.getAttribute('type') || 'text').toLowerCase();
      if (type === 'checkbox' || type === 'radio') {
        if (source.checked) clone.setAttribute('checked', '');
      } else if (canExposeValue(source)) {
        const safeValue = String(source.value || '').slice(0, MAX_VALUE_LENGTH);
        if (spend(budget, 'value' + safeValue, 6, priority)) clone.setAttribute('value', safeValue);
      }
    } else if (source.localName === 'textarea' && canExposeValue(source)) {
      const safeValue = String(source.value || '').slice(0, MAX_VALUE_LENGTH);
      if (spend(budget, safeValue, 0, priority)) clone.textContent = safeValue;
    } else if (source.localName === 'option' && source.selected) {
      clone.setAttribute('selected', '');
    } else if (source.localName === 'img') {
      const currentSrc = safeUrl(source.currentSrc || source.getAttribute('src') || '');
      if (currentSrc && spend(budget, 'src' + currentSrc, 6, priority)) clone.setAttribute('src', currentSrc);
      clone.removeAttribute('loading');
    }
  }

  function shallowSanitizedClone(source, budget, fidelity, priority = true) {
    if (/^(?:canvas|video|iframe|frame|object|embed)$/.test(source.localName)) {
      const rect = source.getBoundingClientRect();
      if (rect.width > 1 && rect.height > 1) {
        const style = getComputedStyle(source);
        if (style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0') {
          const id = idFor(source);
          if (!id) return null;
          const placeholder = document.createElement('div');
          placeholder.setAttribute('data-ucm-id', id);
          placeholder.setAttribute('data-ucm-placeholder', source.localName);
          placeholder.setAttribute('aria-label', source.localName + ' region');
          if (fidelity && (source.localName === 'iframe' || source.localName === 'frame') && isCrossOriginFrame(source)) fidelity.crossOriginFrames += 1;
          const width = Math.round(rect.width);
          const height = Math.round(rect.height);
          placeholder.style.cssText = 'display:grid;place-items:center;box-sizing:border-box;width:' + width + 'px;height:' + height + 'px;max-width:100%;background:#e8e4dc;color:#666;font:12px/1.4 system-ui,sans-serif;border:1px dashed #aaa;min-height:48px;';
          placeholder.textContent = source.localName + ' (' + width + 'x' + height + ')';
          if (!spend(budget, placeholder.outerHTML, 0, priority)) return null;
          return placeholder;
        }
      }
    }
    if (shouldOmitElement(source, fidelity)) return null;
    let clone;
    try {
      clone = source.cloneNode(false);
    } catch (_error) {
      return null;
    }
    for (const attribute of Array.from(clone.attributes || [])) clone.removeAttributeNode(attribute);
    populateSafeAttributes(source, clone, budget, priority);
    const id = idFor(source);
    if (!id) return null;
    clone.setAttribute('data-ucm-id', id);
    return clone;
  }

  function cloneSanitized(source, budget, fidelity) {
    if (!source || budget.nodes >= MAX_NODES || state.overflow) {
      budget.truncated = true;
      return null;
    }

    if (source.nodeType === Node.TEXT_NODE) {
      let text = source.data || '';
      const parent = source.parentElement;
      if (parent && parent.localName === 'style') text = safeCss(text).replace(/</g, ' ');
      else if (parent && (parent.localName === 'title' || parent.localName === 'textarea')) text = safeRawText(text);
      if (text.length > MAX_TEXT_LENGTH) {
        text = text.slice(0, MAX_TEXT_LENGTH);
        budget.truncated = true;
      }
      const priority = !budget.priorityNodes || Boolean(parent && budget.priorityNodes.has(parent));
      if (!spend(budget, text, 0, priority)) return null;
      return document.createTextNode(text);
    }
    if (source.nodeType === Node.COMMENT_NODE) {
      return null;
    }
    if (source.nodeType !== Node.ELEMENT_NODE) return null;

    const priority = !budget.priorityNodes || budget.priorityNodes.has(source);
    if (!spend(budget, source.localName || '', 12, priority)) return null;
    budget.nodes += 1;
    const clone = shallowSanitizedClone(source, budget, fidelity, priority);
    if (!clone) return null;
    if (clone.hasAttribute('data-ucm-placeholder')) return clone;
    // Textarea child text is a form value; only the allowlisted value set above may cross.
    if (source.localName === 'textarea') return clone;

    if (source.localName === 'style') {
      let liveCss = '';
      try {
        liveCss = Array.from(source.sheet?.cssRules || []).map((rule) => rule.cssText).join('\n');
      } catch (_error) {
        // Inaccessible sheets fall back to their DOM text below.
      }
      if (liveCss) {
        const text = safeCss(liveCss).replace(/</g, ' ');
        if (!spend(budget, text, 0, priority)) return clone;
        clone.appendChild(document.createTextNode(text));
        return clone;
      }
    }

    if (source.shadowRoot && source.shadowRoot.mode === 'open') {
      if (fidelity) fidelity.shadowRoots += 1;
      const template = document.createElement('template');
      template.setAttribute('shadowrootmode', 'open');
      for (const child of Array.from(source.shadowRoot.childNodes)) {
        const childClone = cloneSanitized(child, budget, fidelity);
        if (childClone) template.content.appendChild(childClone);
      }
      clone.appendChild(template);
    }

    if (source.localName === 'textarea') {
      return clone;
    }

    const sourceChildren = source.localName === 'template' && source.content
      ? source.content.childNodes
      : source.childNodes;
    const cloneTarget = source.localName === 'template' && clone.content ? clone.content : clone;
    for (const child of Array.from(sourceChildren)) {
      const childClone = cloneSanitized(child, budget, fidelity);
      if (childClone) cloneTarget.appendChild(childClone);
      if (budget.truncated && budget.bytes >= budget.limit) break;
    }
    return clone;
  }

  function attributesObject(element) {
    const result = {};
    for (const attribute of Array.from(element.attributes || [])) result[attribute.name] = attribute.value;
    return result;
  }

  function doctypeString(doctype) {
    if (!doctype) return '';
    const name = String(doctype.name || 'html').replace(/[^a-z0-9:_-]/gi, '') || 'html';
    const publicId = String(doctype.publicId || '').slice(0, 4096).replace(/[<>]/g, '').replace(/"/g, '&quot;');
    const systemId = String(doctype.systemId || '').slice(0, 4096).replace(/[<>]/g, '').replace(/"/g, '&quot;');
    let value = '<!DOCTYPE ' + name;
    if (publicId) value += ' PUBLIC "' + publicId + '"';
    if (systemId) value += (publicId ? ' "' : ' SYSTEM "') + systemId + '"';
    return value + '>';
  }

  function walkOpenTree(root, visit, limit) {
    const stack = [];
    for (const child of Array.from(root.children || []).reverse()) stack.push(child);
    let seen = 0;
    while (stack.length && seen < limit) {
      const element = stack.pop();
      seen += 1;
      visit(element);
      const children = Array.from(element.children || []);
      for (let index = children.length - 1; index >= 0; index -= 1) stack.push(children[index]);
      if (element.shadowRoot && element.shadowRoot.mode === 'open') {
        const shadowChildren = Array.from(element.shadowRoot.children || []);
        for (let index = shadowChildren.length - 1; index >= 0; index -= 1) stack.push(shadowChildren[index]);
      }
    }
  }

  function isInViewport(element) {
    const rect = element.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return false;
    return rect.bottom > 0 && rect.right > 0 && rect.top < window.innerHeight && rect.left < window.innerWidth;
  }

  function collectViewportPriorityNodes() {
    const priorityNodes = new WeakSet();
    walkOpenTree(document, (element) => {
      if (!isInViewport(element)) return;
      for (let current = element; current; current = parentElementAcrossShadow(current)) {
        priorityNodes.add(current);
      }
    }, MAX_NODES);
    return priorityNodes;
  }

  function countVisualRegions() {
    let count = 0;
    walkOpenTree(document, (element) => {
      if (!/^(?:canvas|video|iframe|object|embed)$/.test(element.localName)) return;
      const rect = element.getBoundingClientRect();
      if (rect.width <= 1 || rect.height <= 1) return;
      const style = getComputedStyle(element);
      if (style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0') count += 1;
    }, 10000);
    return count;
  }

  function collectScrollPositions() {
    const positions = [];
    walkOpenTree(document, (element) => {
      if (positions.length >= MAX_SCROLL_POSITIONS) return;
      if (element === document.scrollingElement || element === document.documentElement || element === document.body) return;
      const x = Math.round(element.scrollLeft || 0);
      const y = Math.round(element.scrollTop || 0);
      if (!x && !y) return;
      const targetId = state.nodeToId.get(element);
      if (targetId) positions.push({ targetId, x, y });
    }, MAX_NODES);
    return positions;
  }

  function serializeAdoptedStyleSheets(priorityNodes) {
    const entries = [];
    const MAX_ADOPTED_BYTES = 256 * 1024;
    let totalBytes = 0;
    let omittedSheets = 0;
    const addSheets = (sheets, hostId) => {
      if (!sheets || !sheets.length) return;
      const rules = [];
      let sheetCount = 0;
      for (const sheet of sheets) {
        let css = '';
        try {
          css = safeCss(Array.from(sheet.cssRules || []).map((rule) => rule.cssText).join('\n')).replace(/</g, ' ');
        } catch (_error) {
          omittedSheets += 1;
          continue;
        }
        if (!css) continue;
        const cssBytes = byteLength(css);
        if (totalBytes + cssBytes > MAX_ADOPTED_BYTES) {
          omittedSheets += 1;
          continue;
        }
        rules.push(css);
        sheetCount += 1;
        totalBytes += cssBytes;
      }
      if (rules.length) {
        entries.push({ hostId, css: rules.join('\n'), sheetCount });
      }
    };
    addSheets(document.adoptedStyleSheets, 'document');
    const hosts = [];
    walkOpenTree(document, (element) => {
      if (element.shadowRoot && element.shadowRoot.mode === 'open') {
        hosts.push(element);
      }
    }, 10000);
    hosts.sort((left, right) => Number(priorityNodes.has(right)) - Number(priorityNodes.has(left)));
    for (const host of hosts) {
      const sheets = host.shadowRoot.adoptedStyleSheets;
      const hostId = state.nodeToId.get(host) || '';
      if (!hostId) {
        omittedSheets += sheets?.length || 0;
        continue;
      }
      addSheets(sheets, hostId);
    }
    return { entries, omittedSheets };
  }

  const fidelity = {
    visualRegions: countVisualRegions(),
    crossOriginFrames: 0,
    shadowRoots: 0,
    omittedAdoptedStyleSheets: 0,
    omittedSensitiveFields: 0,
    truncated: false
  };
  const priorityNodes = collectViewportPriorityNodes();
  const captureLimit = Math.floor(MAX_CAPTURE_BYTES * 0.72);
  const budget = {
    bytes: 0,
    limit: captureLimit,
    nonPriorityBytes: 0,
    nonPriorityLimit: Math.floor(captureLimit * (1 - VIEWPORT_BUDGET_RESERVE)),
    priorityNodes,
    nodes: 0,
    truncated: false
  };
  const htmlClone = document.documentElement ? cloneSanitized(document.documentElement, budget, fidelity) : null;
  const headClone = htmlClone ? Array.from(htmlClone.children).find((child) => child.localName === 'head') : null;
  const bodyClone = htmlClone ? Array.from(htmlClone.children).find((child) => child.localName === 'body') : null;
  state.headId = document.head ? state.nodeToId.get(document.head) || '' : '';
  fidelity.truncated = budget.truncated || state.overflow;

  const adoptedResult = serializeAdoptedStyleSheets(priorityNodes);
  const adoptedStyles = adoptedResult.entries;
  fidelity.omittedAdoptedStyleSheets = adoptedResult.omittedSheets;

  const snapshot = {
    captureEpoch: state.captureEpoch,
    url: location.href.slice(0, MAX_TEXT_LENGTH),
    title: String(document.title || '').slice(0, MAX_TEXT_LENGTH),
    doctype: doctypeString(document.doctype),
    head: headClone ? headClone.innerHTML : '',
    body: bodyClone ? bodyClone.innerHTML : '',
    htmlAttrs: htmlClone ? attributesObject(htmlClone) : {},
    bodyAttrs: bodyClone ? attributesObject(bodyClone) : {},
    viewport: {
      width: Math.max(0, Math.round(window.innerWidth || 0)),
      height: Math.max(0, Math.round(window.innerHeight || 0)),
      deviceScaleFactor: Number(window.devicePixelRatio || 1),
      scrollX: Math.round(window.scrollX || 0),
      scrollY: Math.round(window.scrollY || 0)
    },
    scrollPositions: collectScrollPositions(),
    fidelity,
    adoptedStyles,
    hash: '',
    rawBytes: 0
  };

  function refreshSnapshotHash() {
    snapshot.hash = hashString(JSON.stringify([
      snapshot.url, snapshot.title, snapshot.doctype, snapshot.head, snapshot.body,
      snapshot.htmlAttrs, snapshot.bodyAttrs, snapshot.viewport, snapshot.scrollPositions, snapshot.adoptedStyles
    ]));
  }

  refreshSnapshotHash();
  let snapshotJson = stringifyWithSize(snapshot);
  if (byteLength(snapshotJson) > MAX_CAPTURE_BYTES) {
    snapshot.body = '<div data-ucm-capture-truncated="output-limit"></div>';
    snapshot.fidelity.truncated = true;
    refreshSnapshotHash();
    snapshotJson = stringifyWithSize(snapshot);
  }
  if (byteLength(snapshotJson) > MAX_CAPTURE_BYTES) {
    snapshot.head = '';
    refreshSnapshotHash();
    snapshotJson = stringifyWithSize(snapshot);
  }
  if (byteLength(snapshotJson) > MAX_CAPTURE_BYTES) {
    const htmlId = snapshot.htmlAttrs['data-ucm-id'];
    const bodyId = snapshot.bodyAttrs['data-ucm-id'];
    snapshot.title = snapshot.title.slice(0, 4096);
    snapshot.doctype = snapshot.doctype.slice(0, 4096);
    snapshot.htmlAttrs = htmlId ? { 'data-ucm-id': htmlId } : {};
    snapshot.bodyAttrs = bodyId ? { 'data-ucm-id': bodyId } : {};
    refreshSnapshotHash();
    snapshotJson = stringifyWithSize(snapshot);
  }

  function isOmittedSubtree(node) {
    for (let current = node && node.nodeType === 1 ? node : parentElementAcrossShadow(node);
         current;
         current = parentElementAcrossShadow(current)) {
      if (shouldOmitElement(current, null)) return true;
    }
    return false;
  }

  function dirtySize() {
    return state.replaceRoots.size + state.attributeDirty.size + state.textDirty.size +
      state.removedDirty.size + state.stateDirty.size + state.scrollDirty.size;
  }

  function checkDirtyLimit() {
    if (dirtySize() > MAX_DIRTY) state.overflow = true;
  }

  function markReplace(element) {
    if (!element || element.nodeType !== 1 || isOmittedSubtree(element)) return;
    if (element === document.head || element === document.body || element === document.documentElement) {
      state.overflow = true;
      return;
    }
    if (!state.nodeToId.get(element)) {
      const parent = parentElementAcrossShadow(element);
      if (parent) return markReplace(parent);
      state.overflow = true;
      return;
    }
    for (const root of Array.from(state.replaceRoots)) {
      if (isComposedAncestor(root, element)) return;
      if (isComposedAncestor(element, root)) state.replaceRoots.delete(root);
    }
    state.replaceRoots.add(element);
    checkDirtyLimit();
  }

  function observeOpenRoots(root) {
    if (!state.observer || !root) return;
    const observe = (candidate) => {
      if (!candidate || state.observedRoots.has(candidate)) return;
      state.observedRoots.add(candidate);
      state.observer.observe(candidate, {
        subtree: true,
        childList: true,
        attributes: true,
        characterData: true
      });
    };
    if (root.nodeType === Node.DOCUMENT_NODE || root.nodeType === Node.DOCUMENT_FRAGMENT_NODE) observe(root);
    if (root.nodeType === Node.ELEMENT_NODE && root.shadowRoot && root.shadowRoot.mode === 'open') observe(root.shadowRoot);
    walkOpenTree(root.nodeType === Node.ELEMENT_NODE ? { children: [root] } : root, (element) => {
      if (element.shadowRoot && element.shadowRoot.mode === 'open') observe(element.shadowRoot);
    }, 10000);
  }

  function onMutations(records) {
    if (state.disposed || state.overflow) return;
    state.recordCount += records.length;
    if (state.recordCount > MAX_RECORDS) {
      state.overflow = true;
      return;
    }

    for (const record of records) {
      const targetElement = record.target && record.target.nodeType === 1
        ? record.target
        : record.target?.parentElement;
      if (targetElement && (
        targetElement === document.head || targetElement === document.body || targetElement === document.documentElement ||
        Boolean(document.head && document.head.contains(targetElement))
      )) {
        state.overflow = true;
        return;
      }
      if (record.type === 'childList') {
        for (const added of Array.from(record.addedNodes)) observeOpenRoots(added);
        if (record.addedNodes.length) markReplace(record.target.nodeType === 1 ? record.target : record.target.host);
        for (const removed of Array.from(record.removedNodes)) {
          if (record.addedNodes.length || removed.nodeType !== 1) {
            if (!record.addedNodes.length && removed.nodeType !== 1) {
              markReplace(record.target.nodeType === 1 ? record.target : record.target.host);
            }
            continue;
          }
          const id = state.nodeToId.get(removed);
          if (id) state.removedDirty.set(id, removed);
          else markReplace(record.target.nodeType === 1 ? record.target : record.target.host);
        }
      } else if (record.type === 'attributes') {
        if (record.target === document.head) state.overflow = true;
        else if (!isOmittedSubtree(record.target)) state.attributeDirty.add(record.target);
      } else if (record.type === 'characterData') {
        const parent = record.target.parentElement;
        if (!parent || isOmittedSubtree(parent)) continue;
        if (parent.localName === 'style' || parent.localName === 'title' || parent.localName === 'textarea') {
          markReplace(parent);
          continue;
        }
        if (parent.childNodes.length === 1 && parent.firstChild === record.target && state.nodeToId.get(parent)) {
          let text = String(record.target.data || '');
          if (parent.localName === 'style') text = safeCss(text);
          if (text.length <= MAX_TEXT_LENGTH) state.textDirty.set(parent, text);
          else state.overflow = true;
        } else {
          markReplace(parent);
        }
      }
      checkDirtyLimit();
      if (state.overflow) return;
    }
  }

  state.observer = new MutationObserver(onMutations);
  observeOpenRoots(document);

  function onStateEvent(event) {
    const element = event.target;
    if (!element || element.nodeType !== 1 || isSensitive(element) || isOmittedSubtree(element)) return;
    if (element.localName !== 'input' && element.localName !== 'textarea' &&
        element.localName !== 'select' && !element.isContentEditable) return;
    if (!state.nodeToId.get(element)) idFor(element);
    state.stateDirty.add(element);
    checkDirtyLimit();
  }

  function onScrollEvent(event) {
    const target = event.target;
    if (target === document) {
      state.scrollDirty.set(document, {
        x: Math.round(window.scrollX || 0),
        y: Math.round(window.scrollY || 0)
      });
    } else if (target && target.nodeType === 1 && !isOmittedSubtree(target)) {
      if (!state.nodeToId.get(target)) idFor(target);
      state.scrollDirty.set(target, {
        x: Math.round(target.scrollLeft || 0),
        y: Math.round(target.scrollTop || 0)
      });
    }
    checkDirtyLimit();
  }

  document.addEventListener('input', onStateEvent, true);
  document.addEventListener('change', onStateEvent, true);
  document.addEventListener('scroll', onScrollEvent, true);

  function coveredByReplace(element) {
    for (const root of state.replaceRoots) {
      if (isComposedAncestor(root, element)) return true;
    }
    return false;
  }

  function stateOperation(element) {
    const targetId = state.nodeToId.get(element);
    if (!targetId || isSensitive(element)) return null;
    const operation = { op: 'state', targetId };
    if (element.localName === 'input') {
      const type = (element.getAttribute('type') || 'text').toLowerCase();
      if (type === 'checkbox' || type === 'radio') operation.checked = Boolean(element.checked);
      else if (state.allowedValueIds.has(targetId) || canExposeValue(element)) operation.value = String(element.value || '').slice(0, MAX_VALUE_LENGTH);
    } else if (element.localName === 'select') {
      operation.selectedIndex = Number(element.selectedIndex);
    } else if (element.isContentEditable && state.allowedValueIds.has(targetId)) {
      operation.value = String(element.textContent || '').slice(0, MAX_VALUE_LENGTH);
    } else if (state.allowedValueIds.has(targetId) || canExposeValue(element)) {
      operation.value = String(element.value || '').slice(0, MAX_VALUE_LENGTH);
    }
    return Object.keys(operation).length > 2 ? operation : null;
  }

  function clearDirty() {
    state.replaceRoots.clear();
    state.attributeDirty.clear();
    state.textDirty.clear();
    state.removedDirty.clear();
    state.stateDirty.clear();
    state.scrollDirty.clear();
    state.recordCount = 0;
  }

  function resetPayload(previousSeq, nextSeq) {
    clearDirty();
    state.overflow = false;
    state.seq = nextSeq;
    return stringifyWithSize({
      captureEpoch: state.captureEpoch,
      seq: nextSeq,
      previousSeq,
      url: location.href.slice(0, MAX_TEXT_LENGTH),
      operations: [],
      resetRequired: true,
      rawBytes: 0
    });
  }

  state.drain = function drain() {
    const previousSeq = state.seq;
    const nextSeq = previousSeq + 1;
    if (state.disposed || state.overflow || location.href !== state.url || document.documentElement === null) {
      return resetPayload(previousSeq, nextSeq);
    }

    const operations = [];
    for (const [targetId] of state.removedDirty) operations.push({ op: 'remove', targetId });

    for (const element of state.replaceRoots) {
      const targetId = state.nodeToId.get(element);
      if (!targetId || !element.isConnected) continue;
      const fragmentBudget = { bytes: 0, limit: MAX_FRAGMENT_BYTES, nodes: 0, truncated: false };
      const clone = cloneSanitized(element, fragmentBudget, null);
      if (!clone || fragmentBudget.truncated || state.overflow) return resetPayload(previousSeq, nextSeq);
      const html = clone.outerHTML;
      if (byteLength(html) > MAX_FRAGMENT_BYTES) return resetPayload(previousSeq, nextSeq);
      operations.push({ op: 'replace', targetId, html });
    }

    for (const element of state.attributeDirty) {
      if (!element.isConnected || coveredByReplace(element)) continue;
      const targetId = state.nodeToId.get(element);
      if (!targetId) continue;
      if (shouldOmitElement(element, null)) {
        operations.push({ op: 'remove', targetId });
        continue;
      }
      const attributeBudget = { bytes: 0, limit: MAX_FRAGMENT_BYTES, nodes: 0, truncated: false };
      const clone = shallowSanitizedClone(element, attributeBudget, null);
      if (!clone || attributeBudget.truncated) return resetPayload(previousSeq, nextSeq);
      operations.push({ op: 'attributes', targetId, attributes: attributesObject(clone) });
    }

    for (const [element, text] of state.textDirty) {
      if (!element.isConnected || coveredByReplace(element)) continue;
      const targetId = state.nodeToId.get(element);
      if (targetId) operations.push({ op: 'text', targetId, text });
    }

    for (const element of state.stateDirty) {
      if (!element.isConnected || coveredByReplace(element)) continue;
      const operation = stateOperation(element);
      if (operation) operations.push(operation);
    }

    for (const [element, position] of state.scrollDirty) {
      if (element !== document && (!element.isConnected || coveredByReplace(element))) continue;
      const targetId = element === document ? 'document' : state.nodeToId.get(element);
      if (targetId) operations.push({ op: 'scroll', targetId, x: position.x, y: position.y });
    }

    const payload = {
      captureEpoch: state.captureEpoch,
      seq: nextSeq,
      previousSeq,
      url: location.href.slice(0, MAX_TEXT_LENGTH),
      operations,
      rawBytes: 0
    };
    if (operations.length === 0) {
      clearDirty();
      return stringifyWithSize({
        seq: previousSeq,
        previousSeq,
        url: location.href.slice(0, MAX_TEXT_LENGTH),
        operations: [],
        rawBytes: 0
      });
    }
    let encoded = stringifyWithSize(payload);
    if (byteLength(encoded) > MAX_PATCH_BYTES) return resetPayload(previousSeq, nextSeq);

    state.seq = nextSeq;
    clearDirty();
    for (const [id, node] of Array.from(state.idToNode.entries())) {
      if (!node.isConnected) state.idToNode.delete(id);
    }
    return encoded;
  };

  state.prepareOutbound = prepareOutbound;
  state.readOutbound = function readOutbound(offset, length, release) {
    const chunk = state.outbound.slice(offset, offset + length);
    if (release) state.outbound = '';
    return chunk;
  };
  state.isSensitive = isSensitive;
  state.dispose = function dispose() {
    if (state.disposed) return;
    state.disposed = true;
    if (state.observer) state.observer.disconnect();
    document.removeEventListener('input', onStateEvent, true);
    document.removeEventListener('change', onStateEvent, true);
    document.removeEventListener('scroll', onScrollEvent, true);
    clearDirty();
    state.idToNode.clear();
    state.outbound = '';
  };

  return prepareOutbound(snapshotJson);
})()"""


_DRAIN_MIRROR_TEMPLATE = r"""(() => {
  'use strict';
  const state = window[Symbol.for('unchained.mirror.capture.v1')];
  if (state && typeof state.drain === 'function') {
    const encoded = state.drain();
    return typeof state.prepareOutbound === 'function' ? state.prepareOutbound(encoded) : encoded;
  }

  const payload = {
    captureEpoch: '',
    seq: 0,
    previousSeq: 0,
    url: location.href.slice(0, 65536),
    operations: [],
    resetRequired: true,
    rawBytes: 0
  };
  const bytes = (value) => new TextEncoder().encode(value).length;
  for (let index = 0; index < 4; index += 1) {
    const encoded = JSON.stringify(payload);
    const size = bytes(encoded);
    if (size === payload.rawBytes) return encoded;
    payload.rawBytes = size;
  }
  return JSON.stringify(payload);
})()"""


# ---- Per-connection mirror key builders ---------------------------------------
#
# Each Agent View WS connection receives its own mirror key so that concurrent
# clients (phone + desktop) each get an isolated mirror state in the source tab.
# The key is injected into the JS expressions by string-replacing the default
# key, which appears exactly once in each template.

def _with_mirror_key(template: str, mirror_key: str) -> str:
    key = _validate_mirror_key(mirror_key)
    if key == DEFAULT_MIRROR_KEY:
        return template
    return template.replace(f"'{DEFAULT_MIRROR_KEY}'", f"'{key}'")


def build_install_mirror_expression(mirror_key: str = DEFAULT_MIRROR_KEY) -> str:
    """Return the mirror installation JS for a specific connection's symbol key."""
    return _with_mirror_key(_INSTALL_MIRROR_TEMPLATE, mirror_key)


def build_drain_mirror_expression(mirror_key: str = DEFAULT_MIRROR_KEY) -> str:
    """Return the mirror drain JS for a specific connection's symbol key."""
    return _with_mirror_key(_DRAIN_MIRROR_TEMPLATE, mirror_key)


def build_dispose_mirror_expression(mirror_key: str = DEFAULT_MIRROR_KEY) -> str:
    """Return a JS expression that disposes the mirror for the given key.

    Called when a WS connection closes so the source tab doesn't accumulate
    stale mirrors with live MutationObservers from disconnected clients.
    """
    key = _validate_mirror_key(mirror_key)
    return (
        "(() => { 'use strict';"
        f" const s = window[Symbol.for('{key}')];"
        " if (s && typeof s.dispose === 'function') s.dispose();"
        f" window[Symbol.for('{key}')] = null;"
        " })()"
    )


# Backward-compatible aliases (default key).
INSTALL_MIRROR_EXPRESSION = _INSTALL_MIRROR_TEMPLATE
DRAIN_MIRROR_EXPRESSION = _DRAIN_MIRROR_TEMPLATE


def mirror_payload_chunk_expression(
    offset: int,
    length: int,
    release: bool,
    *,
    mirror_key: str = DEFAULT_MIRROR_KEY,
) -> str:
    """Build the PR #2 outbound-buffer read expression."""
    safe_offset = max(0, int(offset))
    safe_length = max(1, int(length))
    release_js = "true" if release else "false"
    key = _validate_mirror_key(mirror_key)
    return f"""(() => {{
  'use strict';
  const state = window[Symbol.for('{key}')];
  if (!state || typeof state.readOutbound !== 'function') return '';
  return state.readOutbound({safe_offset}, {safe_length}, {release_js});
}})()"""


def mirror_action_expression(
    action: SemanticActionInput,
    *,
    expected_seq: int,
    expected_epoch: str,
    confirmed: bool = False,
    mirror_key: str = DEFAULT_MIRROR_KEY,
) -> str:
    """Build a bounded action expression for the currently mirrored document."""
    encoded_action = json.dumps(
        {**action, "confirmed": bool(confirmed)},
        ensure_ascii=False,
        separators=(",", ":"),
    ).replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    safe_seq = max(0, int(expected_seq))
    encoded_epoch = json.dumps(str(expected_epoch or "")[:128])
    key = _validate_mirror_key(mirror_key)
    return f"""(() => {{
  'use strict';
  const action = {encoded_action};
  const expectedSeq = {safe_seq};
  const expectedEpoch = {encoded_epoch};
  const state = window[Symbol.for('{key}')];
  const startUrl = location.href;
  const finish = (ok, reason, extra) => JSON.stringify(Object.assign({{
    ok,
    reason,
    navigated: location.href !== startUrl || Boolean(state && state.url !== location.href),
    currentSeq: state && Number.isSafeInteger(state.seq) ? state.seq : -1,
    captureEpoch: state && typeof state.captureEpoch === 'string' ? state.captureEpoch : ''
  }}, extra || {{}}));

  if (!state || !(state.idToNode instanceof Map)) return finish(false, 'mirror-not-installed');
  if (state.captureEpoch !== expectedEpoch || expectedSeq > state.seq ||
      (action.kind !== 'scroll' && action.confirmed !== true && expectedSeq !== state.seq)) return finish(false, 'stale-document');
  if (typeof action.targetId !== 'string' || !action.targetId) return finish(false, 'invalid-target');
  const documentTarget = action.kind === 'scroll' && action.targetId === 'document';
  const target = documentTarget ? (document.scrollingElement || document.documentElement || document.body) : state.idToNode.get(action.targetId);
  if (!target || target.nodeType !== 1 || !target.isConnected) return finish(false, 'target-not-found');

  const descriptor = [
    target.getAttribute('type') || '', target.getAttribute('name') || '',
    target.getAttribute('id') || '', target.getAttribute('autocomplete') || '',
    target.getAttribute('placeholder') || '', target.getAttribute('aria-label') || '',
    target.getAttribute('inputmode') || ''
  ].join(' ');
  const compactDescriptor = descriptor.toLowerCase().replace(/[^a-z0-9]/g, '');
  const type = (target.getAttribute('type') || '').toLowerCase();
  const autocomplete = (target.getAttribute('autocomplete') || '').toLowerCase();
  const sensitivePattern = /(?:^|[^a-z0-9])(?:cc-?(?:number|name|csc|cvc|cvv|exp|expiry)|card-?(?:number|holder|csc|cvc|cvv|exp|expiry)|credit-?card|security-?code|one-?time-?(?:code|password)|verification-?code|passcode|otp)(?:$|[^a-z0-9])/i;
  const formField = target.localName === 'input' || target.localName === 'textarea' || target.localName === 'select';
  const sensitive = formField && (
    type === 'password' || type === 'hidden' || type === 'file' || target.hasAttribute('hidden') ||
    autocomplete === 'current-password' || autocomplete === 'new-password' ||
    autocomplete === 'one-time-code' || autocomplete.startsWith('cc-') ||
    sensitivePattern.test(descriptor) ||
    /(?:cardnumber|creditcard|securitycode|onetimecode|verificationcode|passcode|cvc|cvv|csc|otp)/.test(compactDescriptor)
  );
  if (sensitive || (typeof state.isSensitive === 'function' && state.isSensitive(target))) {{
    return finish(false, 'sensitive-target');
  }}

  const value = typeof action.value === 'string' ? action.value.slice(0, 16384) : '';
  const dispatch = (name, EventType, init) => target.dispatchEvent(new EventType(name, init));
  let confirmationControl = target;
  for (let depth = 0; confirmationControl && depth < 32; depth += 1) {{
    const confirmationRole = (confirmationControl.getAttribute('role') || '').toLowerCase();
    const confirmationType = (confirmationControl.getAttribute('type') || '').toLowerCase();
    if (confirmationControl.localName === 'button' || confirmationRole === 'button' ||
        (confirmationControl.localName === 'input' && /^(?:button|submit|reset|image)$/.test(confirmationType))) break;
    const parent = confirmationControl.parentElement;
    if (parent) {{ confirmationControl = parent; continue; }}
    const root = confirmationControl.getRootNode ? confirmationControl.getRootNode() : null;
    confirmationControl = root && root.host && root.host.nodeType === 1 ? root.host : null;
  }}
  const confirmationRole = confirmationControl ? (confirmationControl.getAttribute('role') || '').toLowerCase() : '';
  const confirmationType = confirmationControl ? (confirmationControl.getAttribute('type') || '').toLowerCase() : '';
  const buttonLike = Boolean(confirmationControl && (
    confirmationControl.localName === 'button' || confirmationRole === 'button' ||
    (confirmationControl.localName === 'input' && /^(?:button|submit|reset|image)$/.test(confirmationType))
  ));

  try {{
    if (action.kind === 'click') {{
      if (buttonLike && action.confirmed !== true) return finish(false, 'confirmation-required');
      let rect = target.getBoundingClientRect();
      if ((rect.bottom <= 0 || rect.right <= 0 || rect.top >= window.innerHeight || rect.left >= window.innerWidth) &&
          typeof target.scrollIntoView === 'function') {{
        target.scrollIntoView({{ block: 'nearest', inline: 'nearest', behavior: 'auto' }});
        rect = target.getBoundingClientRect();
      }}
      if (rect.width <= 0 || rect.height <= 0) return finish(false, 'target-not-visible');
      const fx = Number.isFinite(action.fx) ? Math.max(0, Math.min(1, action.fx)) : 0.5;
      const fy = Number.isFinite(action.fy) ? Math.max(0, Math.min(1, action.fy)) : 0.5;
      const clickX = Math.max(0, Math.min(Math.max(0, window.innerWidth - 1), Math.round(rect.left + rect.width * fx)));
      const clickY = Math.max(0, Math.min(Math.max(0, window.innerHeight - 1), Math.round(rect.top + rect.height * fy)));
      return finish(false, 'cdp-click-required', {{ targetId: action.targetId, actionKind: 'click', x: clickX, y: clickY }});
    }} else if (action.kind === 'input' || action.kind === 'change') {{
      if (!formField && !target.isContentEditable) return finish(false, 'target-not-editable');
      if (target.localName === 'input' && (type === 'checkbox' || type === 'radio')) {{
        if (typeof action.checked !== 'boolean') return finish(false, 'checked-required');
        target.checked = action.checked;
      }} else if (target.isContentEditable) {{
        target.textContent = value;
      }} else {{
        target.value = value;
      }}
      state.allowedValueIds.add(action.targetId);
      if (action.kind === 'input') {{
        try {{
          dispatch('input', InputEvent, {{ bubbles: true, composed: true, inputType: 'insertText', data: null }});
        }} catch (_error) {{
          dispatch('input', Event, {{ bubbles: true, composed: true }});
        }}
      }} else {{
        dispatch('change', Event, {{ bubbles: true, composed: true }});
      }}
    }} else if (action.kind === 'key') {{
      if (action.key !== 'Enter') return finish(false, 'unsupported-key');
      if (action.confirmed !== true && (target.form || buttonLike)) return finish(false, 'confirmation-required');
      const init = {{ key: 'Enter', code: 'Enter', bubbles: true, cancelable: true, composed: true }};
      const proceed = dispatch('keydown', KeyboardEvent, init);
      dispatch('keypress', KeyboardEvent, init);
      dispatch('keyup', KeyboardEvent, init);
      if (proceed && target.localName === 'input' && target.form && typeof target.form.requestSubmit === 'function') {{
        target.form.requestSubmit();
      }} else if (proceed && /^(?:a|button)$/.test(target.localName) && typeof target.click === 'function') {{
        target.click();
      }}
    }} else if (action.kind === 'scroll') {{
      const x = Number.isFinite(action.x) ? action.x : 0;
      const y = Number.isFinite(action.y) ? action.y : 0;
      if (documentTarget || target === document.body || target === document.documentElement) {{
        window.scrollTo({{ left: x, top: y, behavior: 'auto' }});
      }} else if (typeof target.scrollTo === 'function') {{
        target.scrollTo({{ left: x, top: y, behavior: 'auto' }});
      }} else {{
        target.scrollLeft = x;
        target.scrollTop = y;
      }}
      const actualX = documentTarget ? Math.round(window.scrollX || 0) : Math.round(target.scrollLeft || 0);
      const actualY = documentTarget ? Math.round(window.scrollY || 0) : Math.round(target.scrollTop || 0);
      return finish(true, 'ok', {{ targetId: action.targetId, actionKind: 'scroll', x: actualX, y: actualY }});
    }} else {{
      return finish(false, 'unsupported-action');
    }}
    return finish(true, 'ok');
  }} catch (error) {{
    return finish(false, error && error.name ? 'exception:' + error.name : 'exception');
  }}
}})()"""


def parse_evaluation(raw: str) -> Any:
    """Parse a direct JSON result or the extra JSON-string layer some relays add."""
    if not isinstance(raw, str):
        raise TypeError(f"mirror evaluation result must be str, got {type(raw).__name__}")
    value: Any = json.loads(raw)
    if isinstance(value, str):
        value = json.loads(value)
    return value


def _is_chunk_manifest(value: Any) -> bool:
    if not isinstance(value, dict) or "__unchainedMirrorChunks" not in value:
        return False
    marker = value.get("__unchainedMirrorChunks")
    length = value.get("length")
    if (
        isinstance(marker, bool)
        or not isinstance(marker, int)
        or marker != 1
        or isinstance(length, bool)
        or not isinstance(length, int)
        or length <= 0
        or length > MAX_MIRROR_PAYLOAD_CHARS
    ):
        raise ValueError("invalid or oversized semantic mirror chunk manifest")
    return True


def _decode_chunk(raw: str, expected_length: int) -> str:
    if not isinstance(raw, str):
        raise TypeError(f"mirror payload chunk must be str, got {type(raw).__name__}")

    candidate = raw
    for _ in range(3):
        if _js_char_length(candidate) == expected_length:
            return candidate
        if len(candidate) < 2 or candidate[0] != '"' or candidate[-1] != '"':
            break
        try:
            decoded = json.loads(candidate)
        except json.JSONDecodeError:
            break
        if not isinstance(decoded, str):
            break
        candidate = decoded
    raise ValueError(
        f"mirror payload chunk length mismatch: expected {expected_length}"
    )


def _js_char_length(value: str) -> int:
    return len(value.encode("utf-16-le", errors="surrogatepass")) // 2


async def _evaluate_mirror_payload_unlocked(
    agent_id: str,
    tab_id: str,
    expression: str,
    relay_host: str = "127.0.0.1",
    relay_port: int = 8765,
    *,
    mirror_key: str = DEFAULT_MIRROR_KEY,
) -> dict[str, Any]:
    """Evaluate one capture expression, reconstructing a bounded chunked result."""
    raw = await cloud_tools.run_js(agent_id, tab_id, expression, relay_host, relay_port)
    initial = parse_evaluation(raw)
    if not _is_chunk_manifest(initial):
        if not isinstance(initial, dict):
            raise ValueError("semantic mirror payload must be a JSON object")
        return initial

    expected_length = initial["length"]
    chunks: list[str] = []
    for offset in range(0, expected_length, MIRROR_CHUNK_CHARS):
        length = min(MIRROR_CHUNK_CHARS, expected_length - offset)
        release = offset + length == expected_length
        raw_chunk = await cloud_tools.run_js(
            agent_id,
            tab_id,
            mirror_payload_chunk_expression(offset, length, release, mirror_key=mirror_key),
            relay_host,
            relay_port,
        )
        chunk = _decode_chunk(raw_chunk, length)
        chunks.append(chunk)

    encoded = "".join(chunks)
    if _js_char_length(encoded) != expected_length:
        raise ValueError("mirror payload length mismatch")
    # Normalize a surrogate pair if a JavaScript slice boundary split it.
    encoded = encoded.encode("utf-16-le", errors="surrogatepass").decode("utf-16-le")
    payload = parse_evaluation(encoded)
    if not isinstance(payload, dict):
        raise ValueError("semantic mirror payload must be a JSON object")
    return payload


async def evaluate_mirror_payload(
    agent_id: str,
    tab_id: str,
    expression: str,
    relay_host: str = "127.0.0.1",
    relay_port: int = 8765,
    *,
    operation_lock: asyncio.Lock | None = None,
    mirror_key: str = DEFAULT_MIRROR_KEY,
) -> dict[str, Any]:
    """Evaluate one capture expression under the optional per-source lock."""
    if operation_lock is None:
        return await _evaluate_mirror_payload_unlocked(
            agent_id, tab_id, expression, relay_host, relay_port,
            mirror_key=mirror_key,
        )
    async with operation_lock:
        return await _evaluate_mirror_payload_unlocked(
            agent_id, tab_id, expression, relay_host, relay_port,
            mirror_key=mirror_key,
        )


async def execute_semantic_action(
    agent_id: str,
    tab_id: str,
    action: SemanticActionInput,
    *,
    expected_seq: int,
    expected_epoch: str,
    confirmed: bool = False,
    relay_host: str = "127.0.0.1",
    relay_port: int = 8765,
    operation_lock: asyncio.Lock | None = None,
    mirror_key: str = DEFAULT_MIRROR_KEY,
) -> dict[str, Any]:
    """Execute one server-authorized semantic action on the mirrored source."""
    expression = mirror_action_expression(
        action,
        expected_seq=expected_seq,
        expected_epoch=expected_epoch,
        confirmed=confirmed,
        mirror_key=mirror_key,
    )

    async def run() -> dict[str, Any]:
        raw = await cloud_tools.run_js(
            agent_id, tab_id, expression, relay_host, relay_port
        )
        result = parse_evaluation(raw)
        if not isinstance(result, dict):
            raise ValueError("semantic action result must be a JSON object")
        if result.get("reason") == "cdp-click-required":
            x = result.get("x")
            y = result.get("y")
            if isinstance(x, bool) or not isinstance(x, (int, float)) or isinstance(y, bool) or not isinstance(y, (int, float)):
                raise ValueError("semantic click did not return valid source coordinates")
            await cloud_tools.click(
                agent_id,
                tab_id,
                int(x),
                int(y),
                relay_host,
                relay_port,
            )
            result.update(ok=True, reason="ok", actionKind="click")
        return result

    if operation_lock is None:
        return await run()
    async with operation_lock:
        return await run()


async def stream_semantic_mirror(
    agent_id: str,
    tab_id: str,
    *,
    relay_host: str = "127.0.0.1",
    relay_port: int = 8765,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    stop_requested: Callable[[], bool] | None = None,
    operation_lock: asyncio.Lock | None = None,
    mirror_key: str = DEFAULT_MIRROR_KEY,
) -> AsyncIterator[SemanticMirrorEvent]:
    """Yield an initial snapshot and non-empty patches from an attached tab.

    The caller owns this async iterator's lifetime. Cancelling or closing it
    stops polling; this module never launches, navigates, or otherwise controls
    Chrome.

    ``mirror_key`` isolates this stream's capture state from other concurrent
    streams on the same tab (e.g. phone + desktop Agent View). Each key maps to
    a distinct ``Symbol.for(...)`` in the source Chrome tab.
    """
    if poll_interval <= 0:
        raise ValueError("poll_interval must be greater than zero")

    install_expr = build_install_mirror_expression(mirror_key)
    drain_expr = build_drain_mirror_expression(mirror_key)

    snapshot = await evaluate_mirror_payload(
        agent_id,
        tab_id,
        install_expr,
        relay_host,
        relay_port,
        operation_lock=operation_lock,
        mirror_key=mirror_key,
    )
    yield {"type": "snapshot", "snapshot": snapshot, "resync": False}

    empty_polls = 0
    capture_errors = 0
    while True:
        if stop_requested and stop_requested():
            return
        delay = min(2.0, poll_interval * 3) if empty_polls > 8 else poll_interval
        await asyncio.sleep(delay)
        if stop_requested and stop_requested():
            return
        try:
            patch = await evaluate_mirror_payload(
                agent_id,
                tab_id,
                drain_expr,
                relay_host,
                relay_port,
                operation_lock=operation_lock,
                mirror_key=mirror_key,
            )
            capture_errors = 0
        except asyncio.CancelledError:
            raise
        except Exception:
            capture_errors += 1
            empty_polls = 0
            if capture_errors >= MAX_CONSECUTIVE_CAPTURE_ERRORS:
                raise
            continue
        if patch.get("resetRequired"):
            snapshot = await evaluate_mirror_payload(
                agent_id,
                tab_id,
                install_expr,
                relay_host,
                relay_port,
                operation_lock=operation_lock,
                mirror_key=mirror_key,
            )
            empty_polls = 0
            yield {"type": "snapshot", "snapshot": snapshot, "resync": True}
            continue

        operations = patch.get("operations")
        if not isinstance(operations, list):
            raise ValueError("semantic mirror patch operations must be a list")
        if not operations:
            empty_polls += 1
            continue

        empty_polls = 0
        yield {"type": "patch", "patch": patch}
