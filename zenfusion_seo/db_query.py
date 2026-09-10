#!/usr/bin/env python3
"""
Zenfusion Database Query Helpers
=================================
Helper functions for querying and adding data to the Zenfusion SQLite database.

Usage as a module:
    from db_query import get_brand, get_campaigns, get_backlinks, ...

Usage from CLI:
    python3 ~/.hermes/scripts/db_query.py brands
    python3 ~/.hermes/scripts/db_query.py backlinks --brand "SoftPro Water Systems"
    python3 ~/.hermes/scripts/db_query.py audits --brand "SoftPro Water Systems"
"""

import sqlite3
import os
import json
from datetime import datetime

DB_PATH = os.path.expanduser("~/.hermes/zenfusion.db")


def get_conn():
    """Get a SQLite connection with foreign keys enabled."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _brand_id_by_name(conn, name):
    """Resolve a brand name to its id."""
    row = conn.execute("SELECT id FROM brands WHERE name = ?", (name,)).fetchone()
    if not row:
        raise ValueError(f"Brand not found: {name}")
    return row["id"]


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def get_brand(name):
    """Return the brand record matching `name` (exact or fuzzy)."""
    conn = get_conn()
    # Exact match first
    row = conn.execute("SELECT * FROM brands WHERE name = ?", (name,)).fetchone()
    if not row:
        # Fuzzy: case-insensitive LIKE
        row = conn.execute(
            "SELECT * FROM brands WHERE name LIKE ?", (f"%{name}%",)
        ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_campaigns(brand_id):
    """Return all campaigns for a brand."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM campaigns WHERE brand_id = ? ORDER BY created_at DESC",
        (brand_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_backlinks(brand_id, campaign_id=None):
    """Return backlinks for a brand, optionally filtered by campaign."""
    conn = get_conn()
    if campaign_id:
        rows = conn.execute(
            """SELECT b.* FROM backlinks b
               JOIN campaigns c ON b.campaign_id = c.id
               WHERE c.brand_id = ? AND b.campaign_id = ?
               ORDER BY b.created_at DESC""",
            (brand_id, campaign_id),
        ).fetchall()
    else:
        # Backlinks without campaign (imported directly) — match by tool_source
        # or through campaign linkage
        rows = conn.execute(
            """SELECT b.* FROM backlinks b
               LEFT JOIN campaigns c ON b.campaign_id = c.id
               WHERE c.brand_id = ? OR b.campaign_id IS NULL
               ORDER BY b.created_at DESC""",
            (brand_id,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_page_audits(brand_id):
    """Return audit history for a brand."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM page_audits WHERE brand_id = ? ORDER BY date_run DESC",
        (brand_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_backlink_analyses(brand_id):
    """Return backlink analysis history for a brand."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM backlink_analyses WHERE brand_id = ? ORDER BY date_processed DESC",
        (brand_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_core_keywords(brand_id):
    """Return all core keywords for a brand."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM core_keywords WHERE brand_id = ? ORDER BY monthly_searches DESC",
        (brand_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_competitors(brand_id):
    """Return all competitors for a brand."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM competitors WHERE brand_id = ? ORDER BY type, domain",
        (brand_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_entities(brand_id):
    """Return all entities for a brand."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM entities WHERE brand_id = ? ORDER BY category, entity",
        (brand_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_keyword_gaps(brand_id):
    """Return all keyword gaps for a brand."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM keyword_gaps WHERE brand_id = ? ORDER BY search_volume DESC",
        (brand_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_content_gaps(brand_id):
    """Return all content gaps for a brand."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM content_gaps WHERE brand_id = ? ORDER BY cluster",
        (brand_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_eod_reports(brand_id, limit=None):
    """Return end-of-day reports for a brand, optionally limited."""
    conn = get_conn()
    if limit:
        rows = conn.execute(
            "SELECT * FROM end_of_day_reports WHERE brand_id = ? ORDER BY date DESC LIMIT ?",
            (brand_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM end_of_day_reports WHERE brand_id = ? ORDER BY date DESC",
            (brand_id,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_team_members(brand_id):
    """Return all team members for a brand."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM team_members WHERE brand_id = ? ORDER BY name",
        (brand_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------

def add_brand(name, domain, platform="Shopify", shopify_url=None, gsc_property=None,
              ga4_id=None, brand_color=None, target_geography="US",
              business_type="e-commerce", monthly_budget=None):
    """Create a new brand and return its id."""
    conn = get_conn()
    cursor = conn.execute(
        """INSERT OR IGNORE INTO brands
           (name, domain, platform, shopify_url, gsc_property, ga4_id,
            brand_color, target_geography, business_type, monthly_budget)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (name, domain, platform, shopify_url, gsc_property, ga4_id,
         brand_color, target_geography, business_type, monthly_budget),
    )
    conn.commit()
    brand_id = cursor.lastrowid
    conn.close()
    return brand_id


def add_backlink_analysis(brand_id, source, urls_count=None, majestic_count=None,
                          majestic_pct=None, tier1_high=None, tier1_medium=None,
                          tier1_low=None, avg_strength=None, urls_submitted=None,
                          analysis_json=None, html_report_url=None,
                          date_submitted=None, date_processed=None):
    """Store a backlink analysis result and return its id."""
    conn = get_conn()
    cursor = conn.execute(
        """INSERT INTO backlink_analyses
           (brand_id, source, urls_count, majestic_count, majestic_pct,
            tier1_high, tier1_medium, tier1_low, avg_strength, urls_submitted,
            analysis_json, html_report_url, date_submitted, date_processed)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (brand_id, source, urls_count, majestic_count, majestic_pct,
         tier1_high, tier1_medium, tier1_low, avg_strength, urls_submitted,
         analysis_json, html_report_url, date_submitted, date_processed),
    )
    conn.commit()
    analysis_id = cursor.lastrowid
    conn.close()
    return analysis_id


def add_campaign(brand_id, name, core_keyword_id=None, status="active"):
    """Create a new campaign and return its id."""
    conn = get_conn()
    cursor = conn.execute(
        """INSERT INTO campaigns (brand_id, core_keyword_id, name, status)
           VALUES (?, ?, ?, ?)""",
        (brand_id, core_keyword_id, name, status),
    )
    conn.commit()
    campaign_id = cursor.lastrowid
    conn.close()
    return campaign_id


def add_money_page(campaign_id, url, before_score=None, opt_date=None,
                   new_score=None, schema_check=None, speed=None, skeleton=None):
    """Create a new money page record and return its id."""
    conn = get_conn()
    cursor = conn.execute(
        """INSERT INTO money_pages
           (campaign_id, url, before_score, opt_date, new_score,
            schema_check, speed, skeleton)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (campaign_id, url, before_score, opt_date, new_score,
         schema_check, speed, skeleton),
    )
    conn.commit()
    page_id = cursor.lastrowid
    conn.close()
    return page_id


def add_backlink(campaign_id, url, source_domain=None, source_title=None,
                 anchor_text=None, tier=1, tool_source=None, tool_date=None,
                 money_page_id=None, external_links=None, internal_links=None,
                 first_seen=None, last_seen=None, response_code=None,
                 domain_power=None, link_type=None, backlink_state=None):
    """Add a backlink record and return its id."""
    conn = get_conn()
    cursor = conn.execute(
        """INSERT INTO backlinks
           (campaign_id, money_page_id, url, source_domain, source_title,
            anchor_text, external_links, internal_links, first_seen, last_seen,
            response_code, domain_power, link_type, backlink_state, tier,
            tool_source, tool_date)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (campaign_id, money_page_id, url, source_domain, source_title,
         anchor_text, external_links, internal_links, first_seen, last_seen,
         response_code, domain_power, link_type, backlink_state, tier,
         tool_source, tool_date),
    )
    conn.commit()
    backlink_id = cursor.lastrowid
    conn.close()
    return backlink_id


def add_page_audit(brand_id, url, keyword=None, srs_composite=None,
                   srs_structure=None, srs_relevance=None, srs_signal=None,
                   content_score=None, technical_criticals=None,
                   technical_warnings=None, factors_count=None,
                   html_report_url=None, date_run=None):
    """Add a page audit record and return its id."""
    if date_run is None:
        date_run = datetime.now().strftime("%Y-%m-%d")
    conn = get_conn()
    cursor = conn.execute(
        """INSERT INTO page_audits
           (brand_id, url, keyword, srs_composite, srs_structure, srs_relevance,
            srs_signal, content_score, technical_criticals, technical_warnings,
            factors_count, html_report_url, date_run)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (brand_id, url, keyword, srs_composite, srs_structure, srs_relevance,
         srs_signal, content_score, technical_criticals, technical_warnings,
         factors_count, html_report_url, date_run),
    )
    conn.commit()
    audit_id = cursor.lastrowid
    conn.close()
    return audit_id


def add_eod_report(brand_id, date, team_member, activities, notes=None):
    """Add an end-of-day report and return its id."""
    conn = get_conn()
    cursor = conn.execute(
        """INSERT INTO end_of_day_reports
           (brand_id, date, team_member, activities, notes)
           VALUES (?, ?, ?, ?, ?)""",
        (brand_id, date, team_member, activities, notes),
    )
    conn.commit()
    report_id = cursor.lastrowid
    conn.close()
    return report_id


def add_team_member(brand_id, name, role=None):
    """Add a team member and return their id."""
    conn = get_conn()
    cursor = conn.execute(
        "INSERT INTO team_members (brand_id, name, role) VALUES (?, ?, ?)",
        (brand_id, name, role),
    )
    conn.commit()
    member_id = cursor.lastrowid
    conn.close()
    return member_id


def add_competitor(brand_id, domain, type="main", notes=None):
    """Add a competitor and return its id."""
    conn = get_conn()
    cursor = conn.execute(
        "INSERT INTO competitors (brand_id, domain, type, notes) VALUES (?, ?, ?, ?)",
        (brand_id, domain, type, notes),
    )
    conn.commit()
    comp_id = cursor.lastrowid
    conn.close()
    return comp_id


def table_counts():
    """Return a dict of table_name → row_count for all tables."""
    conn = get_conn()
    tables = [
        "brands", "core_keywords", "campaigns", "money_pages", "backlinks",
        "backlink_analyses", "content_pages", "page_audits",
        "end_of_day_reports", "entities", "competitors", "keyword_gaps",
        "content_gaps", "team_members", "backlink_gap_analyses",
    ]
    counts = {}
    for t in tables:
        counts[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    conn.close()
    return counts


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Query the Zenfusion database")
    parser.add_argument("command", choices=[
        "brands", "keywords", "campaigns", "backlinks", "audits",
        "analyses", "competitors", "entities", "gaps", "content-gaps",
        "eod", "team", "counts",
    ])
    parser.add_argument("--brand", default="SoftPro Water Systems",
                        help="Brand name to query")
    parser.add_argument("--campaign-id", type=int, help="Campaign ID filter")
    parser.add_argument("--limit", type=int, help="Limit number of results")
    args = parser.parse_args()

    brand = get_brand(args.brand)
    if not brand and args.command != "brands" and args.command != "counts":
        print(f"Brand not found: {args.brand}")
        print("Available brands:")
        conn = get_conn()
        for r in conn.execute("SELECT id, name FROM brands").fetchall():
            print(f"  {r['id']}: {r['name']}")
        conn.close()
        exit(1)

    brand_id = brand["id"] if brand else None

    if args.command == "brands":
        conn = get_conn()
        for r in conn.execute("SELECT * FROM brands").fetchall():
            print(dict(r))
        conn.close()
    elif args.command == "counts":
        for table, count in table_counts().items():
            print(f"  {table:30s} {count:>8,} rows")
    elif args.command == "keywords":
        for kw in get_core_keywords(brand_id)[:args.limit or 20]:
            print(f"  {kw['keyword']:40s}  MSV={kw['monthly_searches'] or 0:>10.0f}  "
                  f"Comp={kw['competition'] or '?'}")
    elif args.command == "campaigns":
        for c in get_campaigns(brand_id):
            print(dict(c))
    elif args.command == "backlinks":
        bl = get_backlinks(brand_id, args.campaign_id)
        if args.limit:
            bl = bl[:args.limit]
        print(f"Total: {len(bl)} backlinks")
        for b in bl[:50]:
            print(f"  [{b['id']}] {b['url'][:80]}  domain={b['source_domain']}  "
                  f"DP={b['domain_power']}  state={b['backlink_state']}")
        if len(bl) > 50:
            print(f"  ... and {len(bl) - 50} more")
    elif args.command == "audits":
        for a in get_page_audits(brand_id):
            print(dict(a))
    elif args.command == "analyses":
        for a in get_backlink_analyses(brand_id):
            print(dict(a))
    elif args.command == "competitors":
        for c in get_competitors(brand_id):
            print(f"  {c['domain']:40s}  type={c['type']}")
    elif args.command == "entities":
        for e in get_entities(brand_id):
            print(f"  [{e['category']}] {e['entity']}")
    elif args.command == "gaps":
        gaps = get_keyword_gaps(brand_id)
        if args.limit:
            gaps = gaps[:args.limit]
        for g in gaps:
            print(f"  {g['keyword']:50s}  SV={g['search_volume'] or 0:>10.0f}")
    elif args.command == "content-gaps":
        gaps = get_content_gaps(brand_id)
        if args.limit:
            gaps = gaps[:args.limit]
        for g in gaps:
            print(f"  [{g['cluster']}] {g['long_tail_keyword']}")
    elif args.command == "eod":
        reports = get_eod_reports(brand_id, args.limit)
        for r in reports:
            print(f"  [{r['date']}] {r['team_member']}: {r['activities']}")
    elif args.command == "team":
        for m in get_team_members(brand_id):
            print(f"  {m['name']}  role={m['role'] or ''}")
