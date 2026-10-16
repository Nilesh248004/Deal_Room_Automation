"""Deal Room Agent: pipeline stages.

Usage (called by the Node job runner, argv only, never through a shell):
    python3 agent/stages.py <stage> <job_dir>

Stages (each reads/writes files in <job_dir>; exit code != 0 stops the job):
    intake   -> input.json checked, normalised                     (job.json input)
    capture  -> scrape.json, capture/page.png [, capture/page.har]  (headless Chromium runs scrape_hotel.js)
    convert  -> config.json                                         (scrape_to_config.convert)
    facts    -> facts.json  (every hard fact with source_url + quote)
    copy     -> copy.json   (grounded copy for empty fields; optional LLM polish with grounding check)
    build    -> template.json, report.json, loader.js               (dealroom_builder + validation; email or cvent flow)
    review   -> review.html, review.json (needs_review / severity)
    load     -> backup.json, load.json        (Hippo API) | loader.js only (manual)
    verify   -> verify.json                   (read-back diff; auto-rollback on critical mismatch)
    rollback -> rollback.json                 (restores backup.json)

Deterministic by design: merge fields, colours, IDs and the save format come from code. The only optional AI
step is copy polishing, and its output is rejected unless every number and name in it appears in the hotel's text.
"""
import json, os, re, sys, html, time, urllib.parse, urllib.request, copy as _copy

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import scrape_to_config as S2C                       # noqa: E402
import dealroom_builder as B                         # noqa: E402
from hippo_fields import DEFAULT_MERGE_FIELDS, EMAIL_TOKENS   # noqa: E402

OTA = re.compile(r"(^|\.)(booking|expedia|agoda|hotels|tripadvisor|trivago|kayak|makemytrip|goibibo|airbnb|priceline|orbitz|hotelscombined|trip|cleartrip|yatra|ebookers|travelocity)\.(com|co\.\w+|in|de|fr|net)$", re.I)

def log(*a): print(*a, flush=True)
def jread(p, default=None):
    try: return json.load(open(p))
    except FileNotFoundError: return default
def jwrite(p, obj): json.dump(obj, open(p, "w"), indent=2, ensure_ascii=False)
def fail(msg): log("✖ " + msg); sys.exit(2)


# ---------------------------------------------------------------------------------------------- intake
def stage_intake(d):
    job = jread(f"{d}/job.json"); inp = job["input"]
    u = urllib.parse.urlparse(inp.get("hotel_url", "").strip())
    if u.scheme not in ("https", "http") or not u.netloc: fail("hotel_url must be a full https:// URL")
    if u.scheme == "http" and os.environ.get("ALLOW_HTTP") != "1": fail("hotel_url must use https")
    host = u.hostname or ""
    if OTA.search(host): fail(f"{host} is a booking/travel site, not the hotel's official website. Use the hotel's own page (e.g. the chain's hotel page).")
    allow = [x.strip().lower() for x in os.environ.get("ALLOWED_HOTEL_DOMAINS", "").split(",") if x.strip()]
    if allow and not any(host.lower() == a or host.lower().endswith("." + a) for a in allow): fail(f"{host} is not in ALLOWED_HOTEL_DOMAINS")
    keep = {k: v for k, v in urllib.parse.parse_qsl(u.query) if not re.match(r"(utm_|gclid|gbraid|fbclid|cm_mmc|mktgvar|clk_src|partner|trso|srb_|hmguid)", k, re.I)}
    clean = urllib.parse.urlunparse(u._replace(query=urllib.parse.urlencode(keep), fragment=""))
    if inp.get("flow", "email") not in ("email", "cvent"): fail("flow must be email or cvent")
    if inp.get("mode", "review") not in ("review", "auto"): fail("mode must be review or auto")
    if inp.get("load", "manual") not in ("manual", "api"): fail("load must be manual or api")
    pid = str(inp.get("hippo_page_id", "") or "")
    if pid and not re.fullmatch(r"\d{1,12}", pid): fail("hippo_page_id must be digits")
    if inp.get("load") == "api" and not pid: fail("load=api needs hippo_page_id")
    norm = {**inp, "hotel_url": clean, "hippo_page_id": pid, "flow": inp.get("flow", "email"), "mode": inp.get("mode", "review"), "load": inp.get("load", "manual")}
    jwrite(f"{d}/input.json", norm)
    log(f"✔ intake: {clean} | flow={norm['flow']} mode={norm['mode']} load={norm['load']} page={pid or 'auto'}")


# ---------------------------------------------------------------------------------------------- capture
COOKIE_BUTTONS = ["Accept all", "Accept All", "Accept all cookies", "Accept Cookies", "Accept", "I agree", "I Accept", "Allow all", "Agree", "OK", "Got it"]

def stage_capture(d):
    from playwright.sync_api import sync_playwright
    inp = jread(f"{d}/input.json"); os.makedirs(f"{d}/capture", exist_ok=True)
    js = open(os.path.join(HERE, "scrape_hotel.js")).read()
    har = os.environ.get("CAPTURE_HAR") == "1"
    with sync_playwright() as p:
        br = p.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
        ctx = br.new_context(viewport={"width": 1440, "height": 900}, locale="en-US", accept_downloads=True,
                             user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
                             **({"record_har_path": f"{d}/capture/page.har", "record_har_content": "omit"} if har else {}))
        pg = ctx.new_page(); logs = []
        pg.on("console", lambda m: logs.append(m.text) if "[hotel-scraper]" in m.text else None)
        r = pg.goto(inp["hotel_url"], wait_until="domcontentloaded", timeout=60000)
        if r is None or r.status >= 400: fail(f"page returned HTTP {r.status if r else 'no response'}")
        try: pg.wait_for_load_state("networkidle", timeout=15000)
        except Exception: pass
        for t in COOKIE_BUTTONS:                       # dismiss a cookie banner if one is showing
            try:
                b = pg.get_by_role("button", name=t, exact=True)
                if b.count() and b.first.is_visible(): b.first.click(timeout=2000); pg.wait_for_timeout(600); log(f"  cookie banner: clicked '{t}'"); break
            except Exception: pass
        with pg.expect_download(timeout=240000) as dl:
            pg.evaluate(js)
        dl.value.save_as(f"{d}/scrape.json")
        try: pg.screenshot(path=f"{d}/capture/page.png", full_page=True, timeout=30000)
        except Exception as e: log("  (screenshot skipped:", e, ")")
        ctx.close(); br.close()
    for l in logs: log("  " + l.replace("[hotel-scraper] ", ""))
    sc = jread(f"{d}/scrape.json")
    log(f"✔ capture: {len(sc['images'])} images, {len(sc['logos'])} logos, {len(sc['subpages'])} pages, fonts {[f['family'] for f in sc['fonts']['loaded']][:4]}")


# ---------------------------------------------------------------------------------------------- convert
def stage_convert(d):
    sc = jread(f"{d}/scrape.json")
    try: cfg = S2C.convert(sc)
    except SystemExit as e: fail(str(e))
    jwrite(f"{d}/config.json", cfg)
    log(f"✔ convert: {cfg['name']} | brand {cfg['brand']} | fonts {cfg['fonts']['heading']['site_font']}/{cfg['fonts']['body']['site_font']}"
        f" | {len(cfg['media'])} photos | {len(cfg['venues']['rows'])} venues | {len((cfg.get('room_types') or {}).get('rows', []))} room types")
    for w in cfg["_warnings"]: log("  ⚠ " + w)


# ---------------------------------------------------------------------------------------------- facts (provenance)
def own_pages(sc, cfg):
    srcs = set(cfg.get("sources", []))
    pages = [{"url": sc["url"], "text": S2C.clean(sc.get("page_text", ""))}] if sc["url"] in srcs else []
    return pages + [{"url": p["url"], "text": S2C.clean(p.get("text", ""))} for p in sc.get("subpages", []) if p["url"] in srcs]

def locate(value, pages, prefer=None):
    v = re.sub(r"\s+", " ", str(value)).strip().lower()
    if not v: return None, ""
    if prefer: pages = sorted(pages, key=lambda p: not re.search(prefer, p["url"], re.I))
    for p in pages:
        t = p["text"]; i = t.lower().find(v)
        if i >= 0:
            a = max(t.rfind("\n", 0, i), t.rfind(". ", 0, i)) + 1; b = t.find("\n", i); b = len(t) if b < 0 else b
            return p["url"], t[a:b].strip()[:240]
    return None, ""

def stage_facts(d):
    sc, cfg = jread(f"{d}/scrape.json"), jread(f"{d}/config.json")
    pages = own_pages(sc, cfg); facts = []
    ld = S2C.hotel_ld(sc.get("jsonld", []) + [j for p in sc.get("subpages", []) if p["url"] in cfg["sources"] for j in p.get("jsonld", [])])
    def add(field, value, src=None, quote=""):
        if value in (None, "", []): return
        if not src: src, quote = locate(value, pages)
        facts.append({"field": field, "value": value, "source_url": src, "quote": quote, "verified": bool(src)})
    for f in ("name", "address", "phone"):
        if cfg.get(f): add(f, html.unescape(cfg[f]), "structured data (schema.org Hotel)" if ld else None, "")
    for k, v in (cfg.get("facts") or {}).items(): add(k, v)
    for r in cfg["venues"]["rows"]: add("venue", html.unescape(r[0]) + " | " + " | ".join(r[1:]), *locate(html.unescape(r[0]), pages, r"meeting|event|venue"))
    for r in (cfg.get("room_types") or {}).get("rows", []): add("room_type", html.unescape(" | ".join(r)), *locate(html.unescape(r[0]), pages, r"room|suite|accommodation"))
    for q, a, src in faqs(sc, cfg): add("faq", f"{q} → {a[:160]}", src, "FAQ (schema.org FAQPage)")
    for h in cfg.get("highlights", []): add("highlight", h)
    if cfg.get("map", {}).get("query"):
        q = cfg["map"]["query"]
        src = "structured data (geo / hasMap)" if S2C.coords(ld) and q == ",".join(S2C.coords(ld)) else \
              "structured data (hotel name + address)" if ld and cfg.get("address") and html.unescape(cfg["address"]) in q else None
        add("map", q, src, "") if src else add("map", q)
    unv = [f for f in facts if not f["verified"]]
    jwrite(f"{d}/facts.json", facts)
    log(f"✔ facts: {len(facts)} facts, {len(facts) - len(unv)} with a source" + (f"; ⚠ {len(unv)} without a source: {[f['field'] for f in unv]}" if unv else ""))


# ---------------------------------------------------------------------------------------------- copy (grounded)
STOP = set("The A An Our Your With And For From To Of In At On By Or As Is Are Be Up All Each Every This That These Those Its It "
           "Ask Enjoy Discover Experience Explore Welcome Nestled Set Located Rooms Suites Room Suite Meeting Meetings Event Events "
           "Venue Venues Dining Restaurant Restaurants Bar Bars Spa Pool Wellness Guests Guest Check Getting Here Parking Nearby "
           "Amenities Amenity Highlights Airport Station Hotel Resort Palace Space Spaces Indoor Outdoor Free Onsite Grand Royal".split())

def _ld_nodes(j):
    if isinstance(j, list):
        for x in j: yield from _ld_nodes(x)
    elif isinstance(j, dict):
        yield j
        if isinstance(j.get("@graph"), list): yield from _ld_nodes(j["@graph"])

def faqs(sc, cfg):
    """(question, answer, source_url) from schema.org FAQPage blocks on the hotel's own pages (answers are often hidden in accordions)."""
    out, seen = [], set()
    pages = ([(sc["url"], sc.get("jsonld", []))] if sc["url"] in cfg.get("sources", []) else []) + \
            [(p["url"], p.get("jsonld", [])) for p in sc.get("subpages", []) if p["url"] in cfg.get("sources", [])]
    for url, ld in pages:
        for n in _ld_nodes(ld):
            if "FAQPage" not in str(n.get("@type", "")): continue
            for q in n.get("mainEntity", []) if isinstance(n.get("mainEntity"), list) else [n.get("mainEntity")]:
                if not isinstance(q, dict): continue
                a = q.get("acceptedAnswer") or {}; a = a[0] if isinstance(a, list) and a else a
                qt = S2C.clean(html.unescape(re.sub(r"<[^>]+>", " ", q.get("name", "")))).strip()
                at = re.sub(r"\s+", " ", S2C.clean(html.unescape(re.sub(r"<[^>]+>", " ", (a or {}).get("text", ""))))).strip()
                if qt and at and qt not in seen: seen.add(qt); out.append((qt, at, url))
    return out

def faq(pairs, rx, n=2):
    """First n sentences of the first FAQ answer whose question matches rx."""
    for q, a, _ in pairs:
        if re.search(rx, q, re.I):
            a = re.sub(r"^(?:yes|no|absolutely|certainly|of course)\b[,.!]?\s*", "", a, flags=re.I)
            s = re.split(r"(?<=[.!?])\s+", a)
            out = " ".join(s[:n]).strip()
            return out[:1].upper() + out[1:]
    return ""

def stmt(text, rx, n=1):
    """Like scrape_to_config.find, but only real statements: no questions, no Title Case headings, at least 6 words."""
    out = []
    for s in S2C.sentences(text):
        s = re.sub(r"^(?:Description|Overview|Summary|Details|Note)\s*:\s*", "", s, flags=re.I)
        w = re.findall(r"[A-Za-z][\w'-]*", s)
        if s.endswith("?") or len(w) < 6 or sum(x[0].isupper() for x in w) > 0.6 * len(w): continue
        if re.search(rx, s, re.I) and s not in out: out.append(s)
    return " ".join(out[:n])

def corpus_of(sc, cfg):
    fq = " \n".join(q + " " + a for q, a, _ in faqs(sc, cfg))
    return S2C.clean(" \n".join([sc.get("page_text", "")] + [p.get("text", "") for p in sc.get("subpages", []) if p["url"] in cfg["sources"]]) + " \n" + fq + " " + json.dumps(cfg, ensure_ascii=False))

def ungrounded(text, corpus):
    """Numbers and proper names in `text` that do not appear in the hotel's own text."""
    t = html.unescape(re.sub(r"<[^>]+>", " ", text)); c = corpus.lower().replace(",", "")
    # Hotel sites commonly write WiFi while our display copy uses Wi-Fi. Treat
    # hyphenated spellings as equivalent without weakening ordinary word checks.
    c_without_hyphens = re.sub(r"(?<=[a-z0-9])-(?=[a-z0-9])", "", c)
    bad = [n for n in re.findall(r"\d[\d,.]*", t) if n.replace(",", "").rstrip(".") not in c]
    for sent in re.split(r"(?<=[.!?;:])\s+", t):
        words = re.findall(r"[A-Za-z][\w'&-]*", sent)
        for i, w in enumerate(words):
            if i == 0 or not w[0].isupper() or w in STOP or len(w) < 3: continue
            if w.lower() not in c and w.lower().replace("-", "") not in c_without_hyphens: bad.append(w)
    return sorted(set(bad))

def page_text(sc, cfg, rx):
    return S2C.clean("\n".join(p.get("text", "") for p in sc.get("subpages", []) if p["url"] in cfg["sources"] and re.search(rx, p.get("link_text", "") + " " + p["url"], re.I)))

def outlet_names(sc, cfg):
    dine = page_text(sc, cfg, r"dining|restaurant|\bbar")
    if not dine: return []
    others = page_text(sc, cfg, r"room|suite|meeting|event|gallery|^OVERVIEW$") + "\n" + S2C.clean(sc.get("page_text", ""))
    other_lines = {l.strip().lower() for l in others.split("\n")}
    seen, out = set(), []
    for l in [x.strip() for x in dine.split("\n")]:
        if 2 < len(l) <= 40 and 1 <= len(l.split()) <= 4 and l.lower() not in other_lines and not re.search(r"[.:!?]|\d|\bat\b|dining|travel|stories|explore|gift|offers?|rate", l, re.I) and l.lower() not in seen:
            seen.add(l.lower()); out.append(l)
    return out[:8]

def stage_copy(d):
    sc, cfg = jread(f"{d}/scrape.json"), jread(f"{d}/config.json")
    corpus = corpus_of(sc, cfg); f = cfg.get("facts") or {}; esc = lambda t: html.escape(t or "", quote=False)
    alltext = S2C.clean(sc.get("page_text", "")) + "\n" + page_text(sc, cfg, r".")
    meet, rooms_t = page_text(sc, cfg, r"meeting|event|venue"), page_text(sc, cfg, r"room|suite|accommodation")
    FQ = faqs(sc, cfg)
    out, sources = {}, {}
    def put(k, v, src): out[k] = v; sources[k] = src
    venues = (cfg.get("_raw") or {}).get("venues", [])
    def cap(v): return max([int(re.sub(r"\D", "", x) or 0) for x in v["seating"].values()] or [0])
    big = max(venues, key=cap) if venues else None
    rt = (cfg.get("room_types") or {}).get("rows", [])
    names = outlet_names(sc, cfg)
    pick = lambda faq_rx, text_rx, text=alltext, n=1: faq(FQ, faq_rx, n) or stmt(text, text_rx, n)
    air = pick(r"airport|reach|get to|distance", r"airport|railway station|metro|minutes? (?:from|to)|\bkm from\b|miles from")
    pickup = faq(FQ, r"pick-?up|drop-?off|transfer", 1)
    park = pick(r"parking", r"parking|valet")
    ev = faq(FQ, r"\bev\b|electric|charging", 1)
    near = pick(r"places to visit|near|attractions|sightseeing", r"walking distance|close to|landmarks?|attractions", n=2)
    AVRX = r"video[- ]?conferenc|audio[- ]?visual|\bAV\b|technolog|projector|sound system|LED|screens?\b|microphone|wi-?fi|stage lighting"
    av = max(re.split(r"(?<=[.!?])\s+(?=[A-Z])", stmt(meet or alltext, AVRX, 20)) or [""], key=lambda x: len(re.findall(AVRX, x, re.I)))
    amen = [a for a, rx in [("Swimming pool", r"\bpool\b"), ("Spa", r"\bspa\b"), ("Fitness centre", r"fitness|\bgym\b"), ("Wellness", r"wellness|ayurved|yoga"),
                            ("Gardens", r"\bgardens?\b"), ("Kids' club", r"kids'? club"), ("Wi-Fi", r"wi-?fi"), ("Parking", r"parking|valet"),
                            ("EV charging", r"\bev charg|electric vehicle"), ("Airport transfers", r"pick-?up|drop-?off|airport transfer"),
                            ("Beach access", r"\bbeach\b"), ("Business centre", r"business cent")] if re.search(rx, corpus if rx else "", re.I)]
    # ---- deterministic, grounded copy (only fills what the scrape left empty; overrides always win later) ----
    if not cfg.get("overview"): put("overview", esc(stmt(alltext, r"hotel|resort|palace", 2)), "page text")
    fits = []
    if air: fits.append({"icon": "aeroplane", "title": "Getting Here", "body": esc(air.split(". ")[0].rstrip(".") + "." + (" " + pickup if pickup and pickup not in air else ""))})
    elif cfg.get("address"): fits.append({"icon": "aeroplane", "title": "Getting Here", "body": esc("Located at " + html.unescape(cfg["address"]) + ".")})
    if big:
        top_cap = max(big["seating"].values(), key=lambda x: int(re.sub(r"[^0-9]", "", x) or 0))
        small = min(venues, key=cap)
        span = f"from the {cap(small)}-seat {small['title']} to the {big['title']}" if len(venues) > 1 and cap(small) else f"including the {big['title']}"
        fits.append({"icon": "house", "title": "Event Spaces", "body": esc(f"{len(venues)} venues, {span}, for up to {top_cap} guests.")})
    elif stmt(meet, r"sq|square|meeting room"): fits.append({"icon": "house", "title": "Event Spaces", "body": esc(stmt(meet, r"sq|square|meeting room"))})
    sizes = [int(s) for r in rt for s in re.findall(r"\d+", r[1])] if rt and all("sq m" in r[1] or not r[1] for r in rt) else []
    if f.get("rooms") or rt:
        fits.append({"icon": "money", "title": "Rooms &amp; Suites", "body": esc((f.get("rooms", "").capitalize() or f"{len(rt)} room types") + (f", from {min(sizes)} to {max(sizes)} sq m" if sizes else "") + ".")})
    if cfg.get("highlights"): fits.append({"icon": "star", "title": "Highlights", "body": esc(" · ".join(cfg["highlights"][:3]))})
    elif amen: fits.append({"icon": "star", "title": "Amenities", "body": esc(" · ".join(amen[:4]))})
    fits = [x for x in fits if x["body"]][:4]
    if not any(x.get("body") for x in cfg.get("fits", [])) and fits: put("fits", fits, "facts + FAQ + page text")
    # rooms intro: always rebuilt from facts (the scraped one is often just a page heading)
    rparts = []
    if f.get("rooms"): rparts.append(f["rooms"].capitalize() + (f", from {min(sizes)} to {max(sizes)} sq m" if sizes else "") + ".")
    elif rt: rparts.append(f"{len(rt)} room types" + (f", from {min(sizes)} to {max(sizes)} sq m" if sizes else "") + ".")
    desc = stmt(rooms_t, r"room|suite|view|bed") or stmt(alltext, r"\b(rooms?|suites?)\b.*\b(view|bed|balcon|terrace|design|furnish|decor)")
    if desc and f.get("rooms") and f["rooms"].lower() in desc.lower(): rparts = []
    if desc: rparts.append(desc)
    if rparts: put("rooms_intro", esc(" ".join(rparts)), "facts + rooms page")
    if not cfg.get("meeting_intro"): put("meeting_intro", esc(stmt(meet, r"meeting|event|venue", 1) or "Talk to our events team about the right space for your group."), "meetings page")
    if not cfg.get("dining_intro"):
        lead = (f.get("outlets", "").capitalize() + (": " if names else ".")) if f.get("outlets") else ("Dining at the hotel: " if names else "")
        body = (", ".join(names[:-1]) + " and " + names[-1] + ".") if len(names) > 1 else (names[0] + "." if names else stmt(page_text(sc, cfg, r"dining|restaurant"), r"restaurant|bar|cuisine", 2))
        put("dining_intro", esc((lead + body).strip() or "Ask us about dining options for your group."), "dining page")
    if not cfg.get("av_support"): put("av_support", esc(av or "Ask our events team about audio-visual requirements."), "meetings page")
    if not cfg.get("location_cards"):
        ci = " / ".join(x for x in [("Check-in from " + f["checkin"]) if f.get("checkin") else "", ("check-out until " + f["checkout"]) if f.get("checkout") else ""] if x)
        put("location_cards", [{"icon": "location", "title": "Getting Here", "body": esc(" ".join(x for x in [air, pickup if pickup not in air else ""] if x) or html.unescape(cfg.get("address", "")))},
                               {"icon": "parking", "title": "Parking" + (" &amp; EV Charging" if ev else ""), "body": esc(" ".join(x for x in [park, ev] if x) or "Ask the hotel about parking options.")},
                               {"icon": "report", "title": "Check-in / Check-out", "body": esc(ci + "." if ci else "See hotel policies.")},
                               {"icon": "star", "title": "Nearby", "body": esc(near or "Ask us about nearby attractions.")}], "FAQ + page text")
    if not cfg.get("amenities_line"): put("amenities_line", esc(" · ".join(amen) if amen else "Ask us about onsite amenities."), "page text + FAQ")
    # ---- optional AI polish (OpenAI-compatible endpoint, e.g. a local Ollama). Rejected unless fully grounded ----
    polished = {}
    if os.environ.get("LLM_BASE_URL"):
        for k in ("overview", "dining_intro"):
            src = html.unescape(out.get(k) or cfg.get(k) or "")
            if not src: continue
            try:
                txt = llm_rewrite(src, corpus)
                bad = ungrounded(txt, corpus)
                if bad: log(f"  ⚠ AI rewrite of {k} rejected (not in hotel text: {bad[:6]})")
                elif txt: polished[k] = esc(txt); log(f"  ✔ AI rewrite of {k} accepted (grounded)")
            except Exception as e: log(f"  ⚠ AI rewrite of {k} skipped: {e}")
        out.update(polished)
    # ---- grounding check on everything we produced ----
    problems = {}
    for k, v in out.items():
        bad = ungrounded(json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v, corpus)
        if bad: problems[k] = bad
    jwrite(f"{d}/copy.json", {"fields": out, "sources": sources, "ai_polished": sorted(polished), "ungrounded": problems})
    log(f"✔ copy: filled {sorted(out)}" + (f"; AI-polished {sorted(polished)}" if polished else "") + (f"; ⚠ ungrounded {problems}" if problems else "; all grounded in the hotel's text"))

def llm_rewrite(src, corpus):
    body = {"model": os.environ.get("LLM_MODEL", "llama3.1"), "temperature": 0.2, "messages": [
        {"role": "system", "content": "You rewrite hotel marketing copy for event planners. Use ONLY facts in the source text. Do not add numbers, names, places or claims. Max 60 words. Plain text."},
        {"role": "user", "content": "Source text:\n" + src + "\n\nRewrite it as one polished paragraph."}]}
    req = urllib.request.Request(os.environ["LLM_BASE_URL"].rstrip("/") + "/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"content-type": "application/json", **({"authorization": "Bearer " + os.environ["LLM_API_KEY"]} if os.environ.get("LLM_API_KEY") else {})})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)["choices"][0]["message"]["content"].strip()


# ---------------------------------------------------------------------------------------------- build
CVENT_PREF = {"${Event Name}": "${response.event_name}", "${Event Dates}": "${response.event_dates}", "${Check-In Date}": "${response.check_in_date}",
              "${Check-Out Date}": "${response.check_out_date}", "${Total Room Nights}": "${response.total_room_nights}"}

def email_to_cvent_map():
    m = {}
    for label, cv, em in DEFAULT_MERGE_FIELDS:
        if em in CVENT_PREF: m[em] = CVENT_PREF[em]
        elif em not in m: m[em] = cv
    return m

def to_cvent(tpl):
    m = email_to_cvent_map(); s = json.dumps(tpl, ensure_ascii=False)
    for em, cv in sorted(m.items(), key=lambda kv: -len(kv[0])):
        s = s.replace(em, cv)
        if "&" in em: s = s.replace(em.replace("&", "&amp;"), cv)
    return json.loads(s)

def merged_config(d):
    cfg = jread(f"{d}/config.json"); cp = (jread(f"{d}/copy.json") or {}).get("fields", {}); ov = jread(f"{d}/overrides.json", {}) or {}
    def merge(base, over):
        for k, v in over.items():
            if isinstance(v, dict) and isinstance(base.get(k), dict) and not k.startswith("_"): merge(base[k], v)
            else: base[k] = v
        return base
    raw = merge(merge(_copy.deepcopy(cfg), cp), ov)
    return {k: v for k, v in raw.items() if not k.startswith("_")}, cfg

def stage_build(d):
    inp = jread(f"{d}/input.json"); h, cfg = merged_config(d)
    missing = [k for k in B.REQUIRED if not h.get(k)]
    if missing: fail(f"config still missing {missing}: add them via POST /deal-rooms/:id/overrides")
    for slot, keys in h["slots"].items():
        bad = [k for k in keys if k not in h["media"]]
        if bad: fail(f"slot '{slot}' references unknown media {bad}")
    tpl, checks, P = B.build(h)
    errs, warns, toks = B.validate(tpl, h, checks)
    if os.environ.get("ALLOW_HTTP") == "1":           # local testing only: http:// image references are allowed
        warns += [e + " (allowed: ALLOW_HTTP=1)" for e in errs if e.startswith("bad image url")]
        errs = [e for e in errs if not e.startswith("bad image url")]
    if inp["flow"] == "cvent":
        tpl = to_cvent(tpl)
        s = html.unescape(json.dumps(tpl, ensure_ascii=False)); found = set(re.findall(r"\$\{[^}]*\}", s))
        cvent_ok = {cv for _, cv, _ in DEFAULT_MERGE_FIELDS}
        errs += [f"not a Cvent merge field: {t}" for t in sorted(found - cvent_ok)]
        toks = re.findall(r"\$\{[^}]*\}", s)
    report = {"hotel": h["name"], "flow": inp["flow"], "sections": [s["name"] for s in tpl["sections"]], "merge_tokens": len(toks), "unique_tokens": len(set(toks)),
              "contrast_pairs": len(checks) + 1, "palette": P, "errors": errs, "warnings": warns + cfg.get("_warnings", [])}
    jwrite(f"{d}/report.json", report)
    if errs: fail(f"validation failed with {len(errs)} error(s): {errs[:5]}")
    jwrite(f"{d}/template.json", tpl)
    open(f"{d}/loader.js", "w").write(B.make_loader(tpl, inp.get("hippo_page_id") or "auto", html.unescape(h["name"]), "Cvent" if inp["flow"] == "cvent" else "Email"))
    log(f"✔ build ({inp['flow']}): {len(tpl['sections'])} sections, {len(toks)} merge fields ({len(set(toks))} unique), {len(checks) + 1} colour pairs checked, 0 errors, {len(report['warnings'])} warnings")


# ---------------------------------------------------------------------------------------------- review
HIGH = [r"no logo", r"address not found", r"only a favicon", r"No usable", r"run on a non-overview"]

def stage_review(d):
    inp, rep, tpl = jread(f"{d}/input.json"), jread(f"{d}/report.json"), jread(f"{d}/template.json")
    h, cfg = merged_config(d); facts = jread(f"{d}/facts.json", []); cp = jread(f"{d}/copy.json", {})
    high = [w for w in rep["warnings"] if any(re.search(x, w, re.I) for x in HIGH)]
    if len(h["media"]) < 4: high.append(f"only {len(h['media'])} photos found")
    ov = jread(f"{d}/overrides.json", {}) or {}
    # Re-check at review time so a grounding-normalisation improvement does not
    # leave an old copy.json permanently blocked. A person's override still
    # explicitly owns that field.
    corpus = corpus_of(jread(f"{d}/scrape.json"), cfg)
    ungrounded_left = {}
    for k, v in (cp.get("fields") or {}).items():
        if k in ov: continue
        text = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
        bad = ungrounded(text, corpus)
        if bad: ungrounded_left[k] = bad
    if ungrounded_left: high.append(f"copy not fully grounded: {ungrounded_left}")
    unsourced = [f["field"] for f in facts if not f["verified"]]
    if unsourced: high.append(f"facts without a source: {sorted(set(unsourced))}")
    needs = inp["mode"] == "review" or bool(high)
    jwrite(f"{d}/review.json", {"needs_review": needs, "high_severity": high, "warnings": rep["warnings"]})
    # The review document is sandboxed and some hotel CDNs refuse or throttle a
    # large number of direct, cross-origin image requests.  Route preview images
    # through the app; the original source URL remains in config.json and in the
    # generated deal room.
    open(f"{d}/review.html", "w").write(review_html(os.path.basename(d), inp, rep, tpl, h, facts, cp, high))
    log(f"✔ review: {'waiting for approval' if needs else 'auto-approved'}" + (f" | high-severity: {high}" if high else ""))

def review_html(job_id, inp, rep, tpl, h, facts, cp, high):
    e = html.escape; P = rep["palette"]
    sw = "".join(f'<div class="sw"><span style="background:{v}"></span>{k}<br><code>{v}</code></div>' for k, v in P.items() if isinstance(v, str) and v.startswith("#"))
    logos = "".join(f'<img src="{e(l.get("url", ""))}" alt="logo" style="max-height:60px;background:#fff;padding:6px;border:1px solid #ddd">' for l in h.get("logos", []))
    slot_of = {}
    for slot, keys in h["slots"].items():
        for k in keys: slot_of.setdefault(k, []).append(slot)
    # Do not point the browser at the hotel CDN directly.  The preview route
    # fetches only public HTTPS images and returns an image response, avoiding
    # referrer/hotlink restrictions while preserving lazy loading.
    photos = "".join(f'<figure><img loading="lazy" src="/deal-rooms/{e(job_id)}/media/{urllib.parse.quote(k, safe="")}" alt="{e(m.get("_alt", ""))}"><figcaption><b>{e(k)}</b> → {e(", ".join(slot_of.get(k, ["(unused)"])))}<br>{e(m.get("_alt", "")[:70])}</figcaption></figure>' for k, m in h["media"].items())
    frows = "".join(f'<tr><td>{e(f["field"])}</td><td>{e(str(f["value"]))}</td><td>{"✔" if f["verified"] else "✖"} {e(str(f["source_url"] or "no source"))}<br><small>{e(f["quote"])}</small></td></tr>' for f in facts)
    crows = "".join(f'<tr><td>{e(k)}</td><td>{e(json.dumps(v, ensure_ascii=False)[:600])}</td><td>{e(cp.get("sources", {}).get(k, ""))}</td></tr>' for k, v in cp.get("fields", {}).items())
    secs = "".join(f"<li>{e(s)}</li>" for s in rep["sections"])
    warns = "".join(f"<li>{e(w)}</li>" for w in rep["warnings"])
    hi = "".join(f"<li>{e(w)}</li>" for w in high)
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Review: {e(h['name'])}</title>
<style>body{{font:14px/1.5 system-ui,sans-serif;margin:0;background:#f6f6f4;color:#222}}main{{max-width:1100px;margin:auto;padding:24px}}
section{{background:#fff;border-radius:12px;padding:20px;margin:16px 0;box-shadow:0 1px 3px #0001}}h1{{margin:0}}h2{{margin-top:0}}
.sw{{display:inline-block;width:120px;font-size:12px;margin:4px}}.sw span{{display:block;height:36px;border-radius:6px;border:1px solid #ccc}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}}figure{{margin:0}}figure img{{width:100%;height:140px;object-fit:cover;border-radius:8px;background:#ddd}}
table{{width:100%;border-collapse:collapse}}td,th{{border-top:1px solid #eee;padding:6px;vertical-align:top;text-align:left}}.bad{{color:#a00}}code{{font-size:11px}}</style></head><body><main>
<h1>{e(h['name'])}</h1><p>{e(inp['hotel_url'])} · flow <b>{e(inp['flow'])}</b> · {rep['unique_tokens']} merge fields · {rep['contrast_pairs']} colour pairs checked · 0 validation errors</p>
<section><h2>Decision</h2>{('<p class="bad"><b>High-severity items:</b></p><ul class="bad">' + hi + '</ul>') if high else '<p>No high-severity items.</p>'}
<p>Approve: <code>POST /deal-rooms/&lt;id&gt;/approve</code> · Edit: <code>POST /deal-rooms/&lt;id&gt;/overrides</code></p></section>
<section><h2>Brand</h2>{sw}<p>Heading font: <b>{e(str((h.get('fonts') or {}).get('heading', {}).get('site_font')))}</b> → <code>{e(str((h.get('fonts') or {}).get('heading', {}).get('stack')))}</code><br>
Body font: <b>{e(str((h.get('fonts') or {}).get('body', {}).get('site_font')))}</b> → <code>{e(str((h.get('fonts') or {}).get('body', {}).get('stack')))}</code></p>{logos}</section>
<section><h2>Photos and where they go</h2><div class="grid">{photos}</div></section>
<section><h2>Facts and sources</h2><table><tr><th>Fact</th><th>Value</th><th>Source</th></tr>{frows}</table></section>
<section><h2>Generated copy</h2><table><tr><th>Field</th><th>Text</th><th>From</th></tr>{crows}</table></section>
<section><h2>Sections</h2><ol>{secs}</ol><h3>Warnings</h3><ul>{warns or '<li>none</li>'}</ul></section>
</main></body></html>"""


# ---------------------------------------------------------------------------------------------- Hippo API client
class Hippo:
    """Hippo deal-room API. Auth: HIPPO_API_TOKEN (service key, sent as Bearer) or, as an interim, HIPPO_COOKIE + HIPPO_CSRF."""
    def __init__(self):
        self.base = os.environ.get("HIPPO_BASE_URL", "https://www.hippovideo.io").rstrip("/")
        self.h = {"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}
        if os.environ.get("HIPPO_API_TOKEN"): self.h["Authorization"] = "Bearer " + os.environ["HIPPO_API_TOKEN"]
        elif os.environ.get("HIPPO_COOKIE"):
            self.h["Cookie"] = os.environ["HIPPO_COOKIE"]
            if os.environ.get("HIPPO_CSRF"): self.h["X-CSRF-Token"] = os.environ["HIPPO_CSRF"]
        else: fail("load=api needs HIPPO_API_TOKEN (or HIPPO_COOKIE + HIPPO_CSRF) in the environment")
    def _req(self, method, path, data=None, ctype=None):
        hd = dict(self.h)
        if ctype: hd["Content-Type"] = ctype
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=hd)
        with urllib.request.urlopen(req, timeout=60) as r: return r.status, r.read().decode()
    def get_template(self, pid):
        st, body = self._req("GET", f"/api/video_page/templates/{pid}/editor?fromPreview=false")
        j = json.loads(body)
        if not j.get("template", {}).get("sections"): raise RuntimeError("page has no template/sections")
        return j
    def save(self, pid, tpl):
        keep = {k: tpl[k] for k in B.SAVE_KEYS if k in tpl}
        data = urllib.parse.urlencode({"template": json.dumps(keep, ensure_ascii=False), "page_id": pid}).encode()
        st, body = self._req("PATCH", "/api/video_page/pages", data, "application/x-www-form-urlencoded; charset=UTF-8")
        ok = st == 200 and '"success":true' in body.replace(" ", "")
        return ok, st, body[:300]

UPLOADED = re.compile(r"hippovideo\.io/video/thumbnail|assets\.hippovideo\.io")

def stage_load(d):
    inp = jread(f"{d}/input.json"); tpl = jread(f"{d}/template.json")
    if inp["load"] == "manual":
        jwrite(f"{d}/load.json", {"mode": "manual", "status": "ready", "how": "Paste loader.js in the browser Console / a DevTools Snippet on deal-room/edit/<page>"})
        log("✔ load: manual mode, loader.js is ready (backup + confirm + __hvRestore built in)"); return
    hp, pid = Hippo(), inp["hippo_page_id"]
    cur = hp.get_template(pid)
    if os.path.exists(f"{d}/backup.json"):            # keep the page as it was before this job first touched it
        os.makedirs(f"{d}/backups", exist_ok=True); jwrite(f"{d}/backups/backup-{int(time.time())}.json", cur)
        log(f"  page {pid} already has a backup from this job's first load (kept); current state saved to backups/")
    else:
        jwrite(f"{d}/backup.json", cur); log(f"  backup saved: page {pid}, {len(cur['template']['sections'])} sections")
    page_logos = [l for l in cur["template"].get("multiple_logo_top", []) if UPLOADED.search((l.get("company_logo_top") or {}).get("src", ""))]
    if not any(UPLOADED.search((l.get("company_logo_top") or {}).get("src", "")) for l in tpl["multiple_logo_top"]) and page_logos:
        tpl["multiple_logo_top"] = page_logos; jwrite(f"{d}/template.json", tpl); log(f"  kept {len(page_logos)} uploaded logo(s) already on the page")
    ok, st, body = hp.save(pid, tpl)
    jwrite(f"{d}/load.json", {"mode": "api", "page_id": pid, "http": st, "response": body, "ok": ok, "at": time.time()})
    if not ok: fail(f"Hippo save failed (HTTP {st}): {body}")
    log(f"✔ load: saved to page {pid}")


# ---------------------------------------------------------------------------------------------- verify + rollback
def tokens(obj): return sorted(set(re.findall(r"\$\{[^}]*\}", html.unescape(json.dumps(obj, ensure_ascii=False)))))

def stage_verify(d):
    inp = jread(f"{d}/input.json"); tpl = jread(f"{d}/template.json")
    if inp["load"] != "api":
        jwrite(f"{d}/verify.json", {"status": "skipped", "reason": "manual load: verify after pasting the loader by re-running with load=api, or check the page"}); log("✔ verify: skipped (manual load)"); return
    hp, pid = Hippo(), inp["hippo_page_id"]
    got = hp.get_template(pid)["template"]
    crit, notes = [], []
    want_s, got_s = [(s["id"], s["name"]) for s in tpl["sections"]], [(s["id"], s["name"]) for s in got.get("sections", [])]
    if want_s != got_s: crit.append(f"sections differ: sent {len(want_s)}, page has {len(got_s)}")
    for ws, gs in zip(tpl["sections"], got.get("sections", [])):
        wt, gt = [c["type"] for c in ws["components"]], [c["type"] for c in gs.get("components", [])]
        if wt != gt: crit.append(f"components differ in '{ws['name']}': {wt} vs {gt}")
    tw, tg = tokens(tpl["sections"]), tokens(got.get("sections", []))
    if set(tw) - set(tg): crit.append(f"merge fields missing after save: {sorted(set(tw) - set(tg))[:8]}")
    if set(tg) - set(tw): notes.append(f"extra merge fields on page: {sorted(set(tg) - set(tw))[:8]}")
    for k in ("primaryColor", "menuColor", "secondaryColor"):
        if (tpl["settings"].get(k) or "").upper() != (got.get("settings", {}).get(k) or "").upper(): notes.append(f"settings.{k}: sent {tpl['settings'].get(k)}, page has {got.get('settings', {}).get(k)}")
    unfilled = [s["name"] for s in got.get("sections", []) for c in s.get("components", []) if c.get("type") == "TextCard" for cd in c["data"].get("cards", []) if not cd.get("color")]
    if unfilled: notes.append(f"cards without fill in: {sorted(set(unfilled))}")
    if not any(UPLOADED.search((l.get("company_logo_top") or {}).get("src", "")) for l in got.get("multiple_logo_top", [])): notes.append("no uploaded logo on the page: upload the hotel logo once in the editor header")
    res = {"status": "failed" if crit else "passed", "critical": crit, "notes": notes, "checked_tokens": len(tw)}
    jwrite(f"{d}/verify.json", res)
    if crit and os.environ.get("AUTO_ROLLBACK", "1") == "1":
        log("✖ verify failed: " + "; ".join(crit)); stage_rollback(d); fail("verify failed, page rolled back to the backup")
    if crit: fail("verify failed: " + "; ".join(crit))
    log(f"✔ verify: page matches ({len(tw)} merge fields, {len(want_s)} sections)" + (f" | notes: {notes}" if notes else ""))

def stage_rollback(d):
    inp = jread(f"{d}/input.json"); bk = jread(f"{d}/backup.json")
    if not bk: fail("no backup.json in this job: nothing to roll back")
    ok, st, body = Hippo().save(inp["hippo_page_id"], bk["template"])
    jwrite(f"{d}/rollback.json", {"ok": ok, "http": st, "response": body, "at": time.time()})
    if not ok: fail(f"rollback failed (HTTP {st}): {body}")
    log(f"✔ rollback: page {inp['hippo_page_id']} restored from backup")


STAGES = {"intake": stage_intake, "capture": stage_capture, "convert": stage_convert, "facts": stage_facts, "copy": stage_copy,
          "build": stage_build, "review": stage_review, "load": stage_load, "verify": stage_verify, "rollback": stage_rollback}

if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in STAGES: print("usage: stages.py <" + "|".join(STAGES) + "> <job_dir>"); sys.exit(64)
    d = os.path.abspath(sys.argv[2])
    if not os.path.isfile(f"{d}/job.json"): print("✖ no job.json in", d); sys.exit(64)
    STAGES[sys.argv[1]](d)
