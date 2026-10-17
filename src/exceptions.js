// Exception queue (PDF §13): the controlled fallback for anything that cannot be automated safely.
// One JSON file per exception under <data>/exceptions/. A reviewer fixes the specific problem (overrides, retry) and the
// job resumes from the failed stage; nobody rebuilds the deal room from scratch.
"use strict";
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");

const STATUSES = ["open", "in_review", "resolved", "dismissed"];
const SEVERITIES = ["critical", "high", "medium"];
const ID = /^ex_[a-z0-9]+_[a-f0-9]{8}$/;
const UNRESOLVED = s => s === "open" || s === "in_review";

class ExceptionQueue {
  constructor(dir) { this.dir = dir; fs.mkdirSync(dir, { recursive: true }); }

  file(id) { if (!ID.test(id)) throw Object.assign(new Error("bad exception id"), { status: 400 }); return path.join(this.dir, id + ".json"); }
  save(x) { x.updated_at = new Date().toISOString(); const p = this.file(x.exception_id); fs.writeFileSync(p + ".tmp", JSON.stringify(x, null, 2)); fs.renameSync(p + ".tmp", p); return x; }
  get(id) { try { return JSON.parse(fs.readFileSync(this.file(id), "utf8")); } catch (e) { if (e.status) throw e; return null; } }
  mustGet(id) { const x = this.get(id); if (!x) throw Object.assign(new Error("exception not found"), { status: 404 }); return x; }

  all() {
    return fs.readdirSync(this.dir).filter(n => n.endsWith(".json") && ID.test(n.slice(0, -5))).map(n => this.get(n.slice(0, -5))).filter(Boolean)
      .sort((a, b) => b.created_at.localeCompare(a.created_at));
  }

  list({ status, category, hotel_id, job_id, limit = 100 } = {}) {
    return this.all().filter(x => (!status || (status === "unresolved" ? UNRESOLVED(x.resolution_status) : x.resolution_status === status)) && (!category || x.category === category) &&
      (!hotel_id || x.hotel_id === hotel_id) && (!job_id || x.job_id === job_id)).slice(0, Math.min(Math.max(+limit || 100, 1), 500));
  }

  forJob(jobId) { return this.all().filter(x => x.job_id === jobId); }

  // Open an exception, or re-open the unresolved one for the same job, stage and code (a retry that fails the same way is the same exception).
  open(spec) {
    const same = this.all().find(x => x.job_id === spec.job.id && x.failed_stage === spec.stage && x.error_code === spec.code && UNRESOLVED(x.resolution_status));
    const now = new Date().toISOString();
    if (same) {
      same.occurrences = (same.occurrences || 1) + 1; same.description = spec.message; same.resolution_status = "open";
      same.missing_fields = spec.missing_fields || same.missing_fields; same.retry_eligible = spec.retry_eligible;
      same.history.push({ at: now, by: "system", event: "occurred again", notes: spec.message });
      return { exception: this.save(same), created: false };
    }
    const j = spec.job;
    const x = {
      exception_id: "ex_" + Date.now().toString(36) + "_" + crypto.randomBytes(4).toString("hex"),
      job_id: j.id, hotel_id: (j.profile && j.profile.hotel_id) || j.input.hotel_id || null, hotel_name: j.hotel || null, hotel_url: j.input.hotel_url,
      hippo_page_id: j.input.hippo_page_id || null, opportunity_id: j.input.opportunity_id || null,
      failed_stage: spec.stage, severity: SEVERITIES.includes(spec.severity) ? spec.severity : "high", category: spec.category, error_code: spec.code, description: spec.message,
      missing_fields: spec.missing_fields || [], conflicting_fields: spec.conflicting_fields || [], source_references: spec.source_references || [j.input.hotel_url],
      retry_eligible: !!spec.retry_eligible, assigned_reviewer: null, resolution_notes: "", resolution_status: "open", source: spec.source || "stage_failure",
      occurrences: 1, details: spec.details || {}, created_at: now, updated_at: now, resolved_at: null,
      history: [{ at: now, by: "system", event: "opened", notes: spec.message }],
    };
    return { exception: this.save(x), created: true };
  }

  assign(id, reviewer, by) {
    const x = this.mustGet(id);
    if (!UNRESOLVED(x.resolution_status)) throw Object.assign(new Error(`exception is ${x.resolution_status}`), { status: 409 });
    if (typeof reviewer !== "string" || !reviewer.trim() || reviewer.length > 80) throw Object.assign(new Error("reviewer must be a short name"), { status: 400 });
    x.assigned_reviewer = reviewer.trim(); x.resolution_status = "in_review"; x.history.push({ at: new Date().toISOString(), by, event: "assigned", notes: x.assigned_reviewer });
    return this.save(x);
  }

  resolve(id, { status = "resolved", notes = "" } = {}, by) {
    const x = this.mustGet(id);
    if (!["resolved", "dismissed"].includes(status)) throw Object.assign(new Error("status must be resolved or dismissed"), { status: 400 });
    if (typeof notes !== "string" || notes.length > 2000) throw Object.assign(new Error("notes must be text of at most 2000 characters"), { status: 400 });
    if (!UNRESOLVED(x.resolution_status)) throw Object.assign(new Error(`exception is already ${x.resolution_status}`), { status: 409 });
    x.resolution_status = status; x.resolution_notes = notes; x.resolved_at = new Date().toISOString();
    x.history.push({ at: x.resolved_at, by, event: status, notes });
    return this.save(x);
  }

  // System-side closing: a stage passed on retry, or a reviewer approved the job. Returns the exceptions it closed.
  closeFor(jobId, by, notes, pred = () => true) {
    const closed = [];
    for (const x of this.forJob(jobId)) {
      if (!UNRESOLVED(x.resolution_status) || !pred(x)) continue;
      x.resolution_status = "resolved"; x.resolution_notes = notes; x.resolved_at = new Date().toISOString(); x.history.push({ at: x.resolved_at, by, event: "resolved", notes });
      closed.push(this.save(x));
    }
    return closed;
  }

  // A retry or an edit is under way: unresolved exceptions for the job move to in_review.
  markInReview(jobId, by, notes) {
    for (const x of this.forJob(jobId)) if (x.resolution_status === "open") { x.resolution_status = "in_review"; x.history.push({ at: new Date().toISOString(), by, event: "in_review", notes }); this.save(x); }
  }
}

module.exports = { ExceptionQueue, STATUSES, SEVERITIES, UNRESOLVED };
