from pathlib import Path

path = Path('/freqtrade/front_proxy.py')
s = path.read_text(encoding='utf-8')

if 'import ops_dashboard\n' not in s:
    marker = 'import trade_state\n'
    if marker not in s:
        raise SystemExit('ops-proxy: trade_state import marker missing')
    s = s.replace(marker, marker + 'import ops_dashboard\n', 1)

if 'OPS_DASHBOARD_TOKEN=' not in s:
    marker = "TELEGRAM_BOT_TOKEN=(os.getenv('TELEGRAM_BOT_TOKEN') or '').strip()\n"
    if marker not in s:
        raise SystemExit('ops-proxy: token marker missing')
    s = s.replace(marker, marker + "OPS_DASHBOARD_TOKEN=(os.getenv('OPS_DASHBOARD_TOKEN') or '').strip()\n", 1)

helper_marker = '\n\ndef _json_bytes(payload):\n'
helpers = r'''

def _ops_authorized(handler) -> bool:
    if not OPS_DASHBOARD_TOKEN:
        return False
    auth = str(handler.headers.get('Authorization') or '')
    if auth.startswith('Bearer ') and hmac.compare_digest(auth[7:].strip(), OPS_DASHBOARD_TOKEN):
        return True
    try:
        from urllib.parse import urlsplit, parse_qs
        q = parse_qs(urlsplit(handler.path).query)
        supplied = str((q.get('token') or [''])[0])
        return bool(supplied and hmac.compare_digest(supplied, OPS_DASHBOARD_TOKEN))
    except Exception:
        return False

'''
if 'def _ops_authorized' not in s:
    if helper_marker not in s:
        raise SystemExit('ops-proxy: helper marker missing')
    s = s.replace(helper_marker, helpers + helper_marker, 1)

proxy_marker = "    def proxy(self):\n        if self.path=='/health' or self.path.startswith('/health?'):\n"
proxy_new = """    def proxy(self):
        if self.path.startswith('/ops.json'):
            if not _ops_authorized(self):
                return self.send_json(401, {'ok':False,'status':'OPS_AUTH_REQUIRED'})
            return self.send_json(200, ops_dashboard.snapshot())
        if self.path=='/ops' or self.path.startswith('/ops?'):
            if not _ops_authorized(self):
                return self.send_json(401, {'ok':False,'status':'OPS_AUTH_REQUIRED'})
            data=ops_dashboard.render_html().encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type','text/html; charset=utf-8')
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Frame-Options','DENY')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Referrer-Policy','no-referrer')
            self.send_header('Content-Length',str(len(data)))
            self.send_header('Connection','close')
            self.end_headers(); self.wfile.write(data); return
        if self.path=='/health' or self.path.startswith('/health?'):
"""
if "self.path.startswith('/ops.json')" not in s:
    if proxy_marker not in s:
        raise SystemExit('ops-proxy: proxy marker missing')
    s = s.replace(proxy_marker, proxy_new, 1)

# Health exposes only whether the dashboard is configured, never its token.
health_old = "'incompleteTrackedPositions':snap.get('incomplete_count',0)})\n"
health_new = "'incompleteTrackedPositions':snap.get('incomplete_count',0),'opsDashboardConfigured':bool(OPS_DASHBOARD_TOKEN)})\n"
if "'opsDashboardConfigured'" not in s:
    if health_old not in s:
        raise SystemExit('ops-proxy: health marker missing')
    s = s.replace(health_old, health_new, 1)

compile(s, str(path), 'exec')
path.write_text(s, encoding='utf-8')
print('[ops-dashboard-proxy-patch] OK authenticated /ops + /ops.json enabled with no-store security headers')
