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
const { Jobs, validateInput, validateCustomization } = require("./jobs");

const USAGE = fs.readFileSync(__filename, "utf8").split("\n").filter(l => l.startsWith("//")).slice(1, 20).map(l => l.slice(3)).join("\n");
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
    if (j.status === "awaiting_approval" && flags.approve) { console.log("\n▶ approved from the command line (--approve)"); jobs.approve(j.id, "cli"); j = await follow(j.id); }
    process.exit(summary(j));
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
