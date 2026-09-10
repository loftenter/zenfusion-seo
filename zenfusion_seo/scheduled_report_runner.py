#!/usr/bin/env python3
"""
scheduled_report_runner.py — Standalone orchestrator that runs scheduled SEO
reports and generates an HTML summary.

This is a SCHEDULER/ORCHESTRATOR, not a report generator. It invokes existing
Zenfusion SEO scripts (site_auditor.py, keyword_rank_tracker.py, etc.),
captures their output, persists per-report result files, and produces a
single HTML dashboard linking to them.

Designed to be invoked by a cron job:

    python3 ~/.hermes/scripts/scheduled_report_runner.py \
        --domain softprowatersystems.com \
        --reports site_audit,rank_tracking,backlink_alerts \
        --output /tmp/test_scheduled

Report schedule is configurable via a JSON file at
~/.hermes/audits/schedule_config.json. If absent, a built-in default schedule
covering all supported report types is used.

Python 3.9 compatible. White SaaS theme. noindex/nofollow.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import html
import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

SCRIPTS_DIR = Path.home() / ".hermes" / "scripts"
SCHEDULE_CONFIG_PATH = Path.home() / ".hermes" / "audits" / "schedule_config.json"

# Built-in default schedule: report_key -> spec describing how to invoke it.
# Each spec carries the script filename (relative to SCRIPTS_DIR) and a list of
# CLI argument templates. Templates may reference {domain} which is substituted
# at runtime. The first positional arg is always the script path; python3 is
# prepended automatically.
DEFAULT_SCHEDULE: Dict[str, Dict[str, Any]] = {
    "site_audit": {
        "script": "site_auditor.py",
        "args": ["--domain", "{domain}"],
        "description": "Full technical site audit",
    },
    "rank_tracking": {
        "script": "keyword_rank_tracker.py",
        "args": ["--domain", "{domain}", "--keywords", "water softener,best water softener,water filtration system"],
        "description": "Keyword rank tracking",
    },
    "backlink_alerts": {
        "script": "backlink_alert_report.py",
        "args": ["--domain", "{domain}"],
        "description": "Backlink alert report (new/lost links)",
    },
    "content_decay": {
        "script": "content_decay_detector.py",
        "args": ["--domain", "{domain}"],
        "description": "Content decay / freshness detector",
    },
    "toxic_links": {
        "script": "toxic_link_detector.py",
        "args": ["--domain", "{domain}"],
        "description": "Toxic backlink detector",
    },
    "ai_visibility": {
        "script": "ai_visibility_helper.py",
        "args": ["--brand", "{domain}", "--keyword", "best water softener"],
        "description": "AI answer-engine visibility helper",
    },
    "seo_visibility": {
        "script": "seo_visibility_report.py",
        "args": ["--domain", "{domain}", "--competitors", "aquasana.com,culligan.com"],
        "description": "SEO visibility vs. competitors",
    },
}

# --------------------------------------------------------------------------- #
# Schedule config
# --------------------------------------------------------------------------- #

def get_report_schedule(domain: str) -> List[Dict[str, Any]]:
    """Return the ordered list of reports to run for *domain*.

    The schedule is read from ``~/.hermes/audits/schedule_config.json`` if it
    exists; otherwise the built-in :data:`DEFAULT_SCHEDULE` (all report types)
    is returned. The config file may be either:

    * a JSON object mapping report_key -> spec (preserves insertion order on
      Python 3.7+), or
    * a JSON object with a top-level ``"reports"`` key whose value is a list of
      report keys (to select/order a subset), or a list of spec dicts.

    Each spec dict supports keys: ``script`` (filename), ``args`` (list of
    template strings), ``description`` (str), and ``enabled`` (bool, default
    True). ``{domain}`` placeholders in args are substituted with *domain*.

    Returns a list of fully-resolved spec dicts (in execution order), each
    augmented with: ``key``, ``script_path`` (absolute), ``cmd`` (list[str]).
    """
    config: Optional[Dict[str, Any]] = None
    order: Optional[List[str]] = None

    if SCHEDULE_CONFIG_PATH.exists():
        try:
            with SCHEDULE_CONFIG_PATH.open("r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[WARN] Failed to read schedule config ({exc}); using defaults.",
                  file=sys.stderr)
            raw = None

        if isinstance(raw, list):
            # List of report keys OR list of spec dicts.
            config = {}
            for item in raw:
                if isinstance(item, str):
                    order = order or []
                    order.append(item)
                elif isinstance(item, dict) and "key" in item:
                    config[item["key"]] = item
                    order = order or []
                    order.append(item["key"])
        elif isinstance(raw, dict):
            config = raw
            if "reports" in raw and isinstance(raw["reports"], list):
                order = [r for r in raw["reports"] if isinstance(r, str)]

    # Merge config over defaults so unknown keys still have sane defaults.
    schedule: Dict[str, Dict[str, Any]] = dict(DEFAULT_SCHEDULE)
    if config:
        for key, spec in config.items():
            if not isinstance(spec, dict):
                continue
            merged = dict(schedule.get(key, {}))
            merged.update(spec)
            schedule[key] = merged

    keys = order if order else list(schedule.keys())

    resolved: List[Dict[str, Any]] = []
    for key in keys:
        spec = schedule.get(key)
        if not spec:
            continue
        if not spec.get("enabled", True):
            continue
        script_name = spec.get("script", "")
        if not script_name:
            continue
        script_path = SCRIPTS_DIR / script_name
        # Substitute {domain} in arg templates.
        args = [a.replace("{domain}", domain) for a in spec.get("args", [])]
        resolved.append({
            "key": key,
            "script": script_name,
            "script_path": str(script_path),
            "args": args,
            "description": spec.get("description", key),
            "cmd": [sys.executable, str(script_path), *args],
        })
    return resolved


# --------------------------------------------------------------------------- #
# Report runner
# --------------------------------------------------------------------------- #

def _extract_metrics(report_key: str, stdout_text: str) -> Dict[str, Any]:
    """Best-effort extraction of a few headline metrics from report stdout.

    Looks for common patterns like ``key: value`` or ``key = value`` near the
    top of the output. Returns at most 6 key/value pairs as strings.
    """
    metrics: Dict[str, Any] = {}
    if not stdout_text:
        return metrics
    # Restrict to first ~4000 chars for cheapness.
    head = stdout_text[:4000]
    for line in head.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "//", "/*")):
            continue
        for sep in (": ", " = ", "="):
            if sep in line:
                k, _, v = line.partition(sep)
                k = k.strip().strip("*-→> ").strip()
                v = v.strip().strip(",;")
                if not k or not v:
                    continue
                # Keep it simple: key must be short, value single-token-ish.
                if len(k) <= 40 and len(v) <= 60 and " " not in k:
                    metrics[k] = v
                    break
        if len(metrics) >= 6:
            break
    return metrics


def run_scheduled_reports(
    domain: str,
    reports: Optional[List[str]] = None,
    output_dir: str = "/tmp/zenfusion_scheduled",
) -> List[Dict[str, Any]]:
    """Run each requested report and return a list of result dicts.

    Parameters
    ----------
    domain : str
        The target domain (e.g. ``softprowatersystems.com``).
    reports : list[str] or None
        Report keys to run. ``None`` means "run the full configured schedule".
    output_dir : str
        Directory where per-report stdout/stderr files are written. Created
        if it does not exist.

    Returns
    -------
    list[dict]
        One result dict per report, in execution order. Each dict has keys:
        ``key``, ``description``, ``status`` ("success"|"failed"|"skipped"),
        ``returncode`` (int or None), ``duration_s`` (float), ``output_file``
        (path to saved stdout, or None), ``error_file`` (path to stderr, or
        None), ``error`` (short error string or None), ``metrics`` (dict),
        ``started_at`` (ISO8601), ``command`` (display string).
    """
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    full_schedule = get_report_schedule(domain)
    schedule_by_key = {s["key"]: s for s in full_schedule}

    if reports:
        # Preserve requested order; skip unknown keys gracefully.
        selected: List[Dict[str, Any]] = []
        for r in reports:
            spec = schedule_by_key.get(r)
            if spec is None:
                # Record a skipped/unknown entry so the summary surfaces it.
                selected.append({"key": r, "unknown": True})
            else:
                selected.append(spec)
    else:
        selected = full_schedule

    results: List[Dict[str, Any]] = []
    timestamp_tag = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")

    for spec in selected:
        key = spec.get("key", "?")
        if spec.get("unknown"):
            results.append({
                "key": key,
                "description": "(unknown report type)",
                "status": "skipped",
                "returncode": None,
                "duration_s": 0.0,
                "output_file": None,
                "error_file": None,
                "error": f"Unknown report type: {key}",
                "metrics": {},
                "started_at": _dt.datetime.now().isoformat(timespec="seconds"),
                "command": "",
            })
            continue

        cmd: List[str] = spec["cmd"]
        script_path = spec["script_path"]
        started_at = _dt.datetime.now().isoformat(timespec="seconds")
        t0 = time.monotonic()

        result_entry: Dict[str, Any] = {
            "key": key,
            "description": spec.get("description", key),
            "status": "failed",
            "returncode": None,
            "duration_s": 0.0,
            "output_file": None,
            "error_file": None,
            "error": None,
            "metrics": {},
            "started_at": started_at,
            "command": " ".join(cmd),
        }

        if not Path(script_path).exists():
            result_entry["error"] = f"Script not found: {script_path}"
            result_entry["status"] = "skipped"
            results.append(result_entry)
            print(f"[SKIP] {key}: script not found -> {script_path}", file=sys.stderr)
            continue

        stdout_file = out_path / f"{key}_{timestamp_tag}.stdout.log"
        stderr_file = out_path / f"{key}_{timestamp_tag}.stderr.log"

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600,  # 10 min per report
            )
            stdout_text = proc.stdout or ""
            stderr_text = proc.stderr or ""
            result_entry["returncode"] = proc.returncode
            result_entry["status"] = "success" if proc.returncode == 0 else "failed"

            # Persist output files.
            try:
                stdout_file.write_text(stdout_text, encoding="utf-8")
                result_entry["output_file"] = str(stdout_file)
            except OSError as exc:
                result_entry["error"] = f"Failed to write stdout log: {exc}"
            try:
                stderr_file.write_text(stderr_text, encoding="utf-8")
                result_entry["error_file"] = str(stderr_file)
            except OSError:
                pass

            if proc.returncode != 0:
                tail = stderr_text.strip().splitlines()[-1] if stderr_text.strip() else "non-zero exit"
                result_entry["error"] = tail[:300]

            result_entry["metrics"] = _extract_metrics(key, stdout_text)

        except subprocess.TimeoutExpired as exc:
            result_entry["status"] = "failed"
            stdout_text = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr_text = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            result_entry["error"] = "Timed out after 600s"
            try:
                stdout_file.write_text(stdout_text, encoding="utf-8")
                result_entry["output_file"] = str(stdout_file)
            except OSError:
                pass
            try:
                stderr_file.write_text(stderr_text, encoding="utf-8")
                result_entry["error_file"] = str(stderr_file)
            except OSError:
                pass
        except FileNotFoundError as exc:
            result_entry["status"] = "failed"
            result_entry["error"] = f"Executable not found: {exc}"
        except Exception as exc:  # noqa: BLE001 - orchestrator must survive
            result_entry["status"] = "failed"
            result_entry["error"] = f"{type(exc).__name__}: {exc}"
            # Stash traceback in stderr file for debugging.
            try:
                stderr_file.write_text(traceback.format_exc(), encoding="utf-8")
                result_entry["error_file"] = str(stderr_file)
            except OSError:
                pass

        result_entry["duration_s"] = round(time.monotonic() - t0, 2)

        status_icon = {"success": "OK", "failed": "FAIL", "skipped": "SKIP"}.get(
            result_entry["status"], "?"
        )
        print(
            f"[{status_icon}] {key}: {result_entry['status']} "
            f"({result_entry['duration_s']}s, rc={result_entry['returncode']})",
            file=sys.stderr,
        )
        results.append(result_entry)

    return results


# --------------------------------------------------------------------------- #
# HTML summary
# --------------------------------------------------------------------------- #

def generate_schedule_summary(
    domain: str,
    results: List[Dict[str, Any]],
    output_path: str,
) -> str:
    """Write an HTML summary dashboard for *results* and return its path.

    The page uses a clean white SaaS theme and emits a ``noindex,nofollow``
    robots meta tag (these are internal operational reports, not public
    marketing pages).
    """
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    total = len(results)
    succeeded = sum(1 for r in results if r.get("status") == "success")
    failed = sum(1 for r in results if r.get("status") == "failed")
    skipped = sum(1 for r in results if r.get("status") == "skipped")
    now_str = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def esc(s: Any) -> str:
        return html.escape(str(s), quote=True)

    def status_badge(status: str) -> str:
        styles = {
            "success": "background:#dcfce7;color:#166534;border:1px solid #86efac;",
            "failed": "background:#fee2e2;color:#991b1b;border:1px solid #fca5a5;",
            "skipped": "background:#f1f5f9;color:#475569;border:1px solid #cbd5e1;",
        }
        style = styles.get(status, styles["skipped"])
        return f'<span class="badge" style="{style}">{esc(status.upper())}</span>'

    rows_html: List[str] = []
    for r in results:
        key = esc(r.get("key", "?"))
        desc = esc(r.get("description", ""))
        status = r.get("status", "skipped")
        rc = esc(r.get("returncode") if r.get("returncode") is not None else "—")
        dur = esc(r.get("duration_s", 0))
        cmd = esc(r.get("command", "") or "")
        err = esc(r.get("error") or "")
        out_file = r.get("output_file")
        err_file = r.get("error_file")
        metrics = r.get("metrics") or {}

        metrics_html = ""
        if metrics:
            chips = []
            for mk, mv in metrics.items():
                chips.append(
                    f'<span class="chip"><b>{esc(mk)}</b>: {esc(mv)}</span>'
                )
            metrics_html = '<div class="metrics">' + "".join(chips) + "</div>"

        links_html = ""
        if out_file:
            link = esc(out_file)
            links_html += f'<a href="file://{link}" target="_blank" rel="nofollow">stdout</a>'
        if err_file:
            link = esc(err_file)
            sep = " · " if links_html else ""
            links_html += f'{sep}<a href="file://{link}" target="_blank" rel="nofollow">stderr</a>'
        if not links_html:
            links_html = '<span class="muted">—</span>'

        err_html = ""
        if err:
            err_html = f'<div class="error">⚠ {err}</div>'

        rows_html.append(f"""
        <tr>
          <td><code>{key}</code><div class="desc">{desc}</div></td>
          <td>{status_badge(status)}</td>
          <td>{rc}</td>
          <td>{dur}s</td>
          <td>{metrics_html}{err_html}</td>
          <td class="links">{links_html}</td>
        </tr>""")

    rows_block = "\n".join(rows_html) if rows_html else (
        '<tr><td colspan="6" class="muted">No reports were run.</td></tr>'
    )

    summary_card = f"""
    <div class="cards">
      <div class="card"><div class="card-num">{total}</div><div class="card-label">Total Reports</div></div>
      <div class="card card-ok"><div class="card-num">{succeeded}</div><div class="card-label">Succeeded</div></div>
      <div class="card card-fail"><div class="card-num">{failed}</div><div class="card-label">Failed</div></div>
      <div class="card card-skip"><div class="card-num">{skipped}</div><div class="card-label">Skipped</div></div>
    </div>
    """

    html_doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="robots" content="noindex,nofollow">
<title>Scheduled Report Summary — {esc(domain)}</title>
<style>
  :root {{
    --bg:#f8fafc; --card:#ffffff; --ink:#0f172a; --muted:#64748b;
    --border:#e2e8f0; --accent:#2563eb;
  }}
  * {{ box-sizing:border-box; }}
  body {{
    margin:0; padding:32px; background:var(--bg); color:var(--ink);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    font-size:14px; line-height:1.5;
  }}
  header {{ margin-bottom:24px; }}
  h1 {{ margin:0 0 4px; font-size:22px; font-weight:700; }}
  .subtitle {{ color:var(--muted); font-size:13px; }}
  .cards {{ display:flex; gap:16px; margin:20px 0 24px; flex-wrap:wrap; }}
  .card {{
    background:var(--card); border:1px solid var(--border); border-radius:12px;
    padding:16px 20px; min-width:120px; box-shadow:0 1px 2px rgba(0,0,0,.04);
  }}
  .card-num {{ font-size:26px; font-weight:700; }}
  .card-label {{ color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.04em; }}
  .card-ok .card-num {{ color:#16a34a; }}
  .card-fail .card-num {{ color:#dc2626; }}
  .card-skip .card-num {{ color:#64748b; }}
  table {{ width:100%; border-collapse:collapse; background:var(--card);
    border:1px solid var(--border); border-radius:12px; overflow:hidden; }}
  th,td {{ padding:12px 14px; text-align:left; vertical-align:top; border-bottom:1px solid var(--border); }}
  th {{ background:#f1f5f9; font-weight:600; font-size:12px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); }}
  tr:last-child td {{ border-bottom:none; }}
  code {{ font-family:"SF Mono",Menlo,Consolas,monospace; font-size:12px; background:#f1f5f9; padding:2px 6px; border-radius:4px; }}
  .desc {{ color:var(--muted); font-size:12px; margin-top:2px; }}
  .badge {{ display:inline-block; padding:2px 10px; border-radius:999px; font-size:11px; font-weight:600; letter-spacing:.03em; }}
  .metrics {{ display:flex; flex-wrap:wrap; gap:6px; margin-bottom:4px; }}
  .chip {{ display:inline-block; background:#eff6ff; border:1px solid #bfdbfe; color:#1e40af;
    border-radius:6px; padding:2px 8px; font-size:12px; }}
  .error {{ color:#b91c1c; font-size:12px; margin-top:4px; word-break:break-word; }}
  .links a {{ color:var(--accent); text-decoration:none; font-size:12px; }}
  .links a:hover {{ text-decoration:underline; }}
  .muted {{ color:var(--muted); }}
  footer {{ margin-top:24px; color:var(--muted); font-size:11px; }}
  .cmd {{ font-size:11px; color:var(--muted); margin-top:4px; word-break:break-all; }}
</style>
</head>
<body>
<header>
  <h1>Scheduled Report Summary</h1>
  <div class="subtitle">Domain: <strong>{esc(domain)}</strong> · Generated {esc(now_str)}</div>
</header>
{summary_card}
<table>
  <thead>
    <tr>
      <th>Report</th><th>Status</th><th>Exit</th><th>Duration</th><th>Metrics / Error</th><th>Logs</th>
    </tr>
  </thead>
  <tbody>
{rows_block}
  </tbody>
</table>
<footer>
  <p>Generated by scheduled_report_runner.py · Internal operational report · noindex/nofollow</p>
</footer>
</body>
</html>
"""

    out.write_text(html_doc, encoding="utf-8")
    return str(out)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run scheduled SEO reports and generate an HTML summary.",
    )
    parser.add_argument("--domain", required=True, help="Target domain.")
    parser.add_argument(
        "--reports",
        default="",
        help="Comma-separated report keys to run (default: full schedule).",
    )
    parser.add_argument(
        "--output",
        default="/tmp/zenfusion_scheduled",
        help="Output directory for report logs and summary HTML.",
    )
    parser.add_argument(
        "--summary-name",
        default="schedule_summary.html",
        help="Filename for the HTML summary inside --output (default: schedule_summary.html).",
    )
    args = parser.parse_args(argv)

    domain = args.domain.strip()
    if not domain:
        print("Error: --domain is required.", file=sys.stderr)
        return 2

    reports = None
    if args.reports.strip():
        reports = [r.strip() for r in args.reports.split(",") if r.strip()]

    print(f"=== Scheduled Report Runner ===", file=sys.stderr)
    print(f"Domain : {domain}", file=sys.stderr)
    print(f"Reports: {reports if reports else '(full schedule)'}", file=sys.stderr)
    print(f"Output : {args.output}", file=sys.stderr)

    results = run_scheduled_reports(domain, reports, args.output)

    summary_path = generate_schedule_summary(
        domain, results, os.path.join(args.output, args.summary_name)
    )

    ok = sum(1 for r in results if r.get("status") == "success")
    total = len(results)
    any_failed = any(r.get("status") == "failed" for r in results)
    print(f"\nDone: {ok}/{total} reports succeeded.", file=sys.stderr)
    print(f"Summary: file://{summary_path}", file=sys.stderr)
    # Also print the summary path to stdout for machine consumers.
    print(summary_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
