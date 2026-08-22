import asyncio
import json
import io
import os
import sys
from datetime import datetime, timezone
from aiohttp import web

from loguru import logger

logger.remove()
logger.add(sys.stderr, level="INFO")
logger.add("logs/bot_{time}.log", rotation="1 day", retention="7 days", level="INFO")


async def health_handler(request):
    try:
        import database as db
        stats = await db.get_stats()
        return web.json_response({
            "status": "ok",
            "accounts": stats["accounts_active"],
            "comments_sent": stats["comments_sent"],
            "active_tasks": stats["active_tasks"],
        })
    except Exception:
        return web.json_response({"status": "ok"})


async def dashboard_handler(request):
    try:
        import database as db
        stats = await db.get_stats()
    except Exception:
        stats = {"accounts_active": 0, "accounts_total": 0, "comments_sent": 0, "comments_errors": 0, "active_tasks": 0}
    html = f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>TikTok Commenter</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Inter',sans-serif;background:#0C0C0E;color:#E4E4E8;min-height:100vh;display:flex;align-items:center;justify-content:center}}
.w{{max-width:400px;width:100%;padding:32px 24px}}
h1{{font-size:22px;font-weight:700;letter-spacing:-0.3px;margin-bottom:4px}}
.sub{{font-size:13px;color:#5C5C66;margin-bottom:28px}}
.g{{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:20px}}
.s{{background:#161619;border:1px solid #2A2A30;border-radius:10px;padding:16px}}
.sv{{font-size:28px;font-weight:700;font-variant-numeric:tabular-nums}}
.sl{{font-size:11px;color:#5C5C66;text-transform:uppercase;letter-spacing:0.5px;margin-top:4px}}
.sa .sv{{color:#E84D3A}}.sg .sv{{color:#2DB86A}}
.b{{display:inline-flex;align-items:center;gap:6px;padding:6px 12px;border-radius:6px;font-size:12px;font-weight:500;background:rgba(45,184,106,0.12);color:#2DB86A}}
.d{{width:6px;height:6px;border-radius:50%;background:#2DB86A;animation:p 2s infinite}}
@keyframes p{{0%,100%{{opacity:1}}50%{{opacity:.3}}}}
</style></head><body><div class="w">
<h1>TikTok Commenter</h1><p class="sub">Automation panel</p>
<div class="g">
<div class="s sa"><div class="sv">{stats['comments_sent']}</div><div class="sl">Sent</div></div>
<div class="s"><div class="sv">{stats['comments_errors']}</div><div class="sl">Errors</div></div>
<div class="s sg"><div class="sv">{stats['accounts_active']}</div><div class="sl">Accounts</div></div>
<div class="s"><div class="sv">{stats['active_tasks']}</div><div class="sl">Tasks</div></div>
</div><div class="b"><span class="d"></span>Bot running</div>
</div></body></html>"""
    return web.Response(text=html, content_type="text/html")


async def webapp_handler(request):
    webapp_path = os.path.join(os.path.dirname(__file__), "webapp", "index.html")
    if os.path.exists(webapp_path):
        return web.FileResponse(webapp_path)
    return web.Response(text="WebApp not found", status=404)


_bot_ref = None
_task_runner = None


async def api_stats(request):
    try:
        import database as db
        stats = await db.get_stats()
        accounts = await db.get_accounts()
        tasks = await db.get_all_tasks(limit=10)
        return web.json_response({
            "stats": stats,
            "accounts": [{"id": a["id"], "username": a["username"], "status": a["status"], "comments_today": a["comments_today"], "proxy": a.get("proxy", "")} for a in accounts],
            "tasks": tasks,
        })
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def api_create_task(request):
    try:
        import database as db
        from tiktok_worker import run_task
        data = await request.json()
        query = data.get("query", "").strip()
        comment = data.get("comment", "").strip()
        limit = int(data.get("limit", 100))
        if not query or not comment:
            return web.json_response({"error": "query and comment required"}, status=400)
        accounts = await db.get_accounts()
        if not accounts:
            return web.json_response({"error": "no accounts loaded"}, status=400)
        task_id = await db.create_task(query, comment, limit)
        asyncio.create_task(run_task(task_id))
        if _bot_ref:
            import config
            for admin_id in config.ADMIN_IDS:
                try:
                    await _bot_ref.send_message(admin_id, f"Задача #{task_id} создана через WebApp\n\nЗапрос: {query}\nКомментарий: {comment}\nЛимит: {limit}")
                except Exception:
                    pass
        return web.json_response({"ok": True, "task_id": task_id})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def api_upload_accounts(request):
    try:
        import database as db
        data = await request.json()
        raw = data.get("accounts", "")
        lines = [l.strip() for l in raw.splitlines() if ":" in l]
        accounts = []
        for l in lines:
            parts = l.split(":")
            if len(parts) >= 2:
                user = parts[0].strip()
                passw = parts[1].strip()
                proxy = ":".join(parts[2:]).strip() if len(parts) > 2 else ""
                accounts.append((user, passw, proxy))
        if not accounts:
            return web.json_response({"error": "no valid accounts"}, status=400)
        await db.add_accounts(accounts)
        return web.json_response({"ok": True, "count": len(accounts)})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def api_delete_accounts(request):
    try:
        import database as db
        await db.delete_all_accounts()
        return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def api_cancel_task(request):
    try:
        import database as db
        data = await request.json()
        task_id = int(data.get("task_id", 0))
        if not task_id:
            return web.json_response({"error": "task_id required"}, status=400)
        await db.update_task(task_id, status="cancelled")
        return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def api_test_login(request):
    try:
        import database as db
        from tiktok_worker import test_login
        data = await request.json()
        account_id = data.get("account_id")
        proxy = data.get("proxy", "")
        if account_id:
            accounts = await db.get_accounts()
            acc = next((a for a in accounts if a["id"] == int(account_id)), None)
            if not acc:
                return web.json_response({"error": "account not found"}, status=404)
            username, password = acc["username"], acc["password"]
            proxy = acc.get("proxy", "") or proxy
        else:
            username = data.get("username", "").strip()
            password = data.get("password", "").strip()
        if not username or not password:
            return web.json_response({"error": "username and password required"}, status=400)
        steps = await test_login(username, password, proxy)
        return web.json_response({"ok": True, "steps": steps, "username": username, "proxy": bool(proxy)})
    except Exception as e:
        logger.error(f"Test login error: {e}")
        return web.json_response({"error": str(e)}, status=500)


async def api_screenshot(request):
    try:
        from tiktok_worker import take_debug_screenshot, get_all_active_tasks
        data = await request.json()
        task_id = data.get("task_id")
        if task_id:
            task_id = int(task_id)

        active = get_all_active_tasks()
        if not active:
            return web.json_response({"error": "no active browser sessions"}, status=404)

        if task_id is None:
            task_id = active[0]

        path = await take_debug_screenshot(task_id)
        if path and os.path.exists(path):
            return web.FileResponse(path, headers={"Content-Type": "image/png"})
        return web.json_response({"error": "screenshot failed, browser may be between pages"}, status=404)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def api_errors(request):
    screenshots_dir = os.path.join(os.path.dirname(__file__), "screenshots")
    if not os.path.exists(screenshots_dir):
        return web.json_response({"files": []})
    files = sorted(
        [f for f in os.listdir(screenshots_dir) if f.endswith(".png")],
        key=lambda x: os.path.getmtime(os.path.join(screenshots_dir, x)),
        reverse=True,
    )
    return web.json_response({"files": files[:10]})


async def api_error_image(request):
    name = request.match_info.get("name", "")
    if "/" in name or ".." in name:
        return web.json_response({"error": "invalid"}, status=400)
    screenshots_dir = os.path.join(os.path.dirname(__file__), "screenshots")
    path = os.path.join(screenshots_dir, name)
    if os.path.exists(path) and name.endswith(".png"):
        return web.FileResponse(path, headers={"Content-Type": "image/png"})
    return web.json_response({"error": "not found"}, status=404)


async def start_health_server():
    app = web.Application()
    app.router.add_get("/", webapp_handler)
    app.router.add_get("/health", health_handler)
    app.router.add_get("/dashboard", dashboard_handler)
    app.router.add_get("/webapp", webapp_handler)
    app.router.add_get("/api/stats", api_stats)
    app.router.add_post("/api/test-login", api_test_login)
    app.router.add_post("/api/screenshot", api_screenshot)
    app.router.add_get("/api/errors", api_errors)
    app.router.add_get("/api/errors/{name}", api_error_image)
    app.router.add_post("/api/task", api_create_task)
    app.router.add_post("/api/accounts", api_upload_accounts)
    app.router.add_delete("/api/accounts", api_delete_accounts)
    app.router.add_post("/api/cancel", api_cancel_task)
    port = int(os.environ.get("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Health-сервер запущен на порту {port}")
    return runner


async def start_bot():
    import config
    import database as db

    if not config.BOT_TOKEN:
        logger.error("BOT_TOKEN не задан! Health-сервер работает, бот — нет.")
        return

    global _bot_ref
    from aiogram import Bot, Dispatcher, F, types
    from aiogram.filters import Command
    from aiogram.types import (
        InlineKeyboardButton,
        InlineKeyboardMarkup,
        WebAppInfo,
        BufferedInputFile,
    )
    from tiktok_worker import run_task, take_debug_screenshot, get_all_active_tasks

    bot = Bot(token=config.BOT_TOKEN)
    _bot_ref = bot
    dp = Dispatcher()

    def is_admin(user_id: int) -> bool:
        return not config.ADMIN_IDS or user_id in config.ADMIN_IDS

    @dp.message(Command("start"))
    async def cmd_start(msg: types.Message):
        if not is_admin(msg.from_user.id):
            await msg.answer("Доступ запрещён.")
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Новая задача", callback_data="new_task")],
            [InlineKeyboardButton(text="👥 Аккаунты", callback_data="accounts")],
            [InlineKeyboardButton(text="📊 Статистика", callback_data="stats")],
            [InlineKeyboardButton(text="📜 Задачи", callback_data="tasks")],
        ])
        if config.WEBAPP_URL:
            kb.inline_keyboard.insert(0, [
                InlineKeyboardButton(text="🌐 Панель управления", web_app=WebAppInfo(url=config.WEBAPP_URL))
            ])
        await msg.answer(
            "TikTok Commenter Bot\n\n"
            "Управление автоматическими комментариями в TikTok.\n"
            "Выберите действие:",
            reply_markup=kb,
        )

    @dp.callback_query(F.data == "new_task")
    async def cb_new_task(cb: types.CallbackQuery):
        if not is_admin(cb.from_user.id):
            return
        await cb.message.answer(
            "Для создания задачи отправьте сообщение в формате:\n\n"
            "/task <поисковый запрос> | <текст комментария> | <лимит>\n\n"
            "Пример:\n"
            "/task как заработать в интернете | Подпишись на мой канал! | 50"
        )
        await cb.answer()

    @dp.message(Command("task"))
    async def cmd_task(msg: types.Message):
        if not is_admin(msg.from_user.id):
            return
        text = msg.text.replace("/task", "", 1).strip()
        parts = [p.strip() for p in text.split("|")]
        if len(parts) < 2:
            await msg.answer(
                "Неверный формат. Используйте:\n"
                "/task запрос | комментарий | лимит\n\n"
                "Лимит необязателен (по умолчанию 100)."
            )
            return
        query = parts[0]
        comment = parts[1]
        limit = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 100
        accounts = await db.get_accounts()
        if not accounts:
            await msg.answer("Нет активных аккаунтов. Сначала загрузите аккаунты через /upload.")
            return
        task_id = await db.create_task(query, comment, limit)
        await msg.answer(
            f"Задача #{task_id} создана\n\n"
            f"Запрос: {query}\n"
            f"Комментарий: {comment}\n"
            f"Лимит: {limit}\n"
            f"Аккаунтов: {len(accounts)}\n\n"
            "Запуск...",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отменить", callback_data=f"cancel_{task_id}")]
            ]),
        )
        asyncio.create_task(run_task(task_id))

    @dp.callback_query(F.data.startswith("cancel_"))
    async def cb_cancel_task(cb: types.CallbackQuery):
        if not is_admin(cb.from_user.id):
            return
        task_id = int(cb.data.split("_")[1])
        await db.update_task(task_id, status="cancelled")
        await cb.message.answer(f"Задача #{task_id} отменена.")
        await cb.answer()

    @dp.callback_query(F.data == "accounts")
    async def cb_accounts(cb: types.CallbackQuery):
        if not is_admin(cb.from_user.id):
            return
        accounts = await db.get_accounts()
        total = len(accounts)
        if total == 0:
            text = "Нет загруженных аккаунтов.\n\nОтправьте файл .txt с аккаунтами (формат: login:password, по одному на строку)."
        else:
            lines = [f"Аккаунтов: {total}\n"]
            for acc in accounts[:20]:
                status_icon = "✅" if acc["status"] == "active" else "❌"
                lines.append(f"{status_icon} {acc['username']} — {acc['comments_today']} комм. сегодня")
            if total > 20:
                lines.append(f"\n... и ещё {total - 20}")
            text = "\n".join(lines)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Удалить все", callback_data="del_accounts")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")],
        ])
        await cb.message.answer(text, reply_markup=kb)
        await cb.answer()

    @dp.callback_query(F.data == "del_accounts")
    async def cb_del_accounts(cb: types.CallbackQuery):
        if not is_admin(cb.from_user.id):
            return
        await db.delete_all_accounts()
        await cb.message.answer("Все аккаунты удалены.")
        await cb.answer()

    @dp.message(F.document)
    async def handle_document(msg: types.Message):
        if not is_admin(msg.from_user.id):
            return
        doc = msg.document
        if not doc.file_name.endswith(".txt"):
            await msg.answer("Отправьте .txt файл с аккаунтами (login:password).")
            return
        file = await bot.download(doc)
        content = file.read().decode("utf-8", errors="ignore")
        lines = [l.strip() for l in content.splitlines() if l.strip()]
        accounts = []
        for line in lines:
            if ":" in line:
                parts = line.split(":")
                user = parts[0].strip()
                passw = parts[1].strip()
                proxy = ":".join(parts[2:]).strip() if len(parts) > 2 else ""
                accounts.append((user, passw, proxy))
        if not accounts:
            await msg.answer("Не удалось распарсить аккаунты.\nФормат: login:password или login:password:proxy")
            return
        with_proxy = sum(1 for a in accounts if a[2])
        await db.add_accounts(accounts)
        text = f"Загружено аккаунтов: {len(accounts)}"
        if with_proxy:
            text += f"\nС прокси: {with_proxy}"
        await msg.answer(text)

    @dp.callback_query(F.data == "stats")
    async def cb_stats(cb: types.CallbackQuery):
        if not is_admin(cb.from_user.id):
            return
        stats = await db.get_stats()
        await cb.message.answer(
            f"📊 Статистика\n\n"
            f"Аккаунтов: {stats['accounts_active']}/{stats['accounts_total']} (активных/всего)\n"
            f"Комментариев отправлено: {stats['comments_sent']}\n"
            f"Ошибок: {stats['comments_errors']}\n"
            f"Активных задач: {stats['active_tasks']}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")],
            ]),
        )
        await cb.answer()

    @dp.callback_query(F.data == "tasks")
    async def cb_tasks(cb: types.CallbackQuery):
        if not is_admin(cb.from_user.id):
            return
        tasks = await db.get_all_tasks()
        if not tasks:
            await cb.message.answer("Задач пока нет.")
            await cb.answer()
            return
        status_map = {"pending": "⏳", "running": "▶️", "done": "✅", "cancelled": "❌", "paused_no_accounts": "⚠️"}
        lines = ["📜 Последние задачи:\n"]
        for t in tasks:
            icon = status_map.get(t["status"], "❓")
            lines.append(f"{icon} #{t['id']} | {t['search_query'][:30]}\n   {t['comments_done']}/{t['max_comments']} отправлено, {t['comments_failed']} ошибок")
        await cb.message.answer(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")],
            ]),
        )
        await cb.answer()

    @dp.callback_query(F.data == "back_main")
    async def cb_back(cb: types.CallbackQuery):
        await cmd_start(cb.message)
        await cb.answer()

    @dp.message(Command("screen"))
    async def cmd_screen(msg: types.Message):
        if not is_admin(msg.from_user.id):
            return
        args = msg.text.replace("/screen", "", 1).strip()
        task_id = int(args) if args.isdigit() else None
        active = get_all_active_tasks()
        if not active:
            await msg.answer("Нет активных задач с открытым браузером.")
            return
        if task_id is None and len(active) == 1:
            task_id = active[0]
        elif task_id is None:
            await msg.answer(f"Активные задачи: {', '.join(f'#{t}' for t in active)}\nУкажите ID: /screen <id>")
            return
        await msg.answer(f"Делаю скриншот задачи #{task_id}...")
        path = await take_debug_screenshot(task_id)
        if path and os.path.exists(path):
            with open(path, "rb") as f:
                photo = BufferedInputFile(f.read(), filename=os.path.basename(path))
            await msg.answer_photo(photo, caption=f"Скриншот задачи #{task_id}")
        else:
            await msg.answer(f"Не удалось сделать скриншот. Задача #{task_id} может быть неактивна или браузер закрыт между итерациями.")

    @dp.message(Command("errors"))
    async def cmd_errors(msg: types.Message):
        if not is_admin(msg.from_user.id):
            return
        screenshots_dir = os.path.join(os.path.dirname(__file__), "screenshots")
        if not os.path.exists(screenshots_dir):
            await msg.answer("Нет скриншотов ошибок.")
            return
        files = sorted(
            [f for f in os.listdir(screenshots_dir) if f.endswith(".png")],
            key=lambda x: os.path.getmtime(os.path.join(screenshots_dir, x)),
            reverse=True,
        )
        if not files:
            await msg.answer("Нет скриншотов ошибок.")
            return
        count = min(5, len(files))
        await msg.answer(f"Последние {count} скриншотов ошибок:")
        for fname in files[:count]:
            fpath = os.path.join(screenshots_dir, fname)
            with open(fpath, "rb") as f:
                photo = BufferedInputFile(f.read(), filename=fname)
            await msg.answer_photo(photo, caption=fname)

    @dp.message(Command("help"))
    async def cmd_help(msg: types.Message):
        await msg.answer(
            "Команды:\n\n"
            "/start — Главное меню\n"
            "/task запрос | комментарий | лимит — Создать задачу\n"
            "/stats — Статистика\n"
            "/accounts — Список аккаунтов\n"
            "/screen [id] — Скриншот браузера активной задачи\n"
            "/errors — Последние скриншоты ошибок\n"
            "/help — Помощь\n\n"
            "Загрузка аккаунтов: отправьте .txt файл (login:password, по строке)."
        )

    @dp.message(F.content_type == "web_app_data")
    async def handle_webapp_data(msg: types.Message):
        if not is_admin(msg.from_user.id):
            return
        try:
            data = json.loads(msg.web_app_data.data)
            action = data.get("action")
            if action == "create_task":
                task_id = await db.create_task(data["query"], data["comment"], int(data.get("limit", 100)))
                await msg.answer(f"Задача #{task_id} создана через WebApp.\nЗапуск...")
                asyncio.create_task(run_task(task_id))
            elif action == "upload_accounts":
                raw = data.get("accounts", "")
                lines = [l.strip() for l in raw.splitlines() if ":" in l]
                accounts = [(l.split(":", 1)[0], l.split(":", 1)[1]) for l in lines]
                if accounts:
                    await db.add_accounts(accounts)
                    await msg.answer(f"Загружено {len(accounts)} аккаунтов через WebApp.")
                else:
                    await msg.answer("Не удалось распарсить аккаунты.")
            elif action == "get_stats":
                stats = await db.get_stats()
                await msg.answer(f"Аккаунтов: {stats['accounts_active']}/{stats['accounts_total']}\nОтправлено: {stats['comments_sent']} | Ошибок: {stats['comments_errors']}")
        except Exception as e:
            logger.error(f"WebApp data error: {e}")
            await msg.answer(f"Ошибка: {e}")

    logger.info("Бот запущен, начинаю polling...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


async def main():
    port = int(os.environ.get("PORT", 8080))
    logger.info(f"Запуск приложения, PORT={port}")

    await start_health_server()
    logger.info("Health-сервер готов, Railway healthcheck должен проходить")

    try:
        import database as db
        await db.init_db()
    except Exception as e:
        logger.error(f"Ошибка инициализации БД: {e}")

    try:
        await start_bot()
    except Exception as e:
        logger.error(f"Ошибка бота: {e}")

    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
