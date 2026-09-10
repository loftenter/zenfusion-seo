#!/usr/bin/env python3
"""
Zenfusion Multi-Domain Link Intersect Analyzer

Finds domains linking to 3+ competitors but NOT the client — a prioritized
list of high-probability link-building targets. Fetches referring domains for
the client and each competitor via DataForSEO backlinks/referring_domains/live,
computes the multi-way intersect, scores each opportunity by competitor
overlap count and Majestic Million rank, then renders a white SaaS-themed
HTML report with summary cards, an opportunity table, and recommendations.

Usage:
  python3 multi_domain_link_intersect.py \
      --client softprowatersystems.com \
      --competitors aquasana.com,culligan.com,springwellwater.com \
      --output /tmp/test_multi_intersect

DataForSEO credentials live in ~/.hermes/.env
(DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD). The Majestic Million index lives at
~/.hermes/audits/factor-engine-data/majestic_million_index.json
(domain -> GlobalRank). Both are optional at runtime — the script degrades
gracefully when the Majestic index is absent.

Output: <output_dir>/index.html (plus report_data.json) — noindex/nofollow.
"""

import argparse
import base64
import json
import os
import sys
import urllib.request
from datetime import datetime
from html import escape

DATAFORSEO_REFERRING_DOMAINS_URL = (
    "https://api.dataforseo.com/v3/backlinks/referring_domains/live"
)
MAJESTIC_INDEX_PATH = os.path.expanduser(
    "~/.hermes/audits/factor-engine-data/majestic_million_index.json"
)
ENV_PATH = os.path.expanduser("~/.hermes/.env")


# === CREDENTIALS & DATA LOAD =================================================
def get_creds():
    """Load DataForSEO credentials from ~/.hermes/.env."""
    login, password = "", ""
    if not os.path.exists(ENV_PATH):
        return login, password
    with open(ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if line.startswith("DATAFORSEO_LOGIN="):
                login = line.split("=", 1)[1].strip()
            elif line.startswith("DATAFORSEO_PASSWORD="):
                password = line.split("=", 1)[1].strip()
    return login, password


def load_majestic_index():
    """Load the Majestic Million index (domain -> rank int). Returns {} if absent."""
    if not os.path.exists(MAJESTIC_INDEX_PATH):
        return {}
    try:
        with open(MAJESTIC_INDEX_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


# === DATAFORSEO API ==========================================================
def fetch_referring_domains(domain, creds, limit=1000):
    """Fetch referring domains for a target via DataForSEO backlinks/referring_domains/live.

    Returns a list of dicts: {domain, backlinks, first_seen, last_seen}.
    Degrades to [] on any error.
    """
    login, password = creds
    if not login or not password:
        print(f"  [warn] no DataForSEO credentials — skipping {domain}")
        return []

    auth = base64.b64encode(f"{login}:{password}".encode()).decode()
    payload = json.dumps(
        [
            {
                "target": domain,
                "main_domain": domain,
                "search_mode": "as_is",
                "limit": limit,
            }
        ]
    ).encode()

    req = urllib.request.Request(
        DATAFORSEO_REFERRING_DOMAINS_URL,
        data=payload,
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        print(f"  [error] DataForSEO request failed for {domain}: {e}")
        return []

    if not data.get("tasks"):
        return []
    task = data["tasks"][0]
    if task.get("status_code") not in (20000, None):
        print(
            f"  [warn] DataForSEO task status {task.get('status_code')} "
            f"for {domain}: {task.get('status_message', '')}"
        )
    result = task.get("result") or []
    if not result:
        return []
    items = result[0].get("items", []) or []

    out = []
    for item in items:
        d = (item.get("domain") or "").lower().strip()
        if not d:
            continue
        out.append(
            {
                "domain": d,
                "backlinks": item.get("backlinks", 0) or 0,
                "first_seen": (item.get("first_seen") or "")[:10],
                "last_seen": (item.get("last_seen") or "")[:10],
            }
        )
    return out


# === INTERSECTION LOGIC ======================================================
def normalize_domain(d):
    """Normalize a raw domain string for comparison."""
    d = (d or "").lower().strip()
    d = d.replace("https://", "").replace("http://", "")
    d = d.split("/")[0].split(":")[0]
    if d.startswith("www."):
        d = d[4:]
    return d


def build_domain_sets(client_domain, competitor_domains, creds):
    """Fetch referring-domain sets for client + each competitor.

    Returns (client_set, competitor_sets_dict, raw_items_dict).
    """
    print("\n[1] Fetching referring domains via DataForSEO…")
    client_items = fetch_referring_domains(client_domain, creds)
    client_set = {it["domain"] for it in client_items}
    print(
        f"  {client_domain}: {len(client_set):,} referring domains "
        f"(raw items: {len(client_items):,})"
    )

    competitor_sets = {}
    raw_items = {client_domain: client_items}
    for comp in competitor_domains:
        items = fetch_referring_domains(comp, creds)
        s = {it["domain"] for it in items}
        competitor_sets[comp] = s
        raw_items[comp] = items
        print(
            f"  {comp}: {len(s):,} referring domains (raw items: {len(items):,})"
        )
    return client_set, competitor_sets, raw_items


def find_opportunities(client_set, competitor_sets, majestic_db):
    """Find domains in 3+ competitor sets but NOT the client set.

    Returns a list of opportunity dicts sorted by score desc:
      {domain, competitor_count, competitors, majestic_rank, score}
    """
    # Map each referring domain -> set of competitors it links to
    domain_to_competitors = {}
    for comp, ref_set in competitor_sets.items():
        for d in ref_set:
            domain_to_competitors.setdefault(d, set()).add(comp)

    opps = []
    for domain, comps in domain_to_competitors.items():
        if len(comps) < 3:
            continue
        if domain in client_set:
            continue
        comp_list = sorted(comps)
        comp_count = len(comp_list)

        # Majestic Million rank (lower = stronger). Absent = None.
        majestic_rank = majestic_db.get(domain)
        if majestic_rank is None:
            majestic_rank = None

        # Score: competitor overlap (0-60 pts) + Majestic rank bonus (0-40 pts).
        # Overlap: 3 competitors = 30, each extra +10 (capped 60).
        overlap_pts = min(30 + (comp_count - 3) * 10, 60)
        # Majestic: rank 1 -> 40 pts, rank 1,000,000 -> ~0 pts.
        if majestic_rank:
            majestic_pts = max(0.0, 40.0 * (1.0 - min(majestic_rank, 1_000_000) / 1_000_000))
        else:
            majestic_pts = 0.0
        score = round(overlap_pts + majestic_pts, 1)

        opps.append(
            {
                "domain": domain,
                "competitor_count": comp_count,
                "competitors": comp_list,
                "majestic_rank": majestic_rank,
                "score": score,
            }
        )

    opps.sort(key=lambda o: (-o["score"], o["domain"]))
    return opps


def generate_recommendations(opps, client_domain, competitor_domains):
    """Produce 3-5 actionable recommendation strings from the opportunities."""
    if not opps:
        return [
            "No domains meet the 3+ competitor threshold right now. "
            "Consider expanding the competitor set or re-running after the next "
            "DataForSEO backlink index refresh."
        ]

    top = opps[0]
    recs = []
    recs.append(
        f"<strong>Pursue the top target first</strong> — <code>{escape(top['domain'])}</code> "
        f"links to {top['competitor_count']} of {len(competitor_domains)} competitors "
        f"(score {top['score']:.1f}) but not to {escape(client_domain)}. Start outreach here."
    )

    in_majestic = [o for o in opps if o["majestic_rank"]]
    if in_majestic:
        strongest = in_majestic[0]
        recs.append(
            f"<strong>High-authority priority</strong> — <code>{escape(strongest['domain'])}</code> "
            f"sits at Majestic Million rank #{strongest['majestic_rank']:,}. A single dofollow link "
            f"here would do more for {escape(client_domain)}'s domain authority than several "
            f"lower-ranked referrals."
        )
    else:
        recs.append(
            "None of the intersecting domains currently appear in the Majestic Million index. "
            "Prioritize by competitor overlap count instead and revisit when Majestic refreshes."
        )

    top_5 = opps[:5]
    domains_list = ", ".join(f"<code>{escape(o['domain'])}</code>" for o in top_5)
    recs.append(
        f"<strong>Batch outreach</strong> — the top 5 targets are {domains_list}. "
        "Group outreach by site type (review aggregators, forums, blogs) to personalize "
        "pitches efficiently."
    )

    overlap_4 = [o for o in opps if o["competitor_count"] >= 4]
    if overlap_4:
        recs.append(
            f"<strong>Broadest consensus</strong> — {len(overlap_4)} domain(s) link to 4+ "
            f"competitors. These are the strongest signals of niche-relevance; prioritize "
            f"them even if their Majestic rank is modest."
        )

    recs.append(
        f"<strong>Track acquisition</strong> — re-run this report after each outreach cycle. "
        f"Domains that move from the opportunity list into {escape(client_domain)}'s referring "
        "set confirm successful acquisition."
    )
    return recs


# === HTML REPORT =============================================================
def build_summary_cards(client_domain, client_ref_count, competitor_ref_counts,
                        total_opps, in_majestic_count, top_score):
    """Return HTML for the summary card row."""
    cards = [
        ("Client Domain", escape(client_domain), ""),
        ("Client Referring Domains", f"{client_ref_count:,}", ""),
        ("Opportunities (3+ competitors)", f"{total_opps:,}", "accent"),
        ("In Majestic Million", f"{in_majestic_count:,}", ""),
        ("Top Opportunity Score", f"{top_score:.1f}", "accent"),
    ]
    comp_count = len(competitor_ref_counts)
    cards.append(
        ("Competitors Analyzed", str(comp_count), "")
    )

    def card_html(label, value, extra_class):
        cls = "card" + (f" card-{extra_class}" if extra_class else "")
        return f"""
        <div class="{cls}">
          <div class="card-value">{value}</div>
          <div class="card-label">{label}</div>
        </div>"""

    return "\n".join(card_html(l, v, c) for l, v, c in cards)


def build_opportunity_rows(opps, competitor_domains):
    """Return HTML <tr> rows for the opportunity table."""
    rows = []
    for i, o in enumerate(opps, 1):
        maj = f"#{o['majestic_rank']:,}" if o["majestic_rank"] else "—"
        comps = ", ".join(escape(c) for c in o["competitors"])
        score = o["score"]
        if score >= 60:
            score_cls = "score-high"
        elif score >= 40:
            score_cls = "score-mid"
        else:
            score_cls = "score-low"
        rows.append(f"""
        <tr>
          <td class="rank">{i}</td>
          <td class="domain"><a href="https://{escape(o['domain'])}" target="_blank" rel="noopener noindex nofollow">{escape(o['domain'])}</a></td>
          <td class="num">{o['competitor_count']}</td>
          <td class="competitors">{comps}</td>
          <td class="num">{maj}</td>
          <td class="score {score_cls}">{o['score']:.1f}</td>
        </tr>""")
    return "\n".join(rows)


def generate_html_report(
    client_domain,
    competitor_domains,
    client_ref_count,
    competitor_ref_counts,
    opps,
    recommendations,
    generated_at,
):
    """Render the full white SaaS-themed HTML report."""
    in_majestic_count = sum(1 for o in opps if o["majestic_rank"])
    top_score = opps[0]["score"] if opps else 0.0
    total_opps = len(opps)

    summary_cards = build_summary_cards(
        client_domain,
        client_ref_count,
        competitor_ref_counts,
        total_opps,
        in_majestic_count,
        top_score,
    )
    table_rows = build_opportunity_rows(opps, competitor_domains)

    rec_html = "\n".join(
        f"<li>{rec}</li>" for rec in recommendations
    )

    comp_ref_html = "".join(
        f"<div class='comp-line'><code>{escape(c)}</code> — {cnt:,} referring domains</div>"
        for c, cnt in competitor_ref_counts.items()
    )

    table_body = (
        table_rows
        if table_rows
        else "<tr><td colspan='6' class='empty'>No domains meet the 3+ competitor threshold.</td></tr>"
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex, nofollow">
<title>Multi-Domain Link Intersect: {escape(client_domain)}</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
    background: #f8f9fb;
    color: #1a1d28;
    line-height: 1.6;
    padding: 20px 40px 60px;
  }}
  a {{ color: #2563eb; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  code {{ background: #eef2ff; padding: 1px 6px; border-radius: 4px; font-size: 0.92em; color: #3730a3; }}
  h1 {{ font-size: 26px; margin-bottom: 4px; color: #0f172a; }}
  h2 {{ font-size: 19px; margin: 32px 0 12px; color: #0f172a; }}
  .subtitle {{ color: #64748b; font-size: 14px; margin-bottom: 24px; }}
  .meta {{ color: #64748b; font-size: 13px; margin-bottom: 20px; }}

  .cards {{ display: flex; flex-wrap: wrap; gap: 14px; margin-bottom: 28px; }}
  .card {{
    background: #fff; border: 1px solid #e2e8f0; border-radius: 10px;
    padding: 18px 22px; min-width: 160px; flex: 1 1 160px;
    box-shadow: 0 1px 2px rgba(15,23,42,0.04);
  }}
  .card-accent {{ border-top: 3px solid #2563eb; }}
  .card-value {{ font-size: 26px; font-weight: 700; color: #0f172a; }}
  .card-label {{ font-size: 12px; color: #64748b; text-transform: uppercase; letter-spacing: 0.04em; margin-top: 4px; }}

  .section {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 10px; padding: 20px 24px; margin-bottom: 24px; }}
  .comp-line {{ font-size: 14px; color: #334155; margin-bottom: 6px; }}

  table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
  th {{ text-align: left; padding: 10px 12px; border-bottom: 2px solid #e2e8f0; color: #475569; font-size: 12px; text-transform: uppercase; letter-spacing: 0.03em; }}
  td {{ padding: 10px 12px; border-bottom: 1px solid #f1f5f9; vertical-align: top; }}
  tr:hover td {{ background: #f8fafc; }}
  td.rank {{ color: #94a3b8; font-weight: 600; width: 36px; }}
  td.domain a {{ font-weight: 600; color: #1e40af; }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums; color: #334155; }}
  td.competitors {{ font-size: 12px; color: #64748b; max-width: 280px; }}
  td.empty {{ text-align: center; color: #94a3b8; padding: 32px; }}
  .score {{ font-weight: 700; text-align: right; }}
  .score-high {{ color: #16a34a; }}
  .score-mid {{ color: #ca8a04; }}
  .score-low {{ color: #64748b; }}

  ol.recs {{ padding-left: 20px; }}
  ol.recs li {{ margin-bottom: 10px; font-size: 14px; color: #334155; }}

  .footer {{ margin-top: 32px; font-size: 12px; color: #94a3b8; text-align: center; }}
</style>
</head>
<body>
  <h1>Multi-Domain Link Intersect</h1>
  <div class="subtitle">Domains linking to 3+ competitors but not the client — prioritized link-building targets.</div>
  <div class="meta">
    Client: <code>{escape(client_domain)}</code> &middot;
    Competitors: {", ".join("<code>" + escape(c) + "</code>" for c in competitor_domains)} &middot;
    Generated: {escape(generated_at)}
  </div>

  <h2>Summary</h2>
  <div class="cards">
    {summary_cards}
  </div>

  <h2>Referring Domain Coverage</h2>
  <div class="section">
    <div class="comp-line"><strong>{escape(client_domain)}</strong> (client) — {client_ref_count:,} referring domains</div>
    {comp_ref_html}
  </div>

  <h2>Opportunity Table</h2>
  <div class="section">
    <table>
      <thead>
        <tr>
          <th>#</th>
          <th>Domain</th>
          <th>Competitors Linked</th>
          <th>Competitors</th>
          <th>Majestic Rank</th>
          <th>Score</th>
        </tr>
      </thead>
      <tbody>
        {table_body}
      </tbody>
    </table>
  </div>

  <h2>Recommendations</h2>
  <div class="section">
    <ol class="recs">
      {rec_html}
    </ol>
  </div>

  <div class="footer">
    Zenfusion Multi-Domain Link Intersect &middot; DataForSEO backlinks/referring_domains/live &middot; noindex/nofollow
  </div>
</body>
</html>"""
    return html


# === MAIN ===================================================================
def main():
    parser = argparse.ArgumentParser(
        description="Multi-domain link intersect analyzer (DataForSEO + Majestic Million)."
    )
    parser.add_argument("--client", required=True, help="Client domain (e.g. example.com)")
    parser.add_argument(
        "--competitors",
        required=True,
        help="Comma-separated competitor domains (min 3 for a useful intersect)",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Output directory for the HTML report + JSON data",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=1000,
        help="Max referring domains to fetch per target (default 1000)",
    )
    args = parser.parse_args()

    client_domain = normalize_domain(args.client)
    competitor_domains = [
        normalize_domain(c) for c in args.competitors.split(",") if c.strip()
    ]
    competitor_domains = [c for c in competitor_domains if c and c != client_domain]
    if len(competitor_domains) < 3:
        print(
            f"[warn] only {len(competitor_domains)} competitor(s) provided; "
            "the 3+ competitor threshold requires at least 3."
        )

    creds = get_creds()
    majestic_db = load_majestic_index()
    print(f"[init] Majestic Million index loaded: {len(majestic_db):,} domains")

    # Step 1: fetch referring domains
    client_set, competitor_sets, raw_items = build_domain_sets(
        client_domain, competitor_domains, creds
    )

    # Step 2: find intersect opportunities
    print("\n[2] Computing multi-domain intersect (3+ competitors, not client)…")
    opps = find_opportunities(client_set, competitor_sets, majestic_db)
    print(f"  Found {len(opps):,} opportunity domains")

    # Step 3: recommendations
    recommendations = generate_recommendations(opps, client_domain, competitor_domains)

    # Step 4: write report
    generated_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    competitor_ref_counts = {
        c: len(competitor_sets.get(c, set())) for c in competitor_domains
    }

    html = generate_html_report(
        client_domain,
        competitor_domains,
        len(client_set),
        competitor_ref_counts,
        opps,
        recommendations,
        generated_at,
    )

    out_dir = args.output
    os.makedirs(out_dir, exist_ok=True)
    html_path = os.path.join(out_dir, "index.html")
    with open(html_path, "w") as f:
        f.write(html)

    report_data = {
        "client_domain": client_domain,
        "competitor_domains": competitor_domains,
        "client_ref_count": len(client_set),
        "competitor_ref_counts": competitor_ref_counts,
        "opportunities": opps,
        "recommendations": recommendations,
        "generated_at": generated_at,
        "opportunity_count": len(opps),
    }
    json_path = os.path.join(out_dir, "report_data.json")
    with open(json_path, "w") as f:
        json.dump(report_data, f, indent=2)

    print(f"\n[done] Report written to: {html_path}")
    print(f"[done] JSON data:        {json_path}")
    print(f"[done] Opportunities:    {len(opps):,}")
    # Final machine-readable summary line
    print(
        json.dumps(
            {
                "report_path": html_path,
                "opportunity_count": len(opps),
                "client_ref_count": len(client_set),
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
