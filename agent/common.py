"""Small shared helpers for the pipeline: atomic JSON files, hashing, audit events. Standard library only."""
import hashlib, json, os, re, time

CATEGORIES = {
    "SOURCE_UNVERIFIED": "Official website could not be verified",
    "SOURCE_UNAVAILABLE": "Source unavailable or blocked",
    "REQUIRED_INFO_MISSING": "Required information missing",
    "CONFLICTING_DATA": "Conflicting room, amenity or contact data",
    "ASSET_MISSING": "Logo or asset missing or unusable",
    "USAGE_RIGHTS_UNCLEAR": "Asset usage rights unclear",
    "MERGE_FIELD_UNMAPPED": "Required merge field unmapped",
    "UNSUPPORTED_TEMPLATE": "Unsupported template, component or customization",
    "RENDER_FAILURE": "Rendering or QA failure",
    "SHARE_LINK_FAILURE": "Share-link failure",
    "DELIVERY_FAILURE": "Delivery integration failure",
    "COMMERCIAL_APPROVAL": "Commercial details require approval",
    "UNVERIFIED_CONTENT": "Content not traceable to the hotel's own pages",
    "PUBLISH_FAILURE": "Saving to or reading back from Hippo failed",
    "INVALID_INPUT": "Request input is invalid",
    "INTERNAL_ERROR": "Unexpected internal error",
}


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def jread(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def jwrite(path, obj):
    """Write JSON atomically (tmp file + rename) so a crash never leaves half a file."""
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(obj, n=12):
    return hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()[:n]


def slugify(s, limit=60):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:limit]


def audit(job_dir, event, **data):
    """Append one audit line to the job's audit.jsonl (the Node runner appends to the same file)."""
    line = json.dumps({"at": now_iso(), "event": event, "by": "pipeline", **data}, ensure_ascii=False, default=str)
    with open(os.path.join(job_dir, "audit.jsonl"), "a", encoding="utf-8") as f:
        f.write(line + "\n")
