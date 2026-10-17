// Read-side view of the hotel store that the Python pipeline writes (agent/hotel_profile.py), plus the one thing the API writes:
// a hotel's saved customization. Layout: <data>/hotels/<hotel_id>/{index.json, profile.json, versions/vN.json, customization.json, generations.json, audit.jsonl}
"use strict";
const fs = require("node:fs");
const path = require("node:path");
const audit = require("./audit");

const HOTEL_ID = /^[a-z0-9][a-z0-9-]{2,80}$/;
const readJson = (p, d = null) => { try { return JSON.parse(fs.readFileSync(p, "utf8")); } catch { return d; } };
const writeJson = (p, o) => { fs.writeFileSync(p + ".tmp", JSON.stringify(o, null, 2)); fs.renameSync(p + ".tmp", p); };

class Hotels {
  constructor(dir) { this.dir = dir; fs.mkdirSync(dir, { recursive: true }); }

  p(id, ...parts) { if (!HOTEL_ID.test(id || "")) throw Object.assign(new Error("bad hotel id"), { status: 400 }); return path.join(this.dir, id, ...parts); }
  exists(id) { return fs.existsSync(this.p(id, "index.json")); }

  summary(id) {
    const idx = readJson(this.p(id, "index.json"), {}), pr = readJson(this.p(id, "profile.json"), {}), c = readJson(this.p(id, "customization.json"), {});
    return { hotel_id: id, property_name: pr.property_name || null, official_website_url: pr.official_website_url || null, city: pr.city || null,
      latest_version: idx.latest_version || 0, approved_version: idx.approved_version || null, profile_status: pr.profile_status || null,
      last_verified_at: pr.last_verified_at || null, has_customization: !!(c.customization && Object.keys(c.customization).length) };
  }

  list() {
    return fs.readdirSync(this.dir).filter(n => HOTEL_ID.test(n) && this.exists(n)).map(n => this.summary(n)).sort((a, b) => (a.property_name || a.hotel_id).localeCompare(b.property_name || b.hotel_id));
  }

  // regeneration_required (PDF §10.3): a deal room (per Hippo page or opportunity) was last generated from an older profile version than the one
  // now approved. Whether the change matters is a person's call, so this only says "decide", it never regenerates by itself.
  detail(id) {
    if (!this.exists(id)) return null;
    const s = this.summary(id), gens = readJson(this.p(id, "generations.json"), []), c = readJson(this.p(id, "customization.json"), {});
    const last = new Map();
    for (const g of gens) { const key = g.hippo_page_id ? "page:" + g.hippo_page_id : g.opportunity_id ? "opp:" + g.opportunity_id : null; if (key) last.set(key, g); }
    const stale = [...last.values()].filter(g => s.approved_version && g.profile_version < s.approved_version)
      .map(g => ({ job_id: g.job_id, hippo_page_id: g.hippo_page_id, opportunity_id: g.opportunity_id, generated_from_version: g.profile_version, approved_version: s.approved_version }));
    return { ...s, customization: c.customization || {}, customization_version: c.version || null, customization_updated_by: c.updated_by || null, customization_updated_at: c.updated_at || null,
      generations: gens.slice(-20).reverse(), regeneration_required: stale.length > 0, stale_generations: stale,
      regeneration_note: stale.length ? `profile v${s.approved_version} is approved, but ${stale.length} deal room(s) were generated from an older version: regenerate if the changed facts matter` : null };
  }

  profile(id, version) {
    if (!this.exists(id)) return null;
    if (version === undefined || version === null || version === "") return readJson(this.p(id, "profile.json"));
    if (!/^\d{1,6}$/.test(String(version))) throw Object.assign(new Error("version must be a number"), { status: 400 });
    return readJson(this.p(id, "versions", `v${+version}.json`));
  }

  versions(id) {
    if (!this.exists(id)) return null;
    const dir = this.p(id, "versions");
    return (fs.existsSync(dir) ? fs.readdirSync(dir) : []).map(n => n.match(/^v(\d+)\.json$/)).filter(Boolean).map(m => {
      const v = readJson(path.join(dir, m[0]), {});
      return { version: +m[1], profile_status: v.profile_status, approved_by: v.approved_by || null, approved_at: v.approved_at || null, built_at: v.built_at, content_hash: v.content_hash };
    }).sort((a, b) => b.version - a.version);
  }

  // `customization` has already been validated and normalised by agent/customize.py.
  saveCustomization(id, customization, version, by) {
    if (!this.exists(id)) throw Object.assign(new Error("hotel not found: run a deal room for it first, or pass save_customization with the job"), { status: 404 });
    const prev = readJson(this.p(id, "customization.json"), {});
    const rec = { customization, version, updated_by: by, updated_at: new Date().toISOString() };
    // Who approved the rights, and when, is a legal fact: keep it when the approval is unchanged, stamp it only when it is new.
    const ar = customization.asset_rights, was = (prev.customization || {}).asset_rights;
    if (ar && ar.approved) {
      const kept = was && was.approved && was.approved_by;
      rec.customization.asset_rights = { ...ar, approved_by: kept ? was.approved_by : by, approved_at: kept ? was.approved_at : rec.updated_at };
    }
    writeJson(this.p(id, "customization.json"), rec);
    audit.append(this.p(id, "audit.jsonl"), { event: "customization_saved", by, version, previous_version: prev.version || null,
      asset_rights_approved: !!(ar && ar.approved) });
    return this.detail(id);
  }

  audit(id) { return this.exists(id) ? audit.read(this.p(id, "audit.jsonl")) : null; }
}

module.exports = { Hotels, HOTEL_ID };
