import asyncio
import json
import os
import random
import subprocess
import time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

from loguru import logger
from selenium import webdriver
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

import config
import database as db


_PERSIST_DIR = "/app/data" if os.path.isdir("/app/data") else os.path.dirname(__file__)

SCREENSHOTS_DIR = os.path.join(_PERSIST_DIR, "screenshots")
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)

SESSIONS_DIR = os.path.join(_PERSIST_DIR, "sessions")
os.makedirs(SESSIONS_DIR, exist_ok=True)

_executor = ThreadPoolExecutor(max_workers=3)
_active_drivers: dict[int, webdriver.Chrome] = {}

def _detect_chrome_version() -> str:
    try:
        out = subprocess.check_output(["google-chrome-stable", "--version"], text=True)
        ver = out.strip().split()[-1]
        return ver
    except Exception:
        return "126.0.0.0"

_CHROME_VER = _detect_chrome_version()
UA = f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{_CHROME_VER} Safari/537.36"

_pending_verification: dict[str, dict] = {}
_pending_browser_sessions: dict[str, dict] = {}


# ── SMS-Activate.org API for phone verification ──────────────────────────
SMS_ACTIVATE_API = "https://api.sms-activate.guru/stubs/handler_api.php"
SMS_TIKTOK_SERVICE = "lf"  # TikTok service code on sms-activate


def _sms_get_number(api_key: str, country: str = "0") -> dict | None:
    """Rent a phone number for TikTok verification. Returns {id, number} or None."""
    import requests
    try:
        r = requests.get(SMS_ACTIVATE_API, params={
            "api_key": api_key, "action": "getNumber",
            "service": SMS_TIKTOK_SERVICE, "country": country
        }, timeout=15)
        text = r.text.strip()
        if text.startswith("ACCESS_NUMBER"):
            parts = text.split(":")
            return {"id": parts[1], "number": parts[2]}
        logger.warning(f"SMS-activate getNumber: {text}")
        return None
    except Exception as e:
        logger.error(f"SMS-activate getNumber error: {e}")
        return None


def _sms_set_status(api_key: str, activation_id: str, status: int) -> str:
    """Set activation status: 1=ready, 6=complete, 8=cancel."""
    import requests
    try:
        r = requests.get(SMS_ACTIVATE_API, params={
            "api_key": api_key, "action": "setStatus",
            "id": activation_id, "status": status
        }, timeout=10)
        return r.text.strip()
    except Exception as e:
        return f"error:{e}"


def _sms_get_code(api_key: str, activation_id: str, max_wait: int = 120) -> str | None:
    """Poll for SMS code. Returns code string or None after timeout."""
    import requests
    start = time.time()
    while time.time() - start < max_wait:
        try:
            r = requests.get(SMS_ACTIVATE_API, params={
                "api_key": api_key, "action": "getStatus",
                "id": activation_id
            }, timeout=10)
            text = r.text.strip()
            if text.startswith("STATUS_OK"):
                return text.split(":")[1]
            if text == "STATUS_CANCEL":
                return None
        except Exception:
            pass
        time.sleep(5)
    return None


def _session_path(username: str) -> str:
    safe = username.replace("@", "_at_").replace(".", "_")
    return os.path.join(SESSIONS_DIR, f"{safe}.json")


def _save_cookies(driver: webdriver.Chrome, username: str):
    try:
        cookies = driver.get_cookies()
        with open(_session_path(username), "w") as f:
            json.dump(cookies, f)
        logger.info(f"Cookies сохранены для {username}")
    except Exception as e:
        logger.warning(f"Не удалось сохранить cookies {username}: {e}")


def _load_cookies(driver: webdriver.Chrome, username: str) -> bool:
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
    for i, char in enumerate(text):
        element.send_keys(char)
        # Variable speed: sometimes pause longer (like a real typist)
        if random.random() < 0.1:
            time.sleep(random.uniform(0.15, 0.4))
        else:
            time.sleep(random.uniform(0.04, 0.12))
    _human_delay(0.3, 0.7)


def _react_set_value(driver: webdriver.Chrome, element, text: str):
    """Set input value using React-compatible native setter + event dispatch.
    React intercepts events via its own synthetic system; plain send_keys
    may not trigger onChange handlers. This uses the native HTMLInputElement
    value setter and dispatches input/change events that React picks up."""
    driver.execute_script("""
        var el = arguments[0];
        var value = arguments[1];
        // Use native setter to bypass React's synthetic property
        var nativeSetter = Object.getOwnPropertyDescriptor(
            window.HTMLInputElement.prototype, 'value'
        ).set;
        nativeSetter.call(el, value);
        // Dispatch events React listens for
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
    """, element, text)


def _random_mouse_move(driver: webdriver.Chrome, steps=3):
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


def _dismiss_cookie_banner(driver: webdriver.Chrome):
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
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});

window.chrome = {runtime: {}, loadTimes: function(){return {}}, csi: function(){return {}}, app: {isInstalled: false, getDetails: function(){return {}}, getIsInstalled: function(){return false}, InstallState: {DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed'}, RunningState: {CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running'}}};

Object.defineProperty(navigator, 'plugins', {
    get: () => {
        var arr = [
            {name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format', length: 1},
            {name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '', length: 1},
            {name: 'Native Client', filename: 'internal-nacl-plugin', description: '', length: 1},
        ];
        arr.__proto__ = PluginArray.prototype;
        return arr;
    }
});

Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});
Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});
Object.defineProperty(navigator, 'maxTouchPoints', {get: () => 0});
Object.defineProperty(navigator, 'connection', {get: () => ({effectiveType: '4g', rtt: 50, downlink: 10, saveData: false})});

const getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(param) {
    if (param === 37445) return 'Google Inc. (NVIDIA)';
    if (param === 37446) return 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)';
    return getParameter.call(this, param);
};
try {
    const getParameter2 = WebGL2RenderingContext.prototype.getParameter;
    WebGL2RenderingContext.prototype.getParameter = function(param) {
        if (param === 37445) return 'Google Inc. (NVIDIA)';
        if (param === 37446) return 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)';
        return getParameter2.call(this, param);
    };
} catch(e) {}

const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications'
        ? Promise.resolve({state: Notification.permission})
        : originalQuery(parameters)
);

try {
    Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {
        get: function() { return window; }
    });
} catch(e) {}

// Headless detection bypass
Object.defineProperty(navigator, 'vendor', {get: () => 'Google Inc.'});
Object.defineProperty(navigator, 'appVersion', {get: () => navigator.userAgent.replace('Mozilla/', '')});

// Screen properties for headless
Object.defineProperty(screen, 'colorDepth', {get: () => 24});
Object.defineProperty(screen, 'pixelDepth', {get: () => 24});
if (!window.outerWidth) Object.defineProperty(window, 'outerWidth', {get: () => 1920});
if (!window.outerHeight) Object.defineProperty(window, 'outerHeight', {get: () => 1080});

// Prevent CDP detection via Error stack
const origError = Error;
window.Error = function(...args) {
    const err = new origError(...args);
    const stack = err.stack;
    if (stack && stack.includes('_cdp')) {
        err.stack = stack.replace(/_cdp/g, '_xxx');
    }
    return err;
};
window.Error.prototype = origError.prototype;

// Remove chromedriver-injected properties from document
for (var prop in document) {
    if (prop.match(/^\\$cdc_|^\\$wdc_/)) {
        try { delete document[prop]; } catch(e) {
            try { Object.defineProperty(document, prop, {get: () => undefined}); } catch(e2) {}
        }
    }
}

// Notification permission — headless returns "denied", real browser returns "default"
try {
    Object.defineProperty(Notification, 'permission', {get: () => 'default'});
} catch(e) {}

// window.Notification constructor must exist
if (typeof Notification === 'undefined') {
    window.Notification = function() {};
    window.Notification.permission = 'default';
    window.Notification.requestPermission = function() { return Promise.resolve('default'); };
}

// Battery API — headless lacks it
if (!navigator.getBattery) {
    navigator.getBattery = function() {
        return Promise.resolve({
            charging: true, chargingTime: 0, dischargingTime: Infinity,
            level: 0.97, addEventListener: function(){}
        });
    };
}

// Prevent detection via toString() checks
try {
    const fakeToString = Function.prototype.toString;
    Function.prototype.toString = function() {
        if (this === Function.prototype.toString) return 'function toString() { [native code] }';
        if (this === navigator.permissions.query) return 'function query() { [native code] }';
        return fakeToString.call(this);
    };
} catch(e) {}

// Media devices — headless may have empty list
if (navigator.mediaDevices && !navigator.mediaDevices._patched) {
    const origEnum = navigator.mediaDevices.enumerateDevices;
    navigator.mediaDevices.enumerateDevices = function() {
        return origEnum.call(this).then(function(devices) {
            if (devices.length === 0) {
                return [
                    {deviceId: 'default', kind: 'audioinput', label: '', groupId: 'default'},
                    {deviceId: 'default', kind: 'videoinput', label: '', groupId: 'default'},
                    {deviceId: 'default', kind: 'audiooutput', label: '', groupId: 'default'}
                ];
            }
            return devices;
        });
    };
    navigator.mediaDevices._patched = true;
}
"""


def _try_solve_captcha(driver, steps, step_fn, snap_fn, ts, max_attempts=3):
    """Try to solve TikTok slide/puzzle captcha using OpenCV template matching."""
    try:
        import cv2
        import numpy as np
        import base64
    except ImportError:
        step_fn("captcha_no_cv2", "OpenCV not installed, cannot auto-solve")
        return False

    for attempt in range(max_attempts):
        try:
            # Find captcha images via JS
            captcha_data = driver.execute_script("""
                var result = {bg: null, piece: null, sliderBtn: null, track: null};

                // Method 1: Find images by class/id containing captcha keywords
                var imgs = document.querySelectorAll('img');
                for (var i = 0; i < imgs.length; i++) {
                    var img = imgs[i];
                    if (img.offsetHeight === 0) continue;
                    var src = img.src || '';
                    var cls = (img.className || '').toString().toLowerCase();
                    var id = (img.id || '').toLowerCase();
                    var w = img.offsetWidth, h = img.offsetHeight;

                    // Background image (larger)
                    if (w > 200 && h > 100) {
                        if (!result.bg || w > result.bg.w) {
                            result.bg = {src: src, w: w, h: h, cls: cls};
                        }
                    }
                    // Puzzle piece (smaller, roughly square)
                    if (w > 30 && w < 120 && h > 30 && h < 120) {
                        result.piece = {src: src, w: w, h: h, cls: cls};
                    }
                }

                // Method 2: Check canvas elements
                var canvases = document.querySelectorAll('canvas');
                for (var j = 0; j < canvases.length; j++) {
                    var c = canvases[j];
                    if (c.offsetHeight === 0) continue;
                    try {
                        var dataUrl = c.toDataURL('image/png');
                        if (c.offsetWidth > 200) {
                            result.bg = {src: dataUrl, w: c.offsetWidth, h: c.offsetHeight, cls: 'canvas'};
                        } else if (c.offsetWidth > 30 && c.offsetWidth < 120) {
                            result.piece = {src: dataUrl, w: c.offsetWidth, h: c.offsetHeight, cls: 'canvas'};
                        }
                    } catch(e) {}
                }

                // Method 3: Background images via CSS
                if (!result.bg) {
                    var divs = document.querySelectorAll('[class*="captcha"] div, [class*="verify"] div, [class*="puzzle"] div');
                    for (var k = 0; k < divs.length; k++) {
                        var d = divs[k];
                        if (d.offsetHeight < 50) continue;
                        var style = window.getComputedStyle(d);
                        var bgImg = style.backgroundImage;
                        if (bgImg && bgImg !== 'none' && bgImg.startsWith('url(')) {
                            var url = bgImg.slice(5, -2);
                            if (d.offsetWidth > 200) {
                                result.bg = {src: url, w: d.offsetWidth, h: d.offsetHeight, cls: 'bg-div'};
                            }
                        }
                    }
                }

                // Find slider button/handle
                var sliderSels = [
                    '[class*="slider"] [class*="btn"]', '[class*="slider"] [class*="handle"]',
                    '[class*="slide"] [class*="btn"]', '[class*="drag"]',
                    '[class*="seam-slider-btn"]', '[class*="captcha_verify_slide"]',
                    '[class*="slider-btn"]', '[class*="SliderBtn"]',
                ];
                for (var m = 0; m < sliderSels.length; m++) {
                    var btn = document.querySelector(sliderSels[m]);
                    if (btn && btn.offsetHeight > 0) {
                        result.sliderBtn = {cls: (btn.className || '').toString().substring(0, 60), w: btn.offsetWidth, h: btn.offsetHeight};
                        break;
                    }
                }
                // Fallback: any small draggable-looking element inside captcha area
                if (!result.sliderBtn) {
                    var captchaArea = document.querySelector('[class*="captcha"], [class*="verify"], [id*="captcha"]');
                    if (captchaArea) {
                        var smallEls = captchaArea.querySelectorAll('div, span, img');
                        for (var n = 0; n < smallEls.length; n++) {
                            var se = smallEls[n];
                            if (se.offsetWidth > 20 && se.offsetWidth < 80 && se.offsetHeight > 20 && se.offsetHeight < 80) {
                                var seStyle = window.getComputedStyle(se);
                                if (seStyle.cursor === 'pointer' || seStyle.cursor === 'grab' || seStyle.cursor === 'move') {
                                    result.sliderBtn = {cls: (se.className || '').toString().substring(0, 60), w: se.offsetWidth, h: se.offsetHeight};
                                    break;
                                }
                            }
                        }
                    }
                }

                // Find slider track
                var trackSels = ['[class*="slider-track"]', '[class*="slide-track"]', '[class*="captcha_verify_bar"]', '[class*="slider_bar"]'];
                for (var p = 0; p < trackSels.length; p++) {
                    var tr = document.querySelector(trackSels[p]);
                    if (tr && tr.offsetHeight > 0) {
                        result.track = {w: tr.offsetWidth, h: tr.offsetHeight, left: tr.getBoundingClientRect().left};
                        break;
                    }
                }

                return JSON.stringify(result);
            """)

            step_fn("captcha_scan", f"Attempt {attempt+1}: {captcha_data}")
            data = json.loads(captcha_data)

            if not data.get("bg") or not data.get("sliderBtn"):
                # No standard captcha elements found, try screenshot-based approach
                step_fn("captcha_screenshot", "No captcha elements found, trying full-screenshot method")
                screenshot_path = os.path.join(SCREENSHOTS_DIR, f"captcha_full_{ts}_{attempt}.png")
                driver.save_screenshot(screenshot_path)

                # Try to find and click a simple verify button instead
                clicked = driver.execute_script("""
                    var els = document.querySelectorAll('button, div[role="button"], [class*="verify-btn"]');
                    for (var i = 0; i < els.length; i++) {
                        var t = (els[i].innerText || '').trim().toLowerCase();
                        if ((t.includes('verify') || t.includes('click') || t.includes('drag')) && els[i].offsetHeight > 0) {
                            els[i].click();
                            return 'clicked:' + t;
                        }
                    }
                    return 'none';
                """)
                step_fn("captcha_btn_click", f"Verify button: {clicked}")
                time.sleep(3)
                # Check if captcha disappeared
                page_src_check = driver.page_source.lower()
                if "captcha" not in page_src_check and "puzzle" not in page_src_check:
                    return True
                continue

            # Get background image and find gap position
            bg_src = data["bg"]["src"]
            track_width = data.get("track", {}).get("w", data["bg"]["w"])

            # Take screenshot of captcha area for analysis
            screenshot_path = os.path.join(SCREENSHOTS_DIR, f"captcha_solve_{ts}_{attempt}.png")
            driver.save_screenshot(screenshot_path)
            screenshot = cv2.imread(screenshot_path)

            if screenshot is None:
                step_fn("captcha_no_screenshot", "Failed to read screenshot")
                continue

            # Convert to grayscale and find the gap using edge detection
            gray = cv2.cvtColor(screenshot, cv2.COLOR_BGR2GRAY)
            edges = cv2.Canny(gray, 100, 200)

            # Find the slider button element for drag
            slider_btn = None
            for sel in [
                '[class*="slider"] [class*="btn"]', '[class*="slider"] [class*="handle"]',
                '[class*="slide"] [class*="btn"]', '[class*="drag"]',
                '[class*="seam-slider-btn"]', '[class*="captcha_verify_slide"]',
                '[class*="slider-btn"]', '[class*="SliderBtn"]',
            ]:
                try:
                    slider_btn = driver.find_element(By.CSS_SELECTOR, sel)
                    if slider_btn and slider_btn.is_displayed():
                        break
                    slider_btn = None
                except Exception:
                    continue

            if not slider_btn:
                # Try to find by any element matching captcha slider patterns
                try:
                    slider_btn = driver.execute_script("""
                        var captchaArea = document.querySelector('[class*="captcha"], [class*="verify"], [id*="captcha"]');
                        if (!captchaArea) return null;
                        var els = captchaArea.querySelectorAll('div, span, img, button');
                        for (var i = 0; i < els.length; i++) {
                            var el = els[i];
                            if (el.offsetWidth > 20 && el.offsetWidth < 80 && el.offsetHeight > 20 && el.offsetHeight < 80) {
                                return el;
                            }
                        }
                        return null;
                    """)
                except Exception:
                    pass

            if not slider_btn:
                step_fn("captcha_no_slider", "Could not find slider button element")
                continue

            # Calculate slide distance based on image analysis
            # Use template matching if we have both images, otherwise estimate
            slide_distance = int(track_width * random.uniform(0.3, 0.7))

            # Try to detect gap position from screenshot using contour analysis
            try:
                # Look for the puzzle gap region in the captcha area
                h, w = gray.shape
                # Captcha is usually in the center-upper area
                roi = edges[int(h*0.2):int(h*0.6), int(w*0.2):int(w*0.8)]
                contours, _ = cv2.findContours(roi, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                # Find the largest rectangular contour (the gap)
                best_x = None
                for cnt in contours:
                    x, y, cw, ch = cv2.boundingRect(cnt)
                    if 20 < cw < 100 and 20 < ch < 100:
                        actual_x = x + int(w * 0.2)
                        if best_x is None or cw * ch > best_x[2]:
                            best_x = (actual_x, cw * ch, cw)
                if best_x:
                    # Map screenshot x to track distance
                    bg_elem_w = data["bg"]["w"]
                    ratio = best_x[0] / w
                    slide_distance = int(bg_elem_w * ratio)
                    step_fn("captcha_gap_found", f"Gap detected at x={best_x[0]}, slide={slide_distance}px")
            except Exception as e:
                step_fn("captcha_gap_error", f"Gap detection failed: {e}, using estimate={slide_distance}")

            # Perform human-like drag
            actions = ActionChains(driver)
            actions.click_and_hold(slider_btn)
            actions.pause(random.uniform(0.1, 0.3))

            # Move in steps with slight random y offset (human-like)
            total = 0
            while total < slide_distance:
                chunk = random.randint(5, 25)
                if total + chunk > slide_distance:
                    chunk = slide_distance - total
                y_offset = random.randint(-2, 2)
                actions.move_by_offset(chunk, y_offset)
                actions.pause(random.uniform(0.01, 0.05))
                total += chunk

            # Small overshoot then correction (human-like)
            actions.move_by_offset(random.randint(2, 8), 0)
            actions.pause(random.uniform(0.1, 0.2))
            actions.move_by_offset(random.randint(-8, -2), 0)
            actions.pause(random.uniform(0.2, 0.5))
            actions.release()

            try:
                actions.perform()
                step_fn("captcha_dragged", f"Slider dragged {slide_distance}px")
            except Exception as e:
                step_fn("captcha_drag_error", f"Drag failed: {e}")
                continue

            time.sleep(2)
            snap_fn(driver, f"captcha_after_drag_{attempt}")

            # Check if captcha is gone
            page_src_check = driver.page_source.lower()
            if "captcha" not in page_src_check and "puzzle" not in page_src_check and "slider" not in page_src_check:
                return True

            step_fn("captcha_retry", f"Captcha still present after attempt {attempt+1}")
            time.sleep(2)

        except Exception as e:
            step_fn("captcha_error", f"Attempt {attempt+1} error: {e}")
            time.sleep(1)

    return False


def _make_proxy_auth_extension_dir(host, port, username, password):
    import tempfile
    ext_dir = tempfile.mkdtemp(prefix="proxy_ext_")
    manifest = {
        "version": "1.0.0",
        "manifest_version": 2,
        "name": "Proxy Auth Helper",
        "permissions": ["proxy", "<all_urls>", "webRequest", "webRequestBlocking"],
        "background": {"scripts": ["background.js"]},
        "minimum_chrome_version": "22.0.0"
    }
    with open(os.path.join(ext_dir, "manifest.json"), "w") as f:
        json.dump(manifest, f)
    background = f"""
    var config = {{
        mode: "fixed_servers",
        rules: {{
            singleProxy: {{scheme: "http", host: "{host}", port: parseInt({port})}},
            bypassList: ["localhost"]
        }}
    }};
    chrome.proxy.settings.set({{value: config, scope: "regular"}}, function(){{}});
    chrome.webRequest.onAuthRequired.addListener(
        function(details) {{
            return {{authCredentials: {{username: "{username}", password: "{password}"}}}};
        }},
        {{urls: ["<all_urls>"]}},
        ["blocking"]
    );
    """
    with open(os.path.join(ext_dir, "background.js"), "w") as f:
        f.write(background)
    return ext_dir


def _make_stealth_extension_dir():
    """Create a Chrome extension that injects stealth JS via content script.
    This avoids CDP Page.addScriptToEvaluateOnNewDocument which TikTok detects."""
    import tempfile
    ext_dir = tempfile.mkdtemp(prefix="stealth_ext_")
    manifest = {
        "version": "1.0.0",
        "manifest_version": 2,
        "name": "Content Helper",
        "content_scripts": [{
            "matches": ["<all_urls>"],
            "js": ["stealth.js"],
            "run_at": "document_start",
            "all_frames": True
        }],
        "minimum_chrome_version": "22.0.0"
    }
    with open(os.path.join(ext_dir, "manifest.json"), "w") as f:
        json.dump(manifest, f)
    with open(os.path.join(ext_dir, "stealth.js"), "w") as f:
        f.write(_STEALTH_JS)
    return ext_dir


_xvfb_proc = None

def _ensure_xvfb():
    """Start Xvfb virtual display if not running (replaces --headless)."""
    global _xvfb_proc
    if _xvfb_proc and _xvfb_proc.poll() is None:
        return
    display_num = random.randint(10, 99)
    _w = random.choice([1366, 1440, 1536, 1680, 1920])
    _h = random.choice([768, 900, 864, 1050, 1080])
    os.environ["DISPLAY"] = f":{display_num}"
    try:
        _xvfb_proc = subprocess.Popen(
            ["Xvfb", f":{display_num}", "-screen", "0", f"{_w}x{_h}x24", "-ac", "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        time.sleep(1)
        logger.info(f"Xvfb started on :{display_num} ({_w}x{_h})")
    except FileNotFoundError:
        logger.warning("Xvfb not found, falling back to --headless=new")
        os.environ.pop("DISPLAY", None)


def _create_driver(proxy_str: str = "") -> webdriver.Chrome:
    _ensure_xvfb()
    has_display = "DISPLAY" in os.environ

    options = ChromeOptions()
    if not has_display:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--disable-background-networking")
    options.add_argument("--disable-default-apps")
    options.add_argument("--disable-sync")
    options.add_argument("--disable-translate")
    options.add_argument("--no-first-run")
    options.add_argument("--disable-backgrounding-occluded-windows")
    options.add_argument("--disable-renderer-backgrounding")
    options.add_argument("--js-flags=--max-old-space-size=384")
    _w = random.choice([1366, 1440, 1536])
    _h = random.choice([768, 900, 864])
    options.add_argument(f"--window-size={_w},{_h}")
    options.add_argument(f"--user-agent={UA}")
    options.add_argument("--lang=en-US")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-infobars")
    options.add_argument("--disable-popup-blocking")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    # Load stealth as extension (avoids detectable CDP calls)
    extensions = []
    stealth_ext = _make_stealth_extension_dir()
    extensions.append(stealth_ext)

    if proxy_str and proxy_str.strip():
        parsed = _parse_proxy_parts(proxy_str)
        if parsed:
            host, port, user, passw = parsed
            if user and passw:
                proxy_ext = _make_proxy_auth_extension_dir(host, port, user, passw)
                extensions.append(proxy_ext)
            else:
                options.add_argument(f"--proxy-server=http://{host}:{port}")

    if extensions:
        options.add_argument(f"--load-extension={','.join(extensions)}")

    chrome_binary = None
    for path in ["/usr/bin/google-chrome-stable", "/usr/bin/google-chrome", "/usr/bin/chromium"]:
        if os.path.exists(path):
            chrome_binary = path
            break
    if chrome_binary:
        options.binary_location = chrome_binary

    options.page_load_strategy = 'eager'

    service = Service("/usr/local/bin/chromedriver")
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(45)
    driver.set_window_size(_w, _h)

    # NO CDP stealth injection — all stealth is via extension now
    # Only apply selenium-stealth (it uses CDP but adds minimal footprint)
    try:
        from selenium_stealth import stealth
        stealth(driver,
            languages=["en-US", "en"],
            vendor="Google Inc.",
            platform="Win32",
            webgl_vendor="Intel Inc.",
            renderer="Intel Iris OpenGL Engine",
            fix_hairline=True,
        )
    except Exception as e:
        logger.debug(f"selenium-stealth failed: {e}")

    logger.info(f"Chrome driver created | proxy={bool(proxy_str)} | xvfb={has_display} | stealth=ext")
    return driver


def _parse_proxy_parts(proxy_str: str):
    if not proxy_str or not proxy_str.strip():
        return None
    p = proxy_str.strip()
    for prefix in ("socks5://", "http://", "https://"):
        if p.startswith(prefix):
            p = p[len(prefix):]
            break
    if "@" in p:
        auth, hostport = p.rsplit("@", 1)
        user, passw = auth.split(":", 1) if ":" in auth else (auth, "")
        parts = hostport.split(":")
        host = parts[0]
        port = parts[1] if len(parts) > 1 else "80"
        return host, port, user, passw
    parts = p.split(":")
    if len(parts) == 4:
        user, passw, host, port = parts
        return host, port, user, passw
    if len(parts) == 2:
        return parts[0], parts[1], "", ""
    return p, "80", "", ""


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

        try:
            ActionChains(driver).move_to_element(login_btn).click().perform()
        except Exception:
            driver.execute_script("arguments[0].click()", login_btn)
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
            body_lower_check = body_text.lower()
            if any(p in body_lower_check for p in ["maximum number", "too many attempts", "try again later"]):
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



def _detect_imap_server(email: str) -> str:
    domain = email.split("@")[-1].lower()
    imap_map = {
        "gmail.com": "imap.gmail.com",
        "outlook.com": "imap-mail.outlook.com",
        "hotmail.com": "imap-mail.outlook.com",
        "live.com": "imap-mail.outlook.com",
        "yahoo.com": "imap.mail.yahoo.com",
        "mail.ru": "imap.mail.ru",
        "yandex.ru": "imap.yandex.ru",
        "ya.ru": "imap.yandex.ru",
        "icloud.com": "imap.mail.me.com",
    }
    return imap_map.get(domain, f"imap.{domain}")


def _read_tiktok_code_from_imap(email_addr: str, email_pass: str, imap_server: str = "",
                                 max_wait: int = 60) -> str | None:
    """Read TikTok verification code from email via IMAP."""
    import imaplib
    import email as email_lib
    from email.header import decode_header
    import re

    if not imap_server:
        imap_server = _detect_imap_server(email_addr)

    logger.info(f"IMAP: connecting to {imap_server} for {email_addr}")

    start_time = time.time()
    while time.time() - start_time < max_wait:
        try:
            mail = imaplib.IMAP4_SSL(imap_server, 993)
            mail.login(email_addr, email_pass)
            mail.select("INBOX")

            # Search for recent TikTok emails
            _, msg_ids = mail.search(None, '(FROM "tiktok" UNSEEN)')
            if not msg_ids[0]:
                # Try broader search
                _, msg_ids = mail.search(None, '(OR (FROM "tiktok") (SUBJECT "verification"))')

            ids = msg_ids[0].split()
            if ids:
                # Get the most recent email
                for msg_id in reversed(ids[-5:]):
                    _, msg_data = mail.fetch(msg_id, "(RFC822)")
                    raw = msg_data[0][1]
                    msg = email_lib.message_from_bytes(raw)

                    body = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            if part.get_content_type() == "text/plain":
                                body = part.get_payload(decode=True).decode(errors="ignore")
                                break
                            elif part.get_content_type() == "text/html":
                                body = part.get_payload(decode=True).decode(errors="ignore")
                    else:
                        body = msg.get_payload(decode=True).decode(errors="ignore")

                    # Look for 4-6 digit code
                    codes = re.findall(r'\b(\d{4,6})\b', body)
                    if codes:
                        mail.logout()
                        return codes[0]

            mail.logout()
        except Exception as e:
            logger.warning(f"IMAP error: {e}")

        if time.time() - start_time < max_wait:
            time.sleep(10)

    return None


def _enter_verification_code(driver, code: str) -> bool:
    """Enter verification code into TikTok's code input fields."""
    try:
        # Find code input fields
        entered = driver.execute_script("""
            var code = arguments[0];
            var inputs = document.querySelectorAll('input');
            var codeInputs = [];
            for (var i = 0; i < inputs.length; i++) {
                var inp = inputs[i];
                if (inp.offsetHeight === 0) continue;
                var ph = (inp.placeholder || '').toLowerCase();
                var name = (inp.name || '').toLowerCase();
                if (ph.includes('code') || ph.includes('verify') || name.includes('code')
                    || inp.type === 'tel' || inp.type === 'number'
                    || (inp.maxLength > 0 && inp.maxLength <= 6)) {
                    codeInputs.push(inp);
                }
            }

            if (codeInputs.length === 0) {
                // Try finding any visible text input that appeared recently
                for (var j = 0; j < inputs.length; j++) {
                    if (inputs[j].offsetHeight > 0 && inputs[j].type !== 'hidden'
                        && inputs[j].type !== 'password' && inputs[j].name !== 'username'
                        && inputs[j].name !== 'q') {
                        codeInputs.push(inputs[j]);
                    }
                }
            }

            if (codeInputs.length === 1) {
                // Single input for full code
                var nativeSetter = Object.getOwnPropertyDescriptor(
                    HTMLInputElement.prototype, 'value').set;
                nativeSetter.call(codeInputs[0], code);
                codeInputs[0].dispatchEvent(new Event('input', {bubbles: true}));
                codeInputs[0].dispatchEvent(new Event('change', {bubbles: true}));
                return 'entered_single';
            } else if (codeInputs.length >= 4) {
                // Multiple inputs (one digit each)
                for (var k = 0; k < Math.min(code.length, codeInputs.length); k++) {
                    var nativeSetter2 = Object.getOwnPropertyDescriptor(
                        HTMLInputElement.prototype, 'value').set;
                    nativeSetter2.call(codeInputs[k], code[k]);
                    codeInputs[k].dispatchEvent(new Event('input', {bubbles: true}));
                    codeInputs[k].dispatchEvent(new Event('change', {bubbles: true}));
                }
                return 'entered_multi_' + codeInputs.length;
            }
            return 'no_inputs_found';
        """, code)

        logger.info(f"Code entry result: {entered}")

        # Also try typing via send_keys as backup
        if entered and entered.startswith("no_inputs"):
            inputs = driver.find_elements(By.CSS_SELECTOR, 'input')
            for inp in inputs:
                if inp.is_displayed() and inp.get_attribute("type") not in ("hidden", "password"):
                    name = (inp.get_attribute("name") or "").lower()
                    if name not in ("username", "q"):
                        inp.clear()
                        _human_type(inp, code)
                        return True
            return False

        # Click verify/submit button
        time.sleep(1)
        driver.execute_script("""
            var btns = document.querySelectorAll('button');
            for (var i = 0; i < btns.length; i++) {
                var t = (btns[i].innerText || '').trim().toLowerCase();
                if ((t === 'verify' || t === 'submit' || t === 'confirm' || t === 'next'
                     || t === 'continue' || t === 'подтвердить')
                    && btns[i].offsetHeight > 0 && !btns[i].disabled) {
                    btns[i].click();
                    return true;
                }
            }
            return false;
        """)

        return entered and not entered.startswith("no_inputs")
    except Exception as e:
        logger.error(f"Code entry error: {e}")
        return False


def _browser_fetch_login_sync(username: str, password: str, proxy: str = "",
                               email_password: str = "", imap_server: str = "",
                               sms_api_key: str = "") -> dict:
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
            step("driver_error", str(e))
            return {"ok": False, "steps": steps, "error": f"Chrome failed: {e}"}
        has_xvfb = "DISPLAY" in os.environ
        step("init", f"Browser created | proxy: {bool(proxy)} | xvfb: {has_xvfb}")

        # Check saved cookies first
        has_cookies = _load_cookies(driver, username)
        if has_cookies:
            driver.get("https://www.tiktok.com/foryou")
            time.sleep(5)
            body_text = ""
            try:
                body_text = driver.find_element(By.TAG_NAME, "body").text
            except Exception:
                pass
            page_src_len = len(driver.page_source or "")
            is_logged_in = (
                "login" not in driver.current_url.lower()
                and page_src_len > 1000
                and ("following" in body_text.lower() or "for you" in body_text.lower()
                     or "foryou" in driver.current_url.lower())
            )
            if is_logged_in:
                step("cookie_login", f"LOGIN SUCCESS via saved cookies! URL: {driver.current_url}")
                driver.quit()
                return {"ok": True, "steps": steps}
            step("cookies_expired", f"Cookies invalid. URL: {driver.current_url} | body: {len(body_text)} | src: {page_src_len}")
            # Delete invalid cookies file
            try:
                os.remove(_session_path(username))
            except Exception:
                pass

        # Step 0: Verify proxy connectivity
        if proxy:
            try:
                driver.get("https://httpbin.org/ip")
                time.sleep(5)
                ip_text = driver.find_element(By.TAG_NAME, "body").text.strip()
                page_len = len(driver.page_source or "")
                if not ip_text or page_len < 50:
                    snap(driver, "proxy_fail")
                    step("proxy_fail", f"Proxy returned empty page (src={page_len}b). Auth may have failed.")
                    driver.quit()
                    return {"ok": False, "steps": steps, "error": "Proxy auth failed — empty response. Check proxy credentials."}
                step("proxy_check", f"Proxy OK. Response: {ip_text[:100]}")
            except Exception as e:
                snap(driver, "proxy_fail")
                step("proxy_fail", f"Proxy connection failed: {e}")
                driver.quit()
                return {"ok": False, "steps": steps, "error": f"Proxy failed: {e}"}

        # Step 1: Warm up — browse like a real user before login
        for attempt in range(2):
            try:
                driver.get("https://www.tiktok.com")
                time.sleep(random.uniform(3, 5))
                break
            except Exception as e:
                if attempt == 0:
                    step("homepage_retry", f"Homepage load failed ({type(e).__name__}), retrying with new browser...")
                    try:
                        driver.quit()
                    except Exception:
                        pass
                    time.sleep(2)
                    driver = _create_driver(proxy)
                else:
                    step("homepage_fail", f"Homepage failed twice: {e}")
                    try:
                        driver.quit()
                    except Exception:
                        pass
                    return {"ok": False, "steps": steps, "error": f"Chrome crashed: {e}"}
        _dismiss_cookie_banner(driver)

        # Brief warmup: scroll like a real user (5-8s)
        _random_mouse_move(driver, random.randint(2, 4))
        driver.execute_script("window.scrollBy(0, arguments[0])", random.randint(200, 500))
        time.sleep(random.uniform(1.5, 3.0))
        driver.execute_script("window.scrollBy(0, arguments[0])", random.randint(-100, 300))
        _random_mouse_move(driver, random.randint(1, 3))
        time.sleep(random.uniform(2.0, 4.0))
        driver.execute_script("window.scrollTo(0, 0)")
        time.sleep(random.uniform(1.0, 2.0))

        step("homepage", f"Homepage visited. Src: {len(driver.page_source)}b")

        # Step 2: Navigate to login via UI flow (not direct URL — TikTok blocks direct)
        # First try clicking Log in on homepage
        login_clicked = False
        try:
            login_link = driver.execute_script("""
                var els = document.querySelectorAll('a, button, div[role="button"]');
                for (var i = 0; i < els.length; i++) {
                    var t = (els[i].innerText || '').trim().toLowerCase();
                    if ((t === 'log in' || t === 'login' || t === 'войти' || t === 'sign in') && els[i].offsetHeight > 0) {
                        els[i].click();
                        return 'clicked:' + t;
                    }
                }
                // Try header login button
                var headerBtns = document.querySelectorAll('[data-e2e="top-login-button"], [class*="login"] button, header button');
                for (var j = 0; j < headerBtns.length; j++) {
                    if (headerBtns[j].offsetHeight > 0) {
                        headerBtns[j].click();
                        return 'clicked_header:' + (headerBtns[j].innerText || '').trim();
                    }
                }
                return 'not_found';
            """)
            step("login_click", f"Homepage login: {login_link}")
            if login_link and login_link.startswith("clicked"):
                login_clicked = True
                time.sleep(4)
                _dismiss_cookie_banner(driver)
        except Exception as e:
            step("login_click_fail", str(e))

        if not login_clicked:
            driver.get("https://www.tiktok.com/login")
            time.sleep(5)
            _dismiss_cookie_banner(driver)

        snap(driver, "login_modal")
        step("login_modal", f"Login modal/page. URL: {driver.current_url} | Src: {len(driver.page_source)}b")

        # Human pause: look at modal before clicking
        _random_mouse_move(driver, random.randint(1, 3))
        time.sleep(random.uniform(2.0, 4.0))

        # Now find and click "Email / Username" or "Phone or email" option
        email_option_clicked = False
        try:
            result = driver.execute_script("""
                var links = document.querySelectorAll('a, div[role="link"], div[role="button"], button, p, span');
                for (var i = 0; i < links.length; i++) {
                    var el = links[i];
                    if (el.offsetHeight === 0) continue;
                    var t = (el.innerText || el.textContent || '').trim().toLowerCase();
                    if (t.match(/email.*username|phone.*email|use.*email|email.*phone|email/i) && t.length < 60) {
                        el.click();
                        return 'clicked:' + t;
                    }
                }
                // Try specific TikTok selectors
                var specific = document.querySelectorAll('[data-e2e="channel-item"], [class*="channel"] a, [class*="login-option"]');
                for (var j = 0; j < specific.length; j++) {
                    var sel = specific[j];
                    if (sel.offsetHeight === 0) continue;
                    var st = (sel.innerText || '').toLowerCase();
                    if (st.includes('email') || st.includes('phone')) {
                        sel.click();
                        return 'clicked_specific:' + st.substring(0, 50);
                    }
                }
                return 'not_found';
            """)
            step("email_option", f"Email option: {result}")
            if result and result.startswith("clicked"):
                email_option_clicked = True
                time.sleep(random.uniform(2.5, 4.5))
                _dismiss_cookie_banner(driver)
        except Exception as e:
            step("email_option_fail", str(e))

        # If UI click failed, try direct URL as fallback
        if not email_option_clicked:
            step("fallback_direct", "UI click failed, trying direct URL as fallback")
            driver.get("https://www.tiktok.com/login/phone-or-email/email")
            time.sleep(5)
            _dismiss_cookie_banner(driver)

        # Switch to "Email / Username" tab (default is Phone tab)
        _random_mouse_move(driver, random.randint(1, 2))
        time.sleep(random.uniform(1.5, 3.0))
        tab_result = "not_attempted"
        try:
            tab_result = driver.execute_script("""
                // Helper: get ONLY this element's own text (not children's text)
                function directText(el) {
                    var text = '';
                    for (var i = 0; i < el.childNodes.length; i++) {
                        if (el.childNodes[i].nodeType === 3) text += el.childNodes[i].textContent;
                    }
                    return text.trim();
                }

                // Collect all visible link-like elements
                var allEls = document.querySelectorAll('a, span, p, div[role="tab"], [class*="tab"] > *, label, li');
                var candidates = [];
                for (var i = 0; i < allEls.length; i++) {
                    var el = allEls[i];
                    if (el.offsetHeight === 0 || el.offsetWidth === 0) continue;
                    var dt = directText(el).toLowerCase();
                    var full = (el.innerText || '').trim().toLowerCase();
                    // Use direct text if available, fall back to full innerText for leaf nodes
                    var text = dt || full;
                    if (!text) continue;
                    candidates.push({el: el, text: text, full: full, tag: el.tagName});
                }

                // Priority 1: exact match on direct text
                var exactMatches = ['email / username', 'email/username', 'username or email',
                                    'email or username', 'username', 'log in with email or username'];
                for (var j = 0; j < candidates.length; j++) {
                    for (var k = 0; k < exactMatches.length; k++) {
                        if (candidates[j].text === exactMatches[k]) {
                            candidates[j].el.click();
                            return 'clicked_exact:' + candidates[j].text;
                        }
                    }
                }

                // Priority 2: direct text contains "username" but NOT "phone" (to avoid parent containers)
                for (var m = 0; m < candidates.length; m++) {
                    var t = candidates[m].text;
                    if (t.includes('username') && !t.includes('phone') && t.length < 35) {
                        candidates[m].el.click();
                        return 'clicked_username:' + t;
                    }
                }

                // Priority 3: element with ONLY "email" as text
                for (var n = 0; n < candidates.length; n++) {
                    if (candidates[n].text === 'email') {
                        candidates[n].el.click();
                        return 'clicked_email:' + candidates[n].text;
                    }
                }

                // Debug: list all candidate texts
                var debug = candidates.map(function(c) { return c.tag + ':' + c.text.substring(0, 25); }).slice(0, 15);
                return 'not_found|candidates:' + debug.join(' | ');
            """)
        except Exception as e:
            tab_result = f"error:{e}"
        step("tab_switch", f"Tab switch: {tab_result}")
        time.sleep(random.uniform(2.0, 4.0))

        # Verify we're on the right tab (should see password field or email placeholder)
        tab_check = driver.execute_script("""
            var inputs = document.querySelectorAll('input');
            var visible = [];
            for (var i = 0; i < inputs.length; i++) {
                if (inputs[i].offsetHeight > 0 && inputs[i].type !== 'hidden') {
                    visible.push({type: inputs[i].type, name: inputs[i].name, ph: inputs[i].placeholder});
                }
            }
            return JSON.stringify(visible);
        """)
        step("tab_verify", f"Inputs after tab switch: {tab_check}")

        snap(driver, "login_page")
        page_src_len = len(driver.page_source or "")
        body_text = ""
        try:
            body_text = driver.find_element(By.TAG_NAME, "body").text
        except Exception:
            pass
        step("login_page", f"Login page | body_len={len(body_text)} | src_len={page_src_len} | URL: {driver.current_url}")

        # Save page source for debugging
        try:
            html_path = os.path.join(SCREENSHOTS_DIR, f"login_{ts}_page.html")
            with open(html_path, "w", encoding="utf-8") as hf:
                hf.write(driver.page_source)
        except Exception:
            pass

        # Step 3: Fill credentials (after switching to Username tab)
        email_input = None
        for selector in [
            'input[name="username"]',
            'input[placeholder*="username" i]',
            'input[placeholder*="email" i]',
            'input[type="email"]',
        ]:
            try:
                el = WebDriverWait(driver, 5).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, selector))
                )
                if el and el.is_displayed():
                    # Skip phone input
                    name = el.get_attribute("name") or ""
                    ph = el.get_attribute("placeholder") or ""
                    if "mobile" in name or "phone" in name or "phone" in ph.lower():
                        continue
                    email_input = el
                    step("input_found", f"Email input found via: {selector} (name={name}, ph={ph})")
                    break
            except Exception:
                continue

        # Fallback: find via JS, explicitly skip phone fields
        if not email_input:
            try:
                email_input = driver.execute_script("""
                    var inputs = document.querySelectorAll('input');
                    for (var i = 0; i < inputs.length; i++) {
                        var inp = inputs[i];
                        if (inp.offsetHeight === 0 || inp.type === 'hidden' || inp.type === 'password') continue;
                        var name = (inp.name || '').toLowerCase();
                        var ph = (inp.placeholder || '').toLowerCase();
                        // Skip phone/mobile fields
                        if (name.includes('mobile') || name.includes('phone') || ph.includes('phone')) continue;
                        // Prefer username/email fields
                        if (name.includes('username') || name.includes('email') || ph.includes('username') || ph.includes('email')) {
                            return inp;
                        }
                    }
                    // Last resort: first non-phone text input
                    for (var j = 0; j < inputs.length; j++) {
                        var inp2 = inputs[j];
                        if (inp2.offsetHeight === 0 || inp2.type === 'hidden' || inp2.type === 'password') continue;
                        var n2 = (inp2.name || '').toLowerCase();
                        var p2 = (inp2.placeholder || '').toLowerCase();
                        if (n2.includes('mobile') || n2.includes('phone') || p2.includes('phone')) continue;
                        return inp2;
                    }
                    return null;
                """)
                if email_input:
                    step("input_found", "Email input found via JS (skipping phone fields)")
            except Exception:
                pass

        if not email_input:
            # Try via JavaScript as last resort
            try:
                email_input_js = driver.execute_script("""
                    var inputs = document.querySelectorAll('input');
                    for (var i = 0; i < inputs.length; i++) {
                        var inp = inputs[i];
                        if (inp.type === 'hidden') continue;
                        if (inp.offsetHeight === 0) continue;
                        return {tag: inp.tagName, name: inp.name, type: inp.type, placeholder: inp.placeholder};
                    }
                    return null;
                """)
                step("js_input_scan", f"JS found: {email_input_js}")
            except Exception:
                pass

            snap(driver, "no_email_input")
            step("error", f"Email input not found. URL: {driver.current_url} | Body: {body_text[:300]} | SrcLen: {page_src_len}")
            driver.quit()
            return {"ok": False, "steps": steps, "error": f"Email input not found. Body({len(body_text)}): {body_text[:200]}"}

        _random_mouse_move(driver, 1)
        ActionChains(driver).move_to_element(email_input).click().perform()
        _human_delay(0.5, 1.2)
        # Clear field first, then type (don't use _react_set_value — conflicts with keyboard events)
        email_input.clear()
        _human_delay(0.1, 0.3)
        _human_type(email_input, username)
        _human_delay(0.8, 1.5)

        def _find_pass_input():
            for sel in [
                'input[type="password"]',
                'input[name="password"]',
                'input[placeholder*="assword" i]',
                'input[autocomplete="current-password"]',
            ]:
                try:
                    el = driver.find_element(By.CSS_SELECTOR, sel)
                    if el and el.is_displayed():
                        return el
                except Exception:
                    continue
            # JS fallback: second visible input
            try:
                inputs = driver.find_elements(By.CSS_SELECTOR, 'input')
                visible = [i for i in inputs if i.is_displayed() and i.get_attribute("type") != "hidden"]
                if len(visible) >= 2:
                    return visible[1]
            except Exception:
                pass
            return None

        pass_input = _find_pass_input()

        # Two-step login: password might appear after clicking Next/Continue
        if not pass_input:
            step("no_pass_yet", "Password not visible, looking for Next button (two-step login)...")
            try:
                next_clicked = driver.execute_script("""
                    var btns = document.querySelectorAll('button, div[role="button"], a');
                    for (var i = 0; i < btns.length; i++) {
                        var t = (btns[i].innerText || '').trim().toLowerCase();
                        if ((t === 'next' || t === 'continue' || t === 'далее' || t === 'продолжить'
                             || t === 'log in with password' || t === 'log in'
                             || t.match(/^(next|continue|submit)$/i))
                            && btns[i].offsetHeight > 0) {
                            btns[i].click();
                            return 'clicked:' + t;
                        }
                    }
                    // Also try links with "password" text
                    var links = document.querySelectorAll('a, span, div, p');
                    for (var j = 0; j < links.length; j++) {
                        var lt = (links[j].innerText || '').trim().toLowerCase();
                        if ((lt.includes('password') || lt.includes('пароль'))
                            && lt.length < 40 && links[j].offsetHeight > 0) {
                            links[j].click();
                            return 'clicked_link:' + lt;
                        }
                    }
                    return 'not_found';
                """)
                step("next_btn", f"Next/password button: {next_clicked}")
                if next_clicked and next_clicked.startswith("clicked"):
                    time.sleep(3)
                    pass_input = _find_pass_input()
                    if pass_input:
                        step("pass_appeared", "Password input appeared after clicking Next")
            except Exception as e:
                step("next_btn_error", str(e))

        if not pass_input:
            # Log what's visible for debugging
            try:
                vis_data = driver.execute_script("""
                    var inputs = document.querySelectorAll('input');
                    var visible = [];
                    for (var i = 0; i < inputs.length; i++) {
                        if (inputs[i].offsetHeight > 0 && inputs[i].type !== 'hidden') {
                            visible.push({type: inputs[i].type, name: inputs[i].name, ph: inputs[i].placeholder});
                        }
                    }
                    return JSON.stringify(visible);
                """)
                step("pass_scan", f"Visible inputs: {vis_data}")
            except Exception:
                pass
            body_text = driver.find_element(By.TAG_NAME, "body").text
            snap(driver, "no_pass_field")
            step("error", f"Password input not found. Body: {body_text[:300]}")
            driver.quit()
            return {"ok": False, "steps": steps, "error": "Password input not found"}

        _random_mouse_move(driver, 1)
        ActionChains(driver).move_to_element(pass_input).click().perform()
        _human_delay(0.5, 1.0)
        pass_input.clear()
        _human_delay(0.1, 0.3)
        _human_type(pass_input, password)
        _human_delay(1.0, 2.0)

        # Verify both inputs have correct values in DOM
        email_val = driver.execute_script("return arguments[0].value", email_input)
        pass_val = driver.execute_script("return arguments[0].value", pass_input)
        step("credentials", f"Credentials filled. Email={email_val[:3]}*** Pass={'*'*len(pass_val)} ({len(pass_val)} chars)")

        # Step 4: Click login button — pause like human reading the form
        _random_mouse_move(driver, random.randint(1, 2))
        _human_delay(1.5, 3.0)
        # Find login button — must be visible and inside the login form, not search
        login_btn = driver.execute_script("""
            // Method 1: data-e2e login button
            var e2e = document.querySelector('button[data-e2e="login-button"]');
            if (e2e && e2e.offsetHeight > 0) return e2e;

            // Method 2: button with login-related text inside a modal/dialog/form
            var loginTexts = ['log in', 'login', 'continue', 'sign in'];
            var containers = document.querySelectorAll('[role="dialog"], [class*="modal"], [class*="Modal"], form, [class*="login"], [class*="Login"]');
            for (var c = 0; c < containers.length; c++) {
                var ct = (containers[c].innerText || '').toLowerCase();
                if (ct.indexOf('password') < 0 && ct.indexOf('username') < 0
                    && ct.indexOf('email') < 0) continue;
                var btns = containers[c].querySelectorAll('button');
                for (var i = 0; i < btns.length; i++) {
                    var t = (btns[i].innerText || '').trim().toLowerCase();
                    for (var lt = 0; lt < loginTexts.length; lt++) {
                        if (t === loginTexts[lt] && btns[i].offsetHeight > 0
                            && btns[i].offsetWidth > 50) {
                            return btns[i];
                        }
                    }
                }
            }

            // Method 3: any visible button with login text (wider than 50px)
            var allBtns = document.querySelectorAll('button');
            for (var j = 0; j < allBtns.length; j++) {
                var bt = (allBtns[j].innerText || '').trim().toLowerCase();
                for (var lt2 = 0; lt2 < loginTexts.length; lt2++) {
                    if (bt === loginTexts[lt2] && allBtns[j].offsetHeight > 0
                        && allBtns[j].offsetWidth > 50) {
                        return allBtns[j];
                    }
                }
            }

            // Method 4: submit button inside login form (NOT search)
            var forms = document.querySelectorAll('form');
            for (var f = 0; f < forms.length; f++) {
                var formText = (forms[f].innerText || '').toLowerCase();
                if (formText.indexOf('password') >= 0 || formText.indexOf('username') >= 0) {
                    var sub = forms[f].querySelector('button[type="submit"]');
                    if (sub && sub.offsetHeight > 0) return sub;
                }
            }

            return null;
        """)
        if not login_btn:
            step("error", "Login button not found")
            driver.quit()
            return {"ok": False, "steps": steps, "error": "Login button not found"}

        # Check if login button is disabled (fields might not be validated yet)
        btn_disabled = driver.execute_script("""
            var btn = arguments[0];
            return {
                disabled: btn.disabled,
                ariaDisabled: btn.getAttribute('aria-disabled'),
                classes: btn.className,
                text: (btn.innerText || '').trim()
            };
        """, login_btn)
        step("login_btn_state", f"Button: {json.dumps(btn_disabled)}")

        # Intercept XHR/fetch to monitor login API calls
        driver.execute_script("""
            window.__loginRequests = [];
            // Intercept XMLHttpRequest
            var origOpen = XMLHttpRequest.prototype.open;
            var origSend = XMLHttpRequest.prototype.send;
            XMLHttpRequest.prototype.open = function(method, url) {
                this.__url = url; this.__method = method;
                return origOpen.apply(this, arguments);
            };
            XMLHttpRequest.prototype.send = function(body) {
                var self = this;
                if (self.__url && (self.__url.includes('login') || self.__url.includes('passport'))) {
                    window.__loginRequests.push({type:'xhr', method: self.__method, url: self.__url, time: Date.now()});
                    self.addEventListener('load', function() {
                        window.__loginRequests.push({type:'xhr_resp', status: self.status, url: self.__url,
                            resp: (self.responseText||'').substring(0, 500)});
                    });
                }
                return origSend.apply(this, arguments);
            };
            // Intercept fetch
            var origFetch = window.fetch;
            window.fetch = function(url, opts) {
                var urlStr = typeof url === 'string' ? url : (url.url || '');
                if (urlStr.includes('login') || urlStr.includes('passport')) {
                    window.__loginRequests.push({type:'fetch', method: (opts||{}).method||'GET', url: urlStr, time: Date.now()});
                }
                return origFetch.apply(this, arguments).then(function(resp) {
                    if (urlStr.includes('login') || urlStr.includes('passport')) {
                        resp.clone().text().then(function(t) {
                            window.__loginRequests.push({type:'fetch_resp', status: resp.status, url: urlStr, resp: t.substring(0, 500)});
                        });
                    }
                    return resp;
                });
            };
        """)

        # Click login button — try ActionChains first, fallback to JS click
        try:
            ActionChains(driver).move_to_element(login_btn).click().perform()
        except Exception as click_err:
            step("login_click_fallback", f"ActionChains failed: {click_err}, trying JS click")
            driver.execute_script("arguments[0].click()", login_btn)

        # Wait for login API response
        time.sleep(8)
        snap(driver, "after_login_click")

        # Check intercepted login API calls
        try:
            login_reqs = driver.execute_script("return JSON.stringify(window.__loginRequests || []);")
            step("login_api", f"API calls: {login_reqs[:500]}")
        except Exception:
            step("login_api", "Could not read intercepted requests")

        # Check modal for errors BEFORE navigating away
        modal_text = driver.execute_script("""
            var errors = [];
            var errorEls = document.querySelectorAll(
                '[class*="error"], [class*="Error"], [class*="alert"], [class*="warning"], '
                + '[class*="message"], [data-e2e*="error"]'
            );
            for (var i = 0; i < errorEls.length; i++) {
                var t = (errorEls[i].innerText || '').trim();
                if (t && t.length > 2 && t.length < 200 && errorEls[i].offsetHeight > 0) {
                    errors.push(t);
                }
            }
            // Also check for any red/error colored text
            var allSpans = document.querySelectorAll('span, p, div');
            for (var j = 0; j < allSpans.length; j++) {
                var el = allSpans[j];
                if (el.offsetHeight === 0) continue;
                var style = window.getComputedStyle(el);
                var color = style.color;
                var text = (el.innerText || '').trim();
                // Red-ish text (error messages)
                if (color && color.match(/rgb\\(2[0-5]\\d,\\s*[0-5]?\\d,\\s*[0-5]?\\d\\)/) && text.length > 5 && text.length < 150) {
                    errors.push('RED:' + text);
                }
            }
            // Check if login modal is still visible
            var modal = document.querySelector('[class*="modal"], [class*="Modal"], [class*="dialog"], [role="dialog"]');
            var modalVisible = modal && modal.offsetHeight > 0;
            // Get modal body text
            var modalText = modal ? (modal.innerText || '').substring(0, 300) : '';
            return JSON.stringify({errors: errors, modalVisible: modalVisible, modalText: modalText});
        """)
        step("modal_check", f"Modal state: {modal_text}")

        try:
            modal_data = json.loads(modal_text)
        except Exception:
            modal_data = {}

        # Check for verification/challenge screens that may have appeared
        post_login_state = driver.execute_script("""
            var state = {url: location.href, hasCodeInput: false, hasVerifyText: false,
                         newModals: [], iframes: [], allDialogs: []};
            // Check for verification code input fields
            var inputs = document.querySelectorAll('input');
            var codeInputs = 0;
            for (var i = 0; i < inputs.length; i++) {
                var inp = inputs[i];
                if (inp.offsetHeight === 0) continue;
                var ph = (inp.placeholder || '').toLowerCase();
                var name = (inp.name || '').toLowerCase();
                var type = inp.type;
                if (ph.includes('code') || ph.includes('verify') || name.includes('code')
                    || (type === 'tel' && inp.maxLength <= 6)) {
                    codeInputs++;
                }
            }
            if (codeInputs > 0) state.hasCodeInput = true;
            // Check for verification text
            var bodyText = (document.body.innerText || '').toLowerCase();
            if (bodyText.includes('verification') || bodyText.includes('verify your')
                || bodyText.includes('enter the code') || bodyText.includes('sent a code')
                || bodyText.includes('enter code') || bodyText.includes('confirm your')) {
                state.hasVerifyText = true;
            }
            // Check all visible dialogs/modals
            var dialogs = document.querySelectorAll('[role="dialog"], [class*="modal"], [class*="Modal"], [class*="overlay"]');
            for (var j = 0; j < dialogs.length; j++) {
                if (dialogs[j].offsetHeight > 0) {
                    state.allDialogs.push((dialogs[j].innerText || '').substring(0, 200));
                }
            }
            // Check iframes (captcha/verification often in iframe)
            var iframes = document.querySelectorAll('iframe');
            for (var k = 0; k < iframes.length; k++) {
                if (iframes[k].offsetHeight > 0 && iframes[k].src) {
                    state.iframes.push(iframes[k].src.substring(0, 100));
                }
            }
            return state;
        """)
        step("post_login_state", f"State: {json.dumps(post_login_state)[:500]}")

        # If verification is needed, handle it
        if post_login_state.get("hasCodeInput") or post_login_state.get("hasVerifyText"):
            step("verify_detected", "Verification/challenge detected after login click!")
            snap(driver, "verify_after_login")

            # ── SMS verification flow (if sms_api_key provided) ──
            if sms_api_key:
                step("sms_start", "SMS API key provided, trying phone verification...")
                sms_number_data = _sms_get_number(sms_api_key)
                if sms_number_data:
                    sms_id = sms_number_data["id"]
                    sms_phone = sms_number_data["number"]
                    step("sms_number", f"Got number: +{sms_phone} (id={sms_id})")

                    # Check if verification dialog has Phone option
                    has_phone = driver.execute_script("""
                        var dialogs = document.querySelectorAll('[role="dialog"], [class*="modal"], [class*="Modal"], [class*="verify"], [class*="IDV"]');
                        for (var d = 0; d < dialogs.length; d++) {
                            var dt = (dialogs[d].innerText || '').toLowerCase();
                            if (dt.indexOf('verify') >= 0 && (dt.indexOf('phone') >= 0 || dt.indexOf('sms') >= 0))
                                return true;
                        }
                        return false;
                    """)

                    if has_phone:
                        # Click Phone option
                        phone_clicked = driver.execute_script("""
                            function fullClick(el) {
                                var r = el.getBoundingClientRect();
                                var opts = {bubbles:true, cancelable:true, view:window,
                                    clientX: r.left+r.width/2, clientY: r.top+r.height/2};
                                el.dispatchEvent(new PointerEvent('pointerdown', opts));
                                el.dispatchEvent(new MouseEvent('mousedown', opts));
                                el.dispatchEvent(new PointerEvent('pointerup', opts));
                                el.dispatchEvent(new MouseEvent('mouseup', opts));
                                el.dispatchEvent(new MouseEvent('click', opts));
                            }
                            var dialogs = document.querySelectorAll('[role="dialog"], [class*="modal"], [class*="Modal"], [class*="verify"], [class*="IDV"]');
                            for (var d = 0; d < dialogs.length; d++) {
                                var dt = (dialogs[d].innerText || '').toLowerCase();
                                if (dt.indexOf('verify') < 0) continue;
                                var children = dialogs[d].querySelectorAll('div,span,a,p,button,li');
                                for (var i = 0; i < children.length; i++) {
                                    var el = children[i];
                                    if (el.offsetHeight === 0 || el.offsetHeight > 100) continue;
                                    var t = (el.innerText || '').trim().toLowerCase();
                                    if ((t === 'phone' || t === 'sms' || t === 'phone number')
                                        && el.offsetWidth > 20) {
                                        fullClick(el);
                                        return 'clicked:' + t;
                                    }
                                }
                            }
                            return null;
                        """)
                        step("sms_phone_click", f"Phone option: {phone_clicked}")
                        time.sleep(3)

                        if phone_clicked:
                            # Find phone input, enter number, click send
                            phone_entered = driver.execute_script("""
                                var phone = arguments[0];
                                var inputs = document.querySelectorAll('input');
                                for (var i = 0; i < inputs.length; i++) {
                                    var inp = inputs[i];
                                    if (inp.offsetHeight === 0) continue;
                                    var ph = (inp.placeholder || '').toLowerCase();
                                    var tp = inp.type || '';
                                    if (ph.indexOf('phone') >= 0 || ph.indexOf('number') >= 0
                                        || tp === 'tel') {
                                        var ns = Object.getOwnPropertyDescriptor(
                                            HTMLInputElement.prototype, 'value').set;
                                        ns.call(inp, phone);
                                        inp.dispatchEvent(new Event('input', {bubbles:true}));
                                        inp.dispatchEvent(new Event('change', {bubbles:true}));
                                        return 'entered';
                                    }
                                }
                                return 'no_phone_input';
                            """, sms_phone)
                            step("sms_phone_enter", f"Phone entry: {phone_entered}")

                            if phone_entered == "entered":
                                # Click Send code
                                time.sleep(1)
                                driver.execute_script("""
                                    var btns = document.querySelectorAll('button, div[role="button"]');
                                    for (var i = 0; i < btns.length; i++) {
                                        var t = (btns[i].innerText || '').trim().toLowerCase();
                                        if ((t === 'send code' || t === 'send' || t === 'next'
                                             || t === 'continue' || t === 'verify')
                                            && btns[i].offsetHeight > 0) {
                                            btns[i].click();
                                            return t;
                                        }
                                    }
                                    return null;
                                """)
                                step("sms_send_code", "Send code clicked")

                                # Set status to "ready" (waiting for SMS)
                                _sms_set_status(sms_api_key, sms_id, 1)

                                # Wait for SMS code
                                step("sms_waiting", f"Waiting for SMS code to +{sms_phone}...")
                                sms_code = _sms_get_code(sms_api_key, sms_id, max_wait=120)

                                if sms_code:
                                    step("sms_code_received", f"SMS code: {sms_code}")
                                    code_entered = _enter_verification_code(driver, sms_code)
                                    step("sms_code_entered", f"Code entry: {code_entered}")
                                    time.sleep(2)

                                    # Click verify/next button
                                    driver.execute_script("""
                                        var btns = document.querySelectorAll('button, div[role="button"]');
                                        for (var i = 0; i < btns.length; i++) {
                                            var t = (btns[i].innerText || '').trim().toLowerCase();
                                            if ((t === 'next' || t === 'verify' || t === 'submit'
                                                 || t === 'confirm' || t === 'log in')
                                                && btns[i].offsetHeight > 0 && !btns[i].disabled) {
                                                btns[i].click();
                                                return t;
                                            }
                                        }
                                        return null;
                                    """)
                                    time.sleep(5)

                                    # Mark SMS activation as complete
                                    _sms_set_status(sms_api_key, sms_id, 6)

                                    # Check login
                                    try:
                                        cookies = driver.get_cookies()
                                        cookie_names = [c["name"] for c in cookies]
                                        if "sessionid" in cookie_names or "sid_tt" in cookie_names:
                                            _save_cookies(driver, username)
                                            step("success", "LOGIN SUCCESS via SMS verification!")
                                            driver.quit()
                                            return {"ok": True, "steps": steps}
                                    except Exception:
                                        pass
                                    step("sms_login_check", "SMS code entered, checking status...")
                                else:
                                    step("sms_no_code", "No SMS code received in 120s")
                                    _sms_set_status(sms_api_key, sms_id, 8)
                    else:
                        step("sms_no_phone_option", "Verification dialog has no Phone option, falling back to email")
                else:
                    step("sms_no_number", "Could not get SMS number from API")

            # ── Email verification flow (fallback or primary) ──
            # Intercept verify/send-code API calls to see server response
            driver.execute_script("""
                window.__verifyRequests = [];
                var origFetch = window.fetch;
                window.fetch = function(url, opts) {
                    var urlStr = typeof url === 'string' ? url : (url.url || '');
                    var isVerify = urlStr.indexOf('verify') >= 0 || urlStr.indexOf('send_code') >= 0
                        || urlStr.indexOf('send-code') >= 0 || urlStr.indexOf('idv') >= 0
                        || urlStr.indexOf('passport') >= 0 || urlStr.indexOf('challenge') >= 0;
                    if (isVerify) {
                        window.__verifyRequests.push({type:'fetch', url: urlStr.substring(0,200),
                            method: (opts||{}).method||'GET', time: Date.now()});
                    }
                    return origFetch.apply(this, arguments).then(function(resp) {
                        if (isVerify) {
                            resp.clone().text().then(function(t) {
                                window.__verifyRequests.push({type:'fetch_resp', url: urlStr.substring(0,200),
                                    status: resp.status, body: t.substring(0, 500)});
                            });
                        }
                        return resp;
                    });
                };
                var origXhr = XMLHttpRequest.prototype.open;
                var origSend = XMLHttpRequest.prototype.send;
                XMLHttpRequest.prototype.open = function(m, url) {
                    this.__url = url; this.__method = m;
                    return origXhr.apply(this, arguments);
                };
                XMLHttpRequest.prototype.send = function(body) {
                    var self = this;
                    var u = self.__url || '';
                    var isV = u.indexOf('verify') >= 0 || u.indexOf('send_code') >= 0
                        || u.indexOf('idv') >= 0 || u.indexOf('passport') >= 0;
                    if (isV) {
                        window.__verifyRequests.push({type:'xhr', url: u.substring(0,200),
                            method: self.__method, time: Date.now()});
                        self.addEventListener('load', function() {
                            window.__verifyRequests.push({type:'xhr_resp', url: u.substring(0,200),
                                status: self.status, body: (self.responseText||'').substring(0,500)});
                        });
                    }
                    return origSend.apply(this, arguments);
                };
            """)

            # Click "Email" option in verification dialog
            email_row_el = None
            try:
                email_row_el = driver.execute_script("""
                    var star = String.fromCharCode(42);
                    var dialogs = document.querySelectorAll('[role="dialog"], [class*="modal"], [class*="Modal"], [class*="verify"], [class*="IDV"]');
                    for (var d = 0; d < dialogs.length; d++) {
                        var dt = (dialogs[d].innerText || '').toLowerCase();
                        if (dt.indexOf('verify') < 0 || dialogs[d].offsetHeight === 0) continue;

                        // Strategy 1: find the SMALLEST element containing masked email
                        var children = dialogs[d].querySelectorAll('div,span,a,p,button,li,section');
                        var best = null;
                        var bestArea = Infinity;
                        for (var i = 0; i < children.length; i++) {
                            var el = children[i];
                            if (el.offsetHeight === 0 || el.offsetHeight > 200) continue;
                            var t = (el.innerText || '').trim();
                            if (t.indexOf('@') < 0 || t.indexOf(star) < 0) continue;
                            if (t.toLowerCase().indexOf('verify') >= 0) continue;
                            var area = el.offsetWidth * el.offsetHeight;
                            if (area < bestArea) {
                                bestArea = area;
                                best = el;
                            }
                        }
                        if (best) {
                            // Walk up max 3 levels — ONLY if parent is clickable
                            var target = best;
                            var cur = best;
                            for (var k = 0; k < 3; k++) {
                                var p = cur.parentElement;
                                if (!p || p === dialogs[d] || p.offsetHeight > 200) break;
                                var cs = window.getComputedStyle(p);
                                if (cs.cursor === 'pointer' || p.getAttribute('role') === 'button'
                                    || p.onclick || p.tagName === 'A' || p.tagName === 'BUTTON') {
                                    target = p;
                                    break;
                                }
                                cur = p;
                            }
                            return target;
                        }

                        // Strategy 2: find the row containing "Email" label + masked address
                        for (var j = 0; j < children.length; j++) {
                            var el2 = children[j];
                            if (el2.offsetHeight === 0 || el2.offsetHeight > 200) continue;
                            var t2 = (el2.innerText || '').trim();
                            // Row that starts with "Email" and has the masked address
                            if (t2.indexOf('Email') >= 0 && t2.indexOf(star) >= 0
                                && t2.indexOf('@') >= 0 && t2.length < 100
                                && t2.toLowerCase().indexOf('verify') < 0) {
                                return el2;
                            }
                        }

                        // Strategy 3: just "Email" text element, walk up to row
                        for (var m = 0; m < children.length; m++) {
                            var el3 = children[m];
                            if (el3.offsetHeight === 0) continue;
                            var t3 = (el3.textContent || '').trim();
                            if (t3 === 'Email' && el3.children.length === 0) {
                                // This is the leaf "Email" label — go up to find the row
                                var row = el3;
                                for (var r = 0; r < 4; r++) {
                                    var pp = row.parentElement;
                                    if (!pp || pp === dialogs[d] || pp.offsetHeight > 200) break;
                                    if (pp.offsetHeight > 30 && pp.offsetWidth > 100) {
                                        row = pp;
                                        break;
                                    }
                                    row = pp;
                                }
                                return row;
                            }
                        }
                    }
                    return null;
                """)
            except Exception as e:
                step("verify_find_error", f"Error finding email row: {e}")

            if email_row_el:
                el_info = driver.execute_script("""
                    var e = arguments[0];
                    return {tag: e.tagName, cls: (e.className || '').substring(0, 80),
                            w: e.offsetWidth, h: e.offsetHeight,
                            cursor: window.getComputedStyle(e).cursor,
                            text: (e.innerText || '').trim().substring(0, 80)};
                """, email_row_el)
                step("verify_email_found", f"Found: {json.dumps(el_info)}")
                try:
                    ActionChains(driver).move_to_element(email_row_el).click().perform()
                    step("verify_email_click", f"ActionChains click OK")
                except Exception as e:
                    step("verify_email_click", f"ActionChains click failed: {e}")
                    try:
                        driver.execute_script("arguments[0].click()", email_row_el)
                        step("verify_email_click_js", "Fallback JS click on email row")
                    except Exception:
                        pass
            else:
                step("verify_email_click", "Email row element not found in verify dialog")

            time.sleep(3)
            snap(driver, "after_verify_email_click")

            # Check if dialog changed after first click attempt
            dialog_changed = driver.execute_script("""
                var dialogs = document.querySelectorAll('[role="dialog"], [class*="modal"], [class*="Modal"], [class*="verify"]');
                for (var d = 0; d < dialogs.length; d++) {
                    var dt = (dialogs[d].innerText || '').toLowerCase();
                    if (dt.indexOf('send code') >= 0 || dt.indexOf('send a code') >= 0
                        || dt.indexOf('enter code') >= 0 || dt.indexOf('enter the code') >= 0
                        || dt.indexOf('verification code') >= 0) return true;
                }
                return false;
            """)

            if not dialog_changed and email_row_el:
                step("verify_retry", "Dialog unchanged, trying React-style event dispatch")
                try:
                    driver.execute_script("""
                        var el = arguments[0];
                        function fireEvent(target, type) {
                            var r = target.getBoundingClientRect();
                            var ev = new MouseEvent(type, {
                                bubbles: true, cancelable: true, view: window,
                                clientX: r.left + r.width / 2,
                                clientY: r.top + r.height / 2
                            });
                            target.dispatchEvent(ev);
                        }
                        var targets = [el];
                        var kids = el.querySelectorAll('div,span,a,p,button,li');
                        for (var i = 0; i < kids.length; i++) {
                            if (kids[i].offsetHeight > 0) targets.push(kids[i]);
                        }
                        if (el.parentElement && el.parentElement.offsetHeight < 200) targets.push(el.parentElement);

                        for (var t = 0; t < targets.length; t++) {
                            fireEvent(targets[t], 'pointerdown');
                            fireEvent(targets[t], 'mousedown');
                            fireEvent(targets[t], 'pointerup');
                            fireEvent(targets[t], 'mouseup');
                            fireEvent(targets[t], 'click');
                        }
                    """, email_row_el)
                except Exception as e:
                    step("verify_retry_err", f"dispatchEvent error (stale?): {type(e).__name__}")
                time.sleep(3)

                dialog_changed = driver.execute_script("""
                    var dialogs = document.querySelectorAll('[role="dialog"], [class*="modal"], [class*="Modal"], [class*="verify"]');
                    for (var d = 0; d < dialogs.length; d++) {
                        var dt = (dialogs[d].innerText || '').toLowerCase();
                        if (dt.indexOf('send code') >= 0 || dt.indexOf('send a code') >= 0
                            || dt.indexOf('enter code') >= 0 || dt.indexOf('enter the code') >= 0
                            || dt.indexOf('verification code') >= 0) return true;
                    }
                    return false;
                """)
                step("verify_retry_result", f"Dialog changed: {dialog_changed}")

            if not dialog_changed:
                step("verify_retry2", "Trying JS React-event clicks on each email row candidate")
                try:
                    js_click_result = driver.execute_script("""
                        var star = String.fromCharCode(42);
                        function fullClick(el) {
                            var r = el.getBoundingClientRect();
                            var cx = r.left + r.width / 2;
                            var cy = r.top + r.height / 2;
                            var opts = {bubbles:true, cancelable:true, view:window, clientX:cx, clientY:cy};
                            el.dispatchEvent(new PointerEvent('pointerdown', opts));
                            el.dispatchEvent(new MouseEvent('mousedown', opts));
                            el.dispatchEvent(new PointerEvent('pointerup', opts));
                            el.dispatchEvent(new MouseEvent('mouseup', opts));
                            el.dispatchEvent(new MouseEvent('click', opts));
                        }
                        var dialogs = document.querySelectorAll('[role="dialog"], [class*="modal"], [class*="Modal"], [class*="verify"], [class*="IDV"]');
                        var clicked = [];
                        for (var d = 0; d < dialogs.length; d++) {
                            var dt = (dialogs[d].innerText || '').toLowerCase();
                            if (dt.indexOf('verify') < 0 || dialogs[d].offsetHeight === 0) continue;
                            var children = dialogs[d].querySelectorAll('div,span,a,p,button,li');
                            // Collect candidates sorted by area (smallest first = most specific)
                            var cands = [];
                            for (var i = 0; i < children.length; i++) {
                                var el = children[i];
                                if (el.offsetHeight === 0 || el.offsetHeight > 150) continue;
                                var t = (el.innerText || '').trim();
                                if ((t.indexOf(star) >= 0 && t.indexOf('@') >= 0 && t.length < 80
                                     && t.toLowerCase().indexOf('verify') < 0)
                                    || (t === 'Email')
                                    || (el.textContent.trim() === 'Email' && el.children.length === 0)) {
                                    cands.push({el:el, area: el.offsetWidth * el.offsetHeight,
                                        tag: el.tagName, text: t.substring(0,30)});
                                }
                            }
                            cands.sort(function(a,b){return a.area - b.area;});
                            // Click each candidate with full event sequence
                            for (var c = 0; c < cands.length && c < 6; c++) {
                                fullClick(cands[c].el);
                                clicked.push(cands[c].tag + ':' + cands[c].text.substring(0,20));
                            }
                        }
                        return clicked;
                    """)
                    step("verify_js_clicks", f"Clicked {len(js_click_result)} elements: {js_click_result}")
                    time.sleep(3)
                    dialog_changed = driver.execute_script("""
                        var dialogs = document.querySelectorAll('[role="dialog"], [class*="modal"], [class*="Modal"], [class*="verify"]');
                        for (var d = 0; d < dialogs.length; d++) {
                            var dt = (dialogs[d].innerText || '').toLowerCase();
                            if (dt.indexOf('send code') >= 0 || dt.indexOf('send a code') >= 0
                                || dt.indexOf('enter code') >= 0 || dt.indexOf('enter the code') >= 0
                                || dt.indexOf('verification code') >= 0) return true;
                        }
                        return false;
                    """)
                    step("verify_retry2_result", f"Dialog changed: {dialog_changed}")
                except Exception as e:
                    step("verify_retry2_err", f"Retry2 error: {type(e).__name__}: {e}")

            # After clicking email option, check what changed + click Send/Next
            time.sleep(2)
            snap(driver, "after_verify_all_clicks")

            send_code_result = driver.execute_script("""
                // Check what's visible now in ALL dialogs
                var dialogs = document.querySelectorAll('[role="dialog"], [class*="modal"], [class*="Modal"], [class*="overlay"], [class*="verify"]');
                var dialogTexts = [];
                for (var d = 0; d < dialogs.length; d++) {
                    if (dialogs[d].offsetHeight > 0) {
                        dialogTexts.push((dialogs[d].innerText || '').substring(0, 300));
                    }
                }

                // Look for any clickable action button in dialogs
                var clickPatterns = [
                    /^send$/i, /^send code$/i, /send.*code/i,
                    /^get code$/i, /^resend$/i, /^resend code$/i,
                    /отправить/i
                ];
                var allClickable = document.querySelectorAll('button, div[role="button"], a, [tabindex], [class*="btn"], [class*="Btn"]');
                for (var i = 0; i < allClickable.length; i++) {
                    var btn = allClickable[i];
                    if (btn.offsetHeight === 0 || btn.disabled) continue;
                    var t = (btn.innerText || '').trim();
                    if (t.length > 30) continue;
                    for (var p = 0; p < clickPatterns.length; p++) {
                        if (t.match(clickPatterns[p])) {
                            btn.click();
                            return JSON.stringify({clicked: t, tag: btn.tagName, dialogs: dialogTexts});
                        }
                    }
                }

                // If no button found, check if clicking the email row already triggered the flow
                // (some TikTok versions send code on row click without extra button)
                return JSON.stringify({clicked: null, note: 'no_send_btn', dialogs: dialogTexts});
            """)
            step("send_code_click", f"Send code: {send_code_result}")
            time.sleep(5)
            snap(driver, "after_send_code")

            # Check if code input appeared
            code_state = driver.execute_script("""
                var inputs = document.querySelectorAll('input');
                var codeInputs = [];
                for (var i = 0; i < inputs.length; i++) {
                    var inp = inputs[i];
                    if (inp.offsetHeight === 0) continue;
                    codeInputs.push({type: inp.type, name: inp.name, ph: inp.placeholder,
                                     maxLen: inp.maxLength, autocomplete: inp.autocomplete});
                }
                // Check all visible dialogs for code-related content
                var dialogs = document.querySelectorAll('[role="dialog"], [class*="modal"], [class*="Modal"], [class*="verify"]');
                var dialogTexts = [];
                for (var d = 0; d < dialogs.length; d++) {
                    if (dialogs[d].offsetHeight > 0) dialogTexts.push((dialogs[d].innerText || '').substring(0, 300));
                }
                var bodyText = document.body.innerText || '';
                var hasCodeText = bodyText.match(/enter.*code|code.*sent|verification.*code|verify.*code|we.*sent/i) !== null;
                return {inputs: codeInputs, hasCodeText: hasCodeText, dialogs: dialogTexts,
                        bodySnippet: bodyText.substring(0, 300)};
            """)
            step("verify_code_state", f"Code state: {json.dumps(code_state)[:500]}")

            # Read intercepted verify API responses
            try:
                verify_api = driver.execute_script("return window.__verifyRequests || [];")
                if verify_api:
                    step("verify_api_calls", f"API: {json.dumps(verify_api)[:800]}")
                else:
                    step("verify_api_calls", "No verify API calls intercepted")
            except Exception:
                pass

            # Save browser session for manual code entry via /api/submit-code
            _pending_browser_sessions[username] = {
                "driver": driver, "steps": steps, "snap": snap,
                "email_for_code": username
            }
            _pending_verification[username] = {
                "driver": driver, "steps": steps, "snap": snap,
                "email_for_code": username
            }

            # Try auto-read code via IMAP if email credentials provided
            email_for_imap = username
            imap_pass = email_password
            if email_for_imap and imap_pass:
                step("imap_reading", f"Attempting to read code from {email_for_imap} via IMAP...")
                try:
                    imap_code = _read_tiktok_code_from_imap(email_for_imap, imap_pass, imap_server)
                    if imap_code:
                        step("imap_code_found", f"Code from IMAP: {imap_code}")
                        # Enter the code
                        code_entered = _enter_verification_code(driver, imap_code)
                        if code_entered:
                            time.sleep(5)
                            snap(driver, "after_code_entry")
                            # Check if logged in now
                            driver.get("https://www.tiktok.com/foryou")
                            time.sleep(5)
                            has_login_btn = bool(driver.execute_script("""
                                var els = document.querySelectorAll('button, a, div');
                                for (var i = 0; i < els.length; i++) {
                                    var t = (els[i].innerText || '').trim();
                                    if ((t === 'Log in' || t === 'Login') && els[i].offsetHeight > 0) {
                                        var rect = els[i].getBoundingClientRect();
                                        if (rect.top < 100) return true;
                                    }
                                }
                                return false;
                            """))
                            if not has_login_btn:
                                _save_cookies(driver, username)
                                step("success", "LOGIN SUCCESS after verification code!")
                                driver.quit()
                                return {"ok": True, "steps": steps}
                            step("code_entered_but_not_logged", "Code entered but still not logged in")
                    else:
                        step("imap_no_code", "No TikTok code found in email yet")
                except Exception as e:
                    step("imap_error", f"IMAP read failed: {e}")

            step("verify_waiting", "Verification code needed. Enter code via bot.")
            return {"ok": False, "steps": steps, "error": "VERIFICATION_NEEDED",
                    "verify": True, "needs_code": True, "need_code": True,
                    "username": username}

        # Check for REAL captcha UI (not just word in JS code)
        has_captcha = driver.execute_script("""
            // Check for actual captcha overlay/dialog
            var captchaSelectors = [
                '[class*="captcha_verify"]', '[id*="captcha"]',
                '[class*="TUICaptcha"]', '[class*="captcha-container"]',
                '[class*="verify-bar"]', '[class*="seam-modal"]',
                'iframe[src*="captcha"]', 'iframe[src*="verify"]',
            ];
            for (var i = 0; i < captchaSelectors.length; i++) {
                var el = document.querySelector(captchaSelectors[i]);
                if (el && el.offsetHeight > 0) return true;
            }
            // Check body text for captcha prompt
            var bodyText = document.body.innerText || '';
            if (bodyText.match(/drag.*slider|slide.*puzzle|verify.*human|captcha/i)) return true;
            return false;
        """)

        body_text = driver.find_element(By.TAG_NAME, "body").text
        step("after_login", f"URL: {driver.current_url} | Captcha: {has_captcha} | Body: {body_text[:200]}")

        if has_captcha:
            snap(driver, "captcha_login")
            # Analyze captcha type
            captcha_info = driver.execute_script("""
                var info = {type: 'unknown', iframes: [], elements: [], visible: false};
                // Check iframes (captcha often in iframe)
                var iframes = document.querySelectorAll('iframe');
                for (var i = 0; i < iframes.length; i++) {
                    var src = iframes[i].src || '';
                    if (src) info.iframes.push(src.substring(0, 100));
                }
                // Check captcha-related elements
                var selectors = [
                    '[class*="captcha"]', '[class*="Captcha"]', '[id*="captcha"]',
                    '[class*="verify"]', '[class*="Verify"]',
                    '[class*="puzzle"]', '[class*="slider"]', '[class*="rotate"]',
                    '[class*="whirl"]', '[class*="slide"]',
                    'img[class*="captcha"]', 'canvas',
                ];
                for (var j = 0; j < selectors.length; j++) {
                    var els = document.querySelectorAll(selectors[j]);
                    for (var k = 0; k < els.length; k++) {
                        var el = els[k];
                        if (el.offsetHeight > 0) {
                            info.visible = true;
                            info.elements.push({
                                tag: el.tagName,
                                class: (el.className || '').toString().substring(0, 80),
                                id: el.id || '',
                                size: el.offsetWidth + 'x' + el.offsetHeight
                            });
                        }
                    }
                }
                // Detect type
                var src = document.documentElement.innerHTML.toLowerCase();
                if (src.includes('slide') || src.includes('slider')) info.type = 'slide';
                else if (src.includes('rotate') || src.includes('whirl')) info.type = 'rotate';
                else if (src.includes('puzzle')) info.type = 'puzzle';
                else if (src.includes('shapes') || src.includes('icon')) info.type = 'shapes';
                return JSON.stringify(info);
            """)
            step("captcha_info", f"CAPTCHA details: {captcha_info}")

            # Save captcha page HTML
            try:
                captcha_html = os.path.join(SCREENSHOTS_DIR, f"captcha_{ts}.html")
                with open(captcha_html, "w", encoding="utf-8") as hf:
                    hf.write(driver.page_source)
            except Exception:
                pass

            # Try to solve captcha automatically
            step("captcha_solving", "Attempting auto-solve...")
            solved = _try_solve_captcha(driver, steps, step, snap, ts)
            if solved:
                step("captcha_solved", "CAPTCHA solved!")
                time.sleep(3)
                body_text = driver.find_element(By.TAG_NAME, "body").text
                # Re-check captcha (sometimes multiple rounds)
                page_src2 = driver.page_source.lower()
                if "captcha" in page_src2 or "puzzle" in page_src2:
                    step("captcha_round2", "Second captcha round, trying again...")
                    solved2 = _try_solve_captcha(driver, steps, step, snap, ts)
                    if solved2:
                        step("captcha_solved2", "Second captcha solved!")
                        time.sleep(3)
            else:
                step("captcha_unsolved", "Could not auto-solve captcha")
                _pending_browser_sessions[username] = {"driver": driver, "steps": steps, "has_captcha": True}
                return {"ok": False, "steps": steps, "error": "CAPTCHA detected — auto-solve failed",
                        "captcha": True, "captcha_info": captcha_info}

            body_text = driver.find_element(By.TAG_NAME, "body").text

        # Check for rate limit ONLY inside login modal/overlay, not in page content
        rate_limit_info = driver.execute_script("""
            var phrases = [
                'maximum number', 'too many attempts', 'too many login',
                'try again later', 'too fast', 'rate limit',
                'you\\'ve reached', 'exceeded'
            ];
            // Only check inside modal/dialog/overlay areas
            var containers = document.querySelectorAll(
                '[role="dialog"], [class*="modal"], [class*="Modal"], '
                + '[class*="overlay"], [class*="Overlay"], [class*="toast"], '
                + '[class*="notification"], [class*="alert"], [class*="error"], '
                + '[class*="snackbar"]'
            );
            for (var i = 0; i < containers.length; i++) {
                var t = (containers[i].innerText || '').toLowerCase();
                if (containers[i].offsetHeight === 0) continue;
                for (var j = 0; j < phrases.length; j++) {
                    if (t.includes(phrases[j])) {
                        return {found: true, text: t.substring(0, 200), phrase: phrases[j]};
                    }
                }
            }
            return {found: false};
        """)

        if rate_limit_info and rate_limit_info.get("found"):
            snap(driver, "rate_limited_modal")
            step("rate_limited", f"Rate limited. Match: {rate_limit_info.get('phrase')} | Text: {rate_limit_info.get('text', '')[:200]}")
            driver.quit()
            return {"ok": False, "steps": steps, "error": "Rate limited — wait 2-4 hours"}

        # Check for wrong password or non-existent account
        errors_text = " ".join(modal_data.get("errors", [])).lower()
        modal_text_lower = modal_data.get("modalText", "").lower()
        all_text = errors_text + " " + modal_text_lower
        if "incorrect" in all_text or "wrong" in all_text or "invalid" in all_text:
            step("wrong_creds", f"Wrong credentials: {all_text[:200]}")
            driver.quit()
            return {"ok": False, "steps": steps, "error": "Wrong username or password"}
        if "doesn" in all_text and "exist" in all_text:
            step("account_not_found", f"Account doesn't exist: {all_text[:200]}")
            driver.quit()
            return {"ok": False, "steps": steps, "error": "Account doesn't exist"}

        # Check if modal is still open (login might still be in progress)
        modal_still_open = modal_data.get("modalVisible", False)
        if not modal_still_open:
            step("modal_closed", "Login modal closed — checking login status...")
        else:
            step("modal_still_open", f"Modal still visible. Text: {modal_data.get('modalText', '')[:200]}")

        # Check current URL — if redirected away from homepage, login might have worked
        current_url = driver.current_url.lower()
        if "verify" in current_url or "challenge" in current_url:
            step("verify_redirect", f"Redirected to verification: {driver.current_url}")
        elif "foryou" in current_url or "/following" in current_url:
            step("logged_in_redirect", f"Redirected to feed: {driver.current_url}")

        # Check cookies first — session cookies indicate login before navigation
        try:
            cookies = driver.get_cookies()
            cookie_names = [c["name"] for c in cookies]
            session_cookies = [n for n in cookie_names if n in ("sessionid", "sid_tt", "uid_tt", "sid_guard", "passport_csrf_token")]
            step("cookies_check", f"Session cookies: {session_cookies} | Total: {len(cookies)}")

            if "sessionid" in cookie_names or "sid_tt" in cookie_names:
                _save_cookies(driver, username)
                step("success", "LOGIN SUCCESS — session cookies found!")
                driver.quit()
                return {"ok": True, "steps": steps}
        except Exception as e:
            step("cookies_error", f"Cookie check failed: {type(e).__name__}: {e}")

        # Try navigating to /foryou to confirm login status
        try:
            time.sleep(3)
            driver.get("https://www.tiktok.com/foryou")
            time.sleep(5)
            snap(driver, "foryou_check")

            login_status = driver.execute_script("""
                var result = {hasLoginBtn: false, hasAvatar: false, hasUpload: false, hasFollowing: false};
                var els = document.querySelectorAll('button, a, div');
                for (var i = 0; i < els.length; i++) {
                    var t = (els[i].innerText || '').trim();
                    if ((t === 'Log in' || t === 'Login') && els[i].offsetHeight > 0) {
                        var rect = els[i].getBoundingClientRect();
                        if (rect.top < 100) { result.hasLoginBtn = true; break; }
                    }
                }
                var avatar = document.querySelector('[data-e2e="profile-icon"], [class*="avatar"], img[class*="ImgAvatar"]');
                if (avatar && avatar.offsetHeight > 0) result.hasAvatar = true;
                var upload = document.querySelector('[data-e2e="upload-icon"], a[href*="upload"]');
                if (upload && upload.offsetHeight > 0) result.hasUpload = true;
                var following = document.querySelector('[data-e2e="following-page"]');
                if (following) result.hasFollowing = true;
                return result;
            """)
            step("login_status", f"Status: {json.dumps(login_status)}")

            has_login_btn = login_status.get("hasLoginBtn", True)

            if not has_login_btn:
                _save_cookies(driver, username)
                step("success", "LOGIN SUCCESS — Log in button gone!")
                driver.quit()
                return {"ok": True, "steps": steps}
        except Exception as e:
            step("foryou_error", f"Navigation to /foryou failed: {type(e).__name__}: {e}")
            # Tab may have crashed — try to check cookies from current state
            try:
                cookies = driver.get_cookies()
                cookie_names = [c["name"] for c in cookies]
                if "sessionid" in cookie_names or "sid_tt" in cookie_names:
                    _save_cookies(driver, username)
                    step("success", "LOGIN SUCCESS — session cookies found after crash recovery!")
                    driver.quit()
                    return {"ok": True, "steps": steps}
            except Exception:
                pass

        # Login failed — check if verification needed
        try:
            body_after = driver.execute_script("return document.body ? document.body.innerText : '';") or ""
        except Exception:
            body_after = ""
        body_after_lower = body_after.lower()
        if "verify" in body_after_lower or "code" in body_after_lower:
            step("verify_needed", "Verification page detected after reload")
        else:
            # Collect comprehensive diagnostics
            diag = driver.execute_script("""
                var d = {};
                // Current cookies related to login
                var cookies = document.cookie.split(';').map(function(c){ return c.trim().split('=')[0]; });
                d.loginCookies = cookies.filter(function(c){ return c.match(/sid|session|passport|login|token/i); });
                // Any visible error/notification elements
                d.errors = [];
                var errSels = '[class*="error"], [class*="Error"], [class*="notification"], [class*="toast"], [class*="snack"], [class*="alert"]';
                var errEls = document.querySelectorAll(errSels);
                for (var i = 0; i < errEls.length; i++) {
                    var t = (errEls[i].innerText || '').trim();
                    if (t && t.length > 2 && t.length < 300 && errEls[i].offsetHeight > 0) d.errors.push(t);
                }
                // Page title
                d.title = document.title;
                return JSON.stringify(d);
            """)
            snap(driver, "login_failed_diag")
            step("failed_silent", f"Login failed. Diag: {diag} | ModalErrors: {modal_data.get('errors', [])}")
            driver.quit()
            return {"ok": False, "steps": steps, "error": f"Login failed silently. Diagnostics: {diag}"}

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
        # Use React-compatible nativeSetter to enter code (same as _enter_verification_code)
        entry_result = _enter_verification_code(driver, code)
        step("code_entered", f"Code {code} entered via React nativeSetter: {entry_result}")
        _human_delay(1, 2)

        # Try clicking verify/submit button — must be inside the verification dialog, NOT the main login form
        try:
            verify_btn = driver.execute_script("""
                var btns = document.querySelectorAll('button, div[role="button"]');
                var candidates = [];
                for (var i = 0; i < btns.length; i++) {
                    if (btns[i].offsetHeight === 0) continue;
                    var t = (btns[i].innerText || '').trim().toLowerCase();
                    var inVerifyDialog = false;
                    var p = btns[i];
                    for (var d = 0; d < 15 && p; d++) {
                        var pt = (p.innerText || '').toLowerCase();
                        if (pt.indexOf('verify') !== -1 && pt.indexOf('code') !== -1) {
                            inVerifyDialog = true;
                            break;
                        }
                        p = p.parentElement;
                    }
                    if (inVerifyDialog && t.match(/^(next|verify|submit|confirm|далее|подтвер)$/i)) {
                        candidates.unshift({el: btns[i], t: t, priority: 1});
                    } else if (inVerifyDialog && t.match(/next|verify|submit|confirm|далее|подтвер/i)) {
                        candidates.push({el: btns[i], t: t, priority: 2});
                    } else if (t.match(/^(next|verify|submit|confirm)$/i) && !t.match(/^log.?in$/i)) {
                        candidates.push({el: btns[i], t: t, priority: 3});
                    }
                }
                if (candidates.length > 0) {
                    candidates.sort(function(a, b) { return a.priority - b.priority; });
                    candidates[0].el.click();
                    return 'clicked:' + candidates[0].t + ' (p' + candidates[0].priority + ')';
                }
                return 'no_button';
            """)
            step("verify_btn", f"Verify button: {verify_btn}")
        except Exception as e:
            step("verify_btn", f"Button search failed: {e}, pressing Enter")
            try:
                from selenium.webdriver.common.keys import Keys
                driver.switch_to.active_element.send_keys(Keys.ENTER)
            except Exception:
                pass

        _human_delay(5, 8)

        fname = f"blogin_{int(datetime.now(timezone.utc).timestamp())}_after_code.png"
        path = os.path.join(SCREENSHOTS_DIR, fname)
        try:
            driver.save_screenshot(path)
        except Exception:
            pass

        # Log page state after clicking verify
        try:
            after_state = driver.execute_script("""
                var dialogs = [];
                var els = document.querySelectorAll('[role="dialog"], [class*="modal"], [class*="Modal"], [class*="dialog"], [class*="Dialog"], [class*="verify"], [class*="Verify"]');
                for (var i = 0; i < els.length; i++) {
                    var t = (els[i].innerText || '').trim();
                    if (t.length > 0 && t.length < 500) dialogs.push(t.substring(0, 200));
                }
                return JSON.stringify({url: window.location.href, dialogs: dialogs});
            """)
            step("after_verify_state", f"State after code submit: {after_state}")
        except Exception as e:
            step("after_verify_state", f"Failed to get state: {e}")

        # Check for wrong code first
        try:
            body_text = driver.find_element(By.TAG_NAME, "body").text
        except Exception:
            body_text = ""
        if "incorrect" in body_text.lower() or "invalid" in body_text.lower() or "wrong" in body_text.lower():
            step("wrong_code", "Wrong code entered")
            driver.quit()
            return {"ok": False, "steps": steps, "error": "Wrong verification code"}

        # Verify login by checking session cookies
        try:
            cookies = driver.get_cookies()
            cookie_names = [c["name"] for c in cookies]
            session_cookies = [n for n in cookie_names if n in ("sessionid", "sid_tt", "uid_tt", "sid_guard")]
            step("cookies_after_code", f"Session cookies: {session_cookies} | Total: {len(cookies)}")

            if "sessionid" in cookie_names or "sid_tt" in cookie_names:
                _save_cookies(driver, username)
                step("success", "LOGIN SUCCESS — session cookies confirmed after code!")
                driver.quit()
                return {"ok": True, "steps": steps}
        except Exception as e:
            step("cookies_error", f"Cookie check failed: {e}")

        # Wait more and retry cookie check — TikTok can be slow
        _human_delay(5, 8)
        try:
            cookies = driver.get_cookies()
            cookie_names = [c["name"] for c in cookies]
            session_cookies = [n for n in cookie_names if n in ("sessionid", "sid_tt", "uid_tt", "sid_guard")]
            step("cookies_retry", f"Session cookies retry: {session_cookies} | Total: {len(cookies)}")

            if "sessionid" in cookie_names or "sid_tt" in cookie_names:
                _save_cookies(driver, username)
                step("success", "LOGIN SUCCESS — session cookies confirmed on retry!")
                driver.quit()
                return {"ok": True, "steps": steps}
        except Exception:
            pass

        # Try navigating to /foryou to trigger session
        try:
            driver.get("https://www.tiktok.com/foryou")
            _human_delay(5, 8)
            cookies = driver.get_cookies()
            cookie_names = [c["name"] for c in cookies]
            has_login_btn = driver.execute_script("""
                var els = document.querySelectorAll('button, a, div');
                for (var i = 0; i < els.length; i++) {
                    var t = (els[i].innerText || '').trim();
                    if ((t === 'Log in' || t === 'Login') && els[i].offsetHeight > 0) {
                        var rect = els[i].getBoundingClientRect();
                        if (rect.top < 100) return true;
                    }
                }
                return false;
            """)
            step("foryou_check", f"Login btn: {has_login_btn} | Cookies: {[n for n in cookie_names if n in ('sessionid','sid_tt')]}")

            if ("sessionid" in cookie_names or "sid_tt" in cookie_names) or not has_login_btn:
                _save_cookies(driver, username)
                step("success", "LOGIN SUCCESS after /foryou navigation!")
                driver.quit()
                return {"ok": True, "steps": steps}
        except Exception as e:
            step("foryou_error", f"Navigation failed: {e}")

        step("no_session", f"No session cookies found — login did not complete. Body: {body_text[:300]}")
        driver.quit()
        return {"ok": False, "steps": steps, "error": "Login did not complete — no session cookies"}

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
                              email_password: str = "", imap_server: str = "",
                              sms_api_key: str = "") -> dict:
    loop = asyncio.get_event_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(
                _executor, _browser_fetch_login_sync, username, password, proxy,
                email_password, imap_server, sms_api_key
            ),
            timeout=300
        )
    except asyncio.TimeoutError:
        return {"ok": False, "steps": [{"step": "timeout", "note": "Login timed out after 300s"}],
                "error": "Login timed out after 300 seconds"}


async def browser_submit_manual_code(username: str, code: str) -> dict:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, _browser_submit_manual_code_sync, username, code)


def _restore_session_sync(driver: webdriver.Chrome, username: str) -> bool:
    """Load saved cookies and verify the session is alive."""
    has_cookies = _load_cookies(driver, username)
    if not has_cookies:
        logger.warning(f"No saved cookies for {username}")
        return False

    driver.get("https://www.tiktok.com/foryou")
    time.sleep(random.uniform(4, 6))

    cookies = driver.get_cookies()
    cookie_names = [c["name"] for c in cookies]
    if "sessionid" not in cookie_names and "sid_tt" not in cookie_names:
        logger.warning(f"Cookies expired for {username} — no session cookies")
        return False

    has_login_btn = driver.execute_script("""
        var els = document.querySelectorAll('button, a, div');
        for (var i = 0; i < els.length; i++) {
            var t = (els[i].innerText || '').trim();
            if ((t === 'Log in' || t === 'Login') && els[i].offsetHeight > 0) {
                var rect = els[i].getBoundingClientRect();
                if (rect.top < 100) return true;
            }
        }
        return false;
    """)
    if has_login_btn:
        logger.warning(f"Cookies invalid for {username} — login button visible")
        return False

    logger.info(f"Session restored for {username}")
    _save_cookies(driver, username)
    return True


def _search_videos_sync(driver: webdriver.Chrome, query: str, max_results: int = 50) -> list[str]:
    from urllib.parse import quote
    urls = []
    try:
        search_url = f"https://www.tiktok.com/search/video?q={quote(query)}"
        driver.get(search_url)
        time.sleep(random.uniform(4, 6))
        _dismiss_cookie_banner(driver)

        scroll_count = max(max_results // 5, 3)
        for i in range(scroll_count):
            driver.execute_script("window.scrollBy(0, window.innerHeight)")
            time.sleep(random.uniform(1.5, 3.0))
            if i % 3 == 2:
                _random_mouse_move(driver, 1)

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

        logger.info(f"Found {len(urls)} videos for '{query}'")
    except Exception as e:
        logger.error(f"Search error: {e}")
    return urls


def _post_comment_sync(driver: webdriver.Chrome, video_url: str, comment_text: str) -> tuple[bool, str]:
    """Navigate to video and post a comment. Returns (success, detail)."""
    try:
        driver.get(video_url)
        time.sleep(random.uniform(5, 8))
        _dismiss_cookie_banner(driver)
        _random_mouse_move(driver, 2)

        # Step 1: Click the comment icon to open comment panel (if needed)
        comment_icon_result = driver.execute_script("""
            // Check if comment input is already visible
            var existing = document.querySelector('[data-e2e="comment-input"], [class*="CommentInput"], [class*="comment-input"]');
            if (existing && existing.offsetHeight > 0) return 'already_visible';

            // Try clicking comment icon/button
            var selectors = [
                '[data-e2e="comment-icon"]',
                '[data-e2e="browse-comment-icon"]',
                '[data-e2e="comment-count"]',
                'button[aria-label*="comment" i]',
                'button[aria-label*="Comment" i]',
                'span[data-e2e="comment-icon"]',
            ];
            for (var s = 0; s < selectors.length; s++) {
                var el = document.querySelector(selectors[s]);
                if (el && el.offsetHeight > 0) {
                    el.click();
                    return 'clicked:' + selectors[s];
                }
            }

            // Try finding by comment count text pattern
            var els = document.querySelectorAll('button, span, div');
            for (var i = 0; i < els.length; i++) {
                var e = els[i];
                if (e.offsetHeight === 0) continue;
                var ariaLabel = (e.getAttribute('aria-label') || '').toLowerCase();
                if (ariaLabel.indexOf('comment') !== -1) {
                    e.click();
                    return 'clicked:aria-comment';
                }
            }
            return 'not_found';
        """)
        logger.info(f"Comment icon: {comment_icon_result}")

        if comment_icon_result == 'not_found':
            snap(driver, "no_comment_icon")
            return False, "no_comment_icon"

        if comment_icon_result != 'already_visible':
            _human_delay(2, 4)

        # Step 2: Find the comment input field
        comment_box = driver.execute_script("""
            // Wait a moment for panel to render
            var selectors = [
                '[data-e2e="comment-input"] [contenteditable="true"]',
                '[class*="CommentInput"] [contenteditable="true"]',
                '[class*="comment-input"] [contenteditable="true"]',
                '[contenteditable="true"][data-placeholder*="comment" i]',
                '[contenteditable="true"][data-placeholder*="Comment" i]',
                '[contenteditable="true"][data-placeholder*="Add" i]',
            ];
            for (var s = 0; s < selectors.length; s++) {
                var els = document.querySelectorAll(selectors[s]);
                for (var i = 0; i < els.length; i++) {
                    if (els[i].offsetHeight > 0 && els[i].offsetWidth > 30) {
                        return els[i];
                    }
                }
            }
            // Fallback: any visible contenteditable that's not the video title
            var all = document.querySelectorAll('[contenteditable="true"]');
            for (var j = 0; j < all.length; j++) {
                if (all[j].offsetHeight > 0 && all[j].offsetWidth > 30) {
                    var rect = all[j].getBoundingClientRect();
                    if (rect.width < 800) return all[j];
                }
            }
            return null;
        """)

        if not comment_box:
            snap(driver, "no_comment_box")
            return False, "no_comment_input"

        # Step 3: Click on comment box to focus
        try:
            ActionChains(driver).move_to_element(comment_box).click().perform()
        except Exception:
            driver.execute_script("arguments[0].click()", comment_box)
        _human_delay(0.5, 1.0)

        # Re-find after click (React may re-render)
        comment_box = driver.execute_script("""
            var selectors = [
                '[data-e2e="comment-input"] [contenteditable="true"]',
                '[class*="CommentInput"] [contenteditable="true"]',
                '[contenteditable="true"][data-placeholder*="comment" i]',
                '[contenteditable="true"][data-placeholder*="Add" i]',
            ];
            for (var s = 0; s < selectors.length; s++) {
                var els = document.querySelectorAll(selectors[s]);
                for (var i = 0; i < els.length; i++) {
                    if (els[i].offsetHeight > 0) return els[i];
                }
            }
            var all = document.querySelectorAll('[contenteditable="true"]');
            for (var j = 0; j < all.length; j++) {
                if (all[j].offsetHeight > 0 && all[j].offsetWidth > 30) return all[j];
            }
            return null;
        """) or comment_box

        # Step 4: Type comment
        driver.execute_script("""
            var el = arguments[0];
            var text = arguments[1];
            el.focus();
            el.textContent = '';
            document.execCommand('insertText', false, text);
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
        """, comment_box, comment_text)
        _human_delay(1, 2)

        # Verify text was entered
        entered = driver.execute_script("return (arguments[0].textContent || arguments[0].innerText || '').trim()", comment_box)
        if not entered:
            # Fallback: send_keys char by char
            try:
                ActionChains(driver).move_to_element(comment_box).click().perform()
                _human_delay(0.3, 0.5)
                for ch in comment_text:
                    comment_box.send_keys(ch)
                    _human_delay(0.02, 0.08)
            except Exception:
                pass
            _human_delay(0.5, 1)
            entered = driver.execute_script("return (arguments[0].textContent || '').trim()", comment_box)
            if not entered:
                snap(driver, "comment_text_empty")
                return False, "text_entry_failed"

        logger.info(f"Comment text entered: '{entered[:50]}'")

        # Step 5: Click post button
        post_result = driver.execute_script("""
            // data-e2e post button
            var btn = document.querySelector('[data-e2e="comment-post"]');
            if (btn && btn.offsetHeight > 0) { btn.click(); return 'data-e2e'; }

            // Any visible enabled button/div with "Post" text near comment area
            var commentArea = document.querySelector('[data-e2e="comment-input"], [class*="CommentInput"], [class*="comment-input"], [class*="BottomComment"], [class*="bottom-comment"]');
            if (commentArea) {
                var btns = commentArea.querySelectorAll('button, div[role="button"], span[role="button"]');
                for (var i = 0; i < btns.length; i++) {
                    var t = (btns[i].innerText || btns[i].getAttribute('aria-label') || '').trim().toLowerCase();
                    if ((t === 'post' || t === 'send') && btns[i].offsetHeight > 0) {
                        btns[i].click();
                        return 'area:' + t;
                    }
                }
            }

            // Global search for Post button
            var allBtns = document.querySelectorAll('button, div[role="button"]');
            for (var j = 0; j < allBtns.length; j++) {
                if (allBtns[j].offsetHeight === 0) continue;
                var txt = (allBtns[j].innerText || '').trim().toLowerCase();
                if (txt === 'post') {
                    allBtns[j].click();
                    return 'global:post';
                }
            }

            // Cursor-pointer div with "Post" inside comment section
            var allEls = document.querySelectorAll('div, span');
            for (var k = 0; k < allEls.length; k++) {
                var el = allEls[k];
                if (el.offsetHeight === 0) continue;
                var elText = (el.innerText || '').trim();
                if (elText === 'Post' && window.getComputedStyle(el).cursor === 'pointer') {
                    el.click();
                    return 'cursor:Post';
                }
            }

            return 'not_found';
        """)

        if post_result == 'not_found':
            snap(driver, "no_post_btn")
            return False, "no_post_button"

        logger.info(f"Post button clicked: {post_btn_result}")
        _human_delay(3, 5)

        # Check for errors (rate limit, blocked, etc.)
        error_text = driver.execute_script("""
            var errSels = '[class*="toast"], [class*="Toast"], [class*="snack"], [class*="Snack"], [class*="notification"], [class*="error"], [class*="Error"]';
            var els = document.querySelectorAll(errSels);
            for (var i = 0; i < els.length; i++) {
                var t = (els[i].innerText || '').trim();
                if (t.length > 2 && t.length < 300 && els[i].offsetHeight > 0) return t;
            }
            return '';
        """)

        if error_text:
            err_lower = error_text.lower()
            if any(w in err_lower for w in ["too fast", "too many", "rate", "limit", "blocked", "restricted", "muted"]):
                logger.warning(f"Comment rate limited: {error_text}")
                return False, f"rate_limited: {error_text[:100]}"
            logger.warning(f"Comment error toast: {error_text}")

        logger.info(f"Comment posted: {video_url}")
        return True, "ok"
    except Exception as e:
        logger.error(f"Comment error on {video_url}: {e}")
        try:
            snap(driver, "comment_exception")
        except Exception:
            pass
        return False, str(e)


async def run_task(task_id: int):
    task = await db.get_task(task_id)
    if not task:
        logger.error(f"Task {task_id} not found")
        return

    await db.update_task(task_id, status="running")
    logger.info(f"Starting task #{task_id}: query='{task['search_query']}', limit={task['max_comments']}")

    loop = asyncio.get_event_loop()
    comments_done = task["comments_done"]
    comments_failed = task["comments_failed"]

    try:
        # Get available account
        account = await db.get_available_account(config.MAX_COMMENTS_PER_ACCOUNT)
        if not account:
            logger.warning("No available accounts")
            await db.update_task(task_id, status="paused_no_accounts")
            return

        proxy_str = account.get("proxy", "")

        def _task_worker():
            driver = _create_driver(proxy_str)
            _active_drivers[task_id] = driver
            try:
                # Restore session from cookies
                logged_in = _restore_session_sync(driver, account["username"])
                if not logged_in:
                    return [], "login_failed"

                # Search for videos
                video_urls = _search_videos_sync(driver, task["search_query"], task["max_comments"] * 2)
                return video_urls, "ok"
            except Exception as e:
                logger.error(f"Task worker setup error: {e}")
                return [], str(e)

        video_urls, setup_status = await loop.run_in_executor(_executor, _task_worker)

        if setup_status == "login_failed":
            await db.set_account_status(account["id"], "login_failed")
            await db.update_task(task_id, status="error", finished_at=datetime.now(timezone.utc).isoformat())
            logger.error(f"Task #{task_id}: login failed for {account['username']}")
            return

        if not video_urls:
            logger.warning(f"Task #{task_id}: no videos found")
            await db.update_task(task_id, status="done", finished_at=datetime.now(timezone.utc).isoformat())
            return

        logger.info(f"Task #{task_id}: found {len(video_urls)} videos, commenting...")

        # Comment on videos using the same browser session
        for video_url in video_urls:
            if comments_done >= task["max_comments"]:
                break

            task_check = await db.get_task(task_id)
            if task_check and task_check["status"] == "cancelled":
                logger.info(f"Task #{task_id} cancelled")
                break

            def _do_comment(url=video_url):
                driver = _active_drivers.get(task_id)
                if not driver:
                    return False, "no_driver"
                return _post_comment_sync(driver, url, task["comment_text"])

            success, detail = await loop.run_in_executor(_executor, _do_comment)

            if success:
                comments_done += 1
                await db.increment_account_comments(account["id"])
                await db.log_comment(task_id, account["id"], video_url, "ok")
                logger.info(f"Task #{task_id}: comment {comments_done}/{task['max_comments']} OK")
            else:
                comments_failed += 1
                await db.log_comment(task_id, account["id"], video_url, "error", detail)
                logger.warning(f"Task #{task_id}: comment failed — {detail}")

                if "rate_limited" in detail:
                    logger.warning(f"Task #{task_id}: rate limited, pausing")
                    await db.update_task(task_id, status="paused_rate_limit",
                                        comments_done=comments_done, comments_failed=comments_failed)
                    return

            await db.update_task(task_id, comments_done=comments_done, comments_failed=comments_failed)

            delay = random.uniform(config.DELAY_MIN, config.DELAY_MAX)
            logger.info(f"Task #{task_id}: waiting {delay:.0f}s")
            await asyncio.sleep(delay)

    except Exception as e:
        logger.error(f"Critical error in task #{task_id}: {e}")
    finally:
        driver = _active_drivers.pop(task_id, None)
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
        await db.update_task(
            task_id,
            status="done",
            comments_done=comments_done,
            comments_failed=comments_failed,
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
        logger.info(f"Task #{task_id} finished: sent={comments_done}, errors={comments_failed}")
