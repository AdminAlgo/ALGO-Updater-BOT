# ALGO Task Bot: rules for Claude sessions

- This folder is a **separate product** from the ELD alert bot in the repo root. Never change
  files outside `task_bot/` for this bot, and never touch the ELD bot's Railway project.
- Railway: this bot has its **own Railway project** (root directory `task_bot`, volume at `/data`).
- Secrets (bot token, website password, secret key) live only in Railway Variables. Never commit
  them, never print them.
- Bot language: English. Talk to the owner (Nusret) in simple English + Russian.
- Admins: Nusret (N820, @milord0_0) and Abdulaziz (N520, @Abdulakhatov04). Admin = Telegram username
  in ADMIN_USERNAMES only (the website "Admin" role does NOT grant approval). Only they approve, from
  their own account: `approve T-0040` / `not completed yet T-0040`.
- No employee-ID registration. People are recognized by Telegram account/username; anyone who writes
  in the team group is added automatically. Private chat with the bot is only for admins and
  TASK_CREATOR_USERNAMES (official accounts); only they create tasks in the bot.
- Website changes use history `source='web'`: never announce them in Telegram, never show them
  in the Telegram history, and do not move the card's "Last update" line.
- Company names are CAPITAL LETTERS only. Deadlines are dates (no time): month → day → confirm.
- Check-ins 17:00 and 01:00 Asia/Tashkent; remind +30 min; escalate +60 min; overdue every 3 h.
- Before pushing: `cd task_bot && pytest -q` must pass.
