import email as email_lib
import hashlib
import imaplib
import json
import os
import re
import time

import requests
from loguru import logger

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


def _save_api_cookies(session: requests.Session, username: str):
    safe = username.replace("@", "_at_").replace(".", "_")
    path = os.path.join(SESSIONS_DIR, f"{safe}.json")
    cookies = []
    for c in session.cookies:
        cookies.append({
            "name": c.name,
            "value": c.value,
            "domain": c.domain or ".tiktok.com",
            "path": c.path or "/",
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


def api_login(username: str, password: str, email_pass: str = "",
              imap_server: str = "", proxy: str = "") -> dict:
    steps = []
    session = requests.Session()

    proxies = _parse_proxy_for_requests(proxy)
    if proxies:
        session.proxies = proxies

    session.headers.update({
        "User-Agent": "com.zhiliaoapp.musically/350103 (Linux; U; Android 13; en_US; Pixel 7; Build/TP1A.220624.014; Cronet/TTNetVersion:7be4f78f 2024-01-18)",
        "Accept": "application/json",
        "Accept-Encoding": "gzip, deflate",
        "X-SS-DP": "1233",
        "X-Tt-Store-Region": "us",
        "X-Tt-Store-Region-Src": "uid",
        "passport-sdk-version": "19",
    })

    def step(name, note=""):
        steps.append({"step": name, "note": note})
        logger.info(f"API login [{name}]: {note}")

    step("init", f"Starting API login for {username} | proxy: {bool(proxy)}")

    try:
        resp = session.get("https://www.tiktok.com/", timeout=15)
        step("visit_homepage", f"Status: {resp.status_code}, cookies: {list(session.cookies.keys())}")
    except Exception as e:
        step("visit_homepage_error", str(e))

    try:
        resp = session.get("https://www.tiktok.com/login/phone-or-email/email", timeout=15)
        step("visit_login", f"Status: {resp.status_code}")
    except Exception as e:
        step("visit_login_error", str(e))

    try:
        send_code_urls = [
            "https://www.tiktok.com/passport/web/email_code/send/",
            "https://www.tiktok.com/api/passport/web/email_code/send/",
            "https://www.tiktok.com/passport/email/code/send/",
        ]

        code_sent = False
        for url in send_code_urls:
            for content_type in ["form", "json"]:
                try:
                    if content_type == "form":
                        resp = session.post(url, data={
                            "email": username,
                            "type": "login",
                            "aid": "1459",
                        }, timeout=15)
                    else:
                        resp = session.post(url, json={
                            "email": username,
                            "type": "login",
                            "aid": "1459",
                        }, timeout=15)

                    try:
                        result = resp.json()
                    except Exception:
                        result = {"raw": resp.text[:200]}

                    step("send_code", f"URL: {url} ({content_type}) -> Status: {resp.status_code}, Response: {json.dumps(result, ensure_ascii=False)[:300]}")

                    msg = str(result.get("message", "")).lower()
                    data = result.get("data", {})
                    if msg in ("success", "ok", "") and resp.status_code == 200 and data:
                        code_sent = True
                        break
                    if result.get("error_code") == 0:
                        code_sent = True
                        break
                except Exception as e:
                    step("send_code_error", f"{url}: {e}")
            if code_sent:
                break

        if not code_sent:
            step("send_code_failed", "All endpoints failed to send code. Trying password login...")
            return _api_password_login(session, username, password, email_pass, imap_server, steps)

    except Exception as e:
        step("send_code_exception", str(e))
        return {"ok": False, "steps": steps, "error": str(e)}

    if not email_pass:
        step("waiting_for_code", "Code sent but no email_password provided for IMAP. Enter code manually.")
        return {"ok": False, "steps": steps, "need_code": True}

    step("imap_waiting", f"Waiting for code via IMAP ({_get_imap_server(username)})...")

    code = fetch_code_from_imap(username, email_pass, imap_server, timeout=90)
    if not code:
        step("imap_no_code", "No code received in 90 seconds")
        return {"ok": False, "steps": steps, "error": "Email code not received"}

    step("imap_got_code", f"Got code: {code}")

    login_urls = [
        "https://www.tiktok.com/passport/web/email_code/login/",
        "https://www.tiktok.com/api/passport/web/email_code/login/",
        "https://www.tiktok.com/passport/email/code/login/",
    ]

    for url in login_urls:
        for content_type in ["form", "json"]:
            try:
                payload = {"email": username, "code": code, "aid": "1459"}
                if content_type == "form":
                    resp = session.post(url, data=payload, timeout=15)
                else:
                    resp = session.post(url, json=payload, timeout=15)

                try:
                    result = resp.json()
                except Exception:
                    result = {"raw": resp.text[:200]}

                step("login_with_code", f"{url} ({content_type}) -> {resp.status_code}: {json.dumps(result, ensure_ascii=False)[:300]}")

                if result.get("error_code") == 0 or (result.get("data") and "session" in str(result.get("data", "")).lower()):
                    _save_api_cookies(session, username)
                    step("login_success", f"Logged in! Cookies: {list(session.cookies.keys())}")
                    return {"ok": True, "steps": steps, "cookies": list(session.cookies.keys())}

            except Exception as e:
                step("login_code_error", f"{url}: {e}")

    step("login_failed", "All login endpoints failed")
    return {"ok": False, "steps": steps, "error": "Login with code failed"}


def _api_password_login(session: requests.Session, username: str, password: str,
                        email_pass: str, imap_server: str, steps: list) -> dict:

    def step(name, note=""):
        steps.append({"step": name, "note": note})
        logger.info(f"API pwd login [{name}]: {note}")

    login_urls = [
        "https://www.tiktok.com/passport/web/user/login/",
        "https://www.tiktok.com/api/passport/web/user/login/",
    ]

    for url in login_urls:
        try:
            payload = {
                "email": username,
                "password": password,
                "mix_mode": 1,
                "aid": "1459",
            }
            resp = session.post(url, data=payload, timeout=15)
            try:
                result = resp.json()
            except Exception:
                result = {"raw": resp.text[:200]}

            step("password_login", f"{url} -> {resp.status_code}: {json.dumps(result, ensure_ascii=False)[:300]}")

            err_code = result.get("error_code", -1)
            desc = str(result.get("description", "")).lower()

            if err_code == 0:
                _save_api_cookies(session, username)
                step("login_success", f"Password login successful! Cookies: {list(session.cookies.keys())}")
                return {"ok": True, "steps": steps}

            if "verify" in desc or err_code in (1105, 10000):
                step("need_verify", f"Verification required: {desc}")

                verify_urls = [
                    "https://www.tiktok.com/passport/web/send_code/",
                    "https://www.tiktok.com/api/passport/web/send_code/",
                ]
                for vurl in verify_urls:
                    try:
                        vresp = session.post(vurl, data={
                            "email": username,
                            "type": "verify",
                            "aid": "1459",
                        }, timeout=15)
                        vresult = vresp.json()
                        step("verify_send", f"{vurl} -> {json.dumps(vresult, ensure_ascii=False)[:200]}")
                    except Exception as e:
                        step("verify_send_error", str(e))

                if not email_pass:
                    step("need_email_pass", "Verification needed but no email password for IMAP")
                    return {"ok": False, "steps": steps, "need_code": True}

                code = fetch_code_from_imap(username, email_pass, imap_server, timeout=90)
                if not code:
                    step("imap_no_code", "Code not received")
                    return {"ok": False, "steps": steps, "error": "Verification code not received"}

                step("got_verify_code", f"Code: {code}")

                for vurl in ["https://www.tiktok.com/passport/web/verify_code/",
                             "https://www.tiktok.com/api/passport/web/verify_code/"]:
                    try:
                        vresp = session.post(vurl, data={
                            "email": username,
                            "code": code,
                            "aid": "1459",
                        }, timeout=15)
                        vresult = vresp.json()
                        step("verify_code_submit", f"{vurl} -> {json.dumps(vresult, ensure_ascii=False)[:200]}")
                        if vresult.get("error_code") == 0:
                            _save_api_cookies(session, username)
                            step("login_success", "Login with verification successful!")
                            return {"ok": True, "steps": steps}
                    except Exception as e:
                        step("verify_submit_error", str(e))

        except Exception as e:
            step("password_login_error", f"{url}: {e}")

    step("all_failed", "All login methods failed")
    return {"ok": False, "steps": steps, "error": "All login methods failed"}
