"""
Zenfusion Audit Submission Processor

Runs as a cron job / GitHub Action. Checks the Google Sheet for new audit submissions,
creates GitHub Issues for each, which triggers the audit-runner workflow.

Also can be run directly:
  python3 process_audit_submissions.py
"""

import os, json, base64, urllib.request, csv, time, sys
from datetime import datetime

# ── Config ───────────────────────────────────────────────────────────────────

# Google Sheet ID (the same one the portal writes to via Apps Script)
# The Apps Script writes form submissions to this sheet
SHEET_ID = os.environ.get("ZENFUSION_SHEET_ID", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "loftenter/zenfusion-audits")

# Fallback: try to load from .env
ENV_PATH = os.path.expanduser("~/.hermes/.env")
if not GITHUB_TOKEN and os.path.exists(ENV_PATH):
    with open(ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if line.startswith("GITHUB_TOKEN="):
                GITHUB_TOKEN = line.split("=", 1)[1]

# ── GitHub Issue Creation ───────────────────────────────────────────────────

def create_github_issue(title, body, labels):
    """Create a GitHub issue via the API."""
    if not GITHUB_TOKEN:
        print("ERROR: GITHUB_TOKEN not set")
        return None
    
    url = f"https://api.github.com/repos/{GITHUB_REPO}/issues"
    payload = json.dumps({
        "title": title,
        "body": body,
        "labels": labels
    }).encode()
    
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Authorization", f"token {GITHUB_TOKEN}")
    req.add_header("Accept", "application/vnd.github.v3+json")
    req.add_header("Content-Type", "application/json")
    
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            print(f"  Issue created: {data.get('html_url', 'N/A')}")
            return data
    except Exception as e:
        print(f"  Error creating issue: {e}")
        return None


def check_existing_issues():
    """Check what audit issues already exist to avoid duplicates."""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/issues?state=open&per_page=100"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"token {GITHUB_TOKEN}")
    req.add_header("Accept", "application/vnd.github.v3+json")
    
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            issues = json.loads(resp.read())
            existing = set()
            for issue in issues:
                existing.add(issue.get("title", ""))
            return existing
    except:
        return set()


def process_submissions():
    """Check for new submissions and create GitHub Issues."""
    print(f"\n{'='*60}")
    print(f"  Zenfusion Audit Submission Processor")
    print(f"  {datetime.now().isoformat()}")
    print(f"{'='*60}")
    
    if not GITHUB_TOKEN:
        print("  ERROR: GITHUB_TOKEN not found")
        return
    
    existing_issues = check_existing_issues()
    print(f"  Existing open issues: {len(existing_issues)}")
    
    # For now, this is a manual process — the team can also create issues
    # by running: gh issue create --label page-audit --title "SoftPro: page-audit — ..."
    # 
    # The Google Apps Script needs to be updated to handle create_issue action
    # Until then, submissions via the portal are logged to the Sheet
    # and the team can process them manually or via this script
    
    print("\n  No automated Sheet polling yet — needs Google Sheet API setup.")
    print("  Portal submissions are logged to the Google Sheet.")
    print("  To manually trigger: gh issue create --label page-audit ...")
    
    print(f"\n{'='*60}")
    print(f"  Done")
    print(f"{'='*60}")


if __name__ == "__main__":
    process_submissions()
