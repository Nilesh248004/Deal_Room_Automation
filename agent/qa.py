"""Final quality assurance before a deal room can be approved or published (PDF §11).

A finished build is not publishable just because it built. QA returns one of
  PASS    every critical check passed and nothing needs a person
  REVIEW  something uncertain needs a person's decision (publication waits, even in mode=auto)
  FAIL    a critical requirement is broken (publication is blocked)

What is checked here is the generated template, the plan, the mapping, the profile and the validated assets. What is
NOT checked here is how Hippo renders the saved page on a phone or desktop, or a PDF/share link: the agent has no
Hippo preview URL or export API to open, so those are reported as `not_run` instead of being passed silently.
"""
import html, json, os, re

from dealroom_builder import SECTION_ID_BY_KEY

MONEY = re.compile(r"[$€£₹¥]\s?\d|\b\d[\d,.]*\s?(?:USD|EUR|GBP|INR|AUD|CAD|AED|CHF|JPY)\b|\bper night\b|\bnightly rate\b", re.I)
SAFE_HREF = re.compile(r"^(?:|#|\$\{[^}]+\}|mailto:[^\s]+|https://[^\s\"'<>]+)$")
CTA_EXPECT = {"schedule_site_visit": ("${User.Calendar Link}", None), "request_revision": ("${User.Email}", "email"), "download_pdf": (None, None)}
WIDE_TABLE = 7


def rights_policy():
    p = os.environ.get("ASSET_RIGHTS_POLICY", "require").lower()
    return p if p in ("require", "warn") else "require"


def _texts(tpl):
    """(location, html) for every rich-text and card text in the template."""
    out = []
    def walk(o, where):
        if isinstance(o, dict):
            t = o.get("text")
            if isinstance(t, str) and t and "buttonType" not in o: out.append((where, t))
            for k, v in o.items():
                if k == "text": continue
                walk(v, where)
        elif isinstance(o, list):
            for v in o: walk(v, where)
    for s in tpl.get("sections", []): walk(s["components"], s["name"])
    walk(tpl.get("contactInfo", {}).get("richText"), "contact")
    return out


def _links(tpl):
    out = []
    def walk(o, where):
        if isinstance(o, dict):
            if "buttonType" in o and isinstance(o.get("link"), str): out.append((where, o["link"]))
            if o.get("is_custom_link") and isinstance(o.get("custom_link"), str): out.append((where, o["custom_link"]))
            for v in o.values(): walk(v, where)
        elif isinstance(o, list):
            for v in o: walk(v, where)
        elif isinstance(o, str):
            for m in re.finditer(r'href="([^"]*)"', o): out.append((where, html.unescape(m.group(1))))
    for s in tpl.get("sections", []): walk(s["components"], s["name"])
    walk(tpl.get("header_cta_buttons"), "header"); walk(tpl.get("contactInfo"), "contact")
    return out


def _images(tpl):
    urls = {}
    def walk(o, where):
        if isinstance(o, dict):
            if o.get("type") == "image" and o.get("url"): urls.setdefault(o["url"], where)
            for k in ("thumbnail_url", "videoPoster", "thumbnail"):
                if o.get("is_dynamic_media") is False and isinstance(o.get(k), str) and o[k].startswith("http"): urls.setdefault(o[k], where)
            for v in o.values(): walk(v, where)
        elif isinstance(o, list):
            for v in o: walk(v, where)
    for s in tpl.get("sections", []): walk(s["components"], s["name"])
    return urls


def run(*, tpl, h, plan, profile, pvalid, mapping, assets, others, flow, rights, check):
    """rights: ASSET_RIGHTS_POLICY (require|warn); check: ASSET_CHECK_POLICY (enforce|warn|off)."""
    checks, crit, review, warn, not_run = [], [], [], [], []

    def add(level, cid, name, status, detail="", findings=()):
        checks.append({"id": cid, "level": level, "name": name, "status": status, "detail": detail})
        for f in findings: (crit if status == "fail" else review if status == "review" else warn).append({**f, "check": cid})

    F = lambda code, cat, msg, fields=(): {"code": code, "category": cat, "message": msg, "fields": list(fields)}

    # ---------------- pre-render ----------------
    c = pvalid.get("critical", [])
    add("pre-render", "required_hotel_fields", "Required hotel fields are present and valid", "fail" if c else "pass", "; ".join(x["message"] for x in c), [F(x["code"], x["category"], x["message"], x["fields"]) for x in c])
    pw = [x for x in pvalid.get("warnings", []) if x["category"] in ("UNVERIFIED_CONTENT", "SOURCE_UNVERIFIED")]
    add("pre-render", "source_and_confidence", "Official source confirmed; required fields traceable and confident", "review" if pw else "pass", "; ".join(x["message"] for x in pw), [F(x["code"], x["category"], x["message"], x["fields"]) for x in pw])
    cf = [x for x in pvalid.get("warnings", []) if x["category"] == "CONFLICTING_DATA"]
    add("pre-render", "no_conflicting_values", "No conflicting or implausible values remain", "review" if cf else "pass", "; ".join(x["message"] for x in cf), [F(x["code"], x["category"], x["message"], x["fields"]) for x in cf])
    me = mapping.get("errors", [])
    add("pre-render", "merge_bindings_resolve", "Every merge field and template binding resolves", "fail" if me else "pass", "; ".join(x["message"] for x in me), [F(x["code"], x["category"], x["message"], x["fields"]) for x in me])
    mw = [x for x in mapping.get("warnings", []) if x.get("review")]
    add("pre-render", "mapped_fields_confident", "Required mapped fields have enough confidence", "review" if mw else "pass", "; ".join(x["message"] for x in mw), [F(x["code"], x["category"], x["message"], x["fields"]) for x in mw])
    keys = [s["key"] for s in plan["sections"]]
    miss = [k for k in plan.get("required_sections", []) if k not in keys]
    bl = plan.get("blocking", [])
    add("pre-render", "required_sections_selected", "Required sections are selected and the plan has no blockers", "fail" if miss or bl else "pass",
        "; ".join([f"required section missing: {k}" for k in miss] + [b["message"] for b in bl]),
        [F("REQUIRED_SECTION_MISSING", "REQUIRED_INFO_MISSING", f"required section '{k}' is not in the plan", [k]) for k in miss] + [F(b["code"], b["category"], b["message"], b.get("fields", [])) for b in bl])
    um = plan.get("unmet_requests", [])
    add("pre-render", "customization_met", "Everything the customization asked for was delivered", "review" if um else "pass", "; ".join(f"{u['request']}: {u['reason']}" for u in um),
        [F("REQUEST_UNMET", "UNSUPPORTED_TEMPLATE", f"{u['request']}: {u['reason']}") for u in um])
    # hotel-owned text must not carry prices: commercial values come from an approved opportunity (§7.3)
    money = []
    for where, t in _texts(tpl):
        plain = re.sub(r"\$\{[^}]*\}", " ", re.sub(r"<[^>]+>", " ", html.unescape(t)))
        m = MONEY.search(plain)
        if m: money.append(f"{where}: '{m.group(0).strip()}'")
    add("pre-render", "no_unapproved_commercial_text", "No prices or rates appear in hotel-owned text", "review" if money else "pass", "; ".join(money[:5]),
        [F("COMMERCIAL_TEXT", "COMMERCIAL_APPROVAL", f"hotel text contains commercial wording that needs approval ({x})") for x in money[:5]])
    used = {u: w for u, w in _images(tpl).items() if "hippovideo." not in u}      # images already uploaded to Hippo are Hippo's to serve
    unapproved = [a for a in profile.get("assets", []) if a.get("usage_rights_status") != "approved" and (a.get("asset_type") == "logo" or a.get("source_url") in used)]
    if unapproved and rights == "require":
        add("pre-render", "asset_rights", "Usage rights of the logo and photos are approved", "review", f"{len(unapproved)} asset(s) have unverified usage rights",
            [F("ASSET_RIGHTS_UNVERIFIED", "USAGE_RIGHTS_UNCLEAR", f"usage rights are not confirmed for {len(unapproved)} logo/photo asset(s): a hotel admin must approve them (customization asset_rights) or a reviewer must approve this job")])
    else:
        add("pre-render", "asset_rights", "Usage rights of the logo and photos are approved", "pass", "approved" if not unapproved else f"{len(unapproved)} unverified asset(s), allowed by ASSET_RIGHTS_POLICY=warn")
        if unapproved: warn.append({"code": "ASSET_RIGHTS_UNVERIFIED", "category": "USAGE_RIGHTS_UNCLEAR", "message": f"{len(unapproved)} asset(s) have unverified usage rights", "fields": [], "check": "asset_rights"})
    blob = " ".join(re.sub(r"<[^>]+>", " ", html.unescape(t)) for _, t in _texts(tpl)).lower()
    own = (profile.get("property_name") or "").lower()
    leak = [n for _, n in others if len(n) >= 8 and n.lower() not in own and own not in n.lower() and n.lower() in blob]
    add("pre-render", "no_cross_hotel_leakage", "No other hotel's name appears in this deal room", "fail" if leak else "pass", ", ".join(leak),
        [F("CROSS_HOTEL_LEAK", "CONFLICTING_DATA", f"text from another hotel ('{n}') appears in this deal room") for n in leak])

    # ---------------- post-render (structure of what will be saved) ----------------
    got = [s["name"] for s in tpl.get("sections", [])]
    want = [html.unescape(s["title"]) for s in plan["sections"]]
    ids = [s["id"] for s in tpl.get("sections", [])]
    ok_order = ids == [SECTION_ID_BY_KEY[k] for k in keys]
    add("post-render", "sections_match_plan", "Required sections appear, in the planned order", "pass" if ok_order else "fail", "" if ok_order else f"planned {keys}, built {got}",
        [] if ok_order else [F("PLAN_MISMATCH", "RENDER_FAILURE", f"built sections {got} do not match the plan {want}")])
    s = html.unescape(json.dumps(tpl, ensure_ascii=False))
    broken = re.findall(r"\$\{[^}]*(?=\$\{)|\$\{[^}]*$", s)
    add("post-render", "no_unresolved_tokens", "No malformed or unknown merge-field tokens", "fail" if broken or any(e["code"] == "UNKNOWN_MERGE_FIELD" for e in me) else "pass", "; ".join(broken[:3]),
        [F("MALFORMED_TOKEN", "MERGE_FIELD_UNMAPPED", f"malformed merge-field token near '{b[:40]}'") for b in broken[:3]])
    blank = []
    for sec in tpl.get("sections", []):
        for comp in sec["components"]:
            if comp["type"] == "Text" and not re.sub(r"<[^>]+>|\s|&nbsp;", "", comp["data"].get("text", "")): blank.append(f"{sec['name']}: empty text block")
            if comp["type"] == "TextCard":
                for i, cd in enumerate(comp["data"]["cards"], 1):
                    if not re.sub(r"<[^>]+>|\s|&nbsp;", "", cd.get("text", "")): blank.append(f"{sec['name']}: empty card {i}")
            if comp["type"] == "Carousel" and not comp["data"]["assets"]: blank.append(f"{sec['name']}: empty carousel")
    add("post-render", "no_blank_blocks", "No required block is blank", "fail" if blank else "pass", "; ".join(blank[:5]), [F("BLANK_BLOCK", "RENDER_FAILURE", b) for b in blank[:5]])
    if check == "enforce":
        miss_img = [u for u in used if not (assets.get(u) or {}).get("ok")]
        add("post-render", "images_resolve", "Every image in the deal room was fetched and is a valid image", "fail" if miss_img else "pass", f"{len(used) - len(miss_img)}/{len(used)} images verified",
            [F("IMAGE_UNVERIFIED", "ASSET_MISSING", f"image not verified: {u[:90]}", [u]) for u in miss_img[:5]])
    else:
        miss_img = [u for u in used if not (assets.get(u) or {}).get("ok")] if check == "warn" else list(used)
        add("post-render", "images_resolve", "Every image in the deal room was fetched and is a valid image", "review" if miss_img else "pass" if used else "na", f"ASSET_CHECK_POLICY={check}: {len(miss_img)} of {len(used)} image(s) not verified",
            [F("IMAGES_NOT_VERIFIED", "ASSET_MISSING", f"{len(miss_img)} image(s) were not verified (ASSET_CHECK_POLICY={check})")] if miss_img else [])
    bad_links = [f"{w}: {u[:60]}" for w, u in _links(tpl) if not SAFE_HREF.match(u.strip())]
    add("post-render", "links_safe", "All links use safe destinations", "fail" if bad_links else "pass", "; ".join(bad_links[:5]), [F("UNSAFE_LINK", "RENDER_FAILURE", f"unsafe link ({b})") for b in bad_links[:5]])
    cta_ids = plan["flags"]["cta_buttons"]; hb = tpl.get("header_cta_buttons", [])
    wrong = []
    if len(hb) != len(cta_ids): wrong.append(f"header has {len(hb)} buttons, plan wants {len(cta_ids)}")
    else:
        for b, cid in zip(hb, cta_ids):
            link, btype = CTA_EXPECT[cid]
            if link is not None and b.get("link") != link: wrong.append(f"{cid}: link is '{b.get('link')}'")
            if btype and b.get("buttonType") != btype: wrong.append(f"{cid}: type is '{b.get('buttonType')}'")
    add("post-render", "cta_targets", "CTA buttons point where the plan says", "fail" if wrong else "pass", "; ".join(wrong), [F("CTA_MISMATCH", "RENDER_FAILURE", w) for w in wrong])
    wide = []
    for sec in tpl.get("sections", []):
        for comp in sec["components"]:
            if comp["type"] == "Text" and "<table" in comp["data"].get("text", ""):
                cols = comp["data"]["text"].count("<th ")
                if cols > WIDE_TABLE: wide.append(f"{sec['name']}: table with {cols} columns")
    add("post-render", "layout_risk", "No table is likely to overflow a phone screen", "review" if wide else "pass", "; ".join(wide), [F("WIDE_TABLE", "RENDER_FAILURE", f"{w}: check it on mobile") for w in wide])
    not_run += [{"check": "mobile_and_desktop_layout", "reason": "needs a rendered Hippo page; the agent has no preview URL to open"},
                {"check": "pdf_export", "reason": "no Hippo PDF export API is configured"}, {"check": "share_link", "reason": "no Hippo share-link API is configured"}]
    for p in plan.get("placeholders", []):
        warn.append({"code": "PLACEHOLDER_COPY", "category": "UNVERIFIED_CONTENT", "message": f"'{p}' uses generic wording because no sourced text was found", "fields": [p], "check": "placeholders"})
    outcome = "FAIL" if crit else "REVIEW" if review else "PASS"
    return {"outcome": outcome, "checks": checks, "critical": crit, "review": review, "warnings": warn, "not_run": not_run,
            "counts": {"pass": sum(1 for x in checks if x["status"] == "pass"), "review": sum(1 for x in checks if x["status"] == "review"), "fail": sum(1 for x in checks if x["status"] == "fail")}}
