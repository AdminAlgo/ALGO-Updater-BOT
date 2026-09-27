"""ALGO Task Bot: Telegram bot + admin website in one process.

    python main.py          run everything (Railway runs this)
    python main.py --check  only check settings and the database, then exit
"""
import logging
import os
import sys
import threading

from waitress import serve

from taskbot import config
from taskbot.bot import TaskBot
from taskbot.db import DB
from taskbot.web import create_web

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    log = logging.getLogger("main")

    cfg = config.load()
    issues = config.problems(cfg)
    if issues:
        for p in issues:
            log.error("Setting missing: %s", p)
        sys.exit(1)

    db = DB(cfg.db_path, cfg.tz)
    added = db.seed_members(os.path.join(HERE, "members_seed.json"))
    if added:
        log.info("Added %d team members from members_seed.json", added)
    log.info("Database: %s · check-ins at %s (%s)", cfg.db_path,
             ", ".join(f"{h:02d}:{m:02d}" for h, m in cfg.checkin_times), cfg.tz.key)
    if "--check" in sys.argv:
        print("Config OK")
        return

    bot = TaskBot(cfg, db)
    app = bot.build()
    web = create_web(cfg, db, notifier=bot)
    threading.Thread(target=serve, args=(web,), kwargs={"host": "0.0.0.0", "port": cfg.port, "threads": 4},
                     daemon=True, name="website").start()
    log.info("Website on port %s", cfg.port)
    app.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=False)


if __name__ == "__main__":
    main()
