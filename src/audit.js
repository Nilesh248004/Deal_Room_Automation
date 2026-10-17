// Append-only audit trail: one JSON object per line. The Python stages append to the same per-job file.
// Never put secrets or personal data in an event: ids, stage names, outcomes and who (an API key *name*) only.
"use strict";
const fs = require("node:fs");

function append(file, evt) {
  try { fs.appendFileSync(file, JSON.stringify({ at: new Date().toISOString(), ...evt }) + "\n"); } catch { /* the audit trail must never break a job */ }
}

function read(file, limit = 1000) {
  let text; try { text = fs.readFileSync(file, "utf8"); } catch { return []; }
  const out = [];
  for (const line of text.split("\n")) { if (!line) continue; try { out.push(JSON.parse(line)); } catch { /* skip a torn line */ } }
  return out.slice(-limit);
}

module.exports = { append, read };
