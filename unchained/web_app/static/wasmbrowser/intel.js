// Intel Extraction — runs unchainedsky extraction JS inside QuickJS WASM engine
// Loads scripts from server (private, not bundled)

let scripts = null;
let bayesConfig = null;

async function loadScripts() {
  if (scripts) return;
  const [scriptsRes, bayesRes] = await Promise.all([
    fetch('/api/intel/scripts'),
    fetch('/api/intel/bayes'),
  ]);
  if (scriptsRes.ok) scripts = await scriptsRes.json();
  if (bayesRes.ok) bayesConfig = await bayesRes.json();
}

// Run fingerprinting on the current page in QuickJS
export async function fingerprint(engine) {
  await loadScripts();
  if (!scripts?.FINGERPRINT_JS) return null;

  try {
    const result = engine.eval(`(function() { ${scripts.FINGERPRINT_JS} })()`);
    return result;
  } catch (e) {
    console.warn('[Intel] fingerprint error:', e.message);
    return null;
  }
}

// Categorize fingerprint features (mirrors intel_engine.py categorize_features)
function categorizeFeatures(fp) {
  if (!fp) return {};
  return {
    shadow_roots: fp.shadowRoots > 5 ? 'high' : fp.shadowRoots > 0 ? 'medium' : 'low',
    custom_elements: fp.customEls > 20 ? 'high' : fp.customEls > 0 ? 'medium' : 'none',
    rich_attrs: fp.richAttrEls > 5 ? 'high' : 'low',
    js_global_found: false, // Can't detect JS globals in our virtual DOM
    react_fiber: fp.reactFiber || false,
    data_testids: fp.dataTestIds > 100 ? 'very_many' : fp.dataTestIds > 20 ? 'many' : fp.dataTestIds > 0 ? 'some' : 'none',
    total_elements: fp.totalEls > 500 ? 'dense' : fp.totalEls > 50 ? 'normal' : 'sparse',
  };
}

// Bayesian ranking (mirrors intel_engine.py bayesian_rank)
function bayesianRank(observations) {
  if (!bayesConfig?.priors || !bayesConfig?.likelihoods) {
    // Fallback: just use innerText
    return [{ strategy: 'innerText', score: 1.0 }];
  }

  const results = [];
  for (const [strategy, prior] of Object.entries(bayesConfig.priors)) {
    let logScore = Math.log(prior);
    const likelihoods = bayesConfig.likelihoods[strategy] || {};

    for (const [feature, value] of Object.entries(observations)) {
      const featureLikelihoods = likelihoods[feature] || {};
      const likelihood = featureLikelihoods[String(value)] ?? 0.1;
      logScore += Math.log(likelihood);
    }

    results.push({ strategy, score: Math.exp(logScore) });
  }

  // Normalize
  const total = results.reduce((s, r) => s + r.score, 0);
  results.forEach(r => r.score = r.score / total);
  results.sort((a, b) => b.score - a.score);

  return results;
}

// Map strategy name to JS constant name
const STRATEGY_MAP = {
  innerText: 'EXTRACT_INNERTEXT_JS',
  host_attrs: 'EXTRACT_HOST_ATTRS_JS',
  js_global: 'EXTRACT_JS_GLOBAL_JS',
  img_alt: 'EXTRACT_IMG_ALT_JS',
  heading_hier: 'EXTRACT_HEADING_HIER_JS',
  data_testid: 'EXTRACT_DATA_TESTID_JS',
  shadow_pierce: 'EXTRACT_SHADOW_PIERCE_JS',
  react_fiber: 'EXTRACT_REACT_FIBER_JS',
};

// Run the full extraction pipeline
export async function extract(engine) {
  await loadScripts();

  // 1. Fingerprint
  const fp = await fingerprint(engine);

  // 2. Categorize
  const observations = categorizeFeatures(fp);

  // 3. Rank strategies
  const ranked = bayesianRank(observations);

  // 4. Try top strategy, fall back to innerText
  for (const { strategy, score } of ranked.slice(0, 3)) {
    const jsKey = STRATEGY_MAP[strategy];
    if (!jsKey || !scripts[jsKey]) continue;

    try {
      const result = engine.eval(`(function() { ${scripts[jsKey]} })()`);
      if (result && (typeof result === 'string' ? result.length > 10 : true)) {
        return {
          strategy,
          score,
          data: result,
          fingerprint: fp,
        };
      }
    } catch (e) {
      console.warn(`[Intel] ${strategy} failed:`, e.message);
    }
  }

  // Fallback: just get body text
  try {
    const text = engine.eval('document.body ? document.body.textContent : ""');
    return { strategy: 'fallback_textContent', score: 0, data: text, fingerprint: fp };
  } catch {
    return { strategy: 'none', score: 0, data: '', fingerprint: fp };
  }
}

// Run DDM text extraction
export async function extractText(engine, keyword) {
  await loadScripts();

  if (keyword && scripts?.TEXT_FIND_JS) {
    try {
      // Inject keyword and run
      const js = scripts.TEXT_FIND_JS.replace('__KEYWORD__', keyword);
      return engine.eval(`(function() { ${js} })()`);
    } catch {}
  }

  // Default: innerText extraction
  if (scripts?.EXTRACT_INNERTEXT_JS) {
    try {
      return engine.eval(`(function() { ${scripts.EXTRACT_INNERTEXT_JS} })()`);
    } catch {}
  }

  return engine.eval('document.body ? document.body.textContent : ""');
}

// Detect forms on the page
export async function detectForms(engine) {
  await loadScripts();
  if (!scripts?.FORMS_JS) return [];

  try {
    return engine.eval(`(function() { ${scripts.FORMS_JS} })()`);
  } catch (e) {
    console.warn('[Intel] forms detection error:', e.message);
    return [];
  }
}
