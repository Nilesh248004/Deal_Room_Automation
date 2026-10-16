// End-to-end tests: real headless capture of a mock hotel site, the real pipeline, the HTTP API, and a mock Hippo.
// Run: node --test test/e2e.test.js      (needs python3 + playwright + chromium)
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const http = require("node:http");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "..");
const SITE_PORT = 8766, HIPPO_PORT = 8798, LLM_PORT = 8797;
const SITE = `http://127.0.0.1:${SITE_PORT}/hotels/grand-azure/`;
const HIPPO = `http://127.0.0.1:${HIPPO_PORT}`;
const DATA = fs.mkdtempSync(path.join(os.tmpdir(), "dra-test-"));

Object.assign(process.env, { DATA_DIR: DATA, API_KEYS: "tester:secret-key-123", ALLOW_HTTP: "1", HIPPO_BASE_URL: HIPPO, HIPPO_API_TOKEN: "test-token", MAX_CONCURRENT: "2" });
delete process.env.LLM_BASE_URL;
const { createServer } = require("../src/server");
const { Jobs } = require("../src/jobs");

const children = [];
let api, base, llmServer, llmMode = "off";
const sleep = ms => new Promise(r => setTimeout(r, ms));
const H = { "x-api-key": "secret-key-123", "content-type": "application/json" };
const req = async (method, p, body, headers = H) => {
  const r = await fetch(base + p, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  const t = await r.text(); let j = null; try { j = JSON.parse(t); } catch {}
  return { status: r.status, json: j, text: t, headers: r.headers };
};
const waitFor = async (id, statuses, ms = 240000) => {
  const t0 = Date.now();
  for (;;) {
    const { json: j } = await req("GET", "/deal-rooms/" + id);
    if (statuses.includes(j.status)) return j;
    if (["failed", "succeeded", "cancelled", "rolled_back", "awaiting_approval"].includes(j.status) && !statuses.includes(j.status)) {
      const log = (await req("GET", `/deal-rooms/${id}/logs`)).text;
      assert.fail(`job ${id} ended as ${j.status} (${j.error}); wanted ${statuses}\n${log.slice(-3000)}`);
    }
    if (Date.now() - t0 > ms) assert.fail("timeout waiting for " + statuses);
    await sleep(300);
  }
};
const hippo = async (p, body) => (await fetch(HIPPO + p, body ? { method: "POST", body: JSON.stringify(body) } : {})).json();
const tokens = obj => [...new Set(JSON.stringify(obj).replace(/&amp;/g, "&").match(/\$\{[^}]*\}/g) || [])];

test.before(async () => {
  children.push(spawn("python3", ["-m", "http.server", String(SITE_PORT), "--bind", "127.0.0.1", "--directory", path.join(__dirname, "fixtures", "mocksite")], { stdio: "ignore" }));
  children.push(spawn(process.execPath, [path.join(__dirname, "mock_hippo.js")], { env: { ...process.env, MOCK_HIPPO_PORT: String(HIPPO_PORT) }, stdio: "ignore" }));
  // mock OpenAI-compatible LLM: invents facts for the overview, stays grounded for dining
  llmServer = http.createServer((q, s) => { let b = ""; q.on("data", c => b += c); q.on("end", () => {
    const src = JSON.parse(b).messages[1].content;
    const text = /Sea Salt/.test(src) ? "Sea Salt restaurant serves Atlantic seafood, and Azul Bar sits on the rooftop." : "An award-winning resort with 900 rooms and a Michelin-starred restaurant in Porto.";
    s.writeHead(200, { "content-type": "application/json" }); s.end(JSON.stringify({ choices: [{ message: { content: text } }] }));
  }); }).listen(LLM_PORT, "127.0.0.1");
  const jobs = new Jobs({ dataDir: DATA, maxConcurrent: 2, recover: false });
  api = createServer(jobs); await new Promise(r => api.listen(0, "127.0.0.1", r));
  base = `http://127.0.0.1:${api.address().port}`;
  for (let i = 0; i < 50; i++) { try { await fetch(SITE); await fetch(HIPPO + "/__log"); break; } catch { await sleep(200); } }
  await hippo("/__reset", {});
});
test.after(() => { children.forEach(c => c.kill()); api && api.close(); llmServer && llmServer.close(); });

let scrape;   // captured once, reused by later tests (uploaded-scrape path)

test("health is public; everything else needs a valid key", async () => {
  assert.equal((await req("GET", "/health", undefined, {})).status, 200);
  assert.equal((await req("GET", "/deal-rooms", undefined, {})).status, 401);
  assert.equal((await req("GET", "/deal-rooms", undefined, { "x-api-key": "wrong" })).status, 401);
  assert.equal((await req("GET", "/deal-rooms", undefined, { authorization: "Bearer secret-key-123" })).status, 200);
});

test("bad input is rejected before a job starts", async () => {
  const r = await req("POST", "/deal-rooms", { hotel_url: "not a url", flow: "sms" });
  assert.equal(r.status, 400); assert.ok(r.json.details.length >= 2);
  assert.equal((await req("POST", "/deal-rooms", { hotel_url: SITE, load: "api" })).status, 400, "load=api without page id");
});

test("an OTA link fails at intake with a clear message", async () => {
  const { json } = await req("POST", "/deal-rooms", { hotel_url: "https://www.booking.com/hotel/pt/grand-azure.html" });
  const j = await waitFor(json.id, ["failed"]);
  assert.match(j.error, /intake: .*booking/);
});

let reviewJob;
test("review mode: capture → … → review, then waits for approval", async () => {
  const { status, json } = await req("POST", "/deal-rooms", { hotel_url: SITE, hippo_page_id: "1605252", load: "api" });
  assert.equal(status, 202);
  const j = await waitFor(json.id, ["awaiting_approval"]);
  reviewJob = j.id;
  for (const s of ["intake", "capture", "convert", "facts", "copy", "build", "review"]) assert.equal(j.stages[s].status, "ok", s);
  assert.equal(j.stages.load.status, "pending");
  const log = (await req("GET", `/deal-rooms/${j.id}/logs`)).text;
  assert.match(log, /cookie banner: clicked 'Accept all'/);
  const arts = (await req("GET", `/deal-rooms/${j.id}/artifacts`)).json.artifacts;
  for (const a of ["review.html", "template.json", "loader.js", "facts.json", "copy.json", "page.png"]) assert.ok(arts.includes(a), a);
  const facts = (await req("GET", `/deal-rooms/${j.id}/artifacts/facts.json`)).json;
  assert.ok(facts.every(f => f.verified), "every fact has a source");
  assert.ok(facts.some(f => f.field === "faq"), "FAQ answers read from structured data");
  const copy = (await req("GET", `/deal-rooms/${j.id}/artifacts/copy.json`)).json;
  assert.deepEqual(copy.ungrounded, {});
  assert.match(JSON.stringify(copy.fields.location_cards), /Belem Tower/);
  const tpl = (await req("GET", `/deal-rooms/${j.id}/artifacts/template.json`)).json;
  assert.deepEqual(Object.keys(tpl), ["multiple_logo_top", "sections", "sideNav", "header_cta_buttons", "settings", "contactInfo"]);
  assert.ok(!JSON.stringify(tpl).includes("response."), "no Cvent tokens in an Email deal room");
  assert.ok(tokens(tpl).length > 40);
  assert.equal(tpl.settings.primaryColor, "#0A3D62", "brand colour from the site header");
  const html = await (await fetch(`${base}/deal-rooms/${j.id}/artifacts/review.html`, { headers: H })).text();
  assert.match(html, /Grand Azure Resort Lisbon/);
  scrape = (await req("GET", `/deal-rooms/${j.id}/artifacts/scrape.json`)).json;
  assert.equal((await hippo("/__log")).length, 0, "nothing was sent to Hippo before approval");
});

test("artifacts are allowlisted", async () => {
  assert.equal((await req("GET", `/deal-rooms/${reviewJob}/artifacts/job.json`)).status, 404);
  assert.equal((await req("GET", `/deal-rooms/${reviewJob}/artifacts/..%2Fjob.json`)).status, 404);
  assert.equal((await req("GET", `/deal-rooms/dr_nope_00000000`)).status, 404);
  const r = await fetch(`${base}/deal-rooms/${reviewJob}/artifacts/loader.js`, { headers: H });
  assert.match(r.headers.get("content-disposition"), /attachment/);
});

test("unavailable preview images return 502 without stopping the server", async () => {
  const previewJobId = "dr_previewfailure_00000000";
  const previewJobDir = path.join(DATA, "jobs", previewJobId);
  fs.mkdirSync(previewJobDir, { recursive: true });
  fs.writeFileSync(path.join(previewJobDir, "job.json"), JSON.stringify({ id: previewJobId }));
  const configPath = path.join(previewJobDir, "config.json");
  fs.writeFileSync(configPath, JSON.stringify({ media: { preview_failure_test: { source_url: "https://8.8.8.8/not-an-image" } } }));
  const originalFetch = global.fetch;
  global.fetch = (input, options) => new URL(input).hostname === "8.8.8.8"
    ? Promise.resolve(new Response("not an image", { status: 200, headers: { "content-type": "text/html" } }))
    : originalFetch(input, options);
  try {
    const failed = await originalFetch(`${base}/deal-rooms/${previewJobId}/media/preview_failure_test`);
    assert.equal(failed.status, 502);
    assert.deepEqual(await failed.json(), { error: "preview image unavailable" });
    assert.equal((await originalFetch(`${base}/health`)).status, 200);
  } finally {
    global.fetch = originalFetch;
    fs.rmSync(previewJobDir, { recursive: true, force: true });
  }
});

test("overrides rebuild the deal room and keep it waiting for approval", async () => {
  const r = await req("POST", `/deal-rooms/${reviewJob}/overrides`, { dining_intro: "Sea Salt and Azul Bar, both with Atlantic views." });
  assert.equal(r.status, 202);
  const j = await waitFor(reviewJob, ["awaiting_approval"]);
  assert.equal(j.history.at(-1).event, "overrides");
  const tpl = (await req("GET", `/deal-rooms/${reviewJob}/artifacts/template.json`)).json;
  assert.match(JSON.stringify(tpl), /Sea Salt and Azul Bar, both with Atlantic views\./);
});

test("approve: backup, save the 6 editor keys, read back and verify", async () => {
  await hippo("/__reset", {});
  assert.equal((await req("POST", `/deal-rooms/${reviewJob}/approve`)).status, 202);
  const j = await waitFor(reviewJob, ["succeeded"]);
  assert.equal(j.stages.verify.status, "ok");
  const log = await hippo("/__log");
  assert.deepEqual(log.map(x => x.method), ["GET", "PATCH", "GET"]);
  assert.deepEqual(log[1].keys, ["multiple_logo_top", "sections", "sideNav", "header_cta_buttons", "settings", "contactInfo"]);
  assert.match(log[1].ctype, /x-www-form-urlencoded/);
  const page = await hippo("/__page/1605252");
  const tpl = (await req("GET", `/deal-rooms/${reviewJob}/artifacts/template.json`)).json;
  assert.deepEqual(page.sections.map(s => s.id), tpl.sections.map(s => s.id));
  const backup = (await req("GET", `/deal-rooms/${reviewJob}/artifacts/backup.json`)).json;
  assert.ok(backup.template.sections.length > 0);
  assert.equal((await req("POST", `/deal-rooms/${reviewJob}/approve`)).status, 409, "cannot approve twice");
});

test("rollback restores the page from the backup", async () => {
  const before = JSON.parse(fs.readFileSync(path.join(__dirname, "fixtures", "base_template.json"), "utf8"));
  assert.equal((await req("POST", `/deal-rooms/${reviewJob}/rollback`)).status, 202);
  await waitFor(reviewJob, ["rolled_back"]);
  const page = await hippo("/__page/1605252");
  assert.deepEqual(page.sections, before.sections);
});

test("a bad save is caught by verify and rolled back automatically", async () => {
  await hippo("/__reset", {}); await hippo("/__mode", { drop_section_on_save: true });
  const { json } = await req("POST", "/deal-rooms", { hotel_url: SITE, hippo_page_id: "1605252", load: "api", mode: "auto", scrape });
  const j = await waitFor(json.id, ["failed"]);
  await hippo("/__mode", {});
  assert.equal(j.stages.capture.status, "skipped");
  assert.match(j.error, /verify/); assert.equal(j.rolled_back, true);
  const before = JSON.parse(fs.readFileSync(path.join(__dirname, "fixtures", "base_template.json"), "utf8"));
  assert.deepEqual((await hippo("/__page/1605252")).sections, before.sections);
});

test("Cvent flow uses only Cvent merge fields (manual load gives a loader file)", async () => {
  const { json } = await req("POST", "/deal-rooms", { hotel_url: SITE, flow: "cvent", scrape });
  await waitFor(json.id, ["awaiting_approval"]);
  await req("POST", `/deal-rooms/${json.id}/approve`);
  const j = await waitFor(json.id, ["succeeded"]);
  assert.equal(j.stages.load.status, "ok");
  const tpl = (await req("GET", `/deal-rooms/${json.id}/artifacts/template.json`)).json;
  const t = tokens(tpl);
  assert.ok(t.length > 40 && t.every(x => /^\$\{(response\.|request\.|User\.)/.test(x)), "non-Cvent token: " + t.filter(x => !/^\$\{(response\.|request\.|User\.)/.test(x)));
  const loader = (await req("GET", `/deal-rooms/${json.id}/artifacts/loader.js`)).text;
  assert.match(loader, /response\.hotel_name/);
  assert.equal((await req("GET", `/deal-rooms/${json.id}/artifacts/verify.json`)).json.status, "skipped");
});

test("optional AI copy: invented facts are rejected, grounded rewrites are kept", async () => {
  process.env.LLM_BASE_URL = `http://127.0.0.1:${LLM_PORT}`;
  try {
    const { json } = await req("POST", "/deal-rooms", { hotel_url: SITE, scrape });
    await waitFor(json.id, ["awaiting_approval"]);
    const copy = (await req("GET", `/deal-rooms/${json.id}/artifacts/copy.json`)).json;
    assert.deepEqual(copy.ai_polished, ["dining_intro"]);
    const log = (await req("GET", `/deal-rooms/${json.id}/logs`)).text;
    assert.match(log, /AI rewrite of overview rejected .*(900|Michelin|Porto)/);
  } finally { delete process.env.LLM_BASE_URL; }
});

test("load=api without Hippo credentials fails safely at load", async () => {
  const saved = process.env.HIPPO_API_TOKEN; delete process.env.HIPPO_API_TOKEN;
  try {
    await hippo("/__reset", {});
    const { json } = await req("POST", "/deal-rooms", { hotel_url: SITE, hippo_page_id: "1605252", load: "api", mode: "auto", scrape });
    const j = await waitFor(json.id, ["failed", "awaiting_approval"]);
    if (j.status === "awaiting_approval") { await req("POST", `/deal-rooms/${json.id}/approve`); }
    const f = await waitFor(json.id, ["failed"]);
    assert.match(f.error, /load: .*HIPPO_API_TOKEN/);
    assert.equal((await hippo("/__log")).filter(x => x.method === "PATCH").length, 0);
  } finally { process.env.HIPPO_API_TOKEN = saved; }
});

test("a second load keeps the original backup, so rollback returns to the page before the agent", async () => {
  await hippo("/__reset", {});
  const before = JSON.parse(fs.readFileSync(path.join(__dirname, "fixtures", "base_template.json"), "utf8"));
  const { json } = await req("POST", "/deal-rooms", { hotel_url: SITE, hippo_page_id: "1562033", load: "api", scrape });
  await waitFor(json.id, ["awaiting_approval"]); await req("POST", `/deal-rooms/${json.id}/approve`); await waitFor(json.id, ["succeeded"]);
  await req("POST", `/deal-rooms/${json.id}/overrides`, { overview: "Second version of the overview for the Grand Azure Resort Lisbon." });
  await waitFor(json.id, ["awaiting_approval"]); await req("POST", `/deal-rooms/${json.id}/approve`); await waitFor(json.id, ["succeeded"]);
  assert.match(JSON.stringify(await hippo("/__page/1562033")), /Second version of the overview/);
  await req("POST", `/deal-rooms/${json.id}/rollback`); await waitFor(json.id, ["rolled_back"]);
  assert.deepEqual((await hippo("/__page/1562033")).sections, before.sections);
});

test("queued work resumes after a restart", async () => {
  const paused = new Jobs({ dataDir: DATA, maxConcurrent: 1, recover: false });
  paused.pump = () => {};                                          // simulate a server that dies before running anything
  const j = paused.create({ hotel_url: SITE, scrape }, "restart-test");
  assert.equal(paused.get(j.id).status, "queued");
  const fresh = new Jobs({ dataDir: DATA, maxConcurrent: 1 });     // recover() re-enqueues it
  const t0 = Date.now();
  while (!["awaiting_approval", "failed"].includes(fresh.get(j.id).status) && Date.now() - t0 < 120000) await sleep(300);
  assert.equal(fresh.get(j.id).status, "awaiting_approval");
});

test("cancel stops a queued or running job", async () => {
  const { json } = await req("POST", "/deal-rooms", { hotel_url: SITE });
  await sleep(300);
  assert.equal((await req("POST", `/deal-rooms/${json.id}/cancel`)).status, 200);
  await sleep(1500);
  assert.equal((await req("GET", "/deal-rooms/" + json.id)).json.status, "cancelled");
});
