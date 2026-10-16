#!/usr/bin/env node
// Deal Room Agent CLI: runs the same pipeline as the API, in this process (no server needed).
//
//   node src/cli.js run --url <hotel page> [--page <hippo page id>] [--flow email|cvent] [--mode review|auto]
//                       [--load manual|api] [--scrape <scrape.json>] [--overrides <file.json>] [--approve]
//   node src/cli.js approve  <job id>
//   node src/cli.js overrides <job id> <file.json>
//   node src/cli.js rollback <job id>
//   node src/cli.js status   <job id>
//   node src/cli.js list
//
// Exit codes: 0 succeeded, 10 waiting for approval, 1 failed / cancelled, 64 usage error.
"use strict";
const fs = require("node:fs");
const path = require("node:path");
const { Jobs, validateInput } = require("./jobs");

const USAGE = fs.readFileSync(__filename, "utf8").split("\n").filter(l => l.startsWith("//")).slice(1, 11).map(l => l.slice(3)).join("\n");
const [cmd, ...rest] = process.argv.slice(2);
const flags = {}, pos = [];
for (let i = 0; i < rest.length; i++) {
  if (rest[i].startsWith("--")) { const k = rest[i].slice(2); if (["approve", "json"].includes(k)) flags[k] = true; else flags[k] = rest[++i]; }
  else pos.push(rest[i]);
}
const die = (m, code = 64) => { console.error(m); process.exit(code); };
const readJson = f => { try { return JSON.parse(fs.readFileSync(path.resolve(f), "utf8")); } catch (e) { die(`✖ cannot read ${f}: ${e.message}`); } };

const jobs = new Jobs({ maxConcurrent: 1, recover: false });

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
        if (["queued", "running"].includes(j.status) || jobs.running.has(id) || jobs.queue.some(q => q.id === id)) return;
        clearInterval(t); tick(); resolve(j);
      }, 200);
    });
  });
}

function summary(j) {
  const dir = jobs.jobDir(j.id);
  const rv = (() => { try { return JSON.parse(fs.readFileSync(path.join(dir, "review.json"), "utf8")); } catch { return null; } })();
  console.log(`\n────────\nJob ${j.id}: ${j.status.toUpperCase()}${j.error ? " (" + j.error + ")" : ""}`);
  if (rv && rv.high_severity && rv.high_severity.length) console.log("Check before approving:\n  - " + rv.high_severity.join("\n  - "));
  const show = ["review.html", "loader.js", "template.json", "report.json", "facts.json", "verify.json"].filter(n => fs.existsSync(path.join(dir, n)));
  if (show.length) console.log("Files:\n" + show.map(n => "  " + path.join(dir, n)).join("\n"));
  if (j.status === "awaiting_approval") console.log(`Next: open review.html, then  node src/cli.js approve ${j.id}   (or edit:  node src/cli.js overrides ${j.id} edits.json)`);
  if (j.status === "succeeded" && j.input.load === "manual") console.log("Next: open the Hippo deal room editor, F12 → Console (or a DevTools Snippet), paste loader.js, press Enter.");
  return j.status === "succeeded" || j.status === "rolled_back" ? 0 : j.status === "awaiting_approval" ? 10 : 1;
}

(async () => {
  if (cmd === "run") {
    const body = { hotel_url: flags.url, hippo_page_id: flags.page, flow: flags.flow, mode: flags.mode, load: flags.load, label: flags.label };
    for (const k of Object.keys(body)) if (body[k] === undefined) delete body[k];
    if (flags.scrape) body.scrape = readJson(flags.scrape);
    if (flags.overrides) body.overrides = readJson(flags.overrides);
    const errs = validateInput(body); if (errs.length) die("✖ " + errs.join("\n✖ ") + "\n\n" + USAGE);
    const j0 = jobs.create(body, "cli:" + (process.env.USER || "local"));
    console.log(`▶ job ${j0.id}`);
    let j = await follow(j0.id);
    if (j.status === "awaiting_approval" && flags.approve) { console.log("\n▶ approved from the command line (--approve)"); jobs.approve(j.id, "cli"); j = await follow(j.id); }
    process.exit(summary(j));
  }
  if (["approve", "rollback", "overrides"].includes(cmd)) {
    const id = pos[0] || die(USAGE);
    if (!jobs.get(id)) die("✖ job not found: " + id, 1);
    const p = follow(id);
    try {
      if (cmd === "approve") jobs.approve(id, "cli");
      if (cmd === "rollback") jobs.rollback(id, "cli");
      if (cmd === "overrides") jobs.overrides(id, readJson(pos[1] || die(USAGE)), "cli");
    } catch (e) { die("✖ " + e.message, 1); }
    process.exit(summary(await p));
  }
  if (cmd === "status") { const j = jobs.get(pos[0] || die(USAGE)) || die("✖ job not found", 1); if (flags.json) console.log(JSON.stringify(j, null, 2)); else summary(j); process.exit(0); }
  if (cmd === "list") { for (const j of jobs.list(30)) console.log(`${j.id}  ${j.status.padEnd(18)} ${j.flow.padEnd(6)} ${j.hotel || j.hotel_url}`); process.exit(0); }
  die(USAGE);
})();
