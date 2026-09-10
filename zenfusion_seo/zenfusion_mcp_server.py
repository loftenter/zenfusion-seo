#!/usr/bin/env python3
"""Zenfusion MCP Server — exposes Zenfusion SEO analysis scripts as MCP tools.

Run with the Hermes venv interpreter (where the `mcp` package lives):

    python3 ~/.hermes/scripts/zenfusion_mcp_server.py

Each tool wraps a script in ~/.hermes/scripts/ and returns its stdout.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool

SCRIPTS_DIR = Path(os.path.expanduser("~/.hermes/scripts"))
DEFAULT_TIMEOUT = 300  # seconds per script

# Use the Hermes venv python (which has mcp + script deps) when running child
# scripts, falling back to sys.executable.
HERMES_PYTHON = os.path.expanduser("~/.hermes/hermes-agent/venv/bin/python3")
if not os.path.exists(HERMES_PYTHON):
    HERMES_PYTHON = sys.executable


def _str(val: Any) -> str:
    """Coerce an argument to a string, handling None/bool/list."""
    if val is None:
        return ""
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, (list, tuple)):
        return ",".join(_str(v) for v in val)
    return str(val)


def _run_script(script_name: str, args: list[str], timeout: int = DEFAULT_TIMEOUT) -> str:
    """Execute a Zenfusion script and return its stdout (or an error message)."""
    script_path = SCRIPTS_DIR / script_name
    if not script_path.exists():
        return f"ERROR: script not found: {script_path}"

    cmd = [HERMES_PYTHON, str(script_path), *args]

    # Inherit DataForSEO / .env vars so the scripts can authenticate.
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(SCRIPTS_DIR),
            env=env,
        )
    except subprocess.TimeoutExpired:
        return f"ERROR: {script_name} timed out after {timeout}s"
    except Exception as e:  # pragma: no cover — defensive
        return f"ERROR: failed to launch {script_name}: {e}"

    out = proc.stdout or ""
    err = proc.stderr or ""
    if proc.returncode != 0:
        tail = err.strip().splitlines()[-5:] if err.strip() else out.strip().splitlines()[-5:]
        return f"ERROR: {script_name} exited {proc.returncode}\n" + "\n".join(tail)

    # If very long, keep the last ~4000 chars (most useful for reports).
    if len(out) > 20000:
        out = "…[truncated, showing last 20000 chars]…\n" + out[-20000:]
    if err.strip():
        out = out + "\n[stderr]\n" + err.strip()[-2000:]
    return out or "(no output)"


# ---------------------------------------------------------------------------
# Tool catalogue
# ---------------------------------------------------------------------------

def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    schema = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _str_prop(desc: str) -> dict[str, Any]:
    return {"type": "string", "description": desc}


def _arr_prop(desc: str) -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}, "description": desc}


def _num_prop(desc: str) -> dict[str, Any]:
    return {"type": "number", "description": desc}


def _bool_prop(desc: str) -> dict[str, Any]:
    return {"type": "boolean", "description": desc}


TOOL_DEFS: list[Tool] = [
    Tool(
        name="site_audit",
        description="Run a full SEO site audit for a domain via site_auditor.py.",
        input_schema=_schema(
            {"domain": _str_prop("Root domain to audit, e.g. example.com"), "limit": _num_prop("Max pages to crawl (optional)")},
            ["domain"],
        ),
    ),
    Tool(
        name="page_audit",
        description="Audit a single page for a target keyword via page_auditor.py.",
        input_schema=_schema(
            {"url": _str_prop("Full URL of the page to audit"), "keyword": _str_prop("Target keyword")},
            ["url", "keyword"],
        ),
    ),
    Tool(
        name="instant_page_audit",
        description="Instant on-page audit for a URL via instant_page_audit.py.",
        input_schema=_schema({"url": _str_prop("URL to audit")}, ["url"]),
    ),
    Tool(
        name="seo_visibility",
        description="Generate an SEO visibility report for a domain vs competitors via seo_visibility_report.py.",
        input_schema=_schema(
            {"domain": _str_prop("Client domain"), "competitors": _arr_prop("Competitor domains")},
            ["domain", "competitors"],
        ),
    ),
    Tool(
        name="ai_visibility",
        description="Check AI/LLM visibility for a brand + keyword via ai_visibility_helper.py.",
        input_schema=_schema(
            {"brand": _str_prop("Brand name"), "keyword": _str_prop("Keyword/query")},
            ["brand", "keyword"],
        ),
    ),
    Tool(
        name="backlink_gap",
        description="Find backlink gaps between client and a competitor via backlink_gap_analyzer.py.",
        input_schema=_schema(
            {"client_domain": _str_prop("Client domain"), "competitor_domain": _str_prop("Competitor domain")},
            ["client_domain", "competitor_domain"],
        ),
    ),
    Tool(
        name="anchor_text",
        description="Analyze anchor-text distribution for a domain via anchor_text_analysis.py.",
        input_schema=_schema({"domain": _str_prop("Domain to analyze")}, ["domain"]),
    ),
    Tool(
        name="link_velocity",
        description="Compare link velocity for a client vs competitors via competitive_link_velocity.py.",
        input_schema=_schema(
            {"client_domain": _str_prop("Client domain"), "competitors": _arr_prop("Competitor domains")},
            ["client_domain", "competitors"],
        ),
    ),
    Tool(
        name="lost_backlinks",
        description="Identify lost backlinks for a domain via lost_backlink_recovery.py.",
        input_schema=_schema({"domain": _str_prop("Domain to analyze")}, ["domain"]),
    ),
    Tool(
        name="toxic_links",
        description="Detect toxic backlinks pointing at a domain via toxic_link_detector.py.",
        input_schema=_schema({"domain": _str_prop("Domain to analyze")}, ["domain"]),
    ),
    Tool(
        name="content_decay",
        description="Detect content decay (ranking/traffic decline) for a domain via content_decay_detector.py.",
        input_schema=_schema({"domain": _str_prop("Domain to analyze")}, ["domain"]),
    ),
    Tool(
        name="cannibalization",
        description="Detect keyword cannibalization for a domain via cannibalization_detector.py.",
        input_schema=_schema(
            {"domain": _str_prop("Domain to check"), "keywords": _arr_prop("Keywords to check for cannibalization")},
            ["domain", "keywords"],
        ),
    ),
    Tool(
        name="rank_tracking",
        description="Track keyword rankings for a domain via keyword_rank_tracker.py.",
        input_schema=_schema(
            {"domain": _str_prop("Domain to track"), "keywords": _arr_prop("Keywords to track")},
            ["domain", "keywords"],
        ),
    ),
    Tool(
        name="content_plan",
        description="Build a content plan from a seed keyword via content_plan_builder.py.",
        input_schema=_schema({"seed_keyword": _str_prop("Seed keyword/topic")}, ["seed_keyword"]),
    ),
    Tool(
        name="keyword_difficulty",
        description="Map keyword difficulty for a set of keywords + domain via keyword_difficulty_map.py.",
        input_schema=_schema(
            {"keywords": _arr_prop("Keywords to score"), "domain": _str_prop("Domain for SERP overlap context")},
            ["keywords", "domain"],
        ),
    ),
    Tool(
        name="serp_features",
        description="Check SERP feature presence for a keyword + target URL via serp_feature_check.py.",
        input_schema=_schema(
            {"keyword": _str_prop("Keyword/query"), "url": _str_prop("Target URL")},
            ["keyword", "url"],
        ),
    ),
    Tool(
        name="video_seo",
        description="Track video SEO rankings for keywords + domain via video_seo_tracker.py.",
        input_schema=_schema(
            {"keywords": _arr_prop("Keywords to track"), "domain": _str_prop("Domain to track")},
            ["keywords", "domain"],
        ),
    ),
    Tool(
        name="local_grid",
        description="Track local SEO grid rankings via local_seo_grid_tracker.py.",
        input_schema=_schema(
            {
                "keyword": _str_prop("Local keyword, e.g. 'plumber'"),
                "business": _str_prop("Business name"),
                "lat": _num_prop("Latitude of grid center"),
                "lng": _num_prop("Longitude of grid center"),
            },
            ["keyword", "business", "lat", "lng"],
        ),
    ),
    Tool(
        name="brand_mentions",
        description="Monitor brand mentions across the web via brand_mention_monitor.py.",
        input_schema=_schema(
            {"domain": _str_prop("Domain to monitor"), "brand": _str_prop("Brand name to search for")},
            ["domain", "brand"],
        ),
    ),
    Tool(
        name="competitor_content_gap",
        description="Find content gaps between a client and competitors via competitor_content_gap.py.",
        input_schema=_schema(
            {"client_domain": _str_prop("Client domain"), "competitors": _arr_prop("Competitor domains")},
            ["client_domain", "competitors"],
        ),
    ),
]


# Map tool name -> (script filename, ordered argument flag builders).
# Each builder returns a list of CLI args (flags + values) from the kwargs dict.
def _flags(kwargs: dict[str, Any], *names: str) -> list[str]:
    """Emit --flag value pairs for each present kwarg."""
    args: list[str] = []
    domain_keys = {"domain", "client_domain", "competitor_domain", "client", "competitor"}
    for n in names:
        if n in kwargs and kwargs[n] is not None:
            val = _domain(kwargs[n]) if n in domain_keys else _str(kwargs[n])
            args += [f"--{n.replace('_', '-')}", val]
    return args


def _posargs(*vals: Any) -> list[str]:
    return [_domain(v) if i == 0 else _str(v) for v in vals if v is not None]


def build_command(tool_name: str, args: dict[str, Any]) -> tuple[str, list[str]]:
    """Return (script_name, cli_args) for a given tool call.

    Matches each underlying script's actual CLI conventions (positional vs
    --flags, required --output, --seed, etc.).
    """
    out_base = os.path.expanduser("~/.hermes/audits")

    if tool_name == "site_audit":
        # site_auditor.py: positional domain, optional --limit
        cmd = _posargs(args.get("domain"))
        if args.get("limit") is not None:
            cmd += ["--limit", _str(args["limit"])]
        return "site_auditor.py", cmd

    if tool_name == "page_audit":
        # page_auditor.py: positional url keyword
        return "page_auditor.py", _posargs(args.get("url"), args.get("keyword"))

    if tool_name == "instant_page_audit":
        return "instant_page_audit.py", _flags(args, "url")

    if tool_name == "seo_visibility":
        return "seo_visibility_report.py", _flags(args, "domain", "competitors")

    if tool_name == "ai_visibility":
        # ai_visibility_helper.py: --brand, --keyword (repeatable)
        cmd = ["--brand", _str(args.get("brand"))]
        kws = args.get("keyword")
        if isinstance(kws, list):
            for k in kws:
                cmd += ["--keyword", _str(k)]
        else:
            cmd += ["--keyword", _str(kws)]
        return "ai_visibility_helper.py", cmd

    if tool_name == "backlink_gap":
        # backlink_gap_analyzer.py: --client --competitor
        cmd = []
        if args.get("client_domain") is not None:
            cmd += ["--client", _domain(args["client_domain"])]
        if args.get("competitor_domain") is not None:
            cmd += ["--competitor", _domain(args["competitor_domain"])]
        return "backlink_gap_analyzer.py", cmd

    if tool_name == "anchor_text":
        return "anchor_text_analysis.py", _flags(args, "domain")

    if tool_name == "link_velocity":
        # competitive_link_velocity.py: --client --competitors --output (required)
        cmd = []
        if args.get("client_domain") is not None:
            cmd += ["--client", _domain(args["client_domain"])]
        if args.get("competitors") is not None:
            cmd += ["--competitors", _str(args["competitors"])]
        cmd += ["--output", os.path.join(out_base, "link-velocity")]
        return "competitive_link_velocity.py", cmd

    if tool_name == "lost_backlinks":
        return "lost_backlink_recovery.py", _flags(args, "domain")

    if tool_name == "toxic_links":
        return "toxic_link_detector.py", _flags(args, "domain")

    if tool_name == "content_decay":
        return "content_decay_detector.py", _flags(args, "domain")

    if tool_name == "cannibalization":
        # cannibalization_detector.py: --domain --keywords --output (required)
        cmd = _flags(args, "domain", "keywords")
        cmd += ["--output", os.path.join(out_base, "cannibalization")]
        return "cannibalization_detector.py", cmd

    if tool_name == "rank_tracking":
        # keyword_rank_tracker.py: --domain --keywords --output (required)
        cmd = _flags(args, "domain", "keywords")
        cmd += ["--output", os.path.join(out_base, "rank-tracking")]
        return "keyword_rank_tracker.py", cmd

    if tool_name == "content_plan":
        # content_plan_builder.py: --seed
        cmd = ["--seed", _str(args.get("seed_keyword"))]
        return "content_plan_builder.py", cmd

    if tool_name == "keyword_difficulty":
        # keyword_difficulty_map.py: --seed (required) --domain --keywords
        # Use first keyword as seed if seed not explicitly provided.
        seed = args.get("seed") or args.get("seed_keyword")
        if not seed:
            kws = args.get("keywords")
            if isinstance(kws, list) and kws:
                seed = kws[0]
            elif isinstance(kws, str) and kws:
                seed = kws.split(",")[0]
        cmd = ["--seed", _str(seed)]
        if args.get("domain"):
            cmd += ["--domain", _domain(args["domain"])]
        if args.get("keywords"):
            cmd += ["--keywords", _str(args["keywords"])]
        return "keyword_difficulty_map.py", cmd

    if tool_name == "serp_features":
        return "serp_feature_check.py", _flags(args, "keyword", "url")

    if tool_name == "video_seo":
        # video_seo_tracker.py: --keywords --domain --output (required)
        cmd = _flags(args, "keywords", "domain")
        cmd += ["--output", os.path.join(out_base, "video-seo")]
        return "video_seo_tracker.py", cmd

    if tool_name == "local_grid":
        return "local_seo_grid_tracker.py", _flags(args, "keyword", "business", "lat", "lng")

    if tool_name == "brand_mentions":
        return "brand_mention_monitor.py", _flags(args, "domain", "brand")

    if tool_name == "competitor_content_gap":
        # competitor_content_gap.py: --client --competitors
        cmd = []
        if args.get("client_domain") is not None:
            cmd += ["--client", _domain(args["client_domain"])]
        if args.get("competitors") is not None:
            cmd += ["--competitors", _str(args["competitors"])]
        return "competitor_content_gap.py", cmd

    raise ValueError(f"unknown tool: {tool_name}")


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def on_list_tools(ctx, params=None):
    return ListToolsResult(tools=TOOL_DEFS)


async def on_call_tool(ctx, params):
    name = params.name
    args = params.arguments or {}
    try:
        script_name, cli_args = build_command(name, args)
    except ValueError as e:
        return CallToolResult(content=[TextContent(type="text", text=f"ERROR: {e}")], is_error=True)

    result = await asyncio.to_thread(_run_script, script_name, cli_args)
    return CallToolResult(content=[TextContent(type="text", text=result)])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    server = Server(
        "zenfusion",
        instructions="Zenfusion SEO toolset: run site audits, page audits, backlink analysis, rank tracking, and more.",
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
