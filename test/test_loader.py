"""Browser test for a generated loader.js (the manual-load path). Mocks www.hippovideo.io inside Chromium.
Run: python3 test/test_loader.py <job_dir_with_loader.js>"""
import asyncio, json, sys, urllib.parse, os
from playwright.async_api import async_playwright

JS = open(os.path.join(sys.argv[1], "loader.js")).read()
import re
PID = re.search(r"-> page (\d+|auto)", JS.split("\n")[0]).group(1)
AUTO = PID == "auto"
PID = "1605252" if AUTO else PID
OTHER = "99999" if PID != "99999" else "88888"

async def run(url, accept=True):
    calls, logs, dialogs = [], [], []
    async with async_playwright() as p:
        b = await p.chromium.launch(); ctx = await b.new_context(accept_downloads=True); pg = await ctx.new_page()
        async def h(route, req):
            u = req.url
            if "/deal-room/" in u:
                return await route.fulfill(content_type="text/html", body='<html><head><meta name="csrf-token" content="T"></head><body></body></html>')
            if "/editor?fromPreview=false" in u:
                pid = u.split("/templates/")[1].split("/")[0]; calls.append(("GET", pid))
                return await route.fulfill(content_type="application/json", body=json.dumps({"status": True, "page_name": "Test", "template": {"sections": [{"id": "s_old"}], "multiple_logo_top": []}}))
            if u.endswith("/api/video_page/pages"):
                f = urllib.parse.parse_qs(req.post_data)
                calls.append(("PATCH", f["page_id"][0], sorted(json.loads(f["template"][0])), req.headers.get("x-csrf-token")))
                return await route.fulfill(content_type="application/json", body='{"success":true,"page_id":"%s","message":"Successfully updated"}' % f["page_id"][0])
            return await route.abort()
        await pg.route("https://www.hippovideo.io/**", h)
        pg.on("console", lambda m: logs.append(m.text))
        async def on_dialog(d): dialogs.append(d.message[:80]); await (d.accept() if accept else d.dismiss())
        pg.on("dialog", lambda d: asyncio.ensure_future(on_dialog(d)))
        await pg.goto(url); await pg.evaluate(JS); await pg.wait_for_timeout(1500); await b.close()
    return calls, logs, dialogs

async def main():
    keys = sorted(["multiple_logo_top", "sections", "sideNav", "header_cta_buttons", "settings", "contactInfo"])
    c, l, d = await run(f"https://www.hippovideo.io/deal-room/edit/{PID}", True)
    assert c[0] == ("GET", PID) and c[1][:3] == ("PATCH", PID, keys) and c[1][3] == "T", c
    assert any("Done" in x for x in l), l
    print("  ✔ editor tab + OK: backup, then PATCH with the 6 editor keys and the CSRF token")
    c, l, d = await run(f"https://www.hippovideo.io/deal-room/edit/{PID}", False)
    assert [x[0] for x in c] == ["GET"], c
    print("  ✔ editor tab + Cancel: backup only, nothing saved")
    c, l, d = await run(f"https://www.hippovideo.io/deal-room/preview/{PID}", True)
    assert c == [], c
    print("  ✔ wrong tab (preview): nothing is called")
    if AUTO: print("  · loader targets the open page (auto): skipping the different-page check"); return
    c, l, d = await run(f"https://www.hippovideo.io/deal-room/edit/{OTHER}", True)
    assert c == [] and any("targets page" in x for x in l), (c, l)
    print("  ✔ editor tab for a different page: stops before any call")
asyncio.run(main())
