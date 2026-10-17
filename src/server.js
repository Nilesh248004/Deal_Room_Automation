// Deal Room Agent HTTP API (node:http, no dependencies).
//   GET  /health                               (no auth)
//   GET  /                                     review UI (the page itself is public; every API call it makes needs a key)
//   POST /deal-rooms                           {hotel_url, hippo_page_id?, flow?, mode?, load?, label?, opportunity_id?, customization?, save_customization?,
//                                               idempotency_key?, force?, overrides?, scrape?}   202 new job · 200 an identical request already has one
//   GET  /deal-rooms                           recent jobs
//   GET  /deal-rooms/:id                       job status (stages, error, history)
//   GET  /deal-rooms/:id/logs?from=<byte>      run log (text)
//   GET  /deal-rooms/:id/audit                 audit trail (who did what, when)
//   GET  /deal-rooms/:id/artifacts             list
//   GET  /deal-rooms/:id/artifacts/:name       review.html, template.json, loader.js, plan.json, qa.json, mapping.json, profile.json, ...
//   POST /deal-rooms/:id/overrides             JSON patch merged into overrides.json, then rebuild + review
//   POST /deal-rooms/:id/approve               {notes?} publish gate, then load into Hippo (or hand out loader.js) + verify
//   POST /deal-rooms/:id/retry                 resume a failed job from the stage that failed
//   POST /deal-rooms/:id/rollback              restore the backup taken before loading
//   POST /deal-rooms/:id/cancel
//   GET  /hotels · /hotels/:id                 stored hotel profiles, saved customization, generations, regeneration_required
//   GET  /hotels/:id/profile[?version=N] · /hotels/:id/versions · /hotels/:id/audit
//   PUT  /hotels/:id/customization             replace the hotel's saved customization (validated)
//   GET  /exceptions[?status=&category=&hotel_id=&job_id=] · /exceptions/:id
//   POST /exceptions/:id/assign {reviewer} · /resolve {status?, notes} · /retry
//   GET  /metrics                              queue, stage success/latency, exceptions, automation rate
// Auth: header "x-api-key: <key>" or "Authorization: Bearer <key>" (keys in API_KEYS, each with a role: viewer, operator, admin).
"use strict";
const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const dns = require("node:dns").promises;
const net = require("node:net");
const cfg = require("./config");
const { Jobs, validateInput, validateCustomization, ARTIFACTS, artifactPath, stable } = require("./jobs");

const sha = s => crypto.createHash("sha256").update(String(s)).digest();
const KEYS = cfg.apiKeys.map(k => ({ name: k.name, role: k.role, h: sha(k.key) }));

// Returns { name, role } for a valid key, else null.
function who(req) {
  if (!KEYS.length && cfg.allowNoAuth) return { name: "anonymous", role: "admin" };
  const raw = req.headers["x-api-key"] || (req.headers.authorization || "").replace(/^Bearer\s+/i, "");
  if (!raw) return null;
  const h = sha(raw);
  const hit = KEYS.find(k => crypto.timingSafeEqual(k.h, h));
  return hit ? { name: hit.name, role: hit.role } : null;
}
const RANK = { viewer: 0, operator: 1, admin: 2 };
const requireRole = (user, role) => { if (RANK[user.role] < RANK[role]) throw Object.assign(new Error(`this needs the ${role} role (your key is ${user.role})`), { status: 403 }); };
const mentionsAssetRights = c => !!c && typeof c === "object" && Object.prototype.hasOwnProperty.call(c, "asset_rights");

function send(res, status, body, type = "application/json; charset=utf-8", extra = {}) {
  const data = typeof body === "string" || Buffer.isBuffer(body) ? body : JSON.stringify(body, null, 2);
  res.writeHead(status, { "content-type": type, "cache-control": "no-store", "x-content-type-options": "nosniff", ...extra });
  res.end(data);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let size = 0; const chunks = [];
    req.on("data", c => { size += c.length; if (size > cfg.maxBodyBytes) { reject(Object.assign(new Error("body too large"), { status: 413 })); req.destroy(); } else chunks.push(c); });
    req.on("end", () => {
      if (!size) return resolve({});
      try { resolve(JSON.parse(Buffer.concat(chunks).toString("utf8"), (k, v) => (k === "__proto__" ? undefined : v))); } catch { reject(Object.assign(new Error("body is not valid JSON"), { status: 400 })); }
    });
    req.on("error", reject);
  });
}

function publicJob(j) { const { ...o } = j; return o; }

function isPrivateAddress(address) {
  let a = String(address).toLowerCase();
  const mapped = a.match(/^::ffff:(\d+\.\d+\.\d+\.\d+)$/), hex = a.match(/^::ffff:([0-9a-f]{1,4}):([0-9a-f]{1,4})$/);
  if (mapped) a = mapped[1];                                               // ::ffff:10.0.0.1 is 10.0.0.1
  else if (hex) { const n = ((parseInt(hex[1], 16) << 16) | parseInt(hex[2], 16)) >>> 0; a = [n >>> 24, (n >>> 16) & 255, (n >>> 8) & 255, n & 255].join("."); }
  if (net.isIP(a) === 4) {
    const p = a.split(".").map(Number);
    return p[0] === 0 || p[0] === 10 || p[0] === 127 || p[0] >= 224 ||
      (p[0] === 100 && p[1] >= 64 && p[1] <= 127) ||
      (p[0] === 169 && p[1] === 254) ||
      (p[0] === 172 && p[1] >= 16 && p[1] <= 31) ||
      (p[0] === 192 && (p[1] === 168 || (p[1] === 0 && p[2] === 0) || (p[1] === 0 && p[2] === 2) || (p[1] === 88 && p[2] === 99))) ||
      (p[0] === 198 && (p[1] === 18 || p[1] === 19));
  }
  return a === "::" || a === "::1" || /^f[cd]/.test(a) || /^fe[89ab]/.test(a) || a.startsWith("ff");
}

async function assertPublicHttps(url) {
  if (url.protocol !== "https:" || !url.hostname || url.hostname === "localhost") throw Object.assign(new Error("invalid preview image"), { status: 400 });
  const addresses = await dns.lookup(url.hostname, { all: true, verbatim: true });
  if (!addresses.length || addresses.some(x => isPrivateAddress(x.address))) throw Object.assign(new Error("unsafe preview image host"), { status: 400 });
}

const PREVIEW_MAX = 12 * 1024 * 1024;
const RASTER = /^image\/(png|jpe?g|gif|webp|avif)$/i;     // never SVG: it can carry script and this is served from the API's origin
async function previewImage(dir, key, res) {
  const config = JSON.parse(fs.readFileSync(path.join(dir, "config.json"), "utf8"));
  const logo = /^logo_(\d{1,2})$/.exec(key);
  const source = logo ? (config.logos && config.logos[+logo[1] - 1] || {}).url : config.media && config.media[key] && config.media[key].source_url;
  if (typeof source !== "string") throw Object.assign(new Error("preview image not found"), { status: 404 });
  const sourcePage = typeof config._generated_from === "string" && config._generated_from.startsWith("https://") ? config._generated_from : undefined;
  let url = new URL(source);
  for (let redirects = 0; redirects < 4; redirects++) {
    await assertPublicHttps(url);
    const upstream = await fetch(url, { redirect: "manual", signal: AbortSignal.timeout(15000), headers: {
      "accept": "image/avif,image/webp,image/apng,image/*;q=0.8",
      "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
      ...(sourcePage ? { referer: sourcePage } : {}),
    } });
    if ([301, 302, 303, 307, 308].includes(upstream.status)) {
      const to = upstream.headers.get("location");
      try { await upstream.body?.cancel(); } catch {}
      if (!to) throw Object.assign(new Error("preview image redirect has no location"), { status: 502 });
      url = new URL(to, url);
      continue;
    }
    const type = (upstream.headers.get("content-type") || "").split(";")[0].trim();
    if (!upstream.ok || !RASTER.test(type)) { try { await upstream.body?.cancel(); } catch {} return send(res, 502, { error: "preview image unavailable" }); }
    if (+(upstream.headers.get("content-length") || 0) > PREVIEW_MAX) { try { await upstream.body?.cancel(); } catch {} throw Object.assign(new Error("preview image is too large"), { status: 413 }); }
    const reader = upstream.body.getReader(), chunks = []; let total = 0;     // stop reading at the cap instead of buffering whatever the host sends
    for (;;) {
      const { done, value } = await reader.read(); if (done) break;
      total += value.length;
      if (total > PREVIEW_MAX) { try { await reader.cancel(); } catch {} throw Object.assign(new Error("preview image is too large"), { status: 413 }); }
      chunks.push(value);
    }
    return send(res, 200, Buffer.concat(chunks), type, { "cache-control": "private, max-age=3600", "content-security-policy": "default-src 'none'; sandbox" });
  }
  throw Object.assign(new Error("too many preview image redirects"), { status: 502 });
}

function post(body) {
  if (!cfg.webhookUrl) return;
  fetch(cfg.webhookUrl, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body), signal: AbortSignal.timeout(5000) }).catch(() => {});
}
function webhook(job) {
  post({ event: "deal_room.status", id: job.id, status: job.status, stage: job.stage, hotel: job.hotel || null, hotel_url: job.input.hotel_url, error: job.error || null });
}
function exceptionHook(x) {   // "notify the configured team when necessary" (PDF §13.3)
  post({ event: "deal_room.exception", exception_id: x.exception_id, job_id: x.job_id, hotel: x.hotel_name, category: x.category, severity: x.severity, stage: x.failed_stage, message: x.description });
}

const UNAUTH = { error: "missing or invalid API key (x-api-key or Authorization: Bearer)" };

function createServer(jobs) {
  jobs.on("change", webhook);
  jobs.on("exception", exceptionHook);
  const ui = fs.readFileSync(path.join(__dirname, "ui.html"));
  const links = id => ({ self: `/deal-rooms/${id}`, logs: `/deal-rooms/${id}/logs`, audit: `/deal-rooms/${id}/audit`, review: `/deal-rooms/${id}/artifacts/review.html` });
  const handle = async (req, res) => {
    let url;
    try { url = new URL(req.url, "http://x"); } catch { return send(res, 400, { error: "bad request target" }); }
    let p = url.pathname; while (p.length > 1 && p.endsWith("/")) p = p.slice(0, -1);       // (a regex here is quadratic on a long run of slashes)
    const parts = p.split("/").filter(Boolean);
    const q = k => url.searchParams.get(k) || undefined;
    try {
      if (req.method === "GET" && url.pathname === "/health") return send(res, 200, { ok: true, queue: jobs.queue.length, running: jobs.running.size });
      if (req.method === "GET" && (url.pathname === "/" || url.pathname === "/ui"))
        return send(res, 200, ui, "text/html; charset=utf-8", { "content-security-policy": "default-src 'self'; img-src * data: blob:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; frame-src blob:", "x-frame-options": "DENY" });

      // review.html is a sandboxed, unauthenticated document, so its image
      // previews cannot attach an API key. This route exposes only a named
      // image already present in that job's config and rejects private hosts.
      if (parts[0] === "deal-rooms" && parts[2] === "media" && parts.length === 4 && req.method === "GET") {
        const job = jobs.get(parts[1]); if (!job) return send(res, 404, { error: "job not found" });
        return await previewImage(jobs.jobDir(parts[1]), parts[3], res);
      }

      if (!["deal-rooms", "hotels", "exceptions", "metrics"].includes(parts[0])) return send(res, 404, { error: "not found" });
      const user = who(req);
      if (!user) return send(res, 401, UNAUTH);
      if (req.method !== "GET") requireRole(user, "operator");
      const by = user.name;

      // ---------------- /metrics ----------------
      if (parts[0] === "metrics") return req.method === "GET" && parts.length === 1 ? send(res, 200, jobs.metrics()) : send(res, 404, { error: "not found" });

      // ---------------- /exceptions ----------------
      if (parts[0] === "exceptions") {
        if (parts.length === 1 && req.method === "GET") return send(res, 200, { exceptions: jobs.exceptions.list({ status: q("status"), category: q("category"), hotel_id: q("hotel_id"), job_id: q("job_id"), limit: q("limit") }) });
        const x = jobs.exceptions.get(parts[1] || ""); if (!x) return send(res, 404, { error: "exception not found" });
        if (parts.length === 2 && req.method === "GET") return send(res, 200, x);
        if (req.method === "POST" && parts.length === 3) {
          const b = await readBody(req);
          if (parts[2] === "assign") return send(res, 200, jobs.exceptions.assign(x.exception_id, b.reviewer, by));
          if (parts[2] === "resolve") { const r = jobs.exceptions.resolve(x.exception_id, b, by); jobs.audit(x.job_id, "exception_resolved", by, { exception_id: x.exception_id, status: r.resolution_status }); return send(res, 200, r); }
          if (parts[2] === "retry") {
            if (!x.retry_eligible) return send(res, 409, { error: "this exception is not retryable: fix the data with POST /deal-rooms/:id/overrides instead" });
            const job = jobs.retry(x.job_id, by); return send(res, 202, { exception: jobs.exceptions.get(x.exception_id), job: { id: job.id, status: job.status } });
          }
        }
        return send(res, 404, { error: "not found" });
      }

      // ---------------- /hotels ----------------
      if (parts[0] === "hotels") {
        if (parts.length === 1 && req.method === "GET") return send(res, 200, { hotels: jobs.hotels.list() });
        const id = parts[1], h = jobs.hotels.detail(id);
        if (!h) return send(res, 404, { error: "hotel not found" });
        if (parts.length === 2 && req.method === "GET") return send(res, 200, h);
        if (parts[2] === "profile" && req.method === "GET") { const p = jobs.hotels.profile(id, q("version")); return p ? send(res, 200, p) : send(res, 404, { error: "profile version not found" }); }
        if (parts[2] === "versions" && req.method === "GET") return send(res, 200, { versions: jobs.hotels.versions(id) });
        if (parts[2] === "audit" && req.method === "GET") return send(res, 200, { events: jobs.hotels.audit(id) });
        if (parts[2] === "customization" && req.method === "PUT") {
          const b = await readBody(req);
          // Brand-asset rights are an admin decision. Leaving them out keeps what is stored; sending them back unchanged (an operator editing
          // the rest of the JSON in the UI) is fine; changing or adding them needs an admin key.
          const stored = (h.customization || {}).asset_rights;
          if (mentionsAssetRights(b)) { if (stable(b.asset_rights) !== stable(stored)) requireRole(user, "admin"); }
          else if (stored) b.asset_rights = stored;
          const v = await validateCustomization(b);
          if (!v.ok) return send(res, 400, { error: "invalid customization", details: v.errors });
          return send(res, 200, jobs.hotels.saveCustomization(id, v.customization, v.version, by));
        }
        return send(res, 404, { error: "not found" });
      }

      // ---------------- /deal-rooms ----------------
      if (parts.length === 1) {
        if (req.method === "GET") return send(res, 200, { jobs: jobs.list(+q("limit") || 50) });
        if (req.method === "POST") {
          const b = await readBody(req);
          if (b && typeof b === "object" && !b.idempotency_key && req.headers["idempotency-key"]) b.idempotency_key = String(req.headers["idempotency-key"]);
          const errs = validateInput(b);
          if (errs.length) return send(res, 400, { error: "invalid input", details: errs });
          if (mentionsAssetRights(b.customization) || mentionsAssetRights((b.overrides || {}).customization)) requireRole(user, "admin");
          if (b.customization) { const v = await validateCustomization(b.customization); if (!v.ok) return send(res, 400, { error: "invalid customization", details: v.errors }); }
          if (b.overrides && b.overrides.customization !== undefined) { const v = await validateCustomization(b.overrides.customization); if (!v.ok) return send(res, 400, { error: "invalid customization in overrides", details: v.errors }); }
          const { job, deduplicated } = jobs.createIdempotent(b, by);
          return send(res, deduplicated ? 200 : 202, { id: job.id, status: job.status, deduplicated, links: links(job.id) });
        }
        return send(res, 405, { error: "method not allowed" });
      }

      const id = parts[1];
      const job = jobs.get(id);
      if (!job) return send(res, 404, { error: "job not found" });
      const dir = jobs.jobDir(id);

      if (parts.length === 2 && req.method === "GET") return send(res, 200, job);
      if (parts[2] === "logs" && req.method === "GET") {
        const p = path.join(dir, "run.log"); const from = Math.max(0, +url.searchParams.get("from") || 0);
        const buf = fs.existsSync(p) ? fs.readFileSync(p) : Buffer.alloc(0);
        return send(res, 200, buf.subarray(from), "text/plain; charset=utf-8", { "x-log-size": String(buf.length) });
      }
      if (parts[2] === "audit" && req.method === "GET") return send(res, 200, { events: jobs.auditLog(id, +q("limit") || 1000) });
      if (parts[2] === "artifacts" && req.method === "GET") {
        if (parts.length === 3) return send(res, 200, { artifacts: Object.keys(ARTIFACTS).filter(n => fs.existsSync(artifactPath(dir, n))) });
        const name = parts[3];
        if (!Object.prototype.hasOwnProperty.call(ARTIFACTS, name) || parts.length !== 4) return send(res, 404, { error: "unknown artifact" });
        const p = artifactPath(dir, name);
        if (!fs.existsSync(p)) return send(res, 404, { error: "artifact not produced yet" });
        const extra = name === "review.html" ? { "content-security-policy": "default-src 'none'; img-src * data:; style-src 'unsafe-inline'; sandbox" } : {};
        if (name === "loader.js") extra["content-disposition"] = `attachment; filename="deal-room-loader-${id}.js"`;
        return send(res, 200, fs.readFileSync(p), ARTIFACTS[name], extra);
      }
      if (req.method === "POST" && parts.length === 3) {
        if (parts[2] === "approve") { const b = await readBody(req); return send(res, 202, jobs.approve(id, by, b && b.notes)); }
        if (parts[2] === "retry") return send(res, 202, jobs.retry(id, by));
        if (parts[2] === "rollback") return send(res, 202, jobs.rollback(id, by));
        if (parts[2] === "cancel") return send(res, 200, jobs.cancel(id, by));
        if (parts[2] === "overrides") {
          const b = await readBody(req);
          if (mentionsAssetRights(b.customization)) requireRole(user, "admin");
          if (b.customization) {
            const v = await validateCustomization(b.customization);
            if (!v.ok) return send(res, 400, { error: "invalid customization", details: v.errors });
          }
          return send(res, 202, jobs.overrides(id, b, by));
        }
      }
      return send(res, 404, { error: "not found" });
    } catch (e) {
      return send(res, e.status || 500, e.status ? { error: e.message, ...(e.conflicting_job ? { conflicting_job: e.conflicting_job } : {}) } : { error: "internal error" });
    }
  };
  const server = http.createServer((req, res) => {
    handle(req, res).catch(() => { try { if (!res.headersSent) send(res, 500, { error: "internal error" }); else res.end(); } catch { /* the connection is gone */ } });
  });
  server.on("clientError", (e, socket) => { if (socket.writable) socket.end("HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n"); else socket.destroy(); });
  return server;
}

if (require.main === module) {
  if (!cfg.apiKeys.length && !cfg.allowNoAuth) { console.error("✖ Set API_KEYS (e.g. API_KEYS=ops:<long random key>) or ALLOW_NO_AUTH=1 for local testing."); process.exit(1); }
  const weak = cfg.apiKeys.filter(k => k.key.length < 16 || /^x+$/i.test(k.key) || /change[-_ ]?me|^(secret|password|test)/i.test(k.key));
  if (weak.length && process.env.ALLOW_WEAK_API_KEYS !== "1") { console.error(`✖ API key(s) too short or a placeholder: ${weak.map(k => k.name).join(", ")}. Use at least 16 random characters (openssl rand -hex 32), or ALLOW_WEAK_API_KEYS=1 for local testing.`); process.exit(1); }
  process.on("unhandledRejection", e => console.error("unhandled rejection:", e && e.stack || e));
  const jobs = new Jobs();
  createServer(jobs).listen(cfg.port, cfg.host, () => console.log(`Deal Room Agent listening on http://${cfg.host}:${cfg.port} (data: ${cfg.dataDir}, concurrency ${cfg.maxConcurrent}${cfg.retentionDays ? `, retention ${cfg.retentionDays} days` : ""})`));
  if (cfg.retentionDays) {
    const sweep = () => { try { const r = jobs.purge(cfg.retentionDays); if (r.jobs_removed || r.exceptions_removed) console.log(`retention: removed ${r.jobs_removed} job(s) and ${r.exceptions_removed} exception(s) older than ${cfg.retentionDays} days`); } catch (e) { console.error("retention sweep failed:", e.message); } };
    sweep(); setInterval(sweep, 24 * 3600 * 1000).unref();
  }
  for (const sig of ["SIGINT", "SIGTERM"]) process.on(sig, () => { for (const c of jobs.running.values()) try { c && c.kill("SIGTERM"); } catch {} process.exit(0); });
}

module.exports = { createServer, isPrivateAddress };
