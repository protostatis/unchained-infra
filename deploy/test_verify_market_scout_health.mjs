#!/usr/bin/env node
//
// test_verify_market_scout_health.mjs — async-schema tests for the helper.
//
// Run: node deploy/test_verify_market_scout_health.mjs

import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import { fileURLToPath } from "node:url";
import { execSync } from "node:child_process";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const HELPER_PATH = path.join(__dirname, "verify_market_scout_health.mjs");

const PINNED_SOURCES = [
  { id:"nasdaq-trade-halts",       url:"https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts" },
  { id:"nasdaq-corporate-actions", url:"https://www.nasdaqtrader.com/rss.aspx?feed=currentheadlines&categorylist=105" },
  { id:"sec-current-filings",      url:"https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&count=40&output=atom" },
  { id:"federal-reserve-monetary", url:"https://www.federalreserve.gov/feeds/press_monetary.xml" },
  { id:"bea-news",                 url:"https://apps.bea.gov/rss/rss.xml" },
  { id:"ftc-press-releases",       url:"https://www.ftc.gov/feeds/press-release.xml" },
  { id:"doj-news",                 url:"https://www.justice.gov/feeds/justice-news.xml" },
];

const TEMPLATE_READER = `
import fs from "node:fs";
export async function readMarketEventScoutState(p) {
  let raw;
  try { raw = fs.readFileSync(p, "utf-8"); } catch(e) {
    if (e.code === "ENOENT") return { version:1, updatedAt:Date.now(), sources:[], decisions:[] };
    throw e;
  }
  const data = JSON.parse(raw);
  if (!data || typeof data !== "object" || Array.isArray(data)) throw new Error("bad root");
  if (data.version !== 1) throw new Error("bad version");
  if (!Array.isArray(data.sources)) throw new Error("sources not array");
  for (const s of data.sources) {
    if (!s || typeof s.sourceId !== "string") throw new Error("missing sourceId");
    if (typeof s.lastAttemptAt !== "number") throw new Error("lastAttemptAt not numeric");
  }
  return data;
}
`;

// ---------------------------------------------------------------------------
async function createMockApp(overrides = {}) {
  const tmpDir = await fs.mkdtemp(path.join(os.tmpdir(), "vscout-"));
  const sources = overrides.sources || PINNED_SOURCES;
  const jpath    = overrides.filePath || "/data/market-terminal/market-event-scout.json";
  const lines = [];
  lines.push(`export const DEFAULT_MARKET_EVENT_SOURCES = ${JSON.stringify(sources)};`);

  // marketEventScoutFilePath as function
  if (overrides.pathFn !== undefined) {
    lines.push(`export function marketEventScoutFilePath(cwd,env){ return ${JSON.stringify(overrides.pathFn)}; }`);
  } else {
    lines.push(`export function marketEventScoutFilePath(cwd,env){ return ${JSON.stringify(jpath)}; }`);
  }

  // readMarketEventScoutState — async!
  if (overrides.readerCode) {
    lines.push(overrides.readerCode);
  } else {
    lines.push(TEMPLATE_READER);
  }

  const appPath = path.join(tmpDir, "market-event-scout.js");
  await fs.writeFile(appPath, lines.join("\n"), "utf-8");
  return { tmpDir, appPath };
}

async function importHelper() {
  const mod = await import(HELPER_PATH + `?t=${Date.now()}-${Math.random()}`);
  if (mod._resetAppModule) mod._resetAppModule();
  return mod;
}

function setEnv(obj) {
  const save = {};
  for (const k of Object.keys(obj)) { save[k] = process.env[k]; process.env[k] = obj[k]; }
  return save;
}
function restoreEnv(save) {
  for (const [k,v] of Object.entries(save)) {
    if (v === undefined) delete process.env[k]; else process.env[k] = v;
  }
}

function baseEnv(appPath) {
  return {
    NODE_ENV: "production", PUBLIC_DEMO: "0", MARKET_SCOUT_ENABLED: "1",
    MARKET_SCOUT_LOCAL_CLI: "0", UNBROWSER_MCP_REQUIRED: "1",
    UNBROWSER_MCP_URL: "http://unbrowser-mcp:8767/mcp",
    MARKET_DATA_DIR: "/data/market-terminal",
    MARKET_SCOUT_APP_MODULE_PATH: appPath,
  };
}

// ---------------------------------------------------------------------------
// Invariant tests
// ---------------------------------------------------------------------------

test("invariant: pinned IDs exact match", async () => {
  const { tmpDir, appPath } = await createMockApp();
  const save = setEnv(baseEnv(appPath));
  delete process.env.TERMINAL_RUNTIME_MODE;
  try {
    const mod = await importHelper();
    const r = await mod.validateInvariants();
    assert.equal(r.ok, true, JSON.stringify(r.errors));
    assert.equal(r.scoutEnabled, true);
  } finally { await fs.rm(tmpDir, { recursive: true, force: true }); restoreEnv(save); }
});

test("invariant: extra source rejected (count mismatch)", async () => {
  const extra = [...PINNED_SOURCES, { id:"extra-source", url:"https://x.example.com" }];
  const { tmpDir, appPath } = await createMockApp({ sources: extra });
  const save = setEnv(baseEnv(appPath));
  delete process.env.TERMINAL_RUNTIME_MODE;
  try {
    const mod = await importHelper();
    const r = await mod.validateInvariants();
    assert.equal(r.ok, false);
    assert.ok(r.errors.some(e => e.includes("extra=1")), JSON.stringify(r.errors));
  } finally { await fs.rm(tmpDir, { recursive: true, force: true }); restoreEnv(save); }
});

test("invariant: duplicate source rejected", async () => {
  const duplicate = [...PINNED_SOURCES, PINNED_SOURCES[0]];
  const { tmpDir, appPath } = await createMockApp({ sources: duplicate });
  const save = setEnv(baseEnv(appPath));
  delete process.env.TERMINAL_RUNTIME_MODE;
  try {
    const mod = await importHelper();
    const r = await mod.validateInvariants();
    assert.equal(r.ok, false);
    assert.ok(r.errors.some(e => e.includes("source contract mismatch")), JSON.stringify(r.errors));
  } finally { await fs.rm(tmpDir, { recursive: true, force: true }); restoreEnv(save); }
});

test("invariant: missing source rejected", async () => {
  const missing = PINNED_SOURCES.slice(0, 6);
  const { tmpDir, appPath } = await createMockApp({ sources: missing });
  const save = setEnv(baseEnv(appPath));
  delete process.env.TERMINAL_RUNTIME_MODE;
  try {
    const mod = await importHelper();
    const r = await mod.validateInvariants();
    assert.equal(r.ok, false);
    assert.ok(r.errors.some(e => e.includes("missing=1")), JSON.stringify(r.errors));
  } finally { await fs.rm(tmpDir, { recursive: true, force: true }); restoreEnv(save); }
});

test("invariant: wrong host mapping rejected", async () => {
  const badHost = PINNED_SOURCES.map(s =>
    s.id === "sec-current-filings" ? { ...s, url: "https://wrong.example.com/feed" } : s);
  const { tmpDir, appPath } = await createMockApp({ sources: badHost });
  const save = setEnv(baseEnv(appPath));
  delete process.env.TERMINAL_RUNTIME_MODE;
  try {
    const mod = await importHelper();
    const r = await mod.validateInvariants();
    assert.equal(r.ok, false);
    assert.ok(r.errors.some(e => e.includes("host mismatch")), JSON.stringify(r.errors));
  } finally { await fs.rm(tmpDir, { recursive: true, force: true }); restoreEnv(save); }
});

test("invariant: forward-disable passes", async () => {
  const { tmpDir, appPath } = await createMockApp();
  const save = setEnv({ ...baseEnv(appPath), MARKET_SCOUT_ENABLED: "0" });
  delete process.env.TERMINAL_RUNTIME_MODE;
  try {
    const mod = await importHelper();
    const r = await mod.validateInvariants();
    assert.equal(r.ok, true);
    assert.equal(r.scoutEnabled, false);
  } finally { await fs.rm(tmpDir, { recursive: true, force: true }); restoreEnv(save); }
});

test("invariant: MARKET_RESEARCH_WORKER rejected as singleton marker", async () => {
  const { tmpDir, appPath } = await createMockApp();
  const save = setEnv({ ...baseEnv(appPath), MARKET_RESEARCH_WORKER: "1" });
  delete process.env.TERMINAL_RUNTIME_MODE;
  try {
    const mod = await importHelper();
    const r = await mod.validateInvariants();
    assert.equal(r.ok, false);
    assert.ok(r.errors.some(e => e.includes("non-singleton")));
  } finally { await fs.rm(tmpDir, { recursive: true, force: true }); restoreEnv(save); }
});

test("invariant: no raw env values in errors", async () => {
  const badSources = PINNED_SOURCES.map(s =>
    s.id === "sec-current-filings" ? { ...s, url: "https://SENTINEL_HOST.invalid/feed" } : s);
  const { tmpDir, appPath } = await createMockApp({
    sources: badSources,
    pathFn: "/SENTINEL_PATH/market-event-scout.json",
  });
  const save = setEnv({
    ...baseEnv(appPath),
    NODE_ENV: "SENTINEL_NODE_ENV",
    UNBROWSER_MCP_URL: "https://SENTINEL_MCP.invalid/secret",
    MARKET_DATA_DIR: "/SENTINEL_DATA",
  });
  delete process.env.TERMINAL_RUNTIME_MODE;
  try {
    const mod = await importHelper();
    const r = await mod.validateInvariants();
    assert.equal(r.ok, false);
    for (const e of r.errors) {
      assert.ok(!e.includes("SENTINEL"), `raw value leaked: ${e}`);
    }
  } finally { await fs.rm(tmpDir, { recursive: true, force: true }); restoreEnv(save); }
});

// --- Writable ---
test("writable: ok for writable dir", async () => {
  const { tmpDir, appPath } = await createMockApp();
  process.env.MARKET_DATA_DIR = tmpDir;
  process.env.MARKET_SCOUT_APP_MODULE_PATH = appPath;
  try {
    const mod = await importHelper();
    const r = await mod.checkDataDirWritable();
    assert.equal(r.ok, true);
  } finally { await fs.rm(tmpDir, { recursive: true, force: true }); delete process.env.MARKET_DATA_DIR; delete process.env.MARKET_SCOUT_APP_MODULE_PATH; }
});

// --- Journal sanity via async reader ---
test("journal: ENOENT → reader returns empty state → ok", async () => {
  const { tmpDir, appPath } = await createMockApp();
  const save = setEnv({ ...baseEnv(appPath), MARKET_DATA_DIR: tmpDir });
  try {
    const mod = await importHelper();
    const r = await mod.checkJournalSanity();
    assert.equal(r.ok, true);
    assert.equal(r.journalExists, false);
  } finally { await fs.rm(tmpDir, { recursive: true, force: true }); restoreEnv(save); }
});

test("journal: concurrent first write after ENOENT is accepted", async () => {
  const readerCode = `
import fs from "node:fs";
export async function readMarketEventScoutState(p) {
  const now = Date.now();
  const state = { version:1, updatedAt:now, decisions:[], sources:${JSON.stringify(PINNED_SOURCES.map(source => ({
    sourceId: source.id,
    baselineComplete: true,
    seenEventIds: [],
    nextPollAt: 1,
    lastStatus: "ok",
    lastItemCount: 0,
    baselineItems: 0,
    newItems: 0,
    admitted: 0,
    watched: 0,
    suppressed: 0,
  })))} };
  fs.writeFileSync(p, JSON.stringify(state));
  return state;
}`;
  const { tmpDir, appPath } = await createMockApp({ readerCode });
  const save = setEnv({ ...baseEnv(appPath), MARKET_DATA_DIR: tmpDir });
  try {
    const mod = await importHelper();
    const r = await mod.checkJournalSanity();
    assert.equal(r.ok, true, JSON.stringify(r.errors));
    assert.equal(r.journalExists, true);
    assert.equal(r.sourceCount, 7);
  } finally { await fs.rm(tmpDir, { recursive: true, force: true }); restoreEnv(save); }
});

test("journal: reader rejects → malformed", async () => {
  const badReader = `
export async function readMarketEventScoutState(p) { throw new Error("BAD"); }
`;
  const { tmpDir, appPath } = await createMockApp({ readerCode: badReader });
  const save = setEnv({ ...baseEnv(appPath), MARKET_DATA_DIR: tmpDir });
  await fs.writeFile(path.join(tmpDir, "market-event-scout.json"), "{}", "utf-8");
  try {
    const mod = await importHelper();
    const r = await mod.checkJournalSanity();
    assert.equal(r.ok, false);
    assert.ok(r.errors.some(e => e.includes("rejected")), JSON.stringify(r.errors));
  } finally { await fs.rm(tmpDir, { recursive: true, force: true }); restoreEnv(save); }
});

test("journal: unknown sourceId in journal rejected", async () => {
  const { tmpDir, appPath } = await createMockApp();
  const save = setEnv({ ...baseEnv(appPath), MARKET_DATA_DIR: tmpDir });
  const j = {
    version: 1, updatedAt: Date.now(), decisions: [],
    sources: [
      { sourceId: "nasdaq-trade-halts", lastAttemptAt: Date.now(), baselineComplete: false },
      { sourceId: "unknown-zzz", lastAttemptAt: Date.now(), baselineComplete: false,
        title:"SENTINEL_T", url:"https://sentinel-u.example.com", symbol:"SENT_S", lastError:"SENT_E" },
    ],
  };
  await fs.writeFile(path.join(tmpDir, "market-event-scout.json"), JSON.stringify(j), "utf-8");
  try {
    const mod = await importHelper();
    const r = await mod.checkJournalSanity();
    assert.equal(r.ok, false);
    assert.ok(r.errors.some(e => e.includes("unknown source ID")), JSON.stringify(r.errors));
    for (const e of r.errors) {
      assert.ok(!e.includes("SENTINEL"), `sentinel leaked: ${e}`);
      assert.ok(!e.includes("unknown-zzz"), `ID leaked: ${e}`);
      assert.ok(!e.includes("sentinel-u"), `url leaked: ${e}`);
    }
  } finally { await fs.rm(tmpDir, { recursive: true, force: true }); restoreEnv(save); }
});

// --- Commission tests ---

function makeSources(overrides = []) {
  const now = Date.now();
  const ids = PINNED_SOURCES.map(s => s.id);
  return ids.map((id, i) => {
    const o = overrides[i] || {};
    return {
      sourceId: id,
      lastAttemptAt: o.lastAttemptAt ?? now,
      lastSuccessAt:  o.lastSuccessAt  ?? now,
      baselineComplete: o.baselineComplete ?? true,
      title: "SENTINEL_TITLE", url: "https://sentinel.example.com",
      symbol: "SENT_SYM", lastError: "SENT_ERR", decision: { SENT:"SENT" },
    };
  });
}

test("commission: missing journal → pending → timeout", async () => {
  const { tmpDir, appPath } = await createMockApp();
  const save = setEnv({
    ...baseEnv(appPath), MARKET_DATA_DIR: tmpDir,
    MARKET_SCOUT_CANDIDATE_STARTED_AT: new Date().toISOString(),
    MARKET_SCOUT_COMMISSION_TIMEOUT_MS: "1000",
    MARKET_SCOUT_COMMISSION_POLL_MS: "200",
  });
  try {
    const mod = await importHelper();
    const r = await mod.commissionPoll();
    assert.equal(r.ok, false);
    assert.equal(r.stage, "timeout", JSON.stringify(r.errors));
  } finally { await fs.rm(tmpDir, { recursive: true, force: true }); restoreEnv(save); }
});

test("commission: malformed → immediate failure", async () => {
  const badReader = `export async function readMarketEventScoutState(p) { throw new Error("BAD"); }`;
  const { tmpDir, appPath } = await createMockApp({ readerCode: badReader });
  const save = setEnv({
    ...baseEnv(appPath), MARKET_DATA_DIR: tmpDir,
    MARKET_SCOUT_CANDIDATE_STARTED_AT: new Date().toISOString(),
    MARKET_SCOUT_COMMISSION_TIMEOUT_MS: "5000",
    MARKET_SCOUT_COMMISSION_POLL_MS: "500",
  });
  await fs.writeFile(path.join(tmpDir, "market-event-scout.json"), "{}", "utf-8");
  try {
    const mod = await importHelper();
    const r = await mod.commissionPoll();
    assert.equal(r.ok, false);
    assert.equal(r.stage, "malformed");
  } finally { await fs.rm(tmpDir, { recursive: true, force: true }); restoreEnv(save); }
});

test("commission: stale lastSuccessAt → 0 successes", async () => {
  const { tmpDir, appPath } = await createMockApp();
  const now = Date.now();
  const stale = now - 3600000;
  const save = setEnv({
    ...baseEnv(appPath), MARKET_DATA_DIR: tmpDir,
    MARKET_SCOUT_CANDIDATE_STARTED_AT: new Date(now - 10000).toISOString(),
    MARKET_SCOUT_COMMISSION_TIMEOUT_MS: "1500",
    MARKET_SCOUT_COMMISSION_POLL_MS: "200",
  });
  const sources = makeSources(
    PINNED_SOURCES.map(() => ({ lastSuccessAt: stale, baselineComplete: true })),
  );
  const j = { version:1, updatedAt: now, decisions:[], sources };
  await fs.writeFile(path.join(tmpDir, "market-event-scout.json"), JSON.stringify(j), "utf-8");
  try {
    const mod = await importHelper();
    const r = await mod.commissionPoll();
    assert.equal(r.ok, false);
    assert.equal(r.stage, "timeout");
    assert.equal(r.successCount, 0, "stale lastSuccessAt → 0 successes");
  } finally { await fs.rm(tmpDir, { recursive: true, force: true }); restoreEnv(save); }
});

test("commission: wrong count (5≠7) → allAttempted false", async () => {
  const { tmpDir, appPath } = await createMockApp({
    sources: PINNED_SOURCES.slice(0, 5),
  });
  const now = Date.now();
  const save = setEnv({
    ...baseEnv(appPath), MARKET_DATA_DIR: tmpDir,
    MARKET_SCOUT_CANDIDATE_STARTED_AT: new Date(now - 10000).toISOString(),
    MARKET_SCOUT_COMMISSION_TIMEOUT_MS: "1500",
    MARKET_SCOUT_COMMISSION_POLL_MS: "200",
  });
  const src5 = makeSources().slice(0, 5);
  const j = { version:1, updatedAt: now, decisions:[], sources: src5 };
  await fs.writeFile(path.join(tmpDir, "market-event-scout.json"), JSON.stringify(j), "utf-8");
  try {
    const mod = await importHelper();
    const r = await mod.commissionPoll();
    assert.equal(r.ok, false);
    assert.equal(r.stage, "timeout");
    assert.equal(r.sourceCount, 5);
    assert.equal(r.successCount, 0);
  } finally { await fs.rm(tmpDir, { recursive: true, force: true }); restoreEnv(save); }
});

test("commission: passes with real schema (sourceId, async reader)", async () => {
  const { tmpDir, appPath } = await createMockApp();
  const now = Date.now();
  const save = setEnv({
    ...baseEnv(appPath), MARKET_DATA_DIR: tmpDir,
    MARKET_SCOUT_CANDIDATE_STARTED_AT: new Date(now - 10000).toISOString(),
    MARKET_SCOUT_COMMISSION_TIMEOUT_MS: "20000",
    MARKET_SCOUT_COMMISSION_POLL_MS: "200",
  });
  // 4 successes across 3 hosts (nasdaqtrader, sec.gov, federalreserve.gov)
  const overrides = [
    { baselineComplete: true },   // nasdaq-trade-halts       → nasdaqtrader.com
    { baselineComplete: true },   // nasdaq-corporate-actions → nasdaqtrader.com (same host)
    { baselineComplete: true },   // sec-current-filings      → sec.gov
    { baselineComplete: true },   // federal-reserve-monetary → federalreserve.gov
    { baselineComplete: false },  //
    { baselineComplete: false },  //
    { baselineComplete: false },  //
  ];
  const r1 = { version:1, updatedAt: now, decisions:[], sources: makeSources(overrides) };
  const jpath = path.join(tmpDir, "market-event-scout.json");
  await fs.writeFile(jpath, JSON.stringify(r1), "utf-8");
  // Round 2 after delay
  setTimeout(async () => {
    const t2 = Date.now() + 4000;
    const r2s = makeSources(overrides).map(s =>
      s.sourceId === "nasdaq-trade-halts" ? { ...s, lastAttemptAt: t2 } : s);
    await fs.writeFile(jpath, JSON.stringify({ version:1, updatedAt: t2, decisions:[], sources: r2s }), "utf-8");
  }, 1500);
  try {
    const mod = await importHelper();
    const r = await mod.commissionPoll();
    assert.equal(r.ok, true, JSON.stringify(r.errors));
    assert.equal(r.sourceCount, 7);
    // successes: 4 across 3 hosts
    assert.equal(r.successCount, 4);
    assert.equal(r.hostCount, 3);
    assert.equal(r.schedulerAdvanced, true);
    assert.ok(!JSON.stringify(r).includes("SENTINEL"));
  } finally { await fs.rm(tmpDir, { recursive: true, force: true }); restoreEnv(save); }
});

test("commission: disabled → stage=disabled", async () => {
  const { tmpDir, appPath } = await createMockApp();
  const save = setEnv({
    ...baseEnv(appPath), MARKET_SCOUT_ENABLED: "0",
    MARKET_SCOUT_CANDIDATE_STARTED_AT: new Date().toISOString(),
    MARKET_SCOUT_COMMISSION_TIMEOUT_MS: "1000",
  });
  try {
    const mod = await importHelper();
    const r = await mod.commissionPoll();
    assert.equal(r.ok, false);
    assert.equal(r.stage, "disabled");
  } finally { await fs.rm(tmpDir, { recursive: true, force: true }); restoreEnv(save); }
});

// --- Shell structure tests ---
test("structure: docker exec -e used for env", async () => {
  const dsh = await fs.readFile(path.join(__dirname, "..", "deploy.sh"), "utf-8");
  assert.ok(dsh.includes("-e MARKET_SCOUT_VERIFY_MODE="), "must use docker exec -e");
  // No bare env before docker exec in the verifier function
  const fn = dsh.slice(dsh.indexOf("_verify_market_scout_in_container()"), dsh.indexOf("verify_market_scout_preflight()"));
  assert.ok(!fn.includes("MARKET_SCOUT_VERIFY_MODE=$mode docker exec"), "must not set env before docker exec");
});

test("structure: two explicit reads for container_id and started_at", async () => {
  const dsh = await fs.readFile(path.join(__dirname, "..", "deploy.sh"), "utf-8");
  const fn = dsh.slice(dsh.indexOf("_verify_market_scout_in_container()"), dsh.indexOf("verify_market_scout_preflight()"));
  // Two separate IFS= read -r lines (not chained with &&)
  const readLines = fn.split("\n").filter(l => l.includes("read -r container_id") || l.includes("read -r started_at"));
  assert.equal(readLines.length, 2, `expected 2 read lines, got ${readLines.length}`);
  // No chained && between reads
  assert.ok(!fn.includes("read -r container_id &&"), "reads must not be chained");
  assert.ok(fn.includes("identity changed before market-scout"), "pre-exec check must reject replacement or restart");
  assert.ok(fn.includes(".State.Running"), "pre-exec check must require a running container");
  assert.ok(fn.includes("post_started_at"), "post-check must revalidate StartedAt as well as container ID");
});

// --- Subprocess tests ---
test("subprocess: disabled preflight prints ok", async () => {
  const { tmpDir, appPath } = await createMockApp();
  try {
    const env = { ...process.env, ...baseEnv(appPath), MARKET_SCOUT_ENABLED: "0",
      MARKET_SCOUT_VERIFY_MODE: "preflight", MARKET_DATA_DIR: tmpDir };
    delete env.TERMINAL_RUNTIME_MODE;
    const r = execSync(`node --input-type=module -`, {
      encoding:"utf-8", env, input: `await import("${HELPER_PATH}");`,
      stdio:["pipe","pipe","pipe"],
    });
    assert.ok(r.includes("PREFLIGHT OK (scout disabled)"), r);
  } finally { await fs.rm(tmpDir, { recursive: true, force: true }); }
});

test("subprocess: disabled commission prints SKIPPED and exits 0", async () => {
  const { tmpDir, appPath } = await createMockApp();
  try {
    const env = { ...process.env, ...baseEnv(appPath), MARKET_SCOUT_ENABLED: "0",
      MARKET_SCOUT_VERIFY_MODE: "commission", MARKET_DATA_DIR: tmpDir };
    delete env.TERMINAL_RUNTIME_MODE;
    const r = execSync(`node --input-type=module -`, {
      encoding:"utf-8", env, input: `await import("${HELPER_PATH}");`,
      stdio:["pipe","pipe","pipe"],
    });
    assert.ok(r.includes("COMMISSION SKIPPED (scout disabled)"), r);
  } finally { await fs.rm(tmpDir, { recursive: true, force: true }); }
});

test("subprocess: enabled preflight with no journal prints ok", async () => {
  const { tmpDir, appPath } = await createMockApp();
  try {
    // Use expected data dir for invariants. Writable will fail but that's ok.
    const env = { ...process.env, ...baseEnv(appPath),
      MARKET_SCOUT_VERIFY_MODE: "preflight",
      MARKET_DATA_DIR: "/data/market-terminal" };
    delete env.TERMINAL_RUNTIME_MODE;
    try {
      const r = execSync(`node --input-type=module -`, {
        encoding:"utf-8", env, input: `await import("${HELPER_PATH}");`,
        stdio:["pipe","pipe","pipe"],
      });
      assert.ok(r.includes("PREFLIGHT OK"), r);
    } catch (err) {
      const out = (err.stdout||"") + (err.stderr||"");
      // Writable failure is acceptable in test env; invariants must pass.
      assert.ok(!out.includes("PREFLIGHT FAILED (invariants)"), `invariants failed: ${out}`);
      assert.ok(!out.includes("PREFLIGHT FAILED (journal)"), `journal failed: ${out}`);
    }
  } finally { await fs.rm(tmpDir, { recursive: true, force: true }); }
});

test("subprocess: helper does not print Module loaded message", async () => {
  const { tmpDir, appPath } = await createMockApp();
  try {
    const env = { ...process.env, ...baseEnv(appPath),
      MARKET_SCOUT_VERIFY_MODE: "preflight",
      MARKET_DATA_DIR: "/data/market-terminal" };
    delete env.TERMINAL_RUNTIME_MODE;
    try {
      execSync(`node --input-type=module -`, {
        encoding:"utf-8", env, input: `await import("${HELPER_PATH}");`,
        stdio:["pipe","pipe","pipe"],
      });
    } catch { /* writable failure ok */ }
    // Now test no-mode import (no MARKET_SCOUT_VERIFY_MODE set)
    const env2 = { ...process.env, ...baseEnv(appPath),
      MARKET_DATA_DIR: "/data/market-terminal" };
    delete env2.MARKET_SCOUT_VERIFY_MODE;
    delete env2.TERMINAL_RUNTIME_MODE;
    const r2 = execSync(`node --input-type=module -`, {
      encoding:"utf-8", env: env2, input: `await import("${HELPER_PATH}");`,
      stdio:["pipe","pipe","pipe"],
    });
    assert.ok(!r2.includes("Module loaded"), "no-mode import must be silent");
    assert.ok(!r2.includes("no verify mode"), "no-mode import must be silent");
  } finally { await fs.rm(tmpDir, { recursive: true, force: true }); }
});

console.log("All tests completed.");
