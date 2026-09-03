"""Alert rule evaluation.

Turns a normalized DriverSnapshot (+ Config + AlertState) into the alerts that
should be sent right now, with the exact message text. It updates AlertState to
de-duplicate / throttle, but it does NOT send anything — delivery is Layer 4.

Three rules:
  1. low_hours        — a monitored timer (drive/shift/break) drops to/below a
                        configured minute-threshold. driver_group thresholds go
                        to the company's driver group; team_group thresholds go
                        to the internal team group. One bundled message shows all
                        three timers (matching the original example). Each
                        threshold fires once per shift (reset on recovery).
  2. shift_violation  — shift time remaining reaches 0 (the 14h limit). Fires
                        once per violation episode.
  3. disconnect       — vehicle OFFLINE or telemetry stale > disconnect_stale_
                        minutes, WHILE the driver is in a connection_required
                        status. Throttled by disconnect_realert_minutes; cleared
                        on reconnect. Ignored entirely off-duty / in sleeper, or
                        when no vehicle is paired.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

from . import messages
from .eld.base import DriverSnapshot, normalize_name

log = logging.getLogger("eld_alert_bot")


def _resolve_mention(snap: DriverSnapshot, config, registry):
    """Resolve how to mention the driver. Returns (username_tag, user_id).

    - username_tag: a "@handle" string to prepend in text (or None)
    - user_id:      a numeric id for a text_mention (drivers with no username)
    Manual override (config.manual_driver_tags) wins; value may be a "@handle"
    OR a numeric Telegram user-id. Falls back to auto-captured handle/id.
    """
    manual = None
    for name, val in (getattr(config, "manual_driver_tags", {}) or {}).items():
        if val not in (None, "") and normalize_name(name) == normalize_name(snap.name):
            manual = val
            break
    if manual is not None:
        m = str(manual).strip()
        if m.startswith("@"):
            return "@" + m.lstrip("@"), None
        if m.lstrip("-").isdigit():
            return None, int(m)
        return "@" + m, None  # bare username
    if registry is not None:
        uname = registry.tag_username(snap.driver_id)
        if uname:
            return "@" + str(uname).lstrip("@"), None
        uid = registry.tag_user_id(snap.driver_id)
        if uid:
            return None, int(uid)
    return None, None


def _attach_log_image(alerts: list, snap: DriverSnapshot, config) -> list:
    """If enabled, render the driver's HOS card once and attach to each alert."""
    if not alerts or not getattr(config, "attach_log_image", False):
        return alerts
    try:
        from .log_image import render_hours_card
        stale_min = getattr(config, "disconnect_stale_minutes", 15)
        disconnected = snap.connection.has_vehicle and (
            snap.connection.is_offline or snap.connection.is_stale(stale_min)
        )
        png = render_hours_card(snap, disconnected=disconnected)
    except Exception as exc:  # never let image trouble block the text alert
        log.warning("could not render log image for %s: %s", snap.name, exc)
        return alerts
    return [replace(a, image_png=png) for a in alerts]

# Audience identifiers.
DRIVER_GROUP = "driver_group"
TEAM_GROUP = "team_group"

# Alert kinds.
KIND_LOW_HOURS = "low_hours"
KIND_CYCLE = "cycle"
KIND_SHIFT_VIOLATION = "shift_violation"
KIND_DISCONNECT = "disconnect"
KIND_ON_DUTY = "on_duty"


@dataclass(frozen=True)
class Alert:
    """One alert ready to be delivered.

    Evaluation does NOT record this alert as "fired" — the caller invokes
    ``commit()`` only after a successful send, so a failed send retries next
    cycle instead of being silently suppressed.
    """

    kind: str
    audience: str          # DRIVER_GROUP | TEAM_GROUP
    chat_id: str           # resolved target chat id
    company: str
    driver_name: str
    driver_id: str
    text: str
    dedupe_key: str = field(default="")
    threshold: int | None = None  # the low-hours threshold this alert fired for
    image_png: bytes | None = field(default=None, repr=False)  # optional log card
    # User-ID mention (for drivers with no @username): the sender prepends the
    # name and attaches a text_mention entity so the driver is still pinged.
    mention_user_id: int | None = None
    mention_name: str | None = None
    # Shift-violation only: True if this is a resend, not the episode's first
    # message — tells commit() whether to count it against the resend cap.
    is_resend: bool = False

    def commit(self, state, now) -> None:
        """Record in state that this alert was successfully sent (de-dup)."""
        if self.kind == KIND_LOW_HOURS:
            state.mark_low_hours_fired(self.driver_id, self.audience, self.threshold)
        elif self.kind == KIND_CYCLE:
            state.mark_cycle_fired(self.driver_id, self.audience, self.threshold)
        elif self.kind == KIND_SHIFT_VIOLATION:
            state.mark_shift_violation_sent(self.driver_id, now, is_resend=self.is_resend)
        elif self.kind == KIND_DISCONNECT:
            state.mark_disconnect_alert(self.driver_id, now)
        elif self.kind == KIND_ON_DUTY:
            state.set_on_duty_alerted(self.driver_id, True)


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #
def evaluate_driver(
    snap: DriverSnapshot,
    *,
    company,
    config,
    state,
    now: datetime,
    registry=None,
) -> list[Alert]:
    """Evaluate all rules for one driver; mutate state; return alerts to send.

    If ``registry`` is given, this driver's group is looked up there (per-driver
    routing); a driver with no registered group gets no driver-group alerts
    (central-chat alerts still fire). Without a registry, falls back to the
    company's single driver_group_chat_id.
    """
    alerts: list[Alert] = []
    did = snap.driver_id
    if registry is not None:
        driver_chat = registry.chat_for(did)  # None => not registered yet
    else:
        driver_chat = company.driver_group_chat_id
    team_chat = getattr(company, "team_group_chat_id", None) or config.team_group_chat_id
    log_url = messages.render_log_url(getattr(config, "driver_log_url_template", None), snap)
    tag, mention_uid = _resolve_mention(snap, config, registry)  # @handle or user-id
    # A driver is "active" (and thus alertable for low-hours) only while in a
    # connection_required status (Driving / On Duty / Yard Move) — never while
    # Off Duty or in the Sleeper berth.
    active = snap.duty_status_label in set(config.connection_required_statuses)

    # --- Guard: "no active shift" data artifact --------------------------- #
    # When a driver has just come On Duty (or the ELD has no active shift yet),
    # the API returns shift=0 AND drive=0 while the break timer is still FULL
    # (8h). That is NOT a 14h violation or a low-hours situation — exhausting a
    # 14h shift requires driving, which always depletes the break timer. Skip
    # all HOS alerts for this state (reset transient state so real conditions
    # later still alert).
    BREAK_FULL = 8 * 3600
    no_active_shift = (
        snap.hos.shift_seconds <= 0
        and snap.hos.drive_seconds <= 0
        and snap.hos.break_seconds >= BREAK_FULL - 300
    )
    if no_active_shift:
        state.reset_low_hours(did)
        state.reset_cycle(did)
        state.clear_shift_violation(did)
        state.clear_disconnect(did)
        return []

    # --- Rule 1: low hours ------------------------------------------------- #
    # Trigger value is the most urgent of the three monitored timers (minutes).
    min_minutes = min(
        snap.hos.drive_seconds,
        snap.hos.shift_seconds,
        snap.hos.break_seconds,
    ) // 60

    thr_cfg = config.low_hours_thresholds_minutes
    audiences = (
        (DRIVER_GROUP, thr_cfg.driver_group, driver_chat),
        (TEAM_GROUP, thr_cfg.team_group, team_chat),
    )

    # Recovery: if the driver is comfortably above every threshold again, OR is
    # no longer on duty, clear fired flags so the next shift can alert.
    all_thresholds = list(thr_cfg.driver_group) + list(thr_cfg.team_group)
    if not active or (all_thresholds and min_minutes > max(all_thresholds)):
        state.reset_low_hours(did)

    for audience, thresholds, chat_id in (audiences if active else ()):
        if not chat_id:
            # Driver-group audience but this driver has no registered group yet.
            continue
        # Only the most urgent (smallest) crossed threshold should alert, but
        # every distinct threshold fires at most once per shift.
        crossed = sorted((t for t in thresholds if min_minutes <= t))
        if not crossed:
            continue
        threshold = crossed[0]
        if state.low_hours_already_fired(did, audience, threshold):
            continue
        # NOTE: not marked fired here — committed after a successful send.
        alerts.append(
            Alert(
                kind=KIND_LOW_HOURS,
                audience=audience,
                chat_id=chat_id,
                company=company.name,
                driver_name=snap.name,
                driver_id=did,
                text=messages.low_hours_text(snap, tag, log_url),
                dedupe_key=f"{did}:low:{audience}:{threshold}",
                threshold=threshold,
            )
        )

    # --- Rule 5 (A1): 70-hour Cycle running low ---------------------------- #
    cycle_thr = list(getattr(config, "cycle_alert_thresholds_hours", []) or [])
    if cycle_thr:
        cycle_hours = snap.hos.cycle_seconds / 3600
        # Recovery: comfortably above every threshold again, or no longer on
        # duty — clear fired flags so the next cycle can alert again.
        if not active or cycle_hours > max(cycle_thr):
            state.reset_cycle(did)

        if active:
            crossed = sorted(t for t in cycle_thr if cycle_hours <= t)
            if crossed:
                threshold = crossed[0]  # most urgent (smallest) crossed
                cycle_targets = [(DRIVER_GROUP, driver_chat)]
                if getattr(config, "cycle_also_notify_dispatch", False):
                    cycle_targets.append((TEAM_GROUP, team_chat))
                for audience, chat_id in cycle_targets:
                    if not chat_id:
                        continue
                    if state.cycle_already_fired(did, audience, threshold):
                        continue
                    # NOTE: not marked fired here — committed after a successful send.
                    alerts.append(
                        Alert(
                            kind=KIND_CYCLE,
                            audience=audience,
                            chat_id=chat_id,
                            company=company.name,
                            driver_name=snap.name,
                            driver_id=did,
                            text=messages.cycle_text(snap, tag, log_url),
                            dedupe_key=f"{did}:cycle:{audience}:{threshold}",
                            threshold=threshold,
                        )
                    )

    # --- Rule 2: shift limit violation ------------------------------------ #
    # Only a real violation if the driver is actually on duty/driving — a
    # Sleeper/Off-Duty driver whose shift timer reads 0 is resting, not violating.
    in_violation = active and snap.hos.shift_seconds <= 0
    if in_violation:
        resend_minutes = getattr(config, "shift_violation_resend_minutes", 30)
        max_resends = getattr(config, "shift_violation_max_resends", 5)
        first_send = not state.shift_violation_active(did)
        resends_sent = state.shift_violation_resend_count(did)
        due = first_send or (
            resends_sent < max_resends
            and state.shift_violation_due(did, resend_minutes, now)
        )
        if due:
            violation_targets = [(DRIVER_GROUP, driver_chat)]  # FIX-1: driver group only
            if getattr(config, "shift_violation_also_notify_dispatch", False):
                violation_targets.append((TEAM_GROUP, team_chat))
            for audience, chat_id in violation_targets:
                if not chat_id:
                    continue
                # Marked sent on successful send (Alert.commit), not here.
                alerts.append(
                    Alert(
                        kind=KIND_SHIFT_VIOLATION,
                        audience=audience,
                        chat_id=chat_id,
                        company=company.name,
                        driver_name=snap.name,
                        driver_id=did,
                        text=messages.shift_violation_text(snap, config.shift_limit_hours, tag, log_url),
                        dedupe_key=f"{did}:shift_violation:{'first' if first_send else resends_sent + 1}",
                        is_resend=not first_send,
                    )
                )
    else:
        state.clear_shift_violation(did)

    # --- Rule 3: ELD disconnected while active ---------------------------- #
    # Disabled via config (disconnect_alerts_enabled: false) — the group only
    # gets low-hours updates and shift violations.
    disconnected = getattr(config, "disconnect_alerts_enabled", True) and (
        snap.connection.has_vehicle and (
            snap.connection.is_offline
            or snap.connection.is_stale(config.disconnect_stale_minutes, now)
        )
    )
    if active and disconnected and driver_chat:
        if state.disconnect_due(did, config.disconnect_realert_minutes, now):
            # FIX-5: status_phrase always comes from the SAME snap the status
            # card is rendered from (_attach_log_image below), so the message
            # and the card can never disagree. An unrecognized duty status
            # means skip-and-log rather than guess at wording.
            status_phrase = messages.disconnect_status_phrase(snap)
            if status_phrase is None:
                log.error(
                    "disconnect alert for %s (%s) skipped — no message phrasing "
                    "for duty status %r; card/text mismatch guard tripped",
                    snap.name, did, snap.duty_status_label,
                )
            else:
                # Timestamp recorded on successful send (Alert.commit), not here.
                alerts.append(
                    Alert(
                        kind=KIND_DISCONNECT,
                        audience=DRIVER_GROUP,
                        chat_id=driver_chat,
                        company=company.name,
                        driver_name=snap.name,
                        driver_id=did,
                        text=messages.disconnect_text(snap, status_phrase, tag, log_url),
                        dedupe_key=f"{did}:disconnect",
                    )
                )
    else:
        # Reconnected or no longer active — reset so a future disconnect alerts.
        state.clear_disconnect(did)

    # --- Rule 4: prolonged On Duty (welfare check) ------------------------ #
    on_duty_hours = getattr(config, "on_duty_alert_hours", 0) or 0
    if on_duty_hours and snap.duty_status_label == "On Duty":
        since_iso = state.on_duty_since(did)
        if not since_iso:
            state.set_on_duty_since(did, now.isoformat())  # entered On Duty now
        else:
            try:
                since = datetime.fromisoformat(since_iso)
            except ValueError:
                since = now
            if since.tzinfo is None:
                since = since.replace(tzinfo=timezone.utc)
            elapsed_h = (now - since).total_seconds() / 3600
            if elapsed_h >= on_duty_hours and not state.on_duty_alerted(did) and driver_chat:
                # Marked alerted on successful send (Alert.commit), not here.
                alerts.append(
                    Alert(
                        kind=KIND_ON_DUTY,
                        audience=DRIVER_GROUP,
                        chat_id=driver_chat,
                        company=company.name,
                        driver_name=snap.name,
                        driver_id=did,
                        text=messages.on_duty_text(snap, on_duty_hours, tag, log_url),
                        dedupe_key=f"{did}:on_duty",
                    )
                )
    else:
        # Not On Duty (or disabled) — reset the episode.
        state.clear_on_duty(did)

    # No @username but we have a user-id -> attach a user-id mention so the
    # sender prepends the name + a text_mention entity (pings them anyway).
    if mention_uid:
        alerts = [replace(a, mention_user_id=mention_uid, mention_name=snap.name)
                  for a in alerts]

    return _attach_log_image(alerts, snap, config)


def evaluate_company(snapshots: list[DriverSnapshot], *, company, config, state, now,
                     registry=None) -> list[Alert]:
    alerts: list[Alert] = []
    for snap in snapshots:
        alerts.extend(
            evaluate_driver(snap, company=company, config=config, state=state,
                            now=now, registry=registry)
        )
    return alerts
