import asyncio
import os
import random
from datetime import datetime, timezone

from loguru import logger
from playwright.async_api import async_playwright, Page, Browser

import config
import database as db

SCREENSHOTS_DIR = os.path.join(os.path.dirname(__file__), "screenshots")
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)

_active_pages: dict[int, Page] = {}


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


async def test_login(username: str, password: str) -> list[dict]:
    steps = []
    ts = int(datetime.now(timezone.utc).timestamp())

    async def snap(page: Page, step_name: str, note: str = ""):
        fname = f"test_{ts}_{len(steps)}_{step_name}.png"
        path = os.path.join(SCREENSHOTS_DIR, fname)
        try:
            await page.screenshot(path=path, full_page=False)
        except Exception:
            pass
        steps.append({"step": step_name, "file": fname, "note": note, "url": page.url})

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
        )
        page = await ctx.new_page()

        try:
            await page.goto("https://www.tiktok.com/login/phone-or-email/email")
            await page.wait_for_timeout(4000)
            await snap(page, "login_page", "Login page loaded")

            email_input = page.locator('input[name="username"]')
            count = await email_input.count()
            if count == 0:
                await snap(page, "no_email_field", "Email input not found on page")
                return steps

            await email_input.fill(username)
            await page.wait_for_timeout(500)
            await snap(page, "email_filled", f"Email filled: {username}")

            pass_input = page.locator('input[type="password"]')
            count = await pass_input.count()
            if count == 0:
                await snap(page, "no_pass_field", "Password input not found")
                return steps

            await pass_input.fill(password)
            await page.wait_for_timeout(500)
            await snap(page, "password_filled", "Password filled")

            login_btn = page.locator('button[data-e2e="login-button"]')
            count = await login_btn.count()
            if count == 0:
                all_buttons = await page.locator('button[type="submit"]').count()
                await snap(page, "no_login_btn", f"Login button not found. Submit buttons on page: {all_buttons}")
                return steps

            await login_btn.click()
            await page.wait_for_timeout(3000)
            await snap(page, "after_click", "After clicking login button")

            await page.wait_for_timeout(5000)
            await snap(page, "final_state", f"Final URL: {page.url}")

            if "login" not in page.url.lower():
                steps[-1]["note"] = "LOGIN SUCCESS - redirected away from login page"
            else:
                captcha = await page.locator('[class*="captcha"], [id*="captcha"], iframe[src*="captcha"]').count()
                error_el = await page.locator('[class*="error"], [class*="Error"]').count()
                note = "LOGIN FAILED - still on login page."
                if captcha > 0:
                    note += " CAPTCHA detected!"
                if error_el > 0:
                    try:
                        err_text = await page.locator('[class*="error"], [class*="Error"]').first.text_content()
                        note += f" Error text: {err_text}"
                    except Exception:
                        note += " Error element found."
                steps[-1]["note"] = note

        except Exception as e:
            await snap(page, "exception", f"Error: {str(e)}")
        finally:
            await browser.close()

    return steps


async def login_account(page: Page, username: str, password: str) -> bool:
    try:
        await page.goto("https://www.tiktok.com/login/phone-or-email/email")
        await page.wait_for_timeout(3000)

        email_input = page.locator('input[name="username"]')
        await email_input.fill(username)

        pass_input = page.locator('input[type="password"]')
        await pass_input.fill(password)

        login_btn = page.locator('button[data-e2e="login-button"]')
        await login_btn.click()

        await page.wait_for_timeout(5000)

        if "login" not in page.url.lower():
            logger.info(f"Вход выполнен: {username}")
            return True

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
        browser = await pw.chromium.launch(headless=True)
        comments_done = task["comments_done"]
        comments_failed = task["comments_failed"]

        try:
            video_urls = []
            probe_ctx = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
            )
            probe_page = await probe_ctx.new_page()
            video_urls = await search_videos(probe_page, task["search_query"], task["max_comments"])
            await probe_ctx.close()

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

                ctx = await browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                    viewport={"width": 1920, "height": 1080},
                )
                page = await ctx.new_page()

                _active_pages[task_id] = page

                logged_in = await login_account(page, account["username"], account["password"])
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

                delay = random.uniform(config.DELAY_MIN, config.DELAY_MAX)
                logger.info(f"Задержка {delay:.0f}с перед следующим комментарием")
                await asyncio.sleep(delay)

        except Exception as e:
            logger.error(f"Критическая ошибка задачи #{task_id}: {e}")
        finally:
            _active_pages.pop(task_id, None)
            await browser.close()
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
