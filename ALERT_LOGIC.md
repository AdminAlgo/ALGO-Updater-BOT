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

Each poll cycle: fetch → evaluate every driver against the 4 rules → send each
resulting alert → record it as "sent" **only after Telegram confirms** (a failed
send is retried next cycle, not lost).

---

## 2. The four alert types

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
> Assalomu alaykum.
> Dear {full name}
>
> You have only {X hours and Y minutes} on your {Drive|Break|Shift}. {closing line}
>
> Thank you.

- `{closing line}` default: *"Please let us know if you will need more."*
- Per-company override — **GOWIN EXPEDITED TRANSPORTING LLC**: *"Please Stop
  within those hours, We can add hours once you leave OR"*

---

### 2.2 14-Hour Shift Limit — violation

**Trigger:** **Shift** time-remaining reaches **0** (the 14-hour on-duty window
is used up) while the driver is `Driving` / `On Duty` / `Yard Move`.

- Fires **once per violation episode**. Resets when Shift time-remaining goes
  back above 0 (new shift).
- A Sleeper / Off-Duty driver whose shift timer reads 0 is *resting*, not
  violating — no alert.

**Sent to:** the **team/dispatch group** (this is an escalation, not a nudge).

**Message:**
> Assalomu alaykum.
> Dear {full name}
>
> Your 14-hour shift limit has been reached. For your safety and to stay
> compliant, please stop driving and begin your required rest.
>
> If you have any questions, please let us know.
>
> Thank you.

---

### 2.3 ELD Disconnected — device offline while working

**Trigger:** the paired vehicle is `OFFLINE`, **or** its last telemetry is older
than **`disconnect_stale_minutes` (15 min)**, **while** the driver is `Driving` /
`On Duty` / `Yard Move`.

- Requires `disconnect_alerts_enabled: true` (currently **on**).
- Requires a vehicle to be **paired** — "no vehicle paired" is treated as
  *unknown*, never as *disconnected*.
- **Re-alerts every `disconnect_realert_minutes` (30 min)** while still
  disconnected.
- Clears the moment the device reconnects — the next disconnect alerts
  immediately (no 30-min wait).
- **Never** fires off-duty or in the sleeper berth.

**Sent to:** the driver's group.

**Message:**
> Assalomu alaykum.
> Dear {full name}
>
> Your ELD device appears to be DISCONNECTED while you are driving. Please
> reconnect it as soon as possible so your Hours of Service are recorded
> correctly.
>
> If you are having trouble connecting, let us know and we will guide you
> through it.
>
> Thank you.

---

### 2.4 Long On-Duty — welfare check

**Trigger:** the driver has been in `On Duty` status (not driving) for
**`on_duty_alert_hours` (2 h)** continuously.

- Fires **once per On-Duty stretch**. Resets when the driver leaves On Duty.

**Sent to:** the driver's group.

**Message:**
> Assalomu alaykum.
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
| Driver has **no Telegram group assigned** | Their driver-group alerts (low-hours 120/60/30, disconnect, on-duty) have nowhere to go and are skipped. Only the 14-hour violation (→ team group) still fires |
| Company is `enabled: false` | Not polled at all |
| No vehicle paired | Connection is "unknown" — never counts as disconnected |
| Same condition, already alerted | De-duplicated until it clears and recurs (Disconnect is the exception: re-pings every 30 min) |

---

## 4. Timing & repeat behaviour

- **Within a cycle:** all alerts are sent back-to-back with a ~0.05-second pause
  between messages (Telegram courtesy). Five drivers crossing a threshold in the
  same cycle → five messages within ~1 second.
- **Between cycles:** 120 seconds.
- **Repeats:** none, unless the underlying condition clears and happens again.
  The only timed repeat is **Disconnect → every 30 minutes** while ongoing.
- **After a restart / redeploy:** de-dup state and group assignments are read
  back from the `/data` volume, so the bot does **not** re-send everything it
  already sent. *(This requires the `/data` volume to be attached in Railway.)*

---

## 5. Message anatomy

Every alert is built the same way:

```
[@driver_handle]            ← only if we know the driver's Telegram @username / id
Assalomu alaykum.
Dear {full name}

{the specific issue — see each rule above}

{closing line}              ← low-hours only; per-company configurable
Thank you.

📋 Driver log: {url}         ← only if driver_log_url_template is set (currently not)
```

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
| `low_hours_thresholds_minutes.driver_group` | `[120, 60, 30]` | driver-group low-hours alerts |
| `low_hours_thresholds_minutes.team_group` | `[60, 30]` | team-group low-hours alerts |
| `disconnect_alerts_enabled` | `true` | disconnect alerts on |
| `disconnect_stale_minutes` | `15` | telemetry older than this = disconnected |
| `disconnect_realert_minutes` | `30` | re-alert cadence while disconnected |
| `on_duty_alert_hours` | `2` | long-on-duty welfare check |
| `attach_log_image` | `true` | attach the ring-card PNG |
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
