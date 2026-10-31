#!/usr/bin/env python3
"""
Resolve Google "goto" passthrough URLs in DataForSEO SERP responses.

Why: since Aug 2026 Google wraps result links in https://google.com/goto?url=CAES...
redirects. DataForSEO resolves them for you, but since 2026-10-30 Google throttles
resolution per SERP page, so Live and High-Priority SERP tasks come back with
~50% of URLs still as google.com/goto links. Left alone those rows look like
"google.com" ranks, so rank tracking, competitor lists, cannibalization and
outreach targets silently go wrong.

Fix: after every serp/* call, collect any goto URLs left in the response and send
them to DataForSEO's free converter (POST /v3/appendix/goto_url_converter,
max 100 URLs per request; a full page can take 40-60 s). Resolved URLs replace the
goto links in place, the original is kept in "goto_url", and a "domain" field
that said google.com is corrected. Anything still unresolved is counted in
response["_goto"] and flagged on stderr (stdout is left clean for the MCP server).

Stdlib only. Disable with ZENFUSION_GOTO_RESOLVE=0.

Usage:
    from goto_resolver import fix_goto_urls
    data = fix_goto_urls(json.loads(resp.read()), endpoint)

CLI (resolve URLs or rewrite a saved SERP JSON file in place):
    python3 goto_resolver.py https://google.com/goto?url=CAES...
    python3 goto_resolver.py --file research/dfs_serp_x.json
"""

import base64
import json
import os
import re
import sys
import urllib.request
from urllib.parse import urlparse

CONVERTER = "https://api.dataforseo.com/v3/appendix/goto_url_converter"
BATCH = 100
TIMEOUT = 180
GOTO_RE = re.compile(r"^https?://(?:www\.)?google\.[a-z.]+/goto\?", re.I)
GOOGLE_HOSTS = re.compile(r"^(?:www\.)?google\.[a-z.]+$", re.I)


def is_goto(value):
    return isinstance(value, str) and bool(GOTO_RE.match(value.strip()))


def _creds():
    login = os.environ.get("DATAFORSEO_LOGIN")
    password = os.environ.get("DATAFORSEO_PASSWORD")
    if login and password:
        return login, password
    found = {}
    for path in ("~/.zenfusion/.env", "~/.hermes/.env"):
        p = os.path.expanduser(path)
        if not os.path.exists(p):
            continue
        try:
            with open(p) as f:
                for line in f:
                    m = re.match(r"^\s*(DATAFORSEO_LOGIN|DATAFORSEO_PASSWORD)\s*=\s*(.+?)\s*$", line)
                    if m and m.group(2).strip("'\"") and m.group(1) not in found:
                        found[m.group(1)] = m.group(2).strip("'\"")
        except OSError:
            continue
    if "DATAFORSEO_LOGIN" in found and "DATAFORSEO_PASSWORD" in found:
        return found["DATAFORSEO_LOGIN"], found["DATAFORSEO_PASSWORD"]
    return None


def _walk(node):
    """Yield every dict in a JSON tree."""
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            yield cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


def collect_goto_urls(data):
    seen = []
    stack = [data]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
        elif is_goto(cur) and cur.strip() not in seen:
            seen.append(cur.strip())
    return seen


def _parse_converter(resp, sent):
    """Build {goto_url: direct_url} from a converter response, tolerant of shape."""
    mapping = {}
    for d in _walk(resp):
        # shape A: {"goto_url": "...goto...", "url": "https://dest"} (any key names)
        gotos = [v.strip() for v in d.values() if is_goto(v)]
        dests = [v for k, v in sorted(d.items(), key=lambda kv: "goto" in kv[0])
                 if isinstance(v, str) and v.startswith("http") and not is_goto(v)]
        if len(gotos) == 1 and dests:
            mapping.setdefault(gotos[0], dests[0])
        # shape B: {"https://google.com/goto?...": "https://dest", ...}
        for k, v in d.items():
            if is_goto(k) and isinstance(v, str) and v.startswith("http") and not is_goto(v):
                mapping.setdefault(k.strip(), v)
    # shape C: an ordered list of destinations matching the request order
    if not mapping:
        for d in _walk(resp):
            for v in d.values():
                if (isinstance(v, list) and len(v) == len(sent)
                        and all(isinstance(x, str) and x.startswith("http") and not is_goto(x) for x in v)):
                    mapping.update(zip(sent, v))
    return mapping


def resolve_goto_urls(urls, creds=None, timeout=TIMEOUT):
    """Return {goto_url: direct_url} for whatever the free converter could resolve."""
    urls = [u.strip() for u in urls if is_goto(u)]
    if not urls:
        return {}
    creds = creds or _creds()
    if not creds:
        print("goto_resolver: no DataForSEO credentials; goto URLs left unresolved", file=sys.stderr)
        return {}
    auth = base64.b64encode(f"{creds[0]}:{creds[1]}".encode()).decode()
    mapping = {}
    for i in range(0, len(urls), BATCH):
        chunk = urls[i:i + BATCH]
        req = urllib.request.Request(
            CONVERTER, data=json.dumps([{"goto_urls": chunk}]).encode(), method="POST",
            headers={"Authorization": "Basic " + auth, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                resp = json.loads(r.read())
        except Exception as e:  # network/timeout: leave these unresolved, never crash the caller
            print(f"goto_resolver: converter call failed ({e}); {len(chunk)} URLs left as goto", file=sys.stderr)
            continue
        got = _parse_converter(resp, chunk)
        if not got:
            dump = os.path.expanduser("~/.zenfusion/logs/goto_converter_unparsed.json")
            try:
                os.makedirs(os.path.dirname(dump), exist_ok=True)
                with open(dump, "w") as f:
                    json.dump(resp, f, indent=1)
            except OSError:
                dump = "(could not write dump)"
            print(f"goto_resolver: converter reply not understood; raw reply saved to {dump}", file=sys.stderr)
        mapping.update(got)
    return mapping


def apply_mapping(data, mapping):
    """Replace goto URLs in place; keep originals in 'goto_url'; fix google.com domains."""
    if not mapping:
        return 0
    replaced = 0
    for d in list(_walk(data)):
        for k, v in list(d.items()):
            if isinstance(v, str) and v.strip() in mapping:
                d[k] = mapping[v.strip()]
                replaced += 1
                if k == "url":
                    d.setdefault("goto_url", v)
                    dom = d.get("domain")
                    if not dom or (isinstance(dom, str) and GOOGLE_HOSTS.match(dom)):
                        d["domain"] = urlparse(d[k]).netloc
            elif isinstance(v, list):
                for i, x in enumerate(v):
                    if isinstance(x, str) and x.strip() in mapping:
                        v[i] = mapping[x.strip()]
                        replaced += 1
    return replaced


def fix_goto_urls(data, endpoint=None):
    """Resolve leftover goto URLs in a SERP response. Safe no-op for non-SERP calls."""
    if os.environ.get("ZENFUSION_GOTO_RESOLVE", "1") == "0":
        return data
    if endpoint is not None and "serp/" not in str(endpoint):
        return data
    if not isinstance(data, (dict, list)):
        return data
    found = collect_goto_urls(data)
    if not found:
        return data
    mapping = resolve_goto_urls(found)
    apply_mapping(data, mapping)
    left = len([u for u in found if u not in mapping])
    if isinstance(data, dict):
        data["_goto"] = {"found": len(found), "resolved": len(found) - left, "unresolved": left}
    if left:
        print(f"goto_resolver: {left}/{len(found)} google.com/goto URLs still unresolved — "
              f"treat those rows as unknown, not as google.com", file=sys.stderr)
    return data


def _main(argv):
    if len(argv) >= 2 and argv[0] == "--file":
        for path in argv[1:]:
            with open(path) as f:
                data = json.load(f)
            fix_goto_urls(data)
            with open(path, "w") as f:
                json.dump(data, f, indent=1)
            print(path, json.dumps(data.get("_goto") if isinstance(data, dict) else {}))
        return 0
    if argv:
        print(json.dumps(resolve_goto_urls(argv), indent=1))
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
