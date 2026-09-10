"""
ZenfusionSEO CLI — unified command-line interface for all SEO tools.

Usage:
    zenfusion-seo site-audit --domain DOMAIN [--limit N] [--output PATH]
    zenfusion-seo page-audit --url URL --keyword KEYWORD [--output PATH]
    zenfusion-seo ai-visibility --domain DOMAIN --keyword KEYWORD [--output PATH]
    zenfusion-seo backlink-gap --client DOMAIN --competitor DOMAIN [--output PATH]
    zenfusion-seo anchor-text --domain DOMAIN --brand BRAND [--output PATH]
    zenfusion-seo link-velocity --client DOMAIN --competitors D1,D2 [--days N] [--output PATH]
    zenfusion-seo toxic-links --domain DOMAIN [--output PATH]
    zenfusion-seo content-decay --domain DOMAIN [--output PATH]
    zenfusion-seo cannibalization --domain DOMAIN --keywords K1,K2 [--output PATH]
    zenfusion-seo rank-tracking --domain DOMAIN --keywords K1,K2 [--output PATH]
    zenfusion-seo video-seo --keywords K1,K2 --domain DOMAIN [--output PATH]
    zenfusion-seo local-grid --keyword KEYWORD --business NAME --lat LAT --lng LNG [--output PATH]
    zenfusion-seo shopify-duplicates --domain DOMAIN [--limit N] [--output PATH]
    zenfusion-seo shopify-schema --domain DOMAIN [--limit N] [--output PATH]
    zenfusion-seo shopify-crawl --domain DOMAIN [--limit N] [--output PATH]
    zenfusion-seo serve [--port PORT] [--host HOST]
    zenfusion-seo list  # list all available tools
"""

import sys
import os
import subprocess
import argparse

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))

# Tool registry: name → (script, description, level)
TOOLS = {
    # Domain Level
    "site-audit": ("site_auditor.py", "Full technical SEO crawl: 13 categories, 100+ pages", "Domain"),
    "seo-visibility": ("seo_visibility_report.py", "Domain rank overview, traffic, competitor intersection", "Domain"),
    "portfolio-audit": ("portfolio_audit.py", "7 DataForSEO endpoints per domain across brands", "Domain"),
    "structured-data": ("structured_data_audit.py", "Domain-wide JSON-LD, microdata, RDFa schema coverage", "Domain"),
    "page-speed": ("page_speed_audit.py", "Domain-wide PSI + CrUX, Core Web Vitals", "Domain"),
    "domain-history": ("domain_history_audit.py", "Visibility trends, backlink history, competitor discovery", "Domain"),
    
    # Page Level
    "page-audit": ("page_auditor.py", "200 ranking factors, SRS scoring, SERP preview, schema", "Page"),
    "instant-audit": ("instant_page_audit.py", "Single-URL instant audit via DataForSEO", "Page"),
    "ai-visibility": ("ai_visibility_helper.py", "Multi-LLM AEO: ChatGPT, Gemini, Claude, Perplexity", "Page"),
    "page-backlinks": ("page_backlink_audit.py", "All backlinks pointing to a specific URL", "Page"),
    "speed-deep-dive": ("page_speed_deep_dive.py", "Full Lighthouse with 153 audit results", "Page"),
    "serp-features": ("serp_feature_check.py", "AI Overview, PAA, featured snippets analysis", "Page"),
    
    # Backlink Level
    "backlink-gap": ("backlink_gap_analyzer.py", "Domain intersection, LLM citation flagging", "Backlink"),
    "anchor-text": ("anchor_text_analysis.py", "Anchor distribution + over-optimization risk", "Backlink"),
    "link-velocity": ("competitive_link_velocity.py", "90-day link building speed comparison", "Backlink"),
    "lost-backlinks": ("lost_backlink_recovery.py", "Lost backlink tracking + outreach templates", "Backlink"),
    "toxic-links": ("toxic_link_detector.py", "Spam/toxic link detection + disavow file", "Backlink"),
    "backlink-alerts": ("backlink_alert_report.py", "30-day backlink activity timeline + alerts", "Backlink"),
    "referring-timeline": ("referring_domains_timeline.py", "When each referring domain first linked", "Backlink"),
    "multi-intersect": ("multi_domain_link_intersect.py", "Domains linking to 3+ competitors but not you", "Backlink"),
    
    # Market Level
    "content-plan": ("content_plan_builder.py", "Seed → keywords → clusters → quick wins → XLSX", "Market"),
    "serp-landscape": ("serp_feature_landscape.py", "SERP feature dominance across market keywords", "Market"),
    "intent-map": ("search_intent_map.py", "Info/commercial/transactional/navigational split", "Market"),
    "seasonal-trends": ("seasonal_trends_report.py", "Monthly volume trends, SVG heatmap", "Market"),
    "keyword-difficulty": ("keyword_difficulty_map.py", "Difficulty distribution, winnability scoring", "Market"),
    
    # Competitor Level
    "content-gap": ("competitor_content_gap.py", "Keywords competitors rank for that you don't", "Competitor"),
    "share-of-voice": ("share_of_voice_trend.py", "SoV comparison across competitors", "Competitor"),
    "page-monitor": ("competitor_page_monitor.py", "Tracks new pages published by competitors", "Competitor"),
    "brand-mentions": ("brand_mention_monitor.py", "Brand mentions across AI + web", "Competitor"),
    
    # Keyword Level
    "cannibalization": ("cannibalization_detector.py", "Keyword cannibalization + $ at risk", "Keyword"),
    "rank-tracking": ("keyword_rank_tracker.py", "Position history with delta alerts", "Keyword"),
    "content-decay": ("content_decay_detector.py", "Pages losing organic traffic", "Keyword"),
    
    # Local SEO
    "local-grid": ("local_seo_grid_tracker.py", "Local grid ranking with SVG heatmap", "Local"),
    
    # Video SEO
    "video-seo": ("video_seo_tracker.py", "Video carousel presence + opportunities", "Video"),
    
    # Shopify-Specific
    "shopify-duplicates": ("shopify_duplicate_detector.py", "Shopify duplicate URLs + canonical chains", "Shopify"),
    "shopify-schema": ("shopify_schema_auditor.py", "Shopify page-type schema checks + Liquid fixes", "Shopify"),
    "shopify-crawl": ("shopify_crawl_budget.py", "Shopify crawl waste + robots.txt recommendations", "Shopify"),
    
    # Infrastructure
    "internal-links": ("internal_link_equity.py", "Internal link equity flow + orphan detection", "Domain"),
    "crawl-budget": ("crawl_budget_analyzer.py", "Crawl budget waste estimation", "Domain"),
    "redirect-chains": ("redirect_chain_analyzer.py", "Redirect chain tracer + issue detection", "Domain"),
}


def run_script(script_name, args_list, output_dir=None):
    """Run a ZenfusionSEO script with the given arguments."""
    script_path = os.path.join(SCRIPTS_DIR, script_name)
    if not os.path.exists(script_path):
        print(f"Error: Script not found: {script_name}")
        return 1
    
    cmd = [sys.executable, script_path] + args_list
    if output_dir and '--output' not in args_list:
        cmd += ['--output', output_dir]
    
    try:
        result = subprocess.run(cmd, capture_output=False, timeout=600)
        return result.returncode
    except subprocess.TimeoutExpired:
        print(f"Error: Script timed out after 600 seconds")
        return 1
    except Exception as e:
        print(f"Error running {script_name}: {e}")
        return 1


def list_tools():
    """Print all available tools organized by level."""
    levels = {}
    for name, (script, desc, level) in TOOLS.items():
        if level not in levels:
            levels[level] = []
        levels[level].append((name, desc))
    
    print("\nZenfusionSEO — Available Tools\n")
    print("=" * 70)
    for level in ["Domain", "Page", "Backlink", "Market", "Competitor", "Keyword", "Local", "Video", "Shopify"]:
        if level in levels:
            print(f"\n  {level} Level:")
            for name, desc in sorted(levels[level]):
                print(f"    zenfusion-seo {name:<25} {desc}")
    print(f"\n  Total: {len(TOOLS)} tools")
    print("=" * 70)


def main():
    parser = argparse.ArgumentParser(
        prog="zenfusion-seo",
        description="Open-source SEO/AEO/CRO toolkit for Shopify stores",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available tools")
    
    # site-audit
    p = subparsers.add_parser("site-audit", help="Full technical SEO crawl")
    p.add_argument("--domain", required=True)
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--output", default=None)
    
    # page-audit
    p = subparsers.add_parser("page-audit", help="200-factor page audit")
    p.add_argument("--url", required=True)
    p.add_argument("--keyword", required=True)
    p.add_argument("--output", default=None)
    
    # ai-visibility
    p = subparsers.add_parser("ai-visibility", help="Multi-LLM AEO test")
    p.add_argument("--domain", required=True)
    p.add_argument("--keyword", required=True)
    p.add_argument("--output", default=None)
    
    # backlink-gap
    p = subparsers.add_parser("backlink-gap", help="Backlink gap analysis")
    p.add_argument("--client", required=True)
    p.add_argument("--competitor", required=True)
    p.add_argument("--output", default=None)
    
    # anchor-text
    p = subparsers.add_parser("anchor-text", help="Anchor text analysis")
    p.add_argument("--domain", required=True)
    p.add_argument("--brand", required=True)
    p.add_argument("--output", default=None)
    
    # link-velocity
    p = subparsers.add_parser("link-velocity", help="Competitive link velocity")
    p.add_argument("--client", required=True)
    p.add_argument("--competitors", required=True)
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--output", default=None)
    
    # toxic-links
    p = subparsers.add_parser("toxic-links", help="Toxic link detection")
    p.add_argument("--domain", required=True)
    p.add_argument("--output", default=None)
    
    # content-decay
    p = subparsers.add_parser("content-decay", help="Content decay detection")
    p.add_argument("--domain", required=True)
    p.add_argument("--output", default=None)
    
    # cannibalization
    p = subparsers.add_parser("cannibalization", help="Keyword cannibalization")
    p.add_argument("--domain", required=True)
    p.add_argument("--keywords", required=True)
    p.add_argument("--output", default=None)
    
    # rank-tracking
    p = subparsers.add_parser("rank-tracking", help="Keyword rank tracking")
    p.add_argument("--domain", required=True)
    p.add_argument("--keywords", required=True)
    p.add_argument("--output", default=None)
    
    # video-seo
    p = subparsers.add_parser("video-seo", help="Video SEO tracking")
    p.add_argument("--keywords", required=True)
    p.add_argument("--domain", required=True)
    p.add_argument("--output", default=None)
    
    # local-grid
    p = subparsers.add_parser("local-grid", help="Local SEO grid tracking")
    p.add_argument("--keyword", required=True)
    p.add_argument("--business", required=True)
    p.add_argument("--lat", type=float, required=True)
    p.add_argument("--lng", type=float, required=True)
    p.add_argument("--output", default=None)
    
    # shopify-duplicates
    p = subparsers.add_parser("shopify-duplicates", help="Shopify duplicate detector")
    p.add_argument("--domain", required=True)
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--output", default=None)
    
    # shopify-schema
    p = subparsers.add_parser("shopify-schema", help="Shopify schema auditor")
    p.add_argument("--domain", required=True)
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--output", default=None)
    
    # shopify-crawl
    p = subparsers.add_parser("shopify-crawl", help="Shopify crawl budget")
    p.add_argument("--domain", required=True)
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--output", default=None)
    
    # list
    subparsers.add_parser("list", help="List all available tools")
    
    # serve
    p = subparsers.add_parser("serve", help="Start web dashboard")
    p.add_argument("--port", type=int, default=5000)
    p.add_argument("--host", default="0.0.0.0")
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return 0
    
    if args.command == "list":
        list_tools()
        return 0
    
    if args.command == "serve":
        # Start Flask web dashboard
        try:
            from .api.server import create_app
            app = create_app()
            print(f"ZenfusionSEO dashboard running at http://localhost:{args.port}")
            app.run(host=args.host, port=args.port, debug=True)
        except ImportError:
            print("Flask not installed. Run: pip install flask")
        return 0
    
    # Map command to script
    if args.command in TOOLS:
        script, desc, level = TOOLS[args.command]
        
        # Build args list from parsed args
        args_list = []
        for key, val in vars(args).items():
            if key in ('command', 'output'):
                continue
            if val is not None:
                flag = f"--{key.replace('_', '-')}"
                args_list.extend([flag, str(val)])
        
        # For page-audit, URL and keyword are positional
        if args.command == "page-audit":
            args_list = [args.url, args.keyword]
        
        output_dir = getattr(args, 'output', None)
        return run_script(script, args_list, output_dir)
    
    print(f"Unknown command: {args.command}")
    return 1