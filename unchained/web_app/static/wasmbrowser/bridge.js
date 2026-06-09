// DOM Sync Bridge — replays QuickJS virtual DOM mutations into an iframe
// Runs in the HOST browser (not inside QuickJS)

export class DOMBridge {
  constructor(iframe, engine) {
    this.iframe = iframe;
    this.engine = engine;
    this.nodeMap = new Map(); // vnode _id → real DOM node
    this.idMap = new WeakMap(); // real DOM node → vnode _id (reverse lookup)
    this._rafId = null;
    this._running = false;
    this._eventListenersAttached = false;
  }

  _trackNode(id, node) {
    if (id == null || !node) return;
    this.nodeMap.set(id, node);
    try { this.idMap.set(node, id); } catch {}
  }

  // Walk up from a real DOM target to find the closest virtual id.
  _findVirtId(target) {
    var n = target;
    while (n) {
      var id = this.idMap.get(n);
      if (id != null) return id;
      n = n.parentNode;
    }
    return null;
  }

  // Attach delegate listeners on the iframe document so user clicks / typing
  // fire dispatchEvent inside the QuickJS sandbox. Without this the bridge is
  // one-way (WASM → iframe) and the visitor can't actually interact.
  attachEventForwarding() {
    if (this._eventListenersAttached) return;
    var doc = this.iframe.contentDocument;
    if (!doc) return;
    var bridge = this;

    function forward(eventType, buildInit) {
      return function(e) {
        var id = bridge._findVirtId(e.target);
        if (id == null) return;
        var init = typeof buildInit === 'function' ? buildInit(e) : (buildInit || {});
        try {
          bridge.engine.eval(
            '__dispatchById(' + id + ', ' + JSON.stringify(eventType) +
            ', ' + JSON.stringify(init) + ')'
          );
          // Drain microtasks/timers and sync mutations immediately so the
          // visitor sees the result on the same paint, not the next frame.
          try {
            bridge.engine.vm.runtime.executePendingJobs();
            bridge.engine.tick && bridge.engine.tick();
          } catch {}
          bridge.flush();
        } catch (err) {
          console.warn('[Bridge] event forward error:', err && err.message);
        }
      };
    }

    doc.addEventListener('click', forward('click', function(e) {
      return { bubbles: true, cancelable: true, button: e.button };
    }), true);
    doc.addEventListener('input', forward('input', function(e) {
      return { bubbles: true, value: e.target ? e.target.value : '' };
    }), true);
    doc.addEventListener('change', forward('change', function(e) {
      return {
        bubbles: true,
        value: e.target ? e.target.value : '',
        checked: e.target ? !!e.target.checked : false,
      };
    }), true);
    doc.addEventListener('submit', function(e) {
      e.preventDefault();
      forward('submit', { bubbles: true, cancelable: true })(e);
    }, true);
    doc.addEventListener('keydown', forward('keydown', function(e) {
      return {
        bubbles: true,
        key: e.key,
        value: e.target ? e.target.value : ''
      };
    }), true);

    this._eventListenersAttached = true;
  }

  // Initialize the iframe with a blank document and map root nodes
  init() {
    const doc = this.iframe.contentDocument;
    if (!doc) return;

    // Clear iframe
    doc.open();
    doc.write('<!DOCTYPE html><html><head></head><body></body></html>');
    doc.close();

    // Map the root nodes (IDs 1=html, 2=head, 3=body from QuickJS dom.js)
    // These IDs are assigned in order: html=1, head=2, body=3
    this._trackNode(1, doc.documentElement);
    this._trackNode(2, doc.head);
    this._trackNode(3, doc.body);
  }

  // Seed the iframe from a full DOM serialization
  seedFromSerialized(tree) {
    const doc = this.iframe.contentDocument;
    if (!doc) return;

    doc.open();
    doc.write('<!DOCTYPE html><html><head></head><body></body></html>');
    doc.close();

    this.nodeMap.clear();
    this.idMap = new WeakMap();
    this._trackNode(tree.id, doc.documentElement);

    // Process children (head and body)
    if (tree.children) {
      for (const child of tree.children) {
        if (child.tag === 'head') {
          this._trackNode(child.id, doc.head);
          this._buildRealNodes(doc.head, child.children || [], doc);
        } else if (child.tag === 'body') {
          this._trackNode(child.id, doc.body);
          if (child.attrs) {
            for (const [k, v] of Object.entries(child.attrs)) {
              doc.body.setAttribute(k, v);
            }
          }
          this._buildRealNodes(doc.body, child.children || [], doc);
        }
      }
    }
  }

  _buildRealNodes(parent, children, doc) {
    for (const child of children) {
      if (child.type === 'text') {
        const textNode = doc.createTextNode(child.content || '');
        this._trackNode(child.id, textNode);
        parent.appendChild(textNode);
      } else if (child.type === 'element') {
        const el = doc.createElement(child.tag);
        this._trackNode(child.id, el);
        if (child.attrs) {
          for (const [k, v] of Object.entries(child.attrs)) {
            try { el.setAttribute(k, v); } catch {}
          }
        }
        if (child.children) {
          this._buildRealNodes(el, child.children, doc);
        }
        parent.appendChild(el);
      }
    }
  }

  // Start the sync loop — drains mutations every frame
  start() {
    if (this._running) return;
    this._running = true;
    this._loop();
  }

  stop() {
    this._running = false;
    if (this._rafId) {
      cancelAnimationFrame(this._rafId);
      this._rafId = null;
    }
  }

  _loop() {
    if (!this._running) return;
    this.flush();
    this._rafId = requestAnimationFrame(() => this._loop());
  }

  // Drain all pending mutations from QuickJS and apply to iframe
  flush() {
    if (!this.engine || !this.engine.vm) return;

    let mutations;
    try {
      mutations = this.engine.eval('__getMutations()');
    } catch {
      return;
    }

    if (!mutations || !mutations.length) return;

    const doc = this.iframe.contentDocument;
    if (!doc) return;

    for (const m of mutations) {
      try {
        this._applyMutation(m, doc);
      } catch (e) {
        console.warn('[Bridge] mutation error:', m.type, e.message);
      }
    }
  }

  _applyMutation(m, doc) {
    switch (m.type) {
      case 'appendChild': {
        const parent = this.nodeMap.get(m.parentId);
        if (!parent) break;
        const child = this._ensureNode(m.childId, m.childDef, doc);
        if (child) parent.appendChild(child);
        break;
      }

      case 'removeChild': {
        const parent = this.nodeMap.get(m.parentId);
        const child = this.nodeMap.get(m.childId);
        if (parent && child && child.parentNode === parent) {
          parent.removeChild(child);
        }
        break;
      }

      case 'insertBefore': {
        const parent = this.nodeMap.get(m.parentId);
        const ref = this.nodeMap.get(m.refId);
        if (!parent) break;
        const child = this._ensureNode(m.childId, m.childDef, doc);
        if (child) parent.insertBefore(child, ref || null);
        break;
      }

      case 'setAttribute': {
        const el = this.nodeMap.get(m.id);
        if (el && el.setAttribute) {
          try { el.setAttribute(m.name, m.value); } catch {}
        }
        break;
      }

      case 'removeAttribute': {
        const el = this.nodeMap.get(m.id);
        if (el && el.removeAttribute) {
          el.removeAttribute(m.name);
        }
        break;
      }

      case 'setTextContent': {
        const node = this.nodeMap.get(m.id);
        if (node) node.textContent = m.value;
        break;
      }

      case 'setInnerHTML': {
        const el = this.nodeMap.get(m.id);
        if (el) el.innerHTML = m.html;
        break;
      }

      case 'setStyle': {
        const el = this.nodeMap.get(m.id);
        if (!el || !el.style) break;
        if (m.prop === '__cssText') {
          el.style.cssText = m.value;
        } else {
          el.style.setProperty(m.prop, m.value);
        }
        break;
      }
    }
  }

  // Create a real DOM node from a serialized definition
  _ensureNode(id, def, doc) {
    if (this.nodeMap.has(id)) return this.nodeMap.get(id);
    if (!def) return null;

    if (def.type === 'text') {
      const node = doc.createTextNode(def.content || '');
      this._trackNode(id, node);
      return node;
    }

    if (def.type === 'element') {
      const el = doc.createElement(def.tag);
      this._trackNode(def.id || id, el);
      if (def.attrs) {
        for (const [k, v] of Object.entries(def.attrs)) {
          try { el.setAttribute(k, v); } catch {}
        }
      }
      if (def.children) {
        for (const child of def.children) {
          const childNode = this._ensureNode(child.id, child, doc);
          if (childNode) el.appendChild(childNode);
        }
      }
      return el;
    }

    return null;
  }

  dispose() {
    this.stop();
    this.nodeMap.clear();
  }
}
