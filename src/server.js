// Deal Room Agent HTTP API (node:http, no dependencies).
//   GET  /health                               (no auth)
//   GET  /                                     review UI (the page itself is public; every API call it makes needs a key)
//   POST /deal-rooms                           {hotel_url, hippo_page_id?, flow?, mode?, load?, label?, overrides?, scrape?}
//   GET  /deal-rooms                           recent jobs
//   GET  /deal-rooms/:id                       job status (stages, error, history)
//   GET  /deal-rooms/:id/logs?from=<byte>      run log (text)
//   GET  /deal-rooms/:id/artifacts             list
//   GET  /deal-rooms/:id/artifacts/:name       review.html, template.json, loader.js, report.json, facts.json, ...
//   POST /deal-rooms/:id/overrides             JSON patch merged into overrides.json, then rebuild + review
//   POST /deal-rooms/:id/approve               load into Hippo (or hand out loader.js) + verify
//   POST /deal-rooms/:id/rollback              restore the backup taken before loading
//   POST /deal-rooms/:id/cancel
// Auth: header "x-api-key: <key>" or "Authorization: Bearer <key>" (keys in API_KEYS).
"use strict";
const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const dns = require("node:dns").promises;
const net = require("node:net");
const cfg = require("./config");
const { Jobs, validateInput, ARTIFACTS, artifactPath } = require("./jobs");

const sha = s => crypto.createHash("sha256").update(String(s)).digest();
const KEYS = cfg.apiKeys.map(k => ({ name: k.name, h: sha(k.key) }));

function who(req) {
  if (!KEYS.length && cfg.allowNoAuth) return "anonymous";
  const raw = req.headers["x-api-key"] || (req.headers.authorization || "").replace(/^Bearer\s+/i, "");
  if (!raw) return null;
  const h = sha(raw);
  const hit = KEYS.find(k => crypto.timingSafeEqual(k.h, h));
  return hit ? hit.name : null;
}

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
      try { resolve(JSON.parse(Buffer.concat(chunks).toString("utf8"))); } catch { reject(Object.assign(new Error("body is not valid JSON"), { status: 400 })); }
    });
    req.on("error", reject);
  });
}

function publicJob(j) { const { ...o } = j; return o; }

function isPrivateAddress(address) {
  if (net.isIP(address) === 4) {
    const p = address.split(".").map(Number);
    return p[0] === 0 || p[0] === 10 || p[0] === 127 || p[0] >= 224 ||
      (p[0] === 100 && p[1] >= 64 && p[1] <= 127) ||
      (p[0] === 169 && p[1] === 254) ||
      (p[0] === 172 && p[1] >= 16 && p[1] <= 31) ||
      (p[0] === 192 && (p[1] === 168 || (p[1] === 0 && p[2] === 0) || (p[1] === 0 && p[2] === 2) || (p[1] === 88 && p[2] === 99))) ||
      (p[0] === 198 && (p[1] === 18 || p[1] === 19));
  }
  const v = address.toLowerCase();
  return v === "::1" || v.startsWith("fc") || v.startsWith("fd") || v.startsWith("fe80:") || v.startsWith("::ffff:127.");
}

async function assertPublicHttps(url) {
  if (url.protocol !== "https:" || !url.hostname || url.hostname === "localhost") throw Object.assign(new Error("invalid preview image"), { status: 400 });
  const addresses = await dns.lookup(url.hostname, { all: true, verbatim: true });
  if (!addresses.length || addresses.some(x => isPrivateAddress(x.address))) throw Object.assign(new Error("unsafe preview image host"), { status: 400 });
}

async function previewImage(dir, key, res) {
  const config = JSON.parse(fs.readFileSync(path.join(dir, "config.json"), "utf8"));
  const source = config.media && config.media[key] && config.media[key].source_url;
  if (typeof source !== "string") throw Object.assign(new Error("preview image not found"), { status: 404 });
  const sourcePage = typeof config._generated_from === "string" && config._generated_from.startsWith("https://") ? config._generated_from : undefined;
  let url = new URL(source);
  for (let redirects = 0; redirects < 4; redirects++) {
    await assertPublicHttps(url);
    const upstream = await fetch(url, { redirect: "manual", signal: AbortSignal.timeout(15000), headers: {
      "accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
      "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
      ...(sourcePage ? { referer: sourcePage } : {}),
    } });
    if ([301, 302, 303, 307, 308].includes(upstream.status)) {
      const to = upstream.headers.get("location");
      if (!to) throw Object.assign(new Error("preview image redirect has no location"), { status: 502 });
      url = new URL(to, url);
      continue;
    }
    if (!upstream.ok || !/^image\//i.test(upstream.headers.get("content-type") || ""))
      return send(res, 502, { error: "preview image unavailable" });
    const length = +(upstream.headers.get("content-length") || 0);
    if (length > 12 * 1024 * 1024) throw Object.assign(new Error("preview image is too large"), { status: 413 });
    const image = Buffer.from(await upstream.arrayBuffer());
    if (image.length > 12 * 1024 * 1024) throw Object.assign(new Error("preview image is too large"), { status: 413 });
    return send(res, 200, image, upstream.headers.get("content-type"), { "cache-control": "private, max-age=3600" });
  }
  throw Object.assign(new Error("too many preview image redirects"), { status: 502 });
}

function webhook(job) {
  if (!cfg.webhookUrl) return;
  const body = JSON.stringify({ event: "deal_room.status", id: job.id, status: job.status, stage: job.stage, hotel: job.hotel || null, hotel_url: job.input.hotel_url, error: job.error || null });
  fetch(cfg.webhookUrl, { method: "POST", headers: { "content-type": "application/json" }, body, signal: AbortSignal.timeout(5000) }).catch(() => {});
}

function createServer(jobs) {
  jobs.on("change", webhook);
  const ui = fs.readFileSync(path.join(__dirname, "ui.html"));
  return http.createServer(async (req, res) => {
    const url = new URL(req.url, "http://x");
    const parts = url.pathname.replace(/\/+$/, "").split("/").filter(Boolean);
    try {
      if (req.method === "GET" && url.pathname === "/health") return send(res, 200, { ok: true, queue: jobs.queue.length, running: jobs.running.size });
      if (req.method === "GET" && (url.pathname === "/" || url.pathname === "/ui"))
        return send(res, 200, ui, "text/html; charset=utf-8", { "content-security-policy": "default-src 'self'; img-src * data: blob:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; frame-src blob:", "x-frame-options": "DENY" });
      if (parts[0] !== "deal-rooms") return send(res, 404, { error: "not found" });
      if (parts.length === 1) {
        const user = who(req);
        if (!user) return send(res, 401, { error: "missing or invalid API key (x-api-key or Authorization: Bearer)" });
        if (req.method === "GET") return send(res, 200, { jobs: jobs.list(+url.searchParams.get("limit") || 50) });
        if (req.method === "POST") {
          const b = await readBody(req); const errs = validateInput(b);
          if (errs.length) return send(res, 400, { error: "invalid input", details: errs });
          const job = jobs.create(b, user);
          return send(res, 202, { id: job.id, status: job.status, links: { self: `/deal-rooms/${job.id}`, logs: `/deal-rooms/${job.id}/logs`, review: `/deal-rooms/${job.id}/artifacts/review.html` } });
        }
        return send(res, 405, { error: "method not allowed" });
      }

      const id = parts[1];
      const job = jobs.get(id);
      if (!job) return send(res, 404, { error: "job not found" });
      const dir = jobs.jobDir(id);

      // review.html is a sandboxed, unauthenticated document, so its image
      // previews cannot attach an API key. This route exposes only a named
      // image already present in that job's config and rejects private hosts.
      if (parts[2] === "media" && parts.length === 4 && req.method === "GET") return await previewImage(dir, parts[3], res);

      const user = who(req);
      if (!user) return send(res, 401, { error: "missing or invalid API key (x-api-key or Authorization: Bearer)" });

      if (parts.length === 2 && req.method === "GET") return send(res, 200, publicJob(job));
      if (parts[2] === "logs" && req.method === "GET") {
        const p = path.join(dir, "run.log"); const from = Math.max(0, +url.searchParams.get("from") || 0);
        const buf = fs.existsSync(p) ? fs.readFileSync(p) : Buffer.alloc(0);
        return send(res, 200, buf.subarray(from), "text/plain; charset=utf-8", { "x-log-size": String(buf.length) });
      }
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
        if (parts[2] === "approve") return send(res, 202, publicJob(jobs.approve(id, user)));
        if (parts[2] === "rollback") return send(res, 202, publicJob(jobs.rollback(id, user)));
        if (parts[2] === "cancel") return send(res, 200, publicJob(jobs.cancel(id, user)));
        if (parts[2] === "overrides") return send(res, 202, publicJob(jobs.overrides(id, await readBody(req), user)));
      }
      return send(res, 404, { error: "not found" });
    } catch (e) {
      return send(res, e.status || 500, { error: e.status ? e.message : "internal error" });
    }
  });
}

if (require.main === module) {
  if (!cfg.apiKeys.length && !cfg.allowNoAuth) { console.error("✖ Set API_KEYS (e.g. API_KEYS=ops:<long random key>) or ALLOW_NO_AUTH=1 for local testing."); process.exit(1); }
  const jobs = new Jobs();
  createServer(jobs).listen(cfg.port, cfg.host, () => console.log(`Deal Room Agent listening on http://${cfg.host}:${cfg.port} (data: ${cfg.dataDir}, concurrency ${cfg.maxConcurrent})`));
  for (const sig of ["SIGINT", "SIGTERM"]) process.on(sig, () => { for (const c of jobs.running.values()) try { c && c.kill("SIGTERM"); } catch {} process.exit(0); });
}

module.exports = { createServer };
