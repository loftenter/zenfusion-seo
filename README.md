# ZenfusionSEO — Open Source SEO/AEO/CRO Toolkit

> The open-source alternative to Ahrefs, SEMrush, and Search Atlas — built specifically for Shopify stores.

67+ Python scripts that generate comprehensive SEO reports from DataForSEO, Google PSI, GSC, Perplexity AI, InfraNodus, and Keywords Everywhere data.

## Features

### 6 Report Levels — 75+ Report Types

#### 🌐 Domain Level (7 reports)
- Site Audit Report — 13 categories, 100+ pages, Lighthouse/CWV
- SEO Visibility Report — traffic estimates, position distribution, competitor intersection
- Portfolio Audit — 7 DataForSEO endpoints per domain across multiple brands
- Keyword Opportunity Dashboard — title/H1 extraction + KE volume + gap analysis
- Structured Data Audit — domain-wide JSON-LD, microdata, RDFa schema coverage
- Page Speed Audit — domain-wide PSI + CrUX, Core Web Vitals classification
- Domain History Audit — visibility trends, backlink history, competitor discovery

#### 📄 Page Level (8 reports)
- Page Auditor — 200 ranking factors, SRS scoring, Google-style SERP preview, schema analysis
- Content Auditor — 6-dimension content quality, keyword usage, citations
- AI Visibility Report — multi-LLM AEO test (ChatGPT, Gemini, Claude, Perplexity)
- Citation Readiness — 7-marker rubric from real AI citation patterns
- Page-Level Backlink Audit — all backlinks pointing to a specific URL
- Instant Page Audit — single-URL instant audit via DataForSEO on_page/instant_pages
- Page Speed Deep Dive — full Lighthouse with 153 individual audit results
- SERP Feature Check — AI Overview, PAA, featured snippets, competition analysis

#### 🔗 Backlink Level (8 reports)
- Backlink Analysis (Batch) — DataForSEO + Majestic Million + Google Index + IndexBolt
- Backlink Gap Analyzer — domain intersection, LLM citation flagging, 7-factor scoring
- Backlink Indexer — live URL check → IndexBolt → Omega fallback
- Backlink DB Storage — SQLite, 18K+ backlinks, 16 tables, brand-first schema
- Anchor Text Analysis — distribution + over-optimization risk scoring
- Competitive Link Velocity — 90-day SVG velocity chart, link intercepts
- Referring Domains Timeline — when each domain first linked, SVG timeline
- Lost Backlink Recovery — loss reason classification, outreach email templates

#### 📊 Market Level (8 reports)
- Content Plan Builder — seed → 1000 keywords → 147 clusters → 73 quick wins
- Collection Opportunities — GSC + KE + SERP intent → collection page priorities
- Information Gain Analysis — consensus vs unique claims across SERP
- SERP Feature Landscape — how often AI Overview, PAA, featured snippets appear
- Search Intent Map — info/commercial/transactional/navigational split
- Seasonal Trends Report — monthly volume trends, SVG heatmap, timing recommendations
- Keyword Difficulty Map — difficulty distribution, SVG scatter, winnability scoring
- Keyword Cannibalization Detector — $-at-risk calculation, URL rotation detection

#### 🏆 Competitor Level (8 reports)
- Backlink Gap Analysis — 451+ missing link opportunities, LLM citation flagging
- Competitive SERP Analysis — 176 ranking factors compared across competitors
- SEO Visibility Comparison — keyword overlap, competitor traffic comparison
- Portfolio Cross-Brand Audit — all 7 brands in one pass
- Competitive Link Velocity — who's gaining fastest, new link intercepts
- Competitor Content Gap — keywords competitors rank for that client doesn't
- Share of Voice Trend — SoV comparison, SVG pie chart, position distribution
- Competitor Page Monitor — tracks new pages published by competitors

#### 🔑 Keyword Level (5 reports)
- Keyword Cannibalization Detector — live + historical SERPs, conflict classification
- GSC Query + Page Analysis — 1000 queries × 1000 pages
- Striking Distance Keywords — positions 5-20 with 10+ impressions
- Keyword Rank Tracker — position history with delta alerts, SVG distribution
- Content Decay Detector — pages losing traffic, decay severity scoring

#### 🛒 Shopify-Specific (3 reports)
- Shopify Duplicate Detector — finds Shopify-generated duplicate URLs, maps canonical chains
- Shopify Schema Auditor — page-type-specific schema checks, Liquid fix instructions
- Shopify Crawl Budget — Shopify-specific waste identification, robots.txt recommendations

#### 🤖 Infrastructure
- MCP Server — 20 SEO tools exposed as MCP tools for AI agents (Claude Code, Hermes, Codex)
- QA Validator — validates all reports before publishing
- Scheduled Report Runner — orchestrator for automated weekly reports via cron
- Portal Dashboard — client dashboard with KPI cards and report library
- Domain Resolver — auto-try www.domain.com if domain.com fails

## Quick Start

### Install

```bash
pip install zenfusion-seo
```

### Configure

Create a `.env` file with your API keys:

```env
DATAFORSEO_LOGIN=your_login
DATAFORSEO_PASSWORD=your_password
SCRAPEOWL_API_KEY=your_key
PERPLEXITY_API_KEY=your_key
KEYWORDS_EVERYWHERE_API_KEY=your_key
INFRANODUS_API_KEY=your_key
INDEXBOLT_API_KEY=your_key
PSI_API_KEY=your_key  # Optional, free tier works
```

### Run

```bash
# CLI
zenfusion-seo site-audit --domain softprowatersystems.com
zenfusion-seo page-audit --url https://softprowatersystems.com/products/softpro-elite-water-softener --keyword "best water softener"
zenfusion-seo ai-visibility --domain softprowatersystems.com --keyword "best water softener"

# Web dashboard
zenfusion-seo serve --port 5000
```

### Use with MCP (Claude Code, Hermes, etc.)

```bash
# Claude Desktop — add to claude_desktop_config.json:
{
  "mcpServers": {
    "zenfusion": {
      "command": "python3",
      "args": ["zenfusion_seo/mcp_server.py"]
    }
  }
}

# Hermes — add to config.yaml:
mcp_servers:
  zenfusion:
    command: "python3"
    args: ["zenfusion_seo/mcp_server.py"]
    timeout: 300
```

Then just ask: "Run a site audit on softprowatersystems.com"

## Data Sources

| Source | What It Provides | Cost |
|---|---|---|
| DataForSEO | SERP, backlinks, on-page, keywords, competitors, AI optimization | Pay-as-you-go |
| Google PSI | Lighthouse, CrUX, Core Web Vitals | Free |
| Google Search Console | Impressions, clicks, CTR, position | Free |
| Perplexity Sonar API | AEO citation testing, brand mentions | ~$0.005/query |
| Keywords Everywhere | Search volume, CPC, competition, trends | Prepaid credits |
| InfraNodus | Entity extraction, knowledge graph analysis | API key |
| ScrapeOwl | JS-rendered page scraping | Prepaid credits |
| IndexBolt | Google indexing for backlinks | Credits |
| Majestic Million | Domain trust/authority index | Free (1M domains) |

## Unique Advantages Over Commercial Tools

| Feature | Why No One Else Has It |
|---|---|
| 4-LLM AEO Testing | Tests ChatGPT, Gemini, Claude, Perplexity — no tool tests all 4 |
| Citation Readiness Rubric | Based on real Claude/Perplexity citation patterns |
| Information Gain Analysis | Finds content gaps competitors don't see |
| $-at-Risk Cannibalization | Dollarifies the problem for client buy-in |
| Lost Link Email Templates | Generates outreach copy — no tool does this |
| Seasonality Heatmap | 56+ months of data with timing recommendations |
| Link Velocity Intercepts | Domains linking to competitors but not you |
| Google-style SERP Preview | Visual context no data table can match |
| Schema as Ranking Factor | Priority + required properties per type |
| Shopify-Specific Audits | Duplicate URLs, schema per page type, crawl budget |

## License

MIT — fork it, self-host it, customize it.

## Contributing

PRs welcome. See CONTRIBUTING.md.