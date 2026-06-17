#!/usr/bin/env python3
"""脱敏版 Codex 注册示例脚本。

请在分享或使用前替换以下配置：
- TEST_EMAIL / TEST_PASSWORD
- TEST_INBOX_API（Inbucket 或 cloudflare_temp_email 基址）
- TEST_MAIL_JWT / TEST_CF_ADMIN_PASSWORD / TEST_EMAIL_DOMAIN（CF 邮箱）

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


def resolve_impersonate() -> str:
    """根据已安装的 curl_cffi 版本选择可用的浏览器指纹。"""
    preferred = os.getenv('CURL_IMPERSONATE', 'chrome136').strip() or 'chrome136'
    try:
        from curl_cffi.requests import BrowserType

        supported = {item.value for item in BrowserType}
        if preferred in supported:
            return preferred
        for fallback in ('chrome136', 'chrome124', 'chrome120', 'chrome116', 'chrome110'):
            if fallback in supported:
                if fallback != preferred:
                    print(f'  curl_cffi 不支持 {preferred}，回退到 {fallback}')
                return fallback
    except Exception:
        pass
    return preferred


def resolve_user_agent(impersonate: str) -> str:
    """生成与 impersonate 版本大致匹配的 User-Agent。"""
    version = '136'
    match = re.search(r'chrome(\d+)', impersonate)
    if match:
        version = match.group(1)
    return (
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
        f'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version}.0.0.0 Safari/537.36'
    )


IMPERSONATE = resolve_impersonate()
UA = resolve_user_agent(IMPERSONATE)
s = curl_requests.Session(impersonate=IMPERSONATE)
s.trust_env = False

def resolve_proxies() -> dict:
    """从环境变量读取 HTTP/HTTPS 代理；留空则直连。"""
    proxy = (
        os.getenv('HTTPS_PROXY', '').strip()
        or os.getenv('HTTP_PROXY', '').strip()
        or os.getenv('ALL_PROXY', '').strip()
    )
    if proxy:
        print(f'  使用代理: {proxy}')
        return {'http': proxy, 'https': proxy}
    return {'https': '', 'http': ''}

s.proxies = resolve_proxies()

def h(r='https://chatgpt.com/'):
    return {'Accept': 'application/json', 'Referer': r, 'Origin': 'https://chatgpt.com', 'User-Agent': UA}

def b64url(b):
    return base64.urlsafe_b64encode(b).decode().rstrip('=')


def normalize_inbox_base_url(url: str) -> str:
    """将收件 API 地址规范为服务根 URL（去掉末尾 /api/v1 等路径）。"""
    base = (url or '').strip().rstrip('/')
    for suffix in ('/api/v1', '/api'):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return base.rstrip('/')


def is_plausible_otp(code: str) -> bool:
    """过滤像年份拼接的 6 位误匹配（如 202123）。"""
    if len(code) != 6 or not code.isdigit():
        return False
    year_prefix = int(code[:4])
    if 1990 <= year_prefix <= 2035:
        return False
    return True


def extract_otp_from_content(*parts: str) -> str:
    """从邮件正文或 HTML 片段中提取 6 位数字 OTP。"""
    patterns = [
        r'(?:verification|verify|security)\s*code[^\d]{0,40}(\d{6})',
        r'(?:code|OTP|otp)[:\s]+(\d{6})',
        r'(\d{6})\s+is your(?: ChatGPT)?(?: verification)? code',
        r'>\s*(\d{6})\s*<',
        r'(?<!\d)(\d{6})(?!\d)',
    ]
    for content in parts:
        if not content:
            continue
        for pattern in patterns:
            for match in re.finditer(pattern, content, re.IGNORECASE):
                code = match.group(1)
                if is_plausible_otp(code):
                    return code
    return ''


def extract_otp_from_mail(mail: dict) -> str:
    """从 cloudflare_temp_email 解析后的邮件对象中提取 OTP。"""
    return extract_otp_from_content(
        mail.get('text', '') or '',
        mail.get('html', '') or '',
        mail.get('subject', '') or '',
    )


def is_openai_mail(mail: dict) -> bool:
    """判断邮件是否可能来自 OpenAI 验证码。"""
    sender = str(mail.get('source') or mail.get('from') or '').lower()
    subject = str(mail.get('subject') or '').lower()
    if 'openai' in sender or 'chatgpt' in sender or 'oaistatic' in sender:
        return True
    return any(k in subject for k in ('verification', 'verify', 'code', 'chatgpt'))


def provision_cf_mailbox(
    session,
    base_url: str,
    admin_password: str,
    *,
    local_part: str = '',
    domain: str = '',
) -> tuple[str, str]:
    """通过 cloudflare_temp_email 管理员接口创建邮箱，返回 (address, jwt)。"""
    name = local_part or ('codex' + secrets.token_hex(4))
    mail_domain = domain or os.getenv('TEST_EMAIL_DOMAIN', '').strip()
    if not mail_domain:
        raise ValueError('缺少 TEST_EMAIL_DOMAIN，无法自动创建 CF 邮箱')

    api = normalize_inbox_base_url(base_url)
    payload = {'name': name, 'domain': mail_domain}
    response = session.post(
        f'{api}/admin/new_address',
        headers={
            'x-admin-auth': admin_password,
            'Content-Type': 'application/json',
        },
        json=payload,
        timeout=30,
    )
    if response.status_code != 200:
        raise RuntimeError(
            f'创建 CF 邮箱失败: HTTP {response.status_code} {response.text[:300]}'
        )
    data = response.json()
    address = str(data.get('address', '') or '').strip()
    jwt = str(data.get('jwt', '') or '').strip()
    if not address or not jwt:
        raise RuntimeError(f'创建 CF 邮箱响应异常: {data}')
    return address, jwt


def wait_otp_from_inbucket(session, inbox_api: str, email: str, attempts: int = 30) -> str:
    """轮询 Inbucket 风格 /api/v1/mailbox 接口，直到拿到 OTP。"""
    mailbox = email.split('@')[0]
    for i in range(attempts):
        time.sleep(2)
        list_resp = session.get(f'{inbox_api}/mailbox/{mailbox}', timeout=10)
        if list_resp.status_code != 200:
            if i % 5 == 0:
                print(f'  wait... ({i + 1}/{attempts})')
            continue
        messages = list_resp.json()
        if not messages:
            if i % 5 == 0:
                print(f'  wait... ({i + 1}/{attempts})')
            continue
        message_id = messages[-1].get('id', '')
        if not message_id:
            continue
        detail_resp = session.get(
            f'{inbox_api}/mailbox/{mailbox}/{message_id}',
            timeout=10,
        )
        if detail_resp.status_code != 200:
            continue
        body_text = (detail_resp.json().get('body', {}) or {}).get('text', '') or ''
        otp = extract_otp_from_content(body_text)
        if otp:
            return otp
        if i % 5 == 0:
            print(f'  wait... ({i + 1}/{attempts})')
    return ''


def wait_otp_from_cf_inbox(session, base_url: str, jwt: str, attempts: int = 30) -> str:
    """轮询 cloudflare_temp_email /api/parsed_mails 接口，直到拿到 OTP。"""
    api = normalize_inbox_base_url(base_url)
    headers = {'Authorization': f'Bearer {jwt}'}
    for i in range(attempts):
        time.sleep(2)
        response = session.get(
            f'{api}/api/parsed_mails',
            params={'limit': 10, 'offset': 0},
            headers=headers,
            timeout=10,
        )
        if response.status_code != 200:
            if i % 5 == 0:
                print(f'  wait... ({i + 1}/{attempts})')
            continue
        results = (response.json() or {}).get('results', []) or []
        for mail in reversed(results):
            if not is_openai_mail(mail):
                continue
            otp = extract_otp_from_mail(mail)
            if otp:
                return otp
        for mail in reversed(results):
            otp = extract_otp_from_mail(mail)
            if otp:
                return otp
        if i % 5 == 0:
            print(f'  wait... ({i + 1}/{attempts})')
    return ''


def resolve_mail_config(session) -> tuple[str, str, str]:
    """解析收件配置，必要时自动创建 CF 邮箱，返回 (mode, inbox_api, mail_jwt)。"""
    inbox_raw = os.getenv('TEST_INBOX_API', 'http://your-mailbox-service.example/api/v1')
    inbox_mode = os.getenv('TEST_INBOX_MODE', '').strip().lower()
    mail_jwt = os.getenv('TEST_MAIL_JWT', '').strip()
    admin_password = os.getenv('TEST_CF_ADMIN_PASSWORD', '').strip()
    email = os.getenv('TEST_EMAIL', '').strip()
    email_domain = os.getenv('TEST_EMAIL_DOMAIN', '').strip()

    if not inbox_mode:
        inbox_mode = 'cf' if mail_jwt or admin_password else 'inbucket'

    if inbox_mode == 'cf':
        base_url = normalize_inbox_base_url(inbox_raw)
        if not mail_jwt and admin_password:
            local_part = email.split('@')[0] if email and '@' in email else ''
            domain = email.split('@')[1] if email and '@' in email else email_domain
            print('[0] 自动创建 CF 临时邮箱...')
            address, mail_jwt = provision_cf_mailbox(
                session,
                base_url,
                admin_password,
                local_part=local_part,
                domain=domain,
            )
            os.environ['TEST_EMAIL'] = address
            os.environ['TEST_MAIL_JWT'] = mail_jwt
            print(f'  address: {address}')
        if not mail_jwt:
            raise RuntimeError(
                'CF 邮箱模式需要 TEST_MAIL_JWT，或提供 TEST_CF_ADMIN_PASSWORD 以自动创建邮箱'
            )
        return 'cf', base_url, mail_jwt

    api_v1 = inbox_raw.rstrip('/')
    if not api_v1.endswith('/api/v1'):
        api_v1 = normalize_inbox_base_url(inbox_raw) + '/api/v1'
    return 'inbucket', api_v1, ''


INBOX_MODE, INBOX_API, MAIL_JWT = resolve_mail_config(s)
EMAIL = os.getenv('TEST_EMAIL', 'your-test-email@example.com')
PASSWORD = os.getenv('TEST_PASSWORD', 'your-password')

# Codex CLI 官方 client_id；Team 跳手机靠的是注册邮箱域名，不是换 client_id
CID = os.getenv('CLIENT_ID', 'app_EMoamEEZ73f0CkXaXp7hrann')
CRI = os.getenv('CODEX_REDIRECT_URI', 'http://localhost:1455/auth/callback')
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
        s.post(
            'https://auth.openai.com/api/accounts/email-otp/send',
            headers={**h('https://auth.openai.com/create-account/password'), 'Content-Type': 'application/json'},
            json={},
            timeout=30,
        )
        print('  send_otp sent')
else:
    print('[3] OTP path...')
    s.post('https://auth.openai.com/api/accounts/email-otp/resend',
           headers={**h('https://auth.openai.com/email-verification'), 'Content-Type': 'application/json'}, timeout=30)

print('[4] Wait OTP...')
if INBOX_MODE == 'cf':
    print('  inbox: cloudflare_temp_email')
    otp = wait_otp_from_cf_inbox(s, INBOX_API, MAIL_JWT)
else:
    print('  inbox: inbucket')
    otp = wait_otp_from_inbucket(s, INBOX_API, EMAIL)
if otp:
    print('  OTP: ' + otp)
if not otp:
    print('  NO OTP!')
    sys.exit(1)

print('[5] Verify OTP...')
s.get('https://auth.openai.com/email-verification', headers=h('https://auth.openai.com/create-account/password'), timeout=15)
snt_otp = get_sentinel_token(s, device_id=did, flow='authorize_continue')
r = s.post(
    'https://auth.openai.com/api/accounts/email-otp/validate',
    headers={
        **h('https://auth.openai.com/email-verification'),
        'Content-Type': 'application/json',
        'openai-sentinel-token': snt_otp,
    },
    json={'code': otp},
    timeout=30,
)
print('  validate: ' + str(r.status_code))
if r.status_code != 200:
    print('  FAIL: ' + r.text[:500])
    sys.exit(1)
otp_data = r.json()
cu = str(otp_data.get('continue_url', '') or '')
page_type = ''
if isinstance(otp_data.get('page'), dict):
    page_type = str(otp_data['page'].get('type', '') or '')
print('  page_type: ' + (page_type or 'NONE'))
print('  continue_url: ' + cu[:120])
if cu.startswith('/'):
    cu = urljoin('https://auth.openai.com', cu)
if cu:
    s.get(cu, headers=h('https://auth.openai.com/email-verification'), timeout=15, allow_redirects=True)
else:
    s.get('https://auth.openai.com/about-you', headers=h('https://auth.openai.com/email-verification'), timeout=15)

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
