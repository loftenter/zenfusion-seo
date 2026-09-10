"""
ZenfusionSEO API — Flask web dashboard for running reports interactively.

Provides a simple web UI where the team can:
- Run any of the 40+ SEO tools via forms
- View report results inline
- Browse the report library organized by level
- See KPI summary from recent reports
"""

from flask import Flask, request, jsonify, render_template_string
import os
import sys
import subprocess
import json

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def create_app():
    app = Flask(__name__, template_folder='../templates')
    
    # Import tool registry
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from cli import TOOLS
    
    @app.route('/')
    def index():
        """Dashboard home — tool library organized by level."""
        levels = {}
        for name, (script, desc, level) in TOOLS.items():
            if level not in levels:
                levels[level] = []
            levels[level].append({"name": name, "desc": desc, "script": script})
        
        return render_template_string(DASHBOARD_HTML, levels=levels, total_tools=len(TOOLS))
    
    @app.route('/api/run/<tool_name>', methods=['POST'])
    def run_tool(tool_name):
        """Run a ZenfusionSEO tool and return results."""
        if tool_name not in TOOLS:
            return jsonify({"error": f"Unknown tool: {tool_name}"}), 404
        
        script, desc, level = TOOLS[tool_name]
        script_path = os.path.join(SCRIPTS_DIR, script)
        
        if not os.path.exists(script_path):
            return jsonify({"error": f"Script not found: {script}"}), 404
        
        # Get params from request
        params = request.json or request.form or {}
        args_list = []
        output_dir = params.get('output', f'/tmp/zenfusion_{tool_name}')
        
        for key, val in params.items():
            if key in ('output',):
                continue
            if val is not None and val != '':
                flag = f"--{key.replace('_', '-')}"
                args_list.extend([flag, str(val)])
        
        # For page-audit, URL and keyword are positional
        if tool_name == "page-audit":
            url = params.get('url', '')
            keyword = params.get('keyword', '')
            args_list = [url, keyword]
        
        args_list.extend(['--output', output_dir])
        
        cmd = [sys.executable, script_path] + args_list
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            return jsonify({
                "success": result.returncode == 0,
                "output": result.stdout[-2000:] if len(result.stdout) > 2000 else result.stdout,
                "error": result.stderr[-500:] if result.stderr else None,
                "output_dir": output_dir,
            })
        except subprocess.TimeoutExpired:
            return jsonify({"error": "Script timed out"}), 504
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    
    @app.route('/api/tools')
    def list_tools():
        """List all available tools."""
        return jsonify({
            name: {"script": script, "desc": desc, "level": level}
            for name, (script, desc, level) in TOOLS.items()
        })
    
    @app.route('/health')
    def health():
        return jsonify({"status": "ok", "version": "1.0.0", "tools": len(TOOLS)})
    
    return app


DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex,nofollow">
<title>ZenfusionSEO Dashboard</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #fff; color: #1a1a1a; font-size: 16px; }
.container { max-width: 1100px; margin: 0 auto; padding: 40px 20px; }
header { text-align: center; margin-bottom: 40px; }
header h1 { font-size: 32px; margin-bottom: 8px; }
header .subtitle { font-size: 18px; color: #555; }
header .stats { font-size: 14px; color: #666; margin-top: 8px; }
.category { margin-bottom: 32px; }
.category-title { font-size: 22px; font-weight: 700; color: #0066cc; border-bottom: 2px solid #e0e0e0; padding-bottom: 10px; margin-bottom: 16px; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 14px; }
.card { background: #f8f9fa; border: 1px solid #e0e0e0; border-radius: 10px; padding: 16px; transition: border-color 0.2s; }
.card:hover { border-color: #0066cc; }
.card h3 { font-size: 16px; color: #0066cc; margin-bottom: 6px; }
.card p { font-size: 14px; color: #444; }
.card .run-btn { display: inline-block; margin-top: 10px; padding: 6px 16px; background: #0066cc; color: white; border: none; border-radius: 6px; font-size: 14px; cursor: pointer; text-decoration: none; }
.card .run-btn:hover { background: #0052a3; }
footer { margin-top: 40px; padding-top: 20px; border-top: 1px solid #e0e0e0; font-size: 14px; color: #666; text-align: center; }
</style>
</head>
<body>
<div class="container">
  <header>
    <h1>ZenfusionSEO</h1>
    <div class="subtitle">Open Source SEO/AEO/CRO Toolkit for Shopify</div>
    <div class="stats">{{ total_tools }} tools · DataForSEO + GSC + PSI + Perplexity + InfraNodus</div>
  </header>
  {% for level, tools in levels.items() %}
  <div class="category">
    <div class="category-title">{{ level }} Level</div>
    <div class="grid">
      {% for tool in tools %}
      <div class="card">
        <h3>{{ tool.name }}</h3>
        <p>{{ tool.desc }}</p>
        <a class="run-btn" href="#" onclick="runTool('{{ tool.name }}'); return false;">Run</a>
      </div>
      {% endfor %}
    </div>
  </div>
  {% endfor %}
  <footer>
    ZenfusionSEO v1.0.0 · MIT License · <a href="https://github.com/loftenter/zenfusion-seo">GitHub</a>
  </footer>
</div>
<script>
function runTool(name) {
  const params = prompt('Enter parameters as JSON (e.g., {"domain": "softprowatersystems.com"}):');
  if (!params) return;
  try {
    const body = JSON.parse(params);
    fetch('/api/run/' + name, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body) })
      .then(r => r.json())
      .then(data => {
        if (data.success) alert('Done! Output at: ' + data.output_dir);
        else alert('Error: ' + (data.error || 'Unknown'));
      })
      .catch(e => alert('Request failed: ' + e));
  } catch(e) { alert('Invalid JSON'); }
}
</script>
</body>
</html>
"""