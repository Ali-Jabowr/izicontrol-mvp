"""Детерминированные проверки поверх выдачи LLM.

Здесь нет ни одного обращения к модели. Идея простая: модель хороша в извлечении
смысла и плоха в арифметике и самоконтроле, поэтому всё, что можно проверить
механически, проверяется механически, а расхождения становятся видимыми флагами.

Пять проверок:
  1. grounding    — цитата обязана дословно встречаться в транскрипте
  2. dates        — независимый пересчёт даты из выражения
  3. weekday      — «пятница» обязана попасть на пятницу
  4. past_date    — действие в прошлом относительно звонка = подозрительно
  5. completeness — пустые обязательные поля превращаются в честные data_gaps
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from .dates import WEEKDAY_PATTERNS, resolve
from .ingest import RU_MONTHS, WEEKDAYS_RU, Transcript, load
from .models import CallAnalysis, DateRef

# 1 → «января», для достройки выражений без месяца
MONTH_NAME = {num: word for word, num in RU_MONTHS.items()}


def normalize(s: str) -> str:
    """Приводим к виду, в котором сравнение цитат устойчиво к типографике.

    Модель может вернуть «ё» как «е», прямые кавычки вместо ёлочек или другой
    тип тире — это не галлюцинация, а нормализация, и валить за это нельзя.
    Но состав слов обязан совпадать.
    """
    s = unicodedata.normalize("NFKC", s).lower()
    s = s.replace("ё", "е")
    s = re.sub(r"[«»\"“”„‟'’‘]", "", s)
    s = re.sub(r"[—–−-]", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip(" .,;:!?")


# ---------------------------------------------------------------- 1. grounding

def check_grounding(analysis: CallAnalysis, transcript: Transcript) -> tuple[int, int]:
    """Помечает каждую цитату verified=True/False. Возвращает (подтверждено, всего)."""
    haystack = normalize(transcript.body)
    total = verified = 0

    def verify_list(items, label: str) -> None:
        nonlocal total, verified
        for item in items:
            ok_any = False
            for ev in item.evidence:
                total += 1
                needle = normalize(ev.quote)
                ev.verified = bool(needle) and needle in haystack
                if ev.verified:
                    verified += 1
                    ok_any = True
            if item.evidence and not ok_any:
                item.flags.append(
                    "НЕ ПОДТВЕРЖДЕНО: ни одна цитата не найдена в транскрипте"
                )
            elif not item.evidence:
                item.flags.append("без цитаты — утверждение не проверяемо")

    verify_list(analysis.client_needs, "client_needs")
    verify_list(analysis.risks, "risks")
    verify_list(analysis.manager_mistakes, "manager_mistakes")
    verify_list(analysis.needs_supervisor_attention, "needs_supervisor_attention")
    verify_list(analysis.next_steps, "next_steps")

    for c in analysis.constraints:
        for ev in c.evidence:
            total += 1
            ev.verified = normalize(ev.quote) in haystack
            verified += int(bool(ev.verified))

    for dr in _all_date_refs(analysis):
        for ev in dr.evidence:
            total += 1
            ev.verified = normalize(ev.quote) in haystack
            verified += int(bool(ev.verified))

    return verified, total


def _all_date_refs(analysis: CallAnalysis) -> list[DateRef]:
    refs = list(analysis.all_dates)
    if analysis.next_action_date:
        refs.append(analysis.next_action_date)
    refs.extend(s.date for s in analysis.next_steps if s.date)
    return refs


# ------------------------------------------------------- 2-4. проверки по датам

def check_dates(analysis: CallAnalysis, transcript: Transcript) -> None:
    anchor = transcript.call_date
    for dr in _all_date_refs(analysis):
        res = resolve(dr.raw_expression, anchor)

        # «после двадцатого» без месяца резолвер не берёт. Достраиваем месяцем
        # из предложения модели: месяцу доверяем, арифметику дня всё равно
        # считаем сами — иначе пришлось бы принять дату модели целиком.
        if res.resolved is None and res.precision == "relative_unresolved" and dr.resolved_by_llm:
            month_word = MONTH_NAME.get(dr.resolved_by_llm.month)
            if month_word:
                retry = resolve(f"{dr.raw_expression} {month_word}", anchor)
                if retry.resolved is not None:
                    retry.basis += f" (месяц «{month_word}» взят из ответа модели)"
                    res = retry

        # резолвер — источник истины там, где он уверен
        if res.resolved is not None:
            if dr.resolved_by_llm and dr.resolved_by_llm != res.resolved:
                dr.flags.append(
                    f"РАСХОЖДЕНИЕ: модель предложила {dr.resolved_by_llm.isoformat()}, "
                    f"резолвер посчитал {res.resolved.isoformat()} ({res.basis}). "
                    f"Взято значение резолвера."
                )
            dr.resolved = res.resolved
            dr.basis = res.basis or dr.basis
        elif res.precision == "month_only":
            dr.resolved = None
            dr.precision = "month_only"
            dr.basis = res.basis
            if dr.resolved_by_llm:
                dr.flags.append(
                    f"Модель подставила точную дату {dr.resolved_by_llm.isoformat()}, "
                    f"хотя в тексте назван только месяц. Точная дата убрана."
                )
        else:
            # резолвер не разобрал — оставляем предложение модели, но помечаем
            dr.resolved = dr.resolved_by_llm
            if dr.resolved_by_llm:
                dr.flags.append(
                    "Дата не подтверждена независимым резолвером — проверить вручную"
                )
            dr.basis = dr.basis or res.basis

        if res.precision != "none" and dr.precision in ("", "none"):
            dr.precision = res.precision
        dr.flags.extend(res.flags)

        # 3. день недели обязан сходиться
        expr = normalize(dr.raw_expression)
        for stem, idx in WEEKDAY_PATTERNS:
            if re.search(rf"\b{stem}", expr) and dr.resolved is not None:
                actual = dr.resolved.weekday()
                if actual != idx:
                    dr.flags.append(
                        f"КОНФЛИКТ: в тексте «{WEEKDAYS_RU[idx]}», "
                        f"а {dr.resolved.isoformat()} — {WEEKDAYS_RU[actual]}"
                    )
                break

        # 4. действие в прошлом
        if dr.resolved is not None and dr.resolved < anchor:
            dr.flags.append(
                f"Дата {dr.resolved.isoformat()} раньше дня разговора "
                f"({anchor.isoformat()}) — вероятна ошибка года"
            )


# --------------------------------------------------------- 5. полнота

REQUIRED = {
    "next_steps": "следующий согласованный шаг",
    "client_needs": "потребности клиента",
    "risks": "основные риски",
    "needs_supervisor_attention": "что требует внимания руководителя",
}


def check_completeness(analysis: CallAnalysis, transcript: Transcript) -> None:
    for field, label in REQUIRED.items():
        if not getattr(analysis, field):
            analysis.data_gaps.append(
                f"«{label}»: в разговоре нет данных для этого поля"
            )

    agreed = [s for s in analysis.next_steps if s.agreed]
    if analysis.next_steps and not agreed:
        analysis.validation_flags.append(
            "Ни один следующий шаг не согласован обеими сторонами — "
            "звонок завершился без договорённости"
        )

    if analysis.next_action_date is None or analysis.next_action_date.resolved is None:
        dated = [s for s in agreed if s.date and s.date.resolved]
        if dated:
            nearest = min(dated, key=lambda s: s.date.resolved)  # type: ignore[union-attr,arg-type]
            analysis.next_action_date = nearest.date
            analysis.validation_flags.append(
                "next_action_date восстановлена как ближайшая дата из согласованных шагов"
            )
        else:
            analysis.data_gaps.append(
                "«дата следующего действия»: конкретная дата в разговоре не названа"
            )

    if not analysis.manager_mistakes:
        analysis.validation_flags.append(
            "Ошибок менеджера не зафиксировано — проверить вручную, "
            "пустой список может означать как чистую работу, так и пропуск модели"
        )

    analysis.validation_flags.extend(transcript.header_conflicts)


# --------------------------------------------------------------- точка входа

def validate(analysis: CallAnalysis, transcript: Transcript) -> CallAnalysis:
    verified, total = check_grounding(analysis, transcript)
    analysis.grounding_score = round(verified / total, 3) if total else None
    if total and verified < total:
        analysis.validation_flags.append(
            f"Цитаты: подтверждено {verified} из {total}. "
            f"Неподтверждённые помечены в карточках."
        )
    check_dates(analysis, transcript)
    check_completeness(analysis, transcript)
    return analysis


def revalidate(analysis: CallAnalysis, transcript_path: Path) -> CallAnalysis:
    """Повторный прогон валидаторов по сохранённому JSON (команда report).

    Негативный тест из README: правите цитату в analysis.json — report обязан
    перечитать её по транскрипту и пометить, а не отрендерить как факт.
    Сбрасываем только то, чем владеют валидаторы (verified, флаги, score):
    data_gaps не трогаем — их наполняет и модель, — но дедуплицируем, потому
    что check_completeness добавит свои причины повторно.
    """
    t = load(transcript_path)

    def reset_items(items: list) -> None:
        for item in items:
            item.flags = []
            for ev in item.evidence:
                ev.verified = None

    for group in (analysis.client_needs, analysis.risks, analysis.manager_mistakes,
                  analysis.needs_supervisor_attention, analysis.next_steps):
        reset_items(group)
    for c in analysis.constraints:
        for ev in c.evidence:
            ev.verified = None
    for dr in _all_date_refs(analysis):
        dr.flags = []
        for ev in dr.evidence:
            ev.verified = None
    analysis.validation_flags = []
    analysis.grounding_score = None

    validate(analysis, t)
    analysis.data_gaps = list(dict.fromkeys(analysis.data_gaps))
    return analysis
