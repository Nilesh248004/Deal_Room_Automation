"""Photos for a deal room: get them out of a job so a person can upload them to Hippo, then read the uploaded ones back.

Until a photo is uploaded to Hippo its carousel slot is a media merge field ("Hotel Fits Image 1") with no image behind it,
so Hippo shows a placeholder. There is no upload API we can call yet, so the flow is:

  1. download  - save every photo the deal room uses to a folder, named by its photo key (exterior_1.jpg, rooms_2.png ...)
  2. a person uploads those files into a carousel in the Hippo editor and clicks Save
  3. browser_snippets/harvest_media.js reads the uploaded assets back (hippo-media-<page>.json)
  4. adopt     - match them to the photo keys by file name and print the overrides that make the template use them

  python3 agent/photos.py download <job dir> <out dir>
  python3 agent/photos.py adopt    <job dir> <hippo-media.json>      (overrides JSON on stdout, notes on stderr)
"""
import json, os, re, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import netsafe                                    # noqa: E402
import assets as AS                               # noqa: E402
from common import jread, jwrite                  # noqa: E402

EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}
MAX_PHOTO = 15_000_000
SLOT_ORDER = ("fits", "rooms", "meeting", "dining", "amenities", "gallery")


def used_photos(h, tpl=None):
    """[(key, source_url, [merge field names])] for each photo the deal room shows, in slot order, once each."""
    fields = {}
    for s in (tpl or {}).get("sections", []):
        for c in s.get("components", []):
            for a in (c.get("data") or {}).get("assets", []) if c.get("type") == "Carousel" else []:
                if a.get("type") == "image" and a.get("merge_field"): fields.setdefault(a.get("url"), []).append(a["merge_field"])
    media, out, seen = h.get("media") or {}, [], set()
    for slot in SLOT_ORDER:
        for key in (h.get("slots") or {}).get(slot, []):
            m = media.get(key)
            if key in seen or not isinstance(m, dict) or not m.get("source_url"): continue
            seen.add(key); out.append((key, m["source_url"], fields.get(m["source_url"], [])))
    return out


def norm(name):
    """'Exterior_1.JPG' and 'exterior 1' are the same photo name."""
    return re.sub(r"[^a-z0-9]+", "", re.sub(r"\.(?:jpe?g|png|webp|gif)$", "", str(name or "").strip().lower()))


def match_harvest(harvest, keys):
    """Match assets uploaded to Hippo (harvest_media.js output) to photo keys by title.
    Returns (media patch, titles that matched no photo). The first asset for a key wins."""
    by_name = {norm(k): k for k in keys}
    patch, unmatched = {}, []
    for im in (harvest or {}).get("images", []) if isinstance(harvest, dict) else []:
        if not isinstance(im, dict): continue
        key = by_name.get(norm(im.get("title")))
        if key and str(im.get("asset_id", "")).isdigit() and im.get("token"):
            patch.setdefault(key, {"hippo": {"asset_id": int(im["asset_id"]), "token": str(im["token"]), "title": str(im.get("title") or key)}})
        elif not key:
            unmatched.append(str(im.get("title") or "(no title)"))
    return patch, unmatched


def _job(job_dir):
    import stages
    h, _ = stages.merged_config(job_dir)
    return h, jread(f"{job_dir}/template.json") or {}


def download(job_dir, out_dir):
    h, tpl = _job(job_dir)
    rows = used_photos(h, tpl)
    if not rows: sys.exit("This job has no photos to export.")
    os.makedirs(out_dir, exist_ok=True)
    referer = (jread(f"{job_dir}/scrape.json") or {}).get("url") or (jread(f"{job_dir}/input.json") or {}).get("hotel_url")
    done, failed = [], []
    for key, url, fields in rows:
        try:
            status, _, body = netsafe.fetch(url, max_bytes=MAX_PHOTO, timeout=20, headers=AS.request_headers(referer))
            ctype, w, hgt = netsafe.image_size(body)
            if status != 200 or ctype not in EXT: failed.append({"key": key, "source_url": url, "reason": f"HTTP {status}, not a usable image"}); continue
            name = key + EXT[ctype]
            with open(os.path.join(out_dir, name), "wb") as f: f.write(body)
            done.append({"file": name, "key": key, "merge_fields": fields, "width": w, "height": hgt, "bytes": len(body),
                         "caption": h["media"][key].get("_alt", ""), "source_url": url})
        except (netsafe.Unsafe, netsafe.Transient) as e:
            failed.append({"key": key, "source_url": url, "reason": str(e)})
    jwrite(os.path.join(out_dir, "manifest.json"), {"photos": done, "failed": failed})
    for d in done: print(f"  {d['file']:<22} {d['width']}x{d['height']}  {d['caption'][:60]}  ->  {', '.join(d['merge_fields'])}")
    for f in failed: print(f"  ✖ {f['key']}: {f['reason']}", file=sys.stderr)
    print(f"\n{len(done)} photo(s) saved to {out_dir}" + (f", {len(failed)} could not be downloaded" if failed else ""))
    return 0 if done else 1


def adopt(job_dir, harvest_file):
    h, tpl = _job(job_dir)
    keys = [k for k, _, _ in used_photos(h, tpl)]
    try:
        harvest = json.load(open(harvest_file, encoding="utf-8"))
    except (OSError, ValueError) as e:
        sys.exit(f"cannot read {harvest_file}: {e}")
    patch, unmatched = match_harvest(harvest, keys)
    missing = [k for k in keys if k not in patch]
    print(f"matched {len(patch)} of {len(keys)} photos by file name" + (f"; not uploaded yet: {', '.join(missing)}" if missing else ""), file=sys.stderr)
    if unmatched: print(f"ignored {len(unmatched)} uploaded image(s) whose name is not a photo key: {', '.join(unmatched[:8])}", file=sys.stderr)
    if not patch: sys.exit("none of the uploaded images is named like a photo key (for example exterior_1): upload the files saved by the `photos` command without renaming them")
    print(json.dumps({"media": patch}))
    return 0


if __name__ == "__main__":
    a = sys.argv[1:]
    if len(a) == 3 and a[0] == "download": sys.exit(download(a[1], a[2]))
    if len(a) == 3 and a[0] == "adopt": sys.exit(adopt(a[1], a[2]))
    sys.exit(__doc__)
