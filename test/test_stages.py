"""Unit tests for the deterministic parts of the pipeline. Run: python3 test/test_stages.py"""
import json, os, re, sys, tempfile, subprocess, html
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "agent"))
import stages as S                                   # noqa: E402
from hippo_fields import DEFAULT_MERGE_FIELDS, EMAIL_TOKENS   # noqa: E402

passed = 0
def ok(cond, name):
    global passed
    if not cond: raise AssertionError(name)
    passed += 1; print("  ✔", name)

def intake(url, **kw):
    d = tempfile.mkdtemp()
    json.dump({"id": "t", "input": {"hotel_url": url, **kw}}, open(f"{d}/job.json", "w"))
    r = subprocess.run([sys.executable, os.path.join(ROOT, "agent", "stages.py"), "intake", d], capture_output=True, text=True, env={**os.environ, "ALLOW_HTTP": "", "ALLOW_PRIVATE_HOSTS": "", "ALLOWED_HOTEL_DOMAINS": ""})
    return r.returncode, r.stdout + r.stderr, d

print("intake")
code, out, d = intake("https://www.booking.com/hotel/in/x.html")
ok(code != 0 and "booking" in out, "rejects booking.com (OTA)")
code, out, d = intake("https://www.expedia.co.uk/Hotel-x")
ok(code != 0, "rejects expedia.co.uk")
code, out, d = intake("http://www.example-hotel.com/")
ok(code != 0 and "https" in out, "rejects plain http")
code, out, d = intake("https://www.tajhotels.com/en-in/hotels/x/?utm_source=a&gclid=b&room=2#top", flow="email")
inp = json.load(open(f"{d}/input.json"))
ok(code == 0 and inp["hotel_url"] == "https://www.tajhotels.com/en-in/hotels/x/?room=2", "strips tracking params and fragment")
code, out, d = intake("https://www.ihg.com/x", load="api")
ok(code != 0 and "hippo_page_id" in out, "load=api needs a page id")
code, out, d = intake("https://www.ihg.com/x", hippo_page_id="12a")
ok(code != 0, "page id must be digits")
code, out, d = intake("https://www.ihg.com/x", flow="sms")
ok(code != 0, "flow must be email|cvent")

print("intake: source safety, customization, structured errors")
for url, why in [("https://169.254.169.254/latest/meta-data", "cloud metadata address"), ("https://localhost/admin", "localhost"), ("https://10.0.0.5/hotel", "private network"),
                 ("https://user:secret@www.ihg.com/x", "credentials in the URL"), ("https://www.ihg.com:8443/x", "non-standard port")]:
    code, out, d = intake(url)
    err = json.load(open(f"{d}/stage_error.json")) if os.path.exists(f"{d}/stage_error.json") else {}
    ok(code == 2 and "not allowed" in out and err.get("code") == "URL_NOT_ALLOWED" and err.get("category") == "SOURCE_UNVERIFIED" and err.get("retryable") is False, f"rejects {why} with a recorded, non-retryable error")
code, out, d = intake("https://www.ihg.com/hotels/x", customization={"focus": "party"})
err = json.load(open(f"{d}/stage_error.json"))
ok(code == 2 and err["code"] == "CUSTOMIZATION_INVALID" and err["category"] == "INVALID_INPUT" and "focus" in err["message"], "an invalid customization is rejected at intake, before any capture")
code, out, d = intake("https://www.ihg.com/hotels/x", opportunity_id="bad id!")
ok(code == 2 and "opportunity_id" in out, "opportunity_id is validated")
code, out, d = intake("https://www.tajhotels.com/en-in/hotels/jai-mahal-palace-jaipur/?utm_source=a", opportunity_id="OPP-42", customization={"focus": "event"})
inp = json.load(open(f"{d}/input.json"))
ok(code == 0 and inp["hotel_id"].startswith("jai-mahal-palace-jaipur-") and inp["opportunity_id"] == "OPP-42" and not os.path.exists(f"{d}/stage_error.json"), "a good intake derives the hotel id and keeps the opportunity reference")

print("grounding")
corpus = "Jai Mahal Palace sits in 18 acres of gardens. 94 rooms and 6 suites. Cinnamon restaurant. Jaipur airport is 30 minutes away."
ok(S.ungrounded("Jai Mahal Palace has 94 rooms and 6 suites in 18 acres of gardens.", corpus) == [], "grounded text passes")
bad = S.ungrounded("Jai Mahal Palace has 120 rooms, a Michelin star and is near Udaipur.", corpus)
ok("120" in bad and "Michelin" in bad and "Udaipur" in bad, "invented number and names are caught: %s" % bad)
ok(S.ungrounded("Enjoy Cinnamon, 30 minutes from the airport.", corpus) == [], "sentence-initial and common words are not flagged")
ok(S.ungrounded("It has 3,000 seats.", "up to 3000 guests") == [], "numbers match with or without thousands separator")
ok(S.ungrounded("Wi-Fi is available.", "WiFi is available to hotel guests.") == [], "Wi-Fi and WiFi are treated as the same sourced term")

print("FAQ + sentences")
ok(S.stmt("What is the check-in time? Check-in from 2 pm for all guests every day.", r"check-in") == "Check-in from 2 pm for all guests every day.", "stmt skips questions")
ok(S.stmt("LUXURY ROOMS AND SUITES AT THE PALACE HOTEL. The rooms have garden views and hand-carved beds.", r"room") == "The rooms have garden views and hand-carved beds.", "stmt skips Title Case headings")
ok(S.stmt("Description : Our ballroom has built-in audio-visual systems and stage lighting.", r"audio") .startswith("Our ballroom"), "stmt drops 'Description :' labels")
ok(S.faq([("How far is the airport?", "Yes, it is 30 minutes away. Transfers are available. Call us.", "u")], r"airport") == "It is 30 minutes away. Transfers are available.", "faq strips 'Yes,' and keeps 2 sentences")

print("cvent conversion")
m = S.email_to_cvent_map()
cvent_ok = {cv for _, cv, _ in DEFAULT_MERGE_FIELDS}
ok(set(m) == EMAIL_TOKENS, "every Email token has a Cvent mapping")
ok(all(v in cvent_ok for v in m.values()), "every mapping targets a real Cvent field")
ok(m["${Event Name}"] == "${response.event_name}" and m["${Total Room Nights}"] == "${response.total_room_nights}", "ambiguous tokens prefer response.*")
tpl = {"sections": [{"x": '<span class="hv-mf-chip" data-merge-field="${Hotel Name}">${Hotel Name}</span> ${Event Name}'}]}
s = json.dumps(S.to_cvent(tpl))
ok("${Hotel Name}" not in s and "${Event Name}" not in s and "${response.event_name}" in s, "to_cvent rewrites chips and plain tokens")
amp = [e for e in EMAIL_TOKENS if "&" in e]
if amp:
    t2 = {"a": amp[0].replace("&", "&amp;")}
    ok(m[amp[0]] in json.dumps(S.to_cvent(t2)), "to_cvent also handles &amp;-escaped tokens (%s)" % amp[0])

print(f"\n{passed} checks passed")

print("dealroom builder")
try:
    import dealroom_builder as DB
    h = {
        "slug": "demo-hotel",
        "name": "Demo Hotel",
        "location_tag": "Paris",
        "address": "1 Rue Demo, Paris",
        "brand": {"primary": "#123456", "dark": "#0B1F33", "tint": "#F5F5F5", "accent": "#AABBCC", "text": "#111111"},
        "fonts": {"heading": {"stack": "Georgia, serif", "transform": "none", "letter_spacing": "normal"}, "body": {"stack": "Arial, sans-serif", "button_font": "Arial"}},
        "logos": [{"url": "https://example.com/logo.png", "logo_type": "your_logo"}],
        "media": {"fits_1": {"source_url": "https://example.com/hotel-hero.jpg", "hippo": None}, "gallery_1": {"source_url": "https://example.com/gallery-1.jpg", "hippo": None}},
        "slots": {"fits": ["fits_1"], "gallery": ["gallery_1"], "rooms": ["gallery_1"], "meeting": ["gallery_1"],
                  "dining": ["gallery_1"], "amenities": ["gallery_1"]},
        "map": {"query": "Paris"},
        "overview": "A hotel with a stylish look.",
        "fits": [{"icon": "house", "title": "City centre", "body": "Walkable location"}],
        "rooms_intro": "Beautiful rooms.",
        "meeting_intro": "Meetings available.",
        "venues": {"rows": []},
        "dining_intro": "Dining options.",
        "av_support": "AV available.",
        "location_intro": "In the city centre.",
        "location_cards": [],
        "amenities_line": "Includes parking.",
        "room_types": {"rows": []},
        "featured_video": None,
    }
    tpl, checks, _ = DB.build(h)
    hero = tpl["sections"][0]["components"][0]["data"]["rightComponents"][0]["data"]
    ok(hero["thumbnail_url"].startswith("https://example.com/"), "hero uses a real hotel image instead of a generic placeholder")
    ok(hero["thumbnail_url"] != "/img/without_img.jpg", "hero image is not the default blank asset")
    ok(hero["videoPoster"] == hero["thumbnail_url"], "hero poster matches the selected hotel image")
    names = [section["name"] for section in tpl["sections"]]
    ok(names == ["Overview", "Why This Hotel Fits", "Guest Rooms", "Concessions & Value Adds", "Meeting Space",
                 "Pricing Summary", "Food & Beverage", "Amenities & Fees", "AV & Technology", "Location & Destination",
                 "Virtual Tour & Gallery", "Proposal PDF", "Next Steps"],
       "sections follow the reference proposal's order with summary and contact details on the overview")
    serialized = json.dumps(tpl)
    ok("${Event Name}" in serialized and "${Total Attendees}" in serialized and "Forshaw" not in serialized and "Kelsey" not in serialized,
       "event-specific content stays dynamic instead of copying sample PDF values")
    errors, _, _ = DB.validate(tpl, h, checks)
    ok(not errors, "PDF-inspired template passes deal-room validation: %s" % errors)
except Exception as e:
    raise AssertionError(f"dealroom builder regression failed: {e}")
