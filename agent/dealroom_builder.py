"""PDF-inspired Hippo Video deal-room builder (Email flow, new editor / template_type 143)  -- v2

Usage:  python dealroom_builder.py hotels/<hotel>.json [--page 1562033]
        (default --page auto: the loader targets whichever deal-room editor tab it is run in)
Output: out/<slug>/template.json            exactly the 6 keys the editor PATCHes to /api/video_page/pages
        out/<slug>/load_<slug>.js           browser-console loader (backup -> confirm -> save, __hvRestore() to undo)
        out/<slug>/report.txt               validation report

Formats emitted are copied from what the Hippo editor itself saves (captured Oct 2026):
  TextCard / Text / HeroVideo / Tags / CtaButton / Carousel (uploaded image asset, dynamic media slot)
  OneColumn > Video (video library), Video with is_embed_link (Google Maps), multiple_logo_top (url / uploaded).
Merge fields come ONLY from hippo_fields.EMAIL_TOKENS. Hotel facts/media come ONLY from the hotel config.
"""
import json, random, re, copy, html as html_lib, sys, os, argparse, urllib.parse
from hippo_fields import EMAIL_TOKENS

ICON = "https://static-assets.hippovideo.ai/deal-room/{}.png"
ICONS = {"aeroplane", "house", "money", "shield", "computer", "wifi", "music", "video", "location", "parking", "report", "star"}
SAVE_KEYS = ["multiple_logo_top", "sections", "sideNav", "header_cta_buttons", "settings", "contactInfo"]
WHITE, INK = "#FFFFFF", "#1A1A1A"
REQUIRED = ["slug", "name", "location_tag", "address", "brand", "logos", "media", "slots", "map", "overview", "fits",
            "rooms_intro", "meeting_intro", "venues", "dining_intro", "av_support", "location_intro", "location_cards", "amenities_line"]
# With a section plan only what the planned sections cannot do without is required; the prose intros are optional
# (an intro with nothing sourced behind it is left out rather than faked: PDF §8.3).
REQUIRED_GLOBAL = ["slug", "name", "location_tag", "address", "brand", "logos", "media", "slots", "map", "overview"]
REQUIRED_BY_SECTION = {"fits": ["fits"], "location": ["location_intro", "location_cards"]}


def required_for(section_keys):
    return REQUIRED_GLOBAL + [k for s in section_keys for k in REQUIRED_BY_SECTION.get(s, [])]
# Section ids kept stable across rebuilds so editor comments stay attached
SECTION_IDS = {"Overview": "section_u62zhL88", "Important Details": "section_ID5XNXDd", "Why This Hotel Fits": "section_14c03f72ff02",
               "Guest Rooms": "section_4a708ecd8dda", "Pricing Summary": "section_iIWtiFcQ", "Concessions & Value Adds": "section_dec0a81bbb3b",
               "Meeting Space": "section_00f53ffe7df4", "Dining": "section_UGFSOfoc", "AV & Technology": "section_70fb5658eb6a",
               "Location & Destination": "section_9c0b417f309d", "Amenities & Fees": "section_Ulm3vepC",
               "Virtual Tour & Gallery": "section_M5wPOto9", "Proposal PDF": "section_kbRd69pl", "Next Steps": "section_U4ncGymv"}
SECTION_ID_BY_KEY = {"overview": SECTION_IDS["Overview"], "fits": SECTION_IDS["Why This Hotel Fits"], "rooms": SECTION_IDS["Guest Rooms"],
                     "concessions": SECTION_IDS["Concessions & Value Adds"], "meeting": SECTION_IDS["Meeting Space"], "pricing": SECTION_IDS["Pricing Summary"],
                     "dining": SECTION_IDS["Dining"], "amenities": SECTION_IDS["Amenities & Fees"], "av": SECTION_IDS["AV & Technology"],
                     "location": SECTION_IDS["Location & Destination"], "gallery": SECTION_IDS["Virtual Tour & Gallery"], "pdf": SECTION_IDS["Proposal PDF"],
                     "next_steps": SECTION_IDS["Next Steps"]}
DEFAULT_ORDER = ["overview", "fits", "rooms", "concessions", "meeting", "pricing", "dining", "amenities", "av", "location", "gallery", "pdf", "next_steps"]
CTA_ORDER = ["schedule_site_visit", "request_revision", "download_pdf"]

# ---------------- colour / contrast (WCAG 2.1) ----------------
def _lum(h):
    h = h.lstrip('#'); r, g, b = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)

def contrast(a, b):
    x, y = sorted([_lum(a), _lum(b)], reverse=True); return (x + 0.05) / (y + 0.05)

def _mix(h, target, t):
    h = h.lstrip('#'); tt = target.lstrip('#')
    c = [round(int(h[i:i + 2], 16) * (1 - t) + int(tt[i:i + 2], 16) * t) for i in (0, 2, 4)]
    return "#%02X%02X%02X" % tuple(c)

def readable(fg, bg, need=4.5):
    """Nudge fg towards black or white (whichever direction helps) until it reads on bg."""
    if contrast(fg, bg) >= need: return fg.upper()
    toward = "#000000" if _lum(bg) > 0.18 else "#FFFFFF"
    for i in range(1, 101):
        c = _mix(fg, toward, i / 100)
        if contrast(c, bg) >= need: return c
    return toward

def bg_for_white(bg, need=4.5):
    """Darken a background until white text reads on it."""
    if contrast(WHITE, bg) >= need: return bg.upper()
    for i in range(1, 101):
        c = _mix(bg, "#000000", i / 100)
        if contrast(WHITE, c) >= need: return c
    return "#000000"


def on(bg, dark_text):
    """Best readable text colour ON an exact brand background (brand colour is never altered if avoidable)."""
    if contrast(WHITE, bg) >= 4.5: return WHITE
    if contrast(dark_text, bg) >= 4.5: return dark_text.upper()
    return None


def palette(b):
    P = {"primary": b["primary"].upper(), "dark": b["dark"].upper(), "tint": b["tint"].upper(), "accent": b["accent"].upper()}
    txt = (b.get("text") or "#1A1A1A").upper()
    P["ink"] = txt if contrast(txt, P["tint"]) >= 4.5 and contrast(txt, WHITE) >= 4.5 else readable(txt, P["tint"])   # site body/heading text colour
    # filled light cards: the site's light colour if it has visible colour, otherwise a soft tint of the brand colour
    P["card_light"] = P["tint"] if _sat(P["tint"]) >= 0.15 else _mix(P["primary"], WHITE, 0.84)
    P["card_dark"] = bg_for_white(P["dark"])
    for key, src in (("card_primary", "primary"), ("btn", "primary")):
        t = on(P[src], P["card_dark"])
        P[key], P[key + "_text"] = (P[src], t) if t else (bg_for_white(P[src]), WHITE)
    P["label_light"] = readable(P["primary"], P["card_light"])    # small caps labels on light cards
    P["label_white_bg"] = readable(P["primary"], WHITE)
    P["label_dark"] = readable(P["primary"] if contrast(P["primary"], P["card_dark"]) >= 4.5 else P["tint"], P["card_dark"])  # labels on hero / dark cards
    t = on(P["accent"], P["card_dark"])
    P["accent_btn"], P["accent_btn_text"] = (P["accent"], t) if t else (bg_for_white(P["accent"]), WHITE)
    P["heading"] = readable(txt if hsl_l(txt) < 0.45 else P["dark"], WHITE)
    # Hippo paints the header bar + sidebar in settings.primaryColor with WHITE text -> it must read on white
    P["chrome"] = P["primary"] if contrast(WHITE, P["primary"]) >= 4.5 else P["card_dark"]
    return P


def _sat(h):
    h = h.lstrip('#'); r, g, b = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    mx, mn = max(r, g, b), min(r, g, b); l = (mx + mn) / 2
    return 0 if mx == mn else (mx - mn) / (1 - abs(2 * l - 1))


def hsl_l(h):
    h = h.lstrip('#'); r, g, b = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    return (max(r, g, b) + min(r, g, b)) / 2


def _hero_media_url(h):
    media = h.get("media") or {}
    slots = h.get("slots") or {}
    ordered = []
    for slot in ("gallery", "fits", "rooms", "meeting", "dining", "amenities"):
        ordered.extend(slots.get(slot, []))
    for key in ordered:
        item = media.get(key)
        if isinstance(item, dict) and item.get("source_url"):
            return item["source_url"]
    for item in media.values():
        if isinstance(item, dict) and item.get("source_url"):
            return item["source_url"]
    return ""


def build(h, plan=None):
    """(template, contrast checks, palette). With a section plan only the planned sections are rendered, in plan order."""
    tpl, checks, P, _ = build_full(h, plan)
    return tpl, checks, P


def build_full(h, plan=None):
    """Like build(), plus the list of hotel-field -> template-slot bindings that were applied (for the mapping report)."""
    keys = [s["key"] for s in plan["sections"]] if plan else list(DEFAULT_ORDER)
    flags = (plan or {}).get("flags") or {}
    want = lambda k: k in keys
    cta_ids = flags.get("cta_buttons") or CTA_ORDER
    video_slot, featured, gal_images = flags.get("tour_video_slot", True), flags.get("featured_video", True), flags.get("gallery_images", True)
    BIND = []
    def bind(section, slot, key, detail=None, required=True):
        BIND.append({"section": section, "slot": slot, "config_key": key, "detail": detail, "required": required})
    random.seed(h["slug"])
    P = palette(h["brand"])
    F = h.get("fonts") or {}
    FH = (F.get("heading") or {}).get("stack", "'Open Sans', Arial, sans-serif")   # headings / big values (site heading font or closest web-safe match)
    FB = (F.get("body") or {}).get("stack", "'Open Sans', Arial, sans-serif")      # body text / labels
    BTN_FONT = (F.get("body") or {}).get("button_font", "Arial")
    HSTYLE = ""
    fh = F.get("heading") or {}
    if fh.get("transform") and fh["transform"] != "none": HSTYLE += f" text-transform: {fh['transform']};"
    if fh.get("letter_spacing") and fh["letter_spacing"] != "normal": HSTYLE += f" letter-spacing: {fh['letter_spacing']};"
    INK = P["ink"]
    CHECKS = []   # (description, fg, bg) pairs the validator re-checks
    hero_src = _hero_media_url(h) or "/img/without_img.jpg"
    
    def rid(prefix, n=8):
        return f"{prefix}_" + "".join(random.choice("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789") for _ in range(n))

    def mf(token):
        assert token in EMAIL_TOKENS, f"not an Email-flow merge field: {token}"
        return f'<span class="hv-mf-chip" data-merge-field="{token}">{token}</span>'

    # ---- card building blocks (every card gets an explicit fill colour) ----
    def card(kind, lbl, value_html, sub_html="", value_size="1.05rem"):
        bg = {"light": P["card_light"], "primary": P["card_primary"], "dark": P["card_dark"]}[kind]
        lc = {"light": P["label_light"], "primary": P["card_primary_text"], "dark": P["label_dark"]}[kind]
        vc = {"light": INK, "primary": P["card_primary_text"], "dark": WHITE}[kind]
        CHECKS.extend([(f"{kind} card label '{lbl}'", lc, bg), (f"{kind} card value '{lbl}'", vc, bg)])
        t = f'<p><strong style="font-family: {FB}; font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.06em; color: {lc};">{lbl}</strong></p>'
        if value_html:
            t += f'\n<p style="line-height: 1.3;"><strong style="font-family: {FB}; font-size: {value_size}; color: {vc}; word-break: normal; overflow-wrap: break-word;">{value_html}</strong></p>'
        if sub_html:
            t += f'\n<p><span style="font-family: {FB}; font-size: 13px; color: {vc};">{sub_html}</span></p>'
        return {"text": t, "color": bg}

    def icon_card(icon, title, body):
        assert icon in ICONS, f"unknown icon {icon}"
        CHECKS.append((f"icon card '{title}'", INK, P["card_light"]))
        return {"text": (f'<p><img src="{ICON.format(icon)}" alt="" width="48" height="48"></p>\n'
                         f'<p><span style="font-family: {FH}; font-size: 18px; color: {INK};{HSTYLE}"><strong>{title}</strong></span></p>\n'
                         f'<div class="feature"><span style="font-family: {FB}; font-size: 14px; color: {INK};">{body}</span></div>'),
                "color": P["card_light"]}

    def cards(lst, section_bg=WHITE, cid=None):
        return {"id": cid or rid("TextCard"), "type": "TextCard", "name": "New TextCard",
                "data": {"cards": lst, "backgroundColor": section_bg}, "spacingTop": 1, "spacingBottom": 1}

    def text(h_):
        return {"id": rid("Text"), "type": "Text", "name": "New Text",
                "data": {"text": h_, "placeholder": "Start writing or paste text to create your content"}, "spacingTop": 1, "spacingBottom": 1}

    def heading(title, sub=None):
        CHECKS.append((f"heading '{title}'", P["heading"], WHITE))
        s = f'<p class="p1"><span style="font-family: {FH}; font-size: 26px; color: {P["heading"]};{HSTYLE}"><strong>{title}</strong></span></p>'
        if sub:
            s += f'\n<div class="desc"><p class="p1"><span style="font-family: {FB}; font-size: 15px; color: {INK};">{sub}</span></p></div>'
        return text(s)

    def small(h_):
        return text(f'<p><span style="font-family: {FB}; font-size: 14px; color: {INK};">{h_}</span></p>')

    def table_mf(t):
        return text(f'<p><span style="font-size: 16px; color: black; background: white;">{mf(t)}</span></p>')

    def button(txt, link="#", btype="default", color=None, text_color=WHITE, size="14", font=None, chat="drift", **extra):
        if color is None: color, text_color = P["btn"], P["btn_text"]
        font = font or BTN_FONT
        CHECKS.append((f"button '{re.sub('<[^>]+>', '', txt)}'", text_color, color))
        b = {"text": txt, "link": link, "textColor": text_color, "color": color, "fontName": font, "fontSize": size, "corner": "pill",
             "buttonType": btype, "linkTarget": "_blank", "chat_type": chat, "chat_id": None, "form_type": 6, "form_id": None,
             "portal_id": None, "embedCode": None, "meeting_type": "calendly", "meetingLink": "", "size": 24}
        b.update(extra); return b

    # ---- media ----
    def image_asset(key, slot_name):
        m = h["media"][key]; hp = m.get("hippo") or {}
        if hp.get("asset_id") and hp.get("token"):
            tok = hp["token"]; th = f"https://www.hippovideo.io/video/thumbnail/{tok}"
            return {"id": str(hp["asset_id"]), "asset_id": int(hp["asset_id"]), "title": hp.get("title") or key, "type": "image",
                    "url": th, "videourl": "", "share_url": f"https://www.hippovideo.io/video/play/{tok}", "preview_url": th,
                    "document_url": "", "thumbnail_url": th, "is_document": False, "is_image": True, "is_custom_link": False,
                    "is_embed_link": False, "token": tok, "file_ext": "", "is_dynamic_media": False, "dynamic_media_type": None,
                    "merge_field": None, "is_video_template": False, "badgeLabel": "Image"}
        src = m["source_url"]
        return {"id": re.sub(r'[^a-z0-9]+', '_', slot_name.lower()).strip('_'), "asset_id": None, "title": slot_name, "type": "image",
                "url": src, "videourl": "", "share_url": "", "preview_url": src, "document_url": "", "thumbnail_url": "/img/without_img.jpg",
                "is_document": False, "is_image": True, "is_custom_link": False, "is_embed_link": False, "file_ext": "",
                "is_dynamic_media": True, "dynamic_media_type": None, "merge_field": slot_name, "is_video_template": False,
                "dynamic_media_description": "", "badgeLabel": "Image"}

    def dyn_named(name, kind, aid):
        is_doc = kind == "document"
        return {"id": aid, "asset_id": None, "title": name, "type": kind, "url": "", "videourl": "", "share_url": "", "preview_url": "",
                "document_url": "", "thumbnail_url": "https://hsv-static-files.s3.amazonaws.com/static/images/document_thumbnail.png?is_play=false" if is_doc else "/img/without_video.jpg",
                "is_document": is_doc, "is_image": False, "is_custom_link": False, "is_embed_link": False, "file_ext": "",
                "is_dynamic_media": True, "dynamic_media_type": None, "merge_field": name, "is_video_template": False,
                "dynamic_media_description": "", "badgeLabel": "Document" if is_doc else "Video"}

    def carousel(assets, cid=None):
        return {"id": cid or rid("Carousel"), "type": "Carousel", "name": "New Carousel", "data": {"assets": assets}, "spacingTop": 1, "spacingBottom": 1}

    def slot_carousel(slot, label):
        keys = h["slots"][slot]
        return carousel([image_asset(k, f"{label} Image {i}") for i, k in enumerate(keys, 1)])

    def map_embed():
        url = "https://www.google.com/maps?q=" + urllib.parse.quote_plus(h["map"]["query"]) + "&output=embed"
        return {"id": rid("Video"), "type": "Video", "name": "New Video", "spacingTop": 1, "spacingBottom": 1, "data": {
            "videoScale": 80, "id": None, "asset_id": None, "name": "", "title": "", "is_dynamic_media": False, "dynamic_media_type": None,
            "merge_field": None, "dynamic_media_title": None, "is_image": None, "is_document": None, "is_embed_link": True,
            "is_custom_link": True, "custom_link": url, "is_video_template": False, "upload_type": "videoLibrary",
            "thumbnail_url": "/img/without_video.jpg", "thumbnail": "/img/without_video.jpg", "videoPoster": "/img/without_video.jpg",
            "preview_url": url, "document_url": "", "doc_url": "", "share_url": url, "url": url, "videoUrl": url}}

    def featured_video():
        fv = h.get("featured_video") or {}
        if not fv.get("hippo"): return []
        return [{"id": rid("OneColumn"), "type": "OneColumn", "name": "New OneColumn", "spacingTop": 1, "spacingBottom": 1,
                 "data": {"components": [{"id": "onecol_Video_645", "type": "Video", "data": copy.deepcopy(fv["hippo"])}]}}]

    NEXT_BTN = {"schedule_site_visit": lambda: button("Schedule Site Visit", link="${User.Calendar Link}"),
                "request_revision": lambda: button("Request Proposal Revision", link="${User.Email}", btype="email"),
                "download_pdf": lambda: button("Download Proposal PDF")}
    HDR_BTN = {"schedule_site_visit": lambda: button("<b>Schedule Site Visit</b>", link="${User.Calendar Link}"),
               "request_revision": lambda: button("<b>Request Proposal Revision</b>", link="${User.Email}", btype="email"),
               "download_pdf": lambda: button("<b>Download Proposal PDF</b>")}

    S = []
    def section(key, name, comps, bg=None):
        plain = html_lib.unescape(name)
        sid = SECTION_ID_BY_KEY[key]
        if key == "dining": rid("section")          # keeps every later id identical to the output before section plans existed
        s = {"id": sid, "name": plain, "order": len(S) + 1, "components": comps, "comments_count": 0}
        if bg: s["backgroundColor"] = bg
        S.append(s)

    # ===== 1 Overview =====
    D = P["card_dark"]
    CHECKS.extend([("hero label", P["label_dark"], D), ("hero text", WHITE, D)])
    left = [
        {"id": "vcc_left_Text_315", "type": "Text", "data": {"text":
            f'<h3 style="line-height: 1;"><span style="font-family: {FB}; font-size: 15px; letter-spacing: 0.08em; color: {P["label_dark"]};"><strong>PROPOSAL OVERVIEW</strong></span></h3>\n'
            f'<p style="line-height: 1.2;"><span style="font-family: {FH}; font-size: 30px; color: {WHITE};"><strong>{mf("${Event Name}")}</strong></span><br>'
            f'<span style="font-family: {FH}; font-size: 20px; color: {WHITE};">at {mf("${Hotel Name}")}</span></p>'}},
        {"id": "vcc_left_Tags_378", "type": "Tags", "data": {"buttons": [
            button("${Total Attendees} Attendees", link="", size="12", chat=4),
            button("${Event Dates}", link="", size="12", chat=4),
            button(h["location_tag"], link="", size="12", chat=4)]}},
        {"id": "vcc_left_Text_460", "type": "Text", "data": {"text":
            f'<p><span style="font-family: {FB}; font-size: 16px; color: {WHITE};"><strong>Dear {mf("${Planner Name}")},</strong></span></p>\n'
            f'<p><span style="font-family: {FB}; font-size: 16px; line-height: 1.5; color: {WHITE};">{mf("${Proposal Introduction}")}</span></p>\n'
            f'<p><span style="font-family: {FB}; font-size: 14px; line-height: 1.5; color: {readable("#DCD4C6", D)};">{h["overview"]}</span></p>'}},
        {"id": "vcc_left_CtaButton_492", "type": "CtaButton", "data": {"buttons": [
            button("<b>Download Proposal PDF</b>", color=P["accent_btn"], text_color=P["accent_btn_text"], size="16", chat=4, padding="12px 24px")] }},
    ]
    if "download_pdf" not in cta_ids: left = [c for c in left if c["type"] != "CtaButton"]
    bind("overview", "hero text", "overview"); bind("overview", "location tag", "location_tag"); bind("overview", "hero image", "media", hero_src)
    hero = {"id": "HeroVideo_FUwfCAZf", "type": "HeroVideo", "name": "New HeroVideo", "spacingTop": 1, "spacingBottom": 1, "data": {
        "leftComponents": left,
        "rightComponents": [{"id": "hero_video_right_fixed", "type": "Video", "data": {
            "videoUrl": "", "token": "", "asset_id": None, "thumbnail": hero_src, "thumbnail_url": hero_src,
            "videoPoster": hero_src, "is_dynamic_media": False, "dynamic_media_type": None, "merge_field": None, "dynamic_media_description": "", "videoScale": 100}}],
        "leftWidth": 60, "headerText": "", "headerTextHtml": "", "paragraphText": "", "paragraphTextHtml": "",
        "buttons": [button("Click to reply", link="#", btype="meeting", color="#000000", size="16", padding="12px 24px")],
        "videoUrl": "", "videoPoster": hero_src, "thumbnail": hero_src, "asset_id": None, "token": "", "is_dynamic_media": False, "dynamic_media_type": None,
        "merge_field": None, "is_document": None, "id": 26447145, "name": "${Event Name} - Danielle", "title": "${Event Name} - Danielle",
        "upload_type": "videoLibrary", "thumbnail_url": hero_src,
        "backgroundColor": D, "is_video_template": False, "vccLeftComponents": copy.deepcopy(left), "dynamic_media_title": None}}
    section("overview", "Overview", [hero, cards([
        card("primary", "Group Room Rate", mf("${Guest Room Rate}")),
        card("primary", "Meeting Space", mf("${Meeting Room Rental Total}")),
        card("primary", "Food &amp; Beverage", mf("${Total F&B}")),
        card("primary", "AV &amp; Technology", mf("${AV Cost}")),
        card("primary", "Concessions Value", mf("${Total Concessions Value}")),
        card("dark", "Total Estimated Investment", mf("${Total Estimated Cost}")),
    ], section_bg=D, cid="TextCard_f0743OLb"), cards([
        card("light", "Group", mf("${Company Name}"), value_size="22px"),
        card("light", "Response Due", mf("${Response Due Date}")),
        card("light", "Decision Date", mf("${Decision Date}")),
        card("light", "Sales Contact", mf("${User.First Name}") + " " + mf("${User.Last Name}"), mf("${User.Title}")),
        card("light", "Contact Number", mf("${User.Phone Number}"), mf("${User.Email}")),
    ], cid="TextCard_tvBfnTXv")], bg=D)

    # ===== 3 Why this hotel fits =====
    if want("fits"):
        bind("fits", "icon cards", "fits"); bind("fits", "intro", "name"); bind("fits", "photo carousel", "slots.fits")
        section("fits", "Why This Hotel Fits", [heading("Why This Hotel Fits", f'Why {h["name"]} works for your event.'),
                                                cards([icon_card(f["icon"], f["title"], f["body"]) for f in h["fits"]]),
                                                slot_carousel("fits", "Hotel Fits")])
    def static_table(title, t):
        th = "".join(f'<th style="padding: 8px; text-align: left; font-family: {FB}; color: {INK};">{c}</th>' for c in t["columns"])
        tr = "".join("<tr>" + "".join(f'<td style="padding: 8px; border-top: 1px solid #e3e0d8; font-family: {FB}; color: {INK};">{c}</td>' for c in r) + "</tr>" for r in t["rows"])
        CHECKS.append((f"{title} table", INK, P["tint"]))
        return (f'<p><strong><span style="font-family: {FH}; font-size: 18px; color: {P["heading"]};{HSTYLE}">{title}</span></strong></p>\n'
                f'<table style="border-collapse: collapse; width: 100%; font-size: 14px;"><thead><tr style="background: {P["tint"]};">{th}</tr></thead><tbody>{tr}</tbody></table>'
                + (f'\n<p><span style="font-size: 12px; color: #595959;">{t["note"]}</span></p>' if t.get("note") else ""))
    rt = h.get("room_types") or {}

    # ===== 4 Guest rooms =====
    if want("rooms"):
        bind("rooms", "intro", "rooms_intro", required=False); bind("rooms", "room table", "room_types", required=False); bind("rooms", "photo carousel", "slots.rooms")
        section("rooms", "Guest Rooms", [heading("Guest Rooms", h.get("rooms_intro")), cards([
            card("light", "Group Rate", mf("${Guest Room Rate}"), "Breakfast included: " + mf("${Rates Include Breakfast}")),
            card("light", "Peak Rooms", mf("${Peak Rooms}"), "Total room nights: " + mf("${Total Room Nights}")),
            card("light", "Sleeping Room Cost", mf("${Total Sleeping Room Cost}"), "Tax included: " + mf("${Rates Include Tax}")),
            card("light", "Availability", mf("${Availability Status}"), "Needs met: " + mf("${Sleeping Room Needs Met}"))]),
            table_mf("${Table-Sleeping Room}"), small(mf("${Sleeping Room Info}"))] + ([text(static_table("Room types", rt))] if rt.get("rows") else []) + [slot_carousel("rooms", "Guest Room")])
    # ===== 5 Pricing =====
    if want("pricing"):
        section("pricing", "Pricing Summary", [heading("Pricing Summary", "Line-item visibility, transparent fees and a single estimate. All amounts in " + mf("${Currency}") + "."),
            table_mf("${Table-Pricing Details}"), cards([
                card("light", "Taxes", "", mf("${Applicable Taxes}")), card("light", "Additional Fees", "", mf("${Additional Fees}")),
                card("light", "Commission", mf("${Commission}")), card("dark", "Total Estimated Cost", mf("${Total Estimated Cost}"))])])
    # ===== 6 Concessions =====
    if want("concessions"):
        section("concessions", "Concessions &amp; Value Adds", [heading("Concessions &amp; Value Adds", "Every concession offered for this event, shown for internal approval and comparison."),
            text(f'<p><span style="font-size: 16px; color: black;">{mf("${Concessions}")}</span></p>'),
            cards([card("primary", "Total Concessions Value", mf("${Total Concessions Value}"), value_size="2rem")])])
    # ===== 7 Meeting space =====
    v = h.get("venues") or {}
    venue_html = static_table("Venue capacities", v) if v.get("rows") else ""
    if want("meeting"):
        bind("meeting", "intro", "meeting_intro", required=False); bind("meeting", "venue table", "venues", required=False); bind("meeting", "photo carousel", "slots.meeting")
        section("meeting", "Meeting Space", [heading("Meeting Space", h.get("meeting_intro")), cards([
            card("light", "Proposed Room", mf("${Meeting Room Name}"), "Setup: " + mf("${Meeting Room Setup}")),
            card("light", "Capacity", mf("${Meeting Room Capacity}"), "Starts " + mf("${Meeting Start Date}")),
            card("light", "Room Rental", mf("${Meeting Room Rental}"), "Total " + mf("${Meeting Room Rental Total}"))]),
            table_mf("${Table-Meeting Agenda}"), small(mf("${Meeting Room Info}"))] + ([text(venue_html)] if v.get("rows") else []) + [slot_carousel("meeting", "Meeting Space")])
    # ===== 8 Dining =====
    dt = h.get("dining_title", "Food &amp; Beverage")
    if want("dining"):
        bind("dining", "intro", "dining_intro", required=False); bind("dining", "photo carousel", "slots.dining")
        section("dining", dt, [heading(dt, h.get("dining_intro")), cards([
            card("light", "Avg Breakfast", mf("${Avg Breakfast Cost}")), card("light", "Avg Lunch", mf("${Avg Lunch Cost}")),
            card("light", "Avg Dinner", mf("${Avg Dinner Cost}")), card("light", "Avg Break", mf("${Avg Break Cost}")),
            card("light", "F&amp;B Minimum", mf("${F&B Minimum}"), "Service charge " + mf("${Service Charge}") + " · Tax " + mf("${F&B Applicable Tax}"))]),
            slot_carousel("dining", "Dining")])
    # ===== 9 AV =====
    if want("av"):
        bind("av", "onsite support", "av_support", required=False)
        section("av", "AV &amp; Technology", [heading("AV &amp; Technology", "Technology readiness for meetings, presentations and hybrid events."), cards([
            icon_card("computer", "AV Estimate", mf("${AV Cost}")), icon_card("wifi", "Meeting Room Internet", mf("${Meeting Room Internet}")),
            icon_card("video", "Your AV Requirements", mf("${AV Requirements}"))] + ([icon_card("music", "Onsite Support", h["av_support"])] if h.get("av_support") else []))])
    # ===== 10 Location + map =====
    if want("location"):
        bind("location", "intro", "location_intro"); bind("location", "cards", "location_cards"); bind("location", "map", "map")
        section("location", "Location &amp; Destination", [heading("Location &amp; Destination", h.get("location_intro")),
            cards([icon_card(c["icon"], c["title"], c["body"]) for c in h["location_cards"]]), map_embed()])
    # ===== 11 Amenities & fees =====
    if want("amenities"):
        bind("amenities", "intro", "amenities_line", required=False); bind("amenities", "photo carousel", "slots.amenities")
        section("amenities", "Amenities &amp; Fees", [heading("Amenities &amp; Fees", h.get("amenities_line")), cards([
            card("light", "Self Parking", mf("${Self Parking Fee}")), card("light", "Valet Parking", mf("${Valet Parking Fee}")),
            card("light", "Resort Fee", mf("${Resort Fee}")), card("light", "Guest Room Internet", mf("${Guest Room Internet}")),
            card("light", "Housekeeping", mf("${Housekeeping}")),
            card("light", "Last Renovated", mf("${Last Renovated}"), "Construction plans: " + mf("${Construction Plans}"))]),
            slot_carousel("amenities", "Amenities")])
    # ===== 12 Virtual tour & gallery =====
    if want("gallery"):
        gal = ([dyn_named("Virtual Tour Video", "video", "virtual_tour_video")] if video_slot else []) + \
              ([image_asset(k, f"Property Image {i}") for i, k in enumerate(h["slots"]["gallery"], 1)] if gal_images else [])
        bind("gallery", "photo carousel", "slots.gallery"); bind("gallery", "featured video", "featured_video", required=False)
        section("gallery", "Virtual Tour &amp; Gallery", [heading("Virtual Tour &amp; Gallery", "A walkthrough of the venue and property.")]
                + (featured_video() if featured else []) + [carousel(gal, cid="Carousel_GcPXYVuF")])
    # ===== 13 Proposal PDF =====
    if want("pdf"):
        section("pdf", "Proposal PDF", [heading("Proposal PDF", "Download the complete proposal and attachments for your approval process."),
            carousel([dyn_named("RFP Response PDF", "document", "rfp_response_pdf")], cid="Carousel_nWNYZMoW"), small(mf("${Proposal Additional Info}"))])
    # ===== 14 Next steps =====
    if want("next_steps"):
        section("next_steps", "Next Steps", [text(f'<p><strong><span style="font-size: 24px; color: {P["heading"]};">Next Steps</span></strong></p>'),
            {"id": "CtaButton_HmKHURLb", "type": "CtaButton", "name": "New CtaButton", "spacingTop": 1, "spacingBottom": 1,
             "data": {"buttons": [NEXT_BTN[c]() for c in cta_ids]}}])

    order = {SECTION_ID_BY_KEY[k]: index for index, k in enumerate(keys)}
    S.sort(key=lambda item: order.get(item["id"], len(order)))
    for index, item in enumerate(S, 1):
        item["order"] = index

    # ---- logos (dedupe) ----
    logos, seen = [], set()
    for i, lg in enumerate(h["logos"], 1):
        obj = copy.deepcopy(lg["hippo"]) if lg.get("hippo") else {"id": i, "company_logo_top": {"src": lg["url"], "url": lg["url"], "thumbnail_url": lg["url"],
              "link": "", "size": 51, "type": "url", "deleted": "false", "media_id": None, "asset_id": None, "logo_type": lg.get("logo_type", "your_logo"), "linkTarget": "_blank"}}
        k = (str(obj["id"]), obj["company_logo_top"]["src"])
        if k in seen: continue
        seen.add(k); logos.append(obj)

    tpl = {
        "multiple_logo_top": logos,
        "sections": S,
        "sideNav": [{"id": "group_1", "name": "Overview", "order": 1, "sections": [s["id"] for s in S], "isCollapsed": False}],
        "header_cta_buttons": [HDR_BTN[c]() for c in cta_ids],
        "settings": {"theme": "light", "primaryColor": P["chrome"], "secondaryColor": P["card_light"], "hideSidebar": False,
                     "cardView": True, "navMode": "sidebar", "menuColor": P["dark"]},
        "contactInfo": {
            "richText": (f'<p><strong><span style="font-size: 14px;">{h["name"]}</span></strong></p>\n'
                         f'<p class="p1"><span style="font-size: 12px;">{h["address"]}</span></p>\n'
                         '<p class="p1"><strong><span style="font-size: 12px;">${User.First Name} ${User.Last Name}</span></strong><br>'
                         '<span style="font-size: 12px;">${User.Title}</span><br><span style="font-size: 12px;">${User.Email}</span><br>'
                         '<span style="font-size: 12px;">${User.Phone Number}</span></p>'),
            "backgroundColor": P["tint"], "name": "${User.First Name}", "role": "${User.Title}",
            "email": "${User.Email}", "phone": "${User.Phone Number}", "heading": "YOUR CONTACT"},
    }
    bind("chrome", "header logos", "logos"); bind("chrome", "contact block", "name"); bind("chrome", "contact block address", "address")
    bind("chrome", "colours", "brand"); bind("chrome", "fonts", "fonts")
    return tpl, CHECKS, P, BIND


FOREIGN = ["Palm Springs", "Kotagiri", "Nilgiris", "Ooty", "Tamil Nadu", "Elmore", "Southlake", "Holiday Inn", "Bengaluru", "Racecourse",
           "Equus", "DoubleTree", "Introduction Text", "City and State", "OVERVIEWs", "HEADER 1", "lorem", "Section 1"]

def validate(tpl, h, checks):
    errs, warns = [], []
    s = json.dumps(tpl, ensure_ascii=False)
    toks = re.findall(r"\$\{[^}]*\}?", html_lib.unescape(s))
    errs += [f"invalid merge field {t}" for t in sorted(set(toks)) if t not in EMAIL_TOKENS]
    if "response." in s or "request." in s: errs.append("Cvent syntax leaked")
    own = json.dumps(h, ensure_ascii=False).lower()
    errs += [f"foreign/leftover text: {w}" for w in FOREIGN if w.lower() in s.lower() and w.lower() not in own]
    S = tpl["sections"]
    ids = [x["id"] for x in S] + [c["id"] for x in S for c in x["components"]]
    if len(ids) != len(set(ids)): errs.append("duplicate section/component ids")
    if tpl["sideNav"][0]["sections"] != [x["id"] for x in S]: errs.append("sideNav does not match sections")
    if [x["order"] for x in S] != list(range(1, len(S) + 1)): errs.append("section order not 1..n")
    if any(re.search(r"Section \d", x["name"]) or "&amp;" in x["name"] for x in S): errs.append("placeholder or escaped section name")
    if list(tpl) != SAVE_KEYS: errs.append("top-level keys differ from editor save payload")
    lid = [(str(l["id"]), l["company_logo_top"]["src"]) for l in tpl["multiple_logo_top"]]
    if len(lid) != len(set(lid)): errs.append("duplicate logos")
    for x in S:
        for c in x["components"]:
            if c["type"] == "TextCard":
                for i, cd in enumerate(c["data"]["cards"]):
                    if not cd.get("color"): errs.append(f"unfilled card in {x['name']} #{i+1}")
            if c["type"] == "Carousel":
                if not c["data"]["assets"]: errs.append(f"empty carousel in {x['name']}")
                for a in c["data"]["assets"]:
                    if a["type"] == "image" and not a["url"].startswith("https://"): errs.append(f"bad image url in {x['name']}")
                    if a["type"] == "image" and a["is_dynamic_media"]: warns.append(f"{x['name']}: '{a['merge_field']}' is a media merge field (image not uploaded to Hippo yet)")
            if c["type"] == "Video" and c["data"].get("is_embed_link"):
                u = c["data"]["custom_link"]
                if not u.startswith("https://www.google.com/maps") or re.search(r'[\s"<>]', u): errs.append(f"bad map url in {x['name']}")
    checks = checks + [("header & sidebar (settings.primaryColor) with white text", WHITE, tpl["settings"]["primaryColor"])]
    for d, fg, bg in checks:
        r = contrast(fg, bg)
        if r < 4.5: errs.append(f"low contrast {r:.2f}: {d} ({fg} on {bg})")
    return errs, warns, toks


def make_loader(tpl, page_id, label, flow="Email"):
    s = json.dumps(tpl, ensure_ascii=False, separators=(',', ':'))
    return r'''// Hippo deal room loader: %(label)s (%(flow)s flow) -> page %(pid)s
// Run in the browser Console while logged in on https://www.hippovideo.io/deal-room/edit/<page id>
(async () => {
  console.log("▶ Deal room loader started: %(label)s");
  if (location.hostname !== "www.hippovideo.io") { console.error("✖ Open the Hippo deal room editor tab first, then run this again."); return; }
  const m = location.pathname.match(/\/deal-room\/edit\/(\d+)/);
  const FIXED = "%(pid)s";
  if (!m) { console.error("✖ Open the deal room EDITOR tab (https://www.hippovideo.io/deal-room/edit/<id>), then run this again."); return; }
  const PAGE_ID = FIXED !== "auto" ? FIXED : m[1];
  if (m && m[1] !== PAGE_ID) { console.error("✖ This tab is page " + m[1] + " but the loader targets page " + PAGE_ID + ". Stopping."); return; }
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content;
  const headers = { "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8", "X-Requested-With": "XMLHttpRequest", "Accept": "application/json, text/javascript, */*; q=0.01" };
  if (csrf) headers["X-CSRF-Token"] = csrf;
  const cur = await fetch("/api/video_page/templates/" + PAGE_ID + "/editor?fromPreview=false", { credentials: "same-origin", headers: { "Accept": "application/json" } });
  if (!cur.ok) { console.error("✖ Backup failed, stopping. HTTP", cur.status); return; }
  const backup = await cur.json();
  if (!backup.template || !backup.template.sections) { console.error("✖ Backup looks wrong, stopping.", backup); return; }
  window.__hvBackup = backup;
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([JSON.stringify(backup, null, 2)], { type: "application/json" }));
  a.download = "deal-room-" + PAGE_ID + "-backup-" + Date.now() + ".json"; a.click();
  console.log("✔ Backup saved for page " + PAGE_ID + " (" + backup.template.sections.length + " sections).");
  if (!confirm('Replace "' + (backup.page_name || "page " + PAGE_ID) + '" (page ' + PAGE_ID + ", " + backup.template.sections.length + " sections) with the %(label)s deal room?\n\nA backup file has just been downloaded.")) { console.warn("Cancelled. Nothing was changed."); return; }
  window.__hvRestore = async () => {
    const t = window.__hvBackup.template, keep = {};
    ["multiple_logo_top","sections","sideNav","header_cta_buttons","settings","contactInfo"].forEach(k => { if (k in t) keep[k] = t[k]; });
    const r = await fetch("/api/video_page/pages", { method: "PATCH", credentials: "same-origin", headers, body: new URLSearchParams({ template: JSON.stringify(keep), page_id: PAGE_ID }) });
    console.log("Restore:", r.status, await r.text());
  };
  const TEMPLATE = %(tpl)s;
  // keep logos already uploaded to Hippo (external logo URLs are not shown by Hippo; uploaded ones are)
  const uploaded = l => /hippovideo\.io\/video\/thumbnail|assets\.hippovideo\.io/.test((l.company_logo_top || {}).src || "");
  const pageLogos = (backup.template.multiple_logo_top || []).filter(uploaded);
  if (!TEMPLATE.multiple_logo_top.some(uploaded) && pageLogos.length) {
    TEMPLATE.multiple_logo_top = pageLogos;
    console.log("✔ Kept the " + pageLogos.length + " logo(s) already uploaded on this page.");
  } else if (!TEMPLATE.multiple_logo_top.some(uploaded)) {
    console.warn("ℹ No uploaded logo on this page yet: upload the hotel logo once in the editor header (it will be kept on future loads).");
  }
  const res = await fetch("/api/video_page/pages", { method: "PATCH", credentials: "same-origin", headers,
    body: new URLSearchParams({ template: JSON.stringify(TEMPLATE), page_id: PAGE_ID }) });
  const txt = await res.text();
  console.log("Save:", res.status, txt);
  if (res.ok && /"success":\s*true/.test(txt)) console.log("✔ Done. Reload the editor to see the %(label)s deal room. If anything is off, run __hvRestore()");
  else console.error("✖ Save did not succeed. Nothing else was changed. Send this output to Claude.");
})();
''' % {"pid": page_id, "tpl": s, "label": label, "flow": flow}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("hotel"); ap.add_argument("--page", default="auto", help="Hippo page id, or 'auto' = the editor tab's page")
    ap.add_argument("--overrides", help="reviewed copy/choices merged over the scraped config (dicts merge, lists replace)")
    a = ap.parse_args()
    def merge(base, over):
        for k, v in over.items():
            if isinstance(v, dict) and isinstance(base.get(k), dict) and not k.startswith("_"): merge(base[k], v)
            else: base[k] = v
        return base
    raw = json.load(open(a.hotel))
    if a.overrides: raw = merge(raw, json.load(open(a.overrides)))
    h = {k: v for k, v in raw.items() if not k.startswith("_")}
    missing = [k for k in REQUIRED if not h.get(k)]
    if missing: sys.exit(f"Hotel config missing: {missing}")
    for slot, keys in h["slots"].items():
        if slot.startswith("_"): continue
        bad = [k for k in keys if k not in h["media"]]
        if bad: sys.exit(f"slot '{slot}' references unknown media {bad}")
    tpl, checks, P = build(h)
    errs, warns, toks = validate(tpl, h, checks)
    out = os.path.join("out", h["slug"]); os.makedirs(out, exist_ok=True)
    rep = [f"Hotel: {h['name']}", f"Sections: {len(tpl['sections'])}", f"Merge-field tokens: {len(toks)} ({len(set(toks))} unique)",
           f"Contrast checks: {len(checks)} pairs", "Palette: " + json.dumps(P), "", "ERRORS:"] + (errs or ["none"]) + ["", "WARNINGS:"] + (warns or ["none"])
    open(os.path.join(out, "report.txt"), "w").write("\n".join(rep))
    if errs:
        print("\n".join(rep)); sys.exit(1)
    json.dump(tpl, open(os.path.join(out, "template.json"), "w"), ensure_ascii=False, indent=2)
    suffix = "" if a.page == "auto" else f"_{a.page}"
    open(os.path.join(out, f"load_{h['slug']}{suffix}.js"), "w").write(make_loader(tpl, a.page, h["name"]))
    print("\n".join(rep[:5])); print(f"OK, 0 errors, {len(warns)} warnings -> {out}/")
