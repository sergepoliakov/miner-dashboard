#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 sergepoliakov
"""Authenticated, full-function gateway for this rig's existing local dashboard."""
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import threading
import time
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from contextlib import contextmanager
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, ProxyHandler, build_opener

from runtime_paths import CONFIG_DIR, STATE_DIR
HOST = os.environ.get('MINER_DASHBOARD_HOST', 'localhost')
ORIGIN = 'https://' + HOST
UPSTREAM_URL = os.environ.get('MINER_DASHBOARD_UPSTREAM', 'http://127.0.0.1:8091')
AUTH_FILE = Path(os.environ.get('MINING_WEB_AUTH_FILE', str(CONFIG_DIR / 'web-auth.json')))
SESSION_FILE = Path(os.environ.get('MINING_WEB_SESSION_FILE', str(STATE_DIR / 'sessions.sqlite3')))
COOKIE = '__Host-miner_session'
TTL = 14 * 86400
READ = {'/', '/index.html', '/api/status', '/api/rig', '/api/history', '/api/rgb', '/api/mining-profiles',
        '/api/thermal-log', '/api/alert-log', '/gpu_fans_strip.jpg', '/gpu_fans_strip.mp4'}
WRITE = {'/api/exchange/order/preview', '/api/exchange/order/submit', '/api/mining/switch',
         '/api/profit-threshold', '/api/tune', '/api/noid/threads', '/api/manual-price',
         '/api/rig/assign', '/api/rig/gpu', '/api/rig/check-updates', '/api/rig/upgrade',
         '/api/rig/notices/check', '/api/rig/notices/read', '/api/rgb',
         '/api/mining-profiles/save', '/api/mining-profiles/apply'}
OPENER = build_opener(ProxyHandler({}))
RATE_LOCK = threading.Lock()
ATTEMPTS = {}
LOGIN = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Miner Dashboard · 矿机管理</title>
<style>body{margin:0;background:#0b1018;color:#e9eef8;font:16px system-ui;display:grid;
min-height:100vh;place-items:center}form{width:min(340px,80vw);background:#141d2a;padding:32px;
border:1px solid #2c3c53;border-radius:20px}h1{font-size:23px;margin-top:0}label{display:block;
margin-top:18px}input,button{box-sizing:border-box;width:100%;padding:13px;border-radius:9px;
border:1px solid #344960;background:#0d1520;color:inherit;font:inherit;margin-top:7px}
button{margin-top:24px;background:#2869df;border:0;cursor:pointer}p{font-size:14px;color:#a8b8cf}
#message{color:#ffb990;min-height:22px}</style><form id="login"><h1>Miner Dashboard · 矿机管理</h1>
<p>登录后可使用完整大屏、挖矿控制和硬件调节。</p>
<label>账户<input id="username" name="username" value="admin" autocomplete="username" required></label>
<label>密码<input id="password" name="password" type="password" autocomplete="current-password" required></label>
<button type="submit">登录</button><p id="message" role="alert"></p></form><script>
document.querySelector('#login').onsubmit=async e=>{e.preventDefault();const b=e.target.querySelector('button');
b.disabled=true;try{const r=await fetch('/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({username:document.querySelector('#username').value,password:document.querySelector('#password').value})});
if(r.ok){location.replace('/')}else{document.querySelector('#message').textContent=r.status===429?'尝试过于频繁，请稍后再试。':'账户或密码不正确。'}}
catch{document.querySelector('#message').textContent='连接暂时中断，请重试。'}finally{b.disabled=false}};</script></html>'''.encode()


def settings():
    return json.loads(AUTH_FILE.read_text())


@contextmanager
def session_db():
    db = sqlite3.connect(SESSION_FILE, timeout=3)
    try:
        with db:
            db.execute('CREATE TABLE IF NOT EXISTS sessions (digest TEXT PRIMARY KEY, expiry INTEGER NOT NULL)')
            yield db
    finally:
        db.close()


def session_value(header):
    cookies = SimpleCookie(); cookies.load(header or '')
    return cookies[COOKIE].value


def valid_session(header, config, now=None):
    try:
        raw = session_value(header)
        payload, signature = raw.rsplit('.', 1)
        expected = hmac.new(bytes.fromhex(config['session_key']), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected): return False
        user, expiry, nonce = base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)).decode().split('|')
        now = int(time.time() if now is None else now)
        if not (user == config['username'] and now < int(expiry) <= now + TTL and len(nonce) == 32): return False
        with session_db() as db:
            row = db.execute('SELECT expiry FROM sessions WHERE digest=?', (hashlib.sha256(raw.encode()).hexdigest(),)).fetchone()
        return row is not None and row[0] == int(expiry) and row[0] > now
    except (ValueError, KeyError, CookieError, UnicodeError, sqlite3.Error, OSError):
        return False


def issue_session(config):
    now = int(time.time()); expiry = now + TTL
    raw = f"{config['username']}|{expiry}|{secrets.token_hex(16)}".encode()
    payload = base64.urlsafe_b64encode(raw).decode().rstrip('=')
    sig = hmac.new(bytes.fromhex(config['session_key']), payload.encode(), hashlib.sha256).hexdigest()
    value = payload + '.' + sig
    with session_db() as db:
        db.execute('DELETE FROM sessions WHERE expiry<=?', (now,))
        db.execute('INSERT INTO sessions VALUES (?,?)', (hashlib.sha256(value.encode()).hexdigest(), expiry))
        db.execute('DELETE FROM sessions WHERE digest NOT IN (SELECT digest FROM sessions ORDER BY expiry DESC LIMIT 128)')
    return value


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def respond(self, status, body, mime='application/json; charset=utf-8', cookie=None, head=False):
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
        self.send_header('Strict-Transport-Security', 'max-age=31536000')
        if cookie: self.send_header('Set-Cookie', cookie)
        self.send_header('Connection', 'close')
        self.end_headers(); self.close_connection = True
        if not head:
            try: self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError): pass

    def error(self, code, label):
        self.respond(code, json.dumps({'error': label}).encode())

    def host_ok(self):
        return self.headers.get('Host', '').lower() == HOST

    def owner(self):
        return valid_session(self.headers.get('Cookie'), settings())

    def route(self, method):
        try:
            route = urlsplit(self.path)
            if route.scheme or route.netloc or not self.host_ok():
                return self.error(400, 'invalid_host_or_path')
            if method == 'GET' and route.path == '/healthz':
                return self.respond(200, b'{"status":"ok","mode":"authenticated_full"}')
            if method == 'POST':
                if self.headers.get('Origin') != ORIGIN:
                    return self.error(403, 'origin_rejected')
                size = self.headers.get('Content-Length', '')
                maximum = 32768 if route.path == '/api/rig/notices/read' else 4096
                if (self.headers.get('Transfer-Encoding') or not size.isdecimal() or
                        not 1 <= int(size) <= maximum or
                        self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json'):
                    return self.error(400, 'invalid_request_body')
                body = self.rfile.read(int(size))
                if route.path == '/auth/login':
                    return self.login(body)
            else: body = None
            if not self.owner():
                if method == 'GET' and route.path in ('/', '/index.html', '/auth/login'):
                    return self.respond(200, LOGIN, 'text/html; charset=utf-8', head=method == 'HEAD')
                return self.error(401, 'login_required')
            if method == 'POST' and route.path == '/auth/logout':
                raw = session_value(self.headers.get('Cookie'))
                with session_db() as db:
                    db.execute('DELETE FROM sessions WHERE digest=?', (hashlib.sha256(raw.encode()).hexdigest(),))
                return self.respond(200, b'{"ok":true}', cookie=COOKIE + '=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Strict')
            if route.path not in (WRITE if method == 'POST' else READ):
                return self.error(404, 'not_found')
            self.forward(method, body)
        except (OSError, ValueError, KeyError, URLError, TimeoutError, sqlite3.Error, subprocess.SubprocessError):
            self.error(503, 'monitor_temporarily_unavailable')

    def login(self, body):
        ip = self.headers.get('CF-Connecting-IP', self.client_address[0])[:64]
        now = time.monotonic()
        with RATE_LOCK:
            for key in list(ATTEMPTS):
                if ATTEMPTS[key][0] < now - 120: del ATTEMPTS[key]
            start, count = ATTEMPTS.get(ip, (now, 0))
            if count >= 8 or len(ATTEMPTS) >= 1024:
                return self.error(429, 'try_later')
            ATTEMPTS[ip] = (start, count + 1)
        try:
            data = json.loads(body); config = settings()
            username, password = data.get('username'), data.get('password')
            if not isinstance(username, str) or not isinstance(password, str) or len(password) > 256:
                raise ValueError('invalid credentials')
            digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(config['salt']), config['iterations']).hex()
            good = hmac.compare_digest(digest, config['password_hash']) and hmac.compare_digest(username, config['username'])
        except (ValueError, TypeError, AttributeError): good = False
        if not good: return self.error(401, 'invalid_credentials')
        with RATE_LOCK: ATTEMPTS.pop(ip, None)
        self.respond(200, b'{"ok":true}', cookie=f'{COOKIE}={issue_session(config)}; Path=/; Max-Age={TTL}; Secure; HttpOnly; SameSite=Strict')

    def forward(self, method, body):
        headers = {'Host': '127.0.0.1:8091', 'Accept': self.headers.get('Accept', '*/*')}
        if method == 'POST':
            headers.update({'Origin': UPSTREAM_URL, 'Content-Type': 'application/json',
                            'X-CSRF-Token': self.headers.get('X-CSRF-Token', '')})
        request = Request(UPSTREAM_URL + self.path, data=body, headers=headers, method=method)
        try: response = OPENER.open(request, timeout=90 if self.path == '/api/mining-profiles/apply' else 25)
        except HTTPError as error: response = error
        with response:
            payload = response.read(32 * 1024 * 1024 + 1)
            if len(payload) > 32 * 1024 * 1024:
                return self.error(502, 'upstream_response_too_large')
            mime = response.headers.get('Content-Type', 'application/octet-stream')
            if response.code == 200 and urlsplit(self.path).path in ('/', '/index.html'):
                extra = b'''<button id="remote-logout" style="position:fixed;bottom:12px;right:16px;z-index:9999;background:#172436;color:#cbd8ea;border:1px solid #34465d;border-radius:8px;padding:7px 12px;cursor:pointer">\xe9\x80\x80\xe5\x87\xba\xe7\xae\xa1\xe7\x90\x86</button><script>document.querySelector('#remote-logout').onclick=async()=>{const r=await fetch('/auth/logout',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});if(r.ok)location.reload();};</script>'''
                payload = payload.replace(b'</body>', extra + b'</body>', 1)
            self.respond(response.code, payload, mime)

    def do_GET(self): self.route('GET')
    def do_POST(self): self.route('POST')
    def reject(self): self.error(405, 'method_not_allowed')
    do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_TRACE = do_HEAD = reject
    def log_message(self, *_): pass


if __name__ == '__main__':
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    config = settings()  # Fail closed if credentials are unavailable.
    if not config.get('password_hash') or len(config.get('session_key', '')) != 64:
        raise RuntimeError('Invalid web authentication configuration')
    ThreadingHTTPServer(('127.0.0.1', 8092), Handler).serve_forever()
