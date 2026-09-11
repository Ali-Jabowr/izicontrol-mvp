"""Детерминированный резолвер русских относительных дат.

Зачем он нужен, если модель и так возвращает дату: арифметика по календарю —
самый частый класс ошибок LLM, и ошибка здесь самая дорогая (сорванная встреча).
Поэтому дата, предложенная моделью, пересчитывается независимо, и расхождение
становится видимым флагом, а не тихо уезжает в отчёт.

Резолвер сознательно неполный: он покрывает конструкции, которые реально
встречаются в деловых звонках, а всё остальное честно помечает как
"relative_unresolved" вместо того, чтобы угадывать.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from .ingest import RU_MONTHS, WEEKDAYS_RU

# Паттерны вместо голых подстрок: «средства» и «в среднем» не должны
# превращаться в среду, а «5 машин» — в 5 мая. Стем обязан стоять отдельным
# словом (\b) и для коротких основ задан класс окончаний.
WEEKDAY_PATTERNS = [
    ("понедельник", 0), ("вторник", 1), ("сред[ауеыо]", 2), ("четверг", 3),
    ("пятниц", 4), ("суббот", 5), ("воскресень", 6),
]

# фрагменты регулярки → номер месяца; «ма» без класса ловила «5 машин» как 5 мая
MONTH_STEMS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма[йяюе]": 5, "июн": 6,
    "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}

ORDINALS = {
    "первого": 1, "второго": 2, "третьего": 3, "четвёртого": 4, "четвертого": 4,
    "пятого": 5, "шестого": 6, "седьмого": 7, "восьмого": 8, "девятого": 9,
    "десятого": 10, "пятнадцатого": 15, "двадцатого": 20, "двадцать первого": 21,
    "двадцать пятого": 25, "тридцатого": 30,
}


@dataclass
class Resolution:
    resolved: date | None = None
    precision: str = "none"  # exact | approximate | month_only | relative_unresolved | none
    basis: str = ""
    flags: list[str] = field(default_factory=list)


def _next_weekday(anchor: date, target_idx: int, *, skip_week: bool = False) -> date:
    """Ближайший <день недели> строго после якорной даты.

    Покрывает и «в этот четверг» (вт → чт той же недели), и «к понедельнику»
    (вт → понедельник следующей недели) одним правилом.
    """
    delta = (target_idx - anchor.weekday()) % 7
    if delta == 0:
        delta = 7
    if skip_week:
        delta += 7
    return anchor + timedelta(days=delta)


def _resolve_month_year(month: int, anchor: date) -> int:
    """Месяц, который в году разговора уже прошёл, относится к следующему году."""
    return anchor.year if month >= anchor.month else anchor.year + 1


def resolve(expression: str, anchor: date) -> Resolution:
    e = (expression or "").lower().strip()
    if not e:
        return Resolution(basis="пустое выражение")

    # 1) явная дата: «10 сентября», «1 ноября 2026»
    m = re.search(r"(\d{1,2})\s+([а-яё]+)(?:\s+(\d{4}))?", e)
    if m:
        day = int(m.group(1))
        word = m.group(2)
        month = RU_MONTHS.get(word)
        if month is None:
            for stem, num in MONTH_STEMS.items():
                if re.fullmatch(rf"{stem}[а-яё]*", word):
                    month = num
                    break
        if month:
            year = int(m.group(3)) if m.group(3) else _resolve_month_year(month, anchor)
            try:
                d = date(year, month, day)
            except ValueError:
                return Resolution(precision="relative_unresolved",
                                  basis=f"некорректная дата {day}.{month}",
                                  flags=["невалидная дата"])
            flags = []
            if not m.group(3) and year != anchor.year:
                flags.append(f"год не назван — вычислен как {year} (месяц уже прошёл в {anchor.year})")
            return Resolution(d, "exact", f"явная дата в тексте: {day} {word}", flags)

    # 2) сегодня / завтра / послезавтра
    for word, offset in (("послезавтра", 2), ("сегодня", 0), ("завтра", 1)):
        if word in e:
            return Resolution(anchor + timedelta(days=offset), "exact",
                              f"«{word}» от {anchor.isoformat()}")

    # 3) «после двадцатого», «после 20 числа»
    # цифровая форма обязана нести маркер «числа»/«-го», иначе «после 11:00» (время)
    # будет ошибочно принято за день месяца
    m = re.search(r"после\s+(\d{1,2})\s*(?:числа|-го|го)\b", e)
    ordinal_day = int(m.group(1)) if m else None
    if ordinal_day is None:
        for word, num in ORDINALS.items():
            if f"после {word}" in e:
                ordinal_day = num
                break
    if ordinal_day is not None:
        month = _find_month(e)
        if month:
            year = _resolve_month_year(month, anchor)
            try:
                d = date(year, month, ordinal_day) + timedelta(days=1)
            except ValueError:
                d = None
            return Resolution(
                d, "approximate",
                f"«после {ordinal_day}-го» {month:02d}.{year} → не ранее {d.isoformat() if d else '?'}",
                ["граница «после N-го» приблизительная: точная дата не названа"],
            )
        return Resolution(None, "relative_unresolved",
                          f"«после {ordinal_day}-го», но месяц не определён",
                          ["месяц для «после N-го» не назван"])

    # 4) день недели
    skip_week = bool(re.search(r"следующ\w*\s+(?:недел|\w*)", e)) and "на следующей неделе" not in e
    for stem, idx in WEEKDAY_PATTERNS:
        if re.search(rf"\b{stem}", e):
            d = _next_weekday(anchor, idx, skip_week=skip_week)
            label = WEEKDAYS_RU[idx]
            basis = f"ближайш{'ий' if idx in (0,1,3) else 'ая'} {label} после {anchor.isoformat()}"
            flags = []
            if skip_week:
                basis += " (+неделя по «следующий»)"
                flags.append("«следующий <день>» трактуется как +1 неделя — возможна неоднозначность")
            return Resolution(d, "exact", basis, flags)

    # 5) «на следующей неделе» — недели хватает, дня нет
    if "на следующей неделе" in e or "следующей неделе" in e:
        monday = _next_weekday(anchor, 0)
        return Resolution(monday, "approximate",
                          f"начало следующей недели от {anchor.isoformat()}",
                          ["конкретный день недели не назван"])

    # 6) «два-три рабочих дня», «через неделю»
    m = re.search(r"(?:через\s+)?(\d+)[-–—]?(?:\s*(\d+))?\s+рабочи\w+\s+дн", e)
    if m:
        lo = int(m.group(1))
        hi = int(m.group(2)) if m.group(2) else lo
        d = _add_business_days(anchor, hi)
        return Resolution(d, "approximate",
                          f"{lo}-{hi} рабочих дней от {anchor.isoformat()} → не ранее {d.isoformat()}",
                          [f"интервал {lo}-{hi} дн.: взята поздняя граница"])
    if "два-три рабочих" in e or "две-три рабочих" in e:
        d = _add_business_days(anchor, 3)
        return Resolution(d, "approximate", f"2-3 рабочих дня → не ранее {d.isoformat()}",
                          ["интервал 2-3 дн.: взята поздняя граница"])
    m = re.search(r"через\s+(\d+)\s+(день|дня|дней|недел\w+)", e)
    if m:
        n = int(m.group(1))
        days = n * 7 if m.group(2).startswith("недел") else n
        return Resolution(anchor + timedelta(days=days), "approximate",
                          f"через {n} {m.group(2)} от {anchor.isoformat()}")

    # 7) только месяц: «в декабре», «во второй половине января»
    month = _find_month(e)
    if month:
        year = _resolve_month_year(month, anchor)
        half = "второй половине" in e or "конце" in e
        flags = [f"назван только месяц ({month:02d}.{year}), точная дата отсутствует"]
        if year != anchor.year:
            flags.append(f"год не назван — вычислен как {year}")
        return Resolution(
            None, "month_only",
            f"месяц {month:02d}.{year}" + (" (вторая половина)" if half else ""),
            flags,
        )

    return Resolution(None, "relative_unresolved",
                      f"выражение «{expression}» не разобрано резолвером",
                      ["выражение не распознано — проверить вручную"])


def _find_month(text: str) -> int | None:
    for stem, num in sorted(MONTH_STEMS.items(), key=lambda kv: -len(kv[0])):
        if re.search(rf"\b{stem}[а-яё]*", text):
            return num
    return None


def _add_business_days(start: date, n: int) -> date:
    d = start
    added = 0
    while added < n:
        d += timedelta(days=1)
        if d.weekday() < 5:
            added += 1
    return d
