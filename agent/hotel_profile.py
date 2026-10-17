"""Versioned hotel profiles with field-level provenance (PDF §5, §7.2).

A profile is the normalized, evidence-backed description of one hotel. The pipeline builds one per job from the
merged config (scrape + grounded copy + reviewer overrides) and saves it in the hotel store as a new version
when, and only when, its content changed:

    <hotels dir>/<hotel_id>/profile.json            latest version
    <hotels dir>/<hotel_id>/versions/v<N>.json      every version
    <hotels dir>/<hotel_id>/index.json              {latest_version, approved_version, latest_hash}
    <hotels dir>/<hotel_id>/customization.json      saved per-hotel customization (written by the API)
    <hotels dir>/<hotel_id>/generations.json        which job produced what, from which profile version

Provenance of a field is one of: officially_verified (found in the official page's text or structured data),
extracted (taken from the site, exact location not confirmed), normalized (derived from extracted values),
inferred (generated copy: never treated as verified), manually_corrected (a reviewer's override).
Approval is separate: nothing is "approved for proposal use" until a person (or an authorised auto-approve) says so.
"""
import contextlib, copy, html, json, os, re, time, urllib.parse

import scrape_to_config as S2C
from common import digest, jread, jwrite, now_iso, slugify

U = html.unescape
HOTEL_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,80}$")
REQUIRED = ("property_name", "official_website_url", "address", "city")
LOW_CONFIDENCE = 0.6
# Confidence is a heuristic, not a probability. "inferred" is generated wording that already passed the grounding check (every number and name is in
# the hotel's own text), so it is usable but never "verified". "extracted" is a value the scraper took from the site without confirming where.
CONF = {"structured": 0.95, "quoted": 0.85, "extracted": 0.55, "normalized": 0.7, "inferred": 0.7, "ai": 0.6, "manual": 1.0}


# ------------------------------------------------------------------ identity
LANGS = set("en fr de es it pt nl ja zh ko ru ar tr th vi sv da fi cs hu el he hi pl".split())     # a bare two-letter first segment is a locale only if it is a language
TRAILING = S2C.SECTION_WORDS | {"hoteldetail", "index", "index.html", "overview", "default.aspx"}


def hotel_path(path):
    """The path that identifies ONE hotel (original case): its overview page, without a trailing section page."""
    base = S2C.hotel_base_path(path).rstrip("/")
    if re.fullmatch(r"[A-Za-z]{2,3}", base.rsplit("/", 1)[-1]):      # ends in a country or language code (…/hotels/us): that prefix is shared by many hotels
        parts = [p for p in path.split("/") if p]
        while parts and parts[-1].lower() in TRAILING: parts.pop()
        base = "/" + "/".join(parts)
    return base


def hotel_identity(url):
    """Stable id from the official host + the hotel's own path. A locale segment (en-in, or a language code) is ignored; a state or city code is not."""
    u = urllib.parse.urlparse(url)
    host = re.sub(r"^www\.", "", (u.hostname or "").lower())
    official = hotel_path(u.path); base = official.lower()
    m = re.match(r"^/([a-z]{2})(?:-([a-z]{2,4}))?(?=/|$)", base)
    if m and (m.group(2) or m.group(1) in LANGS): base = base[m.end():]
    last = base.rsplit("/", 1)[-1] or host.split(".")[0]
    key = host + base
    return {"hotel_id": f"{slugify(last, 40) or 'hotel'}-{digest(key, 8)}", "key": key, "host": host, "base_path": base, "official_path": official}


# ------------------------------------------------------------------ store
class Store:
    def __init__(self, root):
        self.root = root

    def path(self, hid, *parts):
        if not HOTEL_ID.match(hid or ""): raise ValueError("bad hotel id")
        return os.path.join(self.root, hid, *parts)

    @contextlib.contextmanager
    def lock(self, hid, timeout=20):
        d = self.path(hid); os.makedirs(d, exist_ok=True); lk = os.path.join(d, ".lock"); t0 = time.time()
        while True:
            try: os.mkdir(lk); break
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(lk) > 60: os.rmdir(lk); continue      # stale lock from a crashed run
                except OSError: pass
                if time.time() - t0 > timeout: raise TimeoutError(f"hotel store busy for {hid}")
                time.sleep(0.05)
        try: yield
        finally:
            with contextlib.suppress(OSError): os.rmdir(lk)

    def index(self, hid):
        return jread(self.path(hid, "index.json"), {"latest_version": 0, "approved_version": None, "latest_hash": None})

    def get(self, hid, version=None):
        if version is None: return jread(self.path(hid, "profile.json"))
        return jread(self.path(hid, "versions", f"v{int(version)}.json"))

    def save(self, profile):
        """Store `profile` as a new version unless the latest version has identical content. Returns the stored profile."""
        hid = profile["hotel_id"]
        with self.lock(hid):
            idx = self.index(hid)
            if idx["latest_version"] and idx.get("latest_hash") == profile["content_hash"]:
                return self.get(hid, idx["latest_version"])
            n = idx["latest_version"] + 1
            profile = {**profile, "profile_version": n, "profile_status": "draft"}
            os.makedirs(self.path(hid, "versions"), exist_ok=True)
            jwrite(self.path(hid, "versions", f"v{n}.json"), profile)
            jwrite(self.path(hid, "profile.json"), profile)
            jwrite(self.path(hid, "index.json"), {**idx, "latest_version": n, "latest_hash": profile["content_hash"]})
            return profile

    def approve(self, hid, version, by):
        with self.lock(hid):
            p = self.get(hid, version)
            if not p: raise ValueError(f"no version {version} for {hid}")
            p["profile_status"], p["approved_by"], p["approved_at"] = "approved", by, now_iso()
            for e in p.get("evidence", []): e["approval_status"] = "approved"
            for a in p.get("assets", []): a["approval_status"] = "approved"
            jwrite(self.path(hid, "versions", f"v{int(version)}.json"), p)
            idx = self.index(hid)
            if idx["latest_version"] == int(version): jwrite(self.path(hid, "profile.json"), p)
            jwrite(self.path(hid, "index.json"), {**idx, "approved_version": max(idx.get("approved_version") or 0, int(version))})   # approving an older version never un-approves a newer one
            return p

    def generations(self, hid):
        return jread(self.path(hid, "generations.json"), [])

    def record_generation(self, hid, rec):
        with self.lock(hid):
            g = [x for x in self.generations(hid) if x.get("job_id") != rec["job_id"]] + [rec]
            jwrite(self.path(hid, "generations.json"), g[-200:])

    def customization(self, hid):
        return (jread(self.path(hid, "customization.json"), {}) or {}).get("customization") or {}

    def save_customization(self, hid, custom, version, by):
        """Make `custom` the hotel's saved customization. Approving brand-asset rights records who did, and when."""
        c = copy.deepcopy(custom); ar = c.get("asset_rights"); ts = now_iso()
        with self.lock(hid):
            prev = jread(self.path(hid, "customization.json"), {}) or {}
            was = (prev.get("customization") or {}).get("asset_rights") or {}
            if ar and ar.get("approved"):           # who approved the rights, and when, is kept when the approval is unchanged and stamped only when it is new
                kept = was.get("approved") and was.get("approved_by")
                ar["approved_by"], ar["approved_at"] = (was["approved_by"], was.get("approved_at")) if kept else (by, ts)
            jwrite(self.path(hid, "customization.json"), {"customization": c, "version": version, "updated_by": by, "updated_at": ts})
            with open(self.path(hid, "audit.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps({"at": ts, "event": "customization_saved", "by": by, "version": version, "previous_version": prev.get("version"),
                                    "asset_rights_approved": bool(ar and ar.get("approved"))}) + "\n")

    def others(self, hid):
        """(hotel_id, property_name) of every other stored hotel, for cross-hotel leakage checks."""
        out = []
        for n in sorted(os.listdir(self.root)) if os.path.isdir(self.root) else []:
            if n == hid or not HOTEL_ID.match(n): continue
            p = jread(os.path.join(self.root, n, "profile.json"))
            if p and p.get("property_name"): out.append((n, p["property_name"]))
        return out


# ------------------------------------------------------------------ building a profile
def _num(s):
    return int(re.sub(r"\D", "", str(s)) or 0)


def build_profile(*, hotel, inp, h, cfg, facts, cp, ov, custom, scrape, plan_branding=None):
    """Assemble the profile document. `h` is the merged config; `cfg` the raw convert output (with _raw)."""
    ts = now_iso()
    facts_by = {}
    for f in facts or []: facts_by.setdefault(f["field"], f)
    copy_fields, fallbacks = (cp or {}).get("fields", {}), set((cp or {}).get("fallbacks", []))
    ai = set((cp or {}).get("ai_polished", []))
    ld = S2C.hotel_ld((scrape.get("jsonld", []) if scrape else []) +
                      [j for p in (scrape or {}).get("subpages", []) if p["url"] in (cfg.get("sources") or []) for j in p.get("jsonld", [])])
    ld_addr = ld.get("address") if isinstance(ld.get("address"), dict) else {}
    evidence = []

    def add(field, value, prov, conf, src=None, method="page_text"):
        if value in (None, "", [], {}): return
        v = value if isinstance(value, (int, float, bool)) else (str(value)[:500] if not isinstance(value, (list, dict)) else value)
        evidence.append({"field_key": field, "field_value": v, "source_url": src, "extraction_method": method, "confidence_score": conf,
                         "provenance": prov, "approval_status": "pending", "verified_at": ts})

    def from_fact(key):
        """(provenance, confidence, source, method) for a field backed by a facts.json entry."""
        f = facts_by[key]
        if not f.get("verified"): return "extracted", CONF["extracted"], f.get("source_url"), "scraper"
        structured = str(f["source_url"]).startswith("structured data")
        return "officially_verified", CONF["structured" if structured else "quoted"], f["source_url"], "structured_data" if structured else "page_text"

    def scalar(field, value, key):
        """A hotel fact that has the same key in the config and in facts.json (name, address, phone)."""
        if not value: return
        if key in ov: add(field, value, "manually_corrected", CONF["manual"], None, "reviewer_override")
        elif key in facts_by: add(field, value, *from_fact(key))
        else: add(field, value, "extracted", CONF["extracted"], inp["hotel_url"], "scraper")

    name = U(h.get("name", ""))
    address = U(h.get("address", ""))
    phone = U(h.get("phone", "") or "")
    city = U(h.get("location_tag", ""))
    country = ld_addr.get("addressCountry"); country = country.get("name") if isinstance(country, dict) else country
    scalar("property_name", name, "name"); scalar("address", address, "address"); scalar("phone", phone, "phone")
    if city:
        if "location_tag" in ov: add("city", city, "manually_corrected", CONF["manual"], None, "reviewer_override")
        else: add("city", city, "officially_verified", 0.9, "structured data (schema.org Hotel)", "structured_data")
    for fld, val in (("region", ld_addr.get("addressRegion")), ("country", country)):
        if val and isinstance(val, str): add(fld, val, "officially_verified", 0.9, "structured data (schema.org Hotel)", "structured_data")
    email = ld.get("email") if isinstance(ld.get("email"), str) else None
    if email: add("email", email, "officially_verified", 0.9, "structured data (schema.org Hotel)", "structured_data")
    brand = ld.get("brand"); brand = brand.get("name") if isinstance(brand, dict) else brand
    brand_name = brand if isinstance(brand, str) and brand else ((scrape or {}).get("meta", {}).get("site_name") or None)
    if brand_name: add("brand_name", brand_name, "extracted", CONF["extracted"], inp["hotel_url"], "structured_data" if brand == brand_name else "meta_tag")

    # property summary: scraped description, generated copy or a reviewer's text: never all treated alike
    summary = U(h.get("overview", ""))
    if summary:
        if "overview" in ov: add("property_summary", summary, "manually_corrected", CONF["manual"], None, "reviewer_override")
        elif "overview" in copy_fields: add("property_summary", summary, "inferred", CONF["ai" if "overview" in ai else "inferred"], None, "ai_rewrite" if "overview" in ai else "rule_based_copy")
        else: add("property_summary", summary, "extracted", 0.8, inp["hotel_url"], "structured_data" if ld.get("description") else "meta_tag")

    # rooms / venues / amenities
    def status_of(kind, name, full, overridden):
        """(fact, status) for one table row. Facts are matched by the row's own name, never by position. A row is only as verified as the
        hotel's page: if the cells differ from what the page said, or a reviewer replaced the table, it is a manual correction."""
        f = next((x for x in facts or [] if x["field"] == kind and str(x["value"]).split(" | ")[0].strip().lower() == U(name).strip().lower()), None)
        if f and str(f["value"]) == full: return f, ("officially_verified" if f.get("verified") else "extracted")
        if f or overridden: return None, "manually_corrected"
        return None, "extracted"
    rooms = []
    for i, r in enumerate((h.get("room_types") or {}).get("rows", [])):
        f, st = status_of("room_type", r[0], U(" | ".join(r)), "room_types" in ov)
        rooms.append({"room_id": f"room_{i + 1}", "room_name": U(r[0]), "room_category": None, "description": None,
                      "features": [U(r[1])] if len(r) > 1 and r[1] else [], "capacity": U(r[2]) if len(r) > 2 and r[2] else None,
                      "source_url": f and f.get("source_url"), "verification_status": st})
    cols = (h.get("venues") or {}).get("columns", [])
    venues = []
    for i, r in enumerate((h.get("venues") or {}).get("rows", [])):
        f, st = status_of("venue", r[0], U(r[0]) + " | " + " | ".join(r[1:]), "venues" in ov)
        caps = {cols[j]: U(r[j]) for j in range(2, min(len(r), len(cols))) if r[j] not in ("—", "")}
        venues.append({"venue_id": f"venue_{i + 1}", "name": U(r[0]), "area": U(r[1]) if len(r) > 1 else "", "capacities": caps,
                       "source_url": f and f.get("source_url"), "verification_status": st})
    table_prov = lambda items: ("manually_corrected" if any(x["verification_status"] == "manually_corrected" for x in items)
                                else "officially_verified" if all(x["verification_status"] == "officially_verified" for x in items) else "extracted")
    amen_src = (h.get("amenities_line") or "")
    amenities = []
    if amen_src and "amenities_line" not in fallbacks:
        manual = "amenities_line" in ov
        for i, a in enumerate(x.strip() for x in re.split(r"\s·\s|,", U(amen_src)) if x.strip()):
            amenities.append({"amenity_id": f"amenity_{i + 1}", "name": a, "category": None, "description": None, "source_url": None,
                              "verification_status": "manually_corrected" if manual else "normalized"})
    first_src = lambda items: next((x["source_url"] for x in items if x.get("source_url")), None)
    if rooms: add("rooms", f"{len(rooms)} room types", table_prov(rooms), CONF["manual" if table_prov(rooms) == "manually_corrected" else "quoted"], first_src(rooms), "reviewer_override" if table_prov(rooms) == "manually_corrected" else "page_text")
    if venues: add("venues", f"{len(venues)} venues", table_prov(venues), CONF["manual" if table_prov(venues) == "manually_corrected" else "quoted"], first_src(venues), "reviewer_override" if table_prov(venues) == "manually_corrected" else "page_text")
    if amenities: add("amenities", f"{len(amenities)} amenities", "manually_corrected" if "amenities_line" in ov else "normalized", CONF["normalized"], None, "keyword_match")
    for k in ("checkin", "checkout", "rooms", "outlets"):
        f = facts_by.get(k)
        if f: add("facts." + k, f["value"], *from_fact(k))
    if "map" in facts_by: add("map", facts_by["map"]["value"], *from_fact("map"))

    # generated copy is evidence too, so it is never mistaken for a verified fact
    for k, v in copy_fields.items():
        if k in ov or k == "overview": continue
        add("copy." + k, v if isinstance(v, str) else "structured copy", "inferred", CONF["ai" if k in ai else "inferred"], None, "ai_rewrite" if k in ai else "rule_based_copy")
    for k in ov:
        if k in ("overview", "name", "address", "phone", "location_tag", "amenities_line"): continue
        if k in ("brand", "fonts", "slots", "media", "logos", "map", "customization"): continue
        add("override." + k, ov[k] if isinstance(ov[k], str) else "reviewer override", "manually_corrected", CONF["manual"], None, "reviewer_override")

    # brand tokens: extracted from computed styles, unless the hotel's customization or a reviewer set them
    set_by = dict(plan_branding or {})
    for k in ("primary", "accent", "dark", "tint", "text"):
        v = (h.get("brand") or {}).get(k)
        if not v: continue
        if k in set_by or k in (ov.get("brand") or {}): add("brand." + k, v, "manually_corrected", CONF["manual"], None, "customization")
        else: add("brand." + k, v, "extracted", CONF["normalized"], inp["hotel_url"], "computed_style")
    for k in ("heading", "body"):
        f = (h.get("fonts") or {}).get(k) or {}
        if f.get("web_font"):
            prov = "manually_corrected" if k + "_font" in set_by else ("officially_verified" if f.get("exact_match") else "normalized")
            add("font." + k, f["web_font"], prov, CONF["manual" if prov == "manually_corrected" else "normalized"], inp["hotel_url"], "computed_style")

    # assets: references only (nothing is copied or stored); rights are unverified until a person says otherwise
    rights = "approved" if (custom.get("asset_rights") or {}).get("approved") else "unverified"
    assets = []
    for i, lg in enumerate(h.get("logos", [])):
        u = lg.get("url", "")
        assets.append({"asset_id": f"logo_{i + 1}", "asset_type": "logo", "source_url": u if not u.startswith("data:") else "inline-svg", "stored_asset_reference": None,
                       "alt_text": "", "dimensions": None, "approval_status": "pending", "usage_rights_status": rights, "source_page": lg.get("_from"), "content_hash": None})
    for key, m in (h.get("media") or {}).items():
        assets.append({"asset_id": key, "asset_type": "photo", "source_url": m.get("source_url"), "stored_asset_reference": None, "alt_text": m.get("_alt", ""),
                       "dimensions": m.get("_size") or None, "approval_status": "pending", "usage_rights_status": rights, "source_page": m.get("_from") or None, "content_hash": None})

    off = urllib.parse.urlparse(inp["hotel_url"])
    ident = hotel
    sv = source_verification(inp, scrape, ld)
    profile = {
        "schema": 1, "hotel_id": ident["hotel_id"], "property_name": name,
        "official_website_url": f"{off.scheme}://{off.netloc}{ident['official_path']}",
        "brand_name": brand_name, "address": address, "city": city, "region": ld_addr.get("addressRegion") if isinstance(ld_addr.get("addressRegion"), str) else None,
        "country": country if isinstance(country, str) else None, "language": None, "currency": None, "timezone": None,
        "property_summary": summary, "sales_contact": None, "phone": phone, "email": email,
        "facts": dict(h.get("facts") or {}), "rooms": rooms, "venues": venues, "amenities": amenities, "assets": assets,
        "brand": copy.deepcopy(h.get("brand") or {}), "fonts": {k: {x: (h.get("fonts") or {}).get(k, {}).get(x) for x in ("site_font", "web_font", "exact_match")} for k in ("heading", "body")},
        "map_query": (h.get("map") or {}).get("query"), "sources": list(cfg.get("sources") or []),
        "source_verification": sv, "evidence": evidence,
        "profile_status": "draft", "profile_version": 0, "last_verified_at": (cfg.get("_scraped_at") or ts), "built_at": ts,
    }
    profile["content_hash"] = content_hash(profile)
    return profile


def content_hash(profile):
    """Hash of what the profile *says*: not its version, status, approval or timestamps."""
    p = copy.deepcopy(profile)
    for k in ("content_hash", "profile_version", "profile_status", "last_verified_at", "built_at", "approved_by", "approved_at"): p.pop(k, None)
    for e in p.get("evidence", []): e.pop("verified_at", None); e.pop("approval_status", None)
    for a in p.get("assets", []): a.pop("approval_status", None)
    p.get("source_verification", {}).pop("checked_at", None)
    return digest(p, 16)


def source_verification(inp, scrape, ld):
    """Is this page plausibly the hotel's (or its brand's) own? Records the evidence; never claims more than it has."""
    host = re.sub(r"^www\.", "", (urllib.parse.urlparse(inp["hotel_url"]).hostname or "").lower())
    allow = [x.strip().lower() for x in os.environ.get("ALLOWED_HOTEL_DOMAINS", "").split(",") if x.strip()]
    in_allow = any(host == a or host.endswith("." + a) for a in allow) if allow else None
    sc_host = re.sub(r"^www\.", "", (urllib.parse.urlparse((scrape or {}).get("url", "")).hostname or "").lower())
    structured = bool(ld)
    title = ((scrape or {}).get("title") or "") + " " + ((scrape or {}).get("meta", {}).get("og_title") or "")
    name_in_title = bool(ld.get("name")) and str(ld["name"]).lower()[:25] in title.lower()
    verified = bool(in_allow) or (structured and (not sc_host or sc_host == host or sc_host.endswith("." + host) or host.endswith("." + sc_host)))
    return {"host": host, "scraped_host": sc_host or None, "allowlist_configured": bool(allow), "in_allowlist": in_allow,
            "structured_data_hotel": structured, "name_in_title": name_in_title, "status": "verified" if verified else "unverified",
            "checked_at": now_iso()}


# ------------------------------------------------------------------ validation (PDF §7.2 levels 1-3)
def _time(s):
    m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|noon)?", str(s).lower())
    if not m: return None
    hh, mm, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap == "noon": return 12 * 60
    if ap == "pm" and hh < 12: hh += 12
    if ap == "am" and hh == 12: hh = 0
    return hh * 60 + mm


def validate_profile(p, allow_http=False):
    """Structural (1), semantic (2) and source (3) validation. A field is never valid merely because it is non-empty."""
    crit, warn = [], []

    def C(level, code, cat, msg, fields=()): crit.append({"level": level, "code": code, "category": cat, "message": msg, "fields": list(fields)})
    def W(level, code, cat, msg, fields=()): warn.append({"level": level, "code": code, "category": cat, "message": msg, "fields": list(fields)})

    missing = [k for k in REQUIRED if not p.get(k)]
    if missing: C(1, "REQUIRED_FIELD_MISSING", "REQUIRED_INFO_MISSING", f"required hotel field(s) missing: {', '.join(missing)}", missing)
    url = p.get("official_website_url") or ""
    if url and not re.match(r"https://[^/\s@]+", url) and not (allow_http and url.startswith("http://")): C(1, "BAD_URL", "SOURCE_UNVERIFIED", "official_website_url must be an https URL", ["official_website_url"])
    if p.get("phone") and not (7 <= len(re.sub(r"\D", "", p["phone"])) <= 15 and re.fullmatch(r"[\d\s+().\-/xext]+", p["phone"], re.I)): W(1, "BAD_PHONE", "CONFLICTING_DATA", f"phone '{p['phone']}' is not a valid phone number", ["phone"])
    if p.get("email") and (len(p["email"]) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", p["email"])): W(1, "BAD_EMAIL", "CONFLICTING_DATA", f"email '{p['email']}' is not valid", ["email"])
    if p.get("currency") and not re.fullmatch(r"[A-Z]{3}", p["currency"]): W(1, "BAD_CURRENCY", "CONFLICTING_DATA", "currency must be an ISO 4217 code", ["currency"])
    for a in p.get("assets", []):
        u = a.get("source_url") or ""
        if u and u != "inline-svg" and not (u.startswith("https://") or (allow_http and u.startswith("http://"))): W(1, "BAD_ASSET_URL", "ASSET_MISSING", f"asset {a['asset_id']} is not an https URL, so it cannot be used", [a["asset_id"]])

    for v in p.get("venues", []):
        for k, cap in v["capacities"].items():
            n = _num(cap)
            if not 1 <= n <= 20000: W(2, "IMPLAUSIBLE_CAPACITY", "CONFLICTING_DATA", f"{v['name']}: {k} capacity '{cap}' is implausible", [v["venue_id"]])
        if v.get("area") and not 1 <= _num(v["area"]) <= 1_000_000: W(2, "IMPLAUSIBLE_AREA", "CONFLICTING_DATA", f"{v['name']}: area '{v['area']}' is implausible", [v["venue_id"]])
    for kind, items, key in (("room", p.get("rooms", []), "room_name"), ("venue", p.get("venues", []), "name")):
        seen = {}
        for it in items:
            k = re.sub(r"\W+", " ", it[key].lower()).strip()
            if k in seen: W(2, "DUPLICATE_RECORD", "CONFLICTING_DATA", f"duplicate {kind} '{it[key]}' (kept as published: not merged on name alone)", [it.get(kind + "_id", k)])
            seen[k] = 1
    ci, co = _time((p.get("facts") or {}).get("checkin")), _time((p.get("facts") or {}).get("checkout"))
    if ci is not None and co is not None and ci <= co:
        W(2, "CHECKIN_BEFORE_CHECKOUT", "CONFLICTING_DATA", f"check-in ({p['facts']['checkin']}) is not after check-out ({p['facts']['checkout']})", ["facts.checkin", "facts.checkout"])

    sv = p.get("source_verification") or {}
    if sv.get("scraped_host") and sv.get("host") and not (sv["scraped_host"] == sv["host"] or sv["scraped_host"].endswith("." + sv["host"]) or sv["host"].endswith("." + sv["scraped_host"])):
        C(3, "SOURCE_HOST_MISMATCH", "SOURCE_UNVERIFIED", f"the captured page is on {sv['scraped_host']} but hotel_url is on {sv['host']}: wrong or foreign scrape", ["official_website_url"])
    if sv.get("status") != "verified":
        W(3, "SOURCE_NOT_CONFIRMED", "SOURCE_UNVERIFIED", "the page could not be confirmed as the hotel's own site: no schema.org Hotel data and the host is not in ALLOWED_HOTEL_DOMAINS", ["official_website_url"])
    ev = {e["field_key"]: e for e in p.get("evidence", [])}
    nosrc = [k for k in REQUIRED if k != "official_website_url" and p.get(k) and k not in ev]
    if nosrc: W(3, "NO_EVIDENCE", "UNVERIFIED_CONTENT", f"no source evidence recorded for: {', '.join(nosrc)}", nosrc)
    low = [k for k in REQUIRED if k in ev and ev[k]["confidence_score"] < LOW_CONFIDENCE]
    if low: W(3, "LOW_CONFIDENCE", "UNVERIFIED_CONTENT", f"low-confidence required field(s): {', '.join(low)}: verify before publishing", low)

    levels = {str(n): ("fail" if any(c["level"] == n for c in crit) else "warn" if any(w["level"] == n for w in warn) else "pass") for n in (1, 2, 3)}
    return {"critical": crit, "warnings": warn, "levels": levels, "ok": not crit}
