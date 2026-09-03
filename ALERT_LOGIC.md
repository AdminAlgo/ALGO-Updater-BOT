# ALGO Updater Bot — Alert Logic Reference

This is the single source of truth for **what the bot sends, when, to whom, and
in what words**. It is written from the code (`src/rules.py`, `src/scheduler.py`,
`config.yaml`) — if a dispatcher instruction disagrees with this file, this file
is right (or the code needs changing).

The `/faq` command in the bot returns a short version of this.

---

## 1. How the cycle works

| | |
|---|---|
| **Poll interval** | every **120 seconds** the bot fetches live Hours-of-Service data for every *active* driver of every *enabled* company and re-evaluates every rule |
| **Command replies** | `/roster`, `/assign`, … are answered in **1–2 seconds** on a separate channel — they do not wait for a poll cycle |
| **Data source** | Factor ELD / Leader ELD (both the DriveHOS backend) |
| **Delivery** | Telegram. One group per driver (driver + their dispatcher). One shared **team/dispatch group** for escalations |

Each poll cycle: fetch → evaluate every driver against the 4 rules → queue every
resulting alert → drain the queue (rate-limited, retrying Telegram 429s) →
record each one as "sent" **only after Telegram confirms** (a failed send is
retried next cycle, not lost).

---

## 2. The five alert types

### 2.1 Low Hours — the driver is running out of time

**Trigger:** the **most urgent** (smallest) of the driver's **Drive**, **Break**,
and **Shift** time-remaining drops to at or below a threshold.

**Thresholds (minutes remaining):**

| Audience | Thresholds | Meaning |
|---|---|---|
| Driver's group | `120`, `60`, `30` | 2 hours / 1 hour / 30 minutes left |
| Team/dispatch group | `60`, `30` | 1 hour / 30 minutes left |

- Only the **most urgent crossed** threshold fires in a given cycle (if a driver
  jumps straight from 70 min to 25 min, they get the 30-min alert, not both).
- Each distinct threshold fires **once per shift**. The set resets when the
  driver recovers **above the largest threshold** (a 34-hour reset / off-duty
  time that adds hours back) **or** goes Off Duty / Sleeper.
- Fires **only while the driver is `Driving`, `On Duty`, or `Yard Move`**.

**Sent to:** the driver's group for the `120/60/30` levels; the team group *also*
gets the `60` and `30` levels (so dispatch sees the urgent ones without watching
every driver group).

**Message:**
> Hello.
> Dear {full name}
>
> You have only {X hours and Y minutes} on your {Drive|Break|Shift}. Please stop
> within those hours. Let us know if you need help.
>
> Thank you.

(As of FIX-6 this wording is fixed for every company — the old per-company
`low_hours_closing` override, e.g. GOWIN's custom line, no longer has any
effect. The `low_hours_closing` config field still exists but is dead.)

---

### 2.2 70-Hour Cycle — running low (added A1)

**Trigger:** **Cycle** time-remaining drops to at or below a threshold, in
**hours** remaining (not minutes, unlike the low-hours rule).

**Thresholds (hours remaining):** `10`, `5` (`cycle_alert_thresholds_hours`).

- Only the most urgent crossed threshold fires in a given cycle.
- Each distinct threshold fires **once per cycle**. Resets when the driver
  recovers **above the largest threshold** (a 34-hour restart) **or** goes
  Off Duty / Sleeper.
- Fires **only while the driver is `Driving`, `On Duty`, or `Yard Move`**.

**Sent to:** the driver's own group. Also the team/dispatch group if
`cycle_also_notify_dispatch: true` (default: **off**).

**Message:**
> Hello.
> Dear {full name}
>
> You have only {X hours and Y minutes} left on your 70-hour Cycle. Please
> plan your reset.
>
> Let us know if you need help.
>
> Thank you.

---

### 2.3 14-Hour Shift Limit — violation

**Trigger:** **Shift** time-remaining reaches **0** (the 14-hour on-duty window
is used up) while the driver is `Driving` / `On Duty` / `Yard Move`.

- Fires **immediately** when the violation starts, then **re-sends every
  `shift_violation_resend_minutes` (30 min)** while the driver is still in
  violation — up to `shift_violation_max_resends` (5) resends (a safety cap;
  6 messages total, then it stops nagging).
- Stops the moment the driver goes Off Duty / Sleeper, or the shift resets —
  the resend count also resets, so the next violation starts fresh.

**Sent to:** the **driver's own group** (changed 2026-09 — previously went to
dispatch only). Also sent to the team/dispatch group if
`shift_violation_also_notify_dispatch: true` is set (default: **off**).

**Message:**
> Hello.
> Dear {full name}
>
> Your 14-hour Shift limit is finished (00:00). You are in violation and must
> stop driving now.
>
> Please go Off Duty and let us know if you need help.
>
> Thank you.

---

### 2.4 ELD Disconnected — device offline while working

**Trigger:** the paired vehicle is `OFFLINE`, **or** its last telemetry is older
than **`disconnect_stale_minutes` (30 min)**, **while** the driver is `Driving` /
`On Duty` / `Yard Move`.

- Requires `disconnect_alerts_enabled: true` (currently **on**).
- Requires a vehicle to be **paired** — "no vehicle paired" is treated as
  *unknown*, never as *disconnected*.
- **Re-alerts every `disconnect_realert_minutes` (60 min)** while still
  disconnected.
- Clears the moment the device reconnects — the next disconnect alerts
  immediately (no 60-min wait).
- **Never** fires off-duty or in the sleeper berth.

**Sent to:** the driver's group.

**Message:**
> Hello.
> Dear {full name}
>
> Your ELD device appears to be DISCONNECTED {status phrase}. Please
> reconnect it as soon as possible so your ELD will record correctly.
>
> If you are having trouble connecting, let us know please.
>
> Thank you.

`{status phrase}` matches whatever the driver was actually doing (FIX-5 — this
used to be hard-coded to "while you are driving" even for On Duty / Yard Move,
which didn't match the status card image): "while you are driving" / "while you
are On Duty" / "while you are in Yard Move" / "while you are in Personal
Conveyance". If the duty status has no known phrasing, the alert is skipped
and an error is logged rather than guessing.

---

### 2.5 Long On-Duty — welfare check

**Trigger:** the driver has been in `On Duty` status (not driving) for
**`on_duty_alert_hours` (2 h)** continuously.

- Fires **once per On-Duty stretch**. Resets when the driver leaves On Duty.

**Sent to:** the driver's group.

**Message:**
> Hello.
> Dear {full name}
>
> We noticed you have been On Duty for more than 2 hours. Is everything okay?
> If you need any help, please let us know.
>
> Thank you.

---

## 3. What does NOT trigger an alert

| Situation | Why |
|---|---|
| Driver is Off Duty or in Sleeper | Resting — no low-hours, disconnect, or on-duty alerts |
| Driver just came On Duty, no active shift yet (shift = 0, drive = 0, break still full 8 h) | Data artifact, not a 14-hour violation — all HOS alerts skipped |
| Driver has **no Telegram group assigned** | All of their driver-group alerts are skipped, with nowhere to go — low-hours 120/60/30, Cycle 10/5h, disconnect, on-duty, **and (since FIX-1) the 14-hour violation too**, unless the matching `*_also_notify_dispatch` flag is set |
| Company is `enabled: false` | Not polled at all |
| No vehicle paired | Connection is "unknown" — never counts as disconnected |
| Same condition, already alerted | De-duplicated until it clears and recurs (Disconnect re-pings every 60 min; 14-hour violation re-pings every 30 min, up to 5 times — see §2.2/§2.3) |

---

## 4. Timing & repeat behaviour

- **Within a cycle:** every alert is queued, then the queue drains itself
  rate-limited — up to `send_rate_per_second` (25) messages/sec overall, and
  `send_rate_per_chat_per_minute` (20) to any one chat, so a burst to the
  shared team/dispatch chat can't flood it. An HTTP 429 from Telegram is
  retried using its own `retry_after` value (up to 5 times) instead of being
  dropped. Five drivers crossing a threshold in the same cycle, going to five
  different chats → five messages in well under a second; 150 at once still
  finishes in well under the 120-second cycle budget.
- **Between cycles:** 120 seconds.
- **Repeats:** none for low-hours or on-duty, unless the underlying condition
  clears and happens again. Two alerts have a timed repeat while the condition
  stays true: **Disconnect → every 60 minutes**, and **14-hour violation →
  every 30 minutes (capped at 5 resends)**.
- **After a restart / redeploy:** de-dup state and group assignments are read
  back from the `/data` volume, so the bot does **not** re-send everything it
  already sent. *(This requires the `/data` volume to be attached in Railway.)*

---

## 5. Message anatomy

Every alert is built the same way:

```
[@driver_handle]            ← only if we know the driver's Telegram @username / id
Hello.
Dear {full name}            ← falls back to "Dear Driver" if the name is blank

{the specific issue — see each rule above}

Thank you.

📋 Driver log: {url}         ← only if driver_log_url_template is set (currently not)
```

All four templates live in `src/messages.py` — thresholds and targets stay in
`config.py` / `config.yaml`, wording lives only there (FIX-6).

Plus, when `attach_log_image: true` (currently **on**): a generated **PNG "ring
card"** is attached to the message — four gauges (Break / Drive / Shift / Cycle),
the driver's name, duty-status badge, and truck number, matching the DriveHOS
dashboard header. The card border turns red if the ELD is disconnected.

The **@mention**: if the driver has posted in their group at least once, the bot
captures their Telegram handle and pings them by name so the alert notifies them
directly. A manual override can be set in `config.yaml` under `manual_driver_tags`.

---

## 6. Current configuration (`config.yaml`)

| Key | Value | Effect |
|---|---|---|
| `poll_interval_seconds` | `120` | check every 2 minutes |
| `shift_limit_hours` | `14` | the violation threshold |
| `shift_violation_resend_minutes` | `30` | how often the violation alert repeats while ongoing |
| `shift_violation_max_resends` | `5` | safety cap — stop after this many resends (6 messages total) |
| `shift_violation_also_notify_dispatch` | `false` | if true, violation alerts also go to the team/dispatch group |
| `low_hours_thresholds_minutes.driver_group` | `[120, 60, 30]` | driver-group low-hours alerts |
| `low_hours_thresholds_minutes.team_group` | `[60, 30]` | team-group low-hours alerts |
| `cycle_alert_thresholds_hours` | `[10, 5]` | 70-hour Cycle low-time alerts (hours, not minutes) |
| `cycle_also_notify_dispatch` | `false` | if true, Cycle alerts also go to the team/dispatch group |
| `disconnect_alerts_enabled` | `true` | disconnect alerts on |
| `disconnect_stale_minutes` | `30` | telemetry older than this = disconnected |
| `disconnect_realert_minutes` | `60` | re-alert cadence while disconnected |
| `on_duty_alert_hours` | `2` | long-on-duty welfare check |
| `attach_log_image` | `true` | attach the ring-card PNG |
| `send_rate_per_second` | `25` | global outbound Telegram rate cap the send queue respects |
| `send_rate_per_chat_per_minute` | `20` | per-chat outbound rate cap |
| `connection_required_statuses` | `Driving, On Duty, Yard Move` | which statuses count as "active" |
| `team_group_chat_id` | *(set in dashboard)* | where escalations + team low-hours go |

Edit these on the dashboard **Companies** page (applied within one poll cycle, no
redeploy) — but note `config.yaml` ships in the image, so a redeploy reverts
dashboard edits unless they are also committed to the repo.

---

## 7. Bot commands (PM the bot)

| Command | Purpose |
|---|---|
| `/faq` | this logic, short form |
| `/roster <name or truck>` | find a driver on the ELD roster |
| `/groups` | every group the bot is in + who it's assigned to |
| `/assign <name> \| <group-id>` | link a driver to a Telegram group |
| `/unassign <name>` | remove a driver's group link |
| `/whois <group-id>` | who a group is assigned to |
| `/unassigned` | groups the bot is in that still need a driver |
| `/coverage` | how many drivers have a captured @-tag |
| `/add <name>` / `/remove <name>` | run **inside** a driver's group |

Auto-assignment: when the bot is added to a group whose **title contains exactly
one roster driver's full name** (e.g. `#412 John Smith | WWH INC`), it links
automatically. Ambiguous titles (two matching drivers, a co-driver title) are
parked — see `/unassigned` — for a human to resolve with `/assign`.
