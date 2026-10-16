"""Smoke test for the built-in review UI. Needs a running server with API_KEYS=ui:uikey on PORT (default 8791)
and the mock hotel site on 127.0.0.1:8765. Run: python3 test/test_ui.py [port] [screenshot.png]"""
import sys, time
from playwright.sync_api import sync_playwright
port = sys.argv[1] if len(sys.argv) > 1 else "8791"
shot = sys.argv[2] if len(sys.argv) > 2 else None
with sync_playwright() as p:
    b = p.chromium.launch(); pg = b.new_page(viewport={"width": 1366, "height": 1000})
    errors = []; pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(f"http://127.0.0.1:{port}/")
    pg.fill("#key", "uikey"); pg.click("#saveKey")
    pg.fill("#url", "http://127.0.0.1:8765/hotels/grand-azure/"); pg.fill("#page", "1605252")
    pg.click("#go")
    pg.wait_for_selector("#detail .pill.s-awaiting_approval", timeout=180000)
    assert pg.locator("#bApprove").is_enabled(), "approve button should be enabled"
    assert pg.locator("#bReview").is_enabled()
    pg.wait_for_timeout(500)
    if shot: pg.screenshot(path=shot, full_page=True)
    with pg.expect_popup() as pop: pg.click("#bReview")
    rv = pop.value; rv.wait_for_load_state(); assert "Grand Azure" in rv.title(), rv.title()
    pg.once("dialog", lambda d: d.accept()); pg.click("#bApprove")
    pg.wait_for_selector("#detail h2 .pill.s-succeeded", timeout=60000)
    assert not errors, errors
    print("  ✔ UI: key → new job → awaiting approval → review page opens → approve → succeeded")
    b.close()
