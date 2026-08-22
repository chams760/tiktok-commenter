import aiosqlite
from datetime import datetime, timezone
from config import DB_PATH


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                password TEXT NOT NULL,
                proxy TEXT DEFAULT '',
                session_data TEXT DEFAULT '',
                comments_today INTEGER DEFAULT 0,
                last_reset TEXT DEFAULT '',
                status TEXT DEFAULT 'active',
                added_at TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                search_query TEXT NOT NULL,
                comment_text TEXT NOT NULL,
                max_comments INTEGER DEFAULT 100,
                comments_done INTEGER DEFAULT 0,
                comments_failed INTEGER DEFAULT 0,
                status TEXT DEFAULT 'pending',
                created_at TEXT DEFAULT '',
                finished_at TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS comment_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER,
                account_id INTEGER,
                video_url TEXT,
                status TEXT,
                error_text TEXT DEFAULT '',
                created_at TEXT DEFAULT ''
            );
        """)
        for col, default in [("proxy", "''"), ("email_password", "''"), ("imap_server", "''")]:
            try:
                await db.execute(f"ALTER TABLE accounts ADD COLUMN {col} TEXT DEFAULT {default}")
            except Exception:
                pass
        await db.commit()


async def add_accounts(accounts: list[tuple]) -> int:
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for t in accounts:
        u = t[0]
        p = t[1]
        pr = t[2] if len(t) > 2 else ""
        ep = t[3] if len(t) > 3 else ""
        im = t[4] if len(t) > 4 else ""
        rows.append((u, p, pr, ep, im, now, now))
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            "INSERT INTO accounts (username, password, proxy, email_password, imap_server, added_at, last_reset) VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        await db.commit()
        cursor = await db.execute("SELECT changes()")
        row = await cursor.fetchone()
        return row[0] if row else 0


async def get_accounts(status: str = "active") -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM accounts WHERE status = ?", (status,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_available_account(max_per_day: int) -> dict | None:
    now = datetime.now(timezone.utc)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM accounts WHERE status = 'active' ORDER BY comments_today ASC"
        )
        rows = await cursor.fetchall()
        for row in rows:
            acc = dict(row)
            if acc["last_reset"]:
                last = datetime.fromisoformat(acc["last_reset"])
                if (now - last).days >= 1:
                    await db.execute(
                        "UPDATE accounts SET comments_today = 0, last_reset = ? WHERE id = ?",
                        (now.isoformat(), acc["id"]),
                    )
                    await db.commit()
                    acc["comments_today"] = 0
            if acc["comments_today"] < max_per_day:
                return acc
        return None


async def increment_account_comments(account_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE accounts SET comments_today = comments_today + 1 WHERE id = ?",
            (account_id,),
        )
        await db.commit()


async def set_account_status(account_id: int, status: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE accounts SET status = ? WHERE id = ?", (status, account_id)
        )
        await db.commit()


async def delete_all_accounts():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM accounts")
        await db.commit()


async def create_task(query: str, comment: str, max_comments: int) -> int:
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO tasks (search_query, comment_text, max_comments, created_at) VALUES (?, ?, ?, ?)",
            (query, comment, max_comments, now),
        )
        await db.commit()
        return cursor.lastrowid


async def get_task(task_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def update_task(task_id: int, **kwargs):
    async with aiosqlite.connect(DB_PATH) as db:
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        vals = list(kwargs.values()) + [task_id]
        await db.execute(f"UPDATE tasks SET {sets} WHERE id = ?", vals)
        await db.commit()


async def get_active_tasks() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM tasks WHERE status IN ('pending', 'running') ORDER BY created_at DESC"
        )
        return [dict(r) for r in await cursor.fetchall()]


async def get_all_tasks(limit: int = 20) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in await cursor.fetchall()]


async def log_comment(task_id: int, account_id: int, video_url: str, status: str, error: str = ""):
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO comment_log (task_id, account_id, video_url, status, error_text, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, account_id, video_url, status, error, now),
        )
        await db.commit()


async def get_stats() -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        c1 = await db.execute("SELECT COUNT(*) FROM accounts WHERE status = 'active'")
        active = (await c1.fetchone())[0]
        c2 = await db.execute("SELECT COUNT(*) FROM accounts")
        total = (await c2.fetchone())[0]
        c3 = await db.execute("SELECT COUNT(*) FROM comment_log WHERE status = 'ok'")
        sent = (await c3.fetchone())[0]
        c4 = await db.execute("SELECT COUNT(*) FROM comment_log WHERE status = 'error'")
        errors = (await c4.fetchone())[0]
        c5 = await db.execute("SELECT COUNT(*) FROM tasks WHERE status IN ('pending','running')")
        active_tasks = (await c5.fetchone())[0]
        return {
            "accounts_active": active,
            "accounts_total": total,
            "comments_sent": sent,
            "comments_errors": errors,
            "active_tasks": active_tasks,
        }
