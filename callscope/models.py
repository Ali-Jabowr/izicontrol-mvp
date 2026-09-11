"""Схема результата. Всё, что модель утверждает, обязано нести цитату-доказательство."""

from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class Confidence(str, Enum):
    HIGH = "high"       # сказано прямым текстом
    MEDIUM = "medium"   # выводится из контекста без домыслов
    LOW = "low"         # догадка — в отчёт попадает только с пометкой


class Evidence(BaseModel):
    """Дословная цитата из транскрипта. verified проставляет валидатор, не модель."""

    quote: str
    speaker: str | None = None
    verified: bool | None = None  # None = ещё не проверено


class Item(BaseModel):
    """Один пункт списка (потребность, риск, ошибка, сигнал руководителю)."""

    text: str
    confidence: Confidence = Confidence.MEDIUM
    evidence: list[Evidence] = Field(default_factory=list)
    # заполняется валидатором
    flags: list[str] = Field(default_factory=list)


DatePrecision = Literal["exact", "approximate", "month_only", "relative_unresolved", "none"]


class DateRef(BaseModel):
    """Дата никогда не берётся у модели «как есть» — только выражение + предложение.

    resolved_by_llm  — что предложила модель
    resolved         — что посчитал детерминированный резолвер (он главный)
    """

    raw_expression: str                       # «к понедельнику», «после двадцатого»
    resolved_by_llm: date | None = None
    resolved: date | None = None              # финальное значение, ставит dates.py
    time: str | None = None                   # "15:00"
    timezone: str | None = None               # "Екатеринбург (UTC+5)"
    precision: DatePrecision = "none"
    basis: str = ""                           # как получено, для аудита
    confidence: Confidence = Confidence.MEDIUM
    evidence: list[Evidence] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)


class NextStep(BaseModel):
    """Следующий согласованный шаг. Именно СОГЛАСОВАННЫЙ — отклонённые предложения сюда не идут."""

    action: str
    owner: Literal["менеджер", "клиент", "обе стороны", "неизвестно"] = "неизвестно"
    agreed: bool = True               # False => предложение прозвучало, но согласия не было
    date: DateRef | None = None
    confidence: Confidence = Confidence.MEDIUM
    evidence: list[Evidence] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)


class Constraint(BaseModel):
    """Ограничение (недоступен в пятницу, не звонить в магазины) — НЕ действие."""

    text: str
    evidence: list[Evidence] = Field(default_factory=list)


Outcome = Literal[
    "продвижение",        # договорились о конкретном следующем шаге
    "перенос",            # решение отложено на потом
    "отказ",              # клиент закрыл вопрос
    "требует_уточнения",  # из разговора итог не определяется
]


class CallAnalysis(BaseModel):
    """Итоговый разбор одного звонка — 7 полей из ТЗ + служебное."""

    # --- метаданные (детерминированно из шапки, не от модели) ---
    source_file: str
    call_date: date
    call_weekday: str
    company: str | None = None
    manager: str | None = None
    client_contact: str | None = None

    # --- 7 обязательных полей ТЗ ---
    summary: str                                              # итог разговора
    outcome: Outcome = "требует_уточнения"
    next_steps: list[NextStep] = Field(default_factory=list)  # следующий согласованный шаг
    next_action_date: DateRef | None = None                   # ближайшая дата действия
    client_needs: list[Item] = Field(default_factory=list)    # потребности
    risks: list[Item] = Field(default_factory=list)           # риски
    manager_mistakes: list[Item] = Field(default_factory=list)  # ошибки менеджера
    needs_supervisor_attention: list[Item] = Field(default_factory=list)  # внимание руководителя

    # --- дополнительное, но полезное руководителю ---
    constraints: list[Constraint] = Field(default_factory=list)
    decision_makers: list[str] = Field(default_factory=list)
    all_dates: list[DateRef] = Field(default_factory=list)

    # --- служебное, заполняется валидаторами ---
    validation_flags: list[str] = Field(default_factory=list)
    data_gaps: list[str] = Field(default_factory=list)  # «недостаточно данных» с причиной
    grounding_score: float | None = None                # доля подтверждённых цитат
