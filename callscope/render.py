"""Рендер отчёта: один самодостаточный HTML-файл + CSV.

Без сервера и без внешних ресурсов — отчёт открывается двойным кликом
и одинаково работает на машине проверяющего.

Главное в вёрстке: неподтверждённое видно сразу. Флаг, неподтверждённая
цитата и «недостаточно данных» имеют собственный цвет, чтобы руководитель
не принимал догадку модели за факт.
"""

from __future__ import annotations

import csv
import html
import io
from datetime import date

from .models import CallAnalysis, DateRef, Item, NextStep

CONF_LABEL = {"high": "высокая", "medium": "средняя", "low": "низкая"}
OUTCOME_LABEL = {
    "продвижение": ("Продвижение", "ok"),
    "перенос": ("Перенос", "warn"),
    "отказ": ("Отказ", "bad"),
    "требует_уточнения": ("Требует уточнения", "warn"),
}
PRECISION_LABEL = {
    "exact": "точная",
    "approximate": "приблизительная",
    "month_only": "только месяц",
    "relative_unresolved": "не разобрана",
    "none": "нет",
}

RU_MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
                 "августа", "сентября", "октября", "ноября", "декабря"]


def e(s: object) -> str:
    return html.escape(str(s if s is not None else ""))


def fmt_date(d: date | None) -> str:
    return f"{d.day} {RU_MONTHS_GEN[d.month - 1]} {d.year}" if d else "—"


def _evidence_html(item: Item | NextStep | DateRef) -> str:
    if not item.evidence:
        return ""
    parts = []
    for ev in item.evidence:
        cls = "ev-ok" if ev.verified else ("ev-bad" if ev.verified is False else "ev-unk")
        mark = "✓" if ev.verified else ("✗" if ev.verified is False else "?")
        who = f"<b>{e(ev.speaker)}:</b> " if ev.speaker else ""
        parts.append(f'<li class="{cls}"><span class="mk">{mark}</span>{who}«{e(ev.quote)}»</li>')
    return f'<ul class="ev">{"".join(parts)}</ul>'


def _flags_html(flags: list[str]) -> str:
    if not flags:
        return ""
    return "".join(f'<div class="flag">⚑ {e(f)}</div>' for f in flags)


def _items_html(items: list[Item]) -> str:
    if not items:
        return '<p class="empty">нет данных в разговоре</p>'
    rows = []
    for it in items:
        bad = any(f.startswith("НЕ ПОДТВЕРЖДЕНО") for f in it.flags)
        rows.append(
            f'<li class="item{" unverified" if bad else ""}">'
            f'<div class="txt">{e(it.text)}'
            f'<span class="conf c-{it.confidence.value}">{CONF_LABEL[it.confidence.value]}</span></div>'
            f"{_flags_html(it.flags)}{_evidence_html(it)}</li>"
        )
    return f'<ul class="items">{"".join(rows)}</ul>'


def _date_html(d: DateRef | None) -> str:
    if d is None:
        return '<p class="empty">дата не названа</p>'
    val = fmt_date(d.resolved)
    if d.time:
        val += f", {e(d.time)}"
    if d.timezone:
        val += f" <span class='tz'>({e(d.timezone)})</span>"
    return (
        f'<div class="dateblock">'
        f'<div class="dateval">{val}</div>'
        f'<div class="datemeta">из фразы «{e(d.raw_expression)}» · '
        f'точность: {PRECISION_LABEL.get(d.precision, d.precision)}</div>'
        f'<div class="datebasis">{e(d.basis)}</div>'
        f"{_flags_html(d.flags)}{_evidence_html(d)}</div>"
    )


def _steps_html(steps: list[NextStep]) -> str:
    if not steps:
        return '<p class="empty">согласованных шагов нет</p>'
    rows = []
    for s in steps:
        badge = (
            '<span class="agreed yes">согласовано</span>' if s.agreed
            else '<span class="agreed no">НЕ согласовано — клиент не подтвердил</span>'
        )
        d = ""
        if s.date and s.date.resolved:
            d = f'<span class="stepdate">{fmt_date(s.date.resolved)}'
            if s.date.time:
                d += f", {e(s.date.time)}"
            d += "</span>"
        elif s.date:
            d = f'<span class="stepdate none">«{e(s.date.raw_expression)}» — без точной даты</span>'
        rows.append(
            f'<li class="item{"" if s.agreed else " rejected"}">'
            f'<div class="txt">{e(s.action)}{badge}{d}'
            f'<span class="owner">{e(s.owner)}</span></div>'
            f"{_flags_html(s.flags)}{_flags_html(s.date.flags if s.date else [])}"
            f"{_evidence_html(s)}</li>"
        )
    return f'<ul class="items">{"".join(rows)}</ul>'


def _card(a: CallAnalysis) -> str:
    label, cls = OUTCOME_LABEL.get(a.outcome, (a.outcome, "warn"))
    score = f"{a.grounding_score:.0%}" if a.grounding_score is not None else "—"
    score_cls = "ok" if (a.grounding_score or 0) >= 0.9 else "warn"

    gaps = ""
    if a.data_gaps:
        gaps = (
            '<section class="gaps"><h3>Недостаточно данных</h3><ul>'
            + "".join(f"<li>{e(g)}</li>" for g in a.data_gaps)
            + "</ul></section>"
        )
    vflags = ""
    if a.validation_flags:
        vflags = (
            '<section class="vflags"><h3>Флаги валидации</h3>'
            + _flags_html(a.validation_flags)
            + "</section>"
        )
    constraints = ""
    if a.constraints:
        constraints = (
            '<section><h3>Ограничения клиента <small>(не действия)</small></h3><ul class="items">'
            + "".join(
                f'<li class="item"><div class="txt">{e(c.text)}</div>{_evidence_html(c)}</li>'
                for c in a.constraints
            )
            + "</ul></section>"
        )
    dm = ""
    if a.decision_makers:
        dm = f'<div class="dm">Решение принимают: <b>{e(", ".join(a.decision_makers))}</b></div>'

    return f"""
<article class="call">
  <header>
    <div class="head-main">
      <h2>{e(a.company or a.source_file)}</h2>
      <span class="badge {cls}">{label}</span>
    </div>
    <div class="meta">
      {fmt_date(a.call_date)}, {e(a.call_weekday)} ·
      менеджер <b>{e(a.manager)}</b> · клиент <b>{e(a.client_contact)}</b> ·
      <span class="score {score_cls}">цитаты подтверждены: {score}</span>
    </div>
  </header>

  <section><h3>Итог разговора</h3><p class="summary">{e(a.summary)}</p>{dm}</section>
  <section><h3>Дата следующего действия</h3>{_date_html(a.next_action_date)}</section>
  <section><h3>Следующие шаги</h3>{_steps_html(a.next_steps)}</section>
  <section><h3>Потребности клиента</h3>{_items_html(a.client_needs)}</section>
  <section><h3>Риски</h3>{_items_html(a.risks)}</section>
  <section><h3>Ошибки менеджера</h3>{_items_html(a.manager_mistakes)}</section>
  <section><h3>Требует внимания руководителя</h3>{_items_html(a.needs_supervisor_attention)}</section>
  {constraints}{gaps}{vflags}
</article>"""


CSS = """
*{box-sizing:border-box}
body{margin:0;background:#f4f5f7;color:#16191d;font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:32px 16px 80px}
h1{font-size:26px;margin:0 0 6px}
.lede{color:#5c6672;margin:0 0 28px;max-width:70ch}
.legend{display:flex;gap:18px;flex-wrap:wrap;background:#fff;border:1px solid #e1e5ea;border-radius:10px;padding:12px 16px;margin-bottom:28px;font-size:13px;color:#5c6672}
.legend span{display:flex;align-items:center;gap:6px}
.dot{width:10px;height:10px;border-radius:50%;display:inline-block}
.call{background:#fff;border:1px solid #e1e5ea;border-radius:14px;padding:26px 28px;margin-bottom:26px;box-shadow:0 1px 3px rgba(16,24,40,.04)}
.head-main{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.call h2{font-size:21px;margin:0}
.meta{color:#5c6672;font-size:13px;margin-top:6px}
.badge{font-size:12px;font-weight:600;padding:3px 11px;border-radius:99px}
.badge.ok{background:#e3f6ea;color:#12703a}.badge.warn{background:#fdf1dc;color:#8a5a00}.badge.bad{background:#fde8e8;color:#a01b1b}
.score{padding:2px 8px;border-radius:99px;font-weight:600}
.score.ok{background:#e3f6ea;color:#12703a}.score.warn{background:#fdf1dc;color:#8a5a00}
section{margin-top:22px;border-top:1px solid #eef1f4;padding-top:16px}
h3{font-size:13px;text-transform:uppercase;letter-spacing:.5px;color:#7a8593;margin:0 0 10px}
h3 small{text-transform:none;letter-spacing:0;font-weight:400}
.summary{margin:0;font-size:15.5px}
.dm{margin-top:10px;font-size:13.5px;color:#3d4753}
.items{list-style:none;margin:0;padding:0}
.item{padding:11px 0;border-bottom:1px dashed #eef1f4}
.item:last-child{border-bottom:0}
.item.unverified{background:#fff8f8;border-left:3px solid #d94141;padding-left:12px;margin-left:-12px}
.item.rejected{background:#fffaf2;border-left:3px solid #e0a03a;padding-left:12px;margin-left:-12px}
.txt{display:flex;gap:9px;align-items:baseline;flex-wrap:wrap}
.conf{font-size:11px;padding:1px 7px;border-radius:99px;white-space:nowrap}
.c-high{background:#e3f6ea;color:#12703a}.c-medium{background:#eef1f4;color:#5c6672}.c-low{background:#fdf1dc;color:#8a5a00}
.agreed{font-size:11px;padding:1px 7px;border-radius:99px;white-space:nowrap}
.agreed.yes{background:#e3f6ea;color:#12703a}.agreed.no{background:#fde8e8;color:#a01b1b;font-weight:600}
.owner{font-size:11px;color:#7a8593;border:1px solid #e1e5ea;padding:1px 7px;border-radius:99px}
.stepdate{font-size:12px;font-weight:600;color:#1c4fa1;background:#e8f0fd;padding:1px 8px;border-radius:99px}
.stepdate.none{background:#fdf1dc;color:#8a5a00;font-weight:500}
.ev{list-style:none;margin:8px 0 0;padding:0;font-size:12.5px}
.ev li{color:#5c6672;padding:3px 0 3px 22px;position:relative;font-style:italic}
.mk{position:absolute;left:4px;font-style:normal;font-weight:700}
.ev-ok .mk{color:#2f9e5e}.ev-bad{color:#a01b1b}.ev-bad .mk{color:#d94141}.ev-unk .mk{color:#a9b2bd}
.flag{font-size:12.5px;color:#8a5a00;background:#fdf7ec;border-left:3px solid #e0a03a;padding:5px 10px;border-radius:0 5px 5px 0;margin-top:7px}
.gaps{background:#f7f9fb;border-radius:9px;padding:14px 16px;border-top:0}
.gaps ul{margin:0;padding-left:18px;font-size:13.5px;color:#3d4753}
.gaps li{margin:4px 0}
.vflags{border-top:0}
.dateblock{background:#f7f9fb;border-radius:9px;padding:13px 15px}
.dateval{font-size:18px;font-weight:650}
.tz{font-size:13px;font-weight:400;color:#a01b1b}
.datemeta{font-size:12.5px;color:#5c6672;margin-top:3px}
.datebasis{font-size:12px;color:#7a8593;margin-top:2px;font-style:italic}
.empty{color:#a9b2bd;font-style:italic;margin:0;font-size:13.5px}
"""


def render_html(results: list[CallAnalysis]) -> str:
    cards = "".join(_card(a) for a in results)
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Разбор клиентских звонков</title><style>{CSS}</style></head>
<body><div class="wrap">
<h1>Разбор клиентских звонков</h1>
<p class="lede">Каждое утверждение подкреплено дословной цитатой из транскрипта и механически
сверено с исходником. Даты пересчитаны детерминированным резолвером, а не моделью.
Всё, что не подтвердилось, помечено — и не выдаётся за факт.</p>
<div class="legend">
  <span><i class="dot" style="background:#2f9e5e"></i>цитата подтверждена в транскрипте</span>
  <span><i class="dot" style="background:#d94141"></i>цитата не найдена — пункт под сомнением</span>
  <span><i class="dot" style="background:#e0a03a"></i>флаг валидации или неоднозначность</span>
</div>
{cards}
</div></body></html>"""


def render_markdown(results: list[CallAnalysis]) -> str:
    """Компактная таблица для вставки в письмо или чат."""
    out = ["# Разбор клиентских звонков\n"]
    for a in results:
        nad = a.next_action_date
        when = "—"
        if nad and nad.resolved:
            when = fmt_date(nad.resolved)
            if nad.time:
                when += f", {nad.time}"
            if nad.timezone:
                when += f" ({nad.timezone})"
        elif nad:
            when = f"«{nad.raw_expression}» — точной даты нет ({PRECISION_LABEL.get(nad.precision, '')})"

        agreed = [s for s in a.next_steps if s.agreed]
        rejected = [s for s in a.next_steps if not s.agreed]

        def bullets(items: list) -> str:
            if not items:
                return "— нет данных в разговоре"
            return "<br>".join(f"• {getattr(i, 'text', getattr(i, 'action', ''))}" for i in items)

        out.append(f"\n## {a.company or a.source_file} — {a.call_date.isoformat()} ({a.call_weekday})\n")
        out.append(f"**Менеджер:** {a.manager} · **Клиент:** {a.client_contact} · "
                   f"**Статус:** {a.outcome} · **Цитаты подтверждены:** "
                   f"{a.grounding_score:.0%}\n" if a.grounding_score is not None else "\n")
        out.append("| Поле | Значение |")
        out.append("|---|---|")
        out.append(f"| Итог разговора | {a.summary} |")
        out.append(f"| Следующий шаг | {bullets(agreed)} |")
        out.append(f"| Дата следующего действия | {when} |")
        out.append(f"| Потребности клиента | {bullets(a.client_needs)} |")
        out.append(f"| Риски | {bullets(a.risks)} |")
        out.append(f"| Ошибки менеджера | {bullets(a.manager_mistakes)} |")
        out.append(f"| Внимание руководителя | {bullets(a.needs_supervisor_attention)} |")
        if rejected:
            out.append(f"| ⚠ Предложено, но НЕ согласовано | {bullets(rejected)} |")
        if a.constraints:
            out.append(f"| Ограничения клиента | {bullets(a.constraints)} |")
        if a.data_gaps:
            out.append("| Недостаточно данных | "
                       + "<br>".join(f"• {g}" for g in a.data_gaps) + " |")
        # флаги валидации — единственный признак того, что цитата не подтвердилась,
        # поэтому в markdown-версии они обязаны быть видны так же, как в HTML
        if a.validation_flags:
            out.append("| ⚠ Флаги валидации | "
                       + "<br>".join(f"• {f}" for f in a.validation_flags) + " |")
    return "\n".join(out) + "\n"


def render_csv(results: list[CallAnalysis]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow([
        "Файл", "Компания", "Дата звонка", "Менеджер", "Клиент", "Итог (статус)",
        "Итог разговора", "Следующий шаг (согласованный)", "Дата следующего действия",
        "Точность даты", "Потребности", "Риски", "Ошибки менеджера",
        "Внимание руководителя", "Недостаточно данных", "Флаги", "Цитаты подтверждены",
    ])
    for a in results:
        agreed = [s for s in a.next_steps if s.agreed]
        nad = a.next_action_date
        w.writerow([
            a.source_file, a.company or "", a.call_date.isoformat(), a.manager or "",
            a.client_contact or "", a.outcome, a.summary,
            " | ".join(s.action for s in agreed),
            fmt_date(nad.resolved) + (f", {nad.time}" if nad and nad.time else "") if nad else "—",
            PRECISION_LABEL.get(nad.precision, "") if nad else "",
            " | ".join(i.text for i in a.client_needs),
            " | ".join(i.text for i in a.risks),
            " | ".join(i.text for i in a.manager_mistakes),
            " | ".join(i.text for i in a.needs_supervisor_attention),
            " | ".join(a.data_gaps),
            " | ".join(a.validation_flags),
            f"{a.grounding_score:.0%}" if a.grounding_score is not None else "—",
        ])
    return buf.getvalue()
