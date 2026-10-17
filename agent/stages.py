"""Deal Room Agent: pipeline stages.

Usage (called by the Node job runner, argv only, never through a shell):
    python3 agent/stages.py <stage> <job_dir>

Stages (each reads/writes files in <job_dir>; exit code 0 = ok, 2 = permanent failure, 3 = transient failure the runner may retry):
    intake       -> input.json checked, normalised, hotel_id derived       (job.json input)
    capture      -> scrape.json, capture/page.png [, capture/page.har]     (headless Chromium runs scrape_hotel.js; robots.txt, SSRF and redirect checks)
    convert      -> config.json                                            (scrape_to_config.convert)
    facts        -> facts.json  (every hard fact with source_url + quote)
    copy         -> copy.json   (grounded copy for empty fields; optional LLM polish with grounding check)
    profile      -> customization.json, profile.json, profile_validation.json   (versioned hotel profile with provenance; validation levels 1-3)
    plan         -> plan.json   (template family, section plan, omissions, unmet requests: deterministic rules)
    assets       -> assets.json (every image fetched once: type, size, dimensions, hash; broken ones dropped)
    build        -> template.json, report.json, bindings.json, loader.js   (dealroom_builder + validation; email or cvent flow)
    map          -> mapping.json (hotel bindings with provenance + the Hippo merge fields the template uses)
    qa           -> qa.json     (PASS / REVIEW / FAIL)
    review       -> review.html, review.json (needs_review / severity)
    publish_gate -> publication.json (re-checks the gate, promotes the hotel profile version, records the generation)
    load         -> backup.json, load.json        (Hippo API) | loader.js only (manual)
    verify       -> verify.json                   (read-back diff; auto-rollback on critical mismatch)
    rollback     -> rollback.json                 (restores backup.json)

A failing stage also writes stage_error.json (code, category, retryable, missing fields) for the runner's exception queue.

Deterministic by design: merge fields, colours, IDs and the save format come from code. The only optional AI
step is copy polishing, and its output is rejected unless every number and name in it appears in the hotel's text.
"""
import json, os, re, sys, html, socket, time, urllib.error, urllib.parse, urllib.request, copy as _copy

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import scrape_to_config as S2C                       # noqa: E402
import dealroom_builder as B                         # noqa: E402
import hotel_profile as HP                           # noqa: E402
import customize as CU                               # noqa: E402
import assets as AS                                  # noqa: E402
import mapping as MP                                 # noqa: E402
import qa as QA                                      # noqa: E402
import netsafe                                       # noqa: E402
from common import audit, jread, jwrite, now_iso     # noqa: E402
from hippo_fields import DEFAULT_MERGE_FIELDS, EMAIL_TOKENS   # noqa: E402

OTA = re.compile(r"(^|\.)(booking|expedia|agoda|hotels|tripadvisor|trivago|kayak|makemytrip|goibibo|airbnb|priceline|orbitz|hotelscombined|trip|cleartrip|yatra|ebookers|travelocity)\.(com|co\.\w+|in|de|fr|net)$", re.I)
EXIT_PERMANENT, EXIT_TRANSIENT = 2, 3
DEFAULT_CATEGORY = {"intake": "INVALID_INPUT", "capture": "SOURCE_UNAVAILABLE", "convert": "REQUIRED_INFO_MISSING", "facts": "INTERNAL_ERROR", "copy": "INTERNAL_ERROR",
                    "profile": "REQUIRED_INFO_MISSING", "plan": "UNSUPPORTED_TEMPLATE", "assets": "ASSET_MISSING", "build": "MERGE_FIELD_UNMAPPED", "map": "MERGE_FIELD_UNMAPPED",
                    "qa": "RENDER_FAILURE", "review": "INTERNAL_ERROR", "publish_gate": "PUBLISH_FAILURE", "load": "PUBLISH_FAILURE", "verify": "PUBLISH_FAILURE", "rollback": "PUBLISH_FAILURE"}
_JOB_DIR = _STAGE = None

def log(*a): print(*a, flush=True)

def fail(msg, code="STAGE_FAILED", category=None, retryable=False, retry_after=None, missing_fields=(), details=None):
    """Stop the stage. Exit 3 when retrying later may help (the runner backs off and retries), else 2. Always records why."""
    log("✖ " + msg)
    if _JOB_DIR:
        jwrite(f"{_JOB_DIR}/stage_error.json", {"stage": _STAGE, "code": code, "category": category or DEFAULT_CATEGORY.get(_STAGE, "INTERNAL_ERROR"), "retryable": retryable,
                                               "retry_after": retry_after, "message": msg, "missing_fields": list(missing_fields), "details": details or {}, "at": now_iso()})
    sys.exit(EXIT_TRANSIENT if retryable else EXIT_PERMANENT)

def hotels_dir(d):
    return os.environ.get("HOTELS_DIR") or os.path.join(os.path.dirname(os.path.dirname(d)), "hotels")


# ---------------------------------------------------------------------------------------------- intake
def stage_intake(d):
    job = jread(f"{d}/job.json"); inp = job["input"]
    u = urllib.parse.urlparse(inp.get("hotel_url", "").strip())
    if u.scheme not in ("https", "http") or not u.netloc: fail("hotel_url must be a full https:// URL", code="BAD_URL")
    if u.scheme == "http" and os.environ.get("ALLOW_HTTP") != "1": fail("hotel_url must use https", code="BAD_URL")
    host = u.hostname or ""
    if OTA.search(host): fail(f"{host} is a booking/travel site, not the hotel's official website. Use the hotel's own page (e.g. the chain's hotel page).", code="OTA_URL", category="SOURCE_UNVERIFIED")
    try: netsafe.check_url(inp["hotel_url"].strip(), resolve=False)           # credentials, odd ports, literal private addresses (DNS is checked at capture)
    except netsafe.Unsafe as e: fail(f"hotel_url is not allowed: {e}", code="URL_NOT_ALLOWED", category="SOURCE_UNVERIFIED")
    allow = [x.strip().lower() for x in os.environ.get("ALLOWED_HOTEL_DOMAINS", "").split(",") if x.strip()]
    if allow and not any(host.lower() == a or host.lower().endswith("." + a) for a in allow): fail(f"{host} is not in ALLOWED_HOTEL_DOMAINS", code="DOMAIN_NOT_ALLOWED", category="SOURCE_UNVERIFIED")
    keep = {k: v for k, v in urllib.parse.parse_qsl(u.query) if not re.match(r"(utm_|gclid|gbraid|fbclid|cm_mmc|mktgvar|clk_src|partner|trso|srb_|hmguid)", k, re.I)}
    clean = urllib.parse.urlunparse(u._replace(query=urllib.parse.urlencode(keep), fragment=""))
    if inp.get("flow", "email") not in ("email", "cvent"): fail("flow must be email or cvent", code="BAD_INPUT")
    if inp.get("mode", "review") not in ("review", "auto"): fail("mode must be review or auto", code="BAD_INPUT")
    if inp.get("load", "manual") not in ("manual", "api"): fail("load must be manual or api", code="BAD_INPUT")
    pid = str(inp.get("hippo_page_id", "") or "")
    if pid and not re.fullmatch(r"\d{1,12}", pid): fail("hippo_page_id must be digits", code="BAD_INPUT")
    if inp.get("load") == "api" and not pid: fail("load=api needs hippo_page_id", code="BAD_INPUT")
    opp = str(inp.get("opportunity_id", "") or "")
    if opp and not re.fullmatch(r"[A-Za-z0-9_.:\-]{1,64}", opp): fail("opportunity_id may only contain letters, digits and _ . : - (max 64)", code="BAD_INPUT")
    _, cerrs = CU.validate_customization(inp.get("customization"))
    if cerrs: fail("invalid customization: " + "; ".join(cerrs), code="CUSTOMIZATION_INVALID", category="INVALID_INPUT", details={"errors": cerrs})
    ident = HP.hotel_identity(clean)
    norm = {**inp, "hotel_url": clean, "hippo_page_id": pid, "flow": inp.get("flow", "email"), "mode": inp.get("mode", "review"), "load": inp.get("load", "manual"),
            "opportunity_id": opp, "hotel_id": ident["hotel_id"]}
    jwrite(f"{d}/input.json", norm)
    log(f"✔ intake: {clean} | hotel {ident['hotel_id']} | flow={norm['flow']} mode={norm['mode']} load={norm['load']} page={pid or 'auto'}" + (f" opportunity={opp}" if opp else ""))


# ---------------------------------------------------------------------------------------------- capture
COOKIE_BUTTONS = ["Accept all", "Accept All", "Accept all cookies", "Accept Cookies", "Accept", "I agree", "I Accept", "Allow all", "Agree", "OK", "Got it"]

PERMANENT_NAV = re.compile(r"ERR_(NAME_NOT_RESOLVED|CERT_|SSL_|INVALID_URL|UNSAFE_PORT|UNSAFE_REDIRECT|BLOCKED_BY_CLIENT|FAILED\b|ADDRESS_INVALID|DISALLOWED_URL_SCHEME)")

def stage_capture(d):
    from playwright.sync_api import sync_playwright
    inp = jread(f"{d}/input.json"); os.makedirs(f"{d}/capture", exist_ok=True)
    start = inp["hotel_url"]; host = urllib.parse.urlparse(start).hostname or ""
    # ---- source checks before any browser starts: public address, robots.txt ----
    try: netsafe.check_url(start)
    except netsafe.Unsafe as e: fail(f"hotel_url is not allowed: {e}", code="URL_NOT_ALLOWED", category="SOURCE_UNVERIFIED")
    policy, rp = os.environ.get("ROBOTS_POLICY", "enforce").lower(), None
    if policy != "off":
        try: rp = netsafe.robots_for(start)
        except netsafe.Transient as e: fail(f"could not read robots.txt: {e}", code="ROBOTS_UNREADABLE", category="SOURCE_UNAVAILABLE", retryable=True, retry_after=e.retry_after)
        except netsafe.Unsafe as e: fail(f"robots.txt request was blocked: {e}", code="URL_NOT_ALLOWED", category="SOURCE_UNVERIFIED")
        if not netsafe.robots_allows(rp, start):
            msg = f"{host}'s robots.txt does not allow automated access to {urllib.parse.urlparse(start).path or '/'}"
            if policy == "enforce": fail(msg + ". Run agent/scrape_hotel.js in your own browser and upload the file as `scrape`, or ask the hotel for permission.", code="ROBOTS_DISALLOW", category="SOURCE_UNAVAILABLE")
            log("  ⚠ " + msg + " (ROBOTS_POLICY=warn: continuing)")
    js = open(os.path.join(HERE, "scrape_hotel.js")).read()
    har = os.environ.get("CAPTURE_HAR") == "1"
    blocked, skipped = [], []

    def guard(route, request):
        """Chromium's own requests: never reach a private network, never fetch pages robots.txt disallows."""
        u = urllib.parse.urlparse(request.url)
        if u.scheme in ("data", "blob", "about", "chrome-error"): return route.continue_()
        # Chromium resolves names itself, so this check is a filter, not a guarantee (DNS can answer differently the second time):
        # production should also deny private ranges at the network edge (see README). An unresolvable name is refused.
        try: port = u.port
        except ValueError: port = -1
        if u.scheme not in ("http", "https") or (port not in (None, 80, 443) and not netsafe.allow_private()) or not netsafe.host_is_public(u.hostname, fail_closed=True):
            blocked.append(request.url); return route.abort()
        if policy == "enforce" and rp is not None and request.resource_type in ("document", "fetch", "xhr") and netsafe.same_site(u.hostname, host) and not netsafe.robots_allows(rp, request.url):
            skipped.append(request.url); return route.abort()
        route.continue_()

    logs = []
    with sync_playwright() as p:
        br = p.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
        try:
            ctx = br.new_context(viewport={"width": 1440, "height": 900}, locale="en-US", accept_downloads=True, service_workers="block",
                                 user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
                                 **({"record_har_path": f"{d}/capture/page.har", "record_har_content": "omit"} if har else {}))
            ctx.route("**/*", guard)
            pg = ctx.new_page()
            pg.on("console", lambda m: logs.append(m.text) if "[hotel-scraper]" in m.text and len(logs) < 200 else None)
            try: r = pg.goto(start, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                msg = str(e).splitlines()[0]
                if PERMANENT_NAV.search(msg): fail(f"could not open the page: {msg}", code="NAVIGATION_FAILED", category="SOURCE_UNAVAILABLE")
                fail(f"could not open the page: {msg}", code="NAVIGATION_TRANSIENT", category="SOURCE_UNAVAILABLE", retryable=True)
            if r is None: fail("page returned no response", code="NO_RESPONSE", category="SOURCE_UNAVAILABLE", retryable=True)
            if r.status in (429, 500, 502, 503, 504):
                ra = r.headers.get("retry-after", "")
                fail(f"page returned HTTP {r.status}", code="HTTP_TRANSIENT", category="SOURCE_UNAVAILABLE", retryable=True, retry_after=int(ra) if ra.isdigit() else None)
            if r.status in (401, 403): fail(f"page returned HTTP {r.status}: the site refuses automated access (not bypassed). Upload a scrape made in your own browser instead.", code="ACCESS_REFUSED", category="SOURCE_UNAVAILABLE")
            if r.status >= 400: fail(f"page returned HTTP {r.status}", code="HTTP_ERROR", category="SOURCE_UNAVAILABLE")
            if not netsafe.same_site(urllib.parse.urlparse(pg.url).hostname, host):
                fail(f"the page redirected to {urllib.parse.urlparse(pg.url).hostname}, which is not {host}. Use the hotel's final official URL.", code="REDIRECT_OFF_SITE", category="SOURCE_UNVERIFIED")
            try: pg.wait_for_load_state("networkidle", timeout=15000)
            except Exception: pass
            for t in COOKIE_BUTTONS:                       # dismiss a cookie banner if one is showing
                try:
                    b = pg.get_by_role("button", name=t, exact=True)
                    if b.count() and b.first.is_visible(): b.first.click(timeout=2000); pg.wait_for_timeout(600); log(f"  cookie banner: clicked '{t}'"); break
                except Exception: pass
            if not netsafe.same_site(urllib.parse.urlparse(pg.url).hostname, host):         # the page may have navigated on its own since the first check
                fail(f"the page moved to {urllib.parse.urlparse(pg.url).hostname}, which is not {host}", code="REDIRECT_OFF_SITE", category="SOURCE_UNVERIFIED")
            try:
                with pg.expect_download(timeout=240000) as dl:
                    pg.evaluate(js)
            except Exception as e: fail(f"the page scraper did not finish: {str(e).splitlines()[0]}", code="SCRAPER_TIMEOUT", category="SOURCE_UNAVAILABLE", retryable=True)
            dl.value.save_as(f"{d}/scrape.json")
            try: pg.screenshot(path=f"{d}/capture/page.png", full_page=True, timeout=30000)
            except Exception as e: log("  (screenshot skipped:", e, ")")
            ctx.close()
        finally:
            br.close()
    for l in logs: log("  " + l.replace("[hotel-scraper] ", ""))
    if blocked: log(f"  ⚠ blocked {len(blocked)} request(s) to private or reserved addresses, e.g. {blocked[0]}")
    if skipped: log(f"  ⚠ skipped {len(skipped)} page request(s) disallowed by robots.txt, e.g. {skipped[0]}")
    sc = jread(f"{d}/scrape.json")
    log(f"✔ capture: {len(sc['images'])} images, {len(sc['logos'])} logos, {len(sc['subpages'])} pages, fonts {[f['family'] for f in sc['fonts']['loaded']][:4]}")


# ---------------------------------------------------------------------------------------------- convert
def stage_convert(d):
    sc = jread(f"{d}/scrape.json")
    try: cfg = S2C.convert(sc)
    except SystemExit as e: fail(str(e), code="NO_USABLE_PHOTOS", category="ASSET_MISSING")
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
    out, sources, fb = {}, {}, []          # fb: blocks with nothing sourced behind them: they are left out of the copy, never filled with filler text
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
    if not cfg.get("meeting_intro"):
        mi = stmt(meet, r"meeting|event|venue", 1)
        if mi: put("meeting_intro", esc(mi), "meetings page")
        else: fb.append("meeting_intro")                       # nothing sourced: the intro is left out, not faked
    if not cfg.get("dining_intro"):
        lead = (f.get("outlets", "").capitalize() + (": " if names else ".")) if f.get("outlets") else ("Dining at the hotel: " if names else "")
        body = (", ".join(names[:-1]) + " and " + names[-1] + ".") if len(names) > 1 else (names[0] + "." if names else stmt(page_text(sc, cfg, r"dining|restaurant"), r"restaurant|bar|cuisine", 2))
        di = (lead + (" " if lead.endswith(".") else "") + body).strip()
        if di: put("dining_intro", esc(di), "dining page")
        else: fb.append("dining_intro")
    if not cfg.get("av_support"):
        if av: put("av_support", esc(av), "meetings page")
        else: fb.append("av_support")
    if not cfg.get("location_cards"):
        ci = " / ".join(x for x in [("Check-in from " + f["checkin"]) if f.get("checkin") else "", ("check-out until " + f["checkout"]) if f.get("checkout") else ""] if x)
        getting = " ".join(x for x in [air, pickup if pickup not in air else ""] if x) or html.unescape(cfg.get("address", ""))
        cards = [{"icon": "location", "title": "Getting Here", "body": esc(getting)}] if getting else []
        if park or ev: cards.append({"icon": "parking", "title": "Parking" + (" &amp; EV Charging" if ev else ""), "body": esc(" ".join(x for x in [park, ev] if x))})
        if ci: cards.append({"icon": "report", "title": "Check-in / Check-out", "body": esc(ci + ".")})
        if near: cards.append({"icon": "star", "title": "Nearby", "body": esc(near)})
        fb += [x for x, missing in (("location_cards.parking", not (park or ev)), ("location_cards.checkin", not ci), ("location_cards.nearby", not near)) if missing]
        if cards: put("location_cards", cards, "FAQ + page text")
    if not cfg.get("amenities_line"):
        if amen: put("amenities_line", esc(" · ".join(amen)), "page text + FAQ")
        else: fb.append("amenities_line")
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
    jwrite(f"{d}/copy.json", {"fields": out, "sources": sources, "ai_polished": sorted(polished), "ungrounded": problems, "left_out": sorted(fb)})
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

def need(d, name):
    """A required artifact of an earlier stage. Jobs from before the current pipeline lack some: say so, instead of crashing."""
    v = jread(f"{d}/{name}")
    if v is None: fail(f"{name} is missing: this job was started by an older version of the agent. Apply an edit (an empty one, {{}}, is enough) to rebuild it with the current checks.", code="STALE_JOB", category="INVALID_INPUT")
    return v


def merged_config(d, prune=True, emphasis=True, branding=None):
    """The hotel config the builder renders: scrape -> grounded copy -> branding -> a reviewer's overrides.
    Photos the assets stage found unusable are dropped (when it has run), and emphasised rooms are listed first.
    The profile asks for the plain hotel instead (prune=False, emphasis=False, branding=<the hotel's saved branding>), so a one-off choice
    or a re-run can never change what is stored about the hotel."""
    cfg = jread(f"{d}/config.json"); cp = (jread(f"{d}/copy.json") or {}).get("fields", {}); ov = jread(f"{d}/overrides.json", {}) or {}
    ov = {k: v for k, v in ov.items() if k != "customization"}    # customization changes are read by the profile stage, not merged into the hotel's facts
    custom = (jread(f"{d}/customization.json") or {}).get("customization", {})
    def merge(base, over):
        for k, v in over.items():
            if isinstance(v, dict) and isinstance(base.get(k), dict) and not k.startswith("_"): merge(base[k], v)
            else: base[k] = v
        return base
    raw = merge(CU.apply_branding(merge(_copy.deepcopy(cfg), cp), custom.get("branding") if branding is None else branding), ov)
    h = {k: v for k, v in raw.items() if not k.startswith("_")}
    doc = jread(f"{d}/assets.json")
    if prune and doc: h, _ = AS.prune_media(h, doc.get("records", {}), doc.get("policy", "off"))
    plan = jread(f"{d}/plan.json")
    if plan and emphasis: h = CU.apply_emphasis(h, plan["flags"].get("emphasize_rooms"))
    return h, cfg


# ---------------------------------------------------------------------------------------------- profile (versioned, with provenance)
def stage_profile(d):
    inp, sc, cfg0 = jread(f"{d}/input.json"), jread(f"{d}/scrape.json"), jread(f"{d}/config.json")
    facts, cp, ov = jread(f"{d}/facts.json", []), jread(f"{d}/copy.json", {}), jread(f"{d}/overrides.json", {}) or {}
    ident = HP.hotel_identity(inp["hotel_url"]); store = HP.Store(hotels_dir(d)); hid = ident["hotel_id"]
    saved = store.customization(hid)
    if inp.get("save_customization") and inp.get("customization"):          # "make this the hotel's default from now on"
        by = (jread(f"{d}/job.json") or {}).get("created_by", "pipeline")
        merged, mver, merrs = CU.effective(saved, inp["customization"], {})
        if merrs: fail("invalid customization: " + "; ".join(merrs), code="CUSTOMIZATION_INVALID", category="INVALID_INPUT", details={"errors": merrs})
        if merged != saved:
            store.save_customization(hid, merged, mver, by); saved = store.customization(hid)
            audit(d, "hotel_customization_saved", hotel_id=hid, version=mver, by=by)
    custom, ver, errs = CU.effective(saved, inp.get("customization") or {}, ov.get("customization") or {})
    if errs: fail("invalid customization: " + "; ".join(errs), code="CUSTOMIZATION_INVALID", category="INVALID_INPUT", details={"errors": errs})
    jwrite(f"{d}/customization.json", {"hotel_id": hid, "customization": custom, "version": ver,
                                       "layers": {"hotel_saved": bool(saved), "request": bool(inp.get("customization")), "reviewer": bool(ov.get("customization"))}})
    h, cfg = merged_config(d, prune=False, emphasis=False, branding=saved.get("branding") or {})
    bad_colours = [f"brand.{k}" for k in ("primary", "dark", "tint", "accent", "text") if (h.get("brand") or {}).get(k) is not None and not re.fullmatch(r"#[0-9A-Fa-f]{6}", str((h.get("brand") or {})[k]))]
    if bad_colours: fail(f"{', '.join(bad_colours)} must be a #RRGGBB colour", code="BAD_BRAND_COLOUR", category="INVALID_INPUT", missing_fields=bad_colours)
    profile = HP.build_profile(hotel=ident, inp=inp, h=h, cfg=cfg, facts=facts, cp=cp, ov=ov, custom=saved, scrape=sc, plan_branding=saved.get("branding"))
    pv = HP.validate_profile(profile, allow_http=os.environ.get("ALLOW_HTTP") == "1")
    if pv["critical"]:                      # an invalid profile is shown to the reviewer in this job, but never becomes the hotel's stored profile
        jwrite(f"{d}/profile.json", {**profile, "profile_status": "rejected", "profile_version": None, "validation": {"levels": pv["levels"], "ok": False}}); jwrite(f"{d}/profile_validation.json", pv)
        audit(d, "profile_rejected", hotel_id=hid, validation=pv["levels"], codes=[x["code"] for x in pv["critical"]])
        for w in pv["warnings"]: log(f"  ⚠ [L{w['level']}] {w['message']}")
        fail("hotel profile is not valid: " + "; ".join(x["message"] for x in pv["critical"]), code="PROFILE_INVALID", category=pv["critical"][0]["category"],
             missing_fields=[f for x in pv["critical"] for f in x["fields"]], details={"critical": pv["critical"]})
    before = store.index(hid)["latest_version"]
    stored = store.save(profile)
    new = stored["profile_version"] != before
    jwrite(f"{d}/profile.json", {**stored, "validation": {"levels": pv["levels"], "ok": pv["ok"]}}); jwrite(f"{d}/profile_validation.json", pv)
    audit(d, "profile_saved", hotel_id=hid, profile_version=stored["profile_version"], new_version=new, profile_status=stored["profile_status"], validation=pv["levels"], customization_version=ver)
    prov = {}
    for e in stored["evidence"]: prov[e["provenance"]] = prov.get(e["provenance"], 0) + 1
    log(f"✔ profile: {hid} v{stored['profile_version']} ({'new version' if new else 'unchanged'}, {stored['profile_status']}) | validation L1 {pv['levels']['1']} L2 {pv['levels']['2']} L3 {pv['levels']['3']} | evidence {prov} | customization {ver}")
    for w in pv["warnings"]: log(f"  ⚠ [L{w['level']}] {w['message']}")


# ---------------------------------------------------------------------------------------------- plan (template family + sections)
def stage_plan(d):
    custom_doc, profile = need(d, "customization.json"), need(d, "profile.json")
    h, _ = merged_config(d, prune=False); cp = jread(f"{d}/copy.json", {}) or {}; ov = jread(f"{d}/overrides.json", {}) or {}
    cp = {**cp, "left_out": [f for f in cp.get("left_out", []) if f.split(".")[0] not in ov]}      # text a person supplied is no longer missing
    plan = CU.make_plan(profile, h, cp, custom_doc["customization"], custom_doc["version"])
    jwrite(f"{d}/plan.json", plan)
    audit(d, "plan_made", template_family=plan["template_family"], sections=[s["key"] for s in plan["sections"]], omitted=[o["key"] for o in plan["omitted"]], customization_version=plan["customization_version"])
    log(f"✔ plan: family '{plan['template_family']}' ({plan['family_source']}) | {len(plan['sections'])} sections | omitted {[o['key'] for o in plan['omitted']] or 'none'} | unmet requests {len(plan['unmet_requests'])}")
    for x in plan["decisions"]: log("  · " + x)
    for x in plan["unmet_requests"]: log(f"  ⚠ cannot deliver {x['request']}: {x['reason']}")
    if plan["blocking"]:
        fail("the section plan is blocked: " + "; ".join(b["message"] for b in plan["blocking"]), code="PLAN_BLOCKED", category=plan["blocking"][0]["category"],
             missing_fields=[f for b in plan["blocking"] for f in b.get("fields", [])], details={"blocking": plan["blocking"]})


# ---------------------------------------------------------------------------------------------- assets (fetch once, validate, hash)
def stage_assets(d):
    inp, sc, pol = jread(f"{d}/input.json"), jread(f"{d}/scrape.json"), AS.policy()
    h, _ = merged_config(d, prune=False); media = h.get("media") or {}
    photo_urls = list(dict.fromkeys(u for name, keys in (h.get("slots") or {}).items() if not name.startswith("_") for k in keys
                                    for u in [(media.get(k) or {}).get("source_url")] if u and not (media.get(k) or {}).get("hippo")))
    logo_urls = [l["url"] for l in h.get("logos", []) if str(l.get("url", "")).startswith(("http://", "https://")) and not l.get("hippo")]
    if pol == "off":
        jwrite(f"{d}/assets.json", {"policy": "off", "checked_at": now_iso(), "records": {}, "notes": [], "photos": len(photo_urls)})
        log(f"✔ assets: skipped (ASSET_CHECK_POLICY=off): {len(photo_urls)} photo(s) not verified"); return
    hid = inp.get("hotel_id") or HP.hotel_identity(inp["hotel_url"])["hotel_id"]
    cpath = os.path.join(hotels_dir(d), hid, "assets_cache.json"); os.makedirs(os.path.dirname(cpath), exist_ok=True)
    cache = AS.load_cache(cpath)
    recs = AS.check_assets(photo_urls + logo_urls, referer=(sc or {}).get("url"), cache=cache, ttl_hours=float(os.environ.get("ASSET_CACHE_HOURS", "24") or 24))
    AS.save_cache(cpath, cache, recs)
    AS.mark_small(recs, photo_urls)
    photos = [recs[u] for u in photo_urls]; ok = [r for r in photos if r["ok"]]
    changes = []
    h2, notes = AS.prune_media(h, recs, pol, changes)
    jwrite(f"{d}/assets.json", {"policy": pol, "checked_at": now_iso(), "records": recs, "notes": notes, "changes": changes, "photos": len(photo_urls), "usable": len(h2.get("media", {}))})
    audit(d, "assets_checked", policy=pol, photos=len(photo_urls), verified=len(ok), cached=sum(1 for r in recs.values() if r.get("cached")), notes=len(notes))
    log(f"✔ assets: {len(ok)}/{len(photos)} photos verified ({sum(1 for r in recs.values() if r.get('cached'))} from cache), {len(logo_urls)} logo URL(s) checked | policy {pol}")
    for n in notes: log("  ⚠ " + n)
    for r in recs.values():
        if not r["ok"]: log(f"  ⚠ {r['url'][:100]}: {r['error']}" + (" (kept: policy is warn)" if pol == "warn" else ""))
    if pol == "enforce" and photos and not ok:
        bad = [r for r in photos if not r["ok"]]; transient = all(r.get("transient") for r in bad)
        hint = "" if transient else " If the images load in a browser, the host may refuse servers: set ASSET_CHECK_POLICY=warn."
        fail(f"none of the {len(photos)} hotel photos could be verified (first error: {bad[0]['error']}).{hint}", code="ASSETS_UNAVAILABLE", category="ASSET_MISSING", retryable=transient,
             retry_after=None, details={"errors": [{"url": r["url"], "error": r["error"]} for r in bad[:10]]})


# ---------------------------------------------------------------------------------------------- build
def stage_build(d):
    inp = jread(f"{d}/input.json"); plan = need(d, "plan.json"); h, cfg = merged_config(d)
    sections = [s["key"] for s in plan["sections"]]
    missing = [k for k in B.required_for(sections) if not h.get(k)]
    if missing: fail(f"config still missing {missing}: add them via POST /deal-rooms/:id/overrides", code="CONFIG_MISSING_FIELDS", category="REQUIRED_INFO_MISSING", missing_fields=missing)
    for slot, keys in h["slots"].items():
        bad = [k for k in keys if k not in h["media"]]
        if bad: fail(f"slot '{slot}' references unknown media {bad}", code="UNKNOWN_MEDIA", category="ASSET_MISSING", missing_fields=bad)
    tpl, checks, P, binds = B.build_full(h, plan)
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
    pr = jread(f"{d}/profile.json", {})
    report = {"hotel": h["name"], "flow": inp["flow"], "sections": [s["name"] for s in tpl["sections"]], "merge_tokens": len(toks), "unique_tokens": len(set(toks)),
              "contrast_pairs": len(checks) + 1, "palette": P, "errors": errs, "warnings": warns + cfg.get("_warnings", []),
              "template_family": plan["template_family"], "profile_version": pr.get("profile_version"), "customization_version": plan["customization_version"]}
    jwrite(f"{d}/report.json", report)
    if errs: fail(f"validation failed with {len(errs)} error(s): {errs[:5]}", code="TEMPLATE_VALIDATION_FAILED", category="RENDER_FAILURE", details={"errors": errs[:20]})
    jwrite(f"{d}/template.json", tpl); jwrite(f"{d}/bindings.json", binds)
    open(f"{d}/loader.js", "w").write(B.make_loader(tpl, inp.get("hippo_page_id") or "auto", html.unescape(h["name"]), "Cvent" if inp["flow"] == "cvent" else "Email"))
    log(f"✔ build ({inp['flow']}, {plan['template_family']}): {len(tpl['sections'])} sections, {len(toks)} merge fields ({len(set(toks))} unique), {len(checks) + 1} colour pairs checked, 0 errors, {len(report['warnings'])} warnings")


# ---------------------------------------------------------------------------------------------- map (merge-field mapping report)
def stage_map(d):
    inp, plan, profile = jread(f"{d}/input.json"), need(d, "plan.json"), need(d, "profile.json")
    tpl, binds, h = need(d, "template.json"), jread(f"{d}/bindings.json", []), merged_config(d)[0]
    doc = jread(f"{d}/assets.json") or {}
    m = MP.build_mapping(h, plan, profile, tpl, inp["flow"], binds, doc.get("records") if doc.get("policy") == "enforce" else None)
    jwrite(f"{d}/mapping.json", m); s = m["summary"]
    log(f"✔ map: {s['static_bindings']} hotel bindings ({s['static_omitted']} omitted: no value) | {s['dynamic_fields']} Hippo merge fields {s['dynamic_by_category']} | {s['errors']} error(s), {s['warnings']} warning(s)")
    for w in m["warnings"]: log("  ⚠ " + w["message"])
    if m["errors"]:
        fail("merge-field mapping failed: " + "; ".join(e["message"] for e in m["errors"][:5]), code="MAPPING_FAILED", category=m["errors"][0]["category"],
             missing_fields=[f for e in m["errors"] for f in e["fields"]], details={"errors": m["errors"][:20]})


# ---------------------------------------------------------------------------------------------- qa (PASS / REVIEW / FAIL)
def stage_qa(d):
    inp, plan, profile, pv = jread(f"{d}/input.json"), need(d, "plan.json"), need(d, "profile.json"), jread(f"{d}/profile_validation.json", {})
    tpl, m, h = need(d, "template.json"), need(d, "mapping.json"), merged_config(d)[0]
    doc = jread(f"{d}/assets.json") or {}
    others = HP.Store(hotels_dir(d)).others(profile["hotel_id"])
    custom = (jread(f"{d}/customization.json") or {}).get("customization", {})
    res = QA.run(tpl=tpl, h=h, plan=plan, profile=profile, pvalid=pv, mapping=m, assets=doc.get("records", {}), others=others, flow=inp["flow"], rights=QA.rights_policy(), check=doc.get("policy", AS.policy()),
                 rights_approved=bool((custom.get("asset_rights") or {}).get("approved")), asset_changes=doc.get("changes", []))
    jwrite(f"{d}/qa.json", res)
    audit(d, "qa_outcome", outcome=res["outcome"], counts=res["counts"], critical=[x["code"] for x in res["critical"]], review=[x["code"] for x in res["review"]])
    log(f"✔ qa: {res['outcome']} | {res['counts']['pass']} passed, {res['counts']['review']} need review, {res['counts']['fail']} failed" if res["outcome"] != "FAIL" else f"✖ qa: FAIL | {res['counts']}")
    for x in res["critical"]: log(f"  ✖ {x['message']}")
    for x in res["review"]: log(f"  ? {x['message']}")
    for x in res["not_run"]: log(f"  - not run: {x['check']} ({x['reason']})")
    if res["outcome"] == "FAIL":
        c = res["critical"][0]
        fail("QA failed: " + "; ".join(x["message"] for x in res["critical"][:5]), code="QA_FAILED", category=c["category"],
             missing_fields=[f for x in res["critical"] for f in x.get("fields", [])], details={"findings": res["critical"][:20]})


# ---------------------------------------------------------------------------------------------- review
HIGH = [(r"no logo", "ASSET_MISSING"), (r"address not found", "REQUIRED_INFO_MISSING"), (r"only a favicon", "ASSET_MISSING"), (r"No usable", "ASSET_MISSING"),
        (r"run on a non-overview", "SOURCE_UNVERIFIED")]

def stage_review(d):
    inp, rep, tpl = jread(f"{d}/input.json"), jread(f"{d}/report.json"), jread(f"{d}/template.json")
    h, cfg = merged_config(d); facts = jread(f"{d}/facts.json", []); cp = jread(f"{d}/copy.json", {})
    plan, qa, profile, m = need(d, "plan.json"), need(d, "qa.json"), need(d, "profile.json"), need(d, "mapping.json")
    pv = jread(f"{d}/profile_validation.json", {})
    items = []
    def item(code, cat, msg, fields=()): items.append({"code": code, "category": cat, "severity": "high", "message": msg, "fields": list(fields)})
    for w in rep["warnings"]:
        for rx, cat in HIGH:
            if re.search(rx, w, re.I): item("REPORT_WARNING", cat, w); break
    if len(h["media"]) < 4: item("FEW_PHOTOS", "ASSET_MISSING", f"only {len(h['media'])} photos found")
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
    if ungrounded_left: item("UNGROUNDED_COPY", "UNVERIFIED_CONTENT", f"copy not fully grounded: {ungrounded_left}", list(ungrounded_left))
    unsourced = [f["field"] for f in facts if not f["verified"]]
    if unsourced: item("FACTS_WITHOUT_SOURCE", "UNVERIFIED_CONTENT", f"facts without a source: {sorted(set(unsourced))}", sorted(set(unsourced)))
    for x in qa["review"]: item(x["code"], x["category"], x["message"], x.get("fields", []))
    high = [i["message"] for i in items]
    needs = inp["mode"] == "review" or bool(high)
    jwrite(f"{d}/review.json", {"needs_review": needs, "high_severity": high, "items": items, "warnings": rep["warnings"], "qa_outcome": qa["outcome"]})
    # The review document is sandboxed and some hotel CDNs refuse or throttle a
    # large number of direct, cross-origin image requests.  Route preview images
    # through the app; the original source URL remains in config.json and in the
    # generated deal room.
    open(f"{d}/review.html", "w").write(review_html(os.path.basename(d), inp, rep, tpl, h, facts, cp, high, {"plan": plan, "qa": qa, "profile": profile, "mapping": m, "pv": pv}))
    log(f"✔ review: {'waiting for approval' if needs else 'auto-approved'} | QA {qa['outcome']}" + (f" | needs a person: {high}" if high else ""))

def review_html(job_id, inp, rep, tpl, h, facts, cp, high, extra):
    e = html.escape; P = rep["palette"]
    plan, qa, profile, mp, pv = extra["plan"], extra["qa"], extra["profile"], extra["mapping"], extra["pv"]
    sw = "".join(f'<div class="sw"><span style="background:{e(v)}"></span>{e(k)}<br><code>{e(v)}</code></div>' for k, v in P.items() if isinstance(v, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", v))
    # logos load through the app's checked image route, not straight from the hotel's server into the reviewer's browser
    logos = "".join(f'<img src="{e(l["url"]) if str(l.get("url", "")).startswith("data:image/") else "/deal-rooms/" + e(job_id) + "/media/logo_" + str(i)}" alt="logo" style="max-height:60px;background:#fff;padding:6px;border:1px solid #ddd">'
                    for i, l in enumerate(h.get("logos", []), 1) if l.get("url"))
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
    qrows = "".join(f'<tr class="q-{e(c["status"])}"><td>{e(c["status"].upper())}</td><td>{e(c["name"])}</td><td>{e(c["detail"])}</td></tr>' for c in qa["checks"])
    qnot = "".join(f"<li>{e(n['check'])}: {e(n['reason'])}</li>" for n in qa["not_run"])
    prov = {}
    for ev in profile.get("evidence", []): prov[ev["provenance"]] = prov.get(ev["provenance"], 0) + 1
    pl_omit = "".join(f"<li><b>{e(o['title'])}</b>: {e(o['reason'])}</li>" for o in plan["omitted"])
    pl_unmet = "".join(f"<li>{e(u['request'])}: {e(u['reason'])}</li>" for u in plan["unmet_requests"])
    pl_dec = "".join(f"<li>{e(x)}</li>" for x in plan["decisions"])
    dyn = "".join(f"<tr><td>{e(k)}</td><td>{v}</td></tr>" for k, v in mp["summary"]["dynamic_by_category"].items())
    stat = "".join(f'<tr class="m-{e(s["validation"])}"><td>{e(s["section"])} / {e(s["slot"])}</td><td>{e(s["provenance"])}</td><td>{s["confidence"]}</td><td>{e(s["validation"])} {e(s["validation_note"])}</td></tr>' for s in mp["static"])
    # The page carries its own policy, so no script can run in it however it is opened (the UI opens it from a blob, which would not inherit the sandbox header).
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src * data:; style-src 'unsafe-inline'"><title>Review: {e(h['name'])}</title>
<style>body{{font:14px/1.5 system-ui,sans-serif;margin:0;background:#f6f6f4;color:#222}}main{{max-width:1100px;margin:auto;padding:24px}}
section{{background:#fff;border-radius:12px;padding:20px;margin:16px 0;box-shadow:0 1px 3px #0001}}h1{{margin:0}}h2{{margin-top:0}}
.sw{{display:inline-block;width:120px;font-size:12px;margin:4px}}.sw span{{display:block;height:36px;border-radius:6px;border:1px solid #ccc}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}}figure{{margin:0}}figure img{{width:100%;height:140px;object-fit:cover;border-radius:8px;background:#ddd}}
table{{width:100%;border-collapse:collapse}}td,th{{border-top:1px solid #eee;padding:6px;vertical-align:top;text-align:left}}.bad{{color:#a00}}code{{font-size:11px}}
.q-fail td:first-child,.m-error td:last-child{{color:#a00;font-weight:600}}.q-review td:first-child,.m-warn td:last-child{{color:#9a6700;font-weight:600}}.q-pass td:first-child{{color:#1f7a3f}}
.badge{{display:inline-block;padding:2px 10px;border-radius:99px;font-weight:600;border:1px solid currentColor}}.PASS{{color:#1f7a3f}}.REVIEW{{color:#9a6700}}.FAIL{{color:#a00}}</style></head><body><main>
<h1>{e(h['name'])}</h1><p>{e(inp['hotel_url'])} · flow <b>{e(inp['flow'])}</b> · family <b>{e(plan['template_family'])}</b> · {rep['unique_tokens']} merge fields · {rep['contrast_pairs']} colour pairs checked · 0 validation errors</p>
<section><h2>Decision</h2><p>Quality assurance: <span class="badge {e(qa['outcome'])}">{e(qa['outcome'])}</span></p>{('<p class="bad"><b>Needs a person before approval:</b></p><ul class="bad">' + hi + '</ul>') if high else '<p>No high-severity items.</p>'}
<p>Approve: <code>POST /deal-rooms/&lt;id&gt;/approve</code> · Edit: <code>POST /deal-rooms/&lt;id&gt;/overrides</code></p></section>
<section><h2>Quality assurance</h2><table><tr><th>Result</th><th>Check</th><th>Detail</th></tr>{qrows}</table><h3>Not checked by the agent</h3><ul>{qnot}</ul></section>
<section><h2>Section plan</h2><p>Template family <b>{e(plan['template_family'])}</b> ({e(plan['family_source'])}) · customization <code>{e(str(plan['customization_version']))}</code> · hotel profile <b>v{e(str(profile.get('profile_version')))}</b> ({e(str(profile.get('profile_status')))})</p>
<ol>{secs}</ol>{('<h3>Left out</h3><ul>' + pl_omit + '</ul>') if pl_omit else ''}{('<h3>Requests that could not be met</h3><ul class="bad">' + pl_unmet + '</ul>') if pl_unmet else ''}
<h3>Why</h3><ul>{pl_dec}</ul>{('<p><b>Notes from the requester (not interpreted):</b> ' + e(plan['special_notes']) + '</p>') if plan['special_notes'] else ''}</section>
<section><h2>Hotel profile</h2><p>Validation: level 1 <b>{e(pv.get('levels', {}).get('1', ''))}</b> · level 2 <b>{e(pv.get('levels', {}).get('2', ''))}</b> · level 3 <b>{e(pv.get('levels', {}).get('3', ''))}</b> · evidence by provenance: {e(json.dumps(prov))}</p>
<p class="bad">Inferred values are generated wording, never verified facts.</p></section>
<section><h2>Brand</h2>{sw}<p>Heading font: <b>{e(str((h.get('fonts') or {}).get('heading', {}).get('site_font')))}</b> → <code>{e(str((h.get('fonts') or {}).get('heading', {}).get('stack')))}</code><br>
Body font: <b>{e(str((h.get('fonts') or {}).get('body', {}).get('site_font')))}</b> → <code>{e(str((h.get('fonts') or {}).get('body', {}).get('stack')))}</code></p>{logos}</section>
<section><h2>Photos and where they go</h2><div class="grid">{photos}</div></section>
<section><h2>Facts and sources</h2><table><tr><th>Fact</th><th>Value</th><th>Source</th></tr>{frows}</table></section>
<section><h2>Generated copy</h2><table><tr><th>Field</th><th>Text</th><th>From</th></tr>{crows}</table></section>
<section><h2>Merge fields</h2><p>{mp['summary']['dynamic_fields']} Hippo merge fields stay dynamic and are filled by Hippo for each opportunity (never by this agent):</p><table><tr><th>Kind</th><th>Fields</th></tr>{dyn}</table>
<h3>Hotel values written into the template</h3><table><tr><th>Where</th><th>Provenance</th><th>Confidence</th><th>Check</th></tr>{stat}</table></section>
<section><h2>Sections built</h2><ol>{secs}</ol><h3>Warnings</h3><ul>{warns or '<li>none</li>'}</ul></section>
</main></body></html>"""


# ---------------------------------------------------------------------------------------------- publish gate
def record_generation(d):
    """Note which job produced a deal room from which profile version: only once the page was saved and read back (or handed over as a loader)."""
    pub, plan, inp, job = jread(f"{d}/publication.json"), jread(f"{d}/plan.json"), jread(f"{d}/input.json"), jread(f"{d}/job.json", {})
    if not (pub and plan): return
    HP.Store(hotels_dir(d)).record_generation(pub["hotel_id"], {"job_id": job.get("id"), "at": now_iso(), "profile_version": pub["profile_version"], "customization_version": plan["customization_version"],
                                                                 "template_family": plan["template_family"], "flow": inp["flow"], "hippo_page_id": inp.get("hippo_page_id") or None,
                                                                 "opportunity_id": inp.get("opportunity_id") or None, "approved_by": pub["approved_by"], "qa_outcome": pub["qa_outcome"]})


def stage_publish_gate(d):
    """The last check before anything is saved to Hippo, and the moment a hotel profile version becomes approved."""
    job = jread(f"{d}/job.json", {}); plan, qa, profile, pv = need(d, "plan.json"), need(d, "qa.json"), need(d, "profile.json"), jread(f"{d}/profile_validation.json", {})
    tpl, inp = jread(f"{d}/template.json"), jread(f"{d}/input.json")
    problems = []
    if not qa or qa.get("outcome") == "FAIL": problems.append("QA has not passed")
    if pv.get("critical"): problems.append("the hotel profile has critical validation errors")
    if [s["id"] for s in (tpl or {}).get("sections", [])] != [B.SECTION_ID_BY_KEY[s["key"]] for s in plan["sections"]]: problems.append("the template does not match the section plan")
    if not (tpl or {}).get("sections"): problems.append("there is no template")
    if problems: fail("publication blocked: " + "; ".join(problems), code="PUBLICATION_BLOCKED", category="RENDER_FAILURE", details={"problems": problems})
    approver = next((ev.get("by") or "auto" for ev in reversed(job.get("history", [])) if str(ev.get("event", "")).startswith(("approved", "auto-approved"))), "unknown")
    store = HP.Store(hotels_dir(d)); hid, ver = profile["hotel_id"], profile["profile_version"]
    store.approve(hid, ver, approver)
    rights = "approved" if all(a.get("usage_rights_status") == "approved" for a in profile.get("assets", [])) else f"unverified: accepted for this deal room by {approver} at approval"
    jwrite(f"{d}/publication.json", {"hotel_id": hid, "profile_version": ver, "approved_by": approver, "approved_at": now_iso(), "qa_outcome": qa["outcome"], "template_family": plan["template_family"], "asset_rights": rights})
    audit(d, "publication_gate_passed", hotel_id=hid, profile_version=ver, approved_by=approver)
    log(f"✔ publish_gate: QA {qa['outcome']}, profile {hid} v{ver} approved by {approver}")


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
        else: fail("load=api needs HIPPO_API_TOKEN (or HIPPO_COOKIE + HIPPO_CSRF) in the environment", code="HIPPO_NOT_CONFIGURED")
        if not self.base.startswith("https://") and os.environ.get("ALLOW_HTTP") != "1": fail("HIPPO_BASE_URL must be https:// (the token or cookie would travel in clear)", code="HIPPO_NOT_CONFIGURED")
    def _req(self, method, path, data=None, ctype=None):
        hd = dict(self.h)
        if ctype: hd["Content-Type"] = ctype
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=hd)
        try:
            with netsafe._opener.open(req, timeout=60) as r: return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:300]; ra = e.headers.get("Retry-After", "")
            if 300 <= e.code < 400: fail(f"Hippo redirected the request (HTTP {e.code}): the session or token is probably not valid; the credentials were not sent anywhere else", code="HIPPO_AUTH")
            if e.code in (429, 500, 502, 503, 504): fail(f"Hippo returned HTTP {e.code}: {body}", code="HIPPO_TRANSIENT", retryable=True, retry_after=int(ra) if ra.isdigit() else None)
            if e.code in (401, 403): fail(f"Hippo rejected the credentials (HTTP {e.code}): the token or session cookie is missing, expired or not allowed to edit this page", code="HIPPO_AUTH")
            if e.code == 404: fail("Hippo has no such deal room page (HTTP 404): check hippo_page_id", code="HIPPO_PAGE_NOT_FOUND")
            fail(f"Hippo returned HTTP {e.code}: {body}", code="HIPPO_REJECTED")
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
            fail(f"could not reach Hippo: {getattr(e, 'reason', e)}", code="HIPPO_UNREACHABLE", retryable=True)
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
    if os.path.exists(f"{d}/rollback.json"): os.remove(f"{d}/rollback.json")
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
    audit(d, "loaded_to_hippo", page_id=pid, ok=ok, http=st)
    if not ok: fail(f"Hippo save failed (HTTP {st}): {body}", code="HIPPO_SAVE_REJECTED")
    log(f"✔ load: saved to page {pid}")


# ---------------------------------------------------------------------------------------------- verify + rollback
def tokens(obj): return sorted(set(re.findall(r"\$\{[^}]*\}", html.unescape(json.dumps(obj, ensure_ascii=False)))))

def stage_verify(d):
    inp = jread(f"{d}/input.json"); tpl = jread(f"{d}/template.json")
    if inp["load"] != "api":
        jwrite(f"{d}/verify.json", {"status": "skipped", "reason": "manual load: verify after pasting the loader by re-running with load=api, or check the page"}); record_generation(d); log("✔ verify: skipped (manual load)"); return
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
    audit(d, "verified", status=res["status"], critical=len(crit))
    if crit and os.environ.get("AUTO_ROLLBACK", "1") == "1":
        log("✖ verify failed: " + "; ".join(crit)); stage_rollback(d); fail("verify failed, page rolled back to the backup", code="VERIFY_FAILED", details={"critical": crit})
    if crit: fail("verify failed: " + "; ".join(crit), code="VERIFY_FAILED", details={"critical": crit})
    record_generation(d)
    log(f"✔ verify: page matches ({len(tw)} merge fields, {len(want_s)} sections)" + (f" | notes: {notes}" if notes else ""))

def stage_rollback(d):
    inp = jread(f"{d}/input.json"); bk = jread(f"{d}/backup.json")
    if not bk: fail("no backup.json in this job: nothing to roll back", code="NO_BACKUP")
    ok, st, body = Hippo().save(inp["hippo_page_id"], bk["template"])
    jwrite(f"{d}/rollback.json", {"ok": ok, "http": st, "response": body, "at": time.time()})
    audit(d, "rolled_back", page_id=inp["hippo_page_id"], ok=ok)
    if not ok: fail(f"rollback failed (HTTP {st}): {body}", code="ROLLBACK_FAILED")
    log(f"✔ rollback: page {inp['hippo_page_id']} restored from backup")


STAGES = {"intake": stage_intake, "capture": stage_capture, "convert": stage_convert, "facts": stage_facts, "copy": stage_copy, "profile": stage_profile, "plan": stage_plan,
          "assets": stage_assets, "build": stage_build, "map": stage_map, "qa": stage_qa, "review": stage_review, "publish_gate": stage_publish_gate,
          "load": stage_load, "verify": stage_verify, "rollback": stage_rollback}

if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in STAGES: print("usage: stages.py <" + "|".join(STAGES) + "> <job_dir>"); sys.exit(64)
    d = os.path.abspath(sys.argv[2])
    if not os.path.isfile(f"{d}/job.json"): print("✖ no job.json in", d); sys.exit(64)
    _JOB_DIR, _STAGE = d, sys.argv[1]
    try: os.remove(f"{d}/stage_error.json")
    except FileNotFoundError: pass
    try: STAGES[sys.argv[1]](d)
    except SystemExit: raise
    except Exception as e:
        import traceback; traceback.print_exc()
        fail(f"unexpected error: {type(e).__name__}: {e}", code="UNEXPECTED_ERROR", category="INTERNAL_ERROR")
