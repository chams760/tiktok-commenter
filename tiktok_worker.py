import asyncio
import json
import os
import random
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

SCREENSHOTS_DIR = os.path.join(os.path.dirname(__file__), "screenshots")
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)

SESSIONS_DIR = os.path.join(os.path.dirname(__file__), "sessions")
os.makedirs(SESSIONS_DIR, exist_ok=True)

_executor = ThreadPoolExecutor(max_workers=3)
_active_drivers: dict[int, uc.Chrome] = {}

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"


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


def _create_driver(proxy_str: str = "") -> uc.Chrome:
    options = uc.ChromeOptions()
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1280,720")
    options.add_argument(f"--user-agent={UA}")
    options.add_argument("--lang=en-US")
    options.add_argument("--disable-blink-features=AutomationControlled")

    if proxy_str and proxy_str.strip():
        proxy = _parse_proxy_for_selenium(proxy_str)
        if proxy:
            options.add_argument(f"--proxy-server={proxy}")

    chrome_ver = None
    try:
        import subprocess
        out = subprocess.check_output(["google-chrome-stable", "--version"], text=True)
        chrome_ver = int(out.strip().split()[-1].split(".")[0])
        logger.debug(f"Chrome version detected: {chrome_ver}")
    except Exception:
        pass

    driver = uc.Chrome(
        options=options,
        headless=True,
        use_subprocess=True,
        version_main=chrome_ver,
    )
    driver.set_window_size(1280, 720)
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


def _test_login_sync(username: str, password: str, proxy: str = "") -> list[dict]:
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
        snap(driver, "warmup", "Visited TikTok homepage")

        driver.get("https://www.tiktok.com/login/phone-or-email/email")
        _human_delay(3, 5)
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
        snap(driver, "after_click", "After clicking login button")

        _human_delay(5, 8)
        snap(driver, "final_state", f"Final URL: {driver.current_url}")

        if "login" not in driver.current_url.lower():
            steps[-1]["note"] = "LOGIN SUCCESS - redirected away from login page"
            _save_cookies(driver, username)
        else:
            body_text = driver.find_element(By.TAG_NAME, "body").text
            note = "LOGIN FAILED - still on login page."
            if "maximum" in body_text.lower() or "too many" in body_text.lower():
                note += " RATE LIMITED: Too many attempts."
            if "captcha" in driver.page_source.lower() or "verify" in driver.page_source.lower():
                note += " CAPTCHA/Verification detected."
            steps[-1]["note"] = note

    except Exception as e:
        if driver:
            snap(driver, "exception", f"Error: {str(e)}")
        else:
            steps.append({"step": "exception", "file": "", "note": f"Driver failed: {str(e)}", "url": ""})
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

    return steps


async def test_login(username: str, password: str, proxy: str = "") -> list[dict]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, _test_login_sync, username, password, proxy)


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
