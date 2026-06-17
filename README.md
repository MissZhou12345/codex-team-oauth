# Codex Team-跳过手机号注册脚本

## 说明

这是一个脱敏版的 `codex_team_oauth.py` 脚本，用于展示如何使用 Codex client_id 流程自动注册账号并获取 OAuth token。

该脚本依赖本地 `deps/` 目录中的 Sentinel 支持代码。

## 需要分享的文件

- `codex_team_oauth.py`
- `deps/__init__.py`
- `deps/sentinel.py`
- `deps/sentinel_quickjs.py`
- `deps/openai_sentinel_quickjs.js`
- `.env.example`

## 依赖安装

请先安装 Python 依赖：

```bash
python3 -m pip install --upgrade pip
pip install -r requirements.txt
```

macOS 12 (Monterey) 若 `curl_cffi` 报 `_SCDynamicStoreCopyProxies` 错误，请使用 `requirements.txt` 里固定的 `0.7.4` 版本，不要直接装最新版。

如果要走 QuickJS 路径，还需要系统可用 `node`：

```bash
node -v
```

## 环境变量配置

复制 `.env.example` 为 `.env`：

```bash
cp .env.example .env
```

编辑 `.env`，将以下变量替换为真实值：

- `TEST_PASSWORD`：OpenAI 注册密码
- `TEST_INBOX_API`：收件服务根地址（不要带 `/api/v1`）

**方式 A：Cloudflare 临时邮箱（cloudflare_temp_email）**

- `TEST_EMAIL_DOMAIN`：收信域名，例如 `edu.myfe.xyz`
- `TEST_CF_ADMIN_PASSWORD`：Worker 管理员密码（用于自动创建邮箱）
- `TEST_MAIL_JWT`：可选；若已有邮箱 JWT 可填，否则留空由脚本自动创建
- `TEST_EMAIL`：可选；留空则自动随机创建，或填写指定前缀如 `codex001@edu.myfe.xyz`

**方式 B：自建 Inbucket**

- `TEST_INBOX_MODE=inbucket`
- `TEST_INBOX_API`：例如 `http://127.0.0.1:9000/api/v1`
- `TEST_EMAIL`：完整邮箱地址

`CLIENT_ID` 和 `CODEX_REDIRECT_URI` 已固定在脚本中，无需在 `.env` 中配置。

## 运行方式

```bash
python3 codex_team_oauth.py
```

脚本会自动读取当前目录下的 `.env` 文件。

## 注意

- 该脚本仅用于示例和测试，切勿分享 `.env` 中的真实凭据。
- 不要附带 `/tmp/codex_token_*.json` 或其他敏感输出文件。
- `TEST_INBOX_API` 需要指向可用的 OTP 邮件服务接口（支持 Inbucket 或 cloudflare_temp_email）。

## 友情链接

- [LINUX DO - 新的理想型社区](https://linux.do/)