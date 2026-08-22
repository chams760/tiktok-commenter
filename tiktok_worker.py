import asyncio
import json
import os
import random
from datetime import datetime, timezone

from loguru import logger
from playwright.async_api import async_playwright, Page, Browser, BrowserContext

import config
import database as db

SCREENSHOTS_DIR = os.path.join(os.path.dirname(__file__), "screenshots")
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)

SESSIONS_DIR = os.path.join(os.path.dirname(__file__), "sessions")
os.makedirs(SESSIONS_DIR, exist_ok=True)

_active_pages: dict[int, Page] = {}

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
_VIEWPORT = {"width": 1280, "height": 720}


async def _human_delay(min_ms=800, max_ms=2500):
    await asyncio.sleep(random.uniform(min_ms, max_ms) / 1000)


async def _random_mouse_move(page: Page, steps=3):
    vp = page.viewport_size or {"width": 1280, "height": 720}
    for _ in range(steps):
        x = random.randint(100, vp["width"] - 100)
        y = random.randint(100, vp["height"] - 100)
        await page.mouse.move(x, y, steps=random.randint(5, 15))
        await asyncio.sleep(random.uniform(0.1, 0.4))


async def _human_click(page: Page, selector: str):
    el = page.locator(selector)
    box = await el.bounding_box()
    if box:
        x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
        y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
        await page.mouse.move(x, y, steps=random.randint(8, 20))
        await asyncio.sleep(random.uniform(0.05, 0.15))
        await page.mouse.click(x, y)
    else:
        await el.click()


async def _human_type(page: Page, selector: str, text: str):
    el = page.locator(selector)
    await _human_click(page, selector)
    await _human_delay(300, 600)
    for char in text:
        await el.type(char, delay=random.randint(20, 60))
    await _human_delay(200, 500)


async def _detect_captcha(page: Page) -> str | None:
    checks = [
        ('[class*="captcha"]', "CAPTCHA element detected"),
        ('[id*="captcha"]', "CAPTCHA element detected"),
        ('iframe[src*="captcha"]', "CAPTCHA iframe detected"),
        ('[class*="Slider"]', "Slider puzzle detected"),
        ('[class*="slider"]', "Slider puzzle detected"),
        ('[class*="verify"]', "Verification challenge detected"),
        ('[class*="Verify"]', "Verification challenge detected"),
        ('[class*="puzzle"]', "Puzzle captcha detected"),
        ('[class*="secsdk"]', "TikTok security SDK challenge detected"),
    ]
    for selector, msg in checks:
        count = await page.locator(selector).count()
        if count > 0:
            return msg
    return None


_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {
    get: () => undefined,
    configurable: true,
});
delete navigator.__proto__.webdriver;

Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 4});
Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});
Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});
Object.defineProperty(navigator, 'maxTouchPoints', {get: () => 0});

window.chrome = {
    runtime: { onConnect: { addListener: () => {}, removeListener: () => {} } },
    loadTimes: () => ({
        commitLoadTime: Date.now() / 1000,
        connectionInfo: 'h2',
        finishDocumentLoadTime: Date.now() / 1000 + 0.1,
        finishLoadTime: Date.now() / 1000 + 0.2,
        firstPaintAfterLoadTime: 0,
        firstPaintTime: Date.now() / 1000 + 0.05,
        navigationType: 'Other',
        npnNegotiatedProtocol: 'h2',
        requestTime: Date.now() / 1000 - 0.3,
        startLoadTime: Date.now() / 1000 - 0.2,
        wasAlternateProtocolAvailable: false,
        wasFetchedViaSpdy: true,
        wasNpnNegotiated: true,
    }),
    csi: () => ({ pageT: Date.now(), startE: Date.now(), onloadT: Date.now() }),
};

if (window.navigator.permissions) {
    const originalQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (parameters) =>
        parameters.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : originalQuery(parameters);
}

const origGetParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(param) {
    if (param === 37445) return 'Intel Inc.';
    if (param === 37446) return 'Intel Iris OpenGL Engine';
    return origGetParameter.call(this, param);
};

Object.defineProperty(navigator, 'plugins', {
    get: () => {
        const plugins = [
            { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
            { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
            { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' },
        ];
        plugins.length = 3;
        return plugins;
    },
});

const originalCall = Function.prototype.call;
function isNativeCheck(fn) {
    return typeof fn === 'function' && /\\[native code\\]/.test(Function.prototype.toString.call(fn));
}
"""


async def _apply_stealth(page: Page):
    await page.add_init_script(_STEALTH_JS)


async def _create_stealth_context(browser: Browser, proxy_config: dict | None = None) -> BrowserContext:
    ctx = await browser.new_context(
        viewport=_VIEWPORT,
        user_agent=_UA,
        locale="en-US",
        color_scheme="light",
    )
    await ctx.add_init_script(_STEALTH_JS)
    return ctx


def _session_path(username: str) -> str:
    safe = username.replace("@", "_at_").replace(".", "_")
    return os.path.join(SESSIONS_DIR, f"{safe}.json")


async def _save_cookies(ctx: BrowserContext, username: str):
    try:
        cookies = await ctx.cookies()
        with open(_session_path(username), "w") as f:
            json.dump(cookies, f)
        logger.info(f"Cookies сохранены для {username}")
    except Exception as e:
        logger.warning(f"Не удалось сохранить cookies {username}: {e}")


async def _load_cookies(ctx: BrowserContext, username: str) -> bool:
    path = _session_path(username)
    if not os.path.exists(path):
        return False
    try:
        with open(path) as f:
            cookies = json.load(f)
        await ctx.add_cookies(cookies)
        logger.info(f"Cookies загружены для {username}")
        return True
    except Exception as e:
        logger.warning(f"Не удалось загрузить cookies {username}: {e}")
        return False


async def _check_logged_in(page: Page) -> bool:
    try:
        await page.goto("https://www.tiktok.com/foryou", wait_until="domcontentloaded")
        await _human_delay(3000, 5000)
        if "login" in page.url.lower():
            return False
        avatar = await page.locator('[data-e2e="profile-icon"], [class*="avatar"], [class*="Avatar"]').count()
        return avatar > 0
    except Exception:
        return False


async def _warmup(page: Page):
    try:
        await page.goto("https://www.tiktok.com", wait_until="domcontentloaded")
        await _human_delay(2000, 4000)
        for _ in range(random.randint(1, 3)):
            await page.evaluate("window.scrollBy(0, window.innerHeight * 0.5)")
            await _human_delay(1000, 2500)
    except Exception:
        pass


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


def get_active_page(task_id: int) -> Page | None:
    return _active_pages.get(task_id)


def get_all_active_tasks() -> list[int]:
    return list(_active_pages.keys())


async def take_debug_screenshot(task_id: int | None = None) -> str | None:
    if task_id is not None:
        page = _active_pages.get(task_id)
        if page and not page.is_closed():
            path = os.path.join(SCREENSHOTS_DIR, f"debug_task{task_id}_{int(datetime.now(timezone.utc).timestamp())}.png")
            await page.screenshot(path=path, full_page=False)
            logger.info(f"Скриншот задачи #{task_id}: {path}")
            return path
        return None

    for tid, page in _active_pages.items():
        if page and not page.is_closed():
            path = os.path.join(SCREENSHOTS_DIR, f"debug_task{tid}_{int(datetime.now(timezone.utc).timestamp())}.png")
            await page.screenshot(path=path, full_page=False)
            logger.info(f"Скриншот задачи #{tid}: {path}")
            return path
    return None


async def test_login(username: str, password: str, proxy: str = "") -> list[dict]:
    steps = []
    ts = int(datetime.now(timezone.utc).timestamp())
    proxy_config = parse_proxy(proxy)

    async def snap(page: Page, step_name: str, note: str = ""):
        fname = f"test_{ts}_{len(steps)}_{step_name}.png"
        path = os.path.join(SCREENSHOTS_DIR, fname)
        try:
            await page.screenshot(path=path, full_page=False)
        except Exception:
            pass
        info = note
        if proxy_config and step_name == "login_page":
            info += f" | Proxy: {proxy_config['server']}"
        steps.append({"step": step_name, "file": fname, "note": info, "url": page.url})

    async with async_playwright() as pw:
        launch_opts = {
            "headless": True,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--no-sandbox",
                "--window-size=1280,720",
            ],
            "ignore_default_args": ["--enable-automation"],
        }
        if proxy_config:
            launch_opts["proxy"] = proxy_config
        browser = await pw.chromium.launch(**launch_opts)
        ctx = await _create_stealth_context(browser, proxy_config)
        page = await ctx.new_page()
        await _apply_stealth(page)

        try:
            try:
                await page.goto("https://api.ipify.org?format=json", wait_until="domcontentloaded")
                await _human_delay(1000, 2000)
                ip_text = await page.locator("body").inner_text()
                proxy_note = f"IP: {ip_text}"
                if proxy_config:
                    proxy_note += f" | Proxy: {proxy_config['server']}"
                else:
                    proxy_note += " | NO PROXY"
                await snap(page, "ip_check", proxy_note)
            except Exception:
                await snap(page, "ip_check", "Could not check IP")

            has_cookies = await _load_cookies(ctx, username)
            if has_cookies:
                if await _check_logged_in(page):
                    await snap(page, "cookie_login", "LOGIN SUCCESS via saved cookies")
                    return steps
                await snap(page, "cookies_expired", "Saved cookies expired, logging in fresh")

            await _warmup(page)
            await snap(page, "warmup", "Warmup: visited TikTok homepage")
            await _human_delay(1000, 2000)

            await page.goto("https://www.tiktok.com/login/phone-or-email/email", wait_until="domcontentloaded")
            await _human_delay(3000, 5000)
            await _random_mouse_move(page, steps=random.randint(2, 4))
            await snap(page, "login_page", "Login page loaded (stealth mode)")

            captcha_pre = await _detect_captcha(page)
            if captcha_pre:
                await snap(page, "captcha_before_login", f"BLOCKED BEFORE LOGIN: {captcha_pre}")
                return steps

            email_input = page.locator('input[name="username"]')
            count = await email_input.count()
            if count == 0:
                await snap(page, "no_email_field", "Email input not found on page")
                return steps

            await _random_mouse_move(page, 2)
            await _human_type(page, 'input[name="username"]', username)
            await snap(page, "email_filled", f"Email filled: {username}")

            pass_input = page.locator('input[type="password"]')
            count = await pass_input.count()
            if count == 0:
                await snap(page, "no_pass_field", "Password input not found")
                return steps

            await _random_mouse_move(page, 1)
            await _human_type(page, 'input[type="password"]', password)
            await snap(page, "password_filled", "Password filled")

            await _random_mouse_move(page, 2)
            await _human_delay(500, 1200)

            login_btn = page.locator('button[data-e2e="login-button"]')
            count = await login_btn.count()
            if count == 0:
                all_buttons = await page.locator('button[type="submit"]').count()
                await snap(page, "no_login_btn", f"Login button not found. Submit buttons on page: {all_buttons}")
                return steps

            await _human_click(page, 'button[data-e2e="login-button"]')
            await _human_delay(3000, 5000)
            await snap(page, "after_click", "After clicking login button")

            captcha_post = await _detect_captcha(page)
            if captcha_post:
                await snap(page, "captcha_after_click", f"CAPTCHA APPEARED: {captcha_post}")
                await _human_delay(3000, 5000)
                await snap(page, "captcha_wait", "Waiting for captcha timeout...")

            await _human_delay(5000, 8000)
            await snap(page, "final_state", f"Final URL: {page.url}")

            if "login" not in page.url.lower():
                steps[-1]["note"] = "LOGIN SUCCESS - redirected away from login page"
                await _save_cookies(ctx, username)
            else:
                captcha_detail = await _detect_captcha(page)
                error_el = await page.locator('[class*="error"], [class*="Error"]').count()
                page_text = await page.locator("body").inner_text()
                note = "LOGIN FAILED - still on login page."
                if captcha_detail:
                    note += f" {captcha_detail}"
                if "maximum" in page_text.lower() or "too many" in page_text.lower():
                    note += " RATE LIMITED: Too many attempts. Wait 1-2 hours."
                if error_el > 0:
                    try:
                        err_text = await page.locator('[class*="error"], [class*="Error"]').first.text_content()
                        note += f" Error: {err_text}"
                    except Exception:
                        note += " Error element found."
                steps[-1]["note"] = note

        except Exception as e:
            await snap(page, "exception", f"Error: {str(e)}")
        finally:
            await browser.close()

    return steps


async def login_account(ctx: BrowserContext, page: Page, username: str, password: str) -> bool:
    try:
        has_cookies = await _load_cookies(ctx, username)
        if has_cookies:
            if await _check_logged_in(page):
                logger.info(f"Вход через cookies: {username}")
                return True
            logger.info(f"Cookies устарели для {username}, логин заново")

        await _warmup(page)
        await _human_delay(1000, 2000)

        await page.goto("https://www.tiktok.com/login/phone-or-email/email", wait_until="domcontentloaded")
        await _human_delay(3000, 5000)
        await _random_mouse_move(page, random.randint(2, 4))

        await _random_mouse_move(page, 2)
        await _human_type(page, 'input[name="username"]', username)
        await _random_mouse_move(page, 1)
        await _human_type(page, 'input[type="password"]', password)
        await _random_mouse_move(page, 2)
        await _human_delay(500, 1200)

        await _human_click(page, 'button[data-e2e="login-button"]')

        await _human_delay(5000, 8000)

        captcha = await _detect_captcha(page)
        if captcha:
            logger.warning(f"Капча при входе {username}: {captcha}")
            await _human_delay(5000, 8000)

        if "login" not in page.url.lower():
            logger.info(f"Вход выполнен: {username}")
            await _save_cookies(ctx, username)
            return True

        body_text = await page.locator("body").inner_text()
        if "maximum" in body_text.lower() or "too many" in body_text.lower():
            logger.error(f"Rate limit для {username} — слишком много попыток")
        elif captcha:
            logger.error(f"Вход заблокирован капчей для {username}")
        else:
            logger.warning(f"Не удалось войти: {username}")
        return False
    except Exception as e:
        logger.error(f"Ошибка входа {username}: {e}")
        return False


async def search_videos(page: Page, query: str, max_results: int = 50) -> list[str]:
    urls = []
    try:
        search_url = f"https://www.tiktok.com/search/video?q={query}"
        await page.goto(search_url)
        await page.wait_for_timeout(4000)

        scroll_count = max(max_results // 10, 3)
        for _ in range(scroll_count):
            await page.evaluate("window.scrollBy(0, window.innerHeight)")
            await page.wait_for_timeout(random.uniform(1.5, 3.0) * 1000)

        links = await page.locator('a[href*="/video/"]').all()
        seen = set()
        for link in links:
            href = await link.get_attribute("href")
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


async def post_comment(page: Page, video_url: str, comment_text: str) -> bool:
    try:
        await page.goto(video_url)
        await page.wait_for_timeout(random.uniform(3.0, 5.0) * 1000)

        comment_box = page.locator('div[data-e2e="comment-input"] div[contenteditable="true"]')
        await comment_box.click()
        await page.wait_for_timeout(500)
        await comment_box.fill(comment_text)
        await page.wait_for_timeout(1000)

        post_btn = page.locator('div[data-e2e="comment-post"]')
        await post_btn.click()
        await page.wait_for_timeout(3000)

        logger.info(f"Комментарий отправлен: {video_url}")
        return True
    except Exception as e:
        logger.error(f"Ошибка комментария на {video_url}: {e}")
        return False


async def run_task(task_id: int):
    task = await db.get_task(task_id)
    if not task:
        logger.error(f"Задача {task_id} не найдена")
        return

    await db.update_task(task_id, status="running")
    logger.info(f"Запуск задачи #{task_id}: запрос='{task['search_query']}', лимит={task['max_comments']}")

    async with async_playwright() as pw:
        comments_done = task["comments_done"]
        comments_failed = task["comments_failed"]

        try:
            browser = await pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-infobars",
                    "--no-sandbox",
                ],
                ignore_default_args=["--enable-automation"],
            )
            probe_ctx = await _create_stealth_context(browser)
            probe_page = await probe_ctx.new_page()
            await _apply_stealth(probe_page)
            video_urls = await search_videos(probe_page, task["search_query"], task["max_comments"])
            await probe_ctx.close()
            await browser.close()

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
                    logger.warning("Нет доступных аккаунтов, ожидание...")
                    await db.update_task(task_id, status="paused_no_accounts")
                    return

                proxy_config = parse_proxy(account.get("proxy", ""))
                launch_opts = {
                    "headless": True,
                    "args": [
                        "--disable-blink-features=AutomationControlled",
                        "--disable-infobars",
                        "--no-sandbox",
                    ],
                    "ignore_default_args": ["--enable-automation"],
                }
                if proxy_config:
                    launch_opts["proxy"] = proxy_config
                    logger.info(f"Используется прокси: {proxy_config['server']} для {account['username']}")

                browser = await pw.chromium.launch(**launch_opts)
                ctx = await _create_stealth_context(browser, proxy_config)
                page = await ctx.new_page()
                await _apply_stealth(page)

                _active_pages[task_id] = page

                logged_in = await login_account(ctx, page, account["username"], account["password"])
                if not logged_in:
                    await db.set_account_status(account["id"], "login_failed")
                    await db.log_comment(task_id, account["id"], video_url, "error", "login_failed")
                    comments_failed += 1
                    try:
                        err_path = os.path.join(SCREENSHOTS_DIR, f"login_fail_{account['id']}_{int(datetime.now(timezone.utc).timestamp())}.png")
                        await page.screenshot(path=err_path, full_page=False)
                    except Exception:
                        pass
                    _active_pages.pop(task_id, None)
                    await ctx.close()
                    continue

                success = await post_comment(page, video_url, task["comment_text"])
                if success:
                    comments_done += 1
                    await db.increment_account_comments(account["id"])
                    await db.log_comment(task_id, account["id"], video_url, "ok")
                else:
                    comments_failed += 1
                    await db.log_comment(task_id, account["id"], video_url, "error", "comment_failed")
                    try:
                        err_path = os.path.join(SCREENSHOTS_DIR, f"comment_fail_{int(datetime.now(timezone.utc).timestamp())}.png")
                        await page.screenshot(path=err_path, full_page=False)
                    except Exception:
                        pass

                await db.update_task(
                    task_id,
                    comments_done=comments_done,
                    comments_failed=comments_failed,
                )

                _active_pages.pop(task_id, None)
                await ctx.close()
                await browser.close()

                delay = random.uniform(config.DELAY_MIN, config.DELAY_MAX)
                logger.info(f"Задержка {delay:.0f}с перед следующим комментарием")
                await asyncio.sleep(delay)

        except Exception as e:
            logger.error(f"Критическая ошибка задачи #{task_id}: {e}")
        finally:
            _active_pages.pop(task_id, None)
            await db.update_task(
                task_id,
                status="done",
                comments_done=comments_done,
                comments_failed=comments_failed,
                finished_at=datetime.now(timezone.utc).isoformat(),
            )
            logger.info(
                f"Задача #{task_id} завершена: отправлено={comments_done}, ошибок={comments_failed}"
            )
