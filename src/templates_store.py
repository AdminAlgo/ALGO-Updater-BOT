"""Message-template store for the Warnings/Broadcast composer (upgrade spec §3.3).

``data/message_templates.json``::

    {"templates": [{"id": "...", "name": "...", "created": "...",
                    "text": {"en": "...", "ru": "...", "uz": "...", "es": "..."}}]}

A template with only an "en" string is fine — missing languages fall back to
English at render time (never send a blank message).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from .storage import atomic_write_json, read_json

LANGUAGES = ("en", "ru", "uz", "es")


class TemplateStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path else None
        self._templates: list[dict] = []
        if self._path and self._path.exists():
            self.load()

    def load(self) -> None:
        if not self._path:
            return
        data = read_json(self._path, default={}) or {}
        self._templates = data.get("templates", []) or []

    def save(self) -> None:
        if not self._path:
            return
        atomic_write_json(self._path, {"templates": self._templates})

    def all(self) -> list[dict]:
        return list(self._templates)

    def get(self, template_id: str) -> dict | None:
        return next((t for t in self._templates if t.get("id") == template_id), None)

    def add(self, name: str, text: dict[str, str]) -> dict:
        template = {
            "id": uuid.uuid4().hex[:12],
            "name": name,
            "created": datetime.now(timezone.utc).isoformat(),
            "text": {k: v.strip() for k, v in text.items() if v and v.strip()},
        }
        self._templates.append(template)
        self.save()
        return template

    def update(self, template_id: str, name: str | None = None,
               text: dict[str, str] | None = None) -> bool:
        t = self.get(template_id)
        if not t:
            return False
        if name:
            t["name"] = name
        if text is not None:
            t["text"] = {k: v.strip() for k, v in text.items() if v and v.strip()}
        self.save()
        return True

    def delete(self, template_id: str) -> bool:
        before = len(self._templates)
        self._templates = [t for t in self._templates if t.get("id") != template_id]
        changed = len(self._templates) != before
        if changed:
            self.save()
        return changed

    @staticmethod
    def render(template: dict, language: str, values: dict | None = None) -> str:
        """The template's text in ``language`` (English fallback), with
        {name} / {company} / {truck} filled in. An unknown or broken
        placeholder is left as typed rather than failing the send."""
        text = template.get("text") or {}
        body = text.get(language) or text.get("en") or ""
        if not values:
            return body
        try:
            return body.format_map(_Keep(values))
        except (ValueError, IndexError, AttributeError):
            return body

    def seed_defaults(self) -> None:
        """First run only: ready-made messages the dispatchers asked for."""
        if self._templates:
            return
        for name, text in DEFAULT_TEMPLATES:
            self.add(name, text)


class _Keep(dict):
    def __missing__(self, key):
        return "{" + key + "}"


#: placeholders a broadcast template may use
PLACEHOLDERS = ("name", "company", "truck")

DEFAULT_TEMPLATES = [
    ("Inspection week (CVSA Roadcheck)", {
        "en": ("Hello.\nDear {name}\n\nThis week is inspection week (CVSA International "
               "Roadcheck). Please make sure your ELD is connected and your logs are up to "
               "date, your paperwork (registration, insurance, medical card, ELD instruction "
               "sheet) is in the truck, and do a full pre-trip inspection every day.\n\n"
               "If you have any questions, let us know.\n\nThank you."),
        "ru": ("Здравствуйте.\nУважаемый {name}\n\nНа этой неделе проходит неделя инспекций "
               "(CVSA International Roadcheck). Пожалуйста, убедитесь, что ELD подключён и логи "
               "в порядке, документы (регистрация, страховка, медицинская карта, инструкция ELD) "
               "находятся в траке, и каждый день проводите полный pre-trip осмотр.\n\n"
               "Если есть вопросы, сообщите нам.\n\nСпасибо."),
        "uz": ("Salom.\nHurmatli {name}\n\nShu hafta inspeksiya haftasi (CVSA International "
               "Roadcheck). Iltimos, ELD ulanganiga va loglaringiz tartibda ekaniga, hujjatlar "
               "(registratsiya, sug'urta, tibbiy karta, ELD yo'riqnomasi) trakda ekaniga ishonch "
               "hosil qiling va har kuni to'liq pre-trip ko'rigini o'tkazing.\n\n"
               "Savollaringiz bo'lsa, bizga xabar bering.\n\nRahmat."),
        "es": ("Hola.\nEstimado {name}\n\nEsta semana es la semana de inspecciones (CVSA "
               "International Roadcheck). Por favor, asegúrese de que su ELD esté conectado y "
               "sus registros al día, que sus documentos (registro, seguro, tarjeta médica, "
               "instrucciones del ELD) estén en el camión, y haga una inspección pre-viaje "
               "completa cada día.\n\nSi tiene alguna pregunta, avísenos.\n\nGracias."),
    }),
    ("Check your ELD connection", {
        "en": ("Hello.\nDear {name}\n\nPlease check that your ELD device is connected and "
               "your logs are recording correctly. If you see any problem, let us know right "
               "away.\n\nThank you."),
        "ru": ("Здравствуйте.\nУважаемый {name}\n\nПожалуйста, проверьте, что ваше устройство "
               "ELD подключено и логи записываются правильно. Если видите проблему, сразу "
               "сообщите нам.\n\nСпасибо."),
        "uz": ("Salom.\nHurmatli {name}\n\nIltimos, ELD qurilmangiz ulanganini va loglar "
               "to'g'ri yozilayotganini tekshiring. Muammo ko'rsangiz, darhol bizga xabar "
               "bering.\n\nRahmat."),
        "es": ("Hola.\nEstimado {name}\n\nPor favor, verifique que su dispositivo ELD esté "
               "conectado y que sus registros se graben correctamente. Si ve algún problema, "
               "avísenos de inmediato.\n\nGracias."),
    }),
]
