"""Live API smoke test. Defaults to localhost. Pass --base-url for a deployment."""
import argparse
import os
import re
import time
import httpx
from dotenv import load_dotenv

if __name__ == '__main__':
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', default='http://127.0.0.1:8000')
    parser.add_argument('--expect-mode', choices=['demo', 'llm'],
                        help='Reject a different server mode before sending chat')
    parser.add_argument('--wait-seconds', type=float, default=0,
                        help='Wait for HTTP startup (CI starts a real local server)')
    args = parser.parse_args()
    if not 0 <= args.wait_seconds <= 120:
        parser.error('--wait-seconds must be between 0 and 120')
    token = os.getenv('APP_ACCESS_TOKEN', '')
    headers = {'Authorization': 'Bearer ' + token} if token else {}
    with httpx.Client(base_url=args.base_url, headers=headers, timeout=420, trust_env=False) as c:
        deadline = time.monotonic() + args.wait_seconds
        while True:
            try:
                response = c.get('/api/health', timeout=2)
                response.raise_for_status()
                break
            except httpx.HTTPError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(min(1, max(0, deadline - time.monotonic())))
        health = response.json()
        assert health['status'] == 'ok' and health['database_exists']
        if args.expect_mode:
            assert health['mode'] == args.expect_mode, 'Unexpected mode; chat was not sent'
        print('OK /api/health', 'mode:', health['mode'])
        if health['auth_required']:
            denied = c.get('/api/ready', headers={'Authorization': ''})
            assert denied.status_code == 401, 'Protected API accepted no token'
            print('OK missing token rejected')
        responses = {}
        for route in ['/api/ready', '/api/context', '/api/dashboard']:
            r = c.get(route)
            r.raise_for_status()
            responses[route] = r.json()
            print('OK', route)
        assert responses['/api/ready']['mcp'] == 'ok'
        context = responses['/api/context']
        assert context['dataset_id'] == 'builtin' and context['latest_date'] in context['dates']
        assert responses['/api/dashboard']['summary']['as_of'] == context['latest_date']
        response = c.post('/api/chat', json={'question': 'Дай обзор качества портфеля на последнем срезе, с источниками.', 'scenario': 'overview'})
        response.raise_for_status()
        result = response.json()
        assert result['mode'] == health['mode']
        assert result['protocol']['transport'] == 'stdio'
        analytic = {x['ref'] for x in result['trace']
                    if x['ok'] and x['tool'] == 'get_portfolio_summary'}
        assert analytic, 'No successful analytical MCP call'
        assert set(re.findall(r'\[(T\d+)\]', result['answer'])) & analytic
        if health['mode'] == 'demo':
            assert result['usage']['llm_requests'] == 0
            assert result['provenance']['planner'] == 'demo'
        print('OK /api/chat', 'mode:', result['mode'], 'tools:', len(result['trace']))
        print(result['answer'])
