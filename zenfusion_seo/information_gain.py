#!/usr/bin/env python3
"""
Information Gain Measurement Pipeline
=====================================

Measures how much the top organic results for a set of queries repeat each
other, and how much unique information each individual page holds. It gives
you two things per SERP: the CONSENSUS (what most ranking pages say, your
coverage floor) and the UNIQUE claims (what only one page says, the open
space to own).

Why a script and not just a prompt: this pulls the real, live SERP from an
API, tells you exactly which pages it could and couldn't read, is
deterministic (same input, same output), and keeps the measuring layer
separate from the model doing the extraction. A chatbot doing all of it in
one pass can't give you that.

Pipeline stages
---------------
1. SERP      : pull top N organic results per query (DataForSEO)
2. Fetch     : extract main content from each ranking URL (trafilatura)
3. Extract   : turn each page into a list of atomic, checkable claims
4. Cluster   : merge semantically identical claims across pages
5. Measure   : repetition rate, consensus claims, per-page unique share

Every stage caches to ./cache, so a re-run costs nothing.

Two ways to run stages 3-4 (the claim extraction)
-------------------------------------------------
SUBSCRIPTION MODE (default, no model bill). The script fetches the SERPs and
the page text, then writes one paste-ready file per query into ./bundle. You
open the Claude or ChatGPT subscription you already pay for, paste a bundle,
paste the JSON it returns back into ./results, and the script does the rest.
The model work happens inside your flat-rate subscription, so it costs nothing
extra. This is exactly how the original run was done.

API MODE (optional, automated, pay-per-use). If you'd rather run it
unattended, set ANTHROPIC_API_KEY and use --stage full. The script calls the
Claude API once per page. Roughly a few dollars for ~100 pages.

Either way, the SERP data is live and real (that's the DataForSEO part), which
is the whole point of using a script instead of asking a chatbot to guess who
ranks.

Setup
-----
    pip install httpx trafilatura
    export DATAFORSEO_LOGIN="..."       # dataforseo.com account
    export DATAFORSEO_PASSWORD="..."
    # ANTHROPIC_API_KEY only needed for API mode (--stage full)

Then edit the QUERIES list below to your own keywords (keep them the same
intent type), set LOCATION_CODE / LANGUAGE_CODE for your market, and run.

Usage (subscription mode)
-------------------------
    python information_gain.py --stage bundle   # 1. build paste-ready files in ./bundle
    # 2. paste each ./bundle/*.txt into your Claude/ChatGPT, save the JSON it
    #    returns to ./results/<same-name>.json
    python information_gain.py --stage report   # 3. crunch ./results -> output/report.md

Usage (API mode)
----------------
    python information_gain.py --stage full     # fully automated, needs ANTHROPIC_API_KEY

Other
-----
    python information_gain.py --stage serp     # just show the SERPs
    python information_gain.py --stage content  # fetch + show readable pages
    python information_gain.py --no-cache       # ignore cache, refetch everything

Author: Shlomi Asaf. Share freely.
"""

from __future__ import annotations

try:  # resolve google.com/goto passthrough URLs in SERP responses (see goto_resolver.py)
    from .goto_resolver import fix_goto_urls as _fix_goto
except ImportError:
    try:
        from goto_resolver import fix_goto_urls as _fix_goto
    except ImportError:
        def _fix_goto(data, endpoint=None):
            return data
import argparse
import base64
import hashlib
import json
import math
import os
import re
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

# --------------------------------------------------------------------------
# CONFIG
# --------------------------------------------------------------------------

# Your keywords go here. Pick ONE niche and keep the intent type consistent:
# 15-20 informational queries, OR a batch of service/commercial terms. Mixing
# intent types muddies the number. Even a single keyword works if you just want
# the consensus-vs-unique map for one page you're planning.
QUERIES = [
    "best water softener",
]

LOCATION_CODE = 2840   # 2840 = United States, 2076 = Brazil, 2376 = Israel
LANGUAGE_CODE = "en"   # "en" / "pt" / "he"

TOP_N = 10             # results per query to analyse
CONSENSUS_RATIO = 0.7  # a claim is "consensus" when this share of the
                       # readable pages assert it (minimum 2 pages)

MODEL = "claude-sonnet-5"   # only used in API mode (--stage full)
MAX_WORKERS = 4

CONTENT_CHARS = 15_000  # chars of each page fed to the model (per page)

CACHE_DIR = Path("cache")
OUT_DIR = Path("output")
BUNDLE_DIR = Path("bundle")     # paste-ready files for subscription mode
RESULTS_DIR = Path("results")   # you save the model's JSON here (subscription mode)

# --------------------------------------------------------------------------
# INFRA
# --------------------------------------------------------------------------

USE_CACHE = True


def cache_key(*parts: str) -> str:
    return hashlib.sha256("||".join(parts).encode()).hexdigest()[:20]


def cached(namespace: str, key: str, producer):
    """Disk-cache any JSON-serialisable producer result."""
    path = CACHE_DIR / namespace / f"{key}.json"
    if USE_CACHE and path.exists():
        return json.loads(path.read_text())
    value = producer()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False))
    return value


def log(msg: str) -> None:
    print(f"  {msg}", flush=True)


# --------------------------------------------------------------------------
# STAGE 1 - SERP
# --------------------------------------------------------------------------

DFS_ENDPOINT = "https://api.dataforseo.com/v3/serp/google/organic/live/advanced"


def dfs_auth_header() -> dict[str, str]:
    login = os.environ["DATAFORSEO_LOGIN"]
    password = os.environ["DATAFORSEO_PASSWORD"]
    token = base64.b64encode(f"{login}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}", "Content-Type": "application/json"}


def fetch_serp(query: str) -> list[dict[str, Any]]:
    """Top N organic results for one query."""

    def call() -> list[dict[str, Any]]:
        payload = [{
            "keyword": query,
            "location_code": LOCATION_CODE,
            "language_code": LANGUAGE_CODE,
            "device": "desktop",
            "depth": max(TOP_N, 20),
            "use_improved_etv": True,
        }]
        r = httpx.post(DFS_ENDPOINT, headers=dfs_auth_header(),
                       json=payload, timeout=120)
        r.raise_for_status()
        items = _fix_goto(r.json(), 'serp/')["tasks"][0]["result"][0]["items"]
        organic = [i for i in items if i.get("type") == "organic"]
        # DataForSEO checks array (replaces deprecated booleans as of Jan 20, 2027)
        # checks is null when no special properties, or a list of property strings
        return [
            {
                "position": n,
                "url": i["url"],
                "domain": i.get("domain", ""),
                "title": i.get("title", ""),
                "is_highly_cited": "is_highly_cited" in (i.get("checks") or []),
            }
            for n, i in enumerate(organic[:TOP_N], start=1)
        ]

    return cached("serp", cache_key(query, str(LOCATION_CODE), LANGUAGE_CODE), call)


# --------------------------------------------------------------------------
# STAGE 2 - CONTENT
# --------------------------------------------------------------------------

BOILERPLATE = re.compile(
    r"(cookie|subscribe|newsletter|sign up|all rights reserved|privacy policy)",
    re.I,
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    )
}


def fetch_content(url: str) -> str:
    """Main article text. Returns '' when the page can't be read."""

    def call() -> dict[str, str]:
        try:
            r = httpx.get(url, headers=HEADERS, timeout=45, follow_redirects=True)
            r.raise_for_status()
            html = r.text
        except Exception as exc:  # noqa: BLE001
            log(f"fetch failed {url}: {exc}")
            return {"text": ""}

        try:
            import trafilatura
            text = trafilatura.extract(html, include_comments=False,
                                       include_tables=True) or ""
        except ImportError:
            sys.exit("pip install trafilatura")

        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        lines = [ln for ln in lines if not BOILERPLATE.search(ln)]
        return {"text": "\n".join(lines)[:60_000]}

    return cached("content", cache_key(url), call)["text"]


# --------------------------------------------------------------------------
# STAGE 3 - CLAIM EXTRACTION
# --------------------------------------------------------------------------

EXTRACT_PROMPT = """You are extracting atomic claims from a web page for a study \
measuring information redundancy across search results.

A CLAIM is a single, self-contained, checkable assertion about the world.

Include:
- factual statements, definitions, causal assertions
- specific numbers, thresholds, dates, prices, measurements
- procedural steps stated as fact
- explicit recommendations with a stated reason

Exclude:
- marketing copy, brand promotion, calls to action
- navigation, author bios, disclaimers
- pure opinion with no checkable content
- anything about the website itself rather than the topic

Rules:
- One assertion per claim. Split compound sentences.
- Rewrite each claim in neutral, brand-free language so that identical claims \
from different sites come out phrased the same way.
- Do not include the site name or "we" / "our" in a claim.
- Keep each claim under 25 words.
- Extract at most 40 claims. If the page has more, keep the most substantive.
- Mark a claim as "specific": true when it contains a number, date, threshold, \
named study, or first-hand result. Otherwise false.

Return ONLY valid JSON, no markdown fences, in this exact shape:
{"claims": [{"text": "...", "specific": true}]}

TOPIC: {query}

PAGE CONTENT:
{content}"""


def anthropic_call(prompt: str, max_tokens: int = 4000) -> str:
    key = os.environ["ANTHROPIC_API_KEY"]
    body = {
        "model": MODEL,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            r = httpx.post("https://api.anthropic.com/v1/messages",
                           headers=headers, json=body, timeout=180)
            if r.status_code in (429, 500, 502, 503, 529):
                raise RuntimeError(f"retryable {r.status_code}")
            r.raise_for_status()
            return "".join(
                b.get("text", "") for b in r.json()["content"]
                if b.get("type") == "text"
            )
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(2 ** attempt * 2)
    raise RuntimeError(f"anthropic call failed: {last_error}")


def parse_json(raw: str) -> dict[str, Any]:
    cleaned = re.sub(r"^```(?:json)?|```$", "", raw.strip(),
                     flags=re.MULTILINE).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON object found in: {raw[:200]}")
    return json.loads(cleaned[start:end + 1])


def extract_claims(query: str, url: str, content: str) -> list[dict[str, Any]]:
    if len(content) < 400:
        return []

    def call() -> list[dict[str, Any]]:
        prompt = (EXTRACT_PROMPT
                  .replace("{query}", query)
                  .replace("{content}", content[:45_000]))
        try:
            return parse_json(anthropic_call(prompt))["claims"]
        except Exception as exc:  # noqa: BLE001
            log(f"extract failed {url}: {exc}")
            return []

    return cached("claims", cache_key(query, url), call)


# --------------------------------------------------------------------------
# STAGE 4 - CLUSTERING
# --------------------------------------------------------------------------

CLUSTER_PROMPT = """You are consolidating claims collected from {n} different web \
pages that all rank for the same search query. Different pages phrase the same \
idea differently. Your job is to group claims that assert THE SAME THING.

Two claims belong together when a reader who already read one would learn \
nothing new from the other. Different numbers for the same metric are DIFFERENT \
claims. A general statement and a specific quantified version of it are \
DIFFERENT claims.

Do not force unrelated claims together. A cluster of one is a valid result and \
is in fact the most interesting output of this study.

Return ONLY valid JSON, no markdown fences:
{"clusters": [{"canonical": "neutral phrasing of the shared claim", "ids": [1, 7, 12]}]}

Every id below must appear in exactly one cluster.

CLAIMS:
{claims}"""


def cluster_claims(query: str, claims: list[dict[str, Any]],
                   n_pages: int) -> list[dict[str, Any]]:
    if not claims:
        return []

    def call() -> list[dict[str, Any]]:
        listing = "\n".join(f'{c["id"]}. {c["text"]}' for c in claims)
        prompt = (CLUSTER_PROMPT
                  .replace("{n}", str(n_pages))
                  .replace("{claims}", listing))
        try:
            return parse_json(anthropic_call(prompt, max_tokens=8000))["clusters"]
        except Exception as exc:  # noqa: BLE001
            log(f"cluster failed for '{query}': {exc}")
            return [{"canonical": c["text"], "ids": [c["id"]]} for c in claims]

    ids_sig = cache_key(*[c["text"] for c in claims])
    return cached("clusters", cache_key(query, ids_sig), call)


# --------------------------------------------------------------------------
# STAGE 5 - METRICS
# --------------------------------------------------------------------------

@dataclass
class PageResult:
    position: int
    url: str
    domain: str
    claim_count: int = 0
    unique_count: int = 0
    specific_count: int = 0
    unique_claims: list[str] = field(default_factory=list)

    @property
    def unique_share(self) -> float:
        return self.unique_count / self.claim_count if self.claim_count else 0.0


@dataclass
class QueryResult:
    query: str
    pages: list[PageResult]
    clusters: list[dict[str, Any]]
    total_instances: int
    consensus_threshold: int
    consensus_clusters: int
    consensus_instances: int
    singleton_clusters: int

    @property
    def repetition_rate(self) -> float:
        """Share of all claim instances that restate a consensus claim."""
        return (self.consensus_instances / self.total_instances
                if self.total_instances else 0.0)


def analyse_query(query: str) -> QueryResult | None:
    log(f"query: {query}")
    serp = fetch_serp(query)
    if not serp:
        return None

    pages = [PageResult(position=r["position"], url=r["url"],
                        domain=r["domain"]) for r in serp]

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        contents = list(pool.map(lambda r: fetch_content(r["url"]), serp))

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        per_page = list(pool.map(
            lambda pair: extract_claims(query, pair[0]["url"], pair[1]),
            zip(serp, contents),
        ))

    flat: list[dict[str, Any]] = []
    owner: dict[int, int] = {}   # claim id -> page index
    next_id = 1
    for page_idx, claims in enumerate(per_page):
        pages[page_idx].claim_count = len(claims)
        pages[page_idx].specific_count = sum(1 for c in claims
                                             if c.get("specific"))
        for c in claims:
            flat.append({"id": next_id, "text": c["text"]})
            owner[next_id] = page_idx
            next_id += 1

    live_pages = sum(1 for p in pages if p.claim_count)
    consensus_threshold = max(2, math.ceil(CONSENSUS_RATIO * live_pages))
    log(f"  {live_pages}/{len(pages)} pages readable, {len(flat)} claims, "
        f"consensus at {consensus_threshold}+ pages")

    clusters = cluster_claims(query, flat, live_pages)

    total_instances = 0
    consensus_clusters = 0
    consensus_instances = 0
    singleton_clusters = 0
    text_by_id = {c["id"]: c["text"] for c in flat}

    for cl in clusters:
        ids = [i for i in cl.get("ids", []) if i in owner]
        if not ids:
            continue
        page_idxs = {owner[i] for i in ids}
        total_instances += len(ids)

        if len(page_idxs) >= consensus_threshold:
            consensus_clusters += 1
            consensus_instances += len(ids)

        if len(page_idxs) == 1:
            singleton_clusters += 1
            idx = next(iter(page_idxs))
            pages[idx].unique_count += 1
            pages[idx].unique_claims.append(
                cl.get("canonical") or text_by_id.get(ids[0], "")
            )

    return QueryResult(
        query=query,
        pages=pages,
        clusters=clusters,
        total_instances=total_instances,
        consensus_threshold=consensus_threshold,
        consensus_clusters=consensus_clusters,
        consensus_instances=consensus_instances,
        singleton_clusters=singleton_clusters,
    )


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Rank correlation without numpy. Returns None when undefined."""
    n = len(xs)
    if n < 3:
        return None

    def ranks(vals: list[float]) -> list[float]:
        order = sorted(range(n), key=lambda i: vals[i])
        out = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                out[order[k]] = avg
            i = j + 1
        return out

    rx, ry = ranks(xs), ranks(ys)
    mx, my = statistics.mean(rx), statistics.mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return num / den if den else None


# --------------------------------------------------------------------------
# REPORTING
# --------------------------------------------------------------------------

def write_report(results: list[QueryResult]) -> None:
    OUT_DIR.mkdir(exist_ok=True)

    rates = [r.repetition_rate for r in results if r.total_instances]
    overall = statistics.mean(rates) if rates else 0.0

    all_pages = [p for r in results for p in r.pages if p.claim_count]
    positions = [float(p.position) for p in all_pages]
    uniques = [p.unique_share for p in all_pages]
    rho = spearman(positions, uniques)

    lines: list[str] = []
    lines.append("# Information gain measurement\n")
    lines.append(f"- Queries analysed: **{len(results)}**")
    lines.append(f"- Pages analysed: **{len(all_pages)}**")
    lines.append(f"- Claims extracted: **{sum(r.total_instances for r in results)}**")
    lines.append(
        f"- Mean repetition rate: **{overall:.1%}** "
        f"(share of claims restating something {CONSENSUS_RATIO:.0%}+ of the "
        f"readable top results already say)"
    )
    if rho is not None:
        lines.append(f"- Spearman rho, position vs unique share: **{rho:+.2f}**")
    lines.append("\n## Per query\n")
    lines.append("| Query | Claims | Consensus clusters | Singletons | Repetition |")
    lines.append("|---|---|---|---|---|")
    for r in results:
        lines.append(
            f"| {r.query} | {r.total_instances} | {r.consensus_clusters} "
            f"| {r.singleton_clusters} | {r.repetition_rate:.0%} |"
        )

    lines.append("\n## Pages carrying the most unique information\n")
    top = sorted(all_pages, key=lambda p: p.unique_share, reverse=True)[:20]
    lines.append("| Domain | Pos | Claims | Unique | Share |")
    lines.append("|---|---|---|---|---|")
    for p in top:
        lines.append(
            f"| {p.domain} | {p.position} | {p.claim_count} "
            f"| {p.unique_count} | {p.unique_share:.0%} |"
        )

    lines.append("\n## Sample unique claims\n")
    lines.append("These are the only things any single page said that no other "
                 "result said. This is what information gain looks like in "
                 "practice.\n")
    for p in top[:8]:
        if not p.unique_claims:
            continue
        lines.append(f"**{p.domain}** (position {p.position})\n")
        for claim in p.unique_claims[:4]:
            lines.append(f"- {claim}")
        lines.append("")

    (OUT_DIR / "report.md").write_text("\n".join(lines))

    rows = ["query,position,domain,url,claims,unique,unique_share,specific"]
    for r in results:
        for p in r.pages:
            rows.append(
                f'"{r.query}",{p.position},"{p.domain}","{p.url}",'
                f"{p.claim_count},{p.unique_count},{p.unique_share:.4f},"
                f"{p.specific_count}"
            )
    (OUT_DIR / "pages.csv").write_text("\n".join(rows))

    print("\n" + "=" * 60)
    print(f"MEAN REPETITION RATE: {overall:.1%}")
    if rho is not None:
        print(f"POSITION vs UNIQUE SHARE (rho): {rho:+.2f}")
    print("=" * 60)
    print(f"\nWritten: {OUT_DIR/'report.md'}, {OUT_DIR/'pages.csv'}")


# --------------------------------------------------------------------------
# SUBSCRIPTION MODE (no model bill): build paste-ready bundles, read results back
# --------------------------------------------------------------------------

def slugify(text: str) -> str:
    s = re.sub(r"[^\w]+", "-", text.strip().lower())
    return s.strip("-")[:60] or "query"


BUNDLE_INSTRUCTIONS = """\
You are analysing Google page-1 results for ONE keyword to measure information
gain. Below the line are the ranking pages, each labelled by its position and
domain, followed by its main text.

Do this:
1. For each page, extract up to 25 atomic claims. An atomic claim is a single,
   self-contained, checkable statement about the topic, rewritten in neutral,
   brand-free language, under 25 words. Skip marketing copy, navigation, author
   bios, and calls to action. Split compound sentences.
2. Cluster claims that say the same thing across pages. Two claims belong
   together only if a reader who saw one would learn nothing new from the other.
   Different numbers for the same metric are different claims. A general
   statement and its quantified version are different claims. Singleton clusters
   (one page only) are valid and are the most interesting output.
3. Return ONLY valid JSON, no markdown fences, in exactly this shape:
{"query": "<the keyword>",
 "pages": [{"position": 1, "domain": "...", "claim_count": 12}, ...],
 "clusters": [{"canonical": "neutral phrasing", "positions": [1, 3, 5]}, ...]}
"positions" = the unique page positions that assert the claim. Every extracted
claim must appear in exactly one cluster.

Save the JSON it returns as ./results/{slug}.json, then run:
    python information_gain.py --stage report
"""


def build_bundles() -> None:
    BUNDLE_DIR.mkdir(exist_ok=True)
    for q in QUERIES:
        serp = fetch_serp(q)
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            contents = list(pool.map(lambda r: fetch_content(r["url"]), serp))
        slug = slugify(q)
        parts = [BUNDLE_INSTRUCTIONS.replace("{slug}", slug),
                 f"\nKEYWORD: {q}\n", "=" * 70]
        readable = 0
        for r, c in zip(serp, contents):
            if len(c) < 400:
                continue
            readable += 1
            parts.append(f"\n[PAGE position={r['position']} domain={r['domain']}]\n")
            parts.append(c[:CONTENT_CHARS])
            parts.append("\n" + "=" * 70)
        path = BUNDLE_DIR / f"{slug}.txt"
        path.write_text("\n".join(parts))
        print(f"{q}: {readable}/{len(serp)} readable -> {path}")
    print(f"\nNext: paste each ./bundle/*.txt into your Claude/ChatGPT, save the "
          f"JSON reply to ./results/<same-name>.json, then run "
          f"`python information_gain.py --stage report`.")


def report_from_results() -> None:
    """Read the JSON you saved in ./results and compute the metrics."""
    files = sorted(RESULTS_DIR.glob("*.json"))
    if not files:
        sys.exit(f"no result files in {RESULTS_DIR}/. Run --stage bundle first, "
                 f"then save each model reply as ./results/<slug>.json")
    OUT_DIR.mkdir(exist_ok=True)

    lines = ["# Information gain measurement\n"]
    per_query_rows = []
    rates = []
    sections = []
    for f in files:
        d = json.loads(f.read_text())
        pages = [p for p in d.get("pages", []) if p.get("claim_count", 0) > 0]
        live = len(pages)
        if not live:
            continue
        thr = max(2, math.ceil(CONSENSUS_RATIO * live))
        dom = {p["position"]: p.get("domain", f"pos {p['position']}") for p in pages}
        total = cons_inst = 0
        consensus, uniques = [], {}
        for c in d.get("clusters", []):
            pos = sorted(set(c.get("positions", [])))
            if not pos:
                continue
            total += len(pos)
            if len(pos) >= thr:
                cons_inst += len(pos)
                consensus.append((len(pos), c.get("canonical", "")))
            if len(pos) == 1:
                uniques.setdefault(pos[0], []).append(c.get("canonical", ""))
        rep = cons_inst / total if total else 0.0
        rates.append(rep)
        singles = sum(len(v) for v in uniques.values())
        per_query_rows.append((d.get("query", f.stem), live, len(d.get("clusters", [])),
                               len(consensus), singles, rep))

        sec = [f"## {d.get('query', f.stem)}\n",
               f"{live} readable pages, {len(d.get('clusters', []))} clusters, "
               f"consensus at {thr}+ pages, repetition {rep:.0%}.\n"]
        sec.append("**Consensus (the coverage floor):**\n" if consensus
                   else "**No consensus. No claim is shared by "
                        f"{CONSENSUS_RATIO:.0%}+ of pages.**\n")
        for n, txt in sorted(consensus, reverse=True):
            sec.append(f"- ({n} pages) {txt}")
        sec.append("\n**Unique claims (one page only) = your opening:**\n")
        for pos in sorted(uniques):
            sec.append(f"- **{dom.get(pos, pos)}** (pos {pos}):")
            for cl in uniques[pos][:6]:
                sec.append(f"  - {cl}")
        sec.append("")
        sections.append("\n".join(sec))

    overall = statistics.mean(rates) if rates else 0.0
    lines.append(f"- Queries analysed: **{len(per_query_rows)}**")
    lines.append(f"- Mean repetition rate: **{overall:.1%}**\n")
    lines.append("| Query | Clusters | Consensus | Unique | Repetition |")
    lines.append("|---|---|---|---|---|")
    for q, live, ncl, nc, s, rep in per_query_rows:
        lines.append(f"| {q} | {ncl} | {nc} | {s} | {rep:.0%} |")
    lines.append("")
    report = "\n".join(lines) + "\n" + "\n".join(sections)
    (OUT_DIR / "report.md").write_text(report)
    print(f"MEAN REPETITION RATE: {overall:.1%}")
    print(f"Written: {OUT_DIR/'report.md'}")


# --------------------------------------------------------------------------

def main() -> None:
    global USE_CACHE

    ap = argparse.ArgumentParser()
    ap.add_argument("--stage",
                    choices=["serp", "content", "bundle", "report", "full"],
                    default="bundle")
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()
    USE_CACHE = not args.no_cache

    # DataForSEO is needed to pull SERPs and page text (everything except report).
    if args.stage != "report":
        for var in ("DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD"):
            if not os.environ.get(var):
                sys.exit(f"missing env var: {var}")
    # The paid API key is only needed for the automated API mode.
    if args.stage == "full" and not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("--stage full needs ANTHROPIC_API_KEY (or use --stage bundle for "
                 "the free subscription flow)")

    if args.stage == "bundle":
        build_bundles()
        return

    if args.stage == "report":
        report_from_results()
        return

    if args.stage == "serp":
        for q in QUERIES:
            serp = fetch_serp(q)
            print(f"{q}: {len(serp)} results")
            for r in serp:
                print(f"   {r['position']:>2}. {r['domain']}")
        return

    if args.stage == "content":
        total_pages = 0
        total_readable = 0
        for q in QUERIES:
            serp = fetch_serp(q)
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
                contents = list(pool.map(lambda r: fetch_content(r["url"]), serp))
            readable = sum(1 for c in contents if len(c) >= 400)
            total_pages += len(serp)
            total_readable += readable
            print(f"{q}: {readable}/{len(serp)} readable")
            for r, c in zip(serp, contents):
                print(f"   {r['position']:>2}. {r['domain']:<32} {len(c):>7} chars")
        print(f"\nTOTAL: {total_readable}/{total_pages} pages readable")
        return

    results = []
    for q in QUERIES:
        try:
            res = analyse_query(q)
            if res:
                results.append(res)
        except Exception as exc:  # noqa: BLE001
            log(f"query failed '{q}': {exc}")

    if results:
        write_report(results)


if __name__ == "__main__":
    main()
