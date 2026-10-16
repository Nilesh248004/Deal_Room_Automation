// Mock of the two Hippo deal-room endpoints the agent uses, for end-to-end tests.
//   GET   /api/video_page/templates/:id/editor?fromPreview=false
//   PATCH /api/video_page/pages        (form-urlencoded: template=<json>&page_id=<id>)
// Auth: "Authorization: Bearer test-token" or Cookie + X-CSRF-Token.
// Test hooks: GET /__log, POST /__reset, POST /__mode {"drop_section_on_save": true}
"use strict";
const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");

const base = JSON.parse(fs.readFileSync(path.join(__dirname, "fixtures", "base_template.json"), "utf8"));
let pages, log, mode;
const reset = () => { pages = { "1605252": structuredClone(base), "1562033": structuredClone(base) }; log = []; mode = {}; };
reset();

const authed = req => req.headers.authorization === "Bearer test-token" || (/_session=/.test(req.headers.cookie || "") && req.headers["x-csrf-token"] === "csrf-test");
const json = (res, st, o) => { res.writeHead(st, { "content-type": "application/json" }); res.end(JSON.stringify(o)); };
const body = req => new Promise(r => { const c = []; req.on("data", d => c.push(d)); req.on("end", () => r(Buffer.concat(c).toString())); });

const server = http.createServer(async (req, res) => {
  const u = new URL(req.url, "http://x");
  if (u.pathname === "/__log") return json(res, 200, log);
  if (u.pathname === "/__reset") { reset(); return json(res, 200, { ok: true }); }
  if (u.pathname === "/__mode") { mode = JSON.parse(await body(req) || "{}"); return json(res, 200, mode); }
  if (u.pathname.startsWith("/__page/")) return json(res, 200, pages[u.pathname.split("/")[2]] || null);
  if (!authed(req)) { log.push({ method: req.method, path: u.pathname, status: 401 }); return json(res, 401, { status: false, message: "User Not Found" }); }
  let m = u.pathname.match(/^\/api\/video_page\/templates\/(\d+)\/editor$/);
  if (req.method === "GET" && m) {
    log.push({ method: "GET", path: u.pathname, page: m[1] });
    if (!pages[m[1]]) return json(res, 404, { status: false });
    return json(res, 200, { status: true, template: pages[m[1]], template_type: 143, page_name: "[Copy] Test page " + m[1], default_merge_fields: [] });
  }
  if (req.method === "PATCH" && u.pathname === "/api/video_page/pages") {
    const raw = await body(req);
    const f = new URLSearchParams(raw);
    const id = f.get("page_id");
    let t; try { t = JSON.parse(f.get("template")); } catch { return json(res, 422, { success: false, message: "bad template" }); }
    log.push({ method: "PATCH", path: u.pathname, page: id, keys: Object.keys(t), ctype: req.headers["content-type"], sections: (t.sections || []).length });
    if (!pages[id]) return json(res, 404, { success: false });
    const saved = { ...pages[id], ...t };
    if (mode.drop_section_on_save && log.filter(x => x.method === "PATCH").length === 1) saved.sections = saved.sections.slice(0, -1);   // simulate a bad save
    pages[id] = saved;
    return json(res, 200, { success: true, page_id: id, message: "Successfully updated" });
  }
  json(res, 404, { error: "not found" });
});

const port = +process.env.MOCK_HIPPO_PORT || 8799;
server.listen(port, "127.0.0.1", () => console.log("mock hippo on " + port));
