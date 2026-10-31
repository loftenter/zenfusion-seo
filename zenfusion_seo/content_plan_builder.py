#!/usr/bin/env python3
"""
Content Plan Builder — standalone CLI
=====================================
Turns a seed keyword (or domain) into a clustered, prioritized content plan.

Pipeline (mirrors the dataforseo/content-plan-builder skill):
  1. expand_keywords()     — DataForSEO keyword_ideas (with KE cache first for vol/CPC)
  2. enrich                — bulk_keyword_difficulty + search_intent (DataForSEO)
  3. cluster_keywords()    — intent split, then theme grouping by token overlap
  4. prioritize_by_winnability() — volume × (1 - difficulty/100) × SERP opportunity
  5. map_page_type()        — live SERP intent mapping (collection / product / blog)
  6. generate_content_plan() — HTML (white SaaS theme) + XLSX + JSON

Usage:
  python3 content_plan_builder.py --seed 'water softener' --output /tmp/content_plan_test
  python3 content_plan_builder.py --seed softprowatersystems.com --type domain
  python3 content_plan_builder.py --seed 'water softener' --limit 300

Environment:
  DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD  in ~/.hermes/.env
  Optional KE cache: /tmp/ke_cache.json   ({"keyword": {vol,cpc,competition}})
"""

try:  # resolve google.com/goto passthrough URLs in SERP responses (see goto_resolver.py)
    from .goto_resolver import fix_goto_urls as _fix_goto
except ImportError:
    try:
        from goto_resolver import fix_goto_urls as _fix_goto
    except ImportError:
        def _fix_goto(data, endpoint=None):
            return data
import os
import re
import sys
import json
import time
import base64
import argparse
import datetime
from collections import defaultdict, Counter

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

# ---------------------------------------------------------------------------
# Config / credentials
# ---------------------------------------------------------------------------

DEFAULTS = {
    "location_name": "United States",
    "language_code": "en",
    "location_code": 2840,        # US (DataForSEO numeric code)
    "limit": 1000,
}

KE_CACHE_PATH = "/tmp/ke_cache.json"
ENV_PATH = os.path.expanduser("~/.hermes/.env")
SERP_OPP_DEFAULT = 1.0            # neutral multiplier when SERP not fetched

# ---------------------------------------------------------------------------
# .env + API plumbing
# ---------------------------------------------------------------------------

def load_env(path=ENV_PATH):
    """Populate os.environ from a flat KEY=VALUE .env file if not already set."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if k and k not in os.environ:
                os.environ[k] = v

def get_creds():
    login = os.environ.get("DATAFORSEO_LOGIN")
    password = os.environ.get("DATAFORSEO_PASSWORD")
    if not login or not password:
        load_env()
        login = os.environ.get("DATAFORSEO_LOGIN")
        password = os.environ.get("DATAFORSEO_PASSWORD")
    if not login or not password:
        sys.stderr.write("ERROR: DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD not found in env or ~/.hermes/.env\n")
        sys.exit(2)
    return login, password

def _auth_header():
    login, password = get_creds()
    raw = f"{login}:{password}".encode()
    return "Basic " + base64.b64encode(raw).decode()

def dfs_post(path, task, timeout=60, retries=2):
    """POST one task to a DataForSEO Labs/Ads endpoint; return items list or []."""
    if requests is None:
        sys.stderr.write("requests library not installed\n")
        return []
    url = "https://api.dataforseo.com/v3/" + path.lstrip("/")
    headers = {"Authorization": _auth_header(), "Content-Type": "application/json"}
    body = json.dumps([task]).encode()
    last_err = None
    for attempt in range(retries + 1):
        try:
            r = requests.post(url, data=body, headers=headers, timeout=timeout)
            data = _fix_goto(r.json(), path)
            if r.status_code == 401 or data.get("status_code") == 40101:
                sys.stderr.write("DataForSEO 401 — bad credentials.\n")
                return []
            if data.get("status_code") not in (20000, 20001):
                sys.stderr.write(f"DataForSEO {path} status {data.get('status_code')}: {data.get('status_message','')}\n")
                # retry once on transient
                if attempt < retries:
                    time.sleep(1.5)
                    continue
                return []
            tasks = data.get("tasks") or []
            if not tasks:
                return []
            res = tasks[0].get("result") or []
            if not res:
                return []
            return res[0].get("items") or []
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(1.5)
                continue
    sys.stderr.write(f"DataForSEO {path} request failed: {last_err}\n")
    return []

# ---------------------------------------------------------------------------
# KE cache (prepaid volume/CPC — use first)
# ---------------------------------------------------------------------------

_ke_cache = None
def load_ke_cache(path=KE_CACHE_PATH):
    global _ke_cache
    if _ke_cache is not None:
        return _ke_cache
    if os.path.exists(path):
        try:
            with open(path) as f:
                _ke_cache = json.load(f)
        except Exception as e:
            sys.stderr.write(f"KE cache load failed: {e}\n")
            _ke_cache = {}
    else:
        _ke_cache = {}
    return _ke_cache

def ke_lookup(keyword):
    """Return (volume, cpc) from KE cache if present, else (None, None)."""
    cache = load_ke_cache()
    rec = cache.get(keyword) or cache.get(keyword.lower())
    if not rec:
        return None, None
    vol = rec.get("vol") or rec.get("volume")
    cpc = rec.get("cpc")
    if isinstance(cpc, dict):
        cpc = cpc.get("value")
    return vol, cpc

# ---------------------------------------------------------------------------
# 1. expand_keywords
# ---------------------------------------------------------------------------

def expand_keywords(seed_keyword, limit=1000, location_name=None, language_code=None):
    """
    Expand a seed keyword into a full keyword list using DataForSEO
    keyword_ideas (and, if available, KE cache for volume/CPC).

    Returns: list of dicts {keyword, volume, cpc, competition, difficulty(None), intent(None)}
    """
    location_name = location_name or DEFAULTS["location_name"]
    language_code = language_code or DEFAULTS["language_code"]
    seed = seed_keyword.strip().lower()
    if not seed:
        return []

    task = {
        "keywords": [seed],
        "location_name": location_name,
        "language_code": language_code,
        "limit": limit,
        "order_by": ["relevance,desc", "keyword_info.search_volume,desc"],
        "use_improved_etv": True,
    }
    items = dfs_post("dataforseo_labs/google/keyword_ideas/live", task, timeout=90)

    out = []
    seen = set()
    for it in items:
        kw = (it.get("keyword") or "").strip().lower()
        if not kw or kw in seen:
            continue
        seen.add(kw)
        ki = it.get("keyword_info") or {}
        vol = ki.get("search_volume")
        cpc = ki.get("cpc")
        comp = ki.get("competition")
        # KE cache override (prepaid — preferred)
        ke_vol, ke_cpc = ke_lookup(kw)
        if ke_vol:
            vol = ke_vol
        if ke_cpc:
            cpc = ke_cpc
        out.append({
            "keyword": kw,
            "volume": vol or 0,
            "cpc": cpc,
            "competition": comp,
            "difficulty": None,
            "intent": None,
            "intent_probability": None,
        })
    return out

# ---------------------------------------------------------------------------
# Enrichment (difficulty + intent) — bulk
# ---------------------------------------------------------------------------

def enrich_keywords(keywords, location_name=None, language_code=None):
    """Fill in difficulty + intent for the deduped list (chunks of <=1000)."""
    if not keywords:
        return keywords
    location_name = location_name or DEFAULTS["location_name"]
    language_code = language_code or DEFAULTS["language_code"]
    kws = [k["keyword"] for k in keywords]

    # difficulty (bulk)
    diff = {}
    for i in range(0, len(kws), 1000):
        chunk = kws[i:i+1000]
        items = dfs_post("dataforseo_labs/google/bulk_keyword_difficulty/live", {
            "keywords": chunk,
            "location_name": location_name,
            "language_code": language_code,
        }, timeout=90)
        for it in items or []:
            k = (it.get("keyword") or "").strip().lower()
            if k:
                diff[k] = it.get("keyword_difficulty")

    # intent (search_intent — no location, language only)
    intent = {}
    for i in range(0, len(kws), 1000):
        chunk = kws[i:i+1000]
        items = dfs_post("dataforseo_labs/google/search_intent/live", {
            "keywords": chunk,
            "language_code": language_code,
        }, timeout=90)
        for it in items or []:
            k = (it.get("keyword") or "").strip().lower()
            if not k:
                continue
            ki = it.get("keyword_intent") or {}
            intent[k] = {
                "label": ki.get("label"),
                "probability": ki.get("probability"),
            }

    for k in keywords:
        kw = k["keyword"]
        if k["difficulty"] is None:
            k["difficulty"] = diff.get(kw)
        if k["intent"] is None:
            intn = intent.get(kw)
            if intn:
                k["intent"] = intn.get("label") or "informational"
                k["intent_probability"] = intn.get("probability")
        if k["difficulty"] is None:
            k["difficulty"] = 50  # neutral fallback
        if k["intent"] is None:
            k["intent"] = "informational"
    return keywords

# ---------------------------------------------------------------------------
# 2. cluster_keywords
# ---------------------------------------------------------------------------

STOP = set("""a an the of for and or to in on at with by from is are be how what
which best top vs versus buy cheap review reviews guide vs do does can will
should i you your my our we they it this that these those""".split())

def _tokens(kw):
    return set(re.findall(r"[a-z0-9]+", kw.lower())) - STOP

def cluster_keywords(keywords):
    """
    Group keywords into topic clusters: primary split by intent, then within
    each intent group by token-overlap similarity into nameable themes.

    Returns: list of clusters
      {name, intent, keywords:[...], keyword_count, total_volume, avg_difficulty, winnability}
    """
    if not keywords:
        return []

    # 1) primary split by intent
    by_intent = defaultdict(list)
    for k in keywords:
        by_intent[k.get("intent") or "informational"].append(k)

    clusters = []
    for intent, kws in by_intent.items():
        # 2) theme grouping via greedy token overlap
        #    sort by volume desc so head terms seed the clusters
        kws_sorted = sorted(kws, key=lambda x: x.get("volume") or 0, reverse=True)
        sub_clusters = []  # each: {tokens:set, members:list}
        for k in kws_sorted:
            toks = _tokens(k["keyword"])
            best_idx, best_score = -1, 0
            for idx, sc in enumerate(sub_clusters):
                inter = len(toks & sc["tokens"])
                union = len(toks | sc["tokens"]) or 1
                score = inter / union
                if score >= 0.34 and score > best_score:
                    best_idx, best_score = idx, score
            if best_idx >= 0:
                sub_clusters[best_idx]["members"].append(k)
                sub_clusters[best_idx]["tokens"] |= toks
            else:
                sub_clusters.append({"tokens": set(toks), "members": [k]})

        # name each sub-cluster from its most common non-stop head token(s)
        for sc in sub_clusters:
            members = sc["members"]
            if len(members) < 2:
                # try to merge tiny fragments into nearest larger sub-cluster
                continue
        # merge tiny sub-clusters (<3 members) into nearest larger one
        merged = []
        small = [sc for sc in sub_clusters if len(sc["members"]) < 3]
        big = [sc for sc in sub_clusters if len(sc["members"]) >= 3]
        if not big and sub_clusters:
            big = [sub_clusters[0]]
            small = sub_clusters[1:]
        for sc in small:
            toks = sc["tokens"]
            best_idx, best_score = -1, 0
            for idx, bsc in enumerate(big):
                inter = len(toks & bsc["tokens"])
                union = len(toks | bsc["tokens"]) or 1
                score = inter / union
                if score > best_score:
                    best_idx, best_score = idx, score
            if best_idx >= 0:
                big[best_idx]["members"].extend(sc["members"])
                big[best_idx]["tokens"] |= toks
            else:
                big.append(sc)

        for sc in big:
            members = sc["members"]
            if not members:
                continue
            # name: most frequent non-stop token + top volume keyword
            tok_freq = Counter()
            for m in members:
                tok_freq.update(_tokens(m["keyword"]))
            head = tok_freq.most_common(3)
            name_tokens = [t for t, _ in head]
            name = " ".join(name_tokens).title() if name_tokens else members[0]["keyword"].title()
            # de-dup name & keep readable
            name = name.replace("  ", " ").strip()
            total_vol = sum(m.get("volume") or 0 for m in members)
            diffs = [m.get("difficulty") or 0 for m in members]
            avg_diff = sum(diffs) / len(diffs) if diffs else 0
            clusters.append({
                "name": name,
                "intent": intent,
                "keywords": members,
                "keyword_count": len(members),
                "total_volume": total_vol,
                "avg_difficulty": round(avg_diff, 1),
                "winnability": round(100 - avg_diff, 1),
            })

    return clusters

# ---------------------------------------------------------------------------
# 3. prioritize_by_winnability
# ---------------------------------------------------------------------------

def prioritize_by_winnability(keywords, domain=None):
    """
    Score each keyword by winnability:
      score = volume * (1 - difficulty/100) * serp_opportunity
    Also attach a cluster-level priority score + tier.

    `keywords` here is the cluster list returned by cluster_keywords().
    Returns the cluster list with priority_score, tier, pillar, content_type, note
    added, sorted by priority_score desc.
    """
    if not keywords:
        return []
    max_vol = max((c["total_volume"] for c in keywords), default=1) or 1
    volumes = sorted(c["total_volume"] for c in keywords)
    median_vol = volumes[len(volumes)//2] if volumes else 0

    # optional SERP opportunity per cluster's top keyword
    import math
    for c in keywords:
        # log-normalised volume so one giant cluster doesn't swamp the rest
        vol_norm = math.log(1 + c["total_volume"]) / math.log(1 + max_vol)
        win = c["winnability"] / 100.0
        # SERP opportunity from map_page_type if present, else 1.0
        serp_opp = c.get("serp_opportunity", SERP_OPP_DEFAULT)
        c["priority_score"] = round(100 * vol_norm * win * serp_opp)
        # tier
        if c["avg_difficulty"] <= 30 and c["total_volume"] >= median_vol:
            tier = "Quick win"
        elif c["total_volume"] >= median_vol and c["avg_difficulty"] > 30:
            tier = "Strategic"
        else:
            tier = "Fill"
        c["tier"] = tier
        # pillar + content type from dominant intent
        c["pillar"] = _pillar_title(c)
        c["content_type"] = _content_type(c["intent"])
        c["note"] = _cluster_note(c)

    return sorted(keywords, key=lambda x: x["priority_score"], reverse=True)

def _pillar_title(cluster):
    top = sorted(cluster["keywords"], key=lambda k: k.get("volume") or 0, reverse=True)
    head = top[0]["keyword"] if top else cluster["name"]
    return head.title()

def _content_type(intent):
    return {
        "informational": "How-to guide / pillar",
        "commercial": "Comparison / buyer's guide",
        "transactional": "Landing page / product page",
        "navigational": "Brand / help page",
    }.get(intent or "informational", "How-to guide / pillar")

def _cluster_note(c):
    if c["tier"] == "Quick win":
        return f"Strong demand ({c['total_volume']:,}/mo) with low difficulty ({c['avg_difficulty']:.0f}) — move on this first."
    if c["tier"] == "Strategic":
        return f"High-value cluster ({c['total_volume']:,}/mo) but harder to win (diff {c['avg_difficulty']:.0f}); build authority first."
    return f"Supporting / long-tail cluster ({c['total_volume']:,}/mo) to round out topical coverage."

# ---------------------------------------------------------------------------
# 4. map_page_type  (SERP intent mapping per Shopify SOP)
# ---------------------------------------------------------------------------

def map_page_type(keyword, location_name=None, language_code=None):
    """
    Check the live Google SERP for `keyword` and classify the dominant page type
    a site should publish to rank: 'collection', 'product', 'blog', or 'mixed'.

    Heuristic (Shopify SEO SOP): look at the top organic results' URL paths and
    page types. Collection-heavy SERP => category/collection page; product-heavy
    => product page; blog/article-heavy => blog post.

    Returns: {'page_type': str, 'collection': int, 'product': int, 'blog': int,
              'serp_opportunity': float, 'sample_urls': [...]}
    """
    location_name = location_name or DEFAULTS["location_name"]
    language_code = language_code or DEFAULTS["language_code"]
    task = {
        "keyword": keyword,
        "location_name": location_name,
        "language_code": language_code,
        "device": "desktop",
        "os": "windows",
        "use_improved_etv": True,
    }
    items = dfs_post("serp/google/organic/live/advanced", task, timeout=60)

    counts = {"collection": 0, "product": 0, "blog": 0, "other": 0}
    samples = []
    for it in items or []:
        if it.get("type") != "organic":
            continue
        url = (it.get("url") or "").lower()
        title = (it.get("title") or "").lower()
        samples.append(url)
        if "/collections/" in url or "/category/" in url or "/c/" in url or "/shop/" in url:
            counts["collection"] += 1
        elif "/products/" in url or "/p/" in url or url.endswith(".product") or "product" in title:
            counts["product"] += 1
        elif "/blog" in url or "/post" in url or "/article" in url or "/news" in url or "/guide" in url or "how to" in title or "best" in title:
            counts["blog"] += 1
        else:
            counts["other"] += 1

    total = sum(counts.values()) or 1
    # dominant if >40% of classified results
    page_type = "mixed"
    for k, v in counts.items():
        if k == "other":
            continue
        if v / total > 0.40:
            page_type = k
            break

    # SERP opportunity: if SERP is collection/product dominated AND low brand-page
    # dominance, a new content page has more room. Heuristic multiplier in [0.7, 1.3].
    non_brand_share = (counts["blog"] + counts["collection"] + counts["product"]) / total
    serp_opp = round(0.7 + 0.6 * non_brand_share, 2)  # more non-brand = more opportunity
    serp_opp = max(0.7, min(1.3, serp_opp))

    return {
        "page_type": page_type,
        "collection": counts["collection"],
        "product": counts["product"],
        "blog": counts["blog"],
        "serp_opportunity": serp_opp,
        "sample_urls": samples[:10],
    }

# ---------------------------------------------------------------------------
# 5. generate_content_plan
# ---------------------------------------------------------------------------

def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "seed"

def _now():
    return datetime.datetime.now()

def generate_content_plan(seed, output_path, limit=1000, seed_type="topic",
                          domain=None, location_name=None, language_code=None,
                          fetch_serp=True):
    """Full content plan: HTML + XLSX + JSON."""
    os.makedirs(output_path, exist_ok=True)
    period = _now().strftime("%Y-%m")
    date_gen = _now().strftime("%Y-%m-%d")
    slug = _slug(seed) if seed_type != "domain" else _slug(domain or seed)

    print(f"[1/5] Expanding keywords for '{seed}' ...")
    keywords = expand_keywords(seed, limit=limit, location_name=location_name,
                               language_code=language_code)
    if not keywords:
        sys.stderr.write("No keywords returned by DataForSEO. Aborting.\n")
        return None
    print(f"    {len(keywords)} keywords expanded.")

    print("[2/5] Enriching with difficulty + intent ...")
    keywords = enrich_keywords(keywords, location_name=location_name,
                               language_code=language_code)

    print("[3/5] Clustering keywords ...")
    clusters = cluster_keywords(keywords)
    print(f"    {len(clusters)} clusters formed.")

    print("[4/5] Mapping SERP page types for top clusters ...")
    if fetch_serp:
        for c in clusters[:12]:  # cap SERP calls (cost)
            top_kw = sorted(c["keywords"], key=lambda k: k.get("volume") or 0, reverse=True)[0]["keyword"]
            try:
                pm = map_page_type(top_kw, location_name=location_name, language_code=language_code)
                c["serp_opportunity"] = pm["serp_opportunity"]
                c["page_type"] = pm["page_type"]
                c["serp_sample_urls"] = pm["sample_urls"]
            except Exception as e:
                sys.stderr.write(f"    SERP map failed for '{top_kw}': {e}\n")
                c["serp_opportunity"] = SERP_OPP_DEFAULT
                c["page_type"] = "mixed"
    else:
        for c in clusters:
            c["serp_opportunity"] = SERP_OPP_DEFAULT
            c["page_type"] = "mixed"

    print("[5/5] Prioritizing & building outputs ...")
    clusters = prioritize_by_winnability(clusters, domain=domain)
    clusters = _build_roadmap(clusters)

    # ---- assemble data JSON ----
    total_vol = sum(c["total_volume"] for c in clusters)
    quick_wins = sum(1 for c in clusters if c["tier"] == "Quick win")
    intent_mix = _intent_mix(keywords)

    payload = {
        "config": {
            "seed_type": seed_type,
            "seed_value": seed,
            "domain": domain,
            "location_name": location_name or DEFAULTS["location_name"],
            "language_code": language_code or DEFAULTS["language_code"],
            "universe_size": len(keywords),
            "limit": limit,
            "period_current": period,
            "date_generated": date_gen,
            "output_path": output_path,
        },
        "summary": {
            "total_keywords": len(keywords),
            "total_clusters": len(clusters),
            "total_volume": total_vol,
            "quick_win_clusters": quick_wins,
            "intent_mix": intent_mix,
            "narrative": _narrative(seed, keywords, clusters, quick_wins),
            "insight": _summary_insight(clusters, quick_wins),
        },
        "clusters": clusters,
    }

    json_path = os.path.join(output_path, f"{slug}_content_plan_{period}.json")
    html_path = os.path.join(output_path, f"{slug}_content_plan_{period}.html")
    xlsx_path = os.path.join(output_path, f"{slug}_content_plan_{period}.xlsx")

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    _write_html(payload, html_path)
    _write_xlsx(payload, xlsx_path)

    print(f"\nContent plan complete.")
    print(f"  JSON: {json_path}")
    print(f"  HTML: {html_path}")
    print(f"  XLSX: {xlsx_path}")
    print(f"  Headline: {len(keywords)} keywords -> {len(clusters)} clusters "
          f"({quick_wins} quick wins; top priority: {clusters[0]['name']} "
          f"score {clusters[0]['priority_score']})" if clusters else "")
    return payload

# ---- helpers for assembly ----

def _intent_mix(keywords):
    by = defaultdict(lambda: {"kw": 0, "vol": 0})
    for k in keywords:
        i = k.get("intent") or "informational"
        by[i]["kw"] += 1
        by[i]["vol"] += k.get("volume") or 0
    total_kw = len(keywords) or 1
    out = []
    for i, v in sorted(by.items(), key=lambda x: -x[1]["vol"]):
        out.append({"intent": i, "keywords": v["kw"], "volume": v["vol"],
                    "share": round(100 * v["kw"] / total_kw)})
    return out

def _narrative(seed, keywords, clusters, quick_wins):
    top = clusters[0] if clusters else None
    total_vol = sum(c["total_volume"] for c in clusters)
    line = f"The '{seed}' seed expanded into {len(keywords)} keywords representing "
    line += f"{total_vol:,} monthly searches, grouped into {len(clusters)} content clusters. "
    if top:
        line += f"The top opportunity is '{top['name']}' ({top['tier']}, priority {top['priority_score']}). "
    line += f"{quick_wins} quick-win cluster(s) can be attacked immediately for early momentum."
    return line

def _summary_insight(clusters, quick_wins):
    if not clusters:
        return "No clusters formed."
    top = clusters[0]
    return (f"Start with '{top['name']}' — {top['tier'].lower()}, priority {top['priority_score']}; "
            f"{quick_wins} quick-win clusters available.")

def _build_roadmap(clusters):
    """Split clusters into 2-3 phases by tier and assign articles."""
    quick = [c for c in clusters if c["tier"] == "Quick win"]
    strat = [c for c in clusters if c["tier"] == "Strategic"]
    fill = [c for c in clusters if c["tier"] == "Fill"]
    phases = []
    if quick:
        phases.append({"phase": "Phase 1 — Months 1-2", "theme": "Quick wins & foundational pillar",
                       "items": [_cluster_to_roadmap(c, "High") for c in quick]})
    if strat:
        phases.append({"phase": "Phase 2 — Months 3-4", "theme": "Strategic money terms",
                        "items": [_cluster_to_roadmap(c, "Medium") for c in strat]})
    if fill:
        phases.append({"phase": "Phase 3 — Months 5+", "theme": "Fill & long-tail",
                        "items": [_cluster_to_roadmap(c, "Low") for c in fill]})
    # attach roadmap phase back to clusters for xlsx join
    for ph in phases:
        for it in ph["items"]:
            for c in clusters:
                if c["name"] == it["cluster"]:
                    c["roadmap_phase"] = ph["phase"]
    payload_roadmap = {"phases": phases,
                       "insight": "Publish pillars before their supporting pages; "
                                  "cross-link within and across clusters."}
    # store on clusters for the writer
    return clusters

def _cluster_to_roadmap(c, priority):
    top = sorted(c["keywords"], key=lambda k: k.get("volume") or 0, reverse=True)
    pillar_title = c["pillar"]
    supporting = []
    for k in top[1:4]:
        supporting.append({
            "title": k["keyword"].title(),
            "role": "Supporting",
            "links_to": f"Link up to pillar '{pillar_title}'.",
        })
    articles = [{
        "title": pillar_title,
        "role": "Pillar",
        "links_to": f"Hub of cluster — link down to each supporting article; "
                    f"recommended page type: {c.get('page_type','mixed')}.",
    }] + supporting
    return {
        "cluster": c["name"],
        "content_type": c["content_type"],
        "priority": priority,
        "rationale": c["note"],
        "articles": articles,
    }

# ---------------------------------------------------------------------------
# HTML output (white SaaS theme)
# ---------------------------------------------------------------------------

HTML_TMPL = """<!doctype html>
<html lang=\"en\"><head><meta charset=\"utf-8\">
<title>Content Plan — {seed}</title>
<style>
  :root {{ --primary:#1F2937; --accent:#2563EB; --success:#16A34A; --warn:#D97706;
          --bg:#F3F4F6; --muted:#6B7280; --grid:#E5E7EB; --soft:#EFF6FF; }}
  * {{ box-sizing:border-box; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
         margin:0; color:var(--primary); background:#fff; line-height:1.5; }}
  .wrap {{ max-width:1100px; margin:0 auto; padding:32px; }}
  header {{ border-bottom:3px solid var(--accent); padding-bottom:16px; margin-bottom:24px; }}
  h1 {{ margin:0; font-size:28px; }}
  .meta {{ color:var(--muted); font-size:13px; margin-top:4px; }}
  h2 {{ color:var(--accent); border-bottom:1px solid var(--grid); padding-bottom:6px; margin-top:36px; }}
  .kpis {{ display:flex; gap:12px; flex-wrap:wrap; margin:16px 0; }}
  .kpi {{ background:var(--bg); border-top:3px solid var(--accent); border-radius:6px;
          padding:14px 18px; min-width:160px; flex:1; }}
  .kpi .label {{ font-size:11px; text-transform:uppercase; color:var(--muted); letter-spacing:.04em; }}
  .kpi .value {{ font-size:22px; font-weight:700; margin-top:4px; }}
  .kpi .sub {{ font-size:11px; color:var(--muted); }}
  table {{ width:100%; border-collapse:collapse; margin:12px 0; font-size:13px; }}
  th {{ background:var(--primary); color:#fff; text-align:left; padding:8px 10px; font-weight:600; }}
  td {{ padding:8px 10px; border-bottom:1px solid var(--grid); vertical-align:top; }}
  tr:nth-child(even) td {{ background:var(--bg); }}
  .tier {{ display:inline-block; padding:2px 8px; border-radius:10px; font-size:11px; font-weight:600; color:#fff; }}
  .tier-Quick\\ win {{ background:var(--success); }}
  .tier-Strategic {{ background:var(--accent); }}
  .tier-Fill {{ background:var(--muted); }}
  .cluster {{ border:1px solid var(--grid); border-radius:6px; margin:14px 0; overflow:hidden; }}
  .cluster-head {{ background:var(--primary); color:#fff; padding:10px 14px; display:flex; justify-content:space-between; }}
  .cluster-head .name {{ font-weight:600; font-size:15px; }}
  .cluster-meta {{ background:var(--bg); padding:8px 14px; font-size:12px; color:var(--muted);
                   display:flex; gap:18px; flex-wrap:wrap; }}
  .cluster-meta b {{ color:var(--primary); }}
  .pill {{ background:var(--soft); border-left:3px solid var(--accent); padding:8px 12px;
          font-size:13px; margin:10px 14px; border-radius:0 4px 4px 0; }}
  .insight {{ background:var(--soft); border-left:3px solid var(--accent); padding:10px 14px;
             font-style:italic; margin:16px 0; border-radius:0 4px 4px 0; }}
  .phase {{ border:1px solid var(--grid); border-radius:6px; margin:14px 0; }}
  .phase-head {{ background:var(--accent); color:#fff; padding:8px 14px; font-weight:600; }}
  .phase-item {{ padding:10px 14px; border-bottom:1px solid var(--grid); }}
  .phase-item:last-child {{ border-bottom:none; }}
  .rationale {{ font-size:12px; color:var(--muted); margin-top:4px; }}
  footer {{ margin-top:40px; padding-top:16px; border-top:1px solid var(--grid);
           color:var(--muted); font-size:11px; text-align:center; }}
  code {{ background:var(--bg); padding:1px 4px; border-radius:3px; font-size:12px; }}
</style></head><body><div class=\"wrap\">
{body}
<footer>Generated {date_generated} • DataForSEO + KE data • White-label content plan</footer>
</div></body></html>"""

def _tier_class(tier):
    return "tier-" + (tier or "Fill").replace(" ", "\\ ").replace("'", "")

def _write_html(payload, path):
    cfg = payload["config"]
    s = payload["summary"]
    clusters = payload["clusters"]

    seed_val = cfg["seed_value"]
    period = cfg["period_current"]
    loc = cfg.get("location_name", "")
    lang = cfg.get("language_code", "")
    univ = cfg["universe_size"]
    date_gen = cfg["date_generated"]

    body = []
    body.append("<header><h1>Content Plan — " + seed_val + "</h1>")
    body.append("<div class='meta'>" + period + " • " + loc + " • " + lang +
                " • " + str(univ) + " keywords</div></header>")

    # KPIs
    body.append("<div class='kpis'>")
    total_vol_str = "{:,}".format(s["total_volume"])
    for label, val, sub in [
        ("Keywords", str(s["total_keywords"]), "in universe"),
        ("Clusters", str(s["total_clusters"]), "content themes"),
        ("Total Demand", total_vol_str, "monthly searches"),
        ("Quick Wins", str(s["quick_win_clusters"]), "low-difficulty clusters"),
    ]:
        body.append("<div class='kpi'><div class='label'>" + label + "</div>"
                    "<div class='value'>" + val + "</div>"
                    "<div class='sub'>" + sub + "</div></div>")
    body.append("</div>")

    # narrative
    body.append("<p>" + s["narrative"] + "</p>")
    body.append("<div class='insight'>" + s["insight"] + "</div>")

    # intent mix
    if s.get("intent_mix"):
        body.append("<h2>Demand by Search Intent</h2><table><tr><th>Intent</th><th>Keywords</th>"
                    "<th>Total Volume</th><th>Share</th></tr>")
        for m in s["intent_mix"]:
            body.append("<tr><td><b>" + m["intent"].title() + "</b></td><td>" + str(m["keywords"]) + "</td>"
                        "<td>" + "{:,}".format(m["volume"]) + "</td><td>" + str(m["share"]) + "%</td></tr>")
        body.append("</table>")

    # clusters
    body.append("<h2>Topic Clusters</h2>")
    for i, c in enumerate(clusters, 1):
        tier_cls = _tier_class(c["tier"])
        page_type = c.get("page_type", "mixed")
        body.append("<div class='cluster'>")
        body.append("<div class='cluster-head'><span class='name'>" + str(i) + ". " + c["name"] + "</span>"
                    "<span class='tier " + tier_cls + "'>" + c["tier"] + "</span></div>")
        body.append("<div class='cluster-meta'>"
                    "<span><b>Intent:</b> " + c["intent"].title() + "</span>"
                    "<span><b>Content type:</b> " + c["content_type"] + "</span>"
                    "<span><b>Volume:</b> " + "{:,}".format(c["total_volume"]) + "/mo</span>"
                    "<span><b>Avg difficulty:</b> " + str(round(c["avg_difficulty"])) + "</span>"
                    "<span><b>Winnability:</b> " + str(round(c["winnability"])) + "%</span>"
                    "<span><b>Priority:</b> " + str(c["priority_score"]) + "</span>"
                    "<span><b>SERP page type:</b> " + page_type + "</span>"
                    "</div>")
        body.append("<div class='pill'><b>Pillar:</b> " + c["pillar"] + "</div>")
        # keyword table
        kws = sorted(c["keywords"], key=lambda k: k.get("volume") or 0, reverse=True)[:12]
        body.append("<table><tr><th>Keyword</th><th>Intent</th><th>Volume</th>"
                    "<th>Difficulty</th></tr>")
        for k in kws:
            kw_intent = k.get("intent", "")
            kw_vol = k.get("volume", 0)
            kw_diff = k.get("difficulty", "—")
            body.append("<tr><td>" + k["keyword"] + "</td><td>" + kw_intent.title() + "</td>"
                        "<td>" + "{:,}".format(kw_vol) + "</td><td>" + str(kw_diff) + "</td></tr>")
        if len(c["keywords"]) > 12:
            extra = len(c["keywords"]) - 12
            body.append("<tr><td colspan=4 style='color:var(--muted)'>+ " + str(extra) +
                        " more — see XLSX</td></tr>")
        body.append("</table>")
        body.append("<div class='insight'>" + c["note"] + "</div>")
        body.append("</div>")

    # priority matrix
    body.append("<h2>Priority Matrix</h2><table><tr><th>#</th><th>Cluster</th>"
                "<th>Tier</th><th>Volume</th><th>Avg Difficulty</th>"
                "<th>Winnability</th><th>Priority</th></tr>")
    for i, c in enumerate(clusters, 1):
        tier_cls = _tier_class(c["tier"])
        body.append("<tr><td>" + str(i) + "</td><td><b>" + c["name"] + "</b></td>"
                    "<td><span class='tier " + tier_cls + "'>" + c["tier"] + "</span></td>"
                    "<td>" + "{:,}".format(c["total_volume"]) + "</td>"
                    "<td>" + str(round(c["avg_difficulty"])) + "</td>"
                    "<td>" + str(round(c["winnability"])) + "%</td>"
                    "<td><b>" + str(c["priority_score"]) + "</b></td></tr>")
    body.append("</table>")

    # roadmap
    body.append("<h2>Content Roadmap</h2>")
    phases = _roadmap_from_clusters(clusters)
    for ph in phases:
        body.append("<div class='phase'><div class='phase-head'>" + ph["phase"] + " — " + ph["theme"] + "</div>")
        for it in ph["items"]:
            body.append("<div class='phase-item'><b>" + it["cluster"] + "</b> "
                        "(" + it["content_type"] + ") — priority " + it["priority"])
            body.append("<table><tr><th>Article / Page</th><th>Role</th><th>Internal Linking</th></tr>")
            for a in it["articles"]:
                title_html = "<b>" + a["title"] + "</b>" if a["role"] == "Pillar" else a["title"]
                body.append("<tr><td>" + title_html + "</td>"
                            "<td>" + a["role"] + "</td><td>" + a["links_to"] + "</td></tr>")
            body.append("</table>")
            body.append("<div class='rationale'><b>Why now:</b> " + it["rationale"] + "</div></div>")
        body.append("</div>")

    html = HTML_TMPL.format(
        seed=seed_val,
        date_generated=date_gen,
        body="".join(body),
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)

def _roadmap_from_clusters(clusters):
    quick = [c for c in clusters if c["tier"] == "Quick win"]
    strat = [c for c in clusters if c["tier"] == "Strategic"]
    fill = [c for c in clusters if c["tier"] == "Fill"]
    phases = []
    if quick:
        phases.append({"phase": "Phase 1 — Months 1-2", "theme": "Quick wins & foundational pillar",
                       "items": [_cluster_to_roadmap(c, "High") for c in quick]})
    if strat:
        phases.append({"phase": "Phase 2 — Months 3-4", "theme": "Strategic money terms",
                        "items": [_cluster_to_roadmap(c, "Medium") for c in strat]})
    if fill:
        phases.append({"phase": "Phase 3 — Months 5+", "theme": "Fill & long-tail",
                        "items": [_cluster_to_roadmap(c, "Low") for c in fill]})
    return phases

# ---------------------------------------------------------------------------
# XLSX output
# ---------------------------------------------------------------------------

def _write_xlsx(payload, path):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    cfg = payload["config"]
    clusters = payload["clusters"]
    phases = _roadmap_from_clusters(clusters)
    phase_of = {}
    for ph in phases:
        for it in ph["items"]:
            phase_of[it["cluster"]] = ph["phase"]

    wb = Workbook()

    def style_header(ws, n):
        for c in range(1, n + 1):
            cell = ws.cell(row=1, column=c)
            cell.fill = PatternFill("solid", fgColor="FF1F2937")
            cell.font = Font(bold=True, color="FFFFFFFF")
            cell.alignment = Alignment(vertical="center")
        ws.freeze_panes = "A2"

    # Sheet 1 — Keywords
    ws = wb.active
    ws.title = "Keywords"
    heads = ["Keyword", "Cluster", "Intent", "Search Volume", "Difficulty",
             "Winnability", "Priority Tier", "Suggested Pillar", "Content Type",
             "SERP Page Type", "Roadmap Phase"]
    ws.append(heads)
    for c in clusters:
        for k in c["keywords"]:
            ws.append([k["keyword"], c["name"], (k.get("intent") or "").title(),
                       k.get("volume") or 0, k.get("difficulty"),
                       round(c["winnability"]), c["tier"], c["pillar"],
                       c["content_type"], c.get("page_type", "mixed"),
                       phase_of.get(c["name"], "")])
    style_header(ws, len(heads))

    # Sheet 2 — Clusters
    ws2 = wb.create_sheet("Clusters")
    heads2 = ["Cluster", "Intent", "Tier", "Priority Score", "Keywords",
              "Total Volume", "Avg Difficulty", "Winnability", "Suggested Pillar",
              "Content Type", "SERP Page Type", "Takeaway"]
    ws2.append(heads2)
    for c in clusters:
        ws2.append([c["name"], c["intent"].title(), c["tier"], c["priority_score"],
                    c["keyword_count"], c["total_volume"], c["avg_difficulty"],
                    c["winnability"], c["pillar"], c["content_type"],
                    c.get("page_type", "mixed"), c["note"]])
    style_header(ws2, len(heads2))

    # Sheet 3 — Roadmap
    ws3 = wb.create_sheet("Roadmap")
    heads3 = ["Phase", "Theme", "Cluster", "Article / Page", "Role",
              "Content Type", "Priority", "Internal Linking", "Rationale"]
    ws3.append(heads3)
    for ph in phases:
        for it in ph["items"]:
            first = True
            for a in it["articles"]:
                ws3.append([ph["phase"], ph["theme"], it["cluster"], a["title"],
                             a["role"], it["content_type"], it["priority"],
                             a["links_to"], it["rationale"] if first else ""])
                first = False
    style_header(ws3, len(heads3))

    wb.save(path)

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Build a clustered, prioritized content plan from a seed keyword or domain.")
    ap.add_argument("--seed", required=True, help="Seed keyword or domain")
    ap.add_argument("--type", default="topic", choices=["topic", "domain"],
                    help="Seed type (default: topic)")
    ap.add_argument("--output", default="/tmp/content_plan_test", help="Output directory")
    ap.add_argument("--limit", type=int, default=DEFAULTS["limit"], help="Max keywords to expand (default 1000)")
    ap.add_argument("--location", default=DEFAULTS["location_name"], help="Location name (default United States)")
    ap.add_argument("--language", default=DEFAULTS["language_code"], help="Language code (default en)")
    ap.add_argument("--no-serp", action="store_true", help="Skip live SERP page-type mapping (saves credits)")
    args = ap.parse_args()

    load_env()
    seed = args.seed
    domain = None
    if args.type == "domain":
        domain = re.sub(r"^https?://", "", seed).lstrip("www.").rstrip("/")
        seed = domain

    generate_content_plan(
        seed=seed,
        output_path=args.output,
        limit=args.limit,
        seed_type=args.type,
        domain=domain,
        location_name=args.location,
        language_code=args.language,
        fetch_serp=not args.no_serp,
    )

if __name__ == "__main__":
    main()
