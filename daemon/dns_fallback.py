"""DNS-over-HTTPS fallback for the daemons.

The Mac's resolvers (the router, then a public one on UDP 53) went silent
twice in two days while plain HTTPS kept working, so every Graph and MSAL
call died on name resolution and the Teams/email relay went blind. This wraps
socket.getaddrinfo: normal resolution first, and only on failure ask a DoH
server by IP (no DNS needed) and cache the answer 5 minutes. After one
failure it goes DoH-first for FAIL_WINDOW seconds, because the system
resolver takes about 30 s to give up each time.

Import it once at the top of a daemon: `import dns_fallback`. Nothing else
to call. Configure with DNS_DOH_URL (a URL with %s where the host name goes,
answering application/dns-json; Cloudflare's 1.1.1.1 endpoint by default).
"""
import json, os, socket, time, urllib.request

_orig = socket.getaddrinfo
_cache = {}          # host -> (expires, [ips])
_last_fail = [0.0]   # the system resolver takes ~30 s to give up; after one
                     # failure go DoH-first for two minutes instead of waiting.
FAIL_WINDOW = float(os.environ.get("DNS_FAIL_WINDOW_S", 120))
CACHE_S = float(os.environ.get("DNS_CACHE_S", 300))
DOH = os.environ.get("DNS_DOH_URL", "https://1.1.1.1/dns-query?name=%s&type=A")


def _doh(host):
    req = urllib.request.Request(DOH % host, headers={"accept": "application/dns-json"})
    with urllib.request.urlopen(req, timeout=6) as r:
        ans = json.loads(r.read().decode())
    ips = [a["data"] for a in ans.get("Answer", []) if a.get("type") == 1]
    if not ips:
        raise socket.gaierror(8, "DoH: no A record for %s" % host)
    _cache[host] = (time.time() + CACHE_S, ips)
    return ips


def _from_doh(host, port, type, proto):
    exp, ips = _cache.get(host, (0, None))
    if not ips or exp < time.time():
        ips = _doh(host)
    st = type or socket.SOCK_STREAM
    return [(socket.AF_INET, st, proto or 6, "", (ip, port)) for ip in ips]


def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    plain = not isinstance(host, str) or host.replace(".", "").isdigit() or host in ("localhost",)
    if not plain and time.time() - _last_fail[0] < FAIL_WINDOW:
        try:
            return _from_doh(host, port, type, proto)
        except Exception:
            pass
    try:
        return _orig(host, port, family, type, proto, flags)
    except socket.gaierror:
        if plain:
            raise
        _last_fail[0] = time.time()
        return _from_doh(host, port, type, proto)


socket.getaddrinfo = getaddrinfo
