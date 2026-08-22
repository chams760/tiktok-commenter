import email as email_lib
import imaplib
import json
import os
import re
import time

import requests as std_requests
from loguru import logger

try:
    from curl_cffi import requests as cffi_requests
    _HAS_CFFI = True
except ImportError:
    _HAS_CFFI = False
    logger.warning("curl_cffi not available, using standard requests (no TLS impersonation)")

_IMPERSONATE_OPTIONS = [
    "chrome", "chrome120", "chrome119", "chrome116", "chrome110",
    "chrome107", "chrome104", "chrome101", "chrome100", "chrome99",
]


def _create_session(proxy_url: str = ""):
    if _HAS_CFFI:
        for imp in _IMPERSONATE_OPTIONS:
            try:
                session = cffi_requests.Session(impersonate=imp)
                if proxy_url:
                    session.proxies = {"http": proxy_url, "https": proxy_url}
                session.get("https://www.tiktok.com/robots.txt", timeout=10)
                logger.info(f"curl_cffi session created with impersonate={imp}")
                return session, imp
            except Exception as e:
                logger.debug(f"impersonate={imp} failed: {e}")
                continue
        logger.warning("All curl_cffi impersonate options failed, falling back to requests")

    session = std_requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    })
    if proxy_url:
        session.proxies = {"http": proxy_url, "https": proxy_url}
    return session, "requests-fallback"

SESSIONS_DIR = os.path.join(os.path.dirname(__file__), "sessions")
os.makedirs(SESSIONS_DIR, exist_ok=True)

IMAP_SERVERS = {
    "gmail.com": "imap.gmail.com",
    "googlemail.com": "imap.gmail.com",
    "outlook.com": "imap-mail.outlook.com",
    "hotmail.com": "imap-mail.outlook.com",
    "live.com": "imap-mail.outlook.com",
    "yahoo.com": "imap.mail.yahoo.com",
    "mail.ru": "imap.mail.ru",
    "inbox.ru": "imap.mail.ru",
    "list.ru": "imap.mail.ru",
    "bk.ru": "imap.mail.ru",
    "yandex.ru": "imap.yandex.ru",
    "yandex.com": "imap.yandex.ru",
    "ya.ru": "imap.yandex.ru",
    "icloud.com": "imap.mail.me.com",
    "me.com": "imap.mail.me.com",
    "rambler.ru": "imap.rambler.ru",
    "gmx.com": "imap.gmx.com",
    "zoho.com": "imap.zoho.com",
    "firstmail.ltd": "imap.firstmail.ltd",
    "bumblespf.com": "imap.firstmail.ltd",
}


def _get_imap_server(email_addr: str) -> str:
    domain = email_addr.split("@")[-1].lower()
    return IMAP_SERVERS.get(domain, f"imap.{domain}")


def _parse_proxy_for_requests(proxy_str: str) -> dict:
    if not proxy_str:
        return {}
    parts = proxy_str.split(":")
    if len(parts) == 4:
        login, passw, host, port = parts
        url = f"http://{login}:{passw}@{host}:{port}"
        return {"http": url, "https": url}
    elif len(parts) == 2:
        url = f"http://{proxy_str}"
        return {"http": url, "https": url}
    if proxy_str.startswith("http"):
        return {"http": proxy_str, "https": proxy_str}
    return {}


def _save_api_cookies(session, username: str):
    safe = username.replace("@", "_at_").replace(".", "_")
    path = os.path.join(SESSIONS_DIR, f"{safe}.json")
    cookies = []
    jar = session.cookies
    try:
        cookie_dict = dict(jar.items())
    except Exception:
        cookie_dict = {}
        for name in jar:
            cookie_dict[name if isinstance(name, str) else name.name] = jar.get(name if isinstance(name, str) else name.name, "")
    for name, value in cookie_dict.items():
        cookies.append({
            "name": name,
            "value": value,
            "domain": ".tiktok.com",
            "path": "/",
        })
    with open(path, "w") as f:
        json.dump(cookies, f)
    logger.info(f"Saved {len(cookies)} API cookies for {username}")


def fetch_code_from_imap(email_addr: str, email_pass: str, imap_server: str = "", timeout: int = 90) -> str | None:
    if not imap_server:
        imap_server = _get_imap_server(email_addr)

    logger.info(f"IMAP: connecting to {imap_server} for {email_addr}")
    start = time.time()
    mark_time = start - 60

    while time.time() - start < timeout:
        mail = None
        try:
            mail = imaplib.IMAP4_SSL(imap_server, 993)
            mail.login(email_addr, email_pass)
            mail.select("INBOX")

            _, msgs = mail.search(None, '(FROM "tiktok.com" UNSEEN)')
            if not msgs[0]:
                _, msgs = mail.search(None, '(FROM "tiktok" UNSEEN)')
            if not msgs[0]:
                _, msgs = mail.search(None, '(SUBJECT "verification" UNSEEN)')
            if not msgs[0]:
                _, msgs = mail.search(None, '(SUBJECT "code" UNSEEN)')

            if msgs[0]:
                msg_ids = msgs[0].split()
                for msg_id in reversed(msg_ids):
                    _, msg_data = mail.fetch(msg_id, "(RFC822)")
                    raw = msg_data[0][1]
                    msg = email_lib.message_from_bytes(raw)

                    body = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            ct = part.get_content_type()
                            if ct in ("text/plain", "text/html"):
                                payload = part.get_payload(decode=True)
                                if payload:
                                    body += payload.decode("utf-8", errors="ignore")
                    else:
                        payload = msg.get_payload(decode=True)
                        if payload:
                            body = payload.decode("utf-8", errors="ignore")

                    codes = re.findall(r'\b(\d{4,6})\b', body)
                    if codes:
                        code = max(codes, key=len)
                        logger.info(f"IMAP: found TikTok code: {code}")
                        mail.logout()
                        return code

            mail.logout()
        except Exception as e:
            logger.warning(f"IMAP error: {e}")
            if mail:
                try:
                    mail.logout()
                except Exception:
                    pass

        time.sleep(5)

    logger.warning(f"IMAP: no TikTok code found in {timeout}s")
    return None


_pending_api_sessions: dict[str, dict] = {}


def _get_csrf(session) -> str:
    return session.cookies.get("tt_csrf_token", "")


def _post_api(session, url: str, data: dict, step_fn, step_name: str) -> dict | None:
    csrf = _get_csrf(session)
    headers = {}
    if csrf:
        headers["X-CSRFToken"] = csrf

    for method in ["form", "json"]:
        try:
            if method == "form":
                resp = session.post(url, data=data, headers=headers, timeout=15)
            else:
                resp = session.post(url, json=data, headers=headers, timeout=15)

            try:
                result = resp.json()
            except Exception:
                result = {"raw": resp.text[:300]}

            step_fn(step_name, f"{url} ({method}) -> {resp.status_code}: {json.dumps(result, ensure_ascii=False)[:300]}")

            status_code = result.get("status_code")
            error_code = result.get("error_code")
            data_field = result.get("data", {})
            msg = str(result.get("message", "")).lower()
            desc = str(result.get("description", "") or (data_field.get("description", "") if isinstance(data_field, dict) else "")).lower()

            if error_code == 0 or (status_code == 0 and "doesn't match" not in (result.get("status_msg", ""))):
                return {"success": True, "result": result, "desc": desc, "error_code": error_code}

            if "maximum" in desc or "too many" in desc or error_code == 7:
                return {"success": False, "rate_limited": True, "result": result, "desc": desc}

            if "verify" in desc or error_code in (1105, 10000, 10202):
                return {"success": False, "need_verify": True, "result": result, "desc": desc}

        except Exception as e:
            step_fn(f"{step_name}_error", f"{url} ({method}): {e}")

    return None


def api_login(username: str, password: str, email_pass: str = "",
              imap_server: str = "", proxy: str = "") -> dict:
    steps = []

    proxy_url = ""
    if proxy:
        proxies = _parse_proxy_for_requests(proxy)
        proxy_url = proxies.get("https", proxies.get("http", ""))

    session, imp_name = _create_session(proxy_url)
    session.headers.update({
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://www.tiktok.com",
        "Referer": "https://www.tiktok.com/login/phone-or-email/email",
    })

    def step(name, note=""):
        steps.append({"step": name, "note": note})
        logger.info(f"API login [{name}]: {note}")

    step("init", f"Starting API login for {username} | proxy: {bool(proxy)} | TLS: {imp_name}")

    try:
        resp = session.get("https://www.tiktok.com/", timeout=15)
        step("visit_homepage", f"Status: {resp.status_code}, cookies: {list(session.cookies.keys())}")
    except Exception as e:
        step("visit_error", str(e))
        return {"ok": False, "steps": steps, "error": str(e)}

    try:
        resp = session.get("https://www.tiktok.com/login/phone-or-email/email", timeout=15)
        step("visit_login", f"Status: {resp.status_code}, cookies: {list(session.cookies.keys())}")
    except Exception as e:
        step("visit_login_error", str(e))

    csrf = _get_csrf(session)
    step("csrf_token", f"CSRF: {csrf[:10]}..." if csrf else "No CSRF token found")

    send_code_endpoints = [
        "https://www.tiktok.com/passport/web/send_code/",
        "https://www.tiktok.com/passport/web/email/send_code/",
        "https://www.tiktok.com/api/passport/web/send_code/",
        "https://www.tiktok.com/api/passport/web/email/send_code/",
    ]

    code_sent = False
    for url in send_code_endpoints:
        for code_type in [1, 3, "login"]:
            r = _post_api(session, url, {
                "email": username,
                "type": code_type,
                "aid": "1459",
                "account_sdk_source": "web",
                "mix_mode": 1,
                "service": "https://www.tiktok.com",
            }, step, "send_code")
            if r and r.get("success"):
                code_sent = True
                step("code_sent", f"Code sent via {url} (type={code_type})")
                break
            if r and r.get("rate_limited"):
                step("rate_limited", "Too many attempts. Use a proxy or wait.")
                return {"ok": False, "steps": steps, "error": "Rate limited. Use a proxy."}
        if code_sent:
            break

    if not code_sent:
        step("send_code_failed", "Email code endpoints failed. Trying password login...")

        pwd_endpoints = [
            "https://www.tiktok.com/passport/web/user/login/",
            "https://www.tiktok.com/api/passport/web/user/login/",
        ]

        for url in pwd_endpoints:
            r = _post_api(session, url, {
                "email": username,
                "password": password,
                "mix_mode": 1,
                "aid": "1459",
                "account_sdk_source": "web",
                "service": "https://www.tiktok.com",
            }, step, "password_login")

            if r and r.get("success"):
                _save_api_cookies(session, username)
                step("login_success", f"Password login OK! Cookies: {list(session.cookies.keys())}")
                return {"ok": True, "steps": steps}

            if r and r.get("rate_limited"):
                step("rate_limited", "Too many attempts. Use a proxy or wait.")
                return {"ok": False, "steps": steps, "error": "Rate limited. Use a proxy."}

            if r and r.get("need_verify"):
                step("need_verify", "TikTok requires verification after password login")
                for send_url in send_code_endpoints:
                    r2 = _post_api(session, send_url, {
                        "email": username,
                        "type": "verify",
                        "aid": "1459",
                        "account_sdk_source": "web",
                    }, step, "verify_send_code")
                    if r2 and r2.get("success"):
                        code_sent = True
                        break
                if not code_sent:
                    step("verify_send_failed", "Could not send verification code")
                    return {"ok": False, "steps": steps, "error": "Could not send verification code"}
                break

        if not code_sent:
            step("all_failed", "All login methods failed")
            return {"ok": False, "steps": steps, "error": "All login methods failed"}

    if not email_pass:
        step("waiting_for_code", "Code request sent. Enter the code manually.")
        _pending_api_sessions[username] = {"session": session, "steps": steps}
        return {"ok": False, "steps": steps, "need_code": True, "username": username}

    step("imap_waiting", f"Waiting for code via IMAP ({_get_imap_server(username)})...")
    code = fetch_code_from_imap(username, email_pass, imap_server, timeout=90)
    if not code:
        step("imap_no_code", "No verification code received in 90 seconds")
        return {"ok": False, "steps": steps, "error": "Email code not received via IMAP"}

    step("imap_got_code", f"Got code: {code}")

    verify_endpoints = [
        "https://www.tiktok.com/passport/web/user/login/",
        "https://www.tiktok.com/passport/web/email/login/",
        "https://www.tiktok.com/api/passport/web/user/login/",
    ]

    for url in verify_endpoints:
        r = _post_api(session, url, {
            "email": username,
            "code": code,
            "aid": "1459",
            "mix_mode": 1,
            "account_sdk_source": "web",
            "service": "https://www.tiktok.com",
        }, step, "login_with_code")
        if r and r.get("success"):
            _save_api_cookies(session, username)
            step("login_success", f"Login successful! Cookies: {list(session.cookies.keys())}")
            return {"ok": True, "steps": steps}

    verify_code_endpoints = [
        "https://www.tiktok.com/passport/web/verify_code/",
        "https://www.tiktok.com/api/passport/web/verify_code/",
    ]
    for url in verify_code_endpoints:
        r = _post_api(session, url, {
            "email": username,
            "code": code,
            "aid": "1459",
            "account_sdk_source": "web",
        }, step, "verify_code")
        if r and r.get("success"):
            _save_api_cookies(session, username)
            step("login_success", f"Verification successful! Cookies: {list(session.cookies.keys())}")
            return {"ok": True, "steps": steps}

    step("login_failed", "Could not complete login with the code")
    return {"ok": False, "steps": steps, "error": "Login with code failed"}


def api_submit_code(username: str, code: str) -> dict:
    pending = _pending_api_sessions.pop(username, None)
    if not pending:
        return {"ok": False, "error": f"No pending session for {username}. Run login first."}

    session = pending["session"]
    steps = pending["steps"]

    def step(name, note=""):
        steps.append({"step": name, "note": note})
        logger.info(f"API submit code [{name}]: {note}")

    step("manual_code", f"Code entered: {code}")

    verify_endpoints = [
        "https://www.tiktok.com/passport/web/user/login/",
        "https://www.tiktok.com/passport/web/email/login/",
        "https://www.tiktok.com/api/passport/web/user/login/",
    ]
    for url in verify_endpoints:
        r = _post_api(session, url, {
            "email": username,
            "code": code,
            "aid": "1459",
            "mix_mode": 1,
            "account_sdk_source": "web",
            "service": "https://www.tiktok.com",
        }, step, "login_with_code")
        if r and r.get("success"):
            _save_api_cookies(session, username)
            step("login_success", f"Login successful! Cookies: {list(session.cookies.keys())}")
            return {"ok": True, "steps": steps}

    verify_code_endpoints = [
        "https://www.tiktok.com/passport/web/verify_code/",
        "https://www.tiktok.com/api/passport/web/verify_code/",
    ]
    for url in verify_code_endpoints:
        r = _post_api(session, url, {
            "email": username,
            "code": code,
            "aid": "1459",
            "account_sdk_source": "web",
        }, step, "verify_code")
        if r and r.get("success"):
            _save_api_cookies(session, username)
            step("login_success", f"Verification successful! Cookies: {list(session.cookies.keys())}")
            return {"ok": True, "steps": steps}

    step("verify_failed", "Code verification failed")
    return {"ok": False, "steps": steps, "error": "Code verification failed"}
