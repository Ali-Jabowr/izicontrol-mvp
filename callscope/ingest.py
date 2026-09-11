"""Разбор шапки транскрипта. Дата и день недели считаются здесь, в Python, а не моделью.

Это первая линия защиты от галлюцинаций: якорная дата всегда точная,
а день недели пересчитывается из календаря и сверяется с тем, что написано в файле.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

RU_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}

WEEKDAYS_RU = [
    "понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье",
]


@dataclass
class Transcript:
    path: Path
    raw: str            # полный текст файла
    body: str           # текст разговора без шапки
    call_date: date
    weekday: str        # посчитанный из календаря
    weekday_stated: str | None   # как написано в файле
    company: str | None
    manager: str | None
    client_contact: str | None
    header_conflicts: list[str]

    @property
    def name(self) -> str:
        return self.path.name


def _parse_date(header: str) -> date | None:
    m = re.search(r"(\d{1,2})\s+([а-яё]+)\s+(\d{4})", header, re.IGNORECASE)
    if not m:
        return None
    day, month_word, year = int(m.group(1)), m.group(2).lower(), int(m.group(3))
    month = RU_MONTHS.get(month_word)
    if not month:
        return None
    return date(year, month, day)


def _parse_participants(header: str) -> tuple[str | None, str | None, str | None]:
    """«Анна — менеджер интегратора; Сергей — финансовый директор клиента «Альфа-Металл»»"""
    manager = client = company = None

    m = re.search(r"Участники:\s*(.+)", header, re.IGNORECASE | re.DOTALL)
    if not m:
        return manager, client, company

    for part in m.group(1).split(";"):
        part = part.strip()
        nm = re.match(r"([А-ЯЁ][а-яё]+)\s*[—–-]\s*(.+)", part)
        if not nm:
            continue
        person, role = nm.group(1), nm.group(2)
        if "менеджер интегратора" in role.lower():
            manager = person
        else:
            client = person
            cm = re.search(r"[«\"]([^»\"]+)[»\"]", role)
            if cm:
                company = cm.group(1)
    return manager, client, company


def load(path: Path) -> Transcript:
    raw = path.read_text(encoding="utf-8")

    # шапка — всё до горизонтальной линии ---
    split = re.split(r"\n\s*---\s*\n", raw, maxsplit=1)
    header = split[0]
    body = split[1].strip() if len(split) > 1 else raw

    call_date = _parse_date(header)
    if call_date is None:
        raise ValueError(f"{path.name}: не удалось разобрать дату разговора из шапки")

    weekday = WEEKDAYS_RU[call_date.weekday()]

    stated = None
    conflicts: list[str] = []
    for w in WEEKDAYS_RU:
        if w in header.lower():
            stated = w
            break
    if stated and stated != weekday:
        conflicts.append(
            f"В шапке указан «{stated}», но {call_date.isoformat()} — это {weekday}. "
            f"Все относительные даты считаются от календаря."
        )

    manager, client, company = _parse_participants(header)

    return Transcript(
        path=path,
        raw=raw,
        body=body,
        call_date=call_date,
        weekday=weekday,
        weekday_stated=stated,
        company=company,
        manager=manager,
        client_contact=client,
        header_conflicts=conflicts,
    )


def load_dir(d: Path) -> list[Transcript]:
    """Один файл с битой шапкой не должен ронять весь прогон — остальные
    доходят до отчёта, а виновник виден в сообщении."""
    out: list[Transcript] = []
    for p in sorted(d.glob("*.md")):
        try:
            out.append(load(p))
        except ValueError as err:
            print(f"  ! пропуск файла: {err}")
    return out
