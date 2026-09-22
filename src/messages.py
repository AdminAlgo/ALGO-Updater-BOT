"""Message templates — the exact text sent for each alert kind (FIX-6).

Central home for every driver-facing message string, in every supported
language. Thresholds and targets live in config.py / config.yaml; wording
lives here. rules.py builds the DATA (which driver, which timer, which duty
status) and calls these functions to render it — it does not contain literal
message text itself.

Languages: "en" (English), "ru" (Russian), "uz" (Uzbek, Latin script),
"es" (Spanish). Every ``*_text()`` takes a ``language`` keyword; an unknown
language falls back to English — a driver never receives a blank message. ELD
terms the driver sees in the app (Drive / Shift / Break / Cycle / On Duty /
Off Duty) stay in English inside the translations, with the local word in
brackets where it helps.

Operators can replace any kind's wording per language from the dashboard
(Warnings → Alert texts). Those overrides are installed with
``set_overrides()`` and win over the defaults below; a broken override (bad
braces) is ignored at send time so an edit can never silence an alert.
"""

from __future__ import annotations

import string

from .eld.base import DriverSnapshot

DEFAULT_LANGUAGE = "en"

#: language code -> display name (for the dashboard).
LANGUAGES: dict[str, str] = {
    "en": "English",
    "ru": "Русский",
    "uz": "O'zbek",
    "es": "Español",
}


def normalize_language(value) -> str:
    v = str(value or "").strip().lower()
    return v if v in LANGUAGES else DEFAULT_LANGUAGE


# --------------------------------------------------------------------------- #
# Translation tables
# --------------------------------------------------------------------------- #
# Phrase describing what the driver was doing when the ELD disconnected —
# always derived from the same duty-status label the status card image uses,
# so the message text and the card can never disagree (FIX-5).
DUTY_STATUS_PHRASES: dict[str, dict[str, str]] = {
    "en": {
        "Driving": "while you are driving",
        "On Duty": "while you are On Duty",
        "Yard Move": "while you are in Yard Move",
        "Personal Conveyance": "while you are in Personal Conveyance",
    },
    "ru": {
        "Driving": "во время движения",
        "On Duty": "пока вы в статусе On Duty",
        "Yard Move": "пока вы в статусе Yard Move",
        "Personal Conveyance": "пока вы в статусе Personal Conveyance",
    },
    "uz": {
        "Driving": "siz haydayotganingizda",
        "On Duty": "siz On Duty holatida bo'lganingizda",
        "Yard Move": "siz Yard Move holatida bo'lganingizda",
        "Personal Conveyance": "siz Personal Conveyance holatida bo'lganingizda",
    },
    "es": {
        "Driving": "mientras conduce",
        "On Duty": "mientras está en On Duty",
        "Yard Move": "mientras está en Yard Move",
        "Personal Conveyance": "mientras está en Personal Conveyance",
    },
}

# Timer names as they appear in the low-hours message.
CLOCK_NAMES: dict[str, dict[str, str]] = {
    "en": {"Drive": "Drive", "Break": "Break", "Shift": "Shift"},
    "ru": {"Drive": "Drive (вождение)", "Break": "Break (перерыв)", "Shift": "Shift (смена)"},
    "uz": {"Drive": "Drive (haydash)", "Break": "Break (tanaffus)", "Shift": "Shift (smena)"},
    "es": {"Drive": "Drive (conducción)", "Break": "Break (descanso)", "Shift": "Shift (turno)"},
}

FALLBACK_NAME: dict[str, str] = {
    "en": "Driver", "ru": "Водитель", "uz": "Haydovchi", "es": "Conductor",
}

LOG_LINK_LABEL: dict[str, str] = {
    "en": "📋 Driver log:",
    "ru": "📋 Журнал водителя:",
    "uz": "📋 Haydovchi jurnali:",
    "es": "📋 Registro del conductor:",
}

# Alert kinds, in the order the dashboard lists them. Kept here (not only in
# rules.py) so the text editor and the rules agree on one list.
KIND_LABELS: dict[str, str] = {
    "low_hours": "Low hours (Drive / Shift / Break)",
    "cycle": "70-hour Cycle low",
    "shift_violation": "14-hour Shift violation",
    "disconnect": "ELD disconnected",
    "on_duty": "Long On Duty (welfare check)",
    "off_duty_checkin": "Off Duty for days (vacation check-in)",
}

#: the placeholders each kind's text may use — the editor validates against these.
PLACEHOLDERS: dict[str, tuple[str, ...]] = {
    "low_hours": ("name", "amount", "clock"),
    "cycle": ("name", "amount"),
    "shift_violation": ("name", "limit"),
    "disconnect": ("name", "status"),
    "on_duty": ("name", "hours"),
    "off_duty_checkin": ("name", "days"),
}

# Message bodies, per kind, per language.
TEMPLATES: dict[str, dict[str, str]] = {
    "low_hours": {
        "en": (
            "Hello.\n"
            "Dear {name}\n\n"
            "You have only {amount} on your {clock}. Please stop within those "
            "hours. Let us know if you need help.\n\n"
            "Thank you."
        ),
        "ru": (
            "Здравствуйте.\n"
            "Уважаемый {name}\n\n"
            "У вас осталось всего {amount} на таймере {clock}. Пожалуйста, "
            "остановитесь в пределах этого времени. Сообщите нам, если нужна "
            "помощь.\n\n"
            "Спасибо."
        ),
        "uz": (
            "Salom.\n"
            "Hurmatli {name}\n\n"
            "Sizda {clock} taymerida atigi {amount} qoldi. Iltimos, shu vaqt "
            "ichida to'xtang. Yordam kerak bo'lsa, bizga xabar bering.\n\n"
            "Rahmat."
        ),
        "es": (
            "Hola.\n"
            "Estimado {name}\n\n"
            "Su tiempo restante en {clock} es de solo {amount}. Por favor, "
            "deténgase dentro de ese tiempo. Avísenos si necesita ayuda.\n\n"
            "Gracias."
        ),
    },
    "shift_violation": {
        "en": (
            "Hello.\n"
            "Dear {name}\n\n"
            "Your {limit}-hour Shift limit is finished (00:00). You are in "
            "violation and must stop driving now.\n\n"
            "Please go Off Duty and let us know if you need help.\n\n"
            "Thank you."
        ),
        "ru": (
            "Здравствуйте.\n"
            "Уважаемый {name}\n\n"
            "Ваш {limit}-часовой лимит смены (Shift) закончился (00:00). Вы "
            "находитесь в нарушении и должны немедленно прекратить движение.\n\n"
            "Пожалуйста, перейдите в статус Off Duty и сообщите нам, если нужна "
            "помощь.\n\n"
            "Спасибо."
        ),
        "uz": (
            "Salom.\n"
            "Hurmatli {name}\n\n"
            "Sizning {limit} soatlik smena (Shift) limitingiz tugadi (00:00). "
            "Siz qoidabuzarlik holatidasiz va hoziroq haydashni to'xtatishingiz "
            "kerak.\n\n"
            "Iltimos, Off Duty holatiga o'ting va yordam kerak bo'lsa, bizga "
            "xabar bering.\n\n"
            "Rahmat."
        ),
        "es": (
            "Hola.\n"
            "Estimado {name}\n\n"
            "Su límite de turno (Shift) de {limit} horas ha terminado (00:00). "
            "Está en infracción y debe dejar de conducir ahora mismo.\n\n"
            "Por favor, pase a Off Duty y avísenos si necesita ayuda.\n\n"
            "Gracias."
        ),
    },
    "disconnect": {
        "en": (
            "Hello.\n"
            "Dear {name}\n\n"
            "Your ELD device appears to be DISCONNECTED {status}. Please "
            "reconnect it as soon as possible so your ELD will record "
            "correctly.\n\n"
            "If you are having trouble connecting, let us know please.\n\n"
            "Thank you."
        ),
        "ru": (
            "Здравствуйте.\n"
            "Уважаемый {name}\n\n"
            "Ваше устройство ELD, похоже, ОТКЛЮЧЕНО {status}. Пожалуйста, "
            "подключите его как можно скорее, чтобы ELD правильно записывал "
            "данные.\n\n"
            "Если не получается подключиться, сообщите нам.\n\n"
            "Спасибо."
        ),
        "uz": (
            "Salom.\n"
            "Hurmatli {name}\n\n"
            "Sizning ELD qurilmangiz {status} UZILGAN ko'rinadi. ELD to'g'ri "
            "yozib borishi uchun iltimos, uni imkon qadar tezroq qayta "
            "ulang.\n\n"
            "Ulashda muammo bo'lsa, bizga xabar bering.\n\n"
            "Rahmat."
        ),
        "es": (
            "Hola.\n"
            "Estimado {name}\n\n"
            "Su dispositivo ELD parece estar DESCONECTADO {status}. Por favor, "
            "vuelva a conectarlo lo antes posible para que su ELD registre "
            "correctamente.\n\n"
            "Si tiene problemas para conectarlo, avísenos.\n\n"
            "Gracias."
        ),
    },
    "cycle": {
        "en": (
            "Hello.\n"
            "Dear {name}\n\n"
            "You have only {amount} left on your 70-hour Cycle. Please plan "
            "your reset.\n\n"
            "Let us know if you need help.\n\n"
            "Thank you."
        ),
        "ru": (
            "Здравствуйте.\n"
            "Уважаемый {name}\n\n"
            "У вас осталось всего {amount} на 70-часовом цикле (Cycle). "
            "Пожалуйста, запланируйте перезагрузку (reset).\n\n"
            "Сообщите нам, если нужна помощь.\n\n"
            "Спасибо."
        ),
        "uz": (
            "Salom.\n"
            "Hurmatli {name}\n\n"
            "70 soatlik siklingizda (Cycle) atigi {amount} qoldi. Iltimos, "
            "reset (qayta tiklash)ni rejalashtiring.\n\n"
            "Yordam kerak bo'lsa, bizga xabar bering.\n\n"
            "Rahmat."
        ),
        "es": (
            "Hola.\n"
            "Estimado {name}\n\n"
            "Su tiempo restante en el ciclo de 70 horas (Cycle) es de solo "
            "{amount}. Por favor, planifique su reinicio (reset).\n\n"
            "Avísenos si necesita ayuda.\n\n"
            "Gracias."
        ),
    },
    "on_duty": {
        "en": (
            "Hello.\n"
            "Dear {name}\n\n"
            "We noticed you have been On Duty for more than {hours} hours. "
            "Is everything okay?\n"
            "If you need any help, please let us know.\n\n"
            "Thank you."
        ),
        "ru": (
            "Здравствуйте.\n"
            "Уважаемый {name}\n\n"
            "Мы заметили, что вы находитесь в статусе On Duty уже более {hours} "
            "часов. Всё в порядке?\n"
            "Если нужна помощь, сообщите нам.\n\n"
            "Спасибо."
        ),
        "uz": (
            "Salom.\n"
            "Hurmatli {name}\n\n"
            "Siz {hours} soatdan ortiq On Duty holatida ekaningizni payqadik. "
            "Hammasi joyidami?\n"
            "Yordam kerak bo'lsa, bizga xabar bering.\n\n"
            "Rahmat."
        ),
        "es": (
            "Hola.\n"
            "Estimado {name}\n\n"
            "Notamos que ha estado en On Duty por más de {hours} horas. "
            "¿Está todo bien?\n"
            "Si necesita ayuda, avísenos.\n\n"
            "Gracias."
        ),
    },
    # Vacation / home-time check-in: the driver has been off (Off Duty,
    # Sleeper or PC — anything but Driving / On Duty / Yard Move) for days.
    "off_duty_checkin": {
        "en": (
            "Hello.\n"
            "Dear {name}\n\n"
            "We noticed you have been Off Duty for {days}. Just checking in — "
            "is everything okay? Please let us know when you plan to be back "
            "on duty.\n\n"
            "Thank you."
        ),
        "ru": (
            "Здравствуйте.\n"
            "Уважаемый {name}\n\n"
            "Мы заметили, что вы находитесь в статусе Off Duty уже {days}. "
            "Просто хотим узнать — всё ли у вас в порядке? Пожалуйста, "
            "сообщите нам, когда планируете вернуться к работе (On Duty).\n\n"
            "Спасибо."
        ),
        "uz": (
            "Salom.\n"
            "Hurmatli {name}\n\n"
            "Siz {days}dan beri Off Duty holatida ekaningizni payqadik. "
            "Hammasi joyidami? Iltimos, qachon ishga (On Duty) qaytishni "
            "rejalashtirayotganingizni bizga xabar bering.\n\n"
            "Rahmat."
        ),
        "es": (
            "Hola.\n"
            "Estimado {name}\n\n"
            "Notamos que ha estado en Off Duty durante {days}. Solo queremos "
            "saber: ¿está todo bien? Por favor, avísenos cuándo planea volver "
            "a estar en servicio (On Duty).\n\n"
            "Gracias."
        ),
    },
}


# --------------------------------------------------------------------------- #
# Operator overrides (dashboard → Warnings → Alert texts)
# --------------------------------------------------------------------------- #
_OVERRIDES: dict[str, dict[str, str]] = {}


def set_overrides(mapping: dict | None) -> None:
    """Install operator-written texts ({kind: {language: text}}). Replaces the
    whole set atomically, so a reader never sees a half-applied edit."""
    global _OVERRIDES
    clean: dict[str, dict[str, str]] = {}
    for kind, by_lang in (mapping or {}).items():
        if kind not in TEMPLATES or not isinstance(by_lang, dict):
            continue
        texts = {normalize_language(lang): str(text) for lang, text in by_lang.items()
                 if isinstance(text, str) and text.strip()}
        if texts:
            clean[kind] = texts
    _OVERRIDES = clean


def overrides() -> dict[str, dict[str, str]]:
    return {k: dict(v) for k, v in _OVERRIDES.items()}


def default_template(kind: str, language: str) -> str:
    table = TEMPLATES.get(kind) or {}
    return table.get(normalize_language(language)) or table[DEFAULT_LANGUAGE]


def template_for(kind: str, language: str) -> str:
    """The message body for a kind in a language: operator text first, then the
    built-in translation, then English."""
    lang = normalize_language(language)
    custom = (_OVERRIDES.get(kind) or {}).get(lang)
    return custom or default_template(kind, lang)


class _Blank(dict):
    """format_map helper: an unknown {placeholder} renders as itself instead of
    raising, so a typo in an operator's text can't swallow an alert."""

    def __missing__(self, key):
        return "{" + key + "}"


def template_problems(kind: str, text: str) -> list[str]:
    """Why an operator's text would not render — empty list if it is fine."""
    allowed = set(PLACEHOLDERS.get(kind, ()))
    problems: list[str] = []
    try:
        fields = [f for _, f, _, _ in string.Formatter().parse(text) if f is not None]
    except ValueError as exc:
        return [f"broken braces — {exc}. Use {{name}} style placeholders only."]
    for f in fields:
        base = f.split(".")[0].split("[")[0]
        if base == "" or base.isdigit():
            problems.append("empty {} braces — write the placeholder name inside them")
        elif base not in allowed:
            problems.append(f"unknown placeholder {{{base}}} — allowed: "
                            + ", ".join("{" + a + "}" for a in sorted(allowed)))
    return problems


def _render(kind: str, language: str, **values) -> str:
    lang = normalize_language(language)
    text = template_for(kind, lang)
    try:
        return text.format_map(_Blank(values))
    except (ValueError, IndexError, AttributeError):
        # A broken operator override must never cost the driver the alert.
        return default_template(kind, lang).format_map(_Blank(values))


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def disconnect_status_phrase(snap: DriverSnapshot, language: str = DEFAULT_LANGUAGE) -> str | None:
    """The disconnect message's status phrase, or None if this duty status has
    no known phrasing — the caller must skip sending rather than guess
    (FIX-5's card/text mismatch guard). Falls back to English wording if the
    language has no phrase for this status."""
    lang = normalize_language(language)
    phrases = DUTY_STATUS_PHRASES.get(lang) or DUTY_STATUS_PHRASES[DEFAULT_LANGUAGE]
    return phrases.get(snap.duty_status_label) or DUTY_STATUS_PHRASES[DEFAULT_LANGUAGE].get(
        snap.duty_status_label
    )


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


def _driver_name(snap: DriverSnapshot, language: str = DEFAULT_LANGUAGE) -> str:
    if snap.name and snap.name.strip():
        return snap.name.strip()
    return FALLBACK_NAME.get(normalize_language(language), FALLBACK_NAME[DEFAULT_LANGUAGE])


def _finalize(text: str, tag: str | None = None, log_url: str | None = None,
              language: str = DEFAULT_LANGUAGE) -> str:
    """Prepend the driver @mention (to notify them) and append the log link."""
    if tag:
        text = f"{tag}\n\n{text}"
    if log_url:
        label = LOG_LINK_LABEL.get(normalize_language(language), LOG_LINK_LABEL[DEFAULT_LANGUAGE])
        text = f"{text}\n\n{label} {log_url}"
    return text


def _ru_plural(n: int, one: str, few: str, many: str) -> str:
    """Russian plural form: 1 час / 2 часа / 5 часов (11–14 -> many)."""
    n_abs = abs(n)
    if 11 <= n_abs % 100 <= 14:
        return many
    last = n_abs % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many


_UNITS = {
    "en": {"hour": ("hour", "hours"), "minute": ("minute", "minutes"), "day": ("day", "days")},
    "es": {"hour": ("hora", "horas"), "minute": ("minuto", "minutos"), "day": ("día", "días")},
    "uz": {"hour": ("soat", "soat"), "minute": ("daqiqa", "daqiqa"), "day": ("kun", "kun")},
}
_RU_UNITS = {
    "hour": ("час", "часа", "часов"),
    "minute": ("минута", "минуты", "минут"),
    "day": ("день", "дня", "дней"),
}


def _unit(n: int, unit: str, language: str) -> str:
    if language == "ru":
        return f"{n} {_ru_plural(n, *_RU_UNITS[unit])}"
    one, many = (_UNITS.get(language) or _UNITS["en"])[unit]
    return f"{n} {one if n == 1 else many}"


_JOINER = {"en": "and", "ru": "и", "uz": "va", "es": "y"}


def _amount(seconds: int, language: str = DEFAULT_LANGUAGE) -> str:
    lang = normalize_language(language)
    hours, minutes = seconds // 3600, (seconds % 3600) // 60
    if hours and minutes:
        return f"{_unit(hours, 'hour', lang)} {_JOINER[lang]} {_unit(minutes, 'minute', lang)}"
    if hours:
        return _unit(hours, "hour", lang)
    return _unit(minutes, "minute", lang)


def days_text(days: int, language: str = DEFAULT_LANGUAGE) -> str:
    """"2 days" / "2 дня" / "2 kun" / "2 días"."""
    return _unit(int(days), "day", normalize_language(language))


def _fmt_hours(hours) -> str:
    try:
        return str(int(hours)) if float(hours).is_integer() else str(hours)
    except (TypeError, ValueError):
        return str(hours)


# --------------------------------------------------------------------------- #
# Renderers — one per alert kind
# --------------------------------------------------------------------------- #
def low_hours_text(snap: DriverSnapshot, tag: str | None = None,
                   log_url: str | None = None, language: str = DEFAULT_LANGUAGE) -> str:
    lang = normalize_language(language)
    # The triggering timer is the most urgent (lowest) of Drive / Break / Shift.
    timers = {
        "Drive": snap.hos.drive_seconds,
        "Break": snap.hos.break_seconds,
        "Shift": snap.hos.shift_seconds,
    }
    clock_name, secs = min(timers.items(), key=lambda kv: kv[1])
    clock = (CLOCK_NAMES.get(lang) or CLOCK_NAMES[DEFAULT_LANGUAGE]).get(clock_name, clock_name)
    body = _render("low_hours", lang, name=_driver_name(snap, lang),
                   amount=_amount(secs, lang), clock=clock)
    return _finalize(body, tag, log_url, lang)


def shift_violation_text(snap: DriverSnapshot, shift_limit_hours: int,
                         tag: str | None = None, log_url: str | None = None,
                         language: str = DEFAULT_LANGUAGE) -> str:
    lang = normalize_language(language)
    body = _render("shift_violation", lang, name=_driver_name(snap, lang),
                   limit=shift_limit_hours)
    return _finalize(body, tag, log_url, lang)


def disconnect_text(snap: DriverSnapshot, status_phrase: str, tag: str | None = None,
                    log_url: str | None = None, language: str = DEFAULT_LANGUAGE) -> str:
    lang = normalize_language(language)
    body = _render("disconnect", lang, name=_driver_name(snap, lang), status=status_phrase)
    return _finalize(body, tag, log_url, lang)


def cycle_text(snap: DriverSnapshot, tag: str | None = None,
               log_url: str | None = None, language: str = DEFAULT_LANGUAGE) -> str:
    lang = normalize_language(language)
    body = _render("cycle", lang, name=_driver_name(snap, lang),
                   amount=_amount(snap.hos.cycle_seconds, lang))
    return _finalize(body, tag, log_url, lang)


def on_duty_text(snap: DriverSnapshot, hours, tag: str | None = None,
                 log_url: str | None = None, language: str = DEFAULT_LANGUAGE) -> str:
    lang = normalize_language(language)
    body = _render("on_duty", lang, name=_driver_name(snap, lang), hours=_fmt_hours(hours))
    return _finalize(body, tag, log_url, lang)


def off_duty_checkin_text(snap: DriverSnapshot, days: int, tag: str | None = None,
                          log_url: str | None = None,
                          language: str = DEFAULT_LANGUAGE) -> str:
    lang = normalize_language(language)
    body = _render("off_duty_checkin", lang, name=_driver_name(snap, lang),
                   days=days_text(days, lang))
    return _finalize(body, tag, log_url, lang)


def sample_values(kind: str, language: str = DEFAULT_LANGUAGE) -> dict[str, str]:
    """Example placeholder values — for validating and previewing an edit."""
    lang = normalize_language(language)
    clocks = CLOCK_NAMES.get(lang) or CLOCK_NAMES[DEFAULT_LANGUAGE]
    return {
        "name": "John Smith",
        "amount": _amount(3600 + 30 * 60, lang),
        "clock": clocks["Drive"],
        "limit": "14",
        "status": (DUTY_STATUS_PHRASES.get(lang) or DUTY_STATUS_PHRASES["en"])["Driving"],
        "hours": "2",
        "days": days_text(2, lang),
    }


def preview(kind: str, language: str, text: str | None = None) -> str:
    """Render ``text`` (or the current template) with example values."""
    body = text if text is not None else template_for(kind, language)
    try:
        return body.format_map(_Blank(sample_values(kind, language)))
    except (ValueError, IndexError, AttributeError):
        return body
