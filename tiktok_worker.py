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
