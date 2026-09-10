#!/usr/bin/env python3
"""
Zenfusion InfraNodus Helper

Uses InfraNodus API to extract entity graphs, find content gaps, 
and compare entity coverage between pages.

Usage:
  python3 infranodus_helper.py analyze <text-or-file>
  python3 infranodus_helper.py compare <your_text> <competitor_text>
  python3 infranodus_helper.py ai-overview <keyword>
"""

import sys, os, json, urllib.request

def get_api_key():
    env_path = os.path.expanduser('~/.hermes/.env')
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line.startswith('INFRANODUS_API_KEY='):
                return line.split('=', 1)[1]
    return None

def analyze_text(text, api_key=None):
    """Send text to InfraNodus and return entity graph analysis."""
    if not api_key:
        api_key = get_api_key()
    if not api_key:
        return {'error': 'No INFRANODUS_API_KEY found in .env'}
    
    url = "https://infranodus.com/api/v1/graphAndStatements"
    payload = json.dumps({"text": text}).encode()
    req = urllib.request.Request(url, data=payload, method='POST', headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    })
    
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        
        graph_data = data.get('entriesAndGraphOfContext', {}).get('graph', {}).get('graphologyGraph', {})
        attrs = graph_data.get('attributes', {})
        
        top_nodes = attrs.get('top_influential_nodes', [])
        all_clusters = attrs.get('allClusters', [])
        gaps = attrs.get('gaps', [])
        diversity = attrs.get('diversity_stats', {})
        
        result = {
            'entities': [{'node': n['node'], 'degree': n.get('degree',0), 'centrality': n.get('bc',0)} for n in top_nodes[:30]],
            'entity_count': len(graph_data.get('nodes', [])),
            'edge_count': len(graph_data.get('edges', [])),
            'clusters': [{'id': c.get('id'), 'name': c.get('aiName'), 'relationships': c.get('dotGraph', [])[:5]} for c in all_clusters[:10]],
            'cluster_count': len(all_clusters),
            'gaps': [{'from': g.get('from',{}).get('community'), 'to': g.get('to',{}).get('community'),
                       'from_nodes': [n['nodeName'] for n in g.get('from',{}).get('nodes',[])[:5]],
                       'to_nodes': [n['nodeName'] for n in g.get('to',{}).get('nodes',[])[:5]]} for g in gaps[:10]],
            'diversity': diversity,
        }
        return result
    except Exception as e:
        return {'error': str(e)}

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python3 infranodus_helper.py <text>")
        sys.exit(1)
    
    text = sys.argv[1]
    # Check if it's a file path
    if os.path.exists(text):
        with open(text) as f:
            text = f.read()
    
    result = analyze_text(text)
    print(json.dumps(result, indent=2))
