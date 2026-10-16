// Hippo deal room RESTORE FROM BACKUP FILE
// Run on https://www.hippovideo.io/deal-room/edit/<page id>. Pick a "deal-room-<id>-backup-....json" file
// (downloaded by the loader). It shows what it will restore and asks before saving.
(async () => {
  console.log("▶ Deal room restore started");
  const m = location.pathname.match(/\/deal-room\/edit\/(\d+)/);
  if (location.hostname !== "www.hippovideo.io" || !m) { console.error("✖ Open https://www.hippovideo.io/deal-room/edit/<id> first."); return; }
  const PAGE_ID = m[1];
  const file = await new Promise(res => {
    const inp = document.createElement("input"); inp.type = "file"; inp.accept = ".json,application/json";
    inp.style.cssText = "position:fixed;top:20px;left:20px;z-index:999999;background:#fff;padding:12px;border:2px solid #333";
    inp.onchange = () => { res(inp.files[0]); inp.remove(); }; document.body.appendChild(inp);
    console.log("👉 Click the file box at the top-left of the page and choose the backup .json file.");
    inp.click();
  });
  if (!file) { console.warn("No file chosen. Nothing was changed."); return; }
  let b; try { b = JSON.parse(await file.text()); } catch (e) { console.error("✖ That file is not valid JSON. Nothing was changed."); return; }
  const t = b.template || b;
  if (!t.sections || !t.sideNav) { console.error("✖ That file is not a deal room backup. Nothing was changed."); return; }
  const fromPage = (file.name.match(/deal-room-(\d+)-backup/) || [])[1];
  if (fromPage && fromPage !== PAGE_ID && !confirm("This backup was taken from page " + fromPage + " but you are on page " + PAGE_ID + ". Restore it here anyway?")) { console.warn("Cancelled. Nothing was changed."); return; }
  const names = t.sections.map(s => s.name).join(", ");
  if (!confirm("Restore page " + PAGE_ID + " from " + file.name + "?\n\n" + t.sections.length + " sections: " + names.slice(0, 400))) { console.warn("Cancelled. Nothing was changed."); return; }
  const keep = {}; ["multiple_logo_top", "sections", "sideNav", "header_cta_buttons", "settings", "contactInfo"].forEach(k => { if (k in t) keep[k] = t[k]; });
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content;
  const headers = { "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8", "X-Requested-With": "XMLHttpRequest", "Accept": "application/json, text/javascript, */*; q=0.01" };
  if (csrf) headers["X-CSRF-Token"] = csrf;
  const r = await fetch("/api/video_page/pages", { method: "PATCH", credentials: "same-origin", headers, body: new URLSearchParams({ template: JSON.stringify(keep), page_id: PAGE_ID }) });
  const txt = await r.text();
  console.log("Restore:", r.status, txt);
  if (r.ok && /"success":\s*true/.test(txt)) console.log("✔ Restored. Reload the editor to see it.");
  else console.error("✖ Restore did not succeed. Send this output to Claude.");
})();
