// Job store + runner. Each job lives in data/jobs/<id>/ (job.json, run.log, audit.jsonl and the stage artifacts).
// Stages are Python processes started with an argument list (never through a shell).
// Exit code 0 = ok, 3 = transient failure (retried with bounded exponential backoff), anything else = permanent failure.
"use strict";
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const { spawn } = require("node:child_process");
const { EventEmitter } = require("node:events");
const cfg = require("./config");
const audit = require("./audit");
const { ExceptionQueue } = require("./exceptions");
const { Hotels } = require("./hotels");

const PIPELINE = ["intake", "capture", "convert", "facts", "copy", "profile", "plan", "assets", "build", "map", "qa", "review"];
const AFTER_APPROVAL = ["publish_gate", "load", "verify"];
const REBUILD = ["profile", "plan", "assets", "build", "map", "qa", "review"];
const ACTIVE = new Set(["queued", "running", "awaiting_approval"]);
const TERMINAL = new Set(["succeeded", "failed", "cancelled", "rolled_back"]);
const RETRY_ON_TIMEOUT = new Set(["capture", "assets", "load", "verify", "rollback"]);   // network stages: a timeout may be transient
const STAGE_CATEGORY = { intake: "INVALID_INPUT", capture: "SOURCE_UNAVAILABLE", assets: "ASSET_MISSING", qa: "RENDER_FAILURE", publish_gate: "PUBLISH_FAILURE", load: "PUBLISH_FAILURE", verify: "PUBLISH_FAILURE", rollback: "PUBLISH_FAILURE" };
const DELIVERY = { status: "not_configured", detail: "PDF export, share links and email/CRM delivery have no verified Hippo integration yet. The deal room is saved to the Hippo page (or handed over as loader.js); nothing is sent to recipients and no opens or views are tracked." };

// Artifacts a caller may download (anything else in the job folder is private).
const ARTIFACTS = {
  "review.html": "text/html; charset=utf-8", "template.json": "application/json", "loader.js": "text/javascript; charset=utf-8",
  "report.json": "application/json", "review.json": "application/json", "facts.json": "application/json", "copy.json": "application/json",
  "config.json": "application/json", "scrape.json": "application/json", "overrides.json": "application/json", "load.json": "application/json",
  "verify.json": "application/json", "backup.json": "application/json", "rollback.json": "application/json", "result.json": "application/json",
  "profile.json": "application/json", "profile_validation.json": "application/json", "customization.json": "application/json", "plan.json": "application/json",
  "assets.json": "application/json", "mapping.json": "application/json", "qa.json": "application/json", "publication.json": "application/json",
  "audit.jsonl": "application/x-ndjson; charset=utf-8", "page.png": "image/png", "run.log": "text/plain; charset=utf-8",
};
const artifactPath = (dir, name) => path.join(dir, name === "page.png" ? "capture/page.png" : name);

const FLOW = ["email", "cvent"], MODE = ["review", "auto"], LOAD = ["manual", "api"];
const TRACKING = /^(utm_|gclid|gbraid|fbclid|cm_mmc|mktgvar|clk_src|partner|trso|srb_|hmguid)/i;
const err = (message, status, extra = {}) => Object.assign(new Error(message), { status, ...extra });
const sha = s => crypto.createHash("sha256").update(s).digest("hex");

function validateInput(b) {
  const errs = [];
  if (!b || typeof b !== "object") return ["body must be a JSON object"];
  const url = b.hotel_url;
  if (typeof url !== "string" || url.length > 2048) errs.push("hotel_url is required (string)");
  else { try { const u = new URL(url); if (!/^https?:$/.test(u.protocol)) errs.push("hotel_url must be http(s)"); if (u.username || u.password) errs.push("hotel_url must not contain credentials"); } catch { errs.push("hotel_url is not a valid URL"); } }
  if (b.hippo_page_id !== undefined && b.hippo_page_id !== null && b.hippo_page_id !== "" && !/^\d{1,12}$/.test(String(b.hippo_page_id))) errs.push("hippo_page_id must be digits");
  if (b.flow !== undefined && !FLOW.includes(b.flow)) errs.push("flow must be one of " + FLOW.join("|"));
  if (b.mode !== undefined && !MODE.includes(b.mode)) errs.push("mode must be one of " + MODE.join("|"));
  if (b.load !== undefined && !LOAD.includes(b.load)) errs.push("load must be one of " + LOAD.join("|"));
  if ((b.load === "api") && !b.hippo_page_id) errs.push("load=api needs hippo_page_id");
  if (b.overrides !== undefined && (typeof b.overrides !== "object" || b.overrides === null || Array.isArray(b.overrides))) errs.push("overrides must be an object");
  if (b.scrape !== undefined && (typeof b.scrape !== "object" || b.scrape === null || !Array.isArray(b.scrape.images))) errs.push("scrape must be the JSON downloaded by scrape_hotel.js");
  if (b.label !== undefined && (typeof b.label !== "string" || b.label.length > 200)) errs.push("label must be a short string");
  if (b.opportunity_id !== undefined && b.opportunity_id !== "" && !/^[A-Za-z0-9_.:\-]{1,64}$/.test(String(b.opportunity_id))) errs.push("opportunity_id may only contain letters, digits and _ . : - (max 64)");
  if (b.customization !== undefined && (typeof b.customization !== "object" || b.customization === null || Array.isArray(b.customization))) errs.push("customization must be an object");
  if (b.idempotency_key !== undefined && !/^[A-Za-z0-9_.:\-]{1,128}$/.test(String(b.idempotency_key))) errs.push("idempotency_key may only contain letters, digits and _ . : - (max 128)");
  for (const k of ["save_customization", "force"]) if (b[k] !== undefined && typeof b[k] !== "boolean") errs.push(`${k} must be true or false`);
  if (b.save_customization && !b.customization) errs.push("save_customization needs a customization to save");
  return errs;
}

function stageEnv(extra = {}) {
  const env = {}; for (const k of cfg.stageEnvKeys) if (process.env[k] !== undefined) env[k] = process.env[k];
  return { ...env, PYTHONUNBUFFERED: "1", PYTHONIOENCODING: "utf-8", ...extra };
}

// The customization schema lives in one place (agent/customize.py); the API asks it instead of keeping a second copy.
function validateCustomization(c) {
  return new Promise(resolve => {
    if (c === undefined || c === null) return resolve({ ok: true, errors: [], customization: {}, version: null });
    let out = "";
    const child = spawn(cfg.python, [path.join(cfg.ROOT, "agent", "customize.py"), "--validate"], { cwd: cfg.ROOT, env: stageEnv(), shell: false, stdio: ["pipe", "pipe", "ignore"] });
    const t = setTimeout(() => child.kill("SIGKILL"), 10000);
    child.stdout.on("data", d => { out += d; });
    child.on("error", e => { clearTimeout(t); resolve({ ok: false, errors: ["could not validate customization: " + e.message] }); });
    child.on("close", () => { clearTimeout(t); try { resolve(JSON.parse(out)); } catch { resolve({ ok: false, errors: ["customization validator failed to run"] }); } });
    child.stdin.on("error", () => {});
    child.stdin.end(JSON.stringify(c));
  });
}

function stable(v) {
  if (Array.isArray(v)) return "[" + v.map(stable).join(",") + "]";
  if (v && typeof v === "object") return "{" + Object.keys(v).sort().map(k => JSON.stringify(k) + ":" + stable(v[k])).join(",") + "}";
  return JSON.stringify(v === undefined ? null : v);
}
function normUrl(u) {
  try { const x = new URL(String(u).trim()); x.hash = ""; for (const k of [...x.searchParams.keys()]) if (TRACKING.test(k)) x.searchParams.delete(k); return x.href; } catch { return String(u).trim(); }
}

function deepMerge(base, over) {
  for (const [k, v] of Object.entries(over || {})) {
    if (v && typeof v === "object" && !Array.isArray(v) && base[k] && typeof base[k] === "object" && !Array.isArray(base[k])) deepMerge(base[k], v);
    else base[k] = v;
  }
  return base;
}

class Jobs extends EventEmitter {
  constructor(opts = {}) {
    super();
    this.dataDir = opts.dataDir || cfg.dataDir;
    this.dir = path.join(this.dataDir, "jobs");
    this.hotelsDir = path.join(this.dataDir, "hotels");
    this.maxConcurrent = opts.maxConcurrent || cfg.maxConcurrent;
    fs.mkdirSync(this.dir, { recursive: true });
    this.exceptions = new ExceptionQueue(path.join(this.dataDir, "exceptions"));
    this.hotels = new Hotels(this.hotelsDir);
    this.queue = [];               // [{id, stages, then}]
    this.running = new Map();      // id -> child process
    this.timers = new Map();       // id -> pending retry backoff {t, res}
    this.counters = { deduplicated: 0, retries_scheduled: 0 };
    if (opts.recover !== false) this.recover();
  }

  jobDir(id) {
    if (!/^dr_[a-z0-9]+_[a-f0-9]{8}$/.test(id)) throw err("bad job id", 400);
    return path.join(this.dir, id);
  }
  get(id) {
    let d; try { d = this.jobDir(id); } catch { return null; }
    try { return JSON.parse(fs.readFileSync(path.join(d, "job.json"), "utf8")); } catch { return null; }
  }
  save(job) {
    job.updated_at = new Date().toISOString();
    const p = path.join(this.jobDir(job.id), "job.json");
    fs.writeFileSync(p + ".tmp", JSON.stringify(job, null, 2)); fs.renameSync(p + ".tmp", p);
  }
  list(limit = 50) {
    return this.findJobs(() => true).sort((a, b) => b.created_at.localeCompare(a.created_at)).slice(0, limit)
      .map(j => ({ id: j.id, status: j.status, stage: j.stage, hotel: j.hotel || null, hotel_id: (j.profile && j.profile.hotel_id) || j.input.hotel_id || null, hotel_url: j.input.hotel_url, flow: j.input.flow,
        qa_outcome: j.qa_outcome || null, template_family: j.template_family || null, created_at: j.created_at, created_by: j.created_by }));
  }
  findJobs(pred) {
    const out = [];
    for (const n of fs.readdirSync(this.dir)) { if (!n.startsWith("dr_")) continue; const j = this.get(n); if (j && pred(j)) out.push(j); }
    return out;
  }
  log(id, text) { fs.appendFileSync(path.join(this.jobDir(id), "run.log"), text); }
  audit(id, event, by, data = {}) { audit.append(path.join(this.jobDir(id), "audit.jsonl"), { event, by, job_id: id, ...data }); }
  auditLog(id, limit) { this.jobDir(id); return audit.read(path.join(this.jobDir(id), "audit.jsonl"), limit); }

  idempotencyKey(b) {
    return sha(stable({ url: normUrl(b.hotel_url), page: String(b.hippo_page_id || ""), flow: b.flow || "email", mode: b.mode || "review", load: b.load || "manual", opp: b.opportunity_id || "",
      customization: b.customization || null, save: !!b.save_customization, overrides: b.overrides || null, scrape: b.scrape ? sha(stable(b.scrape)) : null }));
  }

  // Same request, same result (PDF §10.3): returns the job that already answers it instead of starting a duplicate.
  createIdempotent(body, who = "cli") {
    const key = this.idempotencyKey(body), clientKey = body.idempotency_key ? `${who}:${body.idempotency_key}` : null;
    const newest = (a, b) => b.created_at.localeCompare(a.created_at);
    if (!body.force) {
      let hit;
      if (clientKey) {
        hit = this.findJobs(j => j.idempotency && j.idempotency.client_key === clientKey).sort(newest)[0];
        if (hit && hit.idempotency.key !== key) throw err(`idempotency_key was already used with different parameters (job ${hit.id})`, 422, { conflicting_job: hit.id });
      } else {
        const now = Date.now();
        hit = this.findJobs(j => j.idempotency && j.idempotency.key === key && (ACTIVE.has(j.status) || (j.status === "succeeded" && now - Date.parse(j.updated_at) < cfg.idempotencyWindowMs))).sort(newest)[0];
      }
      if (hit) { this.counters.deduplicated++; this.audit(hit.id, "deduplicated", who, { reason: clientKey ? "idempotency_key" : "identical request" }); return { job: hit, deduplicated: true }; }
    }
    // never two jobs writing the same Hippo page at once
    if (body.load === "api" && body.hippo_page_id) {
      const busy = this.findJobs(j => j.input.load === "api" && j.input.hippo_page_id === String(body.hippo_page_id) && ACTIVE.has(j.status))[0];
      if (busy) throw err(`Hippo page ${body.hippo_page_id} is already being handled by job ${busy.id} (${busy.status}): approve, cancel or finish it first`, 409, { conflicting_job: busy.id });
    }
    return { job: this.create(body, who, { key, client_key: clientKey }), deduplicated: false };
  }

  create(body, who = "cli", idem = null) {
    const id = "dr_" + Date.now().toString(36) + "_" + crypto.randomBytes(4).toString("hex");
    const d = path.join(this.dir, id); fs.mkdirSync(path.join(d, "capture"), { recursive: true });
    const input = { hotel_url: body.hotel_url.trim(), hippo_page_id: body.hippo_page_id ? String(body.hippo_page_id) : "", flow: body.flow || "email",
      mode: body.mode || "review", load: body.load || "manual", label: body.label || "", opportunity_id: body.opportunity_id ? String(body.opportunity_id) : "" };
    if (body.customization && Object.keys(body.customization).length) input.customization = body.customization;
    if (body.save_customization) input.save_customization = true;
    if (body.scrape) fs.writeFileSync(path.join(d, "scrape.json"), JSON.stringify(body.scrape));
    if (body.overrides) fs.writeFileSync(path.join(d, "overrides.json"), JSON.stringify(body.overrides, null, 2));
    const job = { id, created_at: new Date().toISOString(), created_by: who, status: "queued", stage: null, input, scrape_uploaded: !!body.scrape, attempt_count: 0, retries: 0,
      idempotency: idem || { key: this.idempotencyKey(body), client_key: null },
      stages: Object.fromEntries([...PIPELINE, ...AFTER_APPROVAL].map(s => [s, { status: "pending" }])), history: [] };
    this.save(job);
    this.log(id, `# job ${id} created by ${who}\n# input ${JSON.stringify({ ...input, customization: input.customization ? "(see customization.json)" : undefined })}${body.scrape ? " (+ uploaded scrape)" : ""}\n`);
    this.audit(id, "job_created", who, { hotel_url: input.hotel_url, flow: input.flow, mode: input.mode, load: input.load, opportunity_id: input.opportunity_id || null, hippo_page_id: input.hippo_page_id || null });
    this.enqueue(id, PIPELINE, "gate");
    return job;
  }

  approve(id, who, notes = "") {
    const job = this.mustGet(id);
    if (job.status !== "awaiting_approval") throw err(`job is ${job.status}, not awaiting_approval`, 409);
    job.history.push({ at: new Date().toISOString(), event: "approved", by: who, ...(notes ? { notes: String(notes).slice(0, 500) } : {}) }); job.status = "queued"; this.save(job);
    this.audit(id, "approved", who, { notes: notes ? String(notes).slice(0, 500) : undefined, qa_outcome: job.qa_outcome });
    for (const x of this.exceptions.closeFor(id, who, `approved by ${who} with this item acknowledged`, x => x.source === "review_gate")) this.audit(id, "exception_resolved", who, { exception_id: x.exception_id });
    this.enqueue(id, AFTER_APPROVAL, "done");
    return job;
  }

  overrides(id, patch, who) {
    const job = this.mustGet(id);
    if (!["awaiting_approval", "failed", "succeeded"].includes(job.status) || !job.stages.convert || job.stages.convert.status !== "ok")
      throw err(`overrides can be applied after the convert stage has finished and the job is idle (job is ${job.status})`, 409);
    if (!patch || typeof patch !== "object" || Array.isArray(patch)) throw err("overrides must be a JSON object", 400);
    const p = path.join(this.jobDir(id), "overrides.json");
    const cur = fs.existsSync(p) ? JSON.parse(fs.readFileSync(p, "utf8")) : {};
    const merged = deepMerge(cur, patch);
    fs.writeFileSync(p, JSON.stringify(merged, null, 2));
    job.history.push({ at: new Date().toISOString(), event: "overrides", by: who, keys: Object.keys(patch) });
    for (const s of [...REBUILD, ...AFTER_APPROVAL]) job.stages[s] = { status: "pending" };
    job.status = "queued"; delete job.error; delete job.failed_stage; delete job.failure_reason; this.save(job);
    this.audit(id, "overrides_applied", who, { keys: Object.keys(patch) });
    this.exceptions.markInReview(id, who, "overrides applied: revalidating");
    this.enqueue(id, REBUILD, "gate");
    return job;
  }

  // Resume from the stage that failed (PDF §12.3): stage outputs are kept, so a failed load never rebuilds a valid proposal.
  retry(id, who) {
    const job = this.mustGet(id);
    if (job.status !== "failed") throw err(`only a failed job can be retried (job is ${job.status})`, 409);
    if (this.running.has(id) || this.queue.some(q => q.id === id)) throw err("job is busy", 409);
    if ((job.retries || 0) >= cfg.maxManualRetries) throw err(`this job has already been retried ${job.retries} times: start a new job instead`, 409);
    let stage = job.failed_stage;
    if (!stage) throw err("the job did not record which stage failed", 409);
    if (stage === "verify" && job.rolled_back) stage = "load";                // the page was restored: save it again before verifying
    const inMain = PIPELINE.includes(stage), list = inMain ? PIPELINE : AFTER_APPROVAL;
    if (!inMain && !AFTER_APPROVAL.includes(stage)) throw err(`stage ${stage} cannot be retried`, 409);
    if (inMain && stage === "capture" && job.scrape_uploaded) throw err("capture was skipped for this job", 409);
    const todo = list.slice(list.indexOf(stage));
    for (const s of [...todo, ...(inMain ? AFTER_APPROVAL : [])]) job.stages[s] = { status: "pending" };
    job.retries = (job.retries || 0) + 1; job.status = "queued"; delete job.error; delete job.failed_stage; delete job.failure_reason; delete job.rolled_back;
    job.history.push({ at: new Date().toISOString(), event: "retry requested", by: who, from_stage: stage }); this.save(job);
    this.audit(id, "retry_requested", who, { from_stage: stage, manual_retries: job.retries });
    this.exceptions.markInReview(id, who, `retry from ${stage}`);
    this.enqueue(id, todo, inMain ? "gate" : "done");
    return job;
  }

  rollback(id, who) {
    const job = this.mustGet(id);
    if (this.running.has(id) || this.queue.some(q => q.id === id)) throw err("job is busy", 409);
    if (!fs.existsSync(path.join(this.jobDir(id), "backup.json"))) throw err("no backup for this job (it was never loaded through the API)", 409);
    job.history.push({ at: new Date().toISOString(), event: "rollback requested", by: who }); job.status = "queued"; job.stages.rollback = { status: "pending" }; this.save(job);
    this.audit(id, "rollback_requested", who);
    this.enqueue(id, ["rollback"], "rolled_back");
    return job;
  }

  cancel(id, who) {
    const job = this.mustGet(id);
    const qi = this.queue.findIndex(q => q.id === id);
    if (qi >= 0) this.queue.splice(qi, 1);
    const child = this.running.get(id);
    const waiting = this.timers.get(id);
    if (waiting) { clearTimeout(waiting.t); this.timers.delete(id); waiting.res(); }
    if (child) { child.__cancelled = true; child.kill("SIGTERM"); setTimeout(() => { try { child.kill("SIGKILL"); } catch {} }, 5000); }
    if (!child && !waiting && qi < 0 && !["queued", "running", "awaiting_approval"].includes(job.status)) throw err(`job is ${job.status}`, 409);
    job.status = "cancelled"; job.history.push({ at: new Date().toISOString(), event: "cancelled", by: who }); this.save(job); this.emit("change", job);
    this.audit(id, "cancelled", who);
    this.exceptions.closeFor(id, who, "job cancelled");
    return job;
  }

  mustGet(id) { const j = this.get(id); if (!j) throw err("job not found", 404); return j; }

  enqueue(id, stages, then) {
    const j = this.get(id); if (j) { j.pending = { stages, then }; this.save(j); }   // lets a restart resume queued work
    this.queue.push({ id, stages, then }); setImmediate(() => this.pump());
  }

  pump() {
    while (this.running.size < this.maxConcurrent && this.queue.length) {
      const item = this.queue.shift();
      this.running.set(item.id, null);
      this.runStages(item).catch(e => { this.log(item.id, `\n✖ runner error: ${e.stack || e}\n`); }).finally(() => { this.running.delete(item.id); this.pump(); });
    }
  }

  backoffMs(attempt, retryAfterSec) {
    const exp = Math.min(cfg.retryMaxMs, cfg.retryBaseMs * 2 ** (attempt - 1));
    return Math.min(cfg.retryMaxMs, Math.max(exp * (0.8 + Math.random() * 0.4), (retryAfterSec || 0) * 1000));
  }
  sleep(id, ms) { return new Promise(res => { const t = setTimeout(() => { this.timers.delete(id); res(); }, ms); this.timers.set(id, { t, res }); }); }

  readStageError(id, stage) {
    try { const e = JSON.parse(fs.readFileSync(path.join(this.jobDir(id), "stage_error.json"), "utf8")); return e.stage === stage ? e : null; } catch { return null; }
  }

  async runStages({ id, stages, then }) {
    let job = this.get(id);
    if (!job || job.status === "cancelled") return;
    job.status = "running"; delete job.pending; this.save(job); this.emit("change", job);
    for (const stage of stages) {
      job = this.get(id);
      if (job.status === "cancelled") return;
      if (stage === "capture" && job.scrape_uploaded) {
        job.stages.capture = { status: "skipped", note: "scrape.json uploaded by the caller (browser scraper)" }; this.save(job);
        this.log(id, `\n== capture: skipped (scrape uploaded)\n`); continue;
      }
      let r, se, attempt = 0;
      for (;;) {                                   // one attempt per pass; transient failures come back here after a backoff
        attempt++;
        job = this.get(id); if (job.status === "cancelled") return;
        job.stage = stage; job.attempt_count = (job.attempt_count || 0) + 1;
        job.stages[stage] = { status: "running", started_at: new Date().toISOString(), attempt }; this.save(job); this.emit("change", job);
        this.log(id, `\n== ${stage}${attempt > 1 ? ` (attempt ${attempt}/${cfg.retryMaxAttempts})` : ""}\n`);
        r = await this.spawnStage(id, stage);
        job = this.get(id);
        if (job.status === "cancelled") return;
        if (r.code === 0) break;
        se = this.readStageError(id, stage);
        const transient = r.code === 3 || (r.timedOut && RETRY_ON_TIMEOUT.has(stage));
        const asked = se && se.retry_after ? se.retry_after : 0;
        if (!transient || attempt >= cfg.retryMaxAttempts || asked * 1000 > cfg.retryMaxMs) break;
        const wait = this.backoffMs(attempt, asked);
        job.stages[stage] = { ...job.stages[stage], status: "retrying", exit_code: r.code, next_retry_at: new Date(Date.now() + wait).toISOString(), last_error: (se && se.message) || r.lastLine };
        this.save(job); this.emit("change", job); this.counters.retries_scheduled++;
        this.log(id, `\n↻ ${stage} failed with a transient error (${(se && se.message) || r.lastLine}); retrying in ${Math.round(wait / 100) / 10}s (attempt ${attempt + 1}/${cfg.retryMaxAttempts})\n`);
        this.audit(id, "retry_scheduled", "system", { stage, attempt, wait_ms: Math.round(wait), reason: (se && se.message) || r.lastLine });
        await this.sleep(id, wait);
      }
      job = this.get(id);
      if (job.status === "cancelled") return;
      job.stages[stage] = { ...job.stages[stage], status: r.code === 0 ? "ok" : "failed", ended_at: new Date().toISOString(), exit_code: r.code, attempts: attempt, ...(r.timedOut ? { note: "timed out" } : {}) };
      if (r.code !== 0) { this.failJob(job, stage, r, se, attempt); return; }
      this.afterStage(job, stage);
      this.save(job);
      this.audit(id, "stage_finished", "system", { stage, outcome: "ok", attempts: attempt, ms: Date.parse(job.stages[stage].ended_at) - Date.parse(job.stages[stage].started_at) });
      for (const x of this.exceptions.closeFor(id, "system", `stage ${stage} passed on retry`, x => x.source === "stage_failure" && x.failed_stage === stage)) this.audit(id, "exception_resolved", "system", { exception_id: x.exception_id });
    }
    job = this.get(id); job.stage = null;
    if (then === "gate") {
      const rv = this.readJson(id, "review.json") || {};
      if (rv.needs_review) {
        job.status = "awaiting_approval"; this.save(job); this.writeResult(job);
        this.reconcileReviewExceptions(job, rv);
        this.audit(id, "awaiting_approval", "system", { qa_outcome: rv.qa_outcome, items: (rv.items || []).length });
        this.emit("change", job); return;
      }
      job.history.push({ at: new Date().toISOString(), event: "auto-approved (mode=auto, no high-severity items)" }); this.save(job);
      this.audit(id, "auto_approved", "system", { qa_outcome: rv.qa_outcome });
      return this.runStages({ id, stages: AFTER_APPROVAL, then: "done" });
    }
    job.status = then === "rolled_back" ? "rolled_back" : "succeeded";
    this.save(job); this.writeResult(job); this.emit("change", job);
    this.audit(id, job.status === "succeeded" ? "job_succeeded" : "job_rolled_back", "system", { attempts: job.attempt_count });
  }

  // Facts about the job that later stages and the API want without opening artifacts.
  afterStage(job, stage) {
    const rj = n => this.readJson(job.id, n) || {};
    if (stage === "profile") { const p = rj("profile.json"); job.profile = { hotel_id: p.hotel_id, version: p.profile_version, status: p.profile_status }; job.hotel = p.property_name || job.hotel; }
    if (stage === "plan") { const p = rj("plan.json"); job.template_family = p.template_family; job.customization_version = p.customization_version; }
    if (stage === "qa") job.qa_outcome = rj("qa.json").outcome || null;
    if (stage === "publish_gate") { const p = rj("publication.json"); if (job.profile) job.profile.status = "approved"; job.approved_profile_version = p.profile_version; }
  }

  failJob(job, stage, r, se, attempts) {
    const last = (r.lastLine || "").replace(/^✖\s*/, "");
    const message = (se && se.message) || last || (r.timedOut ? "timed out" : "exit " + r.code);
    job.status = "failed"; job.failed_stage = stage; job.failure_reason = message; job.error = `${stage}: ${last || (r.timedOut ? "timed out" : "exit " + r.code)}`;
    job.stages[stage].error = { code: (se && se.code) || (r.timedOut ? "STAGE_TIMEOUT" : "STAGE_CRASHED"), category: (se && se.category) || STAGE_CATEGORY[stage] || "INTERNAL_ERROR", retryable: !!(se && se.retryable) || r.code === 3 };
    if (stage === "verify" && fs.existsSync(path.join(this.jobDir(job.id), "rollback.json"))) job.rolled_back = true;
    if (stage === "qa" || stage === "publish_gate") job.qa_outcome = (this.readJson(job.id, "qa.json") || {}).outcome || job.qa_outcome || null;
    this.save(job); this.writeResult(job);
    const e = job.stages[stage].error;
    const { exception } = this.exceptions.open({ job, stage, severity: "critical", category: e.category, code: e.code, message: attempts > 1 ? `${message} (after ${attempts} attempts)` : message,
      missing_fields: se && se.missing_fields, retry_eligible: true, details: se && se.details, source: "stage_failure" });
    job.exception_ids = [...new Set([...(job.exception_ids || []), exception.exception_id])]; this.save(job);
    this.audit(job.id, "stage_finished", "system", { stage, outcome: "failed", code: e.code, category: e.category, attempts });
    this.audit(job.id, "job_failed", "system", { stage, code: e.code, reason: message, attempts, exception_id: exception.exception_id });
    this.emit("exception", exception); this.emit("change", job);
  }

  // In mode=auto a review stop means automation could not proceed safely, so each reason is an exception. In mode=review a person
  // is looking at every job anyway: the items are on the review page and nothing is added to the queue.
  reconcileReviewExceptions(job, rv) {
    if (job.input.mode !== "auto") return;
    const codes = new Set();
    for (const it of rv.items || []) {
      codes.add(it.code);
      const { exception, created } = this.exceptions.open({ job, stage: "review", severity: "high", category: it.category, code: it.code, message: it.message, missing_fields: it.fields, retry_eligible: false, source: "review_gate" });
      job.exception_ids = [...new Set([...(job.exception_ids || []), exception.exception_id])];
      if (created) { this.audit(job.id, "exception_opened", "system", { exception_id: exception.exception_id, category: it.category, code: it.code }); this.emit("exception", exception); }
    }
    for (const x of this.exceptions.closeFor(job.id, "system", "no longer raised after revalidation", x => x.source === "review_gate" && !codes.has(x.error_code))) this.audit(job.id, "exception_resolved", "system", { exception_id: x.exception_id });
    this.save(job);
  }

  readJson(id, name) { try { return JSON.parse(fs.readFileSync(path.join(this.jobDir(id), name), "utf8")); } catch { return null; } }

  writeResult(job) {
    const rep = this.readJson(job.id, "report.json") || {}, rv = this.readJson(job.id, "review.json") || {}, vf = this.readJson(job.id, "verify.json");
    const qa = this.readJson(job.id, "qa.json"), plan = this.readJson(job.id, "plan.json");
    job.hotel = rep.hotel || job.hotel || null;
    const d = this.jobDir(job.id);
    const result = { id: job.id, status: job.status, error: job.error || null, failed_stage: job.failed_stage || null, failure_reason: job.failure_reason || null, hotel: job.hotel, input: job.input,
      hotel_id: (job.profile && job.profile.hotel_id) || null, profile: job.profile || null, opportunity_id: job.input.opportunity_id || null,
      template_family: plan && plan.template_family, customization_version: plan && plan.customization_version,
      sections: rep.sections || [], omitted_sections: plan ? plan.omitted.map(o => ({ section: o.title, reason: o.reason })) : [], unmet_requests: plan ? plan.unmet_requests : [],
      merge_fields: rep.unique_tokens, warnings: rep.warnings || [], high_severity: rv.high_severity || [],
      qa: qa ? { outcome: qa.outcome, counts: qa.counts, not_run: qa.not_run.map(n => n.check) } : null, verify: vf,
      attempts: { stage_attempts: job.attempt_count || 0, manual_retries: job.retries || 0 }, exceptions: job.exception_ids || [],
      delivery: DELIVERY, artifacts: Object.keys(ARTIFACTS).filter(n => fs.existsSync(artifactPath(d, n))) };
    fs.writeFileSync(path.join(d, "result.json"), JSON.stringify(result, null, 2));
    this.save(job);
  }

  spawnStage(id, stage) {
    return new Promise(resolve => {
      const env = stageEnv({ HOTELS_DIR: this.hotelsDir });
      const child = spawn(cfg.python, [path.join(cfg.ROOT, "agent", "stages.py"), stage, this.jobDir(id)], { cwd: cfg.ROOT, env, shell: false, stdio: ["ignore", "pipe", "pipe"] });
      this.running.set(id, child);
      let lastLine = "", timedOut = false;
      const onData = buf => { const s = buf.toString(); this.log(id, s); const ls = s.trim().split("\n").filter(Boolean); if (ls.length) lastLine = ls[ls.length - 1]; };
      child.stdout.on("data", onData); child.stderr.on("data", onData);
      const ms = cfg.stageTimeoutMs[stage] || cfg.stageTimeoutMs.default;
      const t = setTimeout(() => { timedOut = true; this.log(id, `\n✖ ${stage} timed out after ${ms / 1000}s\n`); child.kill("SIGTERM"); setTimeout(() => { try { child.kill("SIGKILL"); } catch {} }, 5000); }, ms);
      child.on("error", e => { clearTimeout(t); this.log(id, `✖ could not start ${cfg.python}: ${e.message}\n`); resolve({ code: 127, lastLine: e.message }); });
      child.on("close", code => { clearTimeout(t); resolve({ code: child.__cancelled ? 130 : (code ?? 1), lastLine, timedOut }); });
    });
  }

  // After a restart: jobs that were running are marked failed with an exception (retry resumes at that stage); queued ones resume.
  recover() {
    for (const n of fs.existsSync(this.dir) ? fs.readdirSync(this.dir) : []) {
      if (!n.startsWith("dr_")) continue;
      const j = this.get(n); if (!j) continue;
      if (j.status === "running") {
        j.status = "failed"; j.error = `server restarted during ${j.stage}`; j.failed_stage = j.stage; j.failure_reason = j.error;
        if (j.stage && j.stages[j.stage]) j.stages[j.stage] = { ...j.stages[j.stage], status: "failed", error: { code: "SERVER_RESTARTED", category: "INTERNAL_ERROR", retryable: true } };
        this.save(j);
        if (j.stage) { const { exception } = this.exceptions.open({ job: j, stage: j.stage, severity: "high", category: "INTERNAL_ERROR", code: "SERVER_RESTARTED", message: j.error, retry_eligible: true, source: "stage_failure" }); j.exception_ids = [exception.exception_id]; this.save(j); }
        this.audit(j.id, "job_failed", "system", { stage: j.stage, code: "SERVER_RESTARTED" });
      } else if (j.status === "queued") {
        if (j.pending && Array.isArray(j.pending.stages)) this.enqueue(j.id, j.pending.stages, j.pending.then);
        else { j.status = "failed"; j.error = "server restarted while queued"; j.failure_reason = j.error; this.save(j); }
      }
    }
  }

  // ---- monitoring (PDF §16.2): the raw numbers; thresholds and alerts are set during the pilot ----
  metrics() {
    const jobs = this.findJobs(() => true), by = {}, stages = {}, codes = {};
    let completedAuto = 0, completedManual = 0;
    for (const j of jobs) {
      by[j.status] = (by[j.status] || 0) + 1;
      for (const [s, v] of Object.entries(j.stages || {})) {
        const m = stages[s] = stages[s] || { ok: 0, failed: 0, skipped: 0, total_ms: 0 };
        if (v.status === "ok") { m.ok++; m.total_ms += Date.parse(v.ended_at) - Date.parse(v.started_at); } else if (v.status === "failed") { m.failed++; if (v.error) codes[v.error.code] = (codes[v.error.code] || 0) + 1; } else if (v.status === "skipped") m.skipped++;
      }
      if (j.status === "succeeded") { const manual = (j.history || []).some(h => h.event === "approved" || h.event === "overrides" || h.event === "retry requested"); manual ? completedManual++ : completedAuto++; }
    }
    for (const m of Object.values(stages)) { m.avg_ms = m.ok ? Math.round(m.total_ms / m.ok) : null; delete m.total_ms; }
    const ex = this.exceptions.all(), open = ex.filter(x => x.resolution_status === "open" || x.resolution_status === "in_review"), done = ex.filter(x => x.resolved_at);
    const byCat = {}; for (const x of open) byCat[x.category] = (byCat[x.category] || 0) + 1;
    const started = jobs.length - (by.cancelled || 0);
    return { queue_depth: this.queue.length, running: this.running.size, jobs: { total: jobs.length, by_status: by }, stages, failure_codes: codes,
      exceptions: { open: open.length, open_by_category: byCat, resolved: done.length, avg_resolution_ms: done.length ? Math.round(done.reduce((a, x) => a + (Date.parse(x.resolved_at) - Date.parse(x.created_at)), 0) / done.length) : null },
      automation: { completed_without_manual_intervention: completedAuto, completed_with_manual_intervention: completedManual, started, rate: started ? Math.round(1000 * completedAuto / started) / 1000 : null },
      since_start: { duplicate_requests_deduplicated: this.counters.deduplicated, retries_scheduled: this.counters.retries_scheduled } };
  }

  // ---- retention (PDF §15): remove finished jobs older than `days`; jobs with unresolved exceptions, active jobs and the hotel store are kept ----
  purge(days, { dryRun = false } = {}) {
    if (!(days > 0)) throw err("days must be a positive number", 400);
    const cutoff = Date.now() - days * 86400000, removed = [], open = new Set(this.exceptions.list({ status: "unresolved", limit: 500 }).map(x => x.job_id));
    for (const j of this.findJobs(j => TERMINAL.has(j.status) && Date.parse(j.updated_at) < cutoff && !open.has(j.id))) {
      if (this.running.has(j.id) || this.queue.some(q => q.id === j.id)) continue;
      removed.push(j.id); if (!dryRun) fs.rmSync(this.jobDir(j.id), { recursive: true, force: true });
    }
    let exRemoved = 0;
    for (const x of this.exceptions.all()) if (x.resolved_at && Date.parse(x.resolved_at) < cutoff) { exRemoved++; if (!dryRun) fs.rmSync(this.exceptions.file(x.exception_id), { force: true }); }
    return { dry_run: dryRun, older_than_days: days, jobs_removed: removed.length, exceptions_removed: exRemoved, job_ids: removed };
  }
}

module.exports = { Jobs, validateInput, validateCustomization, ARTIFACTS, artifactPath, PIPELINE, AFTER_APPROVAL, REBUILD, ACTIVE };
