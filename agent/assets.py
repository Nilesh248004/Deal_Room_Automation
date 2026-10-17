"""Asset validation: availability, content type, size, dimensions and content hash (PDF §10.2).

Every image the deal room will show is fetched once (through netsafe.fetch, so redirects and private networks are
checked) and recorded. Results are cached per hotel so a rebuild does not download the same photo again, and the
content hash is used to drop the same picture served under two URLs.

ASSET_CHECK_POLICY: enforce (default) prunes unreachable images and blocks when nothing usable is left;
warn records problems but removes nothing (for CDNs that refuse servers but serve browsers); off skips the check.
"""
import concurrent.futures as cf, copy, hashlib, os, time

import netsafe
from common import jread, jwrite, now_iso

MAX_BYTES = 15_000_000
MIN_WIDTH = 400
RETRIES = 2


def policy():
    p = os.environ.get("ASSET_CHECK_POLICY", "enforce").lower()
    return p if p in ("enforce", "warn", "off") else "enforce"


def request_headers(referer=None):
    """What every photo request carries, in the check and in the export, so the file a person uploads is the one that was verified.
    No avif: its size can't be read here and Hippo's uploader wants jpeg, png, webp or gif."""
    return {"Accept": "image/webp,image/jpeg,image/png,image/gif,image/*;q=0.8", **({"Referer": referer} if referer else {})}


def check_one(url, referer):
    """One validated record for url. Transient network failures are retried; permanent ones are recorded."""
    rec = {"url": url, "ok": False, "status": None, "content_type": None, "bytes": 0, "width": 0, "height": 0, "sha256": None,
           "error": None, "transient": False, "checked_at": now_iso()}
    for attempt in range(RETRIES + 1):
        try:
            st, hd, body = netsafe.fetch(url, max_bytes=MAX_BYTES, timeout=15, headers=request_headers(referer))
        except netsafe.Unsafe as e:
            rec["error"], rec["transient"] = (str(e) if str(e).startswith("host not found") else f"blocked: {e}"), False; return rec
        except netsafe.Transient as e:
            rec["error"], rec["transient"] = str(e), True
            if attempt < RETRIES: time.sleep(min(e.retry_after or 0.5 * 2 ** attempt, 5)); continue
            return rec
        rec["status"] = st
        if st != 200: rec["error"], rec["transient"] = f"HTTP {st}", False; return rec
        sniffed, w, h = netsafe.image_size(body)
        ctype = (hd.get("Content-Type") or "").split(";")[0].strip().lower()
        rec.update(content_type=sniffed or ctype, bytes=len(body), width=w, height=h, sha256=hashlib.sha256(body).hexdigest())
        if not (sniffed or ctype.startswith("image/")): rec["error"] = f"not an image (content-type {ctype or 'unknown'})"
        elif not body: rec["error"] = "empty response"
        else: rec["ok"], rec["transient"] = True, False
        return rec
    return rec


def check_assets(urls, referer, cache=None, ttl_hours=24, workers=6, deadline=100):
    """{url: record} for every url. Reuses fresh successful cache entries; failures are always re-checked."""
    out, todo, cache = {}, [], cache or {}
    now = time.time()
    for u in dict.fromkeys(urls):
        c = cache.get(u)
        if c and c.get("ok") and now - c.get("_ts", 0) < ttl_hours * 3600: out[u] = {**c, "cached": True}
        else: todo.append(u)
    if not todo: return out
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(check_one, u, referer): u for u in todo}
        try:
            for f in cf.as_completed(futs, timeout=deadline):
                r = f.result(); r["_ts"] = time.time(); out[futs[f]] = r
        except cf.TimeoutError:
            for f, u in futs.items():
                if u not in out: f.cancel(); out[u] = {"url": u, "ok": False, "status": None, "error": "not checked: deadline reached", "transient": True, "checked_at": now_iso()}
    return out


def load_cache(path):
    return jread(path, {}) or {}


def save_cache(path, cache, records):
    keep = {u: r for u, r in {**cache, **records}.items() if r.get("ok") and r.get("_ts")}
    jwrite(path, dict(sorted(keep.items(), key=lambda kv: -kv[1]["_ts"])[:500]))


def mark_small(recs, photo_urls, min_width=MIN_WIDTH):
    """Photos narrower than min_width are thumbnails, placeholders or tracking pixels, not something to put in a proposal.
    Only photo URLs are passed in: a logo is allowed to be small. Applied after the cache is saved, so a size rule change never needs a re-download."""
    for u in photo_urls:
        r = recs.get(u)
        if r and r.get("ok") and 0 < (r.get("width") or 0) < min_width:
            r.update(ok=False, transient=False, error=f"too small ({r['width']}x{r['height']}px; photos must be at least {min_width}px wide)")
    return recs


def prune_media(h, assets, pol, changes=None):
    """(h2, notes): h with unreachable or duplicate photos removed and emptied slots refilled. Only under 'enforce'.
    `changes`, if given, receives one dict per removal / substitution / duplicate (kind, key, transient, note) for QA."""
    notes = []
    changes = changes if changes is not None else []
    if pol != "enforce": return h, notes
    h = copy.deepcopy(h); media, slots = h.get("media") or {}, h.get("slots") or {}
    rec = lambda k: assets.get(media[k]["source_url"]) or {}
    bad = {k for k in media if rec(k) and not rec(k).get("ok")}
    seen, repl = {}, {}
    for k in media:                                        # same picture under two URLs: keep the first key
        r = rec(k)
        if k in bad or not r.get("sha256"): continue
        if r["sha256"] in seen:
            repl[k] = seen[r["sha256"]]; bad.add(k); notes.append(f"{k} is the same picture as {seen[r['sha256']]}: kept one")
            changes.append({"kind": "duplicate", "key": k, "transient": False, "note": notes[-1]})
        else: seen[r["sha256"]] = k
    for k in sorted(bad):
        if k not in repl:
            notes.append(f"{k} removed: {rec(k).get('error', 'unusable')}")
            changes.append({"kind": "removed", "key": k, "transient": bool(rec(k).get("transient")), "note": notes[-1]})
    for k in bad: media.pop(k, None)
    spare = [k for k in media if rec(k).get("ok")]        # only a photo that was actually verified may stand in for a missing one
    for name, keys in slots.items():
        if name.startswith("_"): continue
        kept = []
        for k in keys:
            k = repl.get(k, k)
            if k in media and k not in kept: kept.append(k)
        if not kept and spare:
            kept = spare[:1]; notes.append(f"slot '{name}' had no working photo: using {kept[0]}, a different photo")
            changes.append({"kind": "substituted", "key": name, "transient": False, "note": notes[-1]})
        slots[name] = kept
    return h, notes
