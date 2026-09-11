#!/usr/bin/env python3
"""ЗАМЕР 0, шаги 2 и 3: докачка часового ряда и загрузка старших рядов.

ЧТО ЭТОТ СКРИПТ ДЕЛАЕТ:

  шаг 2 (§5 ТЗ) — догружает ЧАСОВОЙ ряд по пяти спотовым инструментам с
      2022-02-01 до последнего ЗАКРЫТОГО часа СУЩЕСТВУЮЩИМ двухпроходным
      загрузчиком ``backtest/loader.py``. Новый загрузчик не пишется: второй
      способ читать те же данные биржи означал бы две их трактовки;
  шаг 3 (§6 ТЗ) — загружает С БИРЖИ дневной, недельный и месячный ряды на
      максимальную доступную глубину.

ЧЕГО ОН НЕ ДЕЛАЕТ. Он не латает пропуски, не собирает старшие бары из часовых и
не пишет ни одной строки вне схемы ``backtest``. Пропуск — это ФАКТ, который
обязан дойти до Замера 1; подстановка синтетики превратила бы измерение в
измерение собственных допущений.

КАК ПРОВЕРЯЕТСЯ ГРАНИЦА ЭТАПА (§13.10 ТЗ; переделано Замером 0.1). ПО СУЩЕСТВУ:
в продакшн-таблице свечей ``public.ohlcv`` нет и не может быть старших
масштабов — продакшн собирает только 1m/5m/15m/1h. Такую строку мог записать
ТОЛЬКО этот этап, и её появление есть запись вне схемы ``backtest``.

Прежняя проверка сравнивала ``count(*)`` продакшн-таблиц до и после, и на
боевой машине она не могла пройти НИКОГДА: продакшн работает 24/7 и пишет
параллельно. Оба прогона 11.09.2026 закончились кодом 6 с «продакшн-таблицы
изменились», и оба раза ложно — за 60 секунд ПОКОЯ счётчики прирастали на
+5/+15/+5/+5, тогда как загрузчик записал 75 417 строк в свою схему. Разница в
три порядка. Счётчики по-прежнему снимаются и печатаются, но это НАБЛЮДЕНИЕ, а
не вердикт, и код возврата от них больше не зависит: код 6 обязан означать
нарушение границы этапа, а не «продакшн работал».

ОСОЗНАННОЕ ИЗМЕНЕНИЕ ПРОТИВ ТЗ 7.4 (§5 ТЗ Замера 0). Там ``BT_PERIOD_TO``
стояло на 01.08.2026, чтобы результат нельзя было подогнать под известное
поведение живой системы. Здесь граница снимается: Замер 1 обязан покрывать и
свежие данные, а защита от подгонки переносится туда, где ей место — в
разделение выборки на обучающий и проверочный отрезки по времени. Загрузка по
01.08.2026 сделала бы такое разделение невозможным. Значение ``BT_PERIOD_TO``
из файла конфигурации здесь НЕ ЧИТАЕТСЯ ВОВСЕ: конец периода — последний
закрытый час на момент запуска, и подменить его из файла нельзя.

БЕЗ ``--apply`` В БАЗУ НЕ УХОДИТ НИ ОДНОГО ЗАПРОСА НА ЗАПИСЬ. Биржа при этом
опрашивается: прочитать биржу — не значит изменить базу, а не опросив её,
показать было бы нечего.

КОДЫ ВОЗВРАТА. Пять кодов §10 ТЗ (0/2/3/4/5) принадлежат ``htf_parity_z0.py``:
только там могут возникнуть все пять условий. Здесь:
  0 — загрузка дошла до конца;
  5 — выборка пуста: ни одного бара ни по одному инструменту;
  6 — загрузка сорвалась (отказ биржи, разошедшийся календарь ряда) ЛИБО
      нарушена граница этапа: в продакшн-таблице появились строки, которые мог
      записать только он.

ЗАПУСК (внутри собранного образа; имена профиля и службы печатает
``deploy/verify_z0.sh``, они взяты из ``docker-compose.yml``):

    # 1. вхолостую — ни одной записи в базу:
    cd /opt/agent-trade && sudo -u agent docker compose --profile backtest \\
        run --rm -e PYTHONUNBUFFERED=1 backtest python scripts/load_htf_z0.py

    # 2. с записью, в фоне (загрузка идёт часами):
    cd /opt/agent-trade && sudo -u agent docker compose --profile backtest \\
        run -d --name z0_load -e PYTHONUNBUFFERED=1 backtest \\
        python scripts/load_htf_z0.py --apply
    docker logs -f z0_load

``Ctrl+C`` при ``docker compose run`` НЕ останавливает контейнер, а только
отсоединяет окно. Остановка — ``docker rm -f z0_load``.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import structlog  # noqa: E402

from backtest import db  # noqa: E402
from backtest.config import ConfigError, read_env_file  # noqa: E402
from backtest.htf import (  # noqa: E402
    BAR_HOURLY,
    HTF_BARS,
    PRODUCTION_FORBIDDEN_TIMEFRAMES,
    HtfError,
    boundary_violation_reasons,
)
from backtest.htf_loader import (  # noqa: E402
    HtfHistory,
    HtfLoadResult,
    backfill_htf,
    verify_stored_chain,
)
from backtest.integrity import SERIES_CANDLES, check_continuity  # noqa: E402
from backtest.loader import (  # noqa: E402
    LoaderError,
    backfill_candles,
    create_http_client,
)
from backtest.z0_metrics import metrics_path, write_metrics  # noqa: E402
from scripts.probe_htf_depth import compose_target  # noqa: E402
from scripts.stop_counterfactual_9_1_4 import peak_rss_mb  # noqa: E402
from src.core.logging import setup_logging  # noqa: E402

_log = structlog.get_logger().bind(component="z0_load")

CONFIG_PATH = Path("backtest/.env.backtest")

# Потолок памяти контейнера (``mem_limit: 1g`` в docker-compose.yml). Поднимать
# запрещено: на Этапе 9.1.3 часть А была убита ядром дважды, пока не стала
# считать потоком.
MEMORY_CAP_MB = 1024.0

# Страница загрузки старших баров. Фактический максимум измеряется зондом
# (scripts/probe_htf_depth.py); здесь значение по умолчанию, перекрываемое
# ключом --page-limit. «По памяти» оно не подставляется никуда, кроме этого
# значения по умолчанию, и оно напечатано в отчёте прогона.
DEFAULT_PAGE_LIMIT = 100


def last_closed_hour(now: datetime) -> datetime:
    """Последний ЗАКРЫТЫЙ час на момент ``now``.

    Час, который идёт сейчас, ещё формируется, и его свеча — цена «пока что».
    Граница периода ставится на НАЧАЛО текущего часа: свеча, открывшаяся в этот
    момент, ещё не закрыта, а предыдущая — закрыта.
    """
    return now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def read_settings(path: Path) -> dict[str, Any]:
    """Читает ключи Замера 0 из ``backtest/.env.backtest``.

    НАПОМИНАНИЕ, СТОИВШЕЕ ЭТАПА 8.7: этот файл В РЕПОЗИТОРИЙ НЕ ВХОДИТ и
    правится на сервере ВРУЧНУЮ. Тогда ``BT_FEE_ROUNDTRIP_PCT`` остался
    неисправленным, потому что правка коснулась файла-образца, а рабочего —
    нет. Поэтому здесь печатается фактический путь и фактически прочитанные
    значения: увидеть «взято не то» надо в первых строках лога, а не в числах
    отчёта.
    """
    values = read_env_file(path)
    instruments = [
        item.strip()
        for item in values.get("BT_HTF_INSTRUMENTS", "").split(",")
        if item.strip()
    ]
    bars = [
        item.strip() for item in values.get("BT_HTF_BARS", "").split(",") if item.strip()
    ]
    if not instruments:
        raise ConfigError(
            "BT_HTF_INSTRUMENTS не задан в " + str(path) + ". Состав Замера 0 — "
            "ТОЛЬКО спот: BTC-USDT,ETH-USDT,SOL-USDT,XRP-USDT,DOGE-USDT"
        )
    if not bars:
        raise ConfigError(f"BT_HTF_BARS не задан в {path}")
    wrong = [bar for bar in bars if bar not in HTF_BARS]
    if wrong:
        raise ConfigError(
            f"BT_HTF_BARS содержит {wrong}: допустимы только {list(HTF_BARS)}. "
            "У OKX дневные, недельные и месячные свечи БЕЗ суффикса utc "
            "открываются по гонконгскому времени (UTC+8), и ряд без суффикса "
            "не используется нигде — ни в загрузке, ни в контроле (§3 ТЗ)"
        )
    period_from = values.get("BT_PERIOD_FROM", "").strip()
    if not period_from:
        raise ConfigError(f"BT_PERIOD_FROM не задан в {path}")
    pause_raw = values.get("BT_REQUEST_PAUSE_MS", "").strip()
    if not pause_raw:
        raise ConfigError(
            f"BT_REQUEST_PAUSE_MS не задан в {path}. Значение берётся ИЗ ЗОНДА "
            "(scripts/probe_htf_depth.py), а не из памяти"
        )
    return {
        "instruments": instruments,
        "bars": bars,
        "period_from": datetime.fromisoformat(period_from.replace("Z", "+00:00")),
        "pause_ms": int(pause_raw),
    }


async def load_hourly(
    instruments: list[str], since: datetime, until: datetime, *, apply: bool
) -> int:
    """Шаг 2: докачка часового ряда СУЩЕСТВУЮЩИМ двухпроходным загрузчиком.

    ``already_in_db`` и ``appended`` печатаются ПО КАЖДОМУ инструменту отдельно
    (§5 ТЗ): суммарное число скрыло бы инструмент, по которому не догрузилось
    ничего.
    """
    total_appended = 0
    print("\n=== Шаг 2. Часовой ряд ===", flush=True)
    print(f"  период: {since.isoformat()} … {until.isoformat()} "
          f"(конец — последний ЗАКРЫТЫЙ час на момент запуска)", flush=True)
    for inst_id in instruments:
        before = int(await db.fetchval(
            "SELECT count(*) FROM backtest.candles WHERE inst_id=$1 AND bar=$2;",
            inst_id, BAR_HOURLY,
        ) or 0)
        if apply:
            await backfill_candles(inst_id, BAR_HOURLY, since, until)
        after = int(await db.fetchval(
            "SELECT count(*) FROM backtest.candles WHERE inst_id=$1 AND bar=$2;",
            inst_id, BAR_HOURLY,
        ) or 0)
        written = after - before
        total_appended += written
        print(f"  {inst_id:<10} already_in_db={before:>7}  appended={written:>7}"
              + ("" if apply else "   (без --apply запись не выполнялась)"),
              flush=True)
    return total_appended


async def check_gaps(
    instruments: list[str], since: datetime, until: datetime
) -> int:
    """Непрерывность часового ряда: пропуски СЧИТАЮТСЯ и ПЕРЕЧИСЛЯЮТСЯ, но не латаются.

    Латать синтетикой запрещено ни при каких условиях (§5 ТЗ): пропуск — это
    факт, который обязан дойти до Замера 1 и быть там учтён, а не сглаженный
    след чужого допущения.

    РАЗРЫВ ВНУТРИ РЯДА И НЕДОСТАЮЩИЙ КРАЙ — РАЗНЫЕ ВЕЩИ, и печатаются они
    порознь. ``check_continuity`` ищет расстояния между СОСЕДНИМИ имеющимися
    барами, и ряд, кончающийся на полгода раньше границы периода, разрывов не
    содержит вовсе: соседей у отсутствующего края нет. Напечатать при этом
    «пропущено 0» рядом с «покрытие 25%» значило бы поставить читателя перед
    двумя противоречащими числами и предложить выбрать. Поэтому итог —
    ``ожидалось минус фактически``, а разрывы внутри перечислены отдельно.
    """
    print("\n=== Шаг 2. Непрерывность часового ряда ===", flush=True)
    total = 0
    for inst_id in instruments:
        report = await check_continuity(
            inst_id, SERIES_CANDLES, since, until, bar=BAR_HOURLY
        )
        border = await db.fetchrow(
            "SELECT min(open_time) AS lo, max(open_time) AS hi "
            "FROM backtest.candles WHERE inst_id=$1 AND bar=$2 "
            "AND open_time BETWEEN $3 AND $4;",
            inst_id, BAR_HOURLY, since, until,
        )
        inside = sum(gap[2] for gap in report.gaps)
        missing = max(0, report.expected_n - report.actual_n)
        total += missing
        low = border["lo"] if border else None
        high = border["hi"] if border else None
        print(f"  {inst_id:<10} часов ожидалось {report.expected_n:>7}, "
              f"фактически {report.actual_n:>7}, ПРОПУЩЕНО ВСЕГО {missing:>6} "
              f"({report.coverage_pct:.3f}% покрытия)", flush=True)
        print(f"             из них разрывами внутри ряда: {inside} "
              f"в {len(report.gaps)} местах", flush=True)
        print(f"             ряд в базе: "
              f"{low.isoformat() if low else '—'} … "
              f"{high.isoformat() if high else '—'}", flush=True)
        if low is not None and low > since:
            head = int((low - since).total_seconds() // 3600)
            print(f"             НАЧАЛО КОРОЧЕ ПЕРИОДА на {head} ч — истории "
                  "биржи до этой точки нет либо она не догружена", flush=True)
        if high is not None and high < until - timedelta(hours=1):
            tail = int((until - high).total_seconds() // 3600)
            print(f"             КОНЕЦ КОРОЧЕ ПЕРИОДА на {tail} ч — свежий край "
                  "не догружен", flush=True)
        for gap_from, gap_to, count in report.gaps[:20]:
            print(f"      разрыв {gap_from.isoformat()} … {gap_to.isoformat()} "
                  f"— {count} ч", flush=True)
        if len(report.gaps) > 20:
            print(f"      … и ещё {len(report.gaps) - 20} разрывов "
                  "(полный перечень — в backtest.gaps)", flush=True)
    print("  Пропуски НЕ ЗАЛАТАНЫ и латанию не подлежат (§5 ТЗ).", flush=True)
    return total


async def load_htf(
    instruments: list[str], bars: list[str], *, pause_ms: int,
    page_limit: int, apply: bool,
) -> list[HtfLoadResult]:
    """Шаг 3: загрузка старших рядов С БИРЖИ на максимальную доступную глубину."""
    print("\n=== Шаг 3. Старшие ряды (загрузка с биржи) ===", flush=True)
    print(f"  пауза между запросами: {pause_ms} мс (значение из зонда), "
          f"страница: {page_limit} баров", flush=True)
    results: list[HtfLoadResult] = []
    http_client = create_http_client()
    client = HtfHistory(http_client, pause_ms=pause_ms)
    try:
        for inst_id in instruments:
            for bar in bars:
                print(f"  → {inst_id} {bar}", flush=True)
                result = await backfill_htf(
                    inst_id, bar, client=client, page_limit=page_limit, apply=apply
                )
                results.append(result)
                print(
                    f"    already_in_db={result.already_in_db:>6}  "
                    f"appended={result.appended:>6}  "
                    f"незакрытых отброшено={result.dropped_unconfirmed:>3}  "
                    f"страниц={result.pages}",
                    flush=True,
                )
                if result.earliest is not None:
                    print(f"    глубина в базе: {result.earliest.isoformat()} … "
                          f"{result.latest.isoformat()}", flush=True)
                for note in result.notes:
                    print(f"    ВНИМАНИЕ: {note}", flush=True)
    finally:
        await http_client.aclose()
    return results


async def verify_chains(instruments: list[str], bars: list[str]) -> int:
    """Сверка календарного правила с фактическим рядом биржи (§3 ТЗ).

    Границы недели и месяца не предполагаются, а проверяются: конец каждого
    сохранённого бара обязан совпасть с началом следующего. Расхождение
    останавливает этап здесь, а не всплывает в §7 как «ошибка арифметики
    сборки».
    """
    print("\n=== Шаг 3. Проверка календаря: конец бара = начало следующего ===",
          flush=True)
    total = 0
    for inst_id in instruments:
        for bar in bars:
            checked = await verify_stored_chain(inst_id, bar)
            total += checked
            print(f"  {inst_id:<10} {bar:<7} пар проверено: {checked}", flush=True)
    return total


def print_counts(title: str, counts: dict[str, int]) -> None:
    print(f"\n  {title}", flush=True)
    for table, value in sorted(counts.items()):
        print(f"    public.{table:<22} {value}", flush=True)


def print_count_drift(before: dict[str, int], after: dict[str, int]) -> None:
    """Прирост счётчиков продакшн-таблиц. НАБЛЮДЕНИЕ, А НЕ ВЕРДИКТ.

    Печатается именно как наблюдение, и это сказано прямо в самом выводе: на
    боевой машине продакшн пишет 24/7, и прирост здесь означает «система
    живёт». Вердикт даёт :func:`check_stage_boundary` — по признаку
    происхождения строки, а не по её количеству.
    """
    drift = {
        table: value - before.get(table, 0)
        for table, value in after.items()
        if value != before.get(table, 0)
    }
    if not drift:
        print("\n  Прирост счётчиков продакшн-таблиц: нулевой. Это НЕ вердикт "
              "о границе этапа — значит лишь, что за время прогона продакшн "
              "ничего не записал.", flush=True)
        return
    print("\n  Прирост счётчиков продакшн-таблиц за время прогона:", flush=True)
    for table, value in sorted(drift.items()):
        print(f"    public.{table:<22} {value:+d}", flush=True)
    print("  Это НАБЛЮДЕНИЕ, а не находка: продакшн работает параллельно и "
          "пишет в свои таблицы сам (измерено 11.09.2026: за 60 секунд покоя "
          "signals +5, agent_outputs +15, ohlcv +5, open_interest +5). Границу "
          "этапа проверяет раздел ниже.", flush=True)


async def check_stage_boundary(before: dict[str, int] | None) -> tuple[int, list[str]]:
    """Граница этапа по существу (§13.10 ТЗ). Возвращает (код, причины).

    Проверяется не число строк, а их ПРОИСХОЖДЕНИЕ: старший масштаб в
    ``public.ohlcv`` мог записать только этот этап. Образец взят из пункта 7
    deploy/verify_z0.sh, где он уже работает.
    """
    after = await db.production_boundary_violations()
    print("\n=== Граница этапа: продакшн не тронут (§13.10 ТЗ) ===", flush=True)
    print("  Проверяется НЕ число строк (продакшн пишет параллельно), а их "
          "происхождение: масштабов "
          f"{', '.join(PRODUCTION_FORBIDDEN_TIMEFRAMES)} в public.ohlcv быть "
          "не может — продакшн собирает только 1m/5m/15m/1h.", flush=True)
    if after is None:
        print("  ⚪ НЕ ПРОВЕРЕНО: таблицы public.ohlcv нет. Это НЕ «нарушений "
              "нет».", flush=True)
    else:
        print(f"  строк со старшим масштабом в public.ohlcv: ДО {before or {}}, "
              f"ПОСЛЕ {after}", flush=True)
    reasons = boundary_violation_reasons(before=before, after=after)
    if not reasons:
        print("  🟢 граница этапа не нарушена: ни одной строки, которую мог бы "
              "записать только этот этап, в продакшн-таблице нет.", flush=True)
        return 0, []
    for reason in reasons:
        print(f"  🔴 {reason}", flush=True)
    return 6, reasons


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Замер 0, шаги 2–3: часовой ряд и старшие ряды. "
                    "Без --apply в базу не уходит ни одного запроса на запись."
    )
    parser.add_argument("--apply", action="store_true",
                        help="разрешить запись в схему backtest")
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument("--page-limit", type=int, default=DEFAULT_PAGE_LIMIT,
                        help="страница загрузки старших баров; максимум измеряется зондом")
    parser.add_argument(
        "--steps", choices=("all", "hourly", "htf"), default="all",
        help=(
            "какие шаги выполнять: all — оба (по умолчанию), hourly — только "
            "часовой ряд (шаг 2), htf — только старшие ряды (шаг 3). Разделение "
            "нужно затем, чтобы между ними лёг слепок целей "
            "(scripts/risk_targets_parity_z0.py --snapshot): иначе расхождение "
            "целей нельзя будет отличить от влияния свежих часовых свечей"
        ),
    )
    args = parser.parse_args()

    setup_logging()
    started = datetime.now(UTC)
    until = last_closed_hour(started)

    print("=" * 100, flush=True)
    print(" ЗАМЕР 0, шаги 2–3: часовой ряд и старшие ряды", flush=True)
    print("=" * 100, flush=True)
    print(f" Время запуска:   {started.isoformat()} (UTC)", flush=True)
    print(f" Docker:          {compose_target()}", flush=True)
    print(f" Конфигурация:    {args.config} "
          "(в репозиторий НЕ входит, правится на сервере вручную)", flush=True)
    print(f" Режим:           {'ЗАПИСЬ (--apply)' if args.apply else 'ТОЛЬКО ЧТЕНИЕ'}",
          flush=True)

    try:
        settings = read_settings(Path(args.config))
    except ConfigError as exc:
        print(f"\n ОТКАЗ КОНФИГУРАЦИИ: {exc}", flush=True)
        return 6

    instruments: list[str] = settings["instruments"]
    bars: list[str] = settings["bars"]
    since: datetime = settings["period_from"]
    print(f" Инструменты:     {instruments} (только СПОТ)", flush=True)
    print(f" Старшие масштабы:{bars}", flush=True)
    print(f" BT_PERIOD_FROM:  {since.isoformat()}", flush=True)
    print(f" BT_REQUEST_PAUSE_MS: {settings['pause_ms']} (из зонда)", flush=True)

    await db.connect()
    exit_code = 0
    try:
        if not await db.schema_exists():
            print("\n ОТКАЗ: схемы backtest нет — миграция 008 не применена.",
                  flush=True)
            return 6
        before = await db.production_row_counts()
        print_counts("Счётчики продакшн-таблиц ДО (наблюдение, не вердикт):",
                     before)
        # Снимок ДО — чтобы отличить строку, появившуюся В ЭТОМ прогоне, от
        # нарушения границы, случившегося раньше. Оба исхода блокирующие, но
        # называются они по-разному, и смешивать их нельзя.
        boundary_before = await db.production_boundary_violations()

        do_hourly = args.steps in ("all", "hourly")
        do_htf = args.steps in ("all", "htf")

        hourly_appended = 0
        gaps = 0
        if do_hourly:
            hourly_appended = await load_hourly(
                instruments, since, until, apply=args.apply
            )
            gaps = await check_gaps(instruments, since, until)
        else:
            print("\n=== Шаг 2 ПРОПУЩЕН (--steps htf) ===", flush=True)
            print("  Часовой ряд не трогался. Числа z0_hourly_appended и "
                  "z0_hourly_gaps ниже — нули ПОТОМУ ЧТО ШАГ НЕ ВЫПОЛНЯЛСЯ, а "
                  "не потому, что дописывать было нечего.", flush=True)

        results: list[HtfLoadResult] = []
        chain_pairs = 0
        if do_htf:
            results = await load_htf(
                instruments, bars, pause_ms=settings["pause_ms"],
                page_limit=args.page_limit, apply=args.apply,
            )
            chain_pairs = await verify_chains(instruments, bars) if args.apply else 0
        else:
            print("\n=== Шаг 3 ПРОПУЩЕН (--steps hourly) ===", flush=True)
            print("  Старшие ряды не грузились. САМОЕ ВРЕМЯ снять слепок целей: "
                  "python scripts/risk_targets_parity_z0.py --snapshot "
                  "/opt/agent-trade/analysis_out/z0_targets.json", flush=True)
        if do_htf and not args.apply:
            print("\n=== Шаг 3. Проверка календаря пропущена ===", flush=True)
            print("  Без --apply новые бары в базу не попали, и проверять "
                  "нечего. Это НЕ «проверка прошла».", flush=True)

        after = await db.production_row_counts()
        print_counts("Счётчики продакшн-таблиц ПОСЛЕ (наблюдение, не вердикт):",
                     after)
        print_count_drift(before, after)
        boundary_code, boundary_reasons = await check_stage_boundary(
            boundary_before
        )
        exit_code = exit_code or boundary_code

        htf_written = sum(item.appended for item in results)
        dropped = sum(item.dropped_unconfirmed for item in results)
        empty = [f"{item.inst_id} {item.bar}" for item in results
                 if item.already_in_db + item.appended == 0]

        print("\n" + "=" * 100, flush=True)
        print(" ИТОГ ШАГОВ 2–3", flush=True)
        print("=" * 100, flush=True)
        print(f"  выполнены шаги:                {args.steps}", flush=True)
        print(f"  часовых баров дописано:        {hourly_appended}"
              + ("" if do_hourly else "   (ШАГ НЕ ВЫПОЛНЯЛСЯ)"), flush=True)
        print(f"  пропущено часов (не латано):   {gaps}"
              + ("" if do_hourly else "   (ШАГ НЕ ВЫПОЛНЯЛСЯ)"), flush=True)
        print(f"  старших баров записано:        {htf_written}"
              + ("" if do_htf else "   (ШАГ НЕ ВЫПОЛНЯЛСЯ)"), flush=True)
        print(f"  незакрытых баров отброшено:    {dropped}", flush=True)
        if dropped == 0 and do_htf:
            print("  ⚠ НОЛЬ ОТБРОШЕННЫХ — ПОДОЗРИТЕЛЬНО (§8 ТЗ). Это доклад, а не "
                  "успех: проверьте, что свежий край брался с /market/candles.",
                  flush=True)
        print(f"  пар «конец = начало» сверено:  {chain_pairs}", flush=True)
        # «Находок», а не «нарушений»: одна из них — «проверить было нечем»,
        # и выдать её за нарушение значило бы соврать в другую сторону.
        # Существо каждой находки названо разделом выше.
        print(f"  находок по границе этапа:      {len(boundary_reasons)}"
              + ("   (старших масштабов в public.ohlcv нет)"
                 if not boundary_reasons else ""), flush=True)
        if empty:
            print(f"  🔴 ПУСТЫЕ РЯДЫ: {empty}", flush=True)
            exit_code = exit_code or 5

        peak = peak_rss_mb()
        print(f"  пиковая память:                {peak:,.0f} МБ "
              f"(потолок {MEMORY_CAP_MB:,.0f} МБ)", flush=True)
        keys: dict[str, Any] = {
            "z0_hourly_appended": hourly_appended,
            "z0_hourly_gaps": gaps,
            "z0_htf_rows_written": htf_written,
            "z0_htf_dropped_unconfirmed": dropped,
            "z0_boundary_violations": len(boundary_reasons),
            "peak_rss_mb": round(peak, 1),
        }
        _log.info("Замер 0: загрузка завершена", **keys)

        # Ключи пишутся ЕЩЁ И В ФАЙЛ: журнал контейнера вытесняется по объёму и
        # исчезает вместе с контейнером, а файл в analysis_out переживает и то,
        # и другое. Пункт 5 verify_z0.sh читает файл, журнал — запасной путь.
        written = write_metrics(f"z0_load_{args.steps}", keys)
        if written is None:
            print(f"  🟡 ключи НЕ записаны в {metrics_path()}: каталог "
                  "недоступен на запись. Загрузку это не отменяет, но "
                  "verify_z0.sh придётся читать журнал контейнера.", flush=True)
        else:
            print(f"  машиночитаемые ключи дописаны в {written}", flush=True)
    except (HtfError, ConfigError, LoaderError) as exc:
        # Отказ биржи и нарушенная посылка — это НАЗВАННАЯ причина и код 6, а
        # не трассировка стека: причину читает человек. Дефект D-8 состоял ровно
        # в том, что зонд, получивший 403, молча повис три раза подряд.
        print(f"\n 🔴 ЗАГРУЗКА ОСТАНОВЛЕНА: {exc}", flush=True)
        exit_code = 6
    finally:
        await db.close()

    print("", flush=True)
    print(f" ЗАМЕР 0, ШАГИ 2–3 ЗАВЕРШЕНЫ, код возврата {exit_code}. "
          f"Время: {datetime.now(UTC).isoformat()}", flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
