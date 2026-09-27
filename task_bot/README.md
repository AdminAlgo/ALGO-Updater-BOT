# ALGO Task Bot

Telegram bot + admin website that tracks team tasks for the ALGO ELD department.
Separate from the ELD alert bot in the folder above: separate code, separate Railway
project, separate Telegram bot.

Телеграм-бот + сайт для админов, который следит за задачами команды. Полностью отдельно
от ELD-бота.

## What it does / Что делает

- **➕ New task**: an 8-step form in private chat: Title, Description, Assignee,
  Deadline (month → day → confirm), Priority, Company (CAPITAL LETTERS), Category, Notes.
  The task card is posted in the team group.
- **Buttons under each task**: Start, Blocked (reason), Done, Reject (reason), Edit, Note, History.
- **Approval, only from an admin's own Telegram account** (see `ADMIN_USERNAMES`):
  - type `approve T-0040` to close the task
  - type `not completed yet T-0040` to send it back to work
- **Check-ins at 17:00 and 01:00 (Tashkent)**: one message per open task. No answer after
  30 min → reminder; after 60 min → admins are tagged. Overdue + "in progress" → a note is required.
- **Overdue reminders** every 3 hours.
- **Website (admins only)**: see and edit everything, add tasks and members, full history.
  Changes made on the website are **never announced in Telegram** and are hidden from the
  Telegram history.

## First setup / Первый запуск

1. **Telegram**: in @BotFather, set **Group Privacy → Disable** for this bot (so it can read
   `approve …` typed in the group). Add the bot to the team group and make it **admin**.
2. **Railway**: a **new project** only for this bot. Service root directory: `task_bot`.
   Add a **Volume** mounted at `/data`.
3. **Railway → Variables**:
   - `TELEGRAM_BOT_TOKEN`: from @BotFather
   - `DASHBOARD_PASSWORD`: website password
   - `SECRET_KEY`: long random text
   - `DATA_DIR=/data`
   - optional: `ADMIN_USERNAMES`, `TIMEZONE`, `CHECKIN_TIMES`, `REMIND_AFTER_MIN`,
     `ESCALATE_AFTER_MIN`, `OVERDUE_EVERY_HOURS` (see `.env.example`)
4. In the team group, an admin sends **/setgroup**. The bot pins its welcome message.
5. Every team member opens the bot in private, presses Start and sends their **employee ID**
   (N820, A430, …). The first list of members comes from `members_seed.json`; add more on
   the website → Members.

## Run locally / Запуск на компьютере

```bash
cd task_bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env        # fill in the values, then export them
python main.py --check      # checks settings and the database
python main.py              # bot + website on http://localhost:8000
pytest -q                   # tests
```

## Files / Файлы

```
task_bot/
  main.py              starts the bot and the website
  members_seed.json    first list of team members (used only when the database is empty)
  taskbot/config.py    settings from environment variables
  taskbot/db.py        SQLite database: members, tasks, history, check-in answers
  taskbot/texts.py     icons, words and message texts
  taskbot/bot.py       Telegram: form, buttons, typed commands, check-ins
  taskbot/web.py       admin website (templates in taskbot/templates/)
  tests/               automatic tests
```
