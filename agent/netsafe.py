"""Outbound-request safety: URL checks, private-network blocking, redirect validation, robots.txt, bounded fetch.

Hotel pages and images are untrusted input. Every outbound request the pipeline makes itself (robots.txt,
asset checks) goes through fetch(), which validates the URL and every redirect hop. Chromium's own requests
are filtered with host_is_public() from the capture stage.

ALLOW_PRIVATE_HOSTS=1 turns the private-network checks off. It exists only so the test suite can talk to a
mock site on 127.0.0.1; never set it in production.
"""
import ipaddress, os, re, socket, time, urllib.error, urllib.parse, urllib.request, urllib.robotparser

UA = "Mozilla/5.0 (compatible; DealRoomAgent/1.0)"
ROBOTS_TOKEN = "DealRoomAgent"


class Unsafe(Exception):
    """The URL or a redirect hop is not allowed. Never retried."""


class Transient(Exception):
    """A network failure worth retrying (timeout, reset, 429, 5xx)."""
    def __init__(self, msg, retry_after=None):
        super().__init__(msg); self.retry_after = retry_after


def allow_private():
    return os.environ.get("ALLOW_PRIVATE_HOSTS") == "1"


def _is_public_ip(addr):
    ip = ipaddress.ip_address(addr)
    if getattr(ip, "ipv4_mapped", None): ip = ip.ipv4_mapped
    return ip.is_global


_dns_cache = {}     # host -> (checked_at, ok); short-lived so a changed answer is noticed
DNS_TTL = 30

def host_is_public(host, resolve=True, fail_closed=False):
    """True when every address the host resolves to is a public one. A host that does not resolve passes unless fail_closed
    (our own requests simply fail; for Chromium, which resolves again by itself, an unresolvable name is refused).
    resolve=False checks only the literal form (IP address or a local-looking name) and never touches DNS."""
    if allow_private(): return True
    host = (host or "").strip("[]").lower()
    if not host or host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan")): return False
    try: return _is_public_ip(host)                       # literal IP
    except ValueError: pass
    if not resolve: return True
    hit = _dns_cache.get(host)
    if hit and time.monotonic() - hit[0] < DNS_TTL: return hit[1]
    try: infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror: return not fail_closed
    ok = all(_is_public_ip(i[4][0]) for i in infos)
    _dns_cache[host] = (time.monotonic(), ok)
    return ok


def check_url(url, allow_http=None, resolve=True):
    """Raise Unsafe unless url is an http(s) URL without credentials, on a standard port, to a public host."""
    allow_http = os.environ.get("ALLOW_HTTP") == "1" if allow_http is None else allow_http
    if "\\" in url or any(ord(c) < 32 for c in url): raise Unsafe("URL contains a backslash or control character")      # Python and Chromium would read the host differently
    try: u = urllib.parse.urlparse(url)
    except ValueError: raise Unsafe("not a valid URL")
    if u.scheme not in ("https", "http"): raise Unsafe(f"unsupported URL scheme '{u.scheme}'")
    if u.scheme == "http" and not allow_http: raise Unsafe("URL must use https")
    if not u.hostname: raise Unsafe("URL has no host")
    if u.username or u.password: raise Unsafe("URL must not contain credentials")
    try: port = u.port
    except ValueError: raise Unsafe("URL has an invalid port")
    if port not in (None, 80, 443) and not allow_private(): raise Unsafe(f"port {port} is not allowed")
    if not host_is_public(u.hostname, resolve): raise Unsafe(f"{u.hostname} resolves to a private or reserved network address")
    return u


def same_site(a, b):
    """Same host (ignoring a leading www.) or one is a subdomain of the other."""
    a, b = [re.sub(r"^www\.", "", (h or "").lower()) for h in (a, b)]
    return bool(a) and bool(b) and (a == b or a.endswith("." + b) or b.endswith("." + a))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k): return None

_opener = urllib.request.build_opener(_NoRedirect)


def fetch(url, *, max_bytes=2_000_000, timeout=10, headers=None, max_redirects=4, method="GET", deadline=30):
    """Fetch url with every hop validated. Returns (status, headers, body). Raises Unsafe or Transient.

    4xx answers are returned (the caller decides); 429 and 5xx raise Transient with Retry-After when given.
    """
    hdrs = {"User-Agent": UA, "Accept": "*/*", **(headers or {})}
    for _ in range(max_redirects + 1):
        check_url(url)
        req = urllib.request.Request(url, headers=hdrs, method=method)
        try:
            with _opener.open(req, timeout=timeout) as r:
                t0, buf = time.monotonic(), bytearray()
                while True:                                          # a total deadline, not just a per-read timeout: a slow drip must not hold a stage
                    chunk = r.read(65536)
                    if not chunk: break
                    buf += chunk
                    if len(buf) > max_bytes: raise Unsafe(f"response is larger than {max_bytes} bytes")
                    if time.monotonic() - t0 > deadline: raise Transient(f"response from {urllib.parse.urlparse(url).hostname} is too slow")
                return r.status, r.headers, bytes(buf)
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                to = e.headers.get("Location")
                if not to: raise Unsafe("redirect without a location")
                url = urllib.parse.urljoin(url, to)
                continue
            if e.code == 429 or e.code >= 500:
                ra = e.headers.get("Retry-After")
                raise Transient(f"HTTP {e.code} from {urllib.parse.urlparse(url).hostname}", int(ra) if ra and ra.isdigit() else None)
            return e.code, e.headers, e.read(max_bytes) if method != "HEAD" else b""
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
            if isinstance(getattr(e, "reason", None), socket.gaierror): raise Unsafe(f"host not found: {urllib.parse.urlparse(url).hostname}")   # DNS says no: retrying will not help
            if re.search(r"Tunnel connection failed: 4\d\d", str(getattr(e, "reason", e))): raise Unsafe(f"refused by the network proxy: {getattr(e, 'reason', e)}")   # a proxy that says 403 is a policy, not a blip
            raise Transient(f"{type(e).__name__}: {getattr(e, 'reason', e)}")
    raise Unsafe("too many redirects")


def robots_for(url):
    """Parsed robots.txt for url's origin, or None when the site publishes none. Raises Transient if it cannot be read."""
    u = urllib.parse.urlparse(url)
    origin = f"{u.scheme}://{u.netloc}"
    status, _, body = fetch(origin + "/robots.txt", max_bytes=512_000, timeout=10)
    if status >= 400: return None                         # RFC 9309: an unavailable robots.txt means no restrictions
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(body.decode("utf-8", "replace").splitlines())
    return rp


def robots_allows(rp, url):
    return True if rp is None else rp.can_fetch(ROBOTS_TOKEN, url)


# ---- image header sniffing (dimensions without an imaging library) ----
def image_size(data):
    """(content_type, width, height) from the first bytes of PNG, JPEG, GIF or WebP data, else (None, 0, 0)."""
    import struct
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        w, h = struct.unpack(">II", data[16:24]); return "image/png", w, h
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        w, h = struct.unpack("<HH", data[6:10]); return "image/gif", w, h
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF: i += 1; continue
            marker = data[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7: i += 2; continue
            seg = struct.unpack(">H", data[i + 2:i + 4])[0]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", data[i + 5:i + 9]); return "image/jpeg", w, h
            i += 2 + seg
        return "image/jpeg", 0, 0
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP" and len(data) >= 30:
        kind = data[12:16]
        if kind == b"VP8 ": w, h = struct.unpack("<HH", data[26:30]); return "image/webp", w & 0x3FFF, h & 0x3FFF
        if kind == b"VP8L":
            b = struct.unpack("<I", data[21:25])[0]; return "image/webp", (b & 0x3FFF) + 1, ((b >> 14) & 0x3FFF) + 1
        if kind == b"VP8X": return "image/webp", int.from_bytes(data[24:27], "little") + 1, int.from_bytes(data[27:30], "little") + 1
        return "image/webp", 0, 0
    return None, 0, 0
