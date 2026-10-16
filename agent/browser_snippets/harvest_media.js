// Hippo deal room MEDIA HARVEST (read-only; changes nothing)
// 1) Upload the hotel's photos/logo/video into any carousel on the editor page and click Save.
// 2) Run this in the Console on that editor tab. It copies a JSON list of every uploaded asset to your clipboard
//    and downloads it as a file. Send that file to Claude (or paste into the hotel config "media" -> "hippo").
(async () => {
  const m = location.pathname.match(/\/deal-room\/edit\/(\d+)/);
  if (location.hostname !== "www.hippovideo.io" || !m) { console.error("✖ Open https://www.hippovideo.io/deal-room/edit/<id> first."); return; }
  const r = await fetch("/api/video_page/templates/" + m[1] + "/editor?fromPreview=false", { credentials: "same-origin", headers: { "Accept": "application/json" } });
  if (!r.ok) { console.error("✖ Could not read page, HTTP", r.status); return; }
  const t = (await r.json()).template || {};
  const out = { page_id: m[1], images: [], videos: [], logos: [] }, seen = new Set();
  const walk = (o, section) => {
    if (Array.isArray(o)) return o.forEach(x => walk(x, section));
    if (!o || typeof o !== "object") return;
    if (o.is_image && o.asset_id && o.token && !o.is_dynamic_media && !seen.has("i" + o.asset_id)) {
      seen.add("i" + o.asset_id); out.images.push({ section, title: o.title, asset_id: o.asset_id, token: o.token });
    }
    if (o.upload_type === "videoLibrary" && o.asset_id && !seen.has("v" + o.asset_id)) {
      seen.add("v" + o.asset_id); out.videos.push({ section, asset_id: o.asset_id, thumbnail_url: o.thumbnail_url, hippo: o });
    }
    Object.values(o).forEach(v => walk(v, section));
  };
  (t.sections || []).forEach(s => walk(s.components, s.name));
  (t.multiple_logo_top || []).forEach(l => out.logos.push({ hippo: l }));
  const json = JSON.stringify(out, null, 2);
  try { await navigator.clipboard.writeText(json); } catch (e) {}
  const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([json], { type: "application/json" }));
  a.download = "hippo-media-" + m[1] + ".json"; a.click();
  console.log("✔ Found " + out.images.length + " uploaded images, " + out.videos.length + " library videos, " + out.logos.length + " logos. Downloaded hippo-media-" + m[1] + ".json");
  console.table(out.images);
})();
