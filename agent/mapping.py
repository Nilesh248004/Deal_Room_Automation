"""Merge-field mapping report (PDF §8).

Two kinds of values end up in a deal room, and the report keeps them apart:

* static bindings: hotel facts the builder writes into the template (name, address, venue table, photos ...).
  Each one is traced to its profile evidence: source, provenance, confidence, approval, validation, fallback.
* dynamic merge fields: Hippo `${...}` tokens that Hippo fills per opportunity when the deal room is sent. These
  are never filled or guessed here (§8.3). Rates, fees, dates and concessions are "commercial/event" fields that
  need an approved opportunity or pricing source, and the report says so.

The report is derived from what dealroom_builder.build_full() actually bound and from the finished template, so
it cannot drift from what is rendered.
"""
import html, json, re

import dealroom_builder as B
from hippo_fields import DEFAULT_MERGE_FIELDS, EMAIL_TOKENS

CVENT_TOKENS = {cv for _, cv, _ in DEFAULT_MERGE_FIELDS}
LABEL_OF = {}
for _label, _cv, _em in DEFAULT_MERGE_FIELDS:
    LABEL_OF.setdefault(_em, _label); LABEL_OF.setdefault(_cv, _label)
SECTION_OF_ID = {v: k for k, v in B.SECTION_ID_BY_KEY.items()}

COMMERCIAL = re.compile(r"rate|cost|fee|tax|charge|commission|concession|pricing|price|minimum|total f&b|currency|housekeeping|internet", re.I)
EVENT = re.compile(r"^(event|check-in|check-out|alternate|date type|total attendees|rooms needed|total room|peak|meeting start|requested|av requirements|business|decision factors|company name|availability|sleeping room|meeting room|attachments|additional questions|last renovated|construction)", re.I)
CONTACT = re.compile(r"^(user\b|contact|planner|first name|last name|email|phone|title|country|city|state|address)", re.I)
SUPPLIER = {"commercial": "an approved opportunity or pricing source in Hippo", "event": "the opportunity / RFP in Hippo",
            "contact": "the sending user's Hippo profile or the recipient record", "proposal": "the proposal record in Hippo", "hotel": "the hotel record in Hippo"}

# static binding -> profile evidence keys to look at, best first
EVIDENCE = {
    "name": ["override.name", "property_name"], "address": ["override.address", "address"], "location_tag": ["override.location_tag", "city"],
    "overview": ["property_summary"], "fits": ["override.fits", "copy.fits"], "rooms_intro": ["override.rooms_intro", "copy.rooms_intro", "facts.rooms", "rooms"],
    "meeting_intro": ["override.meeting_intro", "copy.meeting_intro", "venues"], "dining_intro": ["override.dining_intro", "copy.dining_intro", "facts.outlets"],
    "av_support": ["override.av_support", "copy.av_support"], "location_intro": ["override.location_intro", "address"],
    "location_cards": ["override.location_cards", "copy.location_cards"], "amenities_line": ["override.amenities_line", "amenities", "copy.amenities_line"],
    "room_types": ["rooms"], "venues": ["venues"], "map": ["map"], "brand": ["brand.primary"], "fonts": ["font.body"],
}


def classify(token):
    label = LABEL_OF.get(token) or ""
    if not label: return "proposal"
    if label in ("Hotel Name", "Hotel Address", "Brand", "Chain", "Metro Area"): return "hotel"
    if COMMERCIAL.search(label): return "commercial"
    if CONTACT.match(label): return "contact"
    if EVENT.match(label): return "event"
    return "proposal"


def _tokens(obj):
    return re.findall(r"\$\{[^}]*\}", html.unescape(json.dumps(obj, ensure_ascii=False)))


def _value(h, key, detail):
    if key.startswith("slots."): return [(h.get("media", {}).get(k) or {}).get("source_url") for k in (h.get("slots") or {}).get(key[6:], [])]
    if key == "media": return detail
    if key == "map": return (h.get("map") or {}).get("query")
    return h.get(key)


def _kind(v):
    return "table" if isinstance(v, dict) and "rows" in v else "list" if isinstance(v, list) else "object" if isinstance(v, dict) else "text"


def build_mapping(h, plan, profile, tpl, flow, binds, assets=None):
    ev = {e["field_key"]: e for e in profile.get("evidence", [])}
    asset_by_url = {a.get("source_url"): a for a in profile.get("assets", [])}
    placeholders = {p.split(".")[0] for p in (plan or {}).get("placeholders", [])}
    errors, warnings, static = [], [], []
    for b in binds:
        key, req = b["config_key"], b["required"]
        val = _value(h, key, b["detail"])
        empty = val in (None, "", [], {}) or (isinstance(val, dict) and "rows" in val and not val["rows"]) or (isinstance(val, list) and not any(val))
        if key.startswith("slots.") or key in ("media", "logos"):
            urls = [u for u in (val if isinstance(val, list) else [val]) if u] if key != "logos" else [x.get("url") for x in h.get("logos", [])]
            rec = [a for u in urls for a in [asset_by_url.get(u)] if a]
            prov, conf, src, appr = "extracted", 0.6, None, ("approved" if rec and all(a.get("approval_status") == "approved" for a in rec) else "pending")
            if assets is not None and urls:
                good = [u for u in urls if (assets.get(u) or {}).get("ok")]
                conf = 0.9 if len(good) == len(urls) else 0.5 if good else 0.2
                prov = "officially_verified" if good and len(good) == len(urls) else "extracted"
            src = (rec[0].get("source_page") if rec else None) or (urls[0] if urls else None)
            sf = "assets"
        else:
            e = next((ev[k] for k in EVIDENCE.get(key, [key]) if k in ev), None)
            sf = next((k for k in EVIDENCE.get(key, [key]) if k in ev), key)
            prov, conf, src, appr = (e["provenance"], e["confidence_score"], e["source_url"], e["approval_status"]) if e else ("unknown", 0.0, None, "pending")
        status, note, fallback = "ok", "", "block" if req else "omit_block"
        if empty and req:
            status, note = "error", "required value is empty"
            errors.append({"code": "REQUIRED_BINDING_EMPTY", "category": "MERGE_FIELD_UNMAPPED", "message": f"{b['section']}/{b['slot']}: required field '{key}' has no value", "fields": [key]})
        elif empty: status, note = "omitted", "no value: the block is left out, nothing is made up"
        elif key in placeholders: status, note = "warn", "generic placeholder wording: no sourced text was found"
        elif req and conf < 0.6 and prov != "unknown": status, note = "warn", f"low confidence ({conf}): verify before publishing"
        if status == "warn" and "low confidence" in note: warnings.append({"code": "LOW_CONFIDENCE", "category": "UNVERIFIED_CONTENT", "message": f"{b['section']}/{b['slot']}: {note}", "fields": [key], "review": True})
        static.append({"section": b["section"], "slot": b["slot"], "target": key, "source_field": sf,
                       "value": (val if isinstance(val, (int, float)) else json.dumps(val, ensure_ascii=False)[:140]) if not empty else None,
                       "data_type": "media" if key.startswith("slots.") or key in ("media", "logos") else _kind(val), "required": req, "source_ref": src, "confidence": conf,
                       "provenance": prov, "approval_status": appr, "validation": status, "validation_note": note, "fallback_policy": fallback})

    schema = EMAIL_TOKENS if flow == "email" else CVENT_TOKENS
    per = {}
    parts = [(SECTION_OF_ID.get(s["id"], s["name"]), s) for s in tpl.get("sections", [])] + [("header", tpl.get("header_cta_buttons")), ("chrome", tpl.get("contactInfo"))]
    for sec, obj in parts:
        for t in _tokens(obj): per.setdefault(t, {}).setdefault(sec, 0); per[t][sec] += 1
    dynamic = []
    for t, secs in sorted(per.items()):
        cat = classify(t)
        ok = t in schema
        if not ok: errors.append({"code": "UNKNOWN_MERGE_FIELD", "category": "MERGE_FIELD_UNMAPPED", "message": f"{t} is not a merge field in the {flow} flow's schema", "fields": [t]})
        dynamic.append({"token": t, "label": LABEL_OF.get(t), "category": cat, "supplied_by": SUPPLIER[cat], "sections": sorted(secs), "uses": sum(secs.values()),
                        "validation": "ok" if ok else "error", "fallback_policy": "hippo_supplied"})
    cats = {}
    for d in dynamic: cats[d["category"]] = cats.get(d["category"], 0) + 1
    return {"flow": flow, "template_family": (plan or {}).get("template_family"),
            "summary": {"static_bindings": len(static), "static_ok": sum(1 for s in static if s["validation"] in ("ok", "warn")), "static_omitted": sum(1 for s in static if s["validation"] == "omitted"),
                        "dynamic_fields": len(dynamic), "dynamic_by_category": cats, "errors": len(errors), "warnings": len(warnings)},
            "static": static, "dynamic": dynamic, "errors": errors, "warnings": warnings}
