"""Самопроверка без обращения к LLM: резолвер дат + защита от галлюцинаций.

Запуск: python -m callscope selftest
"""

from __future__ import annotations

from datetime import date

from .dates import resolve
from .ingest import Transcript
from .models import CallAnalysis, Evidence, Item
from .validate import check_grounding, normalize

GREEN, RED, DIM, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"

TUE = date(2026, 9, 8)   # транскрипты 1 и 2
MON = date(2026, 9, 7)   # транскрипт 3
SUN = date(2026, 9, 6)   # транскрипт 4

# (выражение, якорь, ожидаемая дата, ожидаемая точность)
DATE_CASES = [
    ("до среды",                      TUE, "2026-09-09", "exact"),
    ("в этот четверг",                TUE, "2026-09-10", "exact"),
    ("10 сентября в 15:00",           TUE, "2026-09-10", "exact"),
    ("завтра до конца дня",           TUE, "2026-09-09", "exact"),
    ("к понедельнику",                TUE, "2026-09-14", "exact"),
    ("в пятницу после четырёх",       TUE, "2026-09-11", "exact"),
    ("в пятницу после 11:00",         MON, "2026-09-11", "exact"),
    ("два-три рабочих дня",           MON, "2026-09-10", "approximate"),
    ("после двадцатого числа января", SUN, "2027-01-21", "approximate"),
    ("в декабре",                     SUN, None,         "month_only"),
    ("во второй половине января",     SUN, None,         "month_only"),
    ("1 ноября",                      TUE, "2026-11-01", "exact"),
    # регрессии: слово, начинающееся с «ма», не мая; «средства» не среда
    ("5 машин",                       TUE, None,         "relative_unresolved"),
    ("максимум до конца недели",      TUE, None,         "relative_unresolved"),
    ("в пятницу, средств не жду",     TUE, "2026-09-11", "exact"),
]

TRANSCRIPT_SNIPPET = (
    "Сергей: Нет, коммерческое сейчас рано. Мне сначала нужна техническая схема "
    "и понимание, что будет с нашей доработанной 1С."
)


def _fake_transcript() -> Transcript:
    return Transcript(
        path=type("P", (), {"name": "test.md"})(),  # type: ignore[arg-type]
        raw=TRANSCRIPT_SNIPPET, body=TRANSCRIPT_SNIPPET,
        call_date=TUE, weekday="вторник", weekday_stated="вторник",
        company="Тест", manager="Анна", client_contact="Сергей",
        header_conflicts=[],
    )


def _blank_analysis() -> CallAnalysis:
    return CallAnalysis(
        source_file="test.md", call_date=TUE, call_weekday="вторник", summary="—"
    )


def _revalidation_case() -> bool:
    """report обязан перечитывать цитаты по транскрипту, а не верить JSON."""
    import tempfile
    from pathlib import Path

    from .ingest import load
    from .validate import revalidate

    md = (
        "Дата разговора: 8 сентября 2026 года, вторник\n"
        "\n---\n\n" + TRANSCRIPT_SNIPPET
    )
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "test.md"
        p.write_text(md, encoding="utf-8")
        t = load(p)
        a = CallAnalysis(
            source_file="test.md", call_date=t.call_date,
            call_weekday=t.weekday, summary="—",
        )
        a.risks = [Item(
            text="Выдуманный риск",
            evidence=[Evidence(quote="цитата, которой нет в тексте", speaker="Сергей")],
        )]
        a.risks[0].evidence[0].verified = True  # как после реального прогона
        revalidate(a, p)
        item = a.risks[0]
        return (item.evidence[0].verified is False
                and any(f.startswith("НЕ ПОДТВЕРЖДЕНО") for f in item.flags))


def run_selftest() -> int:
    failures = 0

    print(f"\n{DIM}1. Резолвер дат — выражения из реальных транскриптов{RESET}\n")
    for expr, anchor, want_date, want_prec in DATE_CASES:
        r = resolve(expr, anchor)
        got = r.resolved.isoformat() if r.resolved else None
        ok = got == want_date and r.precision == want_prec
        failures += not ok
        mark = f"{GREEN}✓{RESET}" if ok else f"{RED}✗{RESET}"
        print(f"  {mark} {expr:32} → {str(got):12} [{r.precision}]"
              + ("" if ok else f"  {RED}ожидалось {want_date} [{want_prec}]{RESET}"))

    print(f"\n{DIM}2. Защита от галлюцинаций — подложная цитата обязана быть отброшена{RESET}\n")
    t = _fake_transcript()

    real = _blank_analysis()
    real.manager_mistakes = [Item(
        text="Предложила КП, когда клиент просил техническую схему",
        evidence=[Evidence(quote="коммерческое сейчас рано", speaker="Сергей")],
    )]
    check_grounding(real, t)
    ok_real = real.manager_mistakes[0].evidence[0].verified is True
    failures += not ok_real
    print(f"  {GREEN + '✓' + RESET if ok_real else RED + '✗' + RESET} "
          f"настоящая цитата подтверждена (verified=True), флагов: "
          f"{len(real.manager_mistakes[0].flags)}")

    fake = _blank_analysis()
    fake.manager_mistakes = [Item(
        text="Менеджер пообещал скидку 30% и бесплатное внедрение",
        evidence=[Evidence(quote="дам скидку тридцать процентов", speaker="Анна")],
    )]
    check_grounding(fake, t)
    item = fake.manager_mistakes[0]
    ok_fake = (item.evidence[0].verified is False
               and any(f.startswith("НЕ ПОДТВЕРЖДЕНО") for f in item.flags))
    failures += not ok_fake
    print(f"  {GREEN + '✓' + RESET if ok_fake else RED + '✗' + RESET} "
          f"выдуманная цитата отбита: verified={item.evidence[0].verified}, "
          f"флаг={item.flags[0] if item.flags else '—'}")

    norm = _blank_analysis()
    norm.manager_mistakes = [Item(
        text="Проверка нормализации типографики",
        evidence=[Evidence(quote="Нет, коммерческое сейчас рано", speaker="Сергей")],
    )]
    check_grounding(norm, t)
    ok_norm = norm.manager_mistakes[0].evidence[0].verified is True
    failures += not ok_norm
    print(f"  {GREEN + '✓' + RESET if ok_norm else RED + '✗' + RESET} "
          f"цитата с другой типографикой (ё/е, кавычки) всё ещё засчитана")

    total = len(DATE_CASES) + 3
    print(f"\n{DIM}3. Повторная валидация (report) — правленая цитата ловится заново{RESET}\n")
    ok_reval = _revalidation_case()
    failures += not ok_reval
    print(f"  {GREEN + '✓' + RESET if ok_reval else RED + '✗' + RESET} "
          f"после revalidate подложная цитата помечена «НЕ ПОДТВЕРЖДЕНО»")
    total += 1

    color = GREEN if failures == 0 else RED
    print(f"\n{color}ИТОГО: {total - failures}/{total} проверок пройдено{RESET}\n")
    return 1 if failures else 0
