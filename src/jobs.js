// Job store + runner. Each job lives in data/jobs/<id>/ (job.json, run.log and the stage artifacts).
// Stages are Python processes started with an argument list (never through a shell).
"use strict";
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const { spawn } = require("node:child_process");
const { EventEmitter } = require("node:events");
const cfg = require("./config");

const PIPELINE = ["intake", "capture", "convert", "facts", "copy", "build", "review"];
const AFTER_APPROVAL = ["load", "verify"];
const REBUILD = ["build", "review"];

// Artifacts a caller may download (anything else in the job folder is private).
const ARTIFACTS = {
  "review.html": "text/html; charset=utf-8", "template.json": "application/json", "loader.js": "text/javascript; charset=utf-8",
  "report.json": "application/json", "review.json": "application/json", "facts.json": "application/json", "copy.json": "application/json",
  "config.json": "application/json", "scrape.json": "application/json", "overrides.json": "application/json", "load.json": "application/json",
  "verify.json": "application/json", "backup.json": "application/json", "rollback.json": "application/json", "result.json": "application/json",
  "page.png": "image/png", "run.log": "text/plain; charset=utf-8",
};
const artifactPath = (dir, name) => path.join(dir, name === "page.png" ? "capture/page.png" : name);

const FLOW = ["email", "cvent"], MODE = ["review", "auto"], LOAD = ["manual", "api"];

function validateInput(b) {
  const errs = [];
  if (!b || typeof b !== "object") return ["body must be a JSON object"];
  const url = b.hotel_url;
  if (typeof url !== "string" || url.length > 2048) errs.push("hotel_url is required (string)");
  else { try { const u = new URL(url); if (!/^https?:$/.test(u.protocol)) errs.push("hotel_url must be http(s)"); } catch { errs.push("hotel_url is not a valid URL"); } }
  if (b.hippo_page_id !== undefined && b.hippo_page_id !== null && b.hippo_page_id !== "" && !/^\d{1,12}$/.test(String(b.hippo_page_id))) errs.push("hippo_page_id must be digits");
  if (b.flow !== undefined && !FLOW.includes(b.flow)) errs.push("flow must be one of " + FLOW.join("|"));
  if (b.mode !== undefined && !MODE.includes(b.mode)) errs.push("mode must be one of " + MODE.join("|"));
  if (b.load !== undefined && !LOAD.includes(b.load)) errs.push("load must be one of " + LOAD.join("|"));
  if ((b.load === "api") && !b.hippo_page_id) errs.push("load=api needs hippo_page_id");
  if (b.overrides !== undefined && (typeof b.overrides !== "object" || Array.isArray(b.overrides))) errs.push("overrides must be an object");
  if (b.scrape !== undefined && (typeof b.scrape !== "object" || !Array.isArray(b.scrape.images))) errs.push("scrape must be the JSON downloaded by scrape_hotel.js");
  if (b.label !== undefined && (typeof b.label !== "string" || b.label.length > 200)) errs.push("label must be a short string");
  return errs;
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
    this.dir = path.join(opts.dataDir || cfg.dataDir, "jobs");
    this.maxConcurrent = opts.maxConcurrent || cfg.maxConcurrent;
    fs.mkdirSync(this.dir, { recursive: true });
    this.queue = [];               // [{id, stages, then}]
    this.running = new Map();      // id -> child process
    if (opts.recover !== false) this.recover();
  }

  jobDir(id) {
    if (!/^dr_[a-z0-9]+_[a-f0-9]{8}$/.test(id)) throw Object.assign(new Error("bad job id"), { status: 400 });
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
    return fs.readdirSync(this.dir).filter(n => n.startsWith("dr_")).map(n => this.get(n)).filter(Boolean)
      .sort((a, b) => b.created_at.localeCompare(a.created_at)).slice(0, limit)
      .map(j => ({ id: j.id, status: j.status, stage: j.stage, hotel: j.hotel || null, hotel_url: j.input.hotel_url, flow: j.input.flow, created_at: j.created_at, created_by: j.created_by }));
  }
  log(id, text) { fs.appendFileSync(path.join(this.jobDir(id), "run.log"), text); }

  create(body, who = "cli") {
    const id = "dr_" + Date.now().toString(36) + "_" + crypto.randomBytes(4).toString("hex");
    const d = path.join(this.dir, id); fs.mkdirSync(path.join(d, "capture"), { recursive: true });
    const input = { hotel_url: body.hotel_url.trim(), hippo_page_id: body.hippo_page_id ? String(body.hippo_page_id) : "", flow: body.flow || "email",
      mode: body.mode || "review", load: body.load || "manual", label: body.label || "" };
    if (body.scrape) fs.writeFileSync(path.join(d, "scrape.json"), JSON.stringify(body.scrape));
    if (body.overrides) fs.writeFileSync(path.join(d, "overrides.json"), JSON.stringify(body.overrides, null, 2));
    const job = { id, created_at: new Date().toISOString(), created_by: who, status: "queued", stage: null, input, scrape_uploaded: !!body.scrape,
      stages: Object.fromEntries([...PIPELINE, ...AFTER_APPROVAL].map(s => [s, { status: "pending" }])), history: [] };
    this.save(job);
    this.log(id, `# job ${id} created by ${who}\n# input ${JSON.stringify(input)}${body.scrape ? " (+ uploaded scrape)" : ""}\n`);
    this.enqueue(id, PIPELINE, "gate");
    return job;
  }

  approve(id, who) {
    const job = this.mustGet(id);
    if (job.status !== "awaiting_approval") throw Object.assign(new Error(`job is ${job.status}, not awaiting_approval`), { status: 409 });
    job.history.push({ at: new Date().toISOString(), event: "approved", by: who }); job.status = "queued"; this.save(job);
    this.enqueue(id, AFTER_APPROVAL, "done");
    return job;
  }

  overrides(id, patch, who) {
    const job = this.mustGet(id);
    if (!["awaiting_approval", "failed", "succeeded"].includes(job.status) || !job.stages.convert || job.stages.convert.status !== "ok")
      throw Object.assign(new Error(`overrides can be applied after the convert stage has finished and the job is idle (job is ${job.status})`), { status: 409 });
    if (!patch || typeof patch !== "object" || Array.isArray(patch)) throw Object.assign(new Error("overrides must be a JSON object"), { status: 400 });
    const p = path.join(this.jobDir(id), "overrides.json");
    const cur = fs.existsSync(p) ? JSON.parse(fs.readFileSync(p, "utf8")) : {};
    const merged = deepMerge(cur, patch);
    fs.writeFileSync(p, JSON.stringify(merged, null, 2));
    job.history.push({ at: new Date().toISOString(), event: "overrides", by: who, keys: Object.keys(patch) });
    for (const s of [...REBUILD, ...AFTER_APPROVAL]) job.stages[s] = { status: "pending" };
    job.status = "queued"; delete job.error; this.save(job);
    this.enqueue(id, REBUILD, "gate");
    return job;
  }

  rollback(id, who) {
    const job = this.mustGet(id);
    if (this.running.has(id) || this.queue.some(q => q.id === id)) throw Object.assign(new Error("job is busy"), { status: 409 });
    if (!fs.existsSync(path.join(this.jobDir(id), "backup.json"))) throw Object.assign(new Error("no backup for this job (it was never loaded through the API)"), { status: 409 });
    job.history.push({ at: new Date().toISOString(), event: "rollback requested", by: who }); job.status = "queued"; job.stages.rollback = { status: "pending" }; this.save(job);
    this.enqueue(id, ["rollback"], "rolled_back");
    return job;
  }

  cancel(id, who) {
    const job = this.mustGet(id);
    const qi = this.queue.findIndex(q => q.id === id);
    if (qi >= 0) this.queue.splice(qi, 1);
    const child = this.running.get(id);
    if (child) { child.__cancelled = true; child.kill("SIGTERM"); setTimeout(() => { try { child.kill("SIGKILL"); } catch {} }, 5000); }
    if (!child && qi < 0 && !["queued", "running", "awaiting_approval"].includes(job.status)) throw Object.assign(new Error(`job is ${job.status}`), { status: 409 });
    job.status = "cancelled"; job.history.push({ at: new Date().toISOString(), event: "cancelled", by: who }); this.save(job); this.emit("change", job);
    return job;
  }

  mustGet(id) { const j = this.get(id); if (!j) throw Object.assign(new Error("job not found"), { status: 404 }); return j; }

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
      job.stage = stage; job.stages[stage] = { status: "running", started_at: new Date().toISOString() }; this.save(job); this.emit("change", job);
      this.log(id, `\n== ${stage}\n`);
      const r = await this.spawnStage(id, stage);
      job = this.get(id);
      if (job.status === "cancelled") return;
      job.stages[stage] = { ...job.stages[stage], status: r.code === 0 ? "ok" : "failed", ended_at: new Date().toISOString(), exit_code: r.code, ...(r.timedOut ? { note: "timed out" } : {}) };
      if (r.code !== 0) {
        const last = (r.lastLine || "").replace(/^✖\s*/, "");
        job.status = "failed"; job.error = `${stage}: ${last || (r.timedOut ? "timed out" : "exit " + r.code)}`;
        if (stage === "verify" && fs.existsSync(path.join(this.jobDir(id), "rollback.json"))) job.rolled_back = true;
        this.save(job); this.writeResult(job); this.emit("change", job); return;
      }
      this.save(job);
    }
    job = this.get(id); job.stage = null;
    if (then === "gate") {
      const rv = this.readJson(id, "review.json") || {};
      if (rv.needs_review) { job.status = "awaiting_approval"; this.save(job); this.writeResult(job); this.emit("change", job); return; }
      job.history.push({ at: new Date().toISOString(), event: "auto-approved (mode=auto, no high-severity items)" }); this.save(job);
      return this.runStages({ id, stages: AFTER_APPROVAL, then: "done" });
    }
    job.status = then === "rolled_back" ? "rolled_back" : "succeeded";
    this.save(job); this.writeResult(job); this.emit("change", job);
  }

  readJson(id, name) { try { return JSON.parse(fs.readFileSync(path.join(this.jobDir(id), name), "utf8")); } catch { return null; } }

  writeResult(job) {
    const rep = this.readJson(job.id, "report.json") || {}, rv = this.readJson(job.id, "review.json") || {}, vf = this.readJson(job.id, "verify.json");
    job.hotel = rep.hotel || job.hotel || null;
    const d = this.jobDir(job.id);
    const result = { id: job.id, status: job.status, error: job.error || null, hotel: job.hotel, input: job.input,
      sections: rep.sections || [], merge_fields: rep.unique_tokens, warnings: rep.warnings || [], high_severity: rv.high_severity || [], verify: vf,
      artifacts: Object.keys(ARTIFACTS).filter(n => fs.existsSync(artifactPath(d, n))) };
    fs.writeFileSync(path.join(d, "result.json"), JSON.stringify(result, null, 2));
    this.save(job);
  }

  spawnStage(id, stage) {
    return new Promise(resolve => {
      const env = {}; for (const k of cfg.stageEnvKeys) if (process.env[k] !== undefined) env[k] = process.env[k];
      env.PYTHONUNBUFFERED = "1"; env.PYTHONIOENCODING = "utf-8";
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

  // After a restart: jobs that were running are marked failed (their stage can be retried with overrides/approve); queued ones resume.
  recover() {
    for (const n of fs.existsSync(this.dir) ? fs.readdirSync(this.dir) : []) {
      if (!n.startsWith("dr_")) continue;
      const j = this.get(n); if (!j) continue;
      if (j.status === "running") { j.status = "failed"; j.error = `server restarted during ${j.stage}`; this.save(j); }
      else if (j.status === "queued") {
        if (j.pending && Array.isArray(j.pending.stages)) this.enqueue(j.id, j.pending.stages, j.pending.then);
        else { j.status = "failed"; j.error = "server restarted while queued"; this.save(j); }
      }
    }
  }
}

module.exports = { Jobs, validateInput, ARTIFACTS, artifactPath, PIPELINE };
