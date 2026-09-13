"""Message templates — the exact text sent for each alert kind (FIX-6).

Central home for every driver-facing message string. Thresholds and targets
live in config.py / config.yaml; wording lives here. rules.py builds the DATA
(which driver, which timer, which duty status) and calls these functions to
render it — it does not contain literal message text itself.

Multi-language (upgrade spec §5.1): every ``*_text`` function takes a
``language`` code ("en" | "ru" | "uz" | "es"). English and Russian have real
translations; Uzbek and Spanish fall back to English until real copy exists
(never send a blank message) — done via ``_pick`` below.
"""

from __future__ import annotations

from .eld.base import DriverSnapshot

DEFAULT_LANGUAGE = "en"


def _pick(templates: dict[str, str], language: str) -> str:
    """Look up a language's template, falling back to English."""
    return templates.get(language) or templates[DEFAULT_LANGUAGE]


# --------------------------------------------------------------------------- #
# Duty-status phrase (disconnect alert) — always derived from the same
# duty-status label the status card image uses, so the message text and the
# card can never disagree (FIX-5).
# --------------------------------------------------------------------------- #
DUTY_STATUS_PHRASES: dict[str, dict[str, str]] = {
    "en": {
        "Driving": "while you are driving",
        "On Duty": "while you are On Duty",
        "Yard Move": "while you are in Yard Move",
        "Personal Conveyance": "while you are in Personal Conveyance",
    },
    "ru": {
        "Driving": "во время движения",
        "On Duty": "во время дежурства (On Duty)",
        "Yard Move": "во время маневрирования по территории (Yard Move)",
        "Personal Conveyance": "при личном использовании (Personal Conveyance)",
    },
}


def disconnect_status_phrase(snap: DriverSnapshot, language: str = DEFAULT_LANGUAGE) -> str | None:
    """The disconnect message's status phrase, or None if this duty status has
    no known phrasing — the caller must skip sending rather than guess
    (FIX-5's card/text mismatch guard)."""
    table = DUTY_STATUS_PHRASES.get(language) or DUTY_STATUS_PHRASES[DEFAULT_LANGUAGE]
    return table.get(snap.duty_status_label) or DUTY_STATUS_PHRASES[DEFAULT_LANGUAGE].get(snap.duty_status_label)


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


def _finalize(text: str, tag: str | None = None, log_url: str | None = None,
             language: str = DEFAULT_LANGUAGE) -> str:
    """Prepend the driver @mention (to notify them) and append the log link."""
    if tag:
        text = f"{tag}\n\n{text}"
    if log_url:
        label = {"ru": "Журнал водителя"}.get(language, "Driver log")
        text = f"{text}\n\n📋 {label}: {log_url}"
    return text


# --------------------------------------------------------------------------- #
# Localized number formatting — "2 hours and 15 minutes" / "2 часа 15 минут".
# --------------------------------------------------------------------------- #
def _ru_word(n: int, one: str, few: str, many: str) -> str:
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14):
        return few
    return many


def _amount_en(seconds: int) -> str:
    hours, minutes = seconds // 3600, (seconds % 3600) // 60

    def _u(n, unit):
        return f"{n} {unit}" + ("" if n == 1 else "s")

    if hours and minutes:
        return f"{_u(hours, 'hour')} and {_u(minutes, 'minute')}"
    if hours:
        return _u(hours, "hour")
    return _u(minutes, "minute")


def _amount_ru(seconds: int) -> str:
    hours, minutes = seconds // 3600, (seconds % 3600) // 60

    def _h(n):
        return f"{n} {_ru_word(n, 'час', 'часа', 'часов')}"

    def _m(n):
        return f"{n} {_ru_word(n, 'минута', 'минуты', 'минут')}"

    if hours and minutes:
        return f"{_h(hours)} {_m(minutes)}"
    if hours:
        return _h(hours)
    return _m(minutes)


_AMOUNT_FNS = {"en": _amount_en, "ru": _amount_ru}


def _amount(seconds: int, language: str = DEFAULT_LANGUAGE) -> str:
    fn = _AMOUNT_FNS.get(language) or _AMOUNT_FNS[DEFAULT_LANGUAGE]
    return fn(seconds)


_CLOCK_NAMES: dict[str, dict[str, str]] = {
    "en": {"Drive": "Drive", "Break": "Break", "Shift": "Shift"},
    "ru": {"Drive": "Вождению", "Break": "Перерыву", "Shift": "Смене"},
}


def _clock_name(key: str, language: str = DEFAULT_LANGUAGE) -> str:
    table = _CLOCK_NAMES.get(language) or _CLOCK_NAMES[DEFAULT_LANGUAGE]
    return table.get(key, key)


# --------------------------------------------------------------------------- #
# Message bodies
# --------------------------------------------------------------------------- #
_LOW_HOURS = {
    "en": ("Hello.\nDear {name}\n\nYou have only {amount} on your {clock}. Please stop "
           "within those hours. Let us know if you need help.\n\nThank you."),
    "ru": ("Здравствуйте.\nУважаемый(ая) {name}\n\nУ вас осталось всего {amount} по показателю "
           "«{clock}». Пожалуйста, остановитесь в течение этого времени. Сообщите нам, если "
           "нужна помощь.\n\nСпасибо."),
}


def low_hours_text(snap: DriverSnapshot, tag: str | None = None,
                   log_url: str | None = None, language: str = DEFAULT_LANGUAGE) -> str:
    # The triggering timer is the most urgent (lowest) of Drive / Break / Shift.
    timers = {
        "Drive": snap.hos.drive_seconds,
        "Break": snap.hos.break_seconds,
        "Shift": snap.hos.shift_seconds,
    }
    clock_key, secs = min(timers.items(), key=lambda kv: kv[1])
    text = _pick(_LOW_HOURS, language).format(
        name=_driver_name(snap), amount=_amount(secs, language),
        clock=_clock_name(clock_key, language),
    )
    return _finalize(text, tag, log_url, language)


_SHIFT_VIOLATION = {
    "en": ("Hello.\nDear {name}\n\nYour {limit}-hour Shift limit is finished (00:00). You "
           "are in violation and must stop driving now.\n\nPlease go Off Duty and let us "
           "know if you need help.\n\nThank you."),
    "ru": ("Здравствуйте.\nУважаемый(ая) {name}\n\nВаш лимит смены в {limit} часов исчерпан "
           "(00:00). Вы нарушаете правила и должны немедленно прекратить движение.\n\n"
           "Пожалуйста, перейдите в статус Off Duty и сообщите нам, если нужна помощь.\n\n"
           "Спасибо."),
}


def shift_violation_text(snap: DriverSnapshot, shift_limit_hours: int,
                         tag: str | None = None, log_url: str | None = None,
                         language: str = DEFAULT_LANGUAGE) -> str:
    text = _pick(_SHIFT_VIOLATION, language).format(
        name=_driver_name(snap), limit=shift_limit_hours,
    )
    return _finalize(text, tag, log_url, language)


_DISCONNECT = {
    "en": ("Hello.\nDear {name}\n\nYour ELD device appears to be DISCONNECTED {status}. Please "
           "reconnect it as soon as possible so your ELD will record correctly.\n\nIf you are "
           "having trouble connecting, let us know please.\n\nThank you."),
    "ru": ("Здравствуйте.\nУважаемый(ая) {name}\n\nПохоже, ваше устройство ELD ОТКЛЮЧЕНО "
           "{status}. Пожалуйста, переподключите его как можно скорее, чтобы ELD записывал "
           "данные корректно.\n\nЕсли возникли проблемы с подключением, сообщите нам, "
           "пожалуйста.\n\nСпасибо."),
}


def disconnect_text(snap: DriverSnapshot, status_phrase: str, tag: str | None = None,
                    log_url: str | None = None, language: str = DEFAULT_LANGUAGE) -> str:
    text = _pick(_DISCONNECT, language).format(name=_driver_name(snap), status=status_phrase)
    return _finalize(text, tag, log_url, language)


_CYCLE = {
    "en": ("Hello.\nDear {name}\n\nYou have only {amount} left on your 70-hour Cycle. Please "
           "plan your reset.\n\nLet us know if you need help.\n\nThank you."),
    "ru": ("Здравствуйте.\nУважаемый(ая) {name}\n\nУ вас осталось всего {amount} по 70-часовому "
           "циклу (Cycle). Пожалуйста, спланируйте свой reset.\n\nСообщите нам, если нужна "
           "помощь.\n\nСпасибо."),
}


def cycle_text(snap: DriverSnapshot, tag: str | None = None,
               log_url: str | None = None, language: str = DEFAULT_LANGUAGE) -> str:
    text = _pick(_CYCLE, language).format(
        name=_driver_name(snap), amount=_amount(snap.hos.cycle_seconds, language),
    )
    return _finalize(text, tag, log_url, language)


_ON_DUTY = {
    "en": ("Hello.\nDear {name}\n\nWe noticed you have been On Duty for more than {hours} "
           "hours. Is everything okay?\nIf you need any help, please let us know.\n\n"
           "Thank you."),
    "ru": ("Здравствуйте.\nУважаемый(ая) {name}\n\nМы заметили, что вы находитесь в статусе "
           "On Duty более {hours} часов. Всё в порядке?\nЕсли нужна помощь, пожалуйста, "
           "сообщите нам.\n\nСпасибо."),
}


def on_duty_text(snap: DriverSnapshot, hours, tag: str | None = None,
                 log_url: str | None = None, language: str = DEFAULT_LANGUAGE) -> str:
    h = int(hours) if float(hours).is_integer() else hours
    text = _pick(_ON_DUTY, language).format(name=_driver_name(snap), hours=h)
    return _finalize(text, tag, log_url, language)
