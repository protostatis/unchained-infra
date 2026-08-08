#!/usr/bin/env node
//
// verify_market_scout_health.mjs — production commissioning proof for the
// shadow-only market-event scout on the authenticated singleton.
//
// Streamed through `docker exec -i -e ... <container> node --input-type=module -`.
// Supports MARKET_SCOUT_VERIFY_MODE = preflight | commission. Exports functions
// for unit tests when no mode is set.
//
// Safety: never prints env values, secrets, journal JSON, source URLs,
// symbols, titles, lastError, decision fields, or hostnames.

import fs from "node:fs/promises";
import { constants } from "node:fs";
import path from "node:path";

// ---------------------------------------------------------------------------
// Overridable app module path — allow tests to inject a mock.
// ---------------------------------------------------------------------------
function appModulePath() {
  return process.env.MARKET_SCOUT_APP_MODULE_PATH
    || "/app/dist-server/shared/market-event-scout.js";
}

let _appModule = null;
async function getAppModule() {
  if (_appModule) return _appModule;
  _appModule = await import(appModulePath());
  return _appModule;
}
export function _resetAppModule() { _appModule = null; }

// ---------------------------------------------------------------------------
// Pinned constants
// ---------------------------------------------------------------------------
const EXPECTED_NODE_ENV      = "production";
const EXPECTED_PUBLIC_DEMO   = "0";
const EXPECTED_SCOUT_ENABLED = "1";
const EXPECTED_SCOUT_CLI     = "0";
const EXPECTED_MCP_REQUIRED  = "1";
const EXPECTED_MCP_URL       = "http://unbrowser-mcp:8767/mcp";
const EXPECTED_DATA_DIR      = "/data/market-terminal";
const EXPECTED_JOURNAL_PATH  = "/data/market-terminal/market-event-scout.json";

// Pinned exact source ID → origin host mapping (reviewed infra contract).
const PINNED_SOURCE_MAP = Object.freeze({
  "nasdaq-trade-halts":       "www.nasdaqtrader.com",
  "nasdaq-corporate-actions": "www.nasdaqtrader.com",
  "sec-current-filings":      "www.sec.gov",
  "federal-reserve-monetary": "www.federalreserve.gov",
  "bea-news":                 "apps.bea.gov",
  "ftc-press-releases":       "www.ftc.gov",
  "doj-news":                 "www.justice.gov",
});
const PINNED_SOURCE_IDS = Object.keys(PINNED_SOURCE_MAP);
const EXPECTED_SOURCE_COUNT = PINNED_SOURCE_IDS.length;

const SINGLETON_MARKER_SETS = [
  { TERMINAL_RUNTIME_MODE: "public-gateway" },
  { PUBLIC_SESSION_WORKER: "1" },
  { TERMINAL_RUNTIME_MODE: "private-workspace" },
  { FINANCIAL_WORKSPACE_CHECKPOINTS: "1" },
  { MARKET_RESEARCH_WORKER: "1" },
];

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function ms(v) {
  const n = Number(v);
  return Number.isFinite(n) && n > 0 ? n : undefined;
}
function commissionTimeoutMs()  { return ms(process.env.MARKET_SCOUT_COMMISSION_TIMEOUT_MS) || 20 * 60 * 1000; }
function commissionIntervalMs() { return ms(process.env.MARKET_SCOUT_COMMISSION_POLL_MS) || 3000; }
function candidateStartedAt()   { return process.env.MARKET_SCOUT_CANDIDATE_STARTED_AT || ""; }
function safeEnv(n)             { const v = process.env[n]; return v ?? null; }
function scoutIsEnabled()       { return safeEnv("MARKET_SCOUT_ENABLED") === EXPECTED_SCOUT_ENABLED; }
function getDataDir()           { return safeEnv("MARKET_DATA_DIR") || EXPECTED_DATA_DIR; }
function getJournalPath()       { return path.normalize(path.join(getDataDir(), "market-event-scout.json")); }
function sleep(ms)              { return new Promise(r => setTimeout(r, ms)); }

// ---------------------------------------------------------------------------
// Invariant validation
// ---------------------------------------------------------------------------

/**
 * @returns {{ ok: boolean, errors: string[], scoutEnabled: boolean }}
 */
export async function validateInvariants() {
  const errors = [];
  const add = m => errors.push(m);
  const enabled = scoutIsEnabled();

  if (safeEnv("NODE_ENV") !== EXPECTED_NODE_ENV)
    add(`NODE_ENV invariant failed`);

  if (safeEnv("PUBLIC_DEMO") !== EXPECTED_PUBLIC_DEMO)
    add(`PUBLIC_DEMO invariant failed`);

  for (const m of SINGLETON_MARKER_SETS)
    for (const [k, v] of Object.entries(m))
      if (safeEnv(k) === v) add(`non-singleton marker present`);

  if (safeEnv("MARKET_SCOUT_LOCAL_CLI") !== EXPECTED_SCOUT_CLI)
    add(`MARKET_SCOUT_LOCAL_CLI invariant failed`);

  const se = safeEnv("MARKET_SCOUT_ENABLED");
  if (se !== EXPECTED_SCOUT_ENABLED && se !== "0")
    add(`MARKET_SCOUT_ENABLED invalid`);

  if (!enabled)
    return { ok: errors.length === 0, errors, scoutEnabled: false };

  // --- enabled-only ---
  if (safeEnv("UNBROWSER_MCP_REQUIRED") !== EXPECTED_MCP_REQUIRED)
    add(`UNBROWSER_MCP_REQUIRED invariant failed`);
  if (safeEnv("UNBROWSER_MCP_URL") !== EXPECTED_MCP_URL)
    add(`UNBROWSER_MCP_URL invariant failed`);
  if (safeEnv("MARKET_DATA_DIR") !== EXPECTED_DATA_DIR)
    add(`MARKET_DATA_DIR invariant failed`);

  if (getJournalPath() !== EXPECTED_JOURNAL_PATH)
    add(`journal path invariant failed`);

  const mod = await getAppModule();

  // marketEventScoutFilePath(cwd, env) → string
  const rawFp = mod.marketEventScoutFilePath;
  if (typeof rawFp === "function") {
    let resolved;
    try { resolved = rawFp(process.cwd(), process.env); } catch {
      add(`marketEventScoutFilePath call failed`);
    }
    if (typeof resolved === "string") {
      if (path.normalize(path.resolve("/", resolved)) !== EXPECTED_JOURNAL_PATH)
        add(`marketEventScoutFilePath resolves to unexpected path`);
    } else if (resolved !== undefined) {
      add(`marketEventScoutFilePath non-string result`);
    }
  } else if (typeof rawFp === "string") {
    // accept for test backward compat
    if (path.normalize(path.resolve("/", rawFp)) !== EXPECTED_JOURNAL_PATH)
      add(`marketEventScoutFilePath string resolves to unexpected path`);
  } else {
    add(`marketEventScoutFilePath missing or wrong type`);
  }

  // Verify app exports the required strict reader
  if (typeof mod.readMarketEventScoutState !== "function")
    add(`readMarketEventScoutState not exported`);

  // Pinned source contract: app must export exactly the expected IDs
  const appSources = mod.DEFAULT_MARKET_EVENT_SOURCES;
  if (!Array.isArray(appSources)) {
    add(`DEFAULT_MARKET_EVENT_SOURCES not array`);
  } else {
    const appIds = appSources.map(s => s.id);
    const expectedSet = new Set(PINNED_SOURCE_IDS);
    const missing = PINNED_SOURCE_IDS.filter(id => !appIds.includes(id));
    const extra   = appIds.filter(id => !expectedSet.has(id));
    const duplicateCount = appIds.length - new Set(appIds).size;
    if (missing.length || extra.length || appIds.length !== EXPECTED_SOURCE_COUNT || duplicateCount) {
      add(`source contract mismatch (missing=${missing.length} extra=${extra.length})`);
    }
    // Verify host mapping
    let hostMismatch = 0;
    for (const s of appSources) {
      const expectedHost = PINNED_SOURCE_MAP[s.id];
      if (!expectedHost || typeof s.url !== "string") { hostMismatch++; continue; }
      try { if (new URL(s.url).hostname !== expectedHost) hostMismatch++; }
      catch { hostMismatch++; }
    }
    if (hostMismatch) add(`source host mismatch count=${hostMismatch}`);
  }

  return { ok: errors.length === 0, errors, scoutEnabled: true };
}

// ---------------------------------------------------------------------------
// Writable check (enabled-only)
// ---------------------------------------------------------------------------
export async function checkDataDirWritable() {
  try {
    const dd = getDataDir();
    const tmp = path.join(dd, `.verify-scout-writable-${Date.now()}-${Math.random().toString(36).slice(2,10)}`);
    await fs.writeFile(tmp, "ok", { flag: "wx" });
    await fs.unlink(tmp);
    return { ok: true };
  } catch {
    return { ok: false, error: "data dir not writable" };
  }
}

// ---------------------------------------------------------------------------
// Journal sanity — uses strict reader (async!)
// ---------------------------------------------------------------------------

/**
 * Distinguish missing file (ENOENT via fs.access) from readable journal.
 * Reader is awaited; empty state returned on ENOENT → sources=[], ok.
 */
export async function checkJournalSanity() {
  const jpath = getJournalPath();
  const errors = [];

  const mod = await getAppModule();
  const reader = mod.readMarketEventScoutState;
  if (typeof reader !== "function") {
    // Missing export is a deterministic failure
    return { ok: false, errors: ["strict reader not exported"], journalExists: false };
  }

  // Check existence first to distinguish ENOENT from reader errors. The
  // scheduler may atomically create the journal between this check and the
  // strict read, so a later non-empty state always wins over the earlier
  // missing observation.
  let missingBeforeRead = false;
  try { await fs.access(jpath, constants.R_OK); }
  catch (error) {
    if (error?.code !== "ENOENT") {
      return { ok: false, errors: ["journal is not readable"], journalExists: true };
    }
    missingBeforeRead = true;
  }

  let state;
  try {
    state = await reader(jpath);
  } catch {
    // Reader rejected → malformed
    return { ok: false, errors: ["strict reader rejected journal"], journalExists: true };
  }

  if (missingBeforeRead && Array.isArray(state?.sources) && state.sources.length === 0) {
    // Recheck to close the creation race. A still-missing journal is a valid
    // initial state; an empty journal created concurrently is validated below.
    try { await fs.access(jpath, constants.R_OK); }
    catch (error) {
      if (error?.code === "ENOENT") {
        return { ok: true, errors, journalExists: false };
      }
      return { ok: false, errors: ["journal is not readable"], journalExists: true };
    }
  }

  if (!state || typeof state !== "object" || !Array.isArray(state.sources))
    return { ok: false, errors: ["strict reader returned unexpected shape"], journalExists: true };

  const sources = state.sources;
  const srcCount = sources.length;
  const expectedIds = new Set(PINNED_SOURCE_IDS);

  let unknownCount = 0;
  for (const s of sources) {
    const sid = s?.sourceId;
    if (typeof sid !== "string") { errors.push("source entry missing sourceId field"); break; }
    if (!expectedIds.has(sid)) unknownCount++;
  }
  if (unknownCount) errors.push(`journal contains unknown source IDs (count=${unknownCount} of ${srcCount})`);

  return { ok: errors.length === 0, errors, journalExists: true, sourceCount: srcCount };
}

// ---------------------------------------------------------------------------
// Commission
// ---------------------------------------------------------------------------

export async function commissionPoll() {
  if (!scoutIsEnabled()) {
    return { ok: false, errors: ["commission requires MARKET_SCOUT_ENABLED=1"],
      stage: "disabled", sourceCount: 0, successCount: 0, hostCount: 0, schedulerAdvanced: false };
  }
  await getAppModule();
  const mod = _appModule;
  if (!mod || typeof mod.readMarketEventScoutState !== "function") {
    return { ok: false, errors: ["strict reader not available"], stage: "error", sourceCount:0, successCount:0, hostCount:0, schedulerAdvanced:false };
  }

  const startedVal = candidateStartedAt();
  const startedMs  = startedVal ? new Date(startedVal).getTime() : 0;
  if (!startedMs) return { ok: false, errors: ["CANDIDATE_STARTED_AT not set"], stage:"unknown", sourceCount:0, successCount:0, hostCount:0, schedulerAdvanced:false };

  const FLOOR    = startedMs - 2000;
  const deadline = Date.now() + commissionTimeoutMs();
  let firstRound = null;

  while (Date.now() < deadline) {
    const res = await readJournalForCommission(mod);
    if (res.malformed) return { ok: false, errors: res.errors, stage:"malformed", sourceCount:0, successCount:0, hostCount:0, schedulerAdvanced:false };
    if (res.missing) { await sleep(commissionIntervalMs()); continue; }

    // Round 1
    if (firstRound === null) {
      const a = assessRoundOne(res, FLOOR);
      if (a.allAttempted && a.successCount >= 4 && a.hostCount >= 3 && a.sourceCount === EXPECTED_SOURCE_COUNT) {
        firstRound = { ...a, updatedAtMs: res.updatedAtMs, lastAttempts: { ...res.lastAttempts } };
        continue;
      }
    }

    // Round 2
    if (firstRound !== null) {
      if ((res.updatedAtMs || 0) > (firstRound.updatedAtMs || 0)) {
        if (hasAdvancedSource(res.lastAttempts || {}, firstRound.lastAttempts || {})) {
          return { ok: true, errors:[], stage:"commissioned",
            sourceCount: firstRound.sourceCount, successCount: firstRound.successCount,
            hostCount: firstRound.hostCount, schedulerAdvanced: true };
        }
      }
      firstRound.lastAttempts = { ...res.lastAttempts };
    }
    await sleep(commissionIntervalMs());
  }

  const final = await readJournalForCommission(mod);
  const a     = final.ok ? assessRoundOne(final, FLOOR) : { successCount:0, hostCount:0, sourceCount:0 };
  return { ok: false, errors:["commission timeout exceeded"], stage:"timeout",
    sourceCount: a.sourceCount, successCount: a.successCount, hostCount: a.hostCount, schedulerAdvanced: false };
}

/**
 * Read via strict async reader.
 * Returns { ok, missing, malformed, sources, sourceIds, lastAttempts, updatedAtMs }
 */
async function readJournalForCommission(mod) {
  const jpath = getJournalPath();
  const reader = mod.readMarketEventScoutState;

  // Stat first to distinguish missing from reader errors. A concurrent atomic
  // create is accepted when the strict reader returns a non-empty state.
  let missingBeforeRead = false;
  try { await fs.access(jpath, constants.R_OK); }
  catch (error) {
    if (error?.code !== "ENOENT") {
      return { ok: false, missing: false, malformed: true, errors: ["journal is not readable"] };
    }
    missingBeforeRead = true;
  }

  let state;
  try { state = await reader(jpath); } catch {
    return { ok: false, missing: false, malformed: true, errors: ["strict reader rejected journal"] };
  }

  if (missingBeforeRead && Array.isArray(state?.sources) && state.sources.length === 0) {
    try { await fs.access(jpath, constants.R_OK); }
    catch (error) {
      if (error?.code === "ENOENT") {
        return { ok: false, missing: true, malformed: false };
      }
      return { ok: false, missing: false, malformed: true, errors: ["journal is not readable"] };
    }
  }

  if (!state || typeof state !== "object" || !Array.isArray(state.sources))
    return { ok: false, missing: false, malformed: true, errors: ["malformed: unexpected reader output"] };

  const sources   = state.sources;
  const sourceIds = [];
  const lastAttempts = {};
  let updatedAtMs = 0;
  if (typeof state.updatedAt === "number") updatedAtMs = state.updatedAt;

  for (const s of sources) {
    const sid = s?.sourceId;
    if (typeof sid !== "string") return { ok: false, missing: false, malformed: true, errors: ["malformed: source missing sourceId"] };
    sourceIds.push(sid);
    lastAttempts[sid] = typeof s.lastAttemptAt === "number" ? s.lastAttemptAt : 0;
  }

  return { ok: true, missing: false, malformed: false, sources, sourceIds, lastAttempts, updatedAtMs };
}

/**
 * allAttempted: every pinned sourceId present, every lastAttemptAt >= floor.
 * success: baselineComplete===true AND lastSuccessAt >= floor.
 * hosts: from pinned map (never from app runtime sources).
 */
function assessRoundOne(res, floor) {
  if (!res.ok || !res.sources) return { allAttempted: false, successCount: 0, hostCount: 0, sourceCount: 0 };

  const sources = res.sources;
  const sourceCount = sources.length;
  if (sourceCount !== EXPECTED_SOURCE_COUNT) return { allAttempted: false, successCount: 0, hostCount: 0, sourceCount };

  const seenIds = new Set();
  let allAttempted = true;
  let successCount = 0;
  const successHosts = new Set();

  for (const s of sources) {
    const sid = s?.sourceId;
    if (typeof sid !== "string" || !PINNED_SOURCE_MAP[sid]) { allAttempted = false; continue; }
    seenIds.add(sid);

    const laMs = typeof s.lastAttemptAt === "number" ? s.lastAttemptAt : 0;
    if (laMs < floor) allAttempted = false;

    if (s.baselineComplete === true) {
      const lsMs = typeof s.lastSuccessAt === "number" ? s.lastSuccessAt : 0;
      if (lsMs >= floor) {
        successCount++;
        const host = PINNED_SOURCE_MAP[sid];
        if (host) successHosts.add(host);
      }
    }
  }

  // Verify exact set
  if (seenIds.size !== EXPECTED_SOURCE_COUNT) allAttempted = false;

  return { allAttempted, successCount, hostCount: successHosts.size, sourceCount };
}

function hasAdvancedSource(cur, prev) {
  for (const [sid, v] of Object.entries(cur)) {
    const p = prev[sid];
    if (p == null) continue;
    if (v > p) return true;
  }
  return false;
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------
const mode = process.env.MARKET_SCOUT_VERIFY_MODE || "";

if (mode === "preflight") {
  const inv = await validateInvariants();
  if (!inv.ok) {
    console.error("PREFLIGHT FAILED (invariants):");
    for (const e of inv.errors) console.error("  -", e);
    process.exit(1);
  }
  if (inv.scoutEnabled) {
    const w = await checkDataDirWritable();
    if (!w.ok) { console.error("PREFLIGHT FAILED (writable):", w.error); process.exit(1); }
    const j = await checkJournalSanity();
    if (!j.ok) {
      console.error("PREFLIGHT FAILED (journal):");
      for (const e of j.errors) console.error("  -", e);
      process.exit(1);
    }
    console.log("PREFLIGHT OK (scout enabled)");
    console.log(`  journalExists=${j.journalExists}`);
    if (j.sourceCount != null) console.log(`  journalSourceCount=${j.sourceCount}`);
  } else {
    console.log("PREFLIGHT OK (scout disabled)");
  }
  process.exit(0);
}

if (mode === "commission") {
  const inv = await validateInvariants();
  if (!inv.ok) {
    console.error("COMMISSION FAILED (invariants):");
    for (const e of inv.errors) console.error("  -", e);
    process.exit(1);
  }
  if (!inv.scoutEnabled) {
    console.log("COMMISSION SKIPPED (scout disabled)");
    process.exit(0);
  }
  const result = await commissionPoll();
  if (!result.ok) {
    console.error("COMMISSION FAILED");
    console.error(`  stage=${result.stage || "unknown"}`);
    console.error(`  sourceCount=${result.sourceCount}`);
    console.error(`  successCount=${result.successCount}`);
    console.error(`  hostCount=${result.hostCount}`);
    console.error(`  schedulerAdvanced=${result.schedulerAdvanced}`);
    for (const e of result.errors) console.error("  error:", e);
    process.exit(1);
  }
  console.log("COMMISSION OK");
  console.log(`  sourceCount=${result.sourceCount}`);
  console.log(`  successCount=${result.successCount}`);
  console.log(`  hostCount=${result.hostCount}`);
  console.log(`  schedulerAdvanced=${result.schedulerAdvanced}`);
  process.exit(0);
}
