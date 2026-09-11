"""Сборка: транскрипт → LLM → схема → валидаторы → CallAnalysis."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pydantic import ValidationError

from .ingest import Transcript, load_dir
from .models import CallAnalysis
from .prompt import SYSTEM, build_user_message
from .providers import LLMProvider, extract_json
from .validate import validate


def analyze_one(t: Transcript, provider: LLMProvider) -> CallAnalysis:
    raw = provider.complete(SYSTEM, build_user_message(t))
    data = extract_json(raw)

    # метаданные ставим сами — модель к ним не допускается
    data["source_file"] = t.name
    data["call_date"] = t.call_date.isoformat()
    data["call_weekday"] = t.weekday
    data["company"] = t.company
    data["manager"] = t.manager
    data["client_contact"] = t.client_contact

    try:
        analysis = CallAnalysis.model_validate(data)
    except ValidationError as e:
        # MVP: схема — контракт, но один кривой пункт не должен ронять весь прогон
        raise RuntimeError(f"{t.name}: ответ модели не прошёл схему:\n{e}") from e

    return validate(analysis, t)


def analyze_dir(
    transcripts_dir: Path, provider: LLMProvider, workers: int = 4
) -> list[CallAnalysis]:
    """Транскрипты независимы, поэтому идут параллельно — четыре звонка
    обрабатываются за время одного, а не четырёх."""
    transcripts = load_dir(transcripts_dir)
    if not transcripts:
        raise SystemExit(f"В {transcripts_dir} не найдено ни одного .md")

    results: list[tuple[int, CallAnalysis | Exception]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(analyze_one, t, provider): i for i, t in enumerate(transcripts)
        }
        for fut, i in futures.items():
            try:
                results.append((i, fut.result()))
            except Exception as e:  # noqa: BLE001 — падение одного звонка не рушит пакет
                results.append((i, e))

    ok: list[CallAnalysis] = []
    for i, r in sorted(results, key=lambda x: x[0]):
        if isinstance(r, Exception):
            print(f"  ✗ {transcripts[i].name}: {r}")
        else:
            flags = len(r.validation_flags)
            score = f"{r.grounding_score:.0%}" if r.grounding_score is not None else "—"
            print(f"  ✓ {r.source_file:26} цитаты {score:>5}  флагов: {flags}")
            ok.append(r)
    return ok
