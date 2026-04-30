// Page Loader — orchestrates full page loading pipeline
// Fetches page, seeds virtual DOM, executes scripts, syncs to iframe

import { WasmEngine } from './engine.js?v=4';
import { DOM_SOURCE } from './dom.js?v=4';
import { SHIMS_SOURCE } from './shims.js?v=4';
import { DOMBridge } from './bridge.js?v=4';
import { extract, extractText, fingerprint, detectForms } from './intel.js?v=4';

// Layout-free DOM text walker — no innerText, no layout cost.
// Inserts \t between table cells and \n after block elements so
// screener/table pages don't have their columns concatenated.
const _BLOCK = new Set([
  'P','DIV','SECTION','ARTICLE','ASIDE','HEADER','FOOTER','NAV','MAIN',
  'H1','H2','H3','H4','H5','H6','LI','DT','DD','BLOCKQUOTE','PRE',
  'TR','CAPTION','FIGURE','FIGCAPTION','DETAILS','SUMMARY',
]);
const _SKIP = new Set(['SCRIPT','STYLE','NOSCRIPT','TEMPLATE','SVG']);

function _walk(node, parts) {
  if (node.nodeType === 3) { if (node.nodeValue) parts.push(node.nodeValue); return; }
  if (node.nodeType !== 1) return;
  const tag = node.tagName;
  if (_SKIP.has(tag)) return;
  if (tag === 'TD' || tag === 'TH') { parts.push('\t'); for (const c of node.childNodes) _walk(c, parts); return; }
  for (const c of node.childNodes) _walk(c, parts);
  if (_BLOCK.has(tag)) parts.push('\n');
}

function extractTextFast(root) {
  const parts = [];
  _walk(root, parts);
  return parts.join('').replace(/[ \t]+/g, ' ').replace(/\n[ \t]+/g, '\n').replace(/\n{3,}/g, '\n\n').trim();
}

export class PageLoader {
  constructor(iframe) {
    this.iframe = iframe;
    this.engine = null;
    this.bridge = null;
    this.currentUrl = '';
    this._onLog = null;
  }

  // Set a callback for log messages
  onLog(fn) {
    this._onLog = fn;
  }

  _log(msg) {
    if (this._onLog) this._onLog(msg);
    else console.log('[Loader]', msg);
  }

  // Build a fresh WASM engine + bridge (shared by init() and navigate reset)
  async _buildEngine() {
    this.engine = new WasmEngine();
    await this.engine.init();
    this.engine.eval(DOM_SOURCE);
    this.engine.eval(SHIMS_SOURCE);
    this.bridge = new DOMBridge(this.iframe, this.engine);
    this.bridge.init();
  }

  // Initialize the WASM engine and bridge
  async init() {
    this._log('Initializing WASM engine...');
    await this._buildEngine();
    this._log('Engine ready.');
    return this;
  }

  // Load a page: fetch, parse, seed DOM, execute scripts, sync to iframe
  async navigate(url) {
    if (!this.engine) {
      await this.init();
    } else {
      // Fresh VM per page — prevents listener/timer/global/heap accumulation
      // across navigations. Cheap after first load (QuickJS WASM is cached).
      try { this.engine.dispose(); } catch {}
      await this._buildEngine();
    }

    this.currentUrl = url;
    this._log(`Loading ${url}...`);

    // Set location in QuickJS
    this.engine.eval(`__setLocation(${JSON.stringify(url)})`);

    // Fetch page via server
    const response = await fetch('/proxy/load', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    });
    const pageData = await response.json();
    const { html, scripts, baseUrl, error, serverText, links: serverLinks, blocked, block_type } = pageData;
    this._serverText = serverText || '';
    this._serverLinks = serverLinks || [];
    this._blocked = !!blocked;
    this._blockType = block_type || null;

    if (error) {
      this._log(`Error: ${error}`);
      return;
    }

    // Server-detected bot wall (Datadome, Cloudflare, Akamai, PerimeterX, …).
    // The server already rewrote the response into a clean agent-readable
    // message; don't waste work parsing scripts / seeding the DOM on what is
    // essentially a CAPTCHA shell. The iframe still renders the stub body so
    // the user sees why we bailed.
    if (blocked) {
      this._log(`Blocked by ${block_type} — ${(serverText || '').substring(0, 80)}`);
      this.iframe.srcdoc = html;
      await new Promise(r => { this.iframe.onload = r; });
      return;
    }

    this._log(`Got HTML (${html.length} chars) + ${scripts.length} scripts`);

    // 1. Render the script-stripped HTML in the iframe for immediate visual.
    // Cap the wait at 3s — onload fires only after ALL subresources (images,
    // stylesheets) load, which can take 20+ seconds on image-heavy news sites
    // like Engadget since each asset goes through the proxy. The agent uses
    // serverText, not the visual render, so we don't need to wait for images.
    this.iframe.srcdoc = html;
    await Promise.race([
      new Promise(r => { this.iframe.onload = r; }),
      new Promise(r => setTimeout(r, 3000)),
    ]);

    // 2. Parse the HTML into a JSON tree using the browser's DOMParser
    const jsonTree = this._htmlToJson(html);

    // 3. Seed the QuickJS virtual DOM from the JSON tree
    this.engine.eval(`__seedDOM(${JSON.stringify(jsonTree)})`);
    // Clear any mutations from seeding (we already rendered the base HTML)
    this.engine.eval('__getMutations()');

    // 4. Re-init bridge with the seeded iframe
    this.bridge.nodeMap.clear();
    this._mapIframeNodes();

    this._log('DOM seeded. Executing scripts...');

    // 5. Execute scripts in order (limit to prevent UI lockup)
    const MAX_SCRIPTS = 10; // Prevent heavy SPAs from freezing the UI
    const MAX_SCRIPT_SIZE = 500000; // Skip scripts > 500KB (bundled app code)
    let scriptCount = 0;
    let scriptErrors = 0;

    // Separate inline scripts (must run in order) from external scripts.
    // Pre-fetch all external scripts in parallel, then execute everything
    // in original order. This avoids N sequential round-trips for pages
    // like slickdeals.com that load many external JS files.
    const capped = scripts.slice(0, MAX_SCRIPTS);
    const externalScripts = capped.filter(s => s.type === 'external');

    const fetchedMap = new Map(); // src → body string
    await Promise.all(externalScripts.map(async (script) => {
      try {
        const scriptRes = await fetch('/proxy/fetch', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ url: script.src, method: 'GET', headers: '{}', body: '' }),
        });
        const scriptData = await scriptRes.json();
        if (scriptData.body && scriptData.status === 200 && scriptData.body.length < MAX_SCRIPT_SIZE && scriptData.body.length > 0) {
          fetchedMap.set(script.src, scriptData.body);
        }
      } catch {}
    }));

    for (const script of capped) {
      try {
        if (script.type === 'inline') {
          if (script.content.length > MAX_SCRIPT_SIZE) continue;
          this.engine.eval(script.content);
          scriptCount++;
        } else if (script.type === 'external') {
          const body = fetchedMap.get(script.src);
          if (!body) continue;
          this.engine.eval(body);
          scriptCount++;
        }
      } catch (e) {
        scriptErrors++;
        // Log to console only — don't show script errors to consumers
        console.warn(`[loader] Script error: ${e.message.substring(0, 100)}`);
      }

      // Process any pending timers/promises after each script
      try {
        this.engine.vm.runtime.executePendingJobs();
        this.engine.tick();
      } catch {}
    }

    if (scripts.length > MAX_SCRIPTS) {
      this._log(`Skipped ${scripts.length - MAX_SCRIPTS} remaining scripts (limit ${MAX_SCRIPTS})`);
    }

    this._log(`Executed ${scriptCount}/${scripts.length} scripts`);

    // 6. Fire DOMContentLoaded and load events
    try {
      this.engine.eval('__fireDOMContentLoaded()');
      this.engine.vm.runtime.executePendingJobs();
      this.engine.tick();

      this.engine.eval('__fireLoad()');
      this.engine.vm.runtime.executePendingJobs();
      this.engine.tick();
    } catch (e) {
      console.warn(`[loader] Event error: ${e.message.substring(0, 100)}`);
    }

    // 7. Sync any DOM mutations from JS execution to the iframe
    this.bridge.flush();
    if (this.bridge.attachEventForwarding) this.bridge.attachEventForwarding();

    this._log('Page loaded.');
    return this;
  }

  // Static-snapshot loader: skip /proxy/load entirely. Caller supplies a
  // pre-bundled `{html, scripts}` payload — same shape /proxy/load returns,
  // so the rest of the pipeline (DOM seed → script execute → bridge sync) is
  // identical. Used for the marketing-page WASM browser demo where there is
  // no backend.
  async loadStatic(snapshot) {
    if (!this.engine) {
      await this.init();
    } else {
      try { this.engine.dispose(); } catch {}
      await this._buildEngine();
    }

    const { html, baseUrl = 'about:demo' } = snapshot || {};
    let { scripts } = snapshot || {};
    if (!html) throw new Error('loadStatic: snapshot.html required');

    // Auto-extract inline + external scripts from the HTML if the caller
    // didn't supply them — otherwise the rest of the pipeline strips <script>
    // tags during _htmlToJson() and the page never executes any logic.
    if (!Array.isArray(scripts)) {
      scripts = this._extractScriptsFromHtml(html);
    }

    this.currentUrl = baseUrl;
    this.engine.eval(`__setLocation(${JSON.stringify(baseUrl)})`);

    // Render the script-stripped HTML in the iframe for immediate visual.
    this.iframe.srcdoc = html;
    await Promise.race([
      new Promise(r => { this.iframe.onload = r; }),
      new Promise(r => setTimeout(r, 1500)),
    ]);

    // Seed the QuickJS virtual DOM from the same HTML.
    const jsonTree = this._htmlToJson(html);
    this.engine.eval(`__seedDOM(${JSON.stringify(jsonTree)})`);
    this.engine.eval('__getMutations()');

    // Re-init the bridge against the freshly-rendered iframe so its node map
    // points to the real iframe nodes (not stale ones from a previous load).
    this.bridge.nodeMap.clear();
    this.bridge.idMap = new WeakMap();
    this._mapIframeNodes();

    this._log(`Snapshot seeded — ${scripts.length} scripts, executing…`);

    let scriptCount = 0;
    let scriptErrors = 0;
    for (const script of scripts) {
      try {
        if (script.type === 'inline' || (script.content && !script.src)) {
          this.engine.eval(script.content);
          scriptCount++;
        } else if (script.body) {
          this.engine.eval(script.body);
          scriptCount++;
        }
      } catch (e) {
        scriptErrors++;
        console.warn(`[loader] static script error: ${e.message.substring(0, 120)}`);
      }
      try {
        this.engine.vm.runtime.executePendingJobs();
        this.engine.tick();
      } catch {}
    }

    try {
      this.engine.eval('__fireDOMContentLoaded()');
      this.engine.vm.runtime.executePendingJobs(); this.engine.tick();
      this.engine.eval('__fireLoad()');
      this.engine.vm.runtime.executePendingJobs(); this.engine.tick();
    } catch (e) {
      console.warn(`[loader] static event error: ${e.message.substring(0, 120)}`);
    }

    this.bridge.flush();
    if (this.bridge.attachEventForwarding) this.bridge.attachEventForwarding();

    this._log(`Static load done — ${scriptCount}/${scripts.length} scripts executed, ${scriptErrors} errors`);
    return this;
  }

  // Extract <script> blocks from a snapshot HTML string. Returns a list of
  // {type:'inline', content} or {type:'external', src} the same shape the
  // /proxy/load endpoint produces, so the existing executor reuses verbatim.
  _extractScriptsFromHtml(html) {
    const parser = new DOMParser();
    const doc = parser.parseFromString(html, 'text/html');
    const out = [];
    doc.querySelectorAll('script').forEach(s => {
      if (s.src) {
        out.push({ type: 'external', src: s.src });
      } else if (s.textContent && s.textContent.trim()) {
        out.push({ type: 'inline', content: s.textContent });
      }
    });
    return out;
  }

  // Parse HTML string into JSON tree using browser's DOMParser
  _htmlToJson(html) {
    const parser = new DOMParser();
    const doc = parser.parseFromString(html, 'text/html');
    return this._serializeNode(doc.documentElement);
  }

  _serializeNode(node) {
    if (node.nodeType === 3) {
      // Text node
      const text = node.textContent;
      if (!text.trim()) return null; // Skip whitespace-only text nodes
      return { type: 'text', content: text };
    }
    if (node.nodeType !== 1) return null; // Skip comments, etc

    const tag = node.tagName.toLowerCase();

    // Skip script tags (already extracted)
    if (tag === 'script') return null;

    const attrs = {};
    for (const attr of node.attributes) {
      // Skip proxy-rewritten URLs in src/href — keep original structure
      attrs[attr.name] = attr.value;
    }

    const children = [];
    for (const child of node.childNodes) {
      const serialized = this._serializeNode(child);
      if (serialized) children.push(serialized);
    }

    return { type: 'element', tag, attrs, children };
  }

  // Map iframe's real DOM nodes to bridge nodeMap by walking in parallel with virtual DOM
  _mapIframeNodes() {
    const doc = this.iframe.contentDocument;
    if (!doc) return;

    // Get virtual DOM IDs
    const vdomTree = this.engine.eval('__serializeDOM()');
    this._mapNode(doc.documentElement, vdomTree);
  }

  _mapNode(realNode, vNode) {
    if (!realNode || !vNode) return;

    if (this.bridge._trackNode) {
      this.bridge._trackNode(vNode.id, realNode);
    } else {
      this.bridge.nodeMap.set(vNode.id, realNode);
    }

    if (vNode.children) {
      // Match real children with virtual children
      const realChildren = [...realNode.childNodes].filter(n =>
        n.nodeType === 1 || (n.nodeType === 3 && n.textContent.trim())
      );

      for (let i = 0; i < Math.min(realChildren.length, vNode.children.length); i++) {
        this._mapNode(realChildren[i], vNode.children[i]);
      }
    }
  }

  // Query the virtual DOM (for agent use)
  querySelector(selector) {
    try {
      return this.engine.eval(`
        (function() {
          var el = document.querySelector(${JSON.stringify(selector)});
          if (!el) return null;
          return { tag: el.tagName, text: el.textContent, id: el.id, className: el.className };
        })()
      `);
    } catch { return null; }
  }

  querySelectorAll(selector) {
    try {
      return this.engine.eval(`
        (function() {
          var els = document.querySelectorAll(${JSON.stringify(selector)});
          return Array.prototype.map.call(els, function(el) {
            return { tag: el.tagName, text: el.textContent.substring(0, 200), id: el.id, className: el.className };
          });
        })()
      `);
    } catch { return []; }
  }

  getPageText() {
    try {
      return this.engine.eval('document.body.textContent');
    } catch { return ''; }
  }

  // Intel extraction — uses unchainedsky extraction strategies
  async intelExtract() {
    if (!this.engine) return { strategy: 'none', data: '' };
    return extract(this.engine);
  }

  async intelFingerprint() {
    if (!this.engine) return null;
    return fingerprint(this.engine);
  }

  async intelForms() {
    if (!this.engine) return [];
    return detectForms(this.engine);
  }

  getServerText() {
    return this._serverText || '';
  }

  getServerLinks() {
    return this._serverLinks || [];
  }

  // Bot-wall status from the last navigate() — source of truth for whether
  // the page is a CAPTCHA/challenge shell. When true, agent.js should skip
  // its own (weaker) pattern-based block detection and just surface the
  // server's serverText message directly as the tool result.
  isBlocked() {
    return !!this._blocked;
  }

  getBlockType() {
    return this._blockType || null;
  }

  async intelText(keyword) {
    if (!this.engine) return '';
    return extractText(this.engine, keyword);
  }

  // DDM — runs against the iframe's real DOM (has layout, positions, visibility)
  async ddm(opts = {}) {
    const doc = this.iframe.contentDocument;
    if (!doc) return null;

    // Load DDM script from server
    if (!this._ddmScript) {
      try {
        const res = await fetch('/api/intel/scripts');
        if (res.ok) {
          const scripts = await res.json();
          this._ddmScript = scripts.DOM_WALKER_JS;
        }
      } catch {}
    }

    if (!this._ddmScript) return null;

    try {
      // Inject and run DOM_WALKER_JS in the iframe context
      const fn = new Function('document', 'window', this._ddmScript);
      const win = this.iframe.contentWindow;
      const result = fn.call(win, doc, win);
      return result;
    } catch (e) {
      console.warn('[DDM] error:', e.message);
      return null;
    }
  }

  // DDM text extraction — runs against iframe DOM
  async ddmText(keyword) {
    const doc = this.iframe.contentDocument;
    if (!doc) return '';

    if (keyword) {
      // Load text find script
      if (!this._textFindScript) {
        try {
          const res = await fetch('/api/intel/scripts');
          if (res.ok) {
            const scripts = await res.json();
            this._textFindScript = scripts.TEXT_FIND_JS;
          }
        } catch {}
      }

      if (this._textFindScript) {
        try {
          const js = this._textFindScript.replace(/__KEYWORD__/g, keyword);
          const fn = new Function('document', 'window', js);
          return fn.call(this.iframe.contentWindow, doc, this.iframe.contentWindow);
        } catch {}
      }
    }

    // Fallback: layout-free text walk (avoids innerText layout cost).
    try {
      return doc.body ? extractTextFast(doc.body) : '';
    } catch { return ''; }
  }

  // Forms detection — runs against iframe DOM
  async ddmForms() {
    const doc = this.iframe.contentDocument;
    if (!doc) return [];

    if (!this._formsScript) {
      try {
        const res = await fetch('/api/intel/scripts');
        if (res.ok) {
          const scripts = await res.json();
          this._formsScript = scripts.FORMS_JS;
        }
      } catch {}
    }

    if (!this._formsScript) return [];

    try {
      const fn = new Function('document', 'window', this._formsScript);
      return fn.call(this.iframe.contentWindow, doc, this.iframe.contentWindow);
    } catch (e) {
      console.warn('[DDM] forms error:', e.message);
      return [];
    }
  }

  getTitle() {
    try {
      return this.engine.eval('document.title');
    } catch { return ''; }
  }

  dispose() {
    if (this.bridge) { this.bridge.dispose(); this.bridge = null; }
    if (this.engine) { this.engine.dispose(); this.engine = null; }
  }
}
