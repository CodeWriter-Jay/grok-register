
from __future__ import annotations

import json
import random
import re
import string
import time
from email import policy
from email.parser import BytesParser
from html import unescape
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from curl_cffi import requests as curl_requests
except ImportError:
    curl_requests = None

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ============================================================
# VMAIL API 配置（从 config.json 加载）
# 接口文档: https://vmail.codewriterjay.org/api-docs
# ============================================================

_config_path = Path(__file__).parent / "config.json"
_conf: Dict[str, Any] = {}
if _config_path.exists():
    with _config_path.open("r", encoding="utf-8") as _f:
        _conf = json.load(_f)

# API 基础地址，默认指向自建 VMAIL 实例
VMAIL_API_BASE = str(
    _conf.get("vmail_api_base")
    or _conf.get("temp_mail_api_base")
    or "https://vmail.codewriterjay.org/api/v1"
).rstrip("/")

# API Key（在 API 文档页面创建并填入 config.json）
VMAIL_API_KEY = str(
    _conf.get("vmail_api_key")
    or _conf.get("temp_mail_admin_password")
    or _conf.get("duckmail_bearer")
    or ""
)

# 邮箱域名（可选，留空则由服务器随机分配）
TEMP_MAIL_DOMAIN = str(_conf.get("temp_mail_domain", ""))

PROXY = str(_conf.get("proxy", ""))

# ============================================================
# 适配层：为 DrissionPage_example.py 提供简单接口
# ============================================================

_temp_email_cache: Dict[str, str] = {}


def get_email_and_token() -> Tuple[Optional[str], Optional[str]]:
    """
    创建临时邮箱并返回 (email, mailbox_id)。
    供 DrissionPage_example.py 调用。
    注：新版 API 使用 mailbox_id 代替 JWT token 来查询邮件。
    """
    email, mailbox_id, _ = create_temp_email()
    if email and mailbox_id:
        _temp_email_cache[email] = mailbox_id
        return email, mailbox_id
    return None, None


def get_oai_code(mailbox_id: str, email: str, timeout: int = 30) -> Optional[str]:
    """
    轮询收件箱获取 OTP 验证码。
    供 DrissionPage_example.py 调用。

    Args:
        mailbox_id: 创建邮箱时返回的 mailbox ID（原接口为 JWT token）
        email: 邮箱地址（此处仅用于日志输出）
        timeout: 最大等待秒数

    Returns:
        验证码字符串（去除连字符，如 "MM0SF3"）或 None
    """
    code = wait_for_verification_code(mailbox_id=mailbox_id, timeout=timeout)
    if code:
        code = code.replace("-", "")
    return code


# ============================================================
# VMAIL API 核心函数
# ============================================================

def _create_session():
    """创建请求会话（优先 curl_cffi 以绕过反爬检测）。"""
    if curl_requests:
        session = curl_requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        if PROXY:
            session.proxies = {"http": PROXY, "https": PROXY}
        return session, True

    s = requests.Session()
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
        "Content-Type": "application/json",
    })
    if PROXY:
        s.proxies = {"http": PROXY, "https": PROXY}
    return s, False


def _do_request(session, use_cffi, method, url, **kwargs):
    """统一发送请求，curl_cffi 自动附带 impersonate 指纹。"""
    if use_cffi:
        kwargs.setdefault("impersonate", "chrome131")
    return getattr(session, method)(url, **kwargs)


def _build_headers(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """
    构建请求 Headers。
    VMAIL API 使用 X-API-Key 作为全局认证凭据。
    """
    headers: Dict[str, str] = {}
    if VMAIL_API_KEY:
        headers["X-API-Key"] = VMAIL_API_KEY
    if extra:
        headers.update(extra)
    return headers


def _generate_local_part(length: int = 10) -> str:
    """生成随机邮箱前缀（小写字母 + 数字）。"""
    chars = string.ascii_lowercase + string.digits
    return "".join(random.choice(chars) for _ in range(length))


# ============================================================
# 邮箱管理
# ============================================================

def get_available_domains() -> List[str]:
    """
    GET /domains
    获取系统支持的可用邮箱域名列表。
    """
    try:
        session, use_cffi = _create_session()
        res = _do_request(
            session, use_cffi, "get",
            f"{VMAIL_API_BASE}/domains",
            headers=_build_headers(),
            timeout=20,
        )
        if res.status_code == 200:
            data = res.json()
            # 响应格式: {"data": ["domain1.com", "domain2.com"]}
            return data.get("data") or []
    except Exception as e:
        print(f"[!] 获取可用域名失败: {e}")
    return []


def create_temp_email() -> Tuple[str, str, str]:
    """
    POST /mailboxes
    创建临时邮箱，返回 (email, mailbox_id, expires_at)。

    请求体:
        localPart  - 邮箱前缀（可选）
        domain     - 域名（可选，须是 /domains 返回的值）
        expiresIn  - 有效期秒数（可选，默认 3600）

    响应:
        {"data": {"id": "...", "address": "...", "expiresAt": "..."}}
    """
    if not VMAIL_API_KEY:
        raise Exception("vmail_api_key 未设置，请在 config.json 中配置")

    local_part = _generate_local_part(random.randint(8, 12))
    session, use_cffi = _create_session()

    # 构建请求体（domain 可选，留空则由服务端随机分配）
    body: Dict[str, Any] = {
        "localPart": local_part,
        "expiresIn": 3600,
    }
    if TEMP_MAIL_DOMAIN:
        body["domain"] = TEMP_MAIL_DOMAIN

    try:
        res = _do_request(
            session, use_cffi, "post",
            f"{VMAIL_API_BASE}/mailboxes",
            json=body,
            headers=_build_headers(),
            timeout=20,
        )
        if res.status_code not in (200, 201):
            raise Exception(f"创建邮箱失败: {res.status_code} - {res.text[:200]}")

        data = res.json().get("data") or {}
        mailbox_id = data.get("id") or ""
        email = data.get("address") or ""
        expires_at = data.get("expiresAt") or ""

        if not email or not mailbox_id:
            raise Exception(f"接口返回缺少 id/address 字段: {data}")

        print(f"[*] VMAIL 临时邮箱创建成功: {email} (id={mailbox_id})")
        return email, mailbox_id, expires_at

    except Exception as e:
        raise Exception(f"VMAIL 临时邮箱创建失败: {e}")


# ============================================================
# 邮件读取
# ============================================================

def fetch_emails(mailbox_id: str) -> List[Dict[str, Any]]:
    """
    GET /mailboxes/:id/messages
    获取指定邮箱的邮件列表。

    Args:
        mailbox_id: 创建邮箱时返回的 mailbox ID

    Returns:
        邮件列表（每项包含 id、subject、from 等字段）
    """
    try:
        session, use_cffi = _create_session()
        res = _do_request(
            session, use_cffi, "get",
            f"{VMAIL_API_BASE}/mailboxes/{mailbox_id}/messages",
            params={"page": 1, "limit": 20},
            headers=_build_headers(),
            timeout=20,
        )
        if res.status_code == 200:
            data = res.json()
            # 响应格式: {"data": [...]} 或兼容旧版 {"results": [...]}
            return (
                data.get("data")
                or data.get("results")
                or (data if isinstance(data, list) else [])
            )
    except Exception as e:
        print(f"[!] 获取邮件列表失败: {e}")
    return []


def fetch_email_detail(mailbox_id: str, message_id: str) -> Optional[Dict[str, Any]]:
    """
    GET /mailboxes/:id/messages/:messageId
    获取单封邮件详情（含 HTML/text 正文）。

    Args:
        mailbox_id: mailbox ID
        message_id: 邮件 ID

    Returns:
        邮件详情 dict 或 None
    """
    try:
        session, use_cffi = _create_session()
        res = _do_request(
            session, use_cffi, "get",
            f"{VMAIL_API_BASE}/mailboxes/{mailbox_id}/messages/{message_id}",
            headers=_build_headers(),
            timeout=20,
        )
        if res.status_code == 200:
            data = res.json()
            # 响应可能包装在 data 字段内
            return data.get("data") if isinstance(data.get("data"), dict) else data
    except Exception as e:
        print(f"[!] 获取邮件详情失败 (mailbox={mailbox_id}, msg={message_id}): {e}")
    return None


def wait_for_verification_code(mailbox_id: str, timeout: int = 120) -> Optional[str]:
    """
    轮询邮箱，等待并提取验证码邮件。

    Args:
        mailbox_id: VMAIL mailbox ID
        timeout: 最大等待秒数

    Returns:
        验证码字符串或 None
    """
    start = time.time()
    seen_ids: set = set()

    while time.time() - start < timeout:
        messages = fetch_emails(mailbox_id)
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            msg_id = msg.get("id")
            if not msg_id or msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)

            detail = fetch_email_detail(mailbox_id, str(msg_id))
            if not detail:
                continue

            content = _extract_mail_content(detail)
            code = extract_verification_code(content)
            if code:
                print(f"[*] 从 VMAIL 临时邮箱提取到验证码: {code}")
                return code

        time.sleep(3)

    print(f"[!] 等待验证码超时（{timeout}s）")
    return None


# ============================================================
# 邮件内容解析（保持不变）
# ============================================================

def _extract_mail_content(detail: Dict[str, Any]) -> str:
    """兼容 text/html/raw MIME 三种内容来源。"""
    direct_parts = [
        detail.get("subject"),
        detail.get("text"),
        detail.get("html"),
        detail.get("raw"),
        detail.get("source"),
    ]
    direct_content = "\n".join(str(part) for part in direct_parts if part)
    if detail.get("text") or detail.get("html"):
        return direct_content

    raw = detail.get("raw") or detail.get("source")
    if not raw or not isinstance(raw, str):
        return direct_content
    return f"{direct_content}\n{_parse_raw_email(raw)}"


def _parse_raw_email(raw: str) -> str:
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw.encode("utf-8", errors="ignore"))
    except Exception:
        return raw

    parts: List[str] = []
    subject = message.get("subject")
    if subject:
        parts.append(f"Subject: {subject}")

    if message.is_multipart():
        for part in message.walk():
            if part.get_content_maintype() == "multipart":
                continue
            disposition = (part.get_content_disposition() or "").lower()
            if disposition == "attachment":
                continue
            content = _decode_email_part(part)
            if content:
                parts.append(content)
    else:
        content = _decode_email_part(message)
        if content:
            parts.append(content)
    return "\n".join(parts)


def _decode_email_part(part) -> str:
    try:
        content = part.get_content()
        if isinstance(content, bytes):
            charset = part.get_content_charset() or "utf-8"
            content = content.decode(charset, errors="ignore")
        if not isinstance(content, str):
            content = str(content)
        if "html" in (part.get_content_type() or "").lower():
            content = _html_to_text(content)
        return content.strip()
    except Exception:
        payload = part.get_payload(decode=True)
        if isinstance(payload, bytes):
            charset = part.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="ignore").strip()
    return ""


def _html_to_text(html: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return unescape(re.sub(r"[ \t\r\f\v]+", " ", text)).strip()


def extract_verification_code(content: str) -> Optional[str]:
    """
    从邮件内容提取验证码。
    Grok/x.ai 格式：MM0-SF3（3位-3位字母数字混合）或 6 位纯数字。
    """
    if not content:
        return None

    # 模式 1: Grok 格式 XXX-XXX
    m = re.search(r"(?<![A-Z0-9-])([A-Z0-9]{3}-[A-Z0-9]{3})(?![A-Z0-9-])", content)
    if m:
        return m.group(1)

    # 模式 2: 带标签的验证码
    m = re.search(r"(?:verification code|验证码|your code)[:\s]*[<>\s]*([A-Z0-9]{3}-[A-Z0-9]{3})\b", content, re.IGNORECASE)
    if m:
        return m.group(1)

    # 模式 3: HTML 样式包裹
    m = re.search(r"background-color:\s*#F3F3F3[^>]*>[\s\S]*?([A-Z0-9]{3}-[A-Z0-9]{3})[\s\S]*?</p>", content)
    if m:
        return m.group(1)

    # 模式 4: Subject 行 6 位数字
    m = re.search(r"Subject:.*?(\d{6})", content)
    if m and m.group(1) != "177010":
        return m.group(1)

    # 模式 5: HTML 标签内 6 位数字
    for code in re.findall(r">\s*(\d{6})\s*<", content):
        if code != "177010":
            return code

    # 模式 6: 独立 6 位数字
    for code in re.findall(r"(?<![&#\d])(\d{6})(?![&#\d])", content):
        if code != "177010":
            return code

    return None
