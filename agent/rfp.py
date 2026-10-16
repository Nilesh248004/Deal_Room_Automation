"""Cvent RFP reader: the RFP decides WHICH sections and merge fields a deal room carries, never what they say.

Usage:  python3 agent/rfp.py <rfp.pdf|rfp.txt|rfp.json>     prints the parsed RFP and what it switches on

Every RFP value stays a merge field that Hippo/Cvent fills for the RFP being answered. The parsed values are used
only to (1) switch sections and merge fields on or off and (2) show sample values in the review preview.
"""
import json, re, sys, html

DAY = r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)"
DATE = DAY + r",\s+[A-Z][a-z]{2}\s+\d{1,2},\s+\d{4}"
TIME = r"\d{1,2}:\d{2}\s?[AP]M"

# Section headers of a Cvent Supplier Network RFP (any order: PDF text order does not follow the page layout)
HEADERS = [("contact", r"Key Contact Information"), ("org", r"Organization Information"), ("event", r"Event Information"),
           ("rooms", r"Guest Room Requirements"), ("details", r"RFP Details"), ("meeting", r"Meeting Room Requirements"),
           ("agenda", r"Day\s+Time\s+Agenda Item\s+Meeting Room Requested"), ("fnb", r"Food (?:&|and) Beverage Requirements"),
           ("av", r"AV Requirements"), ("concessions", r"Concessions Requested"), ("history", r"Event History"),
           ("objectives", r"Business Objectives"), ("questions", r"Additional Questions"), ("attachments", r"Attachments")]

KINDS = [("breakfast", r"breakfast"), ("lunch", r"lunch|luncheon"), ("dinner", r"dinner|banquet|gala"),
         ("reception", r"reception|cocktail|happy hour"), ("break", r"\bbreaks?\b|coffee|snack|refreshment"),
         ("meeting", r"meeting|session|breakout|workshop|keynote|board|conference|offsite|training|general")]


def pdf_text(path):
    from pypdf import PdfReader
    return "\n".join((p.extract_text() or "") for p in PdfReader(path).pages)


NOISE = re.compile(r"\d+/\d+|cvent\S*|SUPPLIER NETWORK|.*Request for Proposal \(RFP\)_?", re.I)

def _lines(text):
    """Drop page furniture: the running title and organisation line on every page, 'n/m' page numbers, Cvent branding."""
    raw = [l.strip() for l in text.replace("\r", "").split("\n")]
    title = next((l for l in raw if l and not NOISE.fullmatch(l)), "")
    org = _one(r"Organization Name\s+(.+)", text)
    return [l for l in raw if l and l != title and l != org and not NOISE.fullmatch(l)], title


def _sections(T):
    hits = []
    for key, rx in HEADERS:
        m = re.search(r"(?m)^" + rx + r"\b", T)
        if m: hits.append((m.start(), m.end(), key))
    hits.sort()
    return {key: T[e: hits[i + 1][0] if i + 1 < len(hits) else len(T)].strip() for i, (s, e, key) in enumerate(hits)}


def _one(rx, s, flags=0, group=1):
    m = re.search(rx, s or "", flags)
    return re.sub(r"\s+", " ", m.group(group)).strip() if m else ""


def _int(s):
    s = re.sub(r"[^\d]", "", s or "")
    return int(s) if s else 0


def kind_of(*texts):
    for t in texts:
        for k, rx in KINDS:
            if t and re.search(rx, t, re.I): return k
    return "other"


def parse(text):
    lines, title = _lines(text)
    T = "\n".join(lines)
    S = _sections(T)
    c, o, e, r, d = S.get("contact", ""), S.get("org", ""), S.get("event", ""), S.get("rooms", ""), S.get("details", "")
    rfp = {"provided": True, "format": "cvent", "title": title,
           "rfp_name": _one(r"RFP Name\s+(.+)", d), "rfp_code": _one(r"RFP Code\s+(\S+)", d), "rfp_type": _one(r"RFP Type\s+(.+)", d),
           "response_due": _one(r"Response Due Date\s+(" + DATE + ")", d), "decision_due": _one(r"Decision Due Date\s+(" + DATE + ")", d),
           "description": "\n".join(x.strip() for x in (re.search(r"Description\s+(.+)", d, re.S) or [None, ""])[1].split("\n") if x.strip()),
           "contact": {"name": _one(r"Contact Name\s+(.+?)\s+Email Address", c, re.S),
                       "email": re.sub(r"\s+", "", _one(r"Email Address\s+(.+?)(?=\n(?:Organization|Phone|Address)\b|\s+Phone Number|\Z)", c, re.S)),
                       "organization": _one(r"(?m)^Organization\s+(?!Name|Type|Information)(.+?)(?:\s+Phone Number.*)?$", c),
                       "phone": _one(r"Phone Number\s+(\+?[\d][\d\-\s().]{5,}\d)", c), "address": _one(r"(?m)^Address\s+(.+)$", c)},
           "organization": {"name": _one(r"Organization Name\s+(.+)", o), "type": _one(r"Organization Type\s+(.+?)(?:\s+Industry\b.*)?$", o, re.M),
                            "industry": _one(r"Industry\s+(.+)", o), "address": _one(r"(?m)^Address\s+(.+)$", o)},
           "event": {"name": _one(r"Event Name\s+(.+)", e), "attendees": _int(_one(r"Total Attendees\s+([\d,]+)", e)),
                     "type": _one(r"Event Type\s+(.+)", e), "commission": _one(r"(?m)^Commission\s+(.+)$", e),
                     "dates_flexible": _one(r"Dates Flexible\s+(\w+)", e), "dates": _one(r"Event Dates\s+(" + DATE + r"\s*-\s*" + DATE + ")", e),
                     "contract_location": _one(r"Contract Signature Location\s+(.+)", e)}}
    nights = [{"date": re.sub(r"\s+", " ", m.group(1)), "rooms": sum(int(x) for x in m.group(2).split())}
              for m in re.finditer(r"(?m)^(" + DATE + r")\s+([\d ]+)$", r)]
    rfp["guest_rooms"] = {"nights": nights, "check_in": _one(r"Check-In Date\s+(" + DATE + ")", r),
                          "total_room_nights": _int(_one(r"Total Room Nights\s+([\d,]+)", r)) or sum(n["rooms"] for n in nights),
                          "peak_room_nights": _int(_one(r"Peak Room Nights\s+([\d,]+)", r)) or max([n["rooms"] for n in nights] or [0])}
    rfp["meeting_start"] = _one(r"Start Date\s+(" + DATE + ")", S.get("meeting", ""))
    A = S.get("agenda", "")
    rows = list(re.finditer(r"(?m)^(" + DATE + r")\s+(" + TIME + r")\s*-\s*(" + TIME + r")\s+(.+)$", A))
    agenda = []
    for i, m in enumerate(rows):
        body = A[m.end(): rows[i + 1].start() if i + 1 < len(rows) else len(A)]
        bl = [x.strip() for x in body.split("\n") if x.strip()]
        typ = bl[0] if bl and not re.match(r"Notes or Exceptions|\d+ people|.*\(Meeting Room Required\)", bl[0]) else ""
        room = next((x for x in bl if "Meeting Room Required" in x), "")
        agenda.append({"date": re.sub(r"\s+", " ", m.group(1)), "start": m.group(2), "end": m.group(3), "item": m.group(4).strip(), "type": typ,
                       "kind": kind_of(typ, m.group(4)),
                       "notes": _one(r"Notes or Exceptions:\s*(.+?)(?=\n[^\n]*\(Meeting Room Required\)|\n\d+\s+people|\Z)", body, re.S),
                       "setup": room.split("(")[0].strip(), "room_required": bool(room) and "No Meeting Room" not in room,
                       "people": _int(_one(r"(\d[\d,]*)\s+people", body))})
    rfp["agenda"] = agenda
    rfp["av_requirements"] = re.sub(r"\s+", " ", S.get("av", "")).strip()
    rfp["fnb_requirements"] = re.sub(r"\s+", " ", S.get("fnb", "")).strip()
    rfp["concessions_requested"] = re.sub(r"\s+", " ", S.get("concessions", "")).strip()
    qs = []
    parts = re.split(r"(?m)^(\d{1,3})\.\s+", S.get("questions", ""))
    for n, body in zip(parts[1::2], parts[2::2]):
        opts = re.findall(r"(?m)^\[\s?\]\s*(.+)$", body)
        body = re.sub(r"(?m)^\[\s?\]\s*.+$", "", body)
        qtype = _one(r"\((Comment|Single choice|Multiple choice|Yes/No|Number|Date)\)", body) or "Comment"
        text = re.sub(r"\s*\((?:Comment|Single choice|Multiple choice|Yes/No|Number|Date|Required)\)", "", re.sub(r"\s+", " ", body)).strip()
        qs.append({"n": int(n), "text": text, "type": qtype, "required": "(Required)" in body, "options": opts})
    rfp["questions"] = qs
    return rfp


def load(path):
    if path.lower().endswith(".json"):
        j = json.load(open(path))
        j.setdefault("provided", True); return j
    text = pdf_text(path) if path.lower().endswith(".pdf") else open(path, encoding="utf-8", errors="replace").read()
    return parse(text)


# ------------------------------------------------------------------ what the RFP switches on
def needs(rfp):
    """None = no RFP: the deal room carries the full field set (as before RFP support)."""
    if not rfp or not rfp.get("provided"): return None
    ag = rfp.get("agenda") or []
    kinds = {a["kind"] for a in ag}
    qs = " ".join(q["text"] for q in rfp.get("questions") or []).lower()
    every = " ".join([qs, (rfp.get("description") or "").lower(), (rfp.get("av_requirements") or "").lower(),
                      (rfp.get("fnb_requirements") or "").lower(), " ".join((a.get("notes") or "") for a in ag).lower()])
    rtype = (rfp.get("rfp_type") or "").lower()
    gr = rfp.get("guest_rooms") or {}
    n = {"rooms": bool(gr.get("total_room_nights")) or "guest room" in rtype,
         "meeting": "meeting" in kinds or any(a.get("room_required") for a in ag) or "meeting space" in rtype,
         "breakfast": "breakfast" in kinds, "lunch": "lunch" in kinds, "dinner": "dinner" in kinds or "reception" in kinds,
         "break": "break" in kinds, "fnb": bool(kinds & {"breakfast", "lunch", "dinner", "reception", "break"}) or bool(rfp.get("fnb_requirements")),
         "av": bool(rfp.get("av_requirements")) or bool(re.search(r"\bav\b|audio|projector|screen", every)),
         "concessions": "concession" in every or bool(rfp.get("concessions_requested")),
         "commission": (rfp.get("event") or {}).get("commission", "").lower().startswith("yes") or "commission" in qs,
         "questions": bool(rfp.get("questions")),
         "renovation": bool(re.search(r"renovat", qs)), "construction": bool(re.search(r"construction", qs)),
         "parking": "parking" in every, "internet": bool(re.search(r"internet|wi-?fi|bandwidth", every)),
         "housekeeping": "housekeeping" in every, "fees": bool(re.search(r"\bfees?\b|\btax", every)),
         "alternate_dates": (rfp.get("event") or {}).get("dates_flexible", "").lower().startswith("yes")}
    n["dining_info"] = n["fnb"] or bool(re.search(r"dining|restaurant|\bbar\b|food", every))
    n["location_info"] = bool(re.search(r"airport|distance|easy to get|transport|nearby|offsite", every))
    return n


# Email token -> the RFP need that switches it on. Tokens not listed are always kept (event, contact, totals, proposal).
FIELD_NEED = {
    "${Guest Room Rate}": "rooms", "${Peak Rooms}": "rooms", "${Total Room Nights}": "rooms", "${Total Sleeping Room Cost}": "rooms",
    "${Rates Include Breakfast}": "rooms", "${Rates Include Tax}": "rooms", "${Availability Status}": "rooms",
    "${Sleeping Room Needs Met}": "rooms", "${Table-Sleeping Room}": "rooms", "${Sleeping Room Info}": "rooms",
    "${Meeting Room Name}": "meeting", "${Meeting Room Setup}": "meeting", "${Meeting Room Capacity}": "meeting",
    "${Meeting Room Rental}": "meeting", "${Meeting Room Rental Total}": "meeting", "${Meeting Start Date}": "meeting",
    "${Table-Meeting Agenda}": "meeting", "${Meeting Room Info}": "meeting",
    "${Avg Breakfast Cost}": "breakfast", "${Avg Lunch Cost}": "lunch", "${Avg Dinner Cost}": "dinner", "${Avg Break Cost}": "break",
    "${Total F&B}": "fnb", "${F&B Minimum}": "fnb", "${F&B Applicable Tax}": "fnb", "${Service Charge}": "fnb",
    "${AV Cost}": "av", "${AV Requirements}": "av", "${Meeting Room Internet}": "av",
    "${Concessions}": "concessions", "${Total Concessions Value}": "concessions", "${Commission}": "commission",
    "${Additional Questions}": "questions", "${Last Renovated}": "renovation", "${Construction Plans}": "construction",
    "${Self Parking Fee}": "parking", "${Valet Parking Fee}": "parking", "${Guest Room Internet}": "internet",
    "${Housekeeping}": "housekeeping", "${Resort Fee}": "fees", "${Alternate Dates}": "alternate_dates",
}

def wants(need, token):
    """True if this merge field belongs on the page for this RFP (need=None: no RFP, keep everything)."""
    if need is None: return True
    k = FIELD_NEED.get(token)
    return True if k is None else bool(need.get(k))


WHY = {"rooms": "guest room block in the RFP", "meeting": "meeting rooms on the agenda", "breakfast": "breakfast on the agenda",
       "lunch": "lunch on the agenda", "dinner": "dinner or reception on the agenda", "break": "breaks on the agenda",
       "fnb": "meals on the agenda", "av": "AV requirements", "concessions": "concessions asked for", "commission": "commission requested",
       "questions": "additional questions", "renovation": "asks when the hotel was last renovated", "construction": "asks about construction",
       "parking": "mentions parking", "internet": "mentions internet / Wi-Fi", "housekeeping": "mentions housekeeping",
       "fees": "asks for all taxes and fees", "alternate_dates": "dates are flexible", "dining_info": "asks about food / dining",
       "location_info": "asks about getting there / airport / nearby"}

def explain(need):
    return [{"need": k, "on": bool(v), "why": WHY.get(k, k)} for k, v in (need or {}).items()]


# ------------------------------------------------------------------ preview values (review page only, never saved to Hippo)
def _table(head, rows):
    th = "".join(f"<th>{html.escape(h)}</th>" for h in head)
    tr = "".join("<tr>" + "".join(f"<td>{html.escape(str(c))}</td>" for c in r) + "</tr>" for r in rows)
    return f'<table class="rfp-table"><thead><tr>{th}</tr></thead><tbody>{tr}</tbody></table>'

def preview_values(rfp):
    """Email token -> sample value taken from the RFP (request side). Hotel answers (rates, costs) are left to the hotel."""
    if not rfp or not rfp.get("provided"): return {}
    ev, gr, ct, org = rfp.get("event") or {}, rfp.get("guest_rooms") or {}, rfp.get("contact") or {}, rfp.get("organization") or {}
    dates = ev.get("dates", "")
    out = {"${Event Name}": ev.get("name"), "${Company Name}": org.get("name"), "${Event Dates}": dates, "${Total Attendees}": ev.get("attendees"),
           "${Check-In Date}": gr.get("check_in") or dates.split(" - ")[0], "${Check-Out Date}": dates.split(" - ")[-1] if " - " in dates else "",
           "${Total Room Nights}": gr.get("total_room_nights"), "${Peak Room Nights}": gr.get("peak_room_nights"), "${Peak Rooms}": gr.get("peak_room_nights"),
           "${Rooms Needed}": gr.get("peak_room_nights"), "${Planner Name}": ct.get("name"), "${Planner Email}": ct.get("email"),
           "${Response Due Date}": rfp.get("response_due"), "${Decision Date}": rfp.get("decision_due"), "${Event Type}": ev.get("type"),
           "${Commission Required}": ev.get("commission"), "${Commission}": ev.get("commission"), "${AV Requirements}": rfp.get("av_requirements"),
           "${RFP Name}": rfp.get("rfp_name"), "${RFP Code}": rfp.get("rfp_code"), "${Meeting Start Date}": rfp.get("meeting_start"),
           "${Additional Info}": rfp.get("description"), "${Date Type}": "Flexible" if ev.get("dates_flexible", "").lower().startswith("yes") else "Fixed"}
    if gr.get("nights"):
        out["${Table-Sleeping Room}"] = _table(["Night", "Rooms"], [(n["date"], n["rooms"]) for n in gr["nights"]])
    if rfp.get("agenda"):
        t = _table(["Day", "Time", "Item", "Setup", "People"], [(a["date"], f'{a["start"]}-{a["end"]}', a["item"], a["setup"], a["people"]) for a in rfp["agenda"]])
        out["${Table-Requested Agenda}"] = out["${Table-Meeting Agenda}"] = t
    if rfp.get("questions"):
        out["${Additional Questions}"] = "<ol>" + "".join(f"<li>{html.escape(q['text'][:220])} <em>[hotel's answer]</em></li>" for q in rfp["questions"]) + "</ol>"
    return {k: v for k, v in out.items() if v not in (None, "", 0)}


if __name__ == "__main__":
    if len(sys.argv) != 2: print(__doc__); sys.exit(64)
    r = load(sys.argv[1]); nd = needs(r)
    print(json.dumps({"rfp": r, "needs": explain(nd)}, indent=2, ensure_ascii=False))
