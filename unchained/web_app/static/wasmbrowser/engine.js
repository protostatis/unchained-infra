// QuickJS WASM Engine — Phase 1
// Wraps quickjs-emscripten for browser usage

let QuickJS = null;

export class WasmEngine {
  constructor() {
    this.runtime = null;
    this.vm = null;
    this._disposed = false;
  }

  async init() {
    if (!QuickJS) {
      // Dynamic import from CDN
      const mod = await import('https://esm.sh/quickjs-emscripten@0.32.0');
      QuickJS = await mod.getQuickJS();
    }
    // CRITICAL: create a DEDICATED runtime per engine instance, not a
    // context on the shared default runtime. Otherwise every engine
    // piles context state (intern tables, GC list, WASM heap allocations)
    // onto the same long-lived runtime that never gets disposed, and
    // every navigate makes the browser tab's WASM heap bigger. That
    // produces the classic "each action gets slower than the last"
    // memory-accumulation pattern.
    //
    // Matches the pattern scripts/coverage/run.js uses (which itself is
    // there to work around the same quickjs-emscripten heap-growth
    // behavior — see run-batched.js comment).
    this.runtime = QuickJS.newRuntime();
    this.vm = this.runtime.newContext();
    this._setupConsole();
    this._setupTimers();
    this._setupHostFetch();
    return this;
  }

  // Inject console.log/warn/error that forward to real console
  _setupConsole() {
    const vm = this.vm;

    const consoleHandle = vm.newObject();

    for (const method of ['log', 'warn', 'error', 'info', 'debug']) {
      const fnHandle = vm.newFunction(method, (...args) => {
        const nativeArgs = args.map(a => vm.dump(a));
        console[method]('[QuickJS]', ...nativeArgs);
      });
      vm.setProp(consoleHandle, method, fnHandle);
      fnHandle.dispose();
    }

    vm.setProp(vm.global, 'console', consoleHandle);
    consoleHandle.dispose();
  }

  // Basic setTimeout/setInterval (non-async, uses polling)
  _setupTimers() {
    const vm = this.vm;
    this._timers = [];
    this._nextTimerId = 1;

    const setTimeoutHandle = vm.newFunction('setTimeout', (callbackHandle, delayHandle) => {
      const delay = delayHandle ? vm.dump(delayHandle) : 0;
      const callback = vm.dump(callbackHandle);
      const id = this._nextTimerId++;

      // We can't keep the handle alive easily, so store the code string
      // For real usage, we'd need a more sophisticated approach
      this._timers.push({ id, delay, callback: callbackHandle.dup(), time: Date.now() + delay });

      return vm.newNumber(id);
    });
    vm.setProp(vm.global, 'setTimeout', setTimeoutHandle);
    setTimeoutHandle.dispose();

    const clearTimeoutHandle = vm.newFunction('clearTimeout', (idHandle) => {
      const id = vm.dump(idHandle);
      this._timers = this._timers.filter(t => t.id !== id);
    });
    vm.setProp(vm.global, 'clearTimeout', clearTimeoutHandle);
    clearTimeoutHandle.dispose();
  }

  // Synchronous fetch via proxy — used by fetch()/XHR shims inside QuickJS
  _setupHostFetch() {
    const vm = this.vm;
    const handle = vm.newFunction('__hostFetch', function(urlHandle, methodHandle, headersHandle, bodyHandle) {
      const url = vm.dump(urlHandle);
      const method = vm.dump(methodHandle);
      const headers = vm.dump(headersHandle);
      const body = vm.dump(bodyHandle);

      // Synchronous XHR to our proxy endpoint
      const xhr = new XMLHttpRequest();
      xhr.open('POST', '/proxy/fetch', false); // sync
      xhr.setRequestHeader('Content-Type', 'application/json');
      try {
        xhr.send(JSON.stringify({ url, method, headers, body }));
      } catch (e) {
        const errStr = vm.newString(JSON.stringify({ status: 0, statusText: e.message, headers: {}, body: '' }));
        return errStr;
      }

      const result = xhr.status === 200 ? xhr.responseText :
        JSON.stringify({ status: 0, statusText: 'Proxy error', headers: {}, body: '' });
      const resultHandle = vm.newString(result);
      return resultHandle;
    });
    vm.setProp(vm.global, '__hostFetch', handle);
    handle.dispose();
  }

  // Process pending timers
  tick() {
    if (this._disposed || !this.vm) return;
    const now = Date.now();
    const ready = this._timers.filter(t => now >= t.time);
    this._timers = this._timers.filter(t => now < t.time);

    for (const timer of ready) {
      const result = this.vm.callFunction(timer.callback, this.vm.undefined);
      if (result.error) {
        console.error('[QuickJS timer error]', this.vm.dump(result.error));
        result.error.dispose();
      } else {
        result.value.dispose();
      }
      timer.callback.dispose();
    }

    // Execute pending jobs (promises, etc)
    this.vm.runtime.executePendingJobs();
  }

  // Evaluate JavaScript code
  eval(code) {
    if (this._disposed) throw new Error('Engine disposed');

    const result = this.vm.evalCode(code);
    if (result.error) {
      const err = this.vm.dump(result.error);
      result.error.dispose();
      throw new Error(`QuickJS eval error: ${JSON.stringify(err)}`);
    }
    const value = this.vm.dump(result.value);
    result.value.dispose();
    return value;
  }

  // Evaluate and return the raw handle (for advanced usage)
  evalHandle(code) {
    if (this._disposed) throw new Error('Engine disposed');
    const result = this.vm.evalCode(code);
    if (result.error) {
      const err = this.vm.dump(result.error);
      result.error.dispose();
      throw new Error(`QuickJS eval error: ${JSON.stringify(err)}`);
    }
    return result.value;
  }

  // Register a host function on globalThis
  addFunction(name, fn) {
    const handle = this.vm.newFunction(name, (...args) => {
      const nativeArgs = args.map(a => this.vm.dump(a));
      const result = fn(...nativeArgs);
      if (result === undefined || result === null) return this.vm.undefined;
      if (typeof result === 'string') return this.vm.newString(result);
      if (typeof result === 'number') return this.vm.newNumber(result);
      if (typeof result === 'boolean') return result ? this.vm.true : this.vm.false;
      return this.vm.undefined;
    });
    this.vm.setProp(this.vm.global, name, handle);
    handle.dispose();
  }

  // Set a global string variable
  setGlobal(name, value) {
    let handle;
    if (typeof value === 'string') handle = this.vm.newString(value);
    else if (typeof value === 'number') handle = this.vm.newNumber(value);
    else if (typeof value === 'boolean') handle = value ? this.vm.true : this.vm.false;
    else handle = this.vm.newString(JSON.stringify(value));

    this.vm.setProp(this.vm.global, name, handle);
    if (typeof value !== 'boolean') handle.dispose();
  }

  dispose() {
    if (this._disposed) return;
    this._disposed = true;
    // Dispose any remaining timer callbacks
    for (const timer of this._timers) {
      try { timer.callback.dispose(); } catch {}
    }
    this._timers = [];
    // Order matters: context first, then runtime. The runtime owns the
    // GC list and WASM heap allocations; disposing it is what actually
    // releases memory (disposing just the context leaves everything
    // behind, which is the bug this fix addresses).
    if (this.vm) {
      try { this.vm.dispose(); } catch {}
      this.vm = null;
    }
    if (this.runtime) {
      try { this.runtime.dispose(); } catch {}
      this.runtime = null;
    }
  }
}
