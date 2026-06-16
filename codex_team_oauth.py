#!/usr/bin/env python3
"""脱敏版 Codex 注册示例脚本。

请在分享或使用前替换以下配置：
- TEST_EMAIL / TEST_PASSWORD
- TEST_INBOX_API

依赖：
- curl_cffi
- deps/ 目录下包含 sentinel.py, sentinel_quickjs.py, openai_sentinel_quickjs.js
"""

import os
import sys
import time
import json
import uuid
import random
import re
import secrets
import base64
import hashlib
from urllib.parse import urlencode, urlparse, urljoin, parse_qs

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(SCRIPT_DIR, "deps"))

from curl_cffi import requests as curl_requests
from sentinel import get_sentinel_token


def load_dotenv(dotenv_path: str = None) -> None:
    """Load key=value pairs from a .env file into os.environ if not already set."""
    dotenv_path = dotenv_path or os.path.join(SCRIPT_DIR, ".env")
    if not os.path.exists(dotenv_path):
        return
    with open(dotenv_path, encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value

load_dotenv()

s = curl_requests.Session(impersonate='chrome136')
s.trust_env = False
s.proxies = {'https': '', 'http': ''}
UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/136.0.0.0 Safari/537.36'

def h(r='https://chatgpt.com/'):
    return {'Accept': 'application/json', 'Referer': r, 'Origin': 'https://chatgpt.com', 'User-Agent': UA}

def b64url(b):
    return base64.urlsafe_b64encode(b).decode().rstrip('=')

EMAIL = os.getenv('TEST_EMAIL', 'your-test-email@example.com')
PASSWORD = os.getenv('TEST_PASSWORD', 'your-password')
INBOX_API = os.getenv('TEST_INBOX_API', 'http://your-mailbox-service.example/api/v1')

CID = 'app_EMoamEEZ73f0CkXaXp7hrann'
CRI = 'http://localhost:1455/auth/callback'
STATE = b64url(secrets.token_bytes(24))

print('Email: ' + EMAIL + '\n')

s.get('https://chatgpt.com/', headers={'Accept': 'text/html,*/*'}, timeout=15)

verifier = b64url(secrets.token_bytes(64))
challenge = b64url(hashlib.sha256(verifier.encode()).digest())
params = {
    'client_id': CID,
    'response_type': 'code',
    'redirect_uri': CRI,
    'scope': 'openid email profile offline_access',
    'state': STATE,
    'code_challenge': challenge,
    'code_challenge_method': 'S256',
    'id_token_add_organizations': 'true',
    'codex_cli_simplified_flow': 'true',
}
ORIG_AUTH_URL = 'https://auth.openai.com/oauth/authorize?' + urlencode(params)

print('[1] Codex authorize (with extra params)...')
r = s.get(ORIG_AUTH_URL, headers={'Accept': 'text/html,*/*', 'User-Agent': UA, 'Referer': 'https://chatgpt.com/'}, timeout=30, allow_redirects=True)
print('  status: ' + str(r.status_code) + '  url: ' + r.url[:80])

did = ''
for c in s.cookies:
    if hasattr(c, 'name') and c.name == 'oai-did':
        if 'openai.com' in (getattr(c, 'domain', '') or ''):
            did = c.value
            break
if not did:
    try:
        for c in s.cookies.jar:
            if getattr(c, 'name', '') == 'oai-did':
                did = getattr(c, 'value', '')
                break
    except:  # noqa: E722
        pass
if not did:
    did = str(uuid.uuid4())
print('  device_id: ' + did)

print('\n[2] authorize/continue signup...')
snt = get_sentinel_token(s, device_id=did, flow='authorize_continue')
h2 = {**h('https://auth.openai.com/create-account'), 'Content-Type': 'application/json', 'openai-sentinel-token': snt}
r = s.post('https://auth.openai.com/api/accounts/authorize/continue', headers=h2,
           json={'username': {'value': EMAIL, 'kind': 'email'}, 'screen_hint': 'signup'}, timeout=30)
if r.status_code != 200:
    print('  FAIL: ' + str(r.status_code) + ' ' + r.text[:300])
    sys.exit(1)
d = r.json()
pt = (d.get('page', {}) if isinstance(d.get('page'), dict) else {}).get('type', '')
cu = str(d.get('continue_url', '') or '')
print('  page_type: ' + pt)
is_new = (pt == 'create_account_password' or '/create-account/password' in cu)

if is_new:
    print('[3] Register password + send OTP...')
    s.get('https://auth.openai.com/create-account/password', headers=h('https://auth.openai.com/create-account'), timeout=15)
    snt2 = get_sentinel_token(s, device_id=did, flow='username_password_create')
    h3 = {**h('https://auth.openai.com/create-account/password'), 'Content-Type': 'application/json', 'openai-sentinel-token': snt2}
    r = s.post('https://auth.openai.com/api/accounts/user/register', headers=h3,
               json={'password': PASSWORD, 'username': EMAIL}, timeout=30)
    print('  user/register: ' + str(r.status_code))
    if r.status_code == 200:
        s.get('https://auth.openai.com/api/accounts/email-otp/send', headers=h('https://auth.openai.com/create-account/password'), timeout=30)
        print('  send_otp sent')
else:
    print('[3] OTP path...')
    s.post('https://auth.openai.com/api/accounts/email-otp/resend',
           headers={**h('https://auth.openai.com/email-verification'), 'Content-Type': 'application/json'}, timeout=30)

print('[4] Wait OTP...')
otp = ''
mb = EMAIL.split('@')[0]
for i in range(30):
    time.sleep(2)
    r2 = s.get(f'{INBOX_API}/mailbox/' + mb, timeout=10)
    if r2.status_code == 200:
        ms = r2.json()
        if ms:
            mid = ms[-1].get('id', '')
            if mid:
                r3 = s.get(f'{INBOX_API}/mailbox/' + mb + '/' + mid, timeout=10)
                if r3.status_code == 200:
                    body = (r3.json().get('body', {}) or {}).get('text', '') or ''
                    m = re.search(r'(\d{6})', body)
                    if m:
                        otp = m.group(1)
                        print('  OTP: ' + otp)
                        break
    if i % 5 == 0:
        print('  wait... (' + str(i+1) + '/30)')
if not otp:
    print('  NO OTP!')
    sys.exit(1)

print('[5] Verify OTP...')
r = s.post('https://auth.openai.com/api/accounts/email-otp/validate',
           headers={**h('https://auth.openai.com/email-verification'), 'Content-Type': 'application/json'},
           json={'code': otp}, timeout=30)
if r.status_code == 200:
    cu = r.json().get('continue_url', '') or ''
    print('  continue_url: ' + cu[:120])

print('[6] create_account...')
snt3 = get_sentinel_token(s, device_id=did, flow='create_account')
h4 = {**h('https://auth.openai.com/about-you'), 'Content-Type': 'application/json', 'openai-sentinel-token': snt3}
name = random.choice(['James', 'John', 'Robert', 'Michael']) + ' ' + random.choice(['Smith', 'Johnson', 'Williams'])
bd = str(random.randint(1985, 2000)) + '-' + str(random.randint(1, 12)).zfill(2) + '-' + str(random.randint(1, 28)).zfill(2)
r = s.post('https://auth.openai.com/api/accounts/create_account', headers=h4,
           json={'name': name, 'birthdate': bd}, timeout=30)
print('  create_account: ' + str(r.status_code))
if r.status_code != 200:
    print('  ' + r.text[:500])
    sys.exit(1)
d2 = r.json()
cu = d2.get('continue_url', '') or ''
print('  continue_url: ' + cu[:120])
wid = ''
cas = d2.get('oai-client-auth-session', {})
if isinstance(cas, dict):
    ws = cas.get('workspaces', [])
    if ws:
        wid = ws[0].get('id', '')
print('  workspace_id: ' + (wid or 'NONE'))

print('\n[7] Follow /sign-in-with-chatgpt/codex/consent -> workspace select -> callback...')
current = cu
callback = None
for hop in range(15):
    r = s.get(current, headers={'Accept': 'text/html,*/*', 'User-Agent': UA, 'Referer': 'https://chatgpt.com/'}, timeout=30, allow_redirects=False)
    path = urlparse(current).path
    print('  Hop ' + str(hop+1) + ': ' + str(r.status_code) + ' ' + path[:70])

    if 'code=' in current:
        qs = parse_qs(urlparse(current).query)
        if qs.get('code', [''])[0]:
            callback = current
            break

    if r.status_code in (301, 302, 303, 307, 308):
        loc = r.headers.get('Location', '')
        if not loc:
            break
        if loc.startswith('/'):
            loc = urljoin(current, loc)
        if 'code=' in loc:
            callback = loc
            break
        current = loc
    elif r.status_code == 200:
        html = r.text or ''

        if '/consent' in path and wid:
            print('    -> workspace/select...')
            r2 = s.post('https://auth.openai.com/api/accounts/workspace/select',
                       headers={**h('https://auth.openai.com/sign-in-with-chatgpt/codex/consent'), 'Content-Type': 'application/json'},
                       json={'workspace_id': wid}, timeout=30)
            print('    workspace/select: ' + str(r2.status_code))
            nxt = ''
            if r2.status_code == 200:
                nxt = r2.json().get('continue_url', '') or ''
            if nxt:
                if nxt.startswith('/'):
                    nxt = urljoin('https://auth.openai.com', nxt)
                current = nxt
                print('    next: ' + nxt[:100])
                continue

        if '/codex/organization' in path or '/organization' in path:
            print('    -> organization page')
            if wid:
                print('    -> workspace/select (org)...')
                r2 = s.post('https://auth.openai.com/api/accounts/workspace/select',
                           headers={**h('https://auth.openai.com/sign-in-with-chatgpt/codex/organization'), 'Content-Type': 'application/json'},
                           json={'workspace_id': wid}, timeout=30)
                print('    workspace/select: ' + str(r2.status_code))
                if r2.status_code == 200:
                    nxt = r2.json().get('continue_url', '') or ''
                    print('    next: ' + nxt[:100])
                    if nxt and '/organization' not in nxt:
                        if nxt.startswith('/'):
                            nxt = urljoin('https://auth.openai.com', nxt)
                        current = nxt
                        continue

            print('    -> re-authorizing with same URL...')
            r = s.get(ORIG_AUTH_URL, headers={'Accept': 'text/html,*/*', 'User-Agent': UA, 'Referer': 'https://chatgpt.com/'}, timeout=30, allow_redirects=True)
            print('    re-auth: ' + str(r.status_code) + ' ' + r.url[:100])
            if 'code=' in r.url:
                callback = r.url
                break
            current = r.url
            continue

        if '/log-in' in path:
            print('    -> logged out, re-authorizing...')
            r = s.get(ORIG_AUTH_URL, headers={'Accept': 'text/html,*/*', 'User-Agent': UA, 'Referer': 'https://chatgpt.com/'}, timeout=30, allow_redirects=True)
            print('    re-auth: ' + str(r.status_code) + ' ' + r.url[:100])
            if 'code=' in r.url:
                callback = r.url
                break
            current = r.url
            continue

        if '/choose-an-account' in path:
            m = re.search(r"us_[A-Za-z0-9]{16,}", html or "")
            if m:
                sid = m.group(0)
                print('    choose-account: ' + sid)
                r2 = s.post('https://auth.openai.com/api/accounts/session/select',
                           headers={**h('https://auth.openai.com/choose-an-account'), 'Content-Type': 'application/json', 'Origin': 'https://auth.openai.com'},
                           json={'session_id': sid}, timeout=30)
                print('    session/select: ' + str(r2.status_code))
                if r2.status_code == 200:
                    current = ORIG_AUTH_URL
                    continue

        print('    -> unhandled 200 page, breaking')
        break
    else:
        print('    -> unhandled status, breaking')
        break

if callback:
    qs = parse_qs(urlparse(callback).query)
    code = qs.get('code', [''])[0]
    print('\n[8] Callback! Code: ' + code[:30] + '...')
    r2 = s.post('https://auth.openai.com/oauth/token',
               headers={'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json',
                        'Origin': 'https://auth.openai.com', 'User-Agent': UA},
               data=urlencode({'grant_type': 'authorization_code', 'client_id': CID, 'code': code,
                              'redirect_uri': CRI, 'code_verifier': verifier}), timeout=30)
    print('  Token exchange: ' + str(r2.status_code))
    if r2.status_code == 200:
        t = r2.json()
        print('  access_token:  ' + str(len(t.get('access_token', ''))) + ' chars')
        print('  refresh_token: ' + str(len(t.get('refresh_token', ''))) + ' chars')
        print('  id_token:      ' + str(len(t.get('id_token', ''))) + ' chars')
        fname = '/tmp/codex_token_' + EMAIL.split('@')[0] + '.json'
        with open(fname, 'w') as f:
            json.dump({'email': EMAIL, **t}, f, indent=2)
        print('Saved: ' + fname)
    else:
        print('  FAIL: ' + r2.text[:500])
else:
    print('\nNo callback. Final: ' + current[:150])

print('\n=== Session cookies ===')
for c in s.cookies:
    if hasattr(c, 'name') and c.name in ('authsess_*', 'oai-did', '__cf_bm', 'session-id', 'bugout_session_id', '__cflb'):
        print('  ' + str(getattr(c, 'domain', '')) + ' ' + c.name + '=' + c.value[:20])
print('\n=== DONE ===')
