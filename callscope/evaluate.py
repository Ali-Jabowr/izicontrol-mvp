"""Сверка результата с ручным эталоном gold/gold.yaml.

Оценивать дословные формулировки LLM бессмысленно — они каждый раз разные.
Поэтому проверяются проверяемые вещи: даты, год, точность, попадание
отклонённого предложения в согласованные шаги и покрытие ключевых фактов.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import yaml

from .models import CallAnalysis
from .validate import normalize

GREEN, RED, DIM, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"


class Checks:
    def __init__(self) -> None:
        self.rows: list[tuple[bool, str, str]] = []

    def add(self, ok: bool, name: str, detail: str = "") -> None:
        self.rows.append((ok, name, detail))

    @property
    def passed(self) -> int:
        return sum(1 for ok, _, _ in self.rows if ok)

    def __len__(self) -> int:
        return len(self.rows)


def _covered(groups: list[list[str]], haystack: str, checks: Checks, label: str) -> None:
    for group in groups:
        hit = next((k for k in group if normalize(k) in haystack), None)
        checks.add(bool(hit), f"{label}: {'/'.join(group[:2])}",
                   f"найдено «{hit}»" if hit else "не упомянуто")


def _all_resolved(a: CallAnalysis) -> list[date]:
    out = []
    for dr in a.all_dates + ([a.next_action_date] if a.next_action_date else []):
        if dr.resolved:
            out.append(dr.resolved)
    for s in a.next_steps:
        if s.date and s.date.resolved:
            out.append(s.date.resolved)
    return out


def evaluate_one(a: CallAnalysis, gold: dict) -> Checks:
    c = Checks()

    if "outcome" in gold:
        c.add(a.outcome == gold["outcome"], "итог (статус)",
              f"получено «{a.outcome}», ожидалось «{gold['outcome']}»")

    nad = a.next_action_date
    if "next_action_date" in gold:
        got = nad.resolved.isoformat() if nad and nad.resolved else None
        c.add(got == gold["next_action_date"], "дата следующего действия",
              f"получено {got}, ожидалось {gold['next_action_date']}")

    if "next_action_time" in gold:
        got = nad.time if nad else None
        c.add(got == gold["next_action_time"], "время следующего действия",
              f"получено {got}, ожидалось {gold['next_action_time']}")

    if "next_action_year" in gold:
        got = nad.resolved.year if nad and nad.resolved else None
        c.add(got == gold["next_action_year"], "год следующего действия",
              f"получено {got}, ожидалось {gold['next_action_year']}")

    if "timezone_contains" in gold:
        tz = normalize((nad.timezone or "") if nad else "")
        c.add(normalize(gold["timezone_contains"]) in tz, "часовой пояс сохранён",
              f"получено «{nad.timezone if nad else None}»")

    resolved = {d.isoformat() for d in _all_resolved(a)}
    for want in gold.get("dates_present", []):
        c.add(want in resolved, f"дата {want} распознана",
              f"все даты: {sorted(resolved) or '—'}")

    if gold.get("no_date_before_call"):
        bad = [d.isoformat() for d in _all_resolved(a) if d < a.call_date]
        c.add(not bad, "нет дат раньше дня звонка", f"нарушения: {bad}")

    if gold.get("has_month_only_date"):
        has = any(dr.precision == "month_only"
                  for dr in a.all_dates + ([nad] if nad else []))
        c.add(has, "неоднозначная дата помечена (только месяц)",
              "ни одна дата не помечена month_only")

    if "grounding_min" in gold:
        gs = a.grounding_score or 0.0
        c.add(gs >= gold["grounding_min"], "подтверждаемость цитат",
              f"{gs:.0%} при пороге {gold['grounding_min']:.0%}")

    agreed_text = normalize(" ".join(
        s.action + " " + (s.date.raw_expression if s.date else "")
        for s in a.next_steps if s.agreed
    ))
    for group in gold.get("not_in_agreed_steps", []):
        hit = next((k for k in group if normalize(k) in agreed_text), None)
        c.add(hit is None, f"отклонённое не в шагах: {'/'.join(group[:2])}",
              f"ошибочно попало «{hit}»" if hit else "чисто")

    _covered(gold.get("mistakes_cover", []),
             normalize(" ".join(i.text for i in a.manager_mistakes)), c, "ошибка")
    _covered(gold.get("risks_cover", []),
             normalize(" ".join(i.text for i in a.risks)), c, "риск")
    _covered(gold.get("needs_cover", []),
             normalize(" ".join(i.text for i in a.client_needs)), c, "потребность")
    _covered(gold.get("attention_cover", []),
             normalize(" ".join(i.text for i in a.needs_supervisor_attention)), c, "руководителю")
    _covered(gold.get("constraints_cover", []),
             normalize(" ".join(i.text for i in a.constraints)), c, "ограничение")
    _covered(gold.get("decision_makers_cover", []),
             normalize(" ".join(a.decision_makers)), c, "ЛПР")

    return c


def run_eval(source: Path, gold_path: Path) -> int:
    results = [CallAnalysis.model_validate(d)
               for d in json.loads(source.read_text(encoding="utf-8"))]
    gold = yaml.safe_load(gold_path.read_text(encoding="utf-8"))

    total = passed = 0
    print(f"\nЭталон: {gold_path}   Результат: {source}\n")

    for a in results:
        g = gold.get(a.source_file)
        if not g:
            print(f"{a.source_file}: эталона нет, пропуск")
            continue
        c = evaluate_one(a, g)
        total += len(c)
        passed += c.passed
        pct = c.passed / len(c) if len(c) else 0
        color = GREEN if pct == 1 else RED
        print(f"{color}{a.source_file}  {c.passed}/{len(c)}{RESET}  ({a.company})")
        for ok, name, detail in c.rows:
            mark = f"{GREEN}✓{RESET}" if ok else f"{RED}✗{RESET}"
            note = "" if ok else f"  {DIM}{detail}{RESET}"
            print(f"   {mark} {name}{note}")
        print()

    pct = passed / total if total else 0
    color = GREEN if pct >= 0.9 else RED
    print(f"{color}ИТОГО: {passed}/{total} проверок пройдено ({pct:.0%}){RESET}\n")
    return 0 if pct >= 0.9 else 1
