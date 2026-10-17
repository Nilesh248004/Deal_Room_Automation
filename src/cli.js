#!/usr/bin/env node
// Deal Room Agent CLI: runs the same pipeline as the API, in this process (no server needed).
//
//   node src/cli.js run --url <hotel page> [--page <hippo page id>] [--flow email|cvent] [--mode review|auto]
//                       [--load manual|api] [--scrape <scrape.json>] [--overrides <file.json>] [--approve]
//                       [--customization <file.json>] [--save-customization] [--opportunity <id>] [--force]
//   node src/cli.js approve  <job id>
//   node src/cli.js overrides <job id> <file.json>
//   node src/cli.js retry    <job id>          (resume a failed job from the stage that failed)
//   node src/cli.js rollback <job id>
//   node src/cli.js scrape   --url <hotel page> [--from <file.json>|latest]   (saves the scrape to hotel_scraps/<hotel id>.json)
//   node src/cli.js photos   <job id> [--out <folder>]   (save the deal room's photos, named by key, to upload to Hippo)
//   node src/cli.js media    <job id> <hippo-media.json> (use the photos you uploaded: output of harvest_media.js)
//   node src/cli.js status   <job id>
//   node src/cli.js list
//   node src/cli.js exceptions [--status open|in_review|resolved|unresolved]
//   node src/cli.js hotels | hotel <hotel id>
//   node src/cli.js metrics
//   node src/cli.js purge --days <n> [--dry-run]
//
// Exit codes: 0 succeeded, 10 waiting for approval, 1 failed / cancelled, 64 usage error.
"use strict";
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const { spawnSync } = require("node:child_process");
const cfg = require("./config");
const { Jobs, validateInput, validateCustomization, stageEnv } = require("./jobs");

const USAGE = fs.readFileSync(__filename, "utf8").split("\n").filter(l => l.startsWith("//")).slice(1, 24).map(l => l.slice(3)).join("\n");
const [cmd, ...rest] = process.argv.slice(2);
const flags = {}, pos = [];
const BOOL = ["approve", "json", "save-customization", "force", "dry-run"];
for (let i = 0; i < rest.length; i++) {
  if (rest[i].startsWith("--")) { const k = rest[i].slice(2); if (BOOL.includes(k)) flags[k] = true; else flags[k] = rest[++i]; }
  else pos.push(rest[i]);
}
const die = (m, code = 64) => { console.error(m); process.exit(code); };
const readJson = f => { try { return JSON.parse(fs.readFileSync(path.resolve(f), "utf8")); } catch (e) { die(`✖ cannot read ${f}: ${e.message}`); } };

const jobs = new Jobs({ maxConcurrent: 1, recover: false });
const who = "cli:" + (process.env.USER || "local");

function follow(id) {
  const logPath = path.join(jobs.jobDir(id), "run.log");
  let from = fs.existsSync(logPath) ? fs.statSync(logPath).size : 0;
  const tick = () => {
    if (!fs.existsSync(logPath)) return;
    const size = fs.statSync(logPath).size;
    if (size > from) { const fd = fs.openSync(logPath, "r"); const b = Buffer.alloc(size - from); fs.readSync(fd, b, 0, b.length, from); fs.closeSync(fd); process.stdout.write(b); from = size; }
  };
  return new Promise(resolve => {
    setImmediate(() => {              // let the queue pick the job up first
      const t = setInterval(() => {
        tick();
        const j = jobs.get(id);
        if (["queued", "running"].includes(j.status) || jobs.running.has(id) || jobs.queue.some(q => q.id === id) || jobs.timers.has(id)) return;
        clearInterval(t); tick(); resolve(j);
      }, 200);
    });
  });
}

function summary(j) {
  const dir = jobs.jobDir(j.id);
  const rv = (() => { try { return JSON.parse(fs.readFileSync(path.join(dir, "review.json"), "utf8")); } catch { return null; } })();
  console.log(`\n────────\nJob ${j.id}: ${j.status.toUpperCase()}${j.error ? " (" + j.error + ")" : ""}${j.qa_outcome ? ` | QA ${j.qa_outcome}` : ""}${j.template_family ? ` | family ${j.template_family}` : ""}`);
  if (j.profile) console.log(`Hotel ${j.profile.hotel_id}, profile v${j.profile.version} (${j.profile.status})`);
  if (rv && rv.high_severity && rv.high_severity.length) console.log("Check before approving:\n  - " + rv.high_severity.join("\n  - "));
  const open = jobs.exceptions.forJob(j.id).filter(x => x.resolution_status === "open" || x.resolution_status === "in_review");
  if (open.length) console.log("Open exceptions:\n" + open.map(x => `  - ${x.exception_id} [${x.severity}] ${x.category}: ${x.description}${x.missing_fields.length ? " (missing: " + x.missing_fields.join(", ") + ")" : ""}`).join("\n"));
  const show = ["review.html", "loader.js", "template.json", "plan.json", "qa.json", "mapping.json", "profile.json", "report.json", "facts.json", "verify.json"].filter(n => fs.existsSync(path.join(dir, n)));
  if (show.length) console.log("Files:\n" + show.map(n => "  " + path.join(dir, n)).join("\n"));
  if (j.status === "awaiting_approval") console.log(`Next: open review.html, then  node src/cli.js approve ${j.id}   (or edit:  node src/cli.js overrides ${j.id} edits.json)`);
  if (j.status === "failed" && j.failed_stage) console.log(`Next: fix the cause, then  node src/cli.js retry ${j.id}   (or change the data:  node src/cli.js overrides ${j.id} edits.json)`);
  if (j.status === "succeeded" && j.input.load === "manual") console.log("Next: open the Hippo deal room editor, F12 → Console (or a DevTools Snippet), paste loader.js, press Enter.");
  return j.status === "succeeded" || j.status === "rolled_back" ? 0 : j.status === "awaiting_approval" ? 10 : 1;
}

(async () => {
  if (cmd === "run") {
    const body = { hotel_url: flags.url, hippo_page_id: flags.page, flow: flags.flow, mode: flags.mode, load: flags.load, label: flags.label, opportunity_id: flags.opportunity };
    for (const k of Object.keys(body)) if (body[k] === undefined) delete body[k];
    if (flags.scrape) body.scrape = readJson(flags.scrape);
    if (flags.overrides) body.overrides = readJson(flags.overrides);
    if (flags.customization) body.customization = readJson(flags.customization);
    if (flags["save-customization"]) body.save_customization = true;
    if (flags.force) body.force = true;
    if (flags["idempotency-key"]) body.idempotency_key = flags["idempotency-key"];
    const errs = validateInput(body); if (errs.length) die("✖ " + errs.join("\n✖ ") + "\n\n" + USAGE);
    if (body.customization) { const v = await validateCustomization(body.customization); if (!v.ok) die("✖ " + v.errors.join("\n✖ ")); }
    let created;
    try { created = jobs.createIdempotent(body, who); } catch (e) { die("✖ " + e.message, 1); }
    if (created.deduplicated) console.log(`↺ an identical request already has job ${created.job.id} (${created.job.status}); not starting a duplicate (use --force to run again)`);
    else console.log(`▶ job ${created.job.id}`);
    let j = created.deduplicated ? created.job : await follow(created.job.id);
    if (j.status === "awaiting_approval" && flags.approve) { console.log("\n▶ approved from the command line (--approve)"); jobs.approve(j.id, who); j = await follow(j.id); }
    process.exit(summary(j));
  }
  if (cmd === "scrape") {
    const url = flags.url || pos[0] || die(USAGE);
    const errs = validateInput({ hotel_url: url }); if (errs.length) die("✖ " + errs.join("\n✖ "));
    const outDir = process.env.HOTEL_SCRAPS_DIR ? path.resolve(process.env.HOTEL_SCRAPS_DIR) : path.join(cfg.ROOT, "hotel_scraps");
    const work = fs.mkdtempSync(path.join(os.tmpdir(), "hotel-scrape-"));          // a scratch job folder: the stages need one, the result goes to hotel_scraps
    const quit = (m, code = 1) => { fs.rmSync(work, { recursive: true, force: true }); die(m, code); };
    const stage = name => { const r = spawnSync(cfg.python, [path.join(cfg.ROOT, "agent", "stages.py"), name, work], { cwd: cfg.ROOT, env: stageEnv({ HOTELS_DIR: path.join(work, "hotels") }, name), shell: false, encoding: "utf8", maxBuffer: 1 << 26 }); process.stdout.write(r.stdout || ""); process.stderr.write(r.stderr || ""); return r.status === null ? 1 : r.status; };
    const why = () => { try { return JSON.parse(fs.readFileSync(path.join(work, "stage_error.json"), "utf8")); } catch { return {}; } };
    fs.writeFileSync(path.join(work, "job.json"), JSON.stringify({ input: { hotel_url: url, flow: "email", mode: "review", load: "manual" } }));
    if (stage("intake") !== 0) quit("✖ " + (why().message || "the hotel URL was not accepted"), 1);
    const input = JSON.parse(fs.readFileSync(path.join(work, "input.json"), "utf8"));
    let file, text;
    if (flags.from) {
      const downloads = process.env.DOWNLOADS_DIR ? path.resolve(process.env.DOWNLOADS_DIR) : path.join(os.homedir(), "Downloads");     // where your browser saves files
      file = flags.from === "latest"
        ? (fs.existsSync(downloads) ? fs.readdirSync(downloads).filter(n => /^hotel-scrape-.*\.json$/i.test(n)).map(n => path.join(downloads, n)).sort((a, b) => fs.statSync(b).mtimeMs - fs.statSync(a).mtimeMs)[0] : null)
        : path.resolve(flags.from.replace(/^~(?=\/|$)/, os.homedir()));
      if (!file || !fs.existsSync(file)) quit("✖ no scrape file found" + (flags.from === "latest" ? ` (looked for hotel-scrape-*.json in ${downloads})` : `: ${flags.from}`), 64);
      text = fs.readFileSync(file, "utf8");
      let data; try { data = JSON.parse(text); } catch (e) { quit(`✖ ${file} is not valid JSON: ${e.message}`, 64); }
      if (!data || !Array.isArray(data.images) || typeof data.url !== "string") quit("✖ that is not a scrape: it should be the file downloaded by agent/scrape_hotel.js", 64);
      const host = u => { try { return new URL(u).hostname.replace(/^www\./, "").toLowerCase(); } catch { return ""; } };
      if (host(data.url) !== host(input.hotel_url)) quit(`✖ this scrape was made on ${host(data.url) || "an unknown site"}, not ${host(input.hotel_url)}: scrape the hotel's own page`, 64);
      console.log(`▶ using ${file}`);
    } else {
      let code = 1;
      for (let attempt = 1; attempt <= cfg.retryMaxAttempts; attempt++) {
        code = stage("capture");
        if (code === 0 || code !== 3) break;                                                   // 3 = temporary (network, 429, 5xx): worth another go
        const wait = Math.min((why().retry_after || 0) * 1000 || cfg.retryBaseMs * 2 ** (attempt - 1), cfg.retryMaxMs);
        if (attempt < cfg.retryMaxAttempts) { console.log(`… temporary problem, trying again in ${Math.round(wait / 1000)}s (${attempt}/${cfg.retryMaxAttempts})`); Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, wait); }
      }
      if (code !== 0) quit("✖ " + (why().message || "capture failed") + (/refuses automated access|HTTP 40[13]/.test(why().message || "") ? `\n\nThis site blocks servers. Scrape it in your own browser (run agent/scrape_hotel.js on the hotel page: see the top of that file), then:\n  node src/cli.js scrape --url "${url}" --from latest` : ""), 1);
      file = path.join(work, "scrape.json"); text = fs.readFileSync(file, "utf8");
    }
    fs.mkdirSync(outDir, { recursive: true });
    const dest = path.join(outDir, input.hotel_id + ".json"), had = fs.existsSync(dest);
    fs.writeFileSync(dest + ".tmp", text); fs.renameSync(dest + ".tmp", dest);
    const d = JSON.parse(text);
    fs.rmSync(work, { recursive: true, force: true });
    const rel = path.relative(process.cwd(), dest) || dest;
    console.log(`\n✔ ${had ? "replaced" : "saved"} ${rel} (${Math.round(text.length / 1024)} KB, ${d.images.length} images, ${(d.subpages || []).length} sub-pages)`);
    console.log(`Next: node src/cli.js run --url "${input.hotel_url}" --page <hippo page id> --scrape ${rel}`);
    process.exit(0);
  }

  if (["photos", "media"].includes(cmd)) {
    const id = pos[0] || die(USAGE);
    if (!jobs.get(id)) die("✖ job not found: " + id, 1);
    const dir = jobs.jobDir(id), py = f => spawnSync(cfg.python, [path.join(cfg.ROOT, "agent", "photos.py"), ...f], { cwd: cfg.ROOT, env: stageEnv({ HOTELS_DIR: jobs.hotelsDir }, "photos"), shell: false, encoding: "utf8", maxBuffer: 1 << 24 });
    if (cmd === "photos") {
      const out = path.resolve(flags.out || path.join(dir, "photos")), r = py(["download", dir, out]);
      process.stdout.write(r.stdout || ""); process.stderr.write(r.stderr || "");
      if (r.status === 0) console.log("Next: upload these files into a carousel in the Hippo editor (keep the file names), click Save, run agent/browser_snippets/harvest_media.js in that tab, then\n  node src/cli.js media " + id + " <the downloaded hippo-media-….json>");
      process.exit(r.status === null ? 1 : r.status);
    }
    const file = pos[1] || die(USAGE), r = py(["adopt", dir, path.resolve(file)]);
    process.stderr.write(r.stderr || "");
    if (r.status !== 0) process.exit(r.status === null ? 1 : r.status);
    let patch; try { patch = JSON.parse(r.stdout); } catch { die("✖ could not read the matched photos", 1); }
    const p = follow(id);
    try { jobs.overrides(id, patch, who); } catch (e) { die("✖ " + e.message, 1); }
    process.exit(summary(await p));
  }

  if (["approve", "rollback", "overrides", "retry"].includes(cmd)) {
    const id = pos[0] || die(USAGE);
    if (!jobs.get(id)) die("✖ job not found: " + id, 1);
    const p = follow(id);
    try {
      if (cmd === "approve") jobs.approve(id, who);
      if (cmd === "rollback") jobs.rollback(id, who);
      if (cmd === "retry") jobs.retry(id, who);
      if (cmd === "overrides") {
        const patch = readJson(pos[1] || die(USAGE));
        if (patch.customization) { const v = await validateCustomization(patch.customization); if (!v.ok) die("✖ " + v.errors.join("\n✖ ")); }
        jobs.overrides(id, patch, who);
      }
    } catch (e) { die("✖ " + e.message, 1); }
    process.exit(summary(await p));
  }
  if (cmd === "status") { const j = jobs.get(pos[0] || die(USAGE)) || die("✖ job not found", 1); if (flags.json) console.log(JSON.stringify(j, null, 2)); else summary(j); process.exit(0); }
  if (cmd === "list") { for (const j of jobs.list(30)) console.log(`${j.id}  ${j.status.padEnd(18)} ${j.flow.padEnd(6)} ${(j.qa_outcome || "").padEnd(7)} ${j.hotel || j.hotel_url}`); process.exit(0); }
  if (cmd === "exceptions") {
    for (const x of jobs.exceptions.list({ status: flags.status || "unresolved" })) console.log(`${x.exception_id}  ${x.resolution_status.padEnd(10)} ${x.severity.padEnd(8)} ${x.category.padEnd(22)} ${x.job_id}  ${x.description}`);
    process.exit(0);
  }
  if (cmd === "hotels") { for (const h of jobs.hotels.list()) console.log(`${h.hotel_id}  v${h.latest_version} (approved ${h.approved_version ? "v" + h.approved_version : "none"})  ${h.property_name || ""}`); process.exit(0); }
  if (cmd === "hotel") { const h = jobs.hotels.detail(pos[0] || die(USAGE)) || die("✖ hotel not found", 1); console.log(JSON.stringify(h, null, 2)); process.exit(0); }
  if (cmd === "metrics") { console.log(JSON.stringify(jobs.metrics(), null, 2)); process.exit(0); }
  if (cmd === "purge") { if (!flags.days) die(USAGE); try { console.log(JSON.stringify(jobs.purge(+flags.days, { dryRun: !!flags["dry-run"] }), null, 2)); } catch (e) { die("✖ " + e.message, 1); } process.exit(0); }
  die(USAGE);
})();
