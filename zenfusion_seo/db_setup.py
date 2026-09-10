#!/usr/bin/env python3
"""
Zenfusion SQLite Database Setup
================================
Creates ~/.hermes/zenfusion.db with a brand-first schema and imports data
from the Search Promotions workbook.

Usage:
    python3 ~/.hermes/scripts/db_setup.py
"""

import sqlite3
import os
import re
import json
from datetime import datetime
from urllib.parse import urlparse

DB_PATH = os.path.expanduser("~/.hermes/zenfusion.db")
WORKBOOK_PATH = os.path.expanduser(
    "~/Downloads/SoftPro _ QWT - Search Promotions - 2026 (1).xlsx"
)

# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
-- =============================================================
-- 1. brands
-- =============================================================
CREATE TABLE IF NOT EXISTS brands (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    domain          TEXT,
    platform        TEXT DEFAULT 'Shopify',
    shopify_url     TEXT,
    gsc_property    TEXT,
    ga4_id          TEXT,
    brand_color     TEXT,
    target_geography TEXT DEFAULT 'US',
    business_type   TEXT,
    monthly_budget  REAL,
    created_at      TEXT DEFAULT (datetime('now'))
);

-- =============================================================
-- 2. core_keywords
-- =============================================================
CREATE TABLE IF NOT EXISTS core_keywords (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_id        INTEGER NOT NULL,
    keyword         TEXT NOT NULL,
    monthly_searches REAL,
    yoy_change      TEXT,
    competition     TEXT,
    cpc             REAL,
    notes           TEXT,
    created_at      TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (brand_id) REFERENCES brands(id)
);

-- =============================================================
-- 3. campaigns
-- =============================================================
CREATE TABLE IF NOT EXISTS campaigns (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_id        INTEGER NOT NULL,
    core_keyword_id INTEGER,
    name            TEXT,
    status          TEXT DEFAULT 'active',
    created_at      TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (brand_id) REFERENCES brands(id),
    FOREIGN KEY (core_keyword_id) REFERENCES core_keywords(id)
);

-- =============================================================
-- 4. money_pages
-- =============================================================
CREATE TABLE IF NOT EXISTS money_pages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id     INTEGER,
    url             TEXT,
    before_score    REAL,
    opt_date        TEXT,
    new_score       REAL,
    schema_check    TEXT,
    speed           TEXT,
    skeleton        TEXT,
    FOREIGN KEY (campaign_id) REFERENCES campaigns(id)
);

-- =============================================================
-- 5. backlinks
-- =============================================================
CREATE TABLE IF NOT EXISTS backlinks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id     INTEGER,
    money_page_id   INTEGER,
    url             TEXT,
    source_domain   TEXT,
    source_title    TEXT,
    anchor_text     TEXT,
    external_links  REAL,
    internal_links  REAL,
    first_seen      TEXT,
    last_seen       TEXT,
    response_code   INTEGER,
    domain_power    REAL,
    link_type       TEXT,
    backlink_state  TEXT,
    tier            INTEGER DEFAULT 1,
    tool_source     TEXT,
    tool_date       TEXT,
    created_at      TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (campaign_id) REFERENCES campaigns(id),
    FOREIGN KEY (money_page_id) REFERENCES money_pages(id)
);

-- =============================================================
-- 6. backlink_analyses
-- =============================================================
CREATE TABLE IF NOT EXISTS backlink_analyses (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_id        INTEGER NOT NULL,
    source          TEXT,
    urls_count      INTEGER,
    majestic_count  INTEGER,
    majestic_pct    REAL,
    tier1_high      INTEGER,
    tier1_medium    INTEGER,
    tier1_low       INTEGER,
    avg_strength    REAL,
    urls_submitted  INTEGER,
    analysis_json   TEXT,
    html_report_url TEXT,
    date_submitted  TEXT,
    date_processed  TEXT,
    FOREIGN KEY (brand_id) REFERENCES brands(id)
);

-- =============================================================
-- 7. content_pages
-- =============================================================
CREATE TABLE IF NOT EXISTS content_pages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id     INTEGER,
    url             TEXT,
    post_date       TEXT,
    type            TEXT,
    notes           TEXT,
    FOREIGN KEY (campaign_id) REFERENCES campaigns(id)
);

-- =============================================================
-- 8. page_audits
-- =============================================================
CREATE TABLE IF NOT EXISTS page_audits (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_id            INTEGER NOT NULL,
    url                 TEXT,
    keyword             TEXT,
    srs_composite       REAL,
    srs_structure       REAL,
    srs_relevance       REAL,
    srs_signal          REAL,
    content_score       REAL,
    technical_criticals INTEGER,
    technical_warnings  INTEGER,
    factors_count       INTEGER,
    html_report_url     TEXT,
    date_run            TEXT,
    FOREIGN KEY (brand_id) REFERENCES brands(id)
);

-- =============================================================
-- 9. end_of_day_reports
-- =============================================================
CREATE TABLE IF NOT EXISTS end_of_day_reports (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_id        INTEGER,
    date            TEXT,
    team_member     TEXT,
    activities      TEXT,
    notes           TEXT
);

-- =============================================================
-- 10. entities
-- =============================================================
CREATE TABLE IF NOT EXISTS entities (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_id        INTEGER,
    category        TEXT,
    entity          TEXT
);

-- =============================================================
-- 11. competitors
-- =============================================================
CREATE TABLE IF NOT EXISTS competitors (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_id        INTEGER,
    domain          TEXT,
    type            TEXT,
    notes           TEXT
);

-- =============================================================
-- 12. keyword_gaps
-- =============================================================
CREATE TABLE IF NOT EXISTS keyword_gaps (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_id            INTEGER,
    keyword             TEXT,
    search_volume       REAL,
    competitor_ranks_json TEXT
);

-- =============================================================
-- 13. content_gaps
-- =============================================================
CREATE TABLE IF NOT EXISTS content_gaps (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_id        INTEGER,
    cluster         TEXT,
    long_tail_keyword TEXT,
    search_intent   TEXT,
    title           TEXT,
    brand_title     TEXT,
    brand_url       TEXT,
    post_date       TEXT
);

-- =============================================================
-- 14. team_members
-- =============================================================
CREATE TABLE IF NOT EXISTS team_members (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_id        INTEGER,
    name            TEXT,
    role            TEXT
);

-- =============================================================
-- 15. backlink_gap_analyses
-- =============================================================
CREATE TABLE IF NOT EXISTS backlink_gap_analyses (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_id        INTEGER,
    competitor_domain TEXT,
    missing_links_json TEXT,
    html_report_url TEXT,
    date_run        TEXT
);

-- =============================================================
-- Indexes for common queries
-- =============================================================
CREATE INDEX IF NOT EXISTS idx_core_keywords_brand ON core_keywords(brand_id);
CREATE INDEX IF NOT EXISTS idx_campaigns_brand ON campaigns(brand_id);
CREATE INDEX IF NOT EXISTS idx_backlinks_campaign ON backlinks(campaign_id);
CREATE INDEX IF NOT EXISTS idx_backlinks_brand ON backlinks(campaign_id);
CREATE INDEX IF NOT EXISTS idx_backlinks_domain ON backlinks(source_domain);
CREATE INDEX IF NOT EXISTS idx_eod_brand ON end_of_day_reports(brand_id);
CREATE INDEX IF NOT EXISTS idx_eod_date ON end_of_day_reports(date);
CREATE INDEX IF NOT EXISTS idx_entities_brand ON entities(brand_id);
CREATE INDEX IF NOT EXISTS idx_competitors_brand ON competitors(brand_id);
CREATE INDEX IF NOT EXISTS idx_keyword_gaps_brand ON keyword_gaps(brand_id);
CREATE INDEX IF NOT EXISTS idx_content_gaps_brand ON content_gaps(brand_id);
CREATE INDEX IF NOT EXISTS idx_page_audits_brand ON page_audits(brand_id);
CREATE INDEX IF NOT EXISTS idx_ba_brand ON backlink_analyses(brand_id);
"""


def extract_domain(url):
    """Extract the domain from a URL or domain-like string."""
    if not url:
        return None
    url = str(url).strip()
    if not url:
        return None
    if not url.startswith("http"):
        url = "https://" + url
    try:
        parsed = urlparse(url)
        return parsed.netloc.lower().replace("www.", "") if parsed.netloc else None
    except Exception:
        return None


def safe_val(v):
    """Convert a cell value to a safe string or None."""
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def safe_float(v):
    """Convert a cell value to float or None."""
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def safe_int(v):
    """Convert a cell value to int or None."""
    f = safe_float(v)
    return int(f) if f is not None else None


def safe_date(v):
    """Convert a datetime cell to ISO date string or None."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d")
    s = str(v).strip()
    if not s:
        return None
    # Try parsing common formats
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return s


# ---------------------------------------------------------------------------
# Import functions
# ---------------------------------------------------------------------------

def create_schema(conn):
    """Create all tables and indexes."""
    conn.executescript(SCHEMA_SQL)
    conn.commit()


def import_brands(conn):
    """Import brands from LOGINS & RESOURCES tab."""
    import openpyxl

    wb = openpyxl.load_workbook(WORKBOOK_PATH, data_only=True, read_only=True)
    ws = wb["LOGINS & RESOURCES"]

    brands = []
    for row in ws.iter_rows(values_only=True):
        label = safe_val(row[0]) if len(row) > 0 else None
        link = safe_val(row[1]) if len(row) > 1 else None
        if not label or not link:
            continue
        label_lower = label.lower()
        if "shopify store" in label_lower:
            if "softpro" in label_lower:
                brands.append({
                    "name": "SoftPro Water Systems",
                    "domain": "softprowatersystems.com",
                    "shopify_url": link,
                    "brand_color": "#1a73e8",
                })
            elif "qwt" in label_lower or "quality water" in label_lower:
                brands.append({
                    "name": "Quality Water Treatment",
                    "domain": "qualitywatertreatment.com",
                    "shopify_url": link,
                    "brand_color": "#0d652d",
                })

    wb.close()

    count = 0
    for b in brands:
        conn.execute(
            """INSERT OR IGNORE INTO brands (name, domain, platform, shopify_url, brand_color,
               target_geography, business_type)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (b["name"], b["domain"], "Shopify", b["shopify_url"], b["brand_color"],
             "US", "e-commerce"),
        )
        count += 1
    conn.commit()
    return count


def get_brand_ids(conn):
    """Return a dict mapping brand name to id."""
    rows = conn.execute("SELECT id, name FROM brands").fetchall()
    return {r[1]: r[0] for r in rows}


def import_core_keywords(conn, brand_id):
    """Import core keywords from the Core Keywords tab."""
    import openpyxl

    wb = openpyxl.load_workbook(WORKBOOK_PATH, data_only=True, read_only=True)
    ws = wb["Core Keywords"]

    count = 0
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i == 0:  # header
            continue
        keyword = safe_val(row[0]) if len(row) > 0 else None
        if not keyword:
            continue
        monthly_searches = safe_float(row[1]) if len(row) > 1 else None
        yoy_change = safe_val(row[3]) if len(row) > 3 else None
        competition = safe_val(row[4]) if len(row) > 4 else None
        notes = safe_val(row[5]) if len(row) > 5 else None

        conn.execute(
            """INSERT INTO core_keywords (brand_id, keyword, monthly_searches, yoy_change,
               competition, notes)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (brand_id, keyword, monthly_searches, yoy_change, competition, notes),
        )
        count += 1

    wb.close()
    conn.commit()
    return count


def import_eod_reports(conn, brand_id):
    """Import end-of-day reports."""
    import openpyxl

    wb = openpyxl.load_workbook(WORKBOOK_PATH, data_only=True, read_only=True)
    ws = wb["END OF DAY REPORTS"]

    count = 0
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i == 0:  # header
            continue
        date_val = safe_date(row[0]) if len(row) > 0 else None
        team_member = safe_val(row[1]) if len(row) > 1 else None
        activities = safe_val(row[2]) if len(row) > 2 else None
        notes = safe_val(row[3]) if len(row) > 3 else None

        if not date_val and not team_member and not activities:
            continue

        conn.execute(
            """INSERT INTO end_of_day_reports (brand_id, date, team_member, activities, notes)
               VALUES (?, ?, ?, ?, ?)""",
            (brand_id, date_val, team_member, activities, notes),
        )
        count += 1

    wb.close()
    conn.commit()
    return count


def import_competitors(conn, brand_id):
    """Import competitors from the Competitors tab."""
    import openpyxl

    wb = openpyxl.load_workbook(WORKBOOK_PATH, data_only=True, read_only=True)
    ws = wb["Competitors"]

    count = 0
    seen = set()
    current_type = "main"

    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i == 0:  # header
            continue

        col_a = safe_val(row[0]) if len(row) > 0 else None
        col_d = safe_val(row[3]) if len(row) > 3 else None

        # Detect "SECONDARY" marker
        if col_a and col_a.upper() == "SECONDARY":
            current_type = "secondary"
            continue

        # Column A — main/secondary competitors
        if col_a and col_a.upper() not in ("MAIN", "SECONDARY", "LINK DUMP"):
            domain = col_a.lower().lstrip("www.")
            key = (domain, current_type)
            if key not in seen:
                conn.execute(
                    "INSERT INTO competitors (brand_id, domain, type) VALUES (?, ?, ?)",
                    (brand_id, domain, current_type),
                )
                seen.add(key)
                count += 1

        # Column D — link dump
        if col_d:
            domain = col_d.lower().lstrip("www.")
            key = (domain, "link_dump")
            if key not in seen:
                conn.execute(
                    "INSERT INTO competitors (brand_id, domain, type) VALUES (?, ?, ?)",
                    (brand_id, domain, "link_dump"),
                )
                seen.add(key)
                count += 1

    wb.close()
    conn.commit()
    return count


def import_entities(conn, brand_id):
    """Import entities from the Entities tab."""
    import openpyxl

    wb = openpyxl.load_workbook(WORKBOOK_PATH, data_only=True, read_only=True)
    ws = wb["Entities"]

    count = 0
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i == 0:  # header
            continue
        category = safe_val(row[0]) if len(row) > 0 else None
        entity = safe_val(row[1]) if len(row) > 1 else None
        if not entity:
            continue
        # Skip the "Link to Google Doc..." reference row
        if category and "Link to Google Doc" in category:
            continue

        conn.execute(
            "INSERT INTO entities (brand_id, category, entity) VALUES (?, ?, ?)",
            (brand_id, category, entity),
        )
        count += 1

    wb.close()
    conn.commit()
    return count


def import_keyword_gaps(conn, brand_id):
    """Import keyword gap analysis."""
    import openpyxl

    wb = openpyxl.load_workbook(WORKBOOK_PATH, data_only=True, read_only=True)
    ws = wb["Keyword_gap_analysis_gap_04-28-"]

    # First row is header — get competitor domain columns
    header = None
    competitor_cols = {}  # col_index -> domain_name

    count = 0
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i == 0:
            header = row
            for j, val in enumerate(row):
                if val and j > 2:  # competitor rank columns start at index 3
                    domain = str(val).strip().lower()
                    competitor_cols[j] = domain
            continue

        keyword = safe_val(row[1]) if len(row) > 1 else None
        if not keyword:
            continue

        search_volume = safe_float(row[2]) if len(row) > 2 else None

        # Build competitor ranks JSON
        ranks = {}
        for col_idx, domain in competitor_cols.items():
            if col_idx < len(row):
                rank_val = row[col_idx]
                if rank_val is not None and str(rank_val).strip():
                    ranks[domain] = safe_float(rank_val)

        ranks_json = json.dumps(ranks) if ranks else None

        conn.execute(
            """INSERT INTO keyword_gaps (brand_id, keyword, search_volume, competitor_ranks_json)
               VALUES (?, ?, ?, ?)""",
            (brand_id, keyword, search_volume, ranks_json),
        )
        count += 1

    wb.close()
    conn.commit()
    return count


def import_content_gaps(conn, brand_id):
    """Import SP Softener Content Gaps."""
    import openpyxl

    wb = openpyxl.load_workbook(WORKBOOK_PATH, data_only=True, read_only=True)
    ws = wb["SP Softener Content Gaps"]

    count = 0
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i == 0:  # header
            continue
        cluster = safe_val(row[0]) if len(row) > 0 else None
        long_tail = safe_val(row[1]) if len(row) > 1 else None
        if not cluster and not long_tail:
            continue

        search_intent = safe_val(row[2]) if len(row) > 2 else None
        title = safe_val(row[3]) if len(row) > 3 else None
        brand_title = safe_val(row[4]) if len(row) > 4 else None
        brand_url = safe_val(row[5]) if len(row) > 5 else None
        post_date = safe_date(row[6]) if len(row) > 6 else None

        conn.execute(
            """INSERT INTO content_gaps
               (brand_id, cluster, long_tail_keyword, search_intent, title,
                brand_title, brand_url, post_date)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (brand_id, cluster, long_tail, search_intent, title,
             brand_title, brand_url, post_date),
        )
        count += 1

    wb.close()
    conn.commit()
    return count


def import_old_tier1_backlinks(conn, brand_id):
    """Import OLD TIER 1 URLS into backlinks table."""
    import openpyxl

    wb = openpyxl.load_workbook(WORKBOOK_PATH, data_only=True, read_only=True)
    ws = wb["OLD TIER 1 URLS (broad)"]

    count = 0
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        # Row 0: section header (SOFTPRO)
        # Row 1: column headers (Page ascore, TIER 1 URL, TOPIC, GSA DATE, NOTES)
        # Row 2+: data
        if i < 2:
            continue

        tier1_url = safe_val(row[1]) if len(row) > 1 else None
        if not tier1_url:
            continue

        page_ascore = safe_float(row[0]) if len(row) > 0 else None
        topic = safe_val(row[2]) if len(row) > 2 else None
        gsa_date = safe_date(row[3]) if len(row) > 3 else None
        notes = safe_val(row[4]) if len(row) > 4 else None

        source_domain = extract_domain(tier1_url)

        conn.execute(
            """INSERT INTO backlinks
               (campaign_id, money_page_id, url, source_domain, source_title,
                anchor_text, tier, tool_source, tool_date)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (None, None, tier1_url, source_domain, topic, notes, 1,
             "OLD_TIER_1_URLS", gsa_date),
        )
        count += 1

    wb.close()
    conn.commit()
    return count


def import_calc_backlinks(conn, brand_id, tab_name="calc.softprowatersystems.com-us"):
    """Import backlinks from calc.softprowatersystems.com-us tab."""
    import openpyxl

    wb = openpyxl.load_workbook(WORKBOOK_PATH, data_only=True, read_only=True)
    ws = wb[tab_name]

    count = 0
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i == 0:  # header
            continue

        source_url = safe_val(row[2]) if len(row) > 2 else None
        if not source_url:
            continue

        page_ascore = safe_float(row[0]) if len(row) > 0 else None
        source_title = safe_val(row[1]) if len(row) > 1 else None
        # row[2] = source_url (used as url)
        # row[3] = target_url (not in schema, skip)
        anchor_text = safe_val(row[4]) if len(row) > 4 else None
        external_links = safe_float(row[5]) if len(row) > 5 else None
        internal_links = safe_float(row[6]) if len(row) > 6 else None
        first_seen = safe_date(row[7]) if len(row) > 7 else None
        last_seen = safe_date(row[8]) if len(row) > 8 else None
        response_code = safe_int(row[9]) if len(row) > 9 else None
        domain_power = safe_float(row[10]) if len(row) > 10 else None
        link_type = safe_val(row[11]) if len(row) > 11 else None
        backlink_state = safe_val(row[12]) if len(row) > 12 else None

        source_domain = extract_domain(source_url)

        conn.execute(
            """INSERT INTO backlinks
               (campaign_id, money_page_id, url, source_domain, source_title,
                anchor_text, external_links, internal_links, first_seen, last_seen,
                response_code, domain_power, link_type, backlink_state, tier,
                tool_source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (None, None, source_url, source_domain, source_title,
             anchor_text, external_links, internal_links, first_seen, last_seen,
             response_code, domain_power, link_type, backlink_state, 1,
             tab_name),
        )
        count += 1

    wb.close()
    conn.commit()
    return count


def import_team_members(conn, brand_id):
    """Extract unique team member names from EOD reports and insert into team_members."""
    rows = conn.execute(
        "SELECT DISTINCT team_member FROM end_of_day_reports WHERE team_member IS NOT NULL"
    ).fetchall()

    count = 0
    for (name,) in rows:
        if not name or not name.strip():
            continue
        conn.execute(
            "INSERT INTO team_members (brand_id, name) VALUES (?, ?)",
            (brand_id, name.strip()),
        )
        count += 1
    conn.commit()
    return count


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("  Zenfusion Database Setup")
    print("=" * 60)
    print()

    # Remove existing database for a clean import
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
        print(f"[+] Removed existing database: {DB_PATH}")

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    # --- Schema ---
    print("[+] Creating schema (15 tables)...")
    create_schema(conn)
    print("    ✓ All tables and indexes created")
    print()

    # --- Brands ---
    print("[+] Importing brands from LOGINS & RESOURCES...")
    n = import_brands(conn)
    print(f"    ✓ Imported {n} brands")

    brand_ids = get_brand_ids(conn)
    softpro_id = brand_ids.get("SoftPro Water Systems", 1)
    qwt_id = brand_ids.get("Quality Water Treatment", 2)
    print(f"    SoftPro Water Systems → id={softpro_id}")
    print(f"    Quality Water Treatment → id={qwt_id}")
    print()

    # --- Core Keywords ---
    print("[+] Importing core keywords...")
    n = import_core_keywords(conn, softpro_id)
    print(f"    ✓ Imported {n} core keywords (brand_id={softpro_id})")
    print()

    # --- End of Day Reports ---
    print("[+] Importing end-of-day reports...")
    n = import_eod_reports(conn, softpro_id)
    print(f"    ✓ Imported {n} end-of-day reports (brand_id={softpro_id})")
    print()

    # --- Team Members (derived from EOD reports) ---
    print("[+] Extracting team members from EOD reports...")
    n = import_team_members(conn, softpro_id)
    print(f"    ✓ Imported {n} team members")
    print()

    # --- Competitors ---
    print("[+] Importing competitors...")
    n = import_competitors(conn, softpro_id)
    print(f"    ✓ Imported {n} competitors (brand_id={softpro_id})")
    print()

    # --- Entities ---
    print("[+] Importing entities...")
    n = import_entities(conn, softpro_id)
    print(f"    ✓ Imported {n} entities (brand_id={softpro_id})")
    print()

    # --- Keyword Gaps ---
    print("[+] Importing keyword gap analysis...")
    n = import_keyword_gaps(conn, softpro_id)
    print(f"    ✓ Imported {n} keyword gaps (brand_id={softpro_id})")
    print()

    # --- Content Gaps ---
    print("[+] Importing SP Softener content gaps...")
    n = import_content_gaps(conn, softpro_id)
    print(f"    ✓ Imported {n} content gaps (brand_id={softpro_id})")
    print()

    # --- Old Tier 1 Backlinks ---
    print("[+] Importing OLD TIER 1 URLS → backlinks...")
    n = import_old_tier1_backlinks(conn, softpro_id)
    print(f"    ✓ Imported {n} tier-1 backlinks (brand_id={softpro_id})")
    print()

    # --- Calc backlinks ---
    print("[+] Importing calc.softprowatersystems.com-us → backlinks...")
    n = import_calc_backlinks(conn, softpro_id)
    print(f"    ✓ Imported {n} backlinks (brand_id={softpro_id})")
    print()

    # --- Summary ---
    print("=" * 60)
    print("  Import Summary — Table Row Counts")
    print("=" * 60)
    tables = [
        "brands", "core_keywords", "campaigns", "money_pages", "backlinks",
        "backlink_analyses", "content_pages", "page_audits",
        "end_of_day_reports", "entities", "competitors", "keyword_gaps",
        "content_gaps", "team_members", "backlink_gap_analyses",
    ]
    for table in tables:
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"    {table:30s} {count:>8,} rows")
    print()
    print(f"  Database: {DB_PATH}")
    print(f"  File size: {os.path.getsize(DB_PATH) / 1024:.1f} KB")
    print()
    print("  ✓ Setup complete!")
    conn.close()


if __name__ == "__main__":
    main()
