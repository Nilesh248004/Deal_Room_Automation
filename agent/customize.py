"""Hotel-specific customization: validated config -> deterministic section plan (PDF §9).

The plan decides WHICH of the builder's sections a deal room carries and in what order. It does not render
anything: dealroom_builder.build() still renders every section, exactly as before, and the plan only selects and
orders them (§4.3: orchestrate the existing capability, don't build a second template engine).

Everything here is a pure function of (profile, config, customization), so the same inputs always give the same
plan. Free-text notes are kept for the reviewer but never interpreted: an LLM guess must not change a proposal
(§9.4: natural language may only *propose* a plan, and a plan must be explicit config before rendering).

CLI (used by the API to reject bad input before a job starts):
    echo '{"focus": "event"}' | python3 agent/customize.py --validate
"""
import html, json, re, sys

import scrape_to_config as S2C
from common import digest

# (key, default title): the order is the reference proposal's order and what the builder emits with no plan
SECTIONS = [("overview", "Overview"), ("fits", "Why This Hotel Fits"), ("rooms", "Guest Rooms"), ("concessions", "Concessions & Value Adds"),
            ("meeting", "Meeting Space"), ("pricing", "Pricing Summary"), ("dining", "Food & Beverage"), ("amenities", "Amenities & Fees"),
            ("av", "AV & Technology"), ("location", "Location & Destination"), ("gallery", "Virtual Tour & Gallery"), ("pdf", "Proposal PDF"),
            ("next_steps", "Next Steps")]
KEYS = [k for k, _ in SECTIONS]
TITLE = dict(SECTIONS)
FIXED_FIRST, FIXED_LAST = "overview", "next_steps"
ALIASES = {
    "overview": "overview", "why this hotel fits": "fits", "why this hotel": "fits", "fits": "fits", "hotel fit": "fits",
    "guest rooms": "rooms", "rooms": "rooms", "suites": "rooms", "accommodation": "rooms", "accommodations": "rooms",
    "concessions": "concessions", "concessions & value adds": "concessions", "value adds": "concessions",
    "meeting space": "meeting", "meetings": "meeting", "meeting": "meeting", "events": "meeting", "event space": "meeting", "event spaces": "meeting",
    "pricing": "pricing", "pricing summary": "pricing",
    "dining": "dining", "food & beverage": "dining", "food and beverage": "dining", "f&b": "dining", "restaurants": "dining", "restaurants & bars": "dining",
    "amenities": "amenities", "amenities & fees": "amenities", "fees": "amenities",
    "av": "av", "av & technology": "av", "technology": "av",
    "location": "location", "location & destination": "location", "destination": "location",
    "gallery": "gallery", "virtual tour": "gallery", "virtual tour & gallery": "gallery", "tour": "gallery",
    "pdf": "pdf", "proposal pdf": "pdf", "next steps": "next_steps",
}

# Template families are presets over the SAME renderer: a section order and defaults. "requires" are profile prerequisites
# a family cannot work without (explicitly asking for it without them is an error; auto-selection simply won't pick it).
_DEFAULT = KEYS
FAMILIES = {
    "standard": {"order": _DEFAULT, "requires": [], "about": "The reference proposal: every section in the reference order."},
    "premium": {"order": ["overview", "gallery", "fits", "rooms", "meeting", "dining", "amenities", "concessions", "pricing", "av", "location", "pdf", "next_steps"],
                "requires": [], "about": "Visual first: gallery and tour video right after the overview, then rooms and venues."},
    "corporate_event": {"order": ["overview", "fits", "meeting", "av", "dining", "rooms", "concessions", "pricing", "location", "amenities", "gallery", "pdf", "next_steps"],
                        "requires": [], "about": "Event first: meeting space, AV and food & beverage before rooms."},
    "resort_leisure": {"order": ["overview", "rooms", "amenities", "dining", "gallery", "fits", "location", "meeting", "av", "concessions", "pricing", "pdf", "next_steps"],
                       "requires": [], "about": "Leisure first: rooms, amenities, dining and gallery before event details."},
    "meeting_space": {"order": ["overview", "meeting", "av", "dining", "fits", "rooms", "concessions", "pricing", "location", "amenities", "gallery", "pdf", "next_steps"],
                      "requires": ["venues"], "about": "Meeting-space focused: the venue table leads. Needs published venue capacities."},
}
FOCUS_FAMILY = {"event": "corporate_event", "meetings": "meeting_space", "leisure": "resort_leisure", "rooms": "standard"}
CTA = ["schedule_site_visit", "request_revision", "download_pdf"]
RFP_NEEDS = {"rooms": "rooms", "meeting": "meeting", "concessions": "concessions", "av": "av", "dining_info": "dining"}
TOP_KEYS = {"template_family", "focus", "preferred_sections", "exclude_sections", "include_tour_videos", "include_gallery", "include_testimonials",
            "emphasize_rooms", "on_unavailable", "cta_buttons", "special_notes", "branding", "asset_rights", "rfp_needs"}
BRAND_COLORS = ("primary", "accent", "dark", "tint", "text")
FONT_NAMES = {"dm sans": "DM Sans", "dm serif display": "DM Serif Display", "eb garamond": "EB Garamond", "pt serif": "PT Serif", "pt sans": "PT Sans"}


def section_key(name):
    return ALIASES.get(html.unescape(str(name)).strip().lower())


def display_font(name):
    n = name.strip().lower()
    return FONT_NAMES.get(n) or n.title()


def _list(c, key, errs, norm_fn=None, max_items=20):
    v = c.get(key)
    if v is None: return None
    if not isinstance(v, list) or len(v) > max_items: errs.append(f"{key} must be a list of at most {max_items} items"); return None
    out = []
    for x in v:
        n = norm_fn(x) if norm_fn else x
        if n is None: errs.append(f"{key}: unknown value {x!r}")
        elif n not in out: out.append(n)
    return out


def validate_customization(raw):
    """(normalized, errors). Unknown keys are errors: a typo must not silently do nothing."""
    errs = []
    if raw in (None, {}): return {}, []
    if not isinstance(raw, dict): return {}, ["customization must be an object"]
    unknown = sorted(set(raw) - TOP_KEYS)
    if unknown: errs.append(f"unsupported customization field(s): {', '.join(unknown)} (supported: {', '.join(sorted(TOP_KEYS))})")
    n = {}
    fam = raw.get("template_family")
    if fam is not None:
        if fam != "auto" and fam not in FAMILIES: errs.append(f"template_family must be auto or one of {', '.join(FAMILIES)}")
        elif fam != "auto": n["template_family"] = fam
    if raw.get("focus") is not None:
        if raw["focus"] not in FOCUS_FAMILY: errs.append(f"focus must be one of {', '.join(FOCUS_FAMILY)}")
        else: n["focus"] = raw["focus"]
    pref = _list(raw, "preferred_sections", errs, section_key)
    excl = _list(raw, "exclude_sections", errs, section_key)
    if pref is not None: n["preferred_sections"] = pref
    if excl is not None: n["exclude_sections"] = excl
    for k in (excl or []):
        if k in (FIXED_FIRST, FIXED_LAST): errs.append(f"exclude_sections: '{TITLE[k]}' cannot be excluded")
    both = sorted(set(pref or []) & set(excl or []))
    if both: errs.append(f"sections cannot be both preferred and excluded: {', '.join(both)}")
    fam_, focus_ = n.get("template_family"), n.get("focus")
    if "meeting" in (excl or []) and (fam_ in ("meeting_space", "corporate_event") or (not fam_ and focus_ in ("event", "meetings"))):
        errs.append("exclude_sections: Meeting Space cannot be excluded from an event- or meeting-focused proposal")
    for k in ("include_tour_videos", "include_gallery", "include_testimonials"):
        if raw.get(k) is not None:
            if not isinstance(raw[k], bool): errs.append(f"{k} must be true or false")
            else: n[k] = raw[k]
    er = _list(raw, "emphasize_rooms", errs, lambda x: x.strip() if isinstance(x, str) and 1 <= len(x.strip()) <= 120 else None)
    if er is not None: n["emphasize_rooms"] = er
    if raw.get("on_unavailable") is not None:
        if raw["on_unavailable"] not in ("omit", "block"): errs.append("on_unavailable must be omit or block")
        else: n["on_unavailable"] = raw["on_unavailable"]
    cta = _list(raw, "cta_buttons", errs, lambda x: x if x in CTA else None, 3)
    if cta is not None:
        if not cta: errs.append("cta_buttons needs at least one button")
        else: n["cta_buttons"] = cta
    if raw.get("special_notes") is not None:
        if not isinstance(raw["special_notes"], str) or len(raw["special_notes"]) > 500: errs.append("special_notes must be text of at most 500 characters")
        else: n["special_notes"] = raw["special_notes"].strip()
    b = raw.get("branding")
    if b is not None:
        if not isinstance(b, dict): errs.append("branding must be an object")
        else:
            bad = sorted(set(b) - set(BRAND_COLORS) - {"heading_font", "body_font"})
            if bad: errs.append(f"unsupported branding token(s): {', '.join(bad)}")
            nb = {}
            for k in BRAND_COLORS:
                if b.get(k) is None: continue
                hx = S2C.norm_hex(b[k]) if isinstance(b[k], str) and b[k].startswith("#") else None
                if hx: nb[k] = hx
                else: errs.append(f"branding.{k} must be a #RRGGBB colour")
            for k in ("heading_font", "body_font"):
                if b.get(k) is None: continue
                if isinstance(b[k], str) and b[k].strip().lower() in S2C.GOOGLE: nb[k] = display_font(b[k])
                else: errs.append(f"branding.{k} must be a supported web font ({len(S2C.GOOGLE)} available, e.g. Open Sans, Playfair Display, Lato)")
            if nb: n["branding"] = nb
    ar = raw.get("asset_rights")
    if ar is not None:
        if not isinstance(ar, dict) or not isinstance(ar.get("approved"), bool) or set(ar) - {"approved", "approved_by", "approved_at", "note"}:
            errs.append("asset_rights must be {approved: true|false, approved_by?, note?}")
        else:
            n["asset_rights"] = {"approved": ar["approved"], **{k: str(ar[k])[:300] for k in ("approved_by", "approved_at", "note") if ar.get(k)}}
    rn = raw.get("rfp_needs")
    if rn is not None:
        if not isinstance(rn, dict) or any(not isinstance(v, bool) for v in rn.values()): errs.append("rfp_needs must be an object of true/false values (the output of agent/rfp.py needs())")
        else: n["rfp_needs"] = {k: v for k, v in rn.items() if k in RFP_NEEDS}
    return ({} if errs else n), errs


def merge(base, over):
    out = json.loads(json.dumps(base or {}))
    for k, v in (over or {}).items():
        out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def effective(saved, requested, reviewer):
    """Hotel's saved customization, then the request's, then a reviewer's override. Returns (normalized, version, errors)."""
    merged = merge(merge(saved, requested), reviewer)
    n, errs = validate_customization(merged)
    return n, digest(n, 10), errs


# ------------------------------------------------------------------ the plan
def _has(profile, req):
    return {"venues": bool(profile.get("venues"))}.get(req, True)


def make_plan(profile, h, cp, custom, version):
    """Deterministic section plan. `h` = merged config, `cp` = copy.json, `custom` = normalized customization."""
    dec, omitted, unmet, blocking = [], [], [], []
    policy = custom.get("on_unavailable", "omit")
    title = dict(TITLE); title["dining"] = html.unescape(h.get("dining_title") or TITLE["dining"])
    unesc = lambda s: html.unescape(s)

    # ---- 1. template family ----
    fam, src = custom.get("template_family"), "explicit"
    if fam:
        miss = [r for r in FAMILIES[fam]["requires"] if not _has(profile, r)]
        if miss:
            blocking.append({"code": "FAMILY_NOT_SUPPORTED", "category": "UNSUPPORTED_TEMPLATE", "fields": miss,
                             "message": f"template family '{fam}' needs {', '.join(miss)} but the hotel's pages did not provide it"})
        dec.append(f"template family '{fam}' requested explicitly")
    elif custom.get("focus"):
        fam, src = FOCUS_FAMILY[custom["focus"]], "focus"
        if [r for r in FAMILIES[fam]["requires"] if not _has(profile, r)]:
            dec.append(f"focus '{custom['focus']}' prefers '{fam}' but the hotel has no published venue capacities: using 'corporate_event'")
            unmet.append({"request": f"focus '{custom['focus']}' (venue-table template)", "reason": "the hotel's pages published no venue capacities, so the corporate_event family was used instead"})
            fam = "corporate_event"
        else: dec.append(f"focus '{custom['focus']}' selects template family '{fam}'")
    else:
        fam, src = "standard", "default"
        dec.append("no family or focus given: standard family (the reference proposal)")
    order = list(FAMILIES[fam]["order"])
    required = [FIXED_FIRST, FIXED_LAST] + (["meeting"] if fam in ("meeting_space", "corporate_event") else [])      # an event proposal without meeting space is not one (§8.1)

    # ---- 2. what the opportunity does not need, and what was excluded ----
    drop = {}
    for k, need in RFP_NEEDS.items():
        if (custom.get("rfp_needs") or {}).get(k) is False: drop[need] = ("RFP_NOT_NEEDED", "the RFP does not ask for it")
    for k in custom.get("exclude_sections", []): drop[k] = ("EXCLUDED", "excluded by customization")
    for k in custom.get("preferred_sections", []):
        if k in drop and drop[k][0] == "RFP_NOT_NEEDED":
            dec.append(f"'{title[k]}' is preferred, so it stays although the RFP does not ask for it"); del drop[k]

    for k in required:
        if k in drop and k not in (FIXED_FIRST, FIXED_LAST):
            blocking.append({"code": "REQUIRED_SECTION_DROPPED", "category": "REQUIRED_INFO_MISSING", "fields": [k], "message": f"'{title[k]}' is required for the {fam} family but was dropped ({drop[k][1]})"})
            del drop[k]

    # ---- 3. hotel-owned content: sections that exist only to show hotel content need that content ----
    fits_ok = any(x.get("body") for x in h.get("fits") or [])
    loc_ok = bool(h.get("location_cards")) and bool((h.get("map") or {}).get("query"))
    gallery_ok = bool((h.get("slots") or {}).get("gallery"))
    video_on = custom.get("include_tour_videos", True)
    images_on = custom.get("include_gallery", True) and gallery_ok
    content = {"fits": (fits_ok, ["fits"]), "location": (loc_ok, ["location_cards", "map"]), "gallery": (images_on or video_on, ["gallery images or tour video"])}
    if custom.get("include_gallery") is False and not video_on: drop["gallery"] = ("EXCLUDED", "gallery images and tour videos are both switched off")
    for k, (ok, fields) in content.items():
        if ok or k in drop: continue
        if policy == "block": blocking.append({"code": "SECTION_NOT_POPULATED", "category": "REQUIRED_INFO_MISSING", "fields": fields, "message": f"'{title[k]}' cannot be populated: the hotel's pages gave no {', '.join(fields)}"})
        else: drop[k] = ("NO_CONTENT", f"the hotel's pages gave no {', '.join(fields)}")
    requested = set(custom.get("preferred_sections", []))
    for k, (code, why) in drop.items():
        if k in order: order.remove(k)
        omitted.append({"key": k, "title": title[k], "code": code, "reason": why})
        dec.append(f"'{title[k]}' omitted: {why}")
        if k in requested and code == "NO_CONTENT": unmet.append({"request": f"preferred section '{title[k]}'", "reason": why})

    # ---- 4. preferred order: after the overview, before everything else; next steps stays last ----
    pref = [k for k in custom.get("preferred_sections", []) if k in order and k not in (FIXED_FIRST, FIXED_LAST)]
    if pref:
        rest = [k for k in order if k not in pref and k not in (FIXED_FIRST, FIXED_LAST)]
        order = [FIXED_FIRST] + pref + rest + [FIXED_LAST]
        dec.append("preferred sections moved up: " + ", ".join(title[k] for k in pref))

    # ---- 5. flags the renderer honours, and requests that cannot be met ----
    fv = bool((h.get("featured_video") or {}).get("hippo"))
    if custom.get("include_tour_videos") is True and not fv:
        unmet.append({"request": "tour videos", "reason": "no approved video asset is on file; the dynamic Virtual Tour Video slot is kept"})
    if custom.get("include_testimonials") is True:
        unmet.append({"request": "testimonials", "reason": "no approved testimonials exist for this hotel; none are extracted or invented"})
    rows = [r[0] for r in (h.get("room_types") or {}).get("rows", [])]
    found = []
    for want in custom.get("emphasize_rooms", []):
        hit = next((r for r in rows if unesc(r).strip().lower() == want.strip().lower()), None)
        if hit: found.append(hit)
        else: unmet.append({"request": f"emphasize room '{want}'", "reason": "no room with that name was published on the hotel's pages" if rows else "the hotel's pages have no room table"})
    flags = {"tour_video_slot": bool(video_on and "gallery" in order), "featured_video": bool(video_on and fv and "gallery" in order),
             "gallery_images": bool(images_on and "gallery" in order), "cta_buttons": custom.get("cta_buttons") or list(CTA), "emphasize_rooms": found}
    left_out = sorted((cp or {}).get("left_out", []))
    if left_out: dec.append("left out because nothing sourced was found on the hotel's pages: " + ", ".join(left_out))
    return {"plan_version": 1, "hotel_id": profile["hotel_id"], "profile_version": profile.get("profile_version"), "customization_version": version,
            "template_family": fam, "family_source": src, "on_unavailable": policy,
            "sections": [{"key": k, "title": unesc(title[k]), "order": i} for i, k in enumerate(order, 1)],
            "omitted": omitted, "unmet_requests": unmet, "blocking": blocking, "flags": flags, "required_sections": required,
            "left_out": left_out, "special_notes": custom.get("special_notes", ""), "branding": custom.get("branding", {}),
            "decisions": dec}


def apply_branding(h, branding):
    """Return h with the customization's colour and font tokens applied (only supported tokens ever get here)."""
    if not branding: return h
    h = json.loads(json.dumps(h))
    for k in BRAND_COLORS:
        if branding.get(k): h.setdefault("brand", {})[k] = branding[k]
    for slot, key in (("heading", "heading_font"), ("body", "body_font")):
        if branding.get(key):
            old = (h.get("fonts") or {}).get(slot) or {}
            fs = S2C.font_stack({"family": branding[key], "fallbacks": [], "weight": "", "size": "", "transform": old.get("transform", "none"), "letter_spacing": old.get("letter_spacing", "normal"), "weight_used": 0})
            h.setdefault("fonts", {})[slot] = {**fs, "site_font": old.get("site_font") or branding[key]}
    if branding.get("body_font"): h.setdefault("brand", {})["font"] = branding["body_font"]
    return h


def apply_emphasis(h, names):
    """Put the emphasised rooms first in the room table (nothing is merged or renamed)."""
    rt = h.get("room_types") or {}
    if not names or not rt.get("rows"): return h
    h = json.loads(json.dumps(h)); want = [html.unescape(n).lower() for n in names]
    rows = h["room_types"]["rows"]
    rows.sort(key=lambda r: want.index(html.unescape(r[0]).lower()) if html.unescape(r[0]).lower() in want else len(want))
    return h


if __name__ == "__main__":
    if sys.argv[1:] == ["--validate"]:
        try: raw = json.load(sys.stdin)
        except ValueError: print(json.dumps({"ok": False, "errors": ["customization is not valid JSON"]})); sys.exit(0)
        n, errs = validate_customization(raw)
        print(json.dumps({"ok": not errs, "errors": errs, "customization": n, "version": digest(n, 10)})); sys.exit(0)
    print(__doc__); sys.exit(64)
