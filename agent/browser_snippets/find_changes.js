// Hippo deal room CHANGE FINDER (read-only; changes nothing)
// Run 1: run this on the editor tab  -> it remembers the page as it is now.
// Then:  make ONE manual change in the editor (e.g. fill one text card with a colour) and click Save.
// Run 2: run this again               -> it lists exactly which JSON fields Hippo changed, and downloads the list.
(async () => {
  const m = location.pathname.match(/\/deal-room\/edit\/(\d+)/);
  if (location.hostname !== "www.hippovideo.io" || !m) { console.error("✖ Open https://www.hippovideo.io/deal-room/edit/<id> first."); return; }
  const r = await fetch("/api/video_page/templates/" + m[1] + "/editor?fromPreview=false", { credentials: "same-origin", headers: { "Accept": "application/json" } });
  if (!r.ok) { console.error("✖ Could not read page, HTTP", r.status); return; }
  const now = (await r.json()).template;
  const KEY = "hv_snapshot_" + m[1];
  let before = null; try { before = JSON.parse(localStorage.getItem(KEY)); } catch (e) {}
  if (!before) {
    localStorage.setItem(KEY, JSON.stringify(now));
    console.log("✔ Run 1 done: snapshot saved. Now make ONE change in the editor (e.g. colour one card), click Save, then run this again.");
    return;
  }
  const diffs = [];
  const label = (p) => p.replace(/^sections\.(\d+)/, (_, i) => "sections." + i + "[" + ((now.sections[i] || before.sections[i] || {}).name || "?") + "]");
  const walk = (a, b, p) => {
    if (JSON.stringify(a) === JSON.stringify(b)) return;
    if (a && b && typeof a === "object" && typeof b === "object") {
      new Set([...Object.keys(a), ...Object.keys(b)]).forEach(k => walk(a[k], b[k], p ? p + "." + k : k));
    } else diffs.push({ path: label(p), before: a === undefined ? "(missing)" : a, after: b === undefined ? "(removed)" : b });
  };
  walk(before, now, "");
  localStorage.removeItem(KEY);
  const out = JSON.stringify({ page_id: m[1], changes: diffs }, null, 2);
  const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([out], { type: "application/json" }));
  a.download = "hippo-changes-" + m[1] + ".json"; a.click();
  console.log("✔ Run 2 done: " + diffs.length + " field(s) changed. Downloaded hippo-changes-" + m[1] + ".json. Send that file to Claude.");
  console.table(diffs.map(d => ({ path: d.path, before: String(d.before).slice(0, 80), after: String(d.after).slice(0, 80) })));
})();
