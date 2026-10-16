"""Turn a hotel-scrape-*.json (from scrape_hotel.js) into a hotel config for dealroom_builder.py.   (v2)

Usage: python scrape_to_config.py hotel-scrape-xyz.json [-o hotels/<slug>.json]

Visuals (colours, fonts, logos, photos) and hard facts (name, address, phone, rooms, check-in, venues with
capacities, room types with sizes) are taken from the scrape. Descriptive copy is drafted ONLY from the hotel's
own sentences and listed in "_review" for a human check. Nothing is invented: missing facts are left out and
reported in "_warnings".
"""
import json, re, sys, argparse, colorsys, html, urllib.parse

# ---------------- colour helpers ----------------
def hsl(hexv):
    h = hexv.lstrip('#'); r, g, b = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    hh, l, s = colorsys.rgb_to_hls(r, g, b); return hh, s, l

def neutral(hexv):
    _, s, l = hsl(hexv); return s < 0.12 or l < 0.06 or l > 0.97

def norm_hex(v):
    if not isinstance(v, str): return None
    v = v.strip()
    m = re.fullmatch(r"#([0-9a-fA-F]{3})", v)
    if m: v = "#" + "".join(c * 2 for c in m.group(1))
    if v.startswith("rgb"):
        n = re.findall(r"[\d.]+", v)[:3]
        if len(n) == 3: v = "#%02X%02X%02X" % tuple(int(float(x)) for x in n)
    return v.upper() if re.fullmatch(r"#[0-9A-Fa-f]{6}", v) else None

def mix(a, b, t):
    a, b = a.lstrip('#'), b.lstrip('#')
    return "#%02X%02X%02X" % tuple(round(int(a[i:i + 2], 16) * (1 - t) + int(b[i:i + 2], 16) * t) for i in (0, 2, 4))

IGNORE_VAR = re.compile(r"chat|widget|cookie|consent|onetrust|intercom|drift|zendesk|hubspot|yellow|bot|popup|toast|modal", re.I)

def pick_palette(c):
    notes = []
    roles = c.get("by_role", {})
    def role(name, kind):
        return [norm_hex(k.split(":", 1)[1]) for k, _ in roles.get(name, []) if k.startswith(kind + ":") and norm_hex(k.split(":", 1)[1])]
    vars_ = {k: norm_hex(v) for k, v in c.get("css_variables", {}).items() if norm_hex(v) and not IGNORE_VAR.search(k)}
    SKIP = ("accent", "secondary", "sand", "light", "dark", "text", "background", "bg", "neutral", "gray", "grey", "white", "black", "border")
    def named(*words, skip=SKIP):
        return [v for k, v in vars_.items() if any(w in k.lower() for w in words) and not any(x in k.lower() for x in skip) and not neutral(v)]
    bgs = [(norm_hex(k), w) for k, w in c.get("background_by_area", []) if norm_hex(k)]
    texts = [(norm_hex(k), w) for k, w in c.get("text_by_length", []) if norm_hex(k)]
    chroma = lambda lst: [x for x in lst if x and not neutral(x)]
    src = ""
    for label, cand in [("CSS brand variable", named("primary") or named("brand", "main")), ("header background", chroma(role("header", "bg"))),
                        ("button background", chroma(role("buttons", "bg"))), ("heading colour", chroma(role("headings", "fg"))),
                        ("link colour", chroma(role("links", "fg"))), ("text colour", chroma([t for t, _ in texts])), ("background", chroma([b for b, _ in bgs]))]:
        if cand: primary, src = cand[0], label; break
    else:
        primary, src = "#1F3A5F", "default (no brand colour found)"
    notes.append(f"primary {primary} from {src}")
    acc_c = [x for x in (named("accent", "secondary", "highlight", "gold", skip=()) + chroma(role("buttons", "bg")) + chroma(role("links", "fg")) +
                         chroma([b for b, _ in bgs])) if x != primary]
    accent = acc_c[0] if acc_c else mix(primary, "#FFFFFF", 0.35)
    if not acc_c: notes.append("no second brand colour on the site: accent is a lighter shade of primary")
    darks = [(b, w) for b, w in bgs if b and hsl(b)[2] < 0.25] + [(x, 0) for x in role("footer", "bg") + role("header", "bg") if x and hsl(x)[2] < 0.25]
    dark = max(darks, key=lambda bw: (bw[1], bw[0] != "#000000"))[0] if darks else mix(primary, "#000000", 0.55)
    tints = [b for b, _ in bgs if b and 0.88 < hsl(b)[2] < 0.985]
    tint = tints[0] if tints else mix(primary, "#FFFFFF", 0.92)
    body_text = next((t for t, _ in texts if t and hsl(t)[2] < 0.45), "#1A1A1A")
    notes.append(f"dark {dark}, tint {tint}, text {body_text}")
    return {"primary": primary, "dark": dark, "tint": tint, "accent": accent, "text": body_text}, notes

# ---------------- fonts ----------------
GOOGLE = {"open sans", "roboto", "lato", "montserrat", "poppins", "inter", "raleway", "playfair display", "lora", "merriweather",
          "cormorant garamond", "cormorant", "eb garamond", "libre baskerville", "nunito", "source sans pro", "source serif pro", "oswald",
          "work sans", "dm sans", "dm serif display", "josefin sans", "crimson text", "pt serif", "pt sans", "noto serif", "noto sans",
          "cinzel", "marcellus", "jost", "manrope", "barlow", "mulish", "karla", "libre franklin", "prata", "bodoni moda", "italiana",
          "tenor sans", "forum", "gilda display", "nanum myeongjo", "spectral", "fraunces", "outfit", "plus jakarta sans", "figtree"}
SERIF_RX = r"serif|garamond|didot|bodoni|baskerville|caslon|times|georgia|playfair|canela|tiempos|cinzel|palatino|trajan|cormorant|prata|marcellus|gilda|forum|italiana|spectral|fraunces|myeongjo"

def parse_font_entry(e):
    fam, weight, size, transform, spacing = (e[0].split(" | ") + [""] * 5)[:5]
    parts = [p.strip().strip('"\'') for p in fam.split(",") if p.strip()]
    return {"family": parts[0] if parts else "", "fallbacks": parts[1:], "weight": weight, "size": size, "transform": transform,
            "letter_spacing": spacing, "weight_used": e[1]}

def dominant(entries_lists):
    agg = {}
    for entries in entries_lists:
        for e in entries or []:
            f = parse_font_entry(e)
            a = agg.setdefault(f["family"], {"w": 0, "best": f})
            a["w"] += f["weight_used"]
            if f["weight_used"] > a["best"]["weight_used"]: a["best"] = f
    if not agg: return None
    return max(agg.values(), key=lambda a: a["w"])["best"]

def font_stack(f):
    if not f: return {"site_font": None, "web_font": "Open Sans", "stack": "'Open Sans', Arial, Helvetica, sans-serif", "is_serif": False,
                      "button_font": "Arial", "transform": "none", "letter_spacing": "normal"}
    name = f["family"]
    serif = bool(re.search(SERIF_RX, (name + " " + " ".join(f["fallbacks"][:1])).lower())) and "sans" not in name.lower()
    web = name if name.lower() in GOOGLE else ("Playfair Display" if serif else "Open Sans")
    fb = [x for x in f["fallbacks"] if x.lower() not in ("serif", "sans-serif", name.lower())][:2]
    safe = ["Georgia", "serif"] if serif else ["Arial", "Helvetica", "sans-serif"]
    stack = ", ".join(f"'{x}'" if " " in x else x for x in [web] + ([name] if web != name else []) + fb + safe)
    ls = f["letter_spacing"]
    try:
        if ls.endswith("px") and f["size"].endswith("px"): ls = f"{float(ls[:-2]) / float(f['size'][:-2]):.2f}em"
    except ValueError: pass
    return {"site_font": name, "web_font": web, "exact_match": web == name, "stack": stack, "is_serif": serif,
            "button_font": "Times New Roman" if serif else "Arial", "transform": f["transform"] or "none", "letter_spacing": ls or "normal"}

# ---------------- images ----------------
CATS = [("meeting", r"meeting|event|ballroom|conference|boardroom|banquet|wedding|function|pre-?function|venue|cultural setup|lawns?\b|durbar"),
        ("dining", r"restaurant|dining|\bbar\b|lounge|kitchen|breakfast|cafe|food|cuisine|bistro|grill|alfresco"),
        ("rooms", r"\broom|suite|\bbed|king|queen|twin|accommodation|penthouse|apartment|residence|seating area"),
        ("amenities", r"pool|spa|gym|fitness|wellness|beach|sauna|pilates|kids|garden"),
        ("exterior", r"exterior|facade|fa[cç]ade|hero|aerial|building|lobby|entrance|view|skyline|palace|royal|structure|marina|og:image")]
EXCLUDE = r"logo|badge|icon|award|tripadvisor|sprite|avatar|flag|payment|app-?store|google-?play|placeholder|blank|pixel|spinner|loader|\bmap\b|qr|bot|arrow|search|calendar|share-image|-time-img|-temp-img|\b(hotel|dining|wellness|rooms)-image\b|bat\.bing|facebook\.com/tr|doubleclick|adnxs\.com|(?:^|[/?])px(?:[/?]|$)|/seg(?:[/?]|$)"

def real_url(u):
    """Undo image proxies like Next.js /_next/image?url=... so each photo keeps its own identity."""
    try:
        p = urllib.parse.urlparse(u)
        if p.path.endswith("/_next/image"):
            inner = urllib.parse.parse_qs(p.query).get("url", [""])[0]
            if inner: return urllib.parse.urljoin(u, inner)
    except Exception: pass
    return u

def upsize(u):
    """Request a large rendition from image CDNs that support it."""
    p = urllib.parse.urlparse(u)
    if "cdn.sanity.io" in p.netloc: return urllib.parse.urlunparse(p._replace(query="w=1920&auto=format"))
    if "digital.ihg.com" in p.netloc: return urllib.parse.urlunparse(p._replace(query="wid=1920"))
    return u

def dims(img):
    w, h = img.get("w") or 0, img.get("h") or 0
    m = re.search(r"-(\d{2,5})x(\d{2,5})\.(?:jpe?g|png|webp|avif)", img["url"])
    if m: w, h = max(w, int(m.group(1))), max(h, int(m.group(2)))
    return w, h

def classify(img, hotel_words):
    hay = " ".join([img.get("url", ""), img.get("alt", ""), img.get("context", "")]).lower()
    if re.search(EXCLUDE, hay): return None
    w, h = dims(img)
    if w and w < 600: return None
    for cat, rx in CATS:
        if re.search(rx, (img.get("alt", "") + " " + img.get("url", "")).lower()): return cat
    for cat, rx in CATS:
        if re.search(rx, hay): return cat
    return "other"

# ---------------- text helpers ----------------
CSS_JUNK = re.compile(r"(?:\.[\w-]+|@media[^{]*)\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}")
def clean(text):
    return re.sub(r"[ \t​]+", " ", CSS_JUNK.sub("", text or "")).replace("​", "")

def sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", clean(text)) if 25 <= len(s.strip()) <= 260]

def find(text, rx, n=1):
    out, seen = [], set()
    for s in sentences(text):
        if re.search(rx, s, re.I) and s not in seen: out.append(s); seen.add(s)
    return " ".join(out[:n])

def hotel_ld(jsonld):
    flat = []
    for j in jsonld:
        if isinstance(j, list): flat += j
        elif isinstance(j, dict): flat += j.get("@graph", [j])
    best = {}
    for j in flat:
        t = j.get("@type", ""); t = " ".join(t) if isinstance(t, list) else t
        if re.search(r"Hotel|Lodging|Resort|Motel", t):
            for k, v in j.items():
                if v and not best.get(k): best[k] = v
    return best

def addr(a):
    if isinstance(a, str): return a
    if not isinstance(a, dict): return ""
    country = a.get("addressCountry"); country = country if isinstance(country, str) else (country or {}).get("name")
    return ", ".join(x for x in [a.get("streetAddress"), a.get("addressLocality") or a.get("addressRegion"), a.get("postalCode"), country] if x)

def coords(ld):
    g = ld.get("geo")
    g = g[0] if isinstance(g, list) and g else g
    if isinstance(g, dict):
        try:
            la, lo = float(g.get("latitude")), float(g.get("longitude"))
            if -90 <= la <= 90 and -180 <= lo <= 180 and (la, lo) != (0.0, 0.0): return (str(g.get("latitude")).strip(), str(g.get("longitude")).strip())
        except (TypeError, ValueError): pass
    m = re.search(r"(-?\d{1,2}\.\d{3,}),\s*(-?\d{1,3}\.\d{3,})", str(ld.get("hasMap", "")) + " " + json.dumps(ld.get("geo", "")))
    return (m.group(1), m.group(2)) if m else None

def parse_titled_blocks(text):
    """Parse 'Title : X / Description : ... / Specifications : 1: .. / SEATING STYLE 1: Theatre: 130' blocks (Taj/IHCL-style templates)."""
    out = []
    for blk in re.split(r"\bTitle\s*:\s*", clean(text))[1:]:
        lines = [l.strip() for l in blk.split("\n") if l.strip()]
        if not lines: continue
        item = {"title": lines[0].title(), "description": "", "specs": [], "seating": {}}
        mode = None
        for l in lines[1:]:
            if re.match(r"Description\s*:", l): mode = "d"; rest = l.split(":", 1)[1].strip(); item["description"] += rest; continue
            if re.match(r"Specifications\s*:", l): mode = "s"; continue
            if re.match(r"SEATING STYLE", l, re.I): mode = "seat"; continue
            if re.match(r"(Highlights|Services and Amenities)\s*:?", l, re.I): mode = "x"; continue
            if l.upper() == lines[0].upper(): break
            m = re.match(r"\d+\s*:\s*(.+)", l)
            if mode == "d" and not m: item["description"] += " " + l
            elif mode == "s" and m: item["specs"].append(m.group(1).strip())
            elif mode == "seat" and m:
                k, _, v = m.group(1).partition(":")
                if v.strip(): item["seating"][k.strip().title()] = v.strip()
            elif not m and mode in ("s", "seat") and l.isupper(): break
        out.append(item)
    return out

def size_of(specs):
    return next((s for s in specs if re.search(r"sq\.?\s?(m|ft)|sqm|sqft", s, re.I)), "")

def guests_of(specs, seating):
    g = next((re.sub(r"(?i)up to\s*", "", s) for s in specs if re.search(r"guest|people|pax|persons", s, re.I)), "")
    return g.replace(" guests", "").replace(" guest", "").strip()

# words that name a section of a hotel site, not a hotel (keep in sync with SECTION in scrape_hotel.js)
SECTION_WORDS = set("""room rooms suite suites accommodation accommodations dining restaurant restaurants bar bars meeting meetings event events
venue venues wedding weddings gallery photos spa wellness amenities offer offers package packages deal deals special specials promotions
location directions overview contact about faq policies careers news blog press terms privacy reservations booking book rate rates home index""".split())

def slug_is_hotel(path):
    """True when the last path segment is a hotel's own slug, e.g. Hyatt's /unbound-collection/en-US/bnaub-holston-house-nashville."""
    last = path.rstrip("/").rsplit("/", 1)[-1].lower()
    return bool(re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)+", last)) and not re.fullmatch(r"[a-z]{2}-[a-z]{2,4}", last) and not SECTION_WORDS & set(last.split("-"))


def convert(sc):
    review, warnings = [], []
    url = sc.get("url", "")
    # ---- hotel base path: only the hotel's own pages are trusted (chain-wide pages describe other hotels) ----
    path = urllib.parse.urlparse(url).path
    m = re.search(r"(.*/(?:hotels?|resorts?|properties)/[^/]+)", path)
    base = m.group(1) if m else path.rstrip("/") if slug_is_hotel(path) else path.rsplit("/", 1)[0]
    own = [p for p in sc.get("subpages", []) if urllib.parse.urlparse(p["url"]).path.startswith(base)]
    own.sort(key=lambda p: urllib.parse.urlparse(p["url"]).path.rstrip("/") != base.rstrip("/"))   # hotel overview page first
    foreign = [p["url"] for p in sc.get("subpages", []) if p not in own]
    if foreign: warnings.append(f"ignored {len(foreign)} chain-wide page(s) that describe other hotels: " + ", ".join(foreign))
    ld = hotel_ld(sc.get("jsonld", []) + [j for p in own for j in p.get("jsonld", [])])
    name = html.unescape((ld.get("name") or sc.get("meta", {}).get("og_title") or sc.get("title", "").split("|")[0]).strip())
    address = html.unescape(addr(ld.get("address")))
    if not address: warnings.append("address not found in structured data: fill 'address'")
    own_text = clean("\n".join(p.get("text", "") for p in own))
    trusted_main = path.rstrip("/") == base.rstrip("/") or bool(re.search(r"/(rooms|suites|accommodation|dining|restaurants?|meetings?|events?|venues?|gallery|photos|spa|wellness|amenities)", path[len(base):]))
    main_text = clean(sc.get("page_text", "")) if trusted_main else ""
    if not trusted_main: warnings.append("the scraper was run on a non-overview page (" + path[len(base):] + "): its own images and text were skipped; hotel sub-pages were used instead")
    all_text = main_text + "\n" + own_text
    sub = lambda rx: clean("\n".join(p.get("text", "") for p in own if re.search(rx, p.get("link_text", "") + " " + p.get("url", ""), re.I)))

    brand, notes = pick_palette(sc.get("colours", {}))
    F = sc.get("fonts", {})
    fonts = {"heading": font_stack(dominant([F.get("h1"), F.get("h2"), F.get("h3")])), "body": font_stack(dominant([F.get("body")]))}
    brand["font"] = fonts["body"]["web_font"]
    for k in ("heading", "body"):
        f = fonts[k]
        if f["site_font"] and not f.get("exact_match"): warnings.append(f"{k} font '{f['site_font']}' is not a free web font: using '{f['web_font']}' as the closest match")

    # ---- logos: header logo = hotel/brand logo; parent-brand logo (if any) second; portfolio logos ignored ----
    L = sc.get("logos", [])
    header = [l for l in L if l["where"] == "header" and l.get("src")]
    parent = [l for l in L if l.get("src") and re.search(r"group|ihcl|marriott|hilton|ihg|accor|hyatt|parent", (l.get("alt", "") + l["src"]).lower()) and l not in header]
    logos = [{"url": header[0]["src"], "logo_type": "your_logo", "_from": "site header"}] if header else []
    if not logos:
        svg = [l for l in L if l["kind"] == "inline-svg" and l["where"] in ("header", "footer")]
        if svg: logos.append({"url": "data:image/svg+xml;charset=utf-8," + urllib.parse.quote(svg[0]["svg"]), "logo_type": "your_logo", "_from": svg[0]["where"] + " inline SVG"})
    if parent: logos.append({"url": parent[0]["src"], "logo_type": "your_logo", "_from": "parent brand logo"})
    if not logos:
        fav = [l for l in L if l["where"] == "favicon"]
        if fav: logos.append({"url": fav[-1]["src"], "logo_type": "your_logo", "_from": "favicon"}); warnings.append("only a favicon was found for the logo")
        else: warnings.append("no logo found: upload one in Hippo")

    # ---- photos ----
    hotel_words = [w for w in re.findall(r"[a-z]{4,}", name.lower()) if w not in ("hotel", "resort", "palace")]
    cand = []
    extra = [{"url": ld["image"], "alt": name + " exterior", "context": "hotel structured data", "source": "jsonld", "w": 0, "h": 0}] if isinstance(ld.get("image"), str) and ld.get("image") else []
    for img in extra + sc.get("images", []):
        img = dict(img); img["url"] = real_url(img["url"])
        ctxs = img.get("context", "")
        own_urls = {p["url"] for p in own}
        if img.get("page_url"):
            if img["page_url"] not in own_urls: continue                                                      # scraper v1.1: exact page
        elif ctxs.startswith("page: ") and not any(ctxs[6:].strip() == p["link_text"].strip() for p in own): continue   # v1.0: from a chain-wide page
        if re.search(r"\boffers?\b|inclusive rate|\bpackage|\bdeal\b|explore india|gift|promo", (img.get("alt", "") + " " + ctxs).lower()): continue
        if img.get("source") in ("img", "background", "picture", "og") and not main_text:   # main page was not a hotel page (e.g. an offers page)
            continue
        cat = classify(img, hotel_words)
        if cat: cand.append((cat, img))
    media, buckets, seen = {}, {c: [] for c, _ in CATS + [("other", "")]}, set()
    for cat, img in cand:
        b = re.sub(r"[?#].*", "", img["url"])
        if b in seen: continue
        seen.add(b)
        key = f"{cat}_{len(buckets[cat]) + 1}"
        w, h = dims(img)
        media[key] = {"source_url": upsize(img["url"]), "hippo": None, "_alt": img.get("alt", ""), "_size": [w, h], "_from": img.get("context", "")}
        buckets[cat].append(key)
    if not media: sys.exit("No usable hotel photos found. Run the scraper on the hotel's own overview page and let it finish scrolling.")
    pool = [k for c in ["exterior", "rooms", "amenities", "dining", "meeting", "other"] for k in buckets[c]]
    def take(cat, n):
        got = buckets[cat][:n]
        if not got:
            spare = [k for k in pool][:1]
            if spare: warnings.append(f"no '{cat}' photo found; using {spare[0]} ({media[spare[0]]['_alt'][:50]}) instead: check it")
            got = spare
        return got
    rr = [buckets[c] for c in ["exterior", "rooms", "amenities", "dining", "meeting", "other"]]
    gallery, i = [], 0
    while len(gallery) < 8 and any(i < len(b) for b in rr):
        for b in rr:
            if i < len(b) and len(gallery) < 8: gallery.append(b[i])
        i += 1
    slots = {"fits": take("exterior", 1), "rooms": take("rooms", 2), "meeting": take("meeting", 2), "dining": take("dining", 2),
             "amenities": take("amenities", 2), "gallery": gallery}

    # ---- structured facts from the hotel's own pages ----
    meet_txt, dine_txt, room_txt = sub(r"meeting|event|venue"), sub(r"dining|restaurant|\bbar"), sub(r"room|suite|accommodation")
    def uniq(blocks):
        out, seen_t = [], set()
        for b in blocks:
            if b["title"].lower() not in seen_t: seen_t.add(b["title"].lower()); out.append(b)
        return out
    venues_raw = uniq([b for b in parse_titled_blocks(meet_txt) if b["seating"] or size_of(b["specs"])])
    rooms_raw = uniq([b for b in parse_titled_blocks(room_txt) if size_of(b["specs"])])
    styles = []
    for v in venues_raw:
        for k in v["seating"]:
            if k not in styles: styles.append(k)
    order = ["Theatre", "Classroom", "U Shaped", "Boardroom", "Circular", "Reception"]
    styles = sorted(styles, key=lambda s: order.index(s) if s in order else 99)
    venue_rows = [[html.escape(v["title"]), html.escape(size_of(v["specs"]))] + [v["seating"].get(s, "—") for s in styles]
                  for v in sorted(venues_raw, key=lambda v: -int(re.sub(r"\D", "", max(v["seating"].values(), key=lambda x: int(re.sub(r'\D', '', x) or 0)) if v["seating"] else "0") or 0))]
    room_rows = [[html.escape(r["title"]), html.escape(size_of(r["specs"])), html.escape(guests_of(r["specs"], {}))] for r in rooms_raw]
    if not venue_rows: warnings.append("no venue capacities found: the venue table will be left out")
    info = {}
    for k, rx in {"checkin": r"check-?in\s+(?:from|at|time:?)\s*([0-9: ]+\s*(?:am|pm|noon)?)", "checkout": r"check-?out\s+(?:until|by|at|time:?)\s*([0-9: ]*\s*(?:am|pm|noon))",
                  "rooms": r"(\d+\s+(?:guest\s+)?rooms?(?:\s+(?:and|&amp;|&)\s+(?:\d+\s+)?suites?)?)", "outlets": r"(\d+\s+restaurants?(?:\s+and\s+\d+\s+bars?)?)"}.items():
        m2 = re.search(rx, all_text, re.I)
        if m2: info[k] = m2.group(1).strip()
    rooms_line = re.search(r"\d+\s+rooms?(?:\s+and\s+\d+\s+suites?)?", all_text)
    highlights = []
    hm = re.search(r"Highlights\s*\n(.+?)(?:\n(?:Unparalleled|Offers|Information|Hotel\s*Information)|$)", main_text + "\n" + sub(r"overview|" + re.escape(base) + "$"), re.S)
    if hm:
        for l in [x.strip() for x in hm.group(1).split("\n") if x.strip()]:
            if l not in highlights and len(l) < 80: highlights.append(l)
    overview = html.unescape((ld.get("description") or sc.get("meta", {}).get("description") or "").strip())

    cfg = {
        "_generated_from": url, "_scraped_at": sc.get("scraped_at"),
        "slug": re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60],
        "name": html.escape(name),
        "location_tag": html.escape((ld.get("address") or {}).get("addressLocality") or (ld.get("address") or {}).get("addressRegion") or "") if isinstance(ld.get("address"), dict) else "",
        "address": html.escape(address), "phone": (ld.get("telephone") or "").split("/")[0].strip(),
        "sources": [url] + [p["url"] for p in own],
        "brand": brand, "fonts": fonts, "logos": logos,
        "_other_logo_candidates": [l.get("src") for l in L if l.get("src")][:6],
        "media": media, "slots": slots, "featured_video": None,
        "map": {"query": ",".join(coords(ld)) if coords(ld) else html.unescape(name + (", " + address if address else ""))},
        "overview": html.escape(overview),
        "highlights": highlights[:6],
        "facts": info,
        "fits": [{"icon": ic, "title": html.escape(h), "body": ""} for ic, h in zip(["aeroplane", "house", "money", "shield"], highlights[:4])],
        "rooms_intro": html.escape(((rooms_line.group(0) + ". ") if rooms_line else "") + find(room_txt, r"room|suite[^.]*\.", 1)).strip(),
        "room_types": {"columns": ["Room type", "Size", "Guests"], "rows": room_rows} if room_rows else None,
        "meeting_intro": "",
        "venues": {"note": "Capacities as published by the hotel; confirm setups with the hotel.", "columns": ["Venue", "Area"] + styles, "rows": venue_rows},
        "dining_title": "Restaurants &amp; Bars",
        "dining_intro": "",
        "av_support": "",
        "location_intro": html.escape(address or name),
        "location_cards": [], "amenities_line": "",
    }
    if venue_rows:
        big = max(venues_raw, key=lambda v: max([int(re.sub(r"\D", "", x) or 0) for x in v["seating"].values()] or [0]))
        cfg["meeting_intro"] = html.escape(f"{len(venue_rows)} indoor and outdoor venues, from intimate boardrooms to {big['title']} for up to {max(big['seating'].values(), key=lambda x: int(re.sub(r'\D','',x) or 0))} guests.")
    review += ["overview", "fits (bodies empty: write from facts)", "dining_intro", "av_support", "location_cards", "amenities_line"]
    cfg["_review"] = review; cfg["_warnings"] = warnings; cfg["_brand_notes"] = notes
    cfg["_raw"] = {"venues": venues_raw, "rooms": rooms_raw, "dining_text": dine_txt[:3000]}
    return cfg


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("scrape"); ap.add_argument("-o")
    a = ap.parse_args()
    cfg = convert(json.load(open(a.scrape)))
    out = a.o or f"hotels/{cfg['slug']}.json"
    json.dump(cfg, open(out, "w"), indent=2, ensure_ascii=False)
    print(f"wrote {out}\n  brand: {cfg['brand']}\n  heading font: {cfg['fonts']['heading']['site_font']} -> {cfg['fonts']['heading']['stack']} ({cfg['fonts']['heading']['transform']}, {cfg['fonts']['heading']['letter_spacing']})"
          f"\n  body font: {cfg['fonts']['body']['site_font']} -> {cfg['fonts']['body']['stack']}\n  logos: {[l['_from'] for l in cfg['logos']]}"
          f"\n  photos: {len(cfg['media'])} | slots: {cfg['slots']}\n  venues: {len(cfg['venues']['rows'])} | room types: {len((cfg['room_types'] or {}).get('rows', []))} | highlights: {cfg['highlights']}")
    for n in cfg["_brand_notes"]: print("  ·", n)
    for w in cfg["_warnings"]: print("  ⚠", w)
