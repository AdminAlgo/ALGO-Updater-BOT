"""Message templates — the exact text sent for each alert kind (FIX-6).

Central home for every driver-facing message string. Thresholds and targets
live in config.py / config.yaml; wording lives here. rules.py builds the DATA
(which driver, which timer, which duty status) and calls these functions to
render it — it does not contain literal message text itself.
"""

from __future__ import annotations

from .eld.base import DriverSnapshot

# Phrase describing what the driver was doing when the ELD disconnected —
# always derived from the same duty-status label the status card image uses,
# so the message text and the card can never disagree (FIX-5).
DUTY_STATUS_PHRASES: dict[str, str] = {
    "Driving": "while you are driving",
    "On Duty": "while you are On Duty",
    "Yard Move": "while you are in Yard Move",
    "Personal Conveyance": "while you are in Personal Conveyance",
}


def disconnect_status_phrase(snap: DriverSnapshot) -> str | None:
    """The disconnect message's status phrase, or None if this duty status has
    no known phrasing — the caller must skip sending rather than guess
    (FIX-5's card/text mismatch guard)."""
    return DUTY_STATUS_PHRASES.get(snap.duty_status_label)


def render_log_url(template: str | None, snap: DriverSnapshot) -> str | None:
    """Fill a driver-log URL template with this driver's identifiers."""
    if not template:
        return None
    try:
        return template.format(
            driver_id=snap.driver_id, username=snap.username or "", name=snap.name,
        )
    except (KeyError, IndexError, ValueError):
        return template  # unknown placeholder — use template as-is


def _driver_name(snap: DriverSnapshot) -> str:
    return snap.name.strip() if snap.name and snap.name.strip() else "Driver"


def _finalize(text: str, tag: str | None = None, log_url: str | None = None) -> str:
    """Prepend the driver @mention (to notify them) and append the log link."""
    if tag:
        text = f"{tag}\n\n{text}"
    if log_url:
        text = f"{text}\n\n📋 Driver log: {log_url}"
    return text


def _amount(seconds: int) -> str:
    hours, minutes = seconds // 3600, (seconds % 3600) // 60

    def _u(n, unit):
        return f"{n} {unit}" + ("" if n == 1 else "s")

    if hours and minutes:
        return f"{_u(hours, 'hour')} and {_u(minutes, 'minute')}"
    if hours:
        return _u(hours, "hour")
    return _u(minutes, "minute")


def low_hours_text(snap: DriverSnapshot, tag: str | None = None,
                   log_url: str | None = None) -> str:
    # The triggering timer is the most urgent (lowest) of Drive / Break / Shift.
    timers = {
        "Drive": snap.hos.drive_seconds,
        "Break": snap.hos.break_seconds,
        "Shift": snap.hos.shift_seconds,
    }
    clock_name, secs = min(timers.items(), key=lambda kv: kv[1])
    return _finalize(
        "Hello.\n"
        f"Dear {_driver_name(snap)}\n\n"
        f"You have only {_amount(secs)} on your {clock_name}. Please stop "
        "within those hours. Let us know if you need help.\n\n"
        "Thank you.",
        tag, log_url,
    )


def shift_violation_text(snap: DriverSnapshot, shift_limit_hours: int,
                         tag: str | None = None, log_url: str | None = None) -> str:
    return _finalize(
        "Hello.\n"
        f"Dear {_driver_name(snap)}\n\n"
        f"Your {shift_limit_hours}-hour Shift limit is finished (00:00). You "
        "are in violation and must stop driving now.\n\n"
        "Please go Off Duty and let us know if you need help.\n\n"
        "Thank you.",
        tag, log_url,
    )


def disconnect_text(snap: DriverSnapshot, status_phrase: str, tag: str | None = None,
                    log_url: str | None = None) -> str:
    return _finalize(
        "Hello.\n"
        f"Dear {_driver_name(snap)}\n\n"
        f"Your ELD device appears to be DISCONNECTED {status_phrase}. Please "
        "reconnect it as soon as possible so your ELD will record correctly.\n\n"
        "If you are having trouble connecting, let us know please.\n\n"
        "Thank you.",
        tag, log_url,
    )


def cycle_text(snap: DriverSnapshot, tag: str | None = None,
               log_url: str | None = None) -> str:
    return _finalize(
        "Hello.\n"
        f"Dear {_driver_name(snap)}\n\n"
        f"You have only {_amount(snap.hos.cycle_seconds)} left on your "
        "70-hour Cycle. Please plan your reset.\n\n"
        "Let us know if you need help.\n\n"
        "Thank you.",
        tag, log_url,
    )


def on_duty_text(snap: DriverSnapshot, hours, tag: str | None = None,
                 log_url: str | None = None) -> str:
    h = int(hours) if float(hours).is_integer() else hours
    return _finalize(
        "Hello.\n"
        f"Dear {_driver_name(snap)}\n\n"
        f"We noticed you have been On Duty for more than {h} hours. "
        "Is everything okay?\n"
        "If you need any help, please let us know.\n\n"
        "Thank you.",
        tag, log_url,
    )
