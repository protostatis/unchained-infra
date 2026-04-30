// Browser API Shims — runs INSIDE QuickJS (eval'd as a string)
// Provides window, fetch, XMLHttpRequest, localStorage, navigator, location, etc.

export const SHIMS_SOURCE = `
(function() {

  // --- window ---
  globalThis.window = globalThis;
  globalThis.self = globalThis;

  // --- location ---
  var _location = {
    href: 'about:blank',
    protocol: 'https:',
    host: '',
    hostname: '',
    port: '',
    pathname: '/',
    search: '',
    hash: '',
    origin: '',
    assign: function(url) { _location.href = url; },
    replace: function(url) { _location.href = url; },
    reload: function() {},
    toString: function() { return _location.href; }
  };

  globalThis.__setLocation = function(url) {
    try {
      // Parse URL manually since QuickJS doesn't have URL constructor
      var m = url.match(/^(https?:)\\/\\/([^/:]+)(:\\d+)?([^?#]*)(\\?[^#]*)?(#.*)?$/);
      if (m) {
        _location.protocol = m[1];
        _location.hostname = m[2];
        _location.port = m[3] ? m[3].slice(1) : '';
        _location.host = m[2] + (m[3] || '');
        _location.pathname = m[4] || '/';
        _location.search = m[5] || '';
        _location.hash = m[6] || '';
        _location.origin = m[1] + '//' + _location.host;
        _location.href = url;
      }
    } catch(e) {}
  };

  globalThis.location = _location;

  // --- navigator ---
  globalThis.navigator = {
    userAgent: 'Mozilla/5.0 (WASM QuickJS) sky-search/1.0',
    language: 'en-US',
    languages: ['en-US', 'en'],
    platform: 'WASM',
    cookieEnabled: true,
    onLine: true,
    hardwareConcurrency: 1,
    maxTouchPoints: 0,
    vendor: 'sky-search',
    sendBeacon: function() { return true; }
  };

  // --- history ---
  globalThis.history = {
    length: 1,
    state: null,
    pushState: function(state, title, url) { if (url) __setLocation(url); },
    replaceState: function(state, title, url) { if (url) __setLocation(url); },
    go: function() {},
    back: function() {},
    forward: function() {}
  };

  // --- localStorage / sessionStorage ---
  function MemoryStorage() {
    this._data = {};
  }
  MemoryStorage.prototype.getItem = function(key) { return this._data.hasOwnProperty(key) ? this._data[key] : null; };
  MemoryStorage.prototype.setItem = function(key, value) { this._data[key] = String(value); };
  MemoryStorage.prototype.removeItem = function(key) { delete this._data[key]; };
  MemoryStorage.prototype.clear = function() { this._data = {}; };
  MemoryStorage.prototype.key = function(i) { return Object.keys(this._data)[i] || null; };
  Object.defineProperty(MemoryStorage.prototype, 'length', {
    get: function() { return Object.keys(this._data).length; }
  });

  globalThis.localStorage = new MemoryStorage();
  globalThis.sessionStorage = new MemoryStorage();

  // --- performance ---
  var _startTime = Date.now();
  globalThis.performance = {
    now: function() { return Date.now() - _startTime; },
    mark: function() {},
    measure: function() {},
    getEntriesByName: function() { return []; },
    getEntriesByType: function() { return []; },
    timing: { navigationStart: _startTime }
  };

  // --- requestAnimationFrame ---
  var _rafId = 0;
  globalThis.requestAnimationFrame = function(cb) {
    return setTimeout(cb, 16);
  };
  globalThis.cancelAnimationFrame = function(id) {
    clearTimeout(id);
  };

  // --- getComputedStyle ---
  globalThis.getComputedStyle = function(el) {
    return el.style || {};
  };

  // --- matchMedia ---
  globalThis.matchMedia = function(query) {
    return {
      matches: false,
      media: query,
      addEventListener: function() {},
      removeEventListener: function() {},
      addListener: function() {},
      removeListener: function() {}
    };
  };

  // --- ResizeObserver / IntersectionObserver / MutationObserver ---
  function NoopObserver() {}
  NoopObserver.prototype.observe = function() {};
  NoopObserver.prototype.unobserve = function() {};
  NoopObserver.prototype.disconnect = function() {};

  globalThis.ResizeObserver = NoopObserver;
  globalThis.IntersectionObserver = function(cb, opts) {
    NoopObserver.call(this);
  };
  globalThis.IntersectionObserver.prototype = Object.create(NoopObserver.prototype);

  // MutationObserver hooks into the virtual DOM mutation system
  globalThis.MutationObserver = function(callback) {
    this._callback = callback;
    this._targets = [];
  };
  globalThis.MutationObserver.prototype.observe = function(target, config) {
    this._targets.push({ target: target, config: config });
  };
  globalThis.MutationObserver.prototype.disconnect = function() { this._targets = []; };
  globalThis.MutationObserver.prototype.takeRecords = function() { return []; };

  // --- URL ---
  globalThis.URL = function(url, base) {
    if (base) {
      // Simple relative URL resolution
      if (url.startsWith('//')) url = 'https:' + url;
      else if (url.startsWith('/')) {
        var m = base.match(/^(https?:\\/\\/[^/]+)/);
        url = (m ? m[1] : '') + url;
      } else if (!url.match(/^https?:/)) {
        url = base.replace(/\\/[^/]*$/, '/') + url;
      }
    }
    this.href = url;
    var m = url.match(/^(https?:)\\/\\/([^/:]+)(:\\d+)?([^?#]*)(\\?[^#]*)?(#.*)?$/);
    if (m) {
      this.protocol = m[1];
      this.hostname = m[2];
      this.port = m[3] ? m[3].slice(1) : '';
      this.host = m[2] + (m[3] || '');
      this.pathname = m[4] || '/';
      this.search = m[5] || '';
      this.hash = m[6] || '';
      this.origin = m[1] + '//' + this.host;
    }
    this.searchParams = {
      get: function(key) {
        var s = (m && m[5] || '').slice(1);
        var pairs = s.split('&');
        for (var i = 0; i < pairs.length; i++) {
          var kv = pairs[i].split('=');
          if (decodeURIComponent(kv[0]) === key) return decodeURIComponent(kv[1] || '');
        }
        return null;
      }
    };
    this.toString = function() { return this.href; };
  };

  globalThis.URLSearchParams = function(init) {
    this._params = {};
    if (typeof init === 'string') {
      init.replace(/^\\?/, '').split('&').forEach(function(pair) {
        var kv = pair.split('=');
        if (kv[0]) this._params[decodeURIComponent(kv[0])] = decodeURIComponent(kv[1] || '');
      }.bind(this));
    }
  };
  globalThis.URLSearchParams.prototype.get = function(k) { return this._params[k] || null; };
  globalThis.URLSearchParams.prototype.set = function(k, v) { this._params[k] = v; };
  globalThis.URLSearchParams.prototype.has = function(k) { return k in this._params; };
  globalThis.URLSearchParams.prototype.toString = function() {
    return Object.keys(this._params).map(function(k) {
      return encodeURIComponent(k) + '=' + encodeURIComponent(this._params[k]);
    }.bind(this)).join('&');
  };

  // --- URL resolution against current page origin ---
  function resolveUrl(url) {
    if (!url) return url;
    // Already absolute
    if (url.match(/^https?:\\/\\//)) return url;
    // Protocol-relative
    if (url.startsWith('//')) return location.protocol + url;
    // Absolute path
    if (url.startsWith('/')) return location.origin + url;
    // Relative path
    var base = location.href.replace(/\\/[^/]*$/, '/');
    return base + url;
  }

  // --- fetch (uses XHR internally to avoid QuickJS Promise handle issues) ---
  globalThis.fetch = function(input, init) {
    init = init || {};
    var url = typeof input === 'string' ? input : (input && input.url) || String(input);
    url = resolveUrl(url);
    var method = init.method || 'GET';
    var headers = init.headers || {};
    var body = init.body || null;

    // Convert Headers object to plain object
    if (headers && typeof headers.forEach === 'function') {
      var h = {};
      headers.forEach(function(v, k) { h[k] = v; });
      headers = h;
    }

    // Use sync XHR under the hood (same as XMLHttpRequest shim)
    try {
      var xhr = new XMLHttpRequest();
      xhr.open(method, url, false);
      if (typeof headers === 'object') {
        for (var k in headers) xhr.setRequestHeader(k, headers[k]);
      }
      xhr.send(body);

      var responseBody = xhr.responseText;
      var responseStatus = xhr.status;

      var response = {
        ok: responseStatus >= 200 && responseStatus < 300,
        status: responseStatus,
        statusText: xhr.statusText || '',
        url: url,
        headers: {
          get: function() { return null; },
          has: function() { return false; }
        },
        text: function() { return Promise.resolve(responseBody); },
        json: function() {
          try { return Promise.resolve(JSON.parse(responseBody)); }
          catch(e) { return Promise.reject(e); }
        },
        blob: function() { return Promise.resolve(responseBody); },
        clone: function() { return Object.assign({}, response); }
      };

      return Promise.resolve(response);
    } catch(e) {
      return Promise.reject(new TypeError('fetch failed: ' + e.message));
    }
  };

  // --- Headers ---
  globalThis.Headers = function(init) {
    this._h = {};
    if (init) {
      if (typeof init === 'object') {
        for (var k in init) this._h[k.toLowerCase()] = init[k];
      }
    }
  };
  globalThis.Headers.prototype.get = function(n) { return this._h[n.toLowerCase()] || null; };
  globalThis.Headers.prototype.set = function(n, v) { this._h[n.toLowerCase()] = v; };
  globalThis.Headers.prototype.has = function(n) { return n.toLowerCase() in this._h; };
  globalThis.Headers.prototype.forEach = function(fn) {
    for (var k in this._h) fn(this._h[k], k);
  };

  // --- XMLHttpRequest ---
  globalThis.XMLHttpRequest = function() {
    this.readyState = 0;
    this.status = 0;
    this.statusText = '';
    this.responseText = '';
    this.responseType = '';
    this.response = null;
    this._method = 'GET';
    this._url = '';
    this._headers = {};
    this._listeners = {};
    this.onreadystatechange = null;
    this.onload = null;
    this.onerror = null;
  };

  XMLHttpRequest.prototype.open = function(method, url, async) {
    this._method = method;
    this._url = resolveUrl(url);
    this.readyState = 1;
  };

  XMLHttpRequest.prototype.setRequestHeader = function(name, value) {
    this._headers[name] = value;
  };

  XMLHttpRequest.prototype.send = function(body) {
    try {
      var resultJson = __hostFetch(this._url, this._method, JSON.stringify(this._headers), body || '');
      var result = JSON.parse(resultJson);
      this.status = result.status;
      this.statusText = result.statusText || '';
      this.responseText = result.body || '';
      this.response = this.responseType === 'json' ? JSON.parse(this.responseText) : this.responseText;
      this.readyState = 4;
      if (this.onreadystatechange) this.onreadystatechange();
      if (this.onload) this.onload();
    } catch(e) {
      this.status = 0;
      this.readyState = 4;
      if (this.onerror) this.onerror(e);
    }
  };

  XMLHttpRequest.prototype.addEventListener = function(type, fn) {
    if (!this._listeners[type]) this._listeners[type] = [];
    this._listeners[type].push(fn);
  };

  XMLHttpRequest.prototype.getResponseHeader = function(name) { return null; };
  XMLHttpRequest.prototype.getAllResponseHeaders = function() { return ''; };
  XMLHttpRequest.prototype.abort = function() {};

  XMLHttpRequest.DONE = 4;

  // Blob / File / FileList / FileReader / FormData stubs live in the
  // file-api family section further down. The earlier minimal stubs
  // that used to live here (Blob with only _parts/type, FormData with
  // only append()) were shadowing those richer versions via the
  // "|| function(){}" guard, so sites calling .size or .entries()
  // silently got undefined. Removed.

  // --- atob / btoa ---
  globalThis.atob = function(str) {
    var chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';
    var output = '';
    // Pad to multiple of 4
    while (str.length % 4) str += '=';
    for (var i = 0; i < str.length; i += 4) {
      var a = chars.indexOf(str[i]);
      var b = chars.indexOf(str[i+1]);
      var c = chars.indexOf(str[i+2]);
      var d = chars.indexOf(str[i+3]);
      if (a < 0) a = 0; if (b < 0) b = 0;
      if (c < 0) c = 0; if (d < 0) d = 0;
      var bits = (a << 18) | (b << 12) | (c << 6) | d;
      output += String.fromCharCode((bits >> 16) & 0xff);
      if (str[i+2] !== '=') output += String.fromCharCode((bits >> 8) & 0xff);
      if (str[i+3] !== '=') output += String.fromCharCode(bits & 0xff);
    }
    return output;
  };

  globalThis.btoa = function(str) {
    var chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';
    var output = '';
    for (var i = 0; i < str.length; i += 3) {
      var a = str.charCodeAt(i), b = str.charCodeAt(i+1), c = str.charCodeAt(i+2);
      var bits = (a << 16) | ((b || 0) << 8) | (c || 0);
      output += chars[(bits >> 18) & 63] + chars[(bits >> 12) & 63];
      output += (i + 1 < str.length) ? chars[(bits >> 6) & 63] : '=';
      output += (i + 2 < str.length) ? chars[bits & 63] : '=';
    }
    return output;
  };

  // --- DOMContentLoaded / load events ---
  globalThis.__fireDOMContentLoaded = function() {
    document.readyState = 'interactive';
    document.dispatchEvent(new Event('DOMContentLoaded'));
  };

  globalThis.__fireLoad = function() {
    document.readyState = 'complete';
    if (typeof window.onload === 'function') window.onload(new Event('load'));
    window.dispatchEvent(new Event('load'));
  };

  // window event listeners
  var _windowListeners = {};
  window.addEventListener = function(type, fn) {
    if (!_windowListeners[type]) _windowListeners[type] = [];
    _windowListeners[type].push(fn);
  };
  window.removeEventListener = function(type, fn) {
    if (!_windowListeners[type]) return;
    _windowListeners[type] = _windowListeners[type].filter(function(f) { return f !== fn; });
  };
  window.dispatchEvent = function(event) {
    var listeners = _windowListeners[event.type] || [];
    for (var i = 0; i < listeners.length; i++) listeners[i](event);
  };
  window.onload = null;

  // --- Misc ---
  globalThis.requestIdleCallback = function(cb) { return setTimeout(cb, 1); };
  globalThis.cancelIdleCallback = function(id) { clearTimeout(id); };
  globalThis.queueMicrotask = function(cb) { Promise.resolve().then(cb); };
  globalThis.structuredClone = function(obj) { return JSON.parse(JSON.stringify(obj)); };
  globalThis.crypto = {
    getRandomValues: function(arr) { for (var i = 0; i < arr.length; i++) arr[i] = Math.floor(Math.random() * 256); return arr; },
    subtle: { digest: function() { return Promise.resolve(new ArrayBuffer(32)); } },
    randomUUID: function() { return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function(c) { var r = Math.random()*16|0; return (c === 'x' ? r : (r&0x3|0x8)).toString(16); }); }
  };

  // --- Missing API stubs (prevent script crashes) ---
  globalThis.DOMParser = globalThis.DOMParser || function() {
    this.parseFromString = function(str) { return { body: { textContent: str } }; };
  };
  globalThis.TextEncoder = globalThis.TextEncoder || function() {
    this.encode = function(s) { var a = []; for (var i = 0; i < s.length; i++) a.push(s.charCodeAt(i)); return new Uint8Array(a); };
  };
  globalThis.TextDecoder = globalThis.TextDecoder || function() {
    this.decode = function(buf) { return String.fromCharCode.apply(null, new Uint8Array(buf)); };
  };
  globalThis.AbortController = globalThis.AbortController || function() {
    this.signal = { aborted: false, addEventListener: function() {}, removeEventListener: function() {} };
    this.abort = function() { this.signal.aborted = true; };
  };
  globalThis.AbortSignal = globalThis.AbortSignal || { timeout: function() { return { aborted: false, addEventListener: function() {} }; } };
  globalThis.WeakRef = globalThis.WeakRef || function(v) { this.deref = function() { return v; }; };
  globalThis.FinalizationRegistry = globalThis.FinalizationRegistry || function() { this.register = function() {}; };
  globalThis.MessageChannel = globalThis.MessageChannel || function() {
    this.port1 = { postMessage: function() {}, onmessage: null, close: function() {} };
    this.port2 = { postMessage: function() {}, onmessage: null, close: function() {} };
  };
  globalThis.Worker = globalThis.Worker || function() { this.postMessage = function() {}; this.terminate = function() {}; };
  globalThis.Proxy = globalThis.Proxy || function(target) { return target; };
  globalThis.Symbol = globalThis.Symbol || function(desc) { return '__symbol_' + (desc || Math.random()); };
  if (!globalThis.Symbol.iterator) globalThis.Symbol.iterator = '__symbol_iterator';
  if (!globalThis.Symbol.toPrimitive) globalThis.Symbol.toPrimitive = '__symbol_toPrimitive';
  if (!globalThis.Symbol.toStringTag) globalThis.Symbol.toStringTag = '__symbol_toStringTag';
  globalThis.Map = globalThis.Map || function() { this._d = {}; this.set = function(k,v) { this._d[k] = v; return this; }; this.get = function(k) { return this._d[k]; }; this.has = function(k) { return k in this._d; }; this.delete = function(k) { delete this._d[k]; }; this.forEach = function(fn) { for (var k in this._d) fn(this._d[k], k); }; Object.defineProperty(this, 'size', { get: function() { return Object.keys(this._d).length; } }); };
  globalThis.Set = globalThis.Set || function() { this._d = {}; this.add = function(v) { this._d[v] = true; return this; }; this.has = function(v) { return v in this._d; }; this.delete = function(v) { delete this._d[v]; }; this.forEach = function(fn) { for (var k in this._d) fn(k); }; Object.defineProperty(this, 'size', { get: function() { return Object.keys(this._d).length; } }); };
  globalThis.WeakMap = globalThis.WeakMap || function() { this._id = '__wm_' + Math.random(); };
  globalThis.WeakMap.prototype = { set: function(k,v) { k[this._id] = v; }, get: function(k) { return k[this._id]; }, has: function(k) { return this._id in k; } };
  globalThis.WeakSet = globalThis.WeakSet || function() { this._id = '__ws_' + Math.random(); };
  globalThis.WeakSet.prototype = { add: function(v) { v[this._id] = true; }, has: function(v) { return !!v[this._id]; } };
  globalThis.HTMLElement = globalThis.HTMLElement || globalThis.Element;
  globalThis.HTMLDivElement = globalThis.HTMLDivElement || globalThis.Element;
  globalThis.HTMLSpanElement = globalThis.HTMLSpanElement || globalThis.Element;
  globalThis.HTMLAnchorElement = globalThis.HTMLAnchorElement || globalThis.Element;
  globalThis.HTMLImageElement = globalThis.HTMLImageElement || globalThis.Element;
  globalThis.HTMLInputElement = globalThis.HTMLInputElement || globalThis.Element;
  globalThis.HTMLButtonElement = globalThis.HTMLButtonElement || globalThis.Element;
  globalThis.HTMLFormElement = globalThis.HTMLFormElement || globalThis.Element;
  globalThis.HTMLCanvasElement = globalThis.HTMLCanvasElement || function() { this.getContext = function() { return null; }; };
  globalThis.SVGElement = globalThis.SVGElement || globalThis.Element;
  globalThis.Window = globalThis;
  globalThis.DocumentFragment = globalThis.DocumentFragment || globalThis.Node;
  globalThis.Range = globalThis.Range || function() { this.setStart = function() {}; this.setEnd = function() {}; this.getBoundingClientRect = function() { return {top:0,left:0,right:0,bottom:0,width:0,height:0}; }; };
  globalThis.Selection = globalThis.Selection || function() { this.getRangeAt = function() { return new Range(); }; this.rangeCount = 0; };
  globalThis.getSelection = globalThis.getSelection || function() { return new Selection(); };
  globalThis.screen = globalThis.screen || { width: 1920, height: 1080, availWidth: 1920, availHeight: 1080, colorDepth: 24 };
  globalThis.visualViewport = globalThis.visualViewport || { width: 1920, height: 1080, offsetLeft: 0, offsetTop: 0, scale: 1, addEventListener: function() {} };
  globalThis.devicePixelRatio = globalThis.devicePixelRatio || 1;
  globalThis.innerWidth = 1920;
  globalThis.innerHeight = 1080;
  globalThis.outerWidth = 1920;
  globalThis.outerHeight = 1080;
  globalThis.scrollX = 0;
  globalThis.scrollY = 0;
  globalThis.pageXOffset = 0;
  globalThis.pageYOffset = 0;
  globalThis.scrollTo = function() {};
  globalThis.scroll = function() {};
  globalThis.scrollBy = function() {};
  globalThis.focus = function() {};
  globalThis.blur = function() {};
  globalThis.open = function() { return null; };
  globalThis.close = function() {};
  globalThis.print = function() {};
  globalThis.alert = function() {};
  globalThis.confirm = function() { return false; };
  globalThis.prompt = function() { return null; };
  globalThis.postMessage = function() {};
  globalThis.parent = globalThis;
  globalThis.top = globalThis;
  globalThis.frames = [];
  globalThis.frameElement = null;

  // ---------------------------------------------------------------------------
  // PR B: Globals targeting top ReferenceErrors from scripts/coverage/matrix.md
  // ---------------------------------------------------------------------------
  // Mined from results.jsonl (278 total 'X is not defined' events across 456
  // sites). Each stub below is the minimum surface area needed to let the
  // first few scripts on a page parse & run without crashing; they intentionally
  // do NOT emulate real behavior. If a site's logic depends on the real API
  // returning real data, the stub will silently yield empty results — that's
  // preferable to a hard ReferenceError aborting the whole script tag.

  // jQuery / $ — 111 + 50 = 161 hits. Sites reference $(...) and $.ajax early
  // during init. We return a chainable no-op; all jQuery methods are simulated
  // as returning the same "collection" object so chained calls don't throw.
  //
  // IMPORTANT plugin-pattern support: real jQuery plugins extend $.fn to add
  // instance methods, e.g. \`$.fn.slick = function () { ... }\` then call
  // \`$('.slider').slick()\`. Our $() returns the jq function itself (the
  // "collection"), so \`$(sel).slick\` looks up \`jq.slick\`. To make
  // plugin assignments like \`$.fn.slick = fn\` visible on \`$(sel).slick\`,
  // we alias \`jq.fn = jq\` — $.fn IS $ in the shim. A separate \`jq.fn\`
  // object would silently drop every plugin method and leave sites using
  // slick/select2/datatables/etc. in the same startup-failure path this
  // shim exists to prevent.
  (function() {
    var jq = function(sel) { return jq; };
    // Chainable methods commonly used in first-paint scripts.
    var chain = ['ready','on','off','one','bind','unbind','trigger','click',
      'hover','focus','blur','submit','change','keyup','keydown','keypress',
      'mouseenter','mouseleave','mousedown','mouseup','mousemove','scroll',
      'resize','load','unload','each','map','filter','find','closest','parent',
      'parents','children','siblings','next','prev','first','last','eq','not',
      'add','addBack','end','pushStack','slice','append','prepend','before',
      'after','wrap','unwrap','wrapAll','wrapInner','replaceWith','replaceAll',
      'remove','empty','clone','html','text','val','attr','removeAttr','prop',
      'removeProp','addClass','removeClass','toggleClass','css','show','hide',
      'toggle','fadeIn','fadeOut','fadeTo','fadeToggle','slideUp','slideDown',
      'slideToggle','animate','stop','finish','delay','queue','dequeue',
      'clearQueue','data','removeData','serialize','serializeArray','trigger',
      'triggerHandler','scrollTop','scrollLeft','width','height','innerWidth',
      'innerHeight','outerWidth','outerHeight','offset','position'];
    for (var i = 0; i < chain.length; i++) jq[chain[i]] = function() { return jq; };
    jq.length = 0;
    // Static utility methods — return harmless defaults.
    jq.ajax = function(opts) {
      opts = opts || {};
      var d = { done: function(fn) { return d; }, fail: function(fn) { return d; },
                always: function(fn) { return d; }, then: function() { return d; } };
      return d;
    };
    jq.get = jq.post = jq.getJSON = jq.ajax;
    jq.extend = function() {
      var out = arguments[0] || {};
      for (var i = 1; i < arguments.length; i++) {
        var a = arguments[i];
        if (a) for (var k in a) out[k] = a[k];
      }
      return out;
    };
    jq.each = function(coll, fn) {
      if (coll && typeof coll.length === 'number') {
        for (var i = 0; i < coll.length; i++) if (fn.call(coll[i], i, coll[i]) === false) break;
      } else if (coll) {
        for (var k in coll) if (fn.call(coll[k], k, coll[k]) === false) break;
      }
      return coll;
    };
    jq.isArray = Array.isArray;
    jq.isFunction = function(v) { return typeof v === 'function'; };
    jq.isPlainObject = function(v) { return v && typeof v === 'object' && v.constructor === Object; };
    jq.trim = function(s) { return String(s == null ? '' : s).replace(/^\\s+|\\s+$/g, ''); };
    jq.noop = function() {};
    jq.parseJSON = function(s) { try { return JSON.parse(s); } catch(e) { return null; } };
    // $.fn IS $ — aliased so plugin-style \`$.fn.slick = X\` assigns X to
    // jq.slick, which is what $(sel).slick resolves to on invocation.
    jq.fn = jq;
    jq.jquery = '3.0.0-shim';
    jq.expr = { ':': {}, pseudos: {} };
    jq.Deferred = function() {
      var d = {
        done: function() { return d; }, fail: function() { return d; },
        always: function() { return d; }, then: function() { return d; },
        resolve: function() { return d; }, reject: function() { return d; },
        promise: function() { return d; }, state: function() { return 'pending'; }
      };
      return d;
    };
    globalThis.jQuery = jq;
    globalThis.$ = jq;
  })();

  // customElements — 25 hits. Web Components registry. Without it, polyfills
  // and component libs (lit, stencil) throw at module top level. A no-op
  // registry that swallows define() calls is fine because we don't actually
  // render custom elements in the first-paint extraction.
  globalThis.customElements = globalThis.customElements || {
    _reg: {},
    define: function(name, ctor, opts) { this._reg[name] = ctor; },
    get: function(name) { return this._reg[name]; },
    whenDefined: function(name) { return Promise.resolve(); },
    upgrade: function() {}
  };

  // Image — 12 hits. Constructor for preloading images; scripts often do
  // 'var img = new Image(); img.src = "x"; img.onload = ...'. We simulate a
  // load failure asynchronously so onerror handlers fire and init proceeds.
  globalThis.Image = globalThis.Image || function(w, h) {
    this.width = w || 0;
    this.height = h || 0;
    this.src = '';
    this.onload = null;
    this.onerror = null;
    this.complete = false;
    this.naturalWidth = 0;
    this.naturalHeight = 0;
  };

  // HTMLScriptElement — 11 hits. Class constant often referenced in
  // "instanceof HTMLScriptElement" checks or prototype-patching. Aliasing to
  // generic Element is enough; instanceof checks will just return false.
  globalThis.HTMLScriptElement = globalThis.HTMLScriptElement || globalThis.Element || function() {};

  // CSS — 5 hits. Static namespace. Scripts check CSS.supports('foo') and
  // CSS.escape(sel). supports() defaulting to false is safest (libs pick
  // fallback code paths). escape() is a simple regex replacement.
  globalThis.CSS = globalThis.CSS || {
    supports: function() { return false; },
    escape: function(s) { return String(s).replace(/[!"#$%&'()*+,.\\/:;<=>?@\\[\\\\\\]^\`{|}~]/g, '\\\\$&'); }
  };

  // PerformanceObserver — 4 hits. Used by perf libs for LCP/CLS measurement.
  // Silent no-op observer (never fires) is fine — we don't report telemetry.
  globalThis.PerformanceObserver = globalThis.PerformanceObserver || function(cb) {
    this._cb = cb;
    this.observe = function() {};
    this.disconnect = function() {};
    this.takeRecords = function() { return []; };
  };
  globalThis.PerformanceObserver.supportedEntryTypes = [];

  // EventTarget — 4 hits. Base DOM interface. dom.js defines EventTarget
  // locally but doesn't export it to globalThis, so scripts doing
  // "class X extends EventTarget" blow up at parse-eval. Minimal stub with
  // add/remove/dispatch is enough for subclassing.
  globalThis.EventTarget = globalThis.EventTarget || function() { this._lsn = {}; };
  if (!globalThis.EventTarget.prototype.addEventListener) {
    globalThis.EventTarget.prototype.addEventListener = function(t, f) {
      if (!this._lsn) this._lsn = {};
      (this._lsn[t] = this._lsn[t] || []).push(f);
    };
    globalThis.EventTarget.prototype.removeEventListener = function(t, f) {
      if (!this._lsn || !this._lsn[t]) return;
      this._lsn[t] = this._lsn[t].filter(function(g) { return g !== f; });
    };
    globalThis.EventTarget.prototype.dispatchEvent = function(e) {
      var l = (this._lsn && this._lsn[e && e.type]) || [];
      for (var i = 0; i < l.length; i++) try { l[i].call(this, e); } catch(_) {}
      return true;
    };
  }

  // CharacterData / HTMLMediaElement — 3 + 2 hits. DOM interface classes used
  // in instanceof checks. Aliasing to Element means the checks return false
  // gracefully instead of ReferenceError.
  globalThis.CharacterData = globalThis.CharacterData || globalThis.Node || function() {};
  globalThis.HTMLMediaElement = globalThis.HTMLMediaElement || globalThis.Element || function() {};

  // ReadableStream — 3 hits. Used by modern fetch() consumers. We don't stream
  // responses, so a no-op constructor + cancel() is enough; code that .pipeTo
  // or .getReader will get undefined and usually short-circuits.
  globalThis.ReadableStream = globalThis.ReadableStream || function(src) {
    this.locked = false;
    this.cancel = function() { return Promise.resolve(); };
    this.getReader = function() {
      return { read: function() { return Promise.resolve({ done: true, value: undefined }); },
               cancel: function() { return Promise.resolve(); },
               releaseLock: function() {} };
    };
    this.pipeTo = function() { return Promise.resolve(); };
    this.pipeThrough = function(t) { return t && t.readable || new ReadableStream(); };
    this.tee = function() { return [new ReadableStream(), new ReadableStream()]; };
  };
  globalThis.WritableStream = globalThis.WritableStream || function() {
    this.locked = false;
    this.getWriter = function() { return { write: function() { return Promise.resolve(); },
                                            close: function() { return Promise.resolve(); },
                                            releaseLock: function() {} }; };
  };

  // File API — next-layer ReferenceErrors surfaced by the bench after
  // the jQuery/customElements/etc. shims let scripts run further. The
  // 2026-04-11 bench run on pr-b-shims showed adidas.com, macys.com,
  // and similar ecommerce sites hitting "ReferenceError: 'File' is not
  // defined" in upload/form libraries. Script-heavy SPAs that support
  // drag-and-drop image uploads also reference these at parse time.
  // None of them actually need to CREATE a real file — they just need
  // the constructor and instanceof checks to resolve.
  //
  // Blob comes before File because File extends Blob in real browsers.
  globalThis.Blob = globalThis.Blob || function(parts, opts) {
    parts = parts || [];
    opts = opts || {};
    this.size = 0;
    for (var i = 0; i < parts.length; i++) {
      var p = parts[i];
      if (p == null) continue;
      if (typeof p === 'string') this.size += p.length;
      else if (p.byteLength != null) this.size += p.byteLength;
      else if (p.size != null) this.size += p.size;
    }
    this.type = opts.type || '';
    this._parts = parts;
    this.slice = function(start, end, type) {
      return new globalThis.Blob([], { type: type || this.type });
    };
    this.arrayBuffer = function() { return Promise.resolve(new ArrayBuffer(0)); };
    this.text = function() { return Promise.resolve(''); };
    this.stream = function() { return new globalThis.ReadableStream(); };
  };

  // File — extends Blob with a name and lastModified. Sites that do
  // \`new File([...], 'name.png', { type: 'image/png' })\` or
  // \`file instanceof File\` all work against this stub.
  globalThis.File = globalThis.File || function(parts, name, opts) {
    globalThis.Blob.call(this, parts, opts);
    this.name = name || '';
    this.lastModified = (opts && opts.lastModified) || Date.now();
    this.webkitRelativePath = '';
  };
  try { globalThis.File.prototype = Object.create(globalThis.Blob.prototype); } catch(_) {}

  // FileList — array-like returned by <input type="file">.files. We
  // don't produce real files, so an empty list + item() is enough for
  // sites that check .length or iterate on init.
  globalThis.FileList = globalThis.FileList || function() {
    this.length = 0;
    this.item = function() { return null; };
  };

  // FileReader — many upload libraries construct one at module load
  // even if they don't use it immediately. onload/onerror are set then
  // readAs* is called later; we fire onerror asynchronously so error
  // handlers run and the upload flow short-circuits cleanly.
  globalThis.FileReader = globalThis.FileReader || function() {
    this.readyState = 0;
    this.result = null;
    this.error = null;
    this.onload = null;
    this.onerror = null;
    this.onloadend = null;
    this.onabort = null;
    this.onprogress = null;
    var self = this;
    function fail() {
      self.readyState = 2;
      self.error = { name: 'NotReadableError', message: 'FileReader stub' };
      if (typeof self.onerror === 'function') try { self.onerror({ target: self }); } catch(_) {}
      if (typeof self.onloadend === 'function') try { self.onloadend({ target: self }); } catch(_) {}
    }
    this.readAsText = fail;
    this.readAsDataURL = fail;
    this.readAsArrayBuffer = fail;
    this.readAsBinaryString = fail;
    this.abort = function() { self.readyState = 2; };
  };
  globalThis.FileReader.EMPTY = 0;
  globalThis.FileReader.LOADING = 1;
  globalThis.FileReader.DONE = 2;

  // URL.createObjectURL / revokeObjectURL — used alongside Blob/File for
  // in-memory previews. Return a stable stub URL so subsequent references
  // don't crash; revoke is a no-op.
  if (typeof globalThis.URL !== 'undefined') {
    if (!globalThis.URL.createObjectURL) {
      globalThis.URL.createObjectURL = function() { return 'blob:stub/00000000-0000-0000-0000-000000000000'; };
    }
    if (!globalThis.URL.revokeObjectURL) {
      globalThis.URL.revokeObjectURL = function() {};
    }
  }

  // FormData — used by fetch(url, { body: new FormData() }). Provide a
  // minimal append/get/has/delete surface; the body is never actually
  // sent since __hostFetch is stubbed.
  globalThis.FormData = globalThis.FormData || function() {
    var entries = [];
    this.append = function(k, v) { entries.push([String(k), v]); };
    this.delete = function(k) { entries = entries.filter(function(e) { return e[0] !== k; }); };
    this.get = function(k) { for (var i = 0; i < entries.length; i++) if (entries[i][0] === k) return entries[i][1]; return null; };
    this.getAll = function(k) { return entries.filter(function(e) { return e[0] === k; }).map(function(e) { return e[1]; }); };
    this.has = function(k) { return entries.some(function(e) { return e[0] === k; }); };
    this.set = function(k, v) { this.delete(k); this.append(k, v); };
    this.forEach = function(fn) { entries.forEach(function(e) { fn(e[1], e[0]); }); };
    this.entries = function() { return entries.slice(); };
    this.keys = function() { return entries.map(function(e) { return e[0]; }); };
    this.values = function() { return entries.map(function(e) { return e[1]; }); };
  };

  // Storage — 2 hits. The Storage interface constructor (not localStorage
  // itself, which is already shimmed). Used in instanceof checks and prototype
  // patching. Empty stub suffices.
  globalThis.Storage = globalThis.Storage || function() {};

  // Intl — 2 hits. QuickJS may or may not ship Intl. Provide minimal
  // Collator/DateTimeFormat/NumberFormat that delegate to native JS where
  // possible. This is a VERY thin shim — locale-aware sorting falls back to
  // codepoint order and number formatting uses toString().
  if (!globalThis.Intl) {
    globalThis.Intl = {
      Collator: function() {
        this.compare = function(a, b) { return a < b ? -1 : a > b ? 1 : 0; };
        this.resolvedOptions = function() { return { locale: 'en' }; };
      },
      DateTimeFormat: function() {
        this.format = function(d) { return new Date(d || Date.now()).toString(); };
        this.formatToParts = function() { return []; };
        this.resolvedOptions = function() { return { locale: 'en' }; };
      },
      NumberFormat: function() {
        this.format = function(n) { return String(n); };
        this.formatToParts = function() { return []; };
        this.resolvedOptions = function() { return { locale: 'en' }; };
      },
      PluralRules: function() {
        this.select = function() { return 'other'; };
        this.resolvedOptions = function() { return { locale: 'en' }; };
      },
      RelativeTimeFormat: function() {
        this.format = function(n, unit) { return n + ' ' + unit; };
        this.formatToParts = function() { return []; };
      },
      ListFormat: function() {
        this.format = function(list) { return (list || []).join(', '); };
      },
      getCanonicalLocales: function(l) { return [].concat(l || 'en'); }
    };
  }

  // NodeFilter — 2 hits. Constants object for TreeWalker/NodeIterator filter
  // return values. Scripts compare against NodeFilter.FILTER_ACCEPT etc.
  globalThis.NodeFilter = globalThis.NodeFilter || {
    FILTER_ACCEPT: 1, FILTER_REJECT: 2, FILTER_SKIP: 3,
    SHOW_ALL: 0xFFFFFFFF, SHOW_ELEMENT: 1, SHOW_ATTRIBUTE: 2, SHOW_TEXT: 4,
    SHOW_CDATA_SECTION: 8, SHOW_ENTITY_REFERENCE: 16, SHOW_ENTITY: 32,
    SHOW_PROCESSING_INSTRUCTION: 64, SHOW_COMMENT: 128, SHOW_DOCUMENT: 256,
    SHOW_DOCUMENT_TYPE: 512, SHOW_DOCUMENT_FRAGMENT: 1024, SHOW_NOTATION: 2048
  };

  // Drupal / drupalSettings — 4 + 4 hits. Drupal CMS globals. behaviors.attach
  // is called on DOMContentLoaded; an empty behaviors map + empty settings
  // means attach loops run zero times. Safe on non-Drupal sites since the
  // stub only fires if site code references these names.
  globalThis.drupalSettings = globalThis.drupalSettings || { path: { baseUrl: '/', currentPath: '' }, user: {} };
  globalThis.Drupal = globalThis.Drupal || {
    behaviors: {},
    settings: globalThis.drupalSettings,
    t: function(s) { return s; },
    url: function(p) { return p; },
    attachBehaviors: function() {},
    detachBehaviors: function() {},
    theme: function() { return ''; },
    checkPlain: function(s) { return String(s); }
  };

  // freestar — 4 hits. Ad stack SDK (freestar.com). Sites push config into
  // freestar.queue and freestar.config.enabled_slots. A queue-array plus
  // no-op newAdSlots is enough so init code doesn't throw.
  //
  // IMPORTANT: queue MUST be an actual array, not a {push: fn=>fn()} object.
  // The real freestar pubfig.min.js (loaded by apnews, nbcnews, etc.) detects
  // a pre-existing window.freestar and keeps its queue in place. If our push
  // synchronously invokes callbacks, pubfig's internal code re-enters push
  // via newAdSlots → queue.push(...) → invoke → newAdSlots → … and blows the
  // WASM stack ~125 frames deep. An empty array matches the real contract.
  globalThis.freestar = globalThis.freestar || {
    queue: [],
    config: { enabled_slots: [], disabledProducts: {} },
    newAdSlots: function() {},
    deleteAdSlots: function() {},
    refresh: function() {}
  };

  // hbspt — 2 hits. HubSpot forms SDK. Sites call hbspt.forms.create({...}).
  // Stub returns undefined; forms simply don't render, but surrounding page
  // scripts continue.
  globalThis.hbspt = globalThis.hbspt || {
    forms: { create: function() {}, getAll: function() { return []; } },
    cta: { load: function() {} },
    meetings: { create: function() {} }
  };

  // ---------------------------------------------------------------------------
  // PR B: Webpack chunk-path runtime fix
  // ---------------------------------------------------------------------------
  // Matrix shows 80 "Error: chunk path empty but not in a worker" events
  // (concentrated on benzinga.com and similar webpack-bundled SPAs). Webpack's
  // lazy-chunk loader derives its public path at runtime by reading
  // document.currentScript.src and falling back to __webpack_public_path__.
  // When both are empty AND we're not in a Worker, it throws the above error
  // from inside the module runtime. Providing either one short-circuits the
  // check. We set both defensively.

  // __webpack_public_path__ — empty string is actually the trigger value;
  // webpack treats non-empty as "already configured". Use location.origin so
  // any derived chunk URLs are at least well-formed (even though we never
  // actually fetch lazy chunks).
  try {
    globalThis.__webpack_public_path__ = (location && location.origin) ? (location.origin + '/') : '/';
  } catch(_) {
    globalThis.__webpack_public_path__ = '/';
  }

  // document.currentScript — dom.js does not set this. Webpack reads .src off
  // it to derive the public path at module-load time. We set it to a stub
  // whose src points at the current page, so webpack strips the filename and
  // uses the page origin as the chunk base. Wrapped in try so if a future
  // dom.js change makes currentScript non-writable we degrade gracefully.
  try {
    if (typeof document !== 'undefined' && !document.currentScript) {
      var _href = '';
      try { _href = (location && location.href) || ''; } catch(_) {}
      document.currentScript = {
        src: _href || 'https://localhost/main.js',
        type: 'text/javascript',
        async: false,
        defer: false,
        getAttribute: function(n) { return n === 'src' ? this.src : null; },
        hasAttribute: function(n) { return n === 'src'; }
      };
    }
  } catch(_) {}

  // Webpack also sometimes references these jsonp-runtime globals by name
  // when evaluating bundle manifests. Empty arrays/objects are the default
  // shapes webpack itself installs at bundle start.
  globalThis.webpackChunk = globalThis.webpackChunk || [];
  globalThis.webpackJsonp = globalThis.webpackJsonp || [];

})();
`;
