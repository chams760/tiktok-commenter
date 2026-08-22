import asyncio
import json
import os
import random
import subprocess
import time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

from loguru import logger
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import undetected_chromedriver as uc

import config
import database as db

_xvfb_proc = None

def _ensure_xvfb():
    global _xvfb_proc
    if _xvfb_proc and _xvfb_proc.poll() is None:
        return True
    display = os.environ.get("DISPLAY")
    if display:
        return True
    try:
        _xvfb_proc = subprocess.Popen(
            ["Xvfb", ":99", "-screen", "0", "1920x1080x24", "-nolisten", "tcp", "-ac"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        time.sleep(0.5)
        if _xvfb_proc.poll() is not None:
            logger.warning("Xvfb exited immediately, falling back to headless")
            return False
        os.environ["DISPLAY"] = ":99"
        logger.info("Xvfb started on :99")
        return True
    except FileNotFoundError:
        logger.warning("Xvfb not found, using headless mode")
        return False
    except Exception as e:
        logger.warning(f"Xvfb failed: {e}, using headless")
        return False

SCREENSHOTS_DIR = os.path.join(os.path.dirname(__file__), "screenshots")
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)

SESSIONS_DIR = os.path.join(os.path.dirname(__file__), "sessions")
os.makedirs(SESSIONS_DIR, exist_ok=True)

_executor = ThreadPoolExecutor(max_workers=3)
_active_drivers: dict[int, uc.Chrome] = {}

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

_pending_verification: dict[str, dict] = {}


def _session_path(username: str) -> str:
    safe = username.replace("@", "_at_").replace(".", "_")
    return os.path.join(SESSIONS_DIR, f"{safe}.json")


def _save_cookies(driver: uc.Chrome, username: str):
    try:
        cookies = driver.get_cookies()
        with open(_session_path(username), "w") as f:
            json.dump(cookies, f)
        logger.info(f"Cookies сохранены для {username}")
    except Exception as e:
        logger.warning(f"Не удалось сохранить cookies {username}: {e}")


def _load_cookies(driver: uc.Chrome, username: str) -> bool:
    path = _session_path(username)
    if not os.path.exists(path):
        return False
    try:
        with open(path) as f:
            cookies = json.load(f)
        driver.get("https://www.tiktok.com")
        import time; time.sleep(2)
        for c in cookies:
            c.pop("sameSite", None)
            c.pop("httpOnly", None)
            c.pop("expiry", None)
            try:
                driver.add_cookie(c)
            except Exception:
                pass
        logger.info(f"Cookies загружены для {username}")
        return True
    except Exception as e:
        logger.warning(f"Не удалось загрузить cookies {username}: {e}")
        return False


def _human_delay(min_s=0.8, max_s=2.5):
    import time
    time.sleep(random.uniform(min_s, max_s))


def _human_type(element, text):
    for char in text:
        element.send_keys(char)
        import time; time.sleep(random.uniform(0.02, 0.06))
    _human_delay(0.2, 0.5)


def _random_mouse_move(driver: uc.Chrome, steps=3):
    actions = ActionChains(driver)
    for _ in range(steps):
        x = random.randint(-200, 200)
        y = random.randint(-100, 100)
        actions.move_by_offset(x, y)
    try:
        actions.perform()
    except Exception:
        pass
    _human_delay(0.1, 0.3)


def _dismiss_cookie_banner(driver: uc.Chrome):
    try:
        xpaths = [
            '//button[contains(text(),"Decline optional")]',
            '//button[contains(text(),"Reject")]',
            '//button[contains(text(),"Allow all")]',
            '//button[contains(text(),"Accept all")]',
            '//button[contains(text(),"Accept")]',
            '//button[contains(text(),"Alle akzeptieren")]',
            '//button[contains(text(),"Tout accepter")]',
            '//button[contains(text(),"Принять")]',
            '//div[contains(@class,"cookie")]//button',
            '//div[contains(@class,"consent")]//button',
            '//div[contains(@id,"cookie")]//button',
            '//div[contains(@class,"banner")]//button[last()]',
            '//tiktok-cookie-banner//button',
        ]
        for xp in xpaths:
            try:
                btns = driver.find_elements(By.XPATH, xp)
                for btn in btns:
                    if btn.is_displayed() and btn.size['height'] > 0:
                        btn.click()
                        _human_delay(1, 2)
                        logger.debug(f"Cookie banner dismissed via: {xp}")
                        return
            except Exception:
                continue
        try:
            driver.execute_script("""
                document.querySelectorAll('[class*="cookie"], [class*="consent"], [class*="banner"], [id*="cookie"], tiktok-cookie-banner').forEach(el => el.remove());
            """)
        except Exception:
            pass
    except Exception:
        pass


_STEALTH_JS = """
// Override webdriver
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});

// Chrome runtime
window.chrome = {runtime: {}, loadTimes: function(){}, csi: function(){}, app: {isInstalled: false, getDetails: function(){}, getIsInstalled: function(){}}};

// Plugins
Object.defineProperty(navigator, 'plugins', {
    get: () => [
        {name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format'},
        {name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: ''},
        {name: 'Native Client', filename: 'internal-nacl-plugin', description: ''},
    ]
});

// Languages
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});

// Platform
Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});

// Hardware concurrency
Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});

// Device memory
Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});

// WebGL vendor/renderer
const getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(param) {
    if (param === 37445) return 'Google Inc. (NVIDIA)';
    if (param === 37446) return 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)';
    return getParameter.call(this, param);
};

// Permissions
const origQuery = window.Notification && Notification.permission;
if (window.Notification) {
    Notification.requestPermission = function() { return Promise.resolve('default'); };
}

// Iframe contentWindow
try {
    Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {
        get: function() { return window; }
    });
} catch(e) {}
"""


def _create_driver(proxy_str: str = "", force_headless: bool = False) -> uc.Chrome:
    has_display = _ensure_xvfb()
    use_headless = force_headless or not has_display

    options = uc.ChromeOptions()
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--window-size=1920,1080")
    options.add_argument(f"--user-agent={UA}")
    options.add_argument("--lang=en-US")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-infobars")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--start-maximized")
    options.add_argument("--remote-debugging-port=0")

    if proxy_str and proxy_str.strip():
        proxy = _parse_proxy_for_selenium(proxy_str)
        if proxy:
            options.add_argument(f"--proxy-server={proxy}")

    chrome_ver = None
    try:
        out = subprocess.check_output(["google-chrome-stable", "--version"], text=True)
        chrome_ver = int(out.strip().split()[-1].split(".")[0])
        logger.debug(f"Chrome version detected: {chrome_ver}")
    except Exception:
        pass

    driver = uc.Chrome(
        options=options,
        headless=use_headless,
        use_subprocess=True,
        version_main=chrome_ver,
    )
    driver.set_window_size(1920, 1080)

    # Inject stealth scripts
    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": _STEALTH_JS})
    except Exception:
        try:
            driver.execute_script(_STEALTH_JS)
        except Exception:
            pass

    logger.info(f"Chrome driver created | headless={use_headless} | proxy={bool(proxy_str)}")
    return driver


def _parse_proxy_for_selenium(proxy_str: str) -> str | None:
    if not proxy_str or not proxy_str.strip():
        return None
    p = proxy_str.strip()

    scheme = "http"
    if p.startswith("socks5://"):
        scheme = "socks5"
        p = p[len("socks5://"):]
    elif p.startswith("http://"):
        p = p[len("http://"):]
    elif p.startswith("https://"):
        p = p[len("https://"):]

    if "@" in p:
        return f"{scheme}://{p}"

    parts = p.split(":")
    if len(parts) == 4:
        user, passw, host, port = parts
        return f"{scheme}://{user}:{passw}@{host}:{port}"
    return f"{scheme}://{p}"


def parse_proxy(proxy_str: str) -> dict | None:
    if not proxy_str or not proxy_str.strip():
        return None
    p = proxy_str.strip()

    scheme = "http"
    if p.startswith("socks5://"):
        scheme = "socks5"
        p = p[len("socks5://"):]
    elif p.startswith("http://"):
        p = p[len("http://"):]
    elif p.startswith("https://"):
        p = p[len("https://"):]

    if "@" in p:
        auth, hostport = p.rsplit("@", 1)
        username, password = auth.split(":", 1) if ":" in auth else (auth, None)
        return {"server": f"{scheme}://{hostport}", "username": username, "password": password}

    parts = p.split(":")
    if len(parts) == 4:
        username, password, host, port = parts
        return {"server": f"{scheme}://{host}:{port}", "username": username, "password": password}
    if len(parts) == 2:
        return {"server": f"{scheme}://{p}"}
    return {"server": f"{scheme}://{p}"}


def get_active_page(task_id: int):
    return _active_drivers.get(task_id)


def get_all_active_tasks() -> list[int]:
    return list(_active_drivers.keys())


async def take_debug_screenshot(task_id: int | None = None) -> str | None:
    def _snap(tid):
        driver = _active_drivers.get(tid)
        if not driver:
            return None
        path = os.path.join(SCREENSHOTS_DIR, f"debug_task{tid}_{int(datetime.now(timezone.utc).timestamp())}.png")
        try:
            driver.save_screenshot(path)
            return path
        except Exception:
            return None

    if task_id is not None:
        return await asyncio.get_event_loop().run_in_executor(_executor, _snap, task_id)

    for tid in _active_drivers:
        result = await asyncio.get_event_loop().run_in_executor(_executor, _snap, tid)
        if result:
            return result
    return None


def _test_login_sync(username: str, password: str, proxy: str = "", email_password: str = "", imap_server: str = "") -> list[dict]:
    steps = []
    ts = int(datetime.now(timezone.utc).timestamp())

    def snap(driver, step_name, note=""):
        fname = f"test_{ts}_{len(steps)}_{step_name}.png"
        path = os.path.join(SCREENSHOTS_DIR, fname)
        try:
            driver.save_screenshot(path)
        except Exception:
            pass
        steps.append({"step": step_name, "file": fname, "note": note, "url": driver.current_url})

    driver = None
    try:
        driver = _create_driver(proxy)

        try:
            driver.get("https://api.ipify.org?format=json")
            _human_delay(1, 2)
            ip_text = driver.find_element(By.TAG_NAME, "body").text
            proxy_note = f"IP: {ip_text}"
            if proxy:
                proxy_note += f" | Proxy: {proxy}"
            else:
                proxy_note += " | NO PROXY"
            snap(driver, "ip_check", proxy_note)
        except Exception:
            snap(driver, "ip_check", "Could not check IP")

        has_cookies = _load_cookies(driver, username)
        if has_cookies:
            driver.get("https://www.tiktok.com/foryou")
            _human_delay(3, 5)
            if "login" not in driver.current_url.lower():
                snap(driver, "cookie_login", "LOGIN SUCCESS via saved cookies")
                return steps
            snap(driver, "cookies_expired", "Saved cookies expired, logging in fresh")

        driver.get("https://www.tiktok.com")
        _human_delay(2, 4)
        _dismiss_cookie_banner(driver)
        snap(driver, "warmup", "Visited TikTok homepage")

        driver.get("https://www.tiktok.com/login/phone-or-email/email")
        _human_delay(3, 5)
        _dismiss_cookie_banner(driver)
        _human_delay(1, 2)
        _dismiss_cookie_banner(driver)
        _random_mouse_move(driver, 3)
        snap(driver, "login_page", "Login page loaded (undetected-chromedriver)")

        try:
            email_input = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, 'input[name="username"]'))
            )
        except Exception:
            snap(driver, "no_email_field", "Email input not found")
            return steps

        _random_mouse_move(driver, 2)
        ActionChains(driver).move_to_element(email_input).click().perform()
        _human_delay(0.3, 0.6)
        _human_type(email_input, username)
        snap(driver, "email_filled", f"Email filled: {username}")

        try:
            pass_input = driver.find_element(By.CSS_SELECTOR, 'input[type="password"]')
        except Exception:
            snap(driver, "no_pass_field", "Password input not found")
            return steps

        _random_mouse_move(driver, 1)
        ActionChains(driver).move_to_element(pass_input).click().perform()
        _human_delay(0.3, 0.6)
        _human_type(pass_input, password)
        snap(driver, "password_filled", "Password filled")

        _random_mouse_move(driver, 2)
        _human_delay(0.5, 1.2)

        try:
            login_btn = driver.find_element(By.CSS_SELECTOR, 'button[data-e2e="login-button"]')
        except Exception:
            snap(driver, "no_login_btn", "Login button not found")
            return steps

        ActionChains(driver).move_to_element(login_btn).click().perform()
        _human_delay(3, 5)
        _dismiss_cookie_banner(driver)
        snap(driver, "after_click", "After clicking login button")

        _human_delay(5, 8)
        snap(driver, "final_state", f"Final URL: {driver.current_url}")

        body_text = driver.find_element(By.TAG_NAME, "body").text

        if "login" not in driver.current_url.lower() and "verify" not in body_text.lower():
            steps[-1]["note"] = "LOGIN SUCCESS - redirected away from login page"
            _save_cookies(driver, username)
        elif "verify" in body_text.lower() or "verify it" in body_text.lower():
            snap(driver, "verify_detected", "VERIFICATION page detected, clicking Email...")

            snap(driver, "verify_page_source", f"Page text: {body_text[:500]}")

            _dismiss_cookie_banner(driver)
            _human_delay(1, 2)
            snap(driver, "verify_after_cookie_dismiss", "After dismissing cookie banner")

            page_html = driver.page_source
            html_path = os.path.join(SCREENSHOTS_DIR, f"verify_{ts}_page.html")
            try:
                with open(html_path, "w", encoding="utf-8") as hf:
                    hf.write(page_html)
            except Exception:
                pass

            email_clicked = False
            result = driver.execute_script("""
                function findAndClick() {
                    // Strategy 1: find element containing masked email like "e***@" or "***@" or "@"
                    var allEls = document.querySelectorAll('div, span, p, a, button, label');
                    var candidates = [];
                    for (var i = 0; i < allEls.length; i++) {
                        var el = allEls[i];
                        if (el.offsetHeight === 0 || el.offsetWidth === 0) continue;
                        // skip cookie/consent elements
                        var parent = el.closest('[class*="cookie"], [class*="consent"], [id*="cookie"]');
                        if (parent) continue;

                        var t = (el.innerText || el.textContent || '').trim();
                        var directText = '';
                        for (var c = 0; c < el.childNodes.length; c++) {
                            if (el.childNodes[c].nodeType === 3) directText += el.childNodes[c].textContent;
                        }
                        directText = directText.trim();

                        // masked email pattern: contains @ or *** with mail context
                        if (directText.match(/\\*+.*@|@.*\\.(com|net|org|ru|mail)/i)) {
                            candidates.push({el: el, priority: 1, text: directText});
                        }
                        // "email" as standalone word
                        else if (directText.match(/^e-?mail$/i)) {
                            candidates.push({el: el, priority: 2, text: directText});
                        }
                        // contains "email" or "Email" in a short text (not a paragraph)
                        else if (directText.length < 50 && directText.match(/e-?mail/i)) {
                            candidates.push({el: el, priority: 3, text: directText});
                        }
                        // "Send code" or "Отправить код"
                        else if (directText.match(/send.*code|отправить.*код/i)) {
                            candidates.push({el: el, priority: 4, text: directText});
                        }
                    }

                    // Strategy 2: look for verification option containers (clickable cards)
                    if (candidates.length === 0) {
                        var containers = document.querySelectorAll('[class*="verify"] div, [class*="channel"] div, [class*="option"] div, [class*="method"] div');
                        for (var j = 0; j < containers.length; j++) {
                            var cel = containers[j];
                            if (cel.offsetHeight === 0) continue;
                            var ct = (cel.innerText || '').toLowerCase();
                            if (ct.includes('mail') || ct.includes('@')) {
                                candidates.push({el: cel, priority: 5, text: ct.substring(0, 50)});
                            }
                        }
                    }

                    if (candidates.length === 0) return JSON.stringify({status: 'not_found', candidates: 0});

                    // sort by priority
                    candidates.sort(function(a, b) { return a.priority - b.priority; });
                    var best = candidates[0];
                    best.el.click();
                    return JSON.stringify({status: 'clicked', text: best.text, priority: best.priority, total: candidates.length});
                }
                return findAndClick();
            """)

            try:
                result_data = json.loads(result)
                if result_data.get("status") == "clicked":
                    email_clicked = True
                    _human_delay(2, 4)
                    snap(driver, "verify_email_clicked", f"JS clicked: '{result_data.get('text', '')}' (p{result_data.get('priority')}, {result_data.get('total')} candidates)")
                else:
                    snap(driver, "verify_js_not_found", f"JS found 0 candidates")
            except Exception as e:
                snap(driver, "verify_js_error", f"JS error: {e}, result: {result}")

            if not email_clicked:
                snap(driver, "verify_email_not_found", f"Could not find Email option. Body: {body_text[:300]}")
                _pending_verification[username] = {"driver": driver, "steps": steps, "snap": snap}
                steps[-1]["note"] = "COULD_NOT_CLICK_EMAIL - check verify_*_page.html in screenshots for page structure"
                return steps

            _human_delay(3, 5)
            snap(driver, "after_email_click", f"After email click. URL: {driver.current_url}")
            new_body = driver.find_element(By.TAG_NAME, "body").text

            if new_body == body_text:
                snap(driver, "verify_click_no_change", "Page didn't change after click, trying parent element...")
                driver.execute_script("""
                    var allEls = document.querySelectorAll('div, span, p, a, button');
                    for (var i = 0; i < allEls.length; i++) {
                        var el = allEls[i];
                        if (el.offsetHeight === 0) continue;
                        var t = (el.innerText || '').toLowerCase();
                        if ((t.includes('mail') || t.includes('@')) && t.length < 100) {
                            var target = el.parentElement || el;
                            target.click();
                            return;
                        }
                    }
                """)
                _human_delay(3, 5)
                snap(driver, "verify_parent_clicked", "Tried clicking parent element")
                new_body = driver.find_element(By.TAG_NAME, "body").text

            try:
                send_btns = driver.execute_script("""
                    var btns = document.querySelectorAll('button, div[role="button"], a[role="button"], [class*="send"], [class*="Send"]');
                    var results = [];
                    for (var i = 0; i < btns.length; i++) {
                        var t = (btns[i].innerText || '').trim().toLowerCase();
                        if (t.match(/send|отправить|get.*code|получить|request/i) && btns[i].offsetHeight > 0) {
                            results.push(t);
                            btns[i].click();
                            return 'clicked: ' + t;
                        }
                    }
                    return 'no send button found. Buttons: ' + results.join(', ');
                """)
                snap(driver, "send_code_btn", f"Send code button: {send_btns}")
                _human_delay(2, 3)
            except Exception:
                pass

            _pending_verification[username] = {"driver": driver, "steps": steps, "snap": snap}
            steps[-1]["note"] = "WAITING_FOR_CODE - Enter the verification code sent to your email"
            return steps
        else:
            note = "LOGIN FAILED - still on login page."
            if "maximum" in body_text.lower() or "too many" in body_text.lower():
                note += " RATE LIMITED: Too many attempts."
            page_src = driver.page_source.lower()
            if "captcha" in page_src or "puzzle" in page_src:
                note += " CAPTCHA detected."
            steps[-1]["note"] = note

    except Exception as e:
        if driver:
            snap(driver, "exception", f"Error: {str(e)}")
        else:
            steps.append({"step": "exception", "file": "", "note": f"Driver failed: {str(e)}", "url": ""})
    finally:
        if username not in _pending_verification:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass

    return steps


def _submit_code_sync(username: str, code: str) -> list[dict]:
    pending = _pending_verification.get(username)
    if not pending:
        return [{"step": "error", "file": "", "note": "No pending verification for this account", "url": ""}]

    driver = pending["driver"]
    steps = pending["steps"]
    ts = int(datetime.now(timezone.utc).timestamp())

    def snap(driver, step_name, note=""):
        fname = f"verify_{ts}_{len(steps)}_{step_name}.png"
        path = os.path.join(SCREENSHOTS_DIR, fname)
        try:
            driver.save_screenshot(path)
        except Exception:
            pass
        steps.append({"step": step_name, "file": fname, "note": note, "url": driver.current_url})

    try:
        code_inputs = driver.find_elements(By.CSS_SELECTOR, 'input[type="text"], input[type="number"], input[type="tel"]')
        if not code_inputs:
            code_inputs = driver.find_elements(By.CSS_SELECTOR, 'input')

        if len(code_inputs) == 1:
            code_inputs[0].clear()
            _human_type(code_inputs[0], code)
        elif len(code_inputs) >= 4:
            for i, digit in enumerate(code):
                if i < len(code_inputs):
                    code_inputs[i].send_keys(digit)
                    _human_delay(0.05, 0.15)
        else:
            for inp in code_inputs:
                try:
                    inp.clear()
                    _human_type(inp, code)
                    break
                except Exception:
                    continue

        snap(driver, "code_entered", f"Verification code entered: {code}")
        _human_delay(1, 2)

        try:
            verify_btn = driver.find_element(By.XPATH, '//button[contains(text(),"Verify") or contains(text(),"Submit") or contains(text(),"Confirm")]')
            ActionChains(driver).move_to_element(verify_btn).click().perform()
        except Exception:
            from selenium.webdriver.common.keys import Keys
            code_inputs[-1].send_keys(Keys.ENTER)

        _human_delay(5, 8)
        snap(driver, "after_verify", f"After verification. URL: {driver.current_url}")

        if "login" not in driver.current_url.lower():
            steps[-1]["note"] = "LOGIN SUCCESS after verification!"
            _save_cookies(driver, username)
        else:
            body_text = driver.find_element(By.TAG_NAME, "body").text
            if "incorrect" in body_text.lower() or "invalid" in body_text.lower() or "wrong" in body_text.lower():
                steps[-1]["note"] = "WRONG CODE - try again with correct code"
                return steps
            steps[-1]["note"] = f"Still on login page after verify. Body: {body_text[:200]}"

    except Exception as e:
        snap(driver, "verify_exception", f"Error: {str(e)}")
    finally:
        _pending_verification.pop(username, None)
        try:
            driver.quit()
        except Exception:
            pass

    return steps


async def submit_verification_code(username: str, code: str) -> list[dict]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, _submit_code_sync, username, code)


async def test_login(username: str, password: str, proxy: str = "") -> list[dict]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, _test_login_sync, username, password, proxy)


_pending_browser_sessions: dict[str, dict] = {}


def _browser_fetch_login_sync(username: str, password: str, proxy: str = "",
                               email_password: str = "", imap_server: str = "") -> dict:
    """Login via real browser — click through TikTok's actual UI so their JS handles X-Bogus/captcha."""
    steps = []
    ts = int(datetime.now(timezone.utc).timestamp())

    def step(name, note=""):
        steps.append({"step": name, "note": note})
        logger.info(f"BrowserLogin [{name}]: {note}")

    def snap(driver, step_name):
        fname = f"blogin_{ts}_{len(steps)}_{step_name}.png"
        path = os.path.join(SCREENSHOTS_DIR, fname)
        try:
            driver.save_screenshot(path)
        except Exception:
            pass
        return fname

    driver = None
    try:
        try:
            driver = _create_driver(proxy)
        except Exception as e:
            step("driver_error", f"Chrome failed with xvfb: {e}. Trying headless...")
            try:
                driver = _create_driver(proxy, force_headless=True)
            except Exception as e2:
                step("driver_fatal", str(e2))
                return {"ok": False, "steps": steps, "error": f"Chrome failed: {e2}"}
        step("init", f"Browser created | proxy: {bool(proxy)}")

        # Check saved cookies first
        has_cookies = _load_cookies(driver, username)
        if has_cookies:
            driver.get("https://www.tiktok.com/foryou")
            time.sleep(3)
            if "login" not in driver.current_url.lower():
                step("cookie_login", "LOGIN SUCCESS via saved cookies!")
                driver.quit()
                return {"ok": True, "steps": steps}
            step("cookies_expired", "Saved cookies expired")

        # Step 1: Warm up — visit homepage
        driver.get("https://www.tiktok.com")
        time.sleep(3)
        _dismiss_cookie_banner(driver)
        _random_mouse_move(driver, 3)
        step("homepage", "Homepage visited")

        # Step 2: Go to login page
        driver.get("https://www.tiktok.com/login/phone-or-email/email")
        time.sleep(3)
        _dismiss_cookie_banner(driver)
        time.sleep(1)
        _dismiss_cookie_banner(driver)
        snap(driver, "login_page")
        step("login_page", "Login page loaded")

        # Step 3: Fill credentials
        try:
            email_input = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, 'input[name="username"]'))
            )
        except Exception:
            step("error", "Email input not found")
            driver.quit()
            return {"ok": False, "steps": steps, "error": "Email input not found"}

        ActionChains(driver).move_to_element(email_input).click().perform()
        _human_delay(0.2, 0.4)
        _human_type(email_input, username)
        _human_delay(0.3, 0.5)

        try:
            pass_input = driver.find_element(By.CSS_SELECTOR, 'input[type="password"]')
        except Exception:
            step("error", "Password input not found")
            driver.quit()
            return {"ok": False, "steps": steps, "error": "Password input not found"}

        ActionChains(driver).move_to_element(pass_input).click().perform()
        _human_delay(0.2, 0.4)
        _human_type(pass_input, password)
        step("credentials", "Credentials filled")

        # Step 4: Click login button
        _human_delay(0.5, 1.0)
        try:
            login_btn = driver.find_element(By.CSS_SELECTOR, 'button[data-e2e="login-button"]')
        except Exception:
            step("error", "Login button not found")
            driver.quit()
            return {"ok": False, "steps": steps, "error": "Login button not found"}

        ActionChains(driver).move_to_element(login_btn).click().perform()
        time.sleep(5)
        _dismiss_cookie_banner(driver)
        snap(driver, "after_login_click")

        body_text = driver.find_element(By.TAG_NAME, "body").text
        step("after_login", f"URL: {driver.current_url} | Body: {body_text[:200]}")

        # Check: direct login success?
        if "login" not in driver.current_url.lower() and "verify" not in body_text.lower():
            _save_cookies(driver, username)
            step("success", "Direct login success!")
            driver.quit()
            return {"ok": True, "steps": steps}

        # Check: captcha on login page?
        page_src = driver.page_source.lower()
        if "captcha" in page_src or "puzzle" in page_src:
            snap(driver, "captcha_detected")
            step("captcha", "CAPTCHA detected. Waiting 5s...")
            time.sleep(5)
            body_text = driver.find_element(By.TAG_NAME, "body").text
            if "login" not in driver.current_url.lower():
                _save_cookies(driver, username)
                step("success", "Login success after captcha!")
                driver.quit()
                return {"ok": True, "steps": steps}

        # Step 5: Handle verification page
        if "verify" not in body_text.lower() and "code" not in body_text.lower():
            snap(driver, "login_failed")
            step("failed", f"Login failed, no verify page. Body: {body_text[:300]}")
            driver.quit()
            return {"ok": False, "steps": steps, "error": "Login failed"}

        step("verify_page", "Verification page detected")
        snap(driver, "verify_page")

        # Save page HTML for debugging
        try:
            html_path = os.path.join(SCREENSHOTS_DIR, f"blogin_{ts}_verify.html")
            with open(html_path, "w", encoding="utf-8") as hf:
                hf.write(driver.page_source)
        except Exception:
            pass

        # Step 6: Click Email verification option via UI
        _dismiss_cookie_banner(driver)
        time.sleep(1)

        email_clicked = driver.execute_script("""
            function findAndClick() {
                var allEls = document.querySelectorAll('div, span, p, a, button, label');
                var candidates = [];
                for (var i = 0; i < allEls.length; i++) {
                    var el = allEls[i];
                    if (el.offsetHeight === 0 || el.offsetWidth === 0) continue;
                    var parent = el.closest('[class*="cookie"], [class*="consent"], [id*="cookie"]');
                    if (parent) continue;

                    var directText = '';
                    for (var c = 0; c < el.childNodes.length; c++) {
                        if (el.childNodes[c].nodeType === 3) directText += el.childNodes[c].textContent;
                    }
                    directText = directText.trim();

                    if (directText.match(/\\*+.*@|@.*\\.(com|net|org|ru|mail)/i)) {
                        candidates.push({el: el, priority: 1, text: directText});
                    }
                    else if (directText.match(/^e-?mail$/i)) {
                        candidates.push({el: el, priority: 2, text: directText});
                    }
                    else if (directText.length < 50 && directText.match(/e-?mail/i)) {
                        candidates.push({el: el, priority: 3, text: directText});
                    }
                }

                if (candidates.length === 0) {
                    var containers = document.querySelectorAll('[class*="verify"] div, [class*="channel"] div, [class*="option"] div');
                    for (var j = 0; j < containers.length; j++) {
                        var cel = containers[j];
                        if (cel.offsetHeight === 0) continue;
                        var ct = (cel.innerText || '').toLowerCase();
                        if (ct.includes('mail') || ct.includes('@')) {
                            candidates.push({el: cel, priority: 5, text: ct.substring(0, 50)});
                        }
                    }
                }

                if (candidates.length === 0) return 'not_found';
                candidates.sort(function(a, b) { return a.priority - b.priority; });
                var best = candidates[0];
                best.el.click();
                return 'clicked:' + best.text;
            }
            return findAndClick();
        """)

        step("email_option", f"Email click result: {email_clicked}")
        if email_clicked and email_clicked.startswith("clicked:"):
            time.sleep(2)
            snap(driver, "after_email_click")

        # Step 7: Click "Send code" button if visible
        time.sleep(1)
        send_result = driver.execute_script("""
            var btns = document.querySelectorAll('button, div[role="button"], a[role="button"], [class*="send"], [class*="Send"]');
            for (var i = 0; i < btns.length; i++) {
                var t = (btns[i].innerText || '').trim().toLowerCase();
                if (t.match(/send|отправить|get.*code|получить|request/i) && btns[i].offsetHeight > 0) {
                    btns[i].click();
                    return 'clicked:' + t;
                }
            }
            // Also try any visible link-like text
            var links = document.querySelectorAll('a, span[class*="link"], div[class*="link"]');
            for (var j = 0; j < links.length; j++) {
                var lt = (links[j].innerText || '').trim().toLowerCase();
                if (lt.match(/send|code|verify|отправить/i) && links[j].offsetHeight > 0) {
                    links[j].click();
                    return 'clicked_link:' + lt;
                }
            }
            return 'no_send_button';
        """)

        step("send_code_btn", f"Send button: {send_result}")
        snap(driver, "after_send_click")

        # Step 8: Wait a bit and check for captcha
        time.sleep(2)
        page_src = driver.page_source.lower()
        if "captcha" in page_src or "puzzle" in page_src or "slider" in page_src:
            snap(driver, "captcha_verify")
            step("captcha_verify", "CAPTCHA detected on verification. TikTok requires captcha before sending code.")
            # Try to detect and log captcha details
            captcha_info = driver.execute_script("""
                var iframes = document.querySelectorAll('iframe');
                var info = {iframes: iframes.length, classes: []};
                document.querySelectorAll('[class*="captcha"], [class*="Captcha"], [id*="captcha"], [class*="verify-bar"]').forEach(function(el) {
                    info.classes.push(el.className || el.id);
                });
                return JSON.stringify(info);
            """)
            step("captcha_info", f"Captcha details: {captcha_info}")

        # Step 9: Store session, wait for code
        time.sleep(2)
        new_body = driver.find_element(By.TAG_NAME, "body").text
        snap(driver, "waiting_for_code")
        step("waiting", f"Current page state: {new_body[:300]}")

        # Check if code input appeared (TikTok might show it)
        code_inputs = driver.find_elements(By.CSS_SELECTOR, 'input[type="text"], input[type="number"], input[type="tel"]')
        code_inputs = [inp for inp in code_inputs if inp.is_displayed() and inp.get_attribute("name") != "username"]
        if code_inputs:
            step("code_input_found", f"Found {len(code_inputs)} code input field(s). Code was likely sent!")

            # If IMAP available, try auto-read
            if email_password:
                from tiktok_api import fetch_code_from_imap, _get_imap_server
                if not imap_server:
                    imap_server = _get_imap_server(username)
                step("imap_waiting", f"Waiting for code via IMAP ({imap_server})...")
                code = fetch_code_from_imap(username, email_password, imap_server, timeout=90)
                if code:
                    step("imap_got_code", f"Got code: {code}")
                    return _browser_enter_code_ui(driver, username, code, steps)
                step("imap_no_code", "No code via IMAP. Enter manually.")

            _pending_browser_sessions[username] = {"driver": driver, "steps": steps}
            return {"ok": False, "steps": steps, "need_code": True, "username": username}

        step("no_code_input", "No code input field visible. Code may not have been sent.")
        _pending_browser_sessions[username] = {"driver": driver, "steps": steps}
        return {"ok": False, "steps": steps, "need_code": True, "username": username,
                "warning": "Code input not found — check if code was actually sent to your email"}

    except Exception as e:
        step("exception", str(e))
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
        return {"ok": False, "steps": steps, "error": str(e)}


def _browser_enter_code_ui(driver, username: str, code: str, steps: list) -> dict:
    """Enter verification code into TikTok's actual UI input fields."""

    def step(name, note=""):
        steps.append({"step": name, "note": note})
        logger.info(f"BrowserCode [{name}]: {note}")

    try:
        code_inputs = driver.find_elements(By.CSS_SELECTOR, 'input[type="text"], input[type="number"], input[type="tel"]')
        code_inputs = [inp for inp in code_inputs if inp.is_displayed() and inp.get_attribute("name") != "username"]

        if not code_inputs:
            code_inputs = driver.find_elements(By.CSS_SELECTOR, 'input')
            code_inputs = [inp for inp in code_inputs if inp.is_displayed()
                          and inp.get_attribute("type") not in ("password", "hidden")]

        if not code_inputs:
            step("no_input", "No code input field found")
            driver.quit()
            return {"ok": False, "steps": steps, "error": "No code input field found"}

        step("inputs_found", f"Found {len(code_inputs)} input fields")

        if len(code_inputs) == 1:
            code_inputs[0].clear()
            _human_type(code_inputs[0], code)
        elif len(code_inputs) >= 4:
            for i, digit in enumerate(code):
                if i < len(code_inputs):
                    code_inputs[i].send_keys(digit)
                    _human_delay(0.05, 0.15)
        else:
            code_inputs[0].clear()
            _human_type(code_inputs[0], code)

        step("code_entered", f"Code {code} entered into UI")
        _human_delay(1, 2)

        # Try clicking verify/submit button
        try:
            verify_btn = driver.execute_script("""
                var btns = document.querySelectorAll('button, div[role="button"]');
                for (var i = 0; i < btns.length; i++) {
                    var t = (btns[i].innerText || '').trim().toLowerCase();
                    if (t.match(/verify|submit|confirm|log.?in|подтвер|войти|далее|next/i) && btns[i].offsetHeight > 0) {
                        btns[i].click();
                        return 'clicked:' + t;
                    }
                }
                return 'no_button';
            """)
            step("verify_btn", f"Verify button: {verify_btn}")
        except Exception:
            from selenium.webdriver.common.keys import Keys
            code_inputs[-1].send_keys(Keys.ENTER)
            step("verify_btn", "Pressed Enter as fallback")

        _human_delay(5, 8)

        fname = f"blogin_{int(datetime.now(timezone.utc).timestamp())}_after_code.png"
        path = os.path.join(SCREENSHOTS_DIR, fname)
        try:
            driver.save_screenshot(path)
        except Exception:
            pass

        if "login" not in driver.current_url.lower():
            _save_cookies(driver, username)
            step("success", "LOGIN SUCCESS after code entry!")
            driver.quit()
            return {"ok": True, "steps": steps}

        body_text = driver.find_element(By.TAG_NAME, "body").text
        if "incorrect" in body_text.lower() or "invalid" in body_text.lower() or "wrong" in body_text.lower():
            step("wrong_code", "Wrong code entered")
            driver.quit()
            return {"ok": False, "steps": steps, "error": "Wrong verification code"}

        step("still_login", f"Still on login page. Body: {body_text[:300]}")
        driver.quit()
        return {"ok": False, "steps": steps, "error": "Verification did not complete"}

    except Exception as e:
        step("exception", str(e))
        try:
            driver.quit()
        except Exception:
            pass
        return {"ok": False, "steps": steps, "error": str(e)}


def _browser_submit_manual_code_sync(username: str, code: str) -> dict:
    pending = _pending_browser_sessions.pop(username, None)
    if not pending:
        return {"ok": False, "error": f"No pending browser session for {username}. Run login first."}

    driver = pending["driver"]
    steps = pending["steps"]

    def step(name, note=""):
        steps.append({"step": name, "note": note})
    step("manual_code", f"Code entered: {code}")

    return _browser_enter_code_ui(driver, username, code, steps)


async def browser_fetch_login(username: str, password: str, proxy: str = "",
                              email_password: str = "", imap_server: str = "") -> dict:
    loop = asyncio.get_event_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(
                _executor, _browser_fetch_login_sync, username, password, proxy, email_password, imap_server
            ),
            timeout=120
        )
    except asyncio.TimeoutError:
        return {"ok": False, "steps": [{"step": "timeout", "note": "Login timed out after 120s"}],
                "error": "Login timed out after 120 seconds"}


async def browser_submit_manual_code(username: str, code: str) -> dict:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, _browser_submit_manual_code_sync, username, code)


def _login_account_sync(driver: uc.Chrome, username: str, password: str) -> bool:
    import time

    has_cookies = _load_cookies(driver, username)
    if has_cookies:
        driver.get("https://www.tiktok.com/foryou")
        time.sleep(random.uniform(3, 5))
        if "login" not in driver.current_url.lower():
            logger.info(f"Вход через cookies: {username}")
            return True
        logger.info(f"Cookies устарели для {username}")

    driver.get("https://www.tiktok.com")
    time.sleep(random.uniform(2, 4))

    driver.get("https://www.tiktok.com/login/phone-or-email/email")
    time.sleep(random.uniform(3, 5))
    _dismiss_cookie_banner(driver)
    _random_mouse_move(driver, 3)

    try:
        email_input = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, 'input[name="username"]'))
        )
        _random_mouse_move(driver, 2)
        ActionChains(driver).move_to_element(email_input).click().perform()
        _human_delay(0.3, 0.6)
        _human_type(email_input, username)

        pass_input = driver.find_element(By.CSS_SELECTOR, 'input[type="password"]')
        _random_mouse_move(driver, 1)
        ActionChains(driver).move_to_element(pass_input).click().perform()
        _human_delay(0.3, 0.6)
        _human_type(pass_input, password)

        _random_mouse_move(driver, 2)
        _human_delay(0.5, 1.2)

        login_btn = driver.find_element(By.CSS_SELECTOR, 'button[data-e2e="login-button"]')
        ActionChains(driver).move_to_element(login_btn).click().perform()

        time.sleep(random.uniform(5, 8))

        if "login" not in driver.current_url.lower():
            logger.info(f"Вход выполнен: {username}")
            _save_cookies(driver, username)
            return True

        body_text = driver.find_element(By.TAG_NAME, "body").text
        if "maximum" in body_text.lower() or "too many" in body_text.lower():
            logger.error(f"Rate limit для {username}")
        else:
            logger.warning(f"Не удалось войти: {username}")
        return False

    except Exception as e:
        logger.error(f"Ошибка входа {username}: {e}")
        return False


def _search_videos_sync(driver: uc.Chrome, query: str, max_results: int = 50) -> list[str]:
    import time
    urls = []
    try:
        driver.get(f"https://www.tiktok.com/search/video?q={query}")
        time.sleep(4)

        scroll_count = max(max_results // 10, 3)
        for _ in range(scroll_count):
            driver.execute_script("window.scrollBy(0, window.innerHeight)")
            time.sleep(random.uniform(1.5, 3.0))

        links = driver.find_elements(By.CSS_SELECTOR, 'a[href*="/video/"]')
        seen = set()
        for link in links:
            href = link.get_attribute("href")
            if href and "/video/" in href and href not in seen:
                if not href.startswith("http"):
                    href = "https://www.tiktok.com" + href
                seen.add(href)
                urls.append(href)
                if len(urls) >= max_results:
                    break

        logger.info(f"Найдено {len(urls)} видео по запросу '{query}'")
    except Exception as e:
        logger.error(f"Ошибка поиска: {e}")
    return urls


def _post_comment_sync(driver: uc.Chrome, video_url: str, comment_text: str) -> bool:
    import time
    try:
        driver.get(video_url)
        time.sleep(random.uniform(3, 5))

        comment_box = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, 'div[data-e2e="comment-input"] div[contenteditable="true"]'))
        )
        ActionChains(driver).move_to_element(comment_box).click().perform()
        time.sleep(0.5)
        comment_box.send_keys(comment_text)
        time.sleep(1)

        post_btn = driver.find_element(By.CSS_SELECTOR, 'div[data-e2e="comment-post"]')
        ActionChains(driver).move_to_element(post_btn).click().perform()
        time.sleep(3)

        logger.info(f"Комментарий отправлен: {video_url}")
        return True
    except Exception as e:
        logger.error(f"Ошибка комментария на {video_url}: {e}")
        return False


def _run_task_sync(task_id: int, task: dict):
    import time

    probe_driver = _create_driver()
    try:
        video_urls = _search_videos_sync(probe_driver, task["search_query"], task["max_comments"])
    finally:
        try:
            probe_driver.quit()
        except Exception:
            pass

    return video_urls


async def run_task(task_id: int):
    task = await db.get_task(task_id)
    if not task:
        logger.error(f"Задача {task_id} не найдена")
        return

    await db.update_task(task_id, status="running")
    logger.info(f"Запуск задачи #{task_id}: запрос='{task['search_query']}', лимит={task['max_comments']}")

    loop = asyncio.get_event_loop()
    comments_done = task["comments_done"]
    comments_failed = task["comments_failed"]

    try:
        video_urls = await loop.run_in_executor(_executor, _run_task_sync, task_id, task)

        if not video_urls:
            logger.warning("Видео не найдены")
            await db.update_task(task_id, status="done", finished_at=datetime.now(timezone.utc).isoformat())
            return

        for video_url in video_urls:
            if comments_done >= task["max_comments"]:
                break

            task_check = await db.get_task(task_id)
            if task_check and task_check["status"] == "cancelled":
                logger.info(f"Задача #{task_id} отменена")
                return

            account = await db.get_available_account(config.MAX_COMMENTS_PER_ACCOUNT)
            if not account:
                logger.warning("Нет доступных аккаунтов")
                await db.update_task(task_id, status="paused_no_accounts")
                return

            proxy_str = account.get("proxy", "")

            def _do_comment():
                driver = _create_driver(proxy_str)
                _active_drivers[task_id] = driver
                try:
                    logged_in = _login_account_sync(driver, account["username"], account["password"])
                    if not logged_in:
                        try:
                            err_path = os.path.join(SCREENSHOTS_DIR, f"login_fail_{account['id']}_{int(datetime.now(timezone.utc).timestamp())}.png")
                            driver.save_screenshot(err_path)
                        except Exception:
                            pass
                        return False, "login_failed"

                    success = _post_comment_sync(driver, video_url, task["comment_text"])
                    if not success:
                        try:
                            err_path = os.path.join(SCREENSHOTS_DIR, f"comment_fail_{int(datetime.now(timezone.utc).timestamp())}.png")
                            driver.save_screenshot(err_path)
                        except Exception:
                            pass
                    return success, "ok" if success else "comment_failed"
                finally:
                    _active_drivers.pop(task_id, None)
                    try:
                        driver.quit()
                    except Exception:
                        pass

            success, status = await loop.run_in_executor(_executor, _do_comment)

            if status == "login_failed":
                await db.set_account_status(account["id"], "login_failed")
                await db.log_comment(task_id, account["id"], video_url, "error", "login_failed")
                comments_failed += 1
            elif success:
                comments_done += 1
                await db.increment_account_comments(account["id"])
                await db.log_comment(task_id, account["id"], video_url, "ok")
            else:
                comments_failed += 1
                await db.log_comment(task_id, account["id"], video_url, "error", "comment_failed")

            await db.update_task(task_id, comments_done=comments_done, comments_failed=comments_failed)

            delay = random.uniform(config.DELAY_MIN, config.DELAY_MAX)
            logger.info(f"Задержка {delay:.0f}с перед следующим комментарием")
            await asyncio.sleep(delay)

    except Exception as e:
        logger.error(f"Критическая ошибка задачи #{task_id}: {e}")
    finally:
        _active_drivers.pop(task_id, None)
        await db.update_task(
            task_id,
            status="done",
            comments_done=comments_done,
            comments_failed=comments_failed,
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
        logger.info(f"Задача #{task_id} завершена: отправлено={comments_done}, ошибок={comments_failed}")
