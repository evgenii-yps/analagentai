#!/usr/bin/env python3
"""ЗАМЕР 0, шаг 1: зонд глубины старших таймфреймов. БЛОКИРУЮЩИЙ, выполняется ПЕРВЫМ.

Ничего не пишет в базу и ничего не меняет. Печатает таблицу «5 инструментов ×
4 масштаба» и три измерения, без которых остальные шаги считать нельзя.

ПОЧЕМУ ЗОНД ВООБЩЕ НУЖЕН, ЕСЛИ ЕСТЬ ДОКУМЕНТАЦИЯ БИРЖИ. Замер 22.08.2026 дал
фактический максимум ``limit`` = 300 при ожидавшихся по документации 100. Число,
взятое из документации, стоило бы втрое большего числа запросов и часов работы
— поэтому ЗДЕСЬ ОНО ИЗМЕРЯЕТСЯ.

ГЛАВНОЕ ИЗМЕРЕНИЕ ЗОНДА — ЧАСОВОЙ ПОЯС (§3 ТЗ). У OKX дневные, недельные и
месячные свечи по умолчанию открываются по ГОНКОНГСКОМУ времени (UTC+8), а для
UTC существуют отдельные значения ``bar``: ``1Dutc``, ``1Wutc``, ``1Mutc``.
Зонд снимает ОБА ряда, печатает их первые метки и разницу в часах. Ожидание —
8 часов, но именно ожидание, а не вывод: знак смещения измеряется, а не
выводится рассуждением. Если разницы нет — поведение биржи изменилось, и это
надо доложить, а не подгонять код под ожидание.

ГРАНИЦЫ НЕДЕЛИ И МЕСЯЦА НЕ ПРЕДПОЛАГАЮТСЯ. Зонд печатает, какой день недели
соответствует началу недельного бара и какое число месяца — началу месячного.
Эти значения ПОДСТАВЛЯЮТСЯ В СБОРКУ (§7) из замера, а не из текста задания.

ТРЕБОВАНИЯ, КАЖДОЕ ИЗ ПРОШЛОГО ДЕФЕКТА:

  * Ходит тем же HTTP-клиентом, что продакшн (``src/core/http.py``, браузерная
    подпись). Стандартная библиотека получает ``403, error code 1010`` ещё до
    проверки ключа, и зонд без подписи напечатал бы «биржа недоступна» — ложь
    в обратную сторону. Подпись и код ответа печатаются ПЕРВОЙ строкой.
  * На каждом запросе таймаут; при любом исходе печатается код ответа и текст
    ошибки (D-8: зонд, получивший 403, молча повис три раза подряд).
  * Прогресс печатается ПО ХОДУ работы, а не в конце (D-10).
  * Имя профиля и службы Docker берётся из ``docker-compose.yml``, а не из
    текста задания (:func:`compose_target`).

НИ ОДИН ИНСТРУМЕНТ НЕ ВЫБРАСЫВАЕТСЯ ИЗ СОСТАВА ПО ИТОГАМ ЗОНДА. Малая глубина —
это факт для Замера 1, а не повод молча сократить состав.

ЗАПУСК (внутри собранного образа; офлайн-прогон доказательством не считается):

    cd /opt/agent-trade && sudo -u agent docker compose --profile backtest \\
        run -d --name z0_probe -e PYTHONUNBUFFERED=1 --no-deps backtest \\
        python scripts/probe_htf_depth.py
    docker logs -f z0_probe

Имена профиля и службы выше — те, что печатает :func:`compose_target` на хосте.
``Ctrl+C`` при ``docker compose run`` НЕ останавливает контейнер, а только
отсоединяет окно; остановка — ``docker rm -f z0_probe``.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import pathlib
import sys
import time
from datetime import UTC, datetime
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest.htf import (  # noqa: E402
    HTF_BARS,
    measure_boundaries,
    period_seconds_hint,
)
from backtest.loader import (  # noqa: E402
    OKX_BASE_URL,
    PATH_HISTORY_CANDLES,
    create_http_client,
)

# Свежий эндпоинт: в отличие от history-candles он отдаёт ФОРМИРУЮЩИЙСЯ бар с
# ``confirm = 0``. Зонду он нужен, чтобы показать незакрытый бар живьём, а
# загрузчику — чтобы счётчик отброшенных не был нулевым по построению (§8 ТЗ).
PATH_CANDLES = "/api/v5/market/candles"

RATE_LIMIT_CODE = "50011"

# Кандидаты на максимальный limit. Проверяются ВСЕ, и печатается «запрошено →
# получено» по каждому: биржа на завышенный limit не отвечает ошибкой, а молча
# отдаёт свой потолок, и увидеть его можно только так.
LIMIT_CANDIDATES = (500, 300, 200, 100)

# Темпы для поиска потолка частоты: пауза между запросами, мс.
PACE_CANDIDATES_MS = (400, 200, 100, 50, 25)

# Сколько запросов подряд делать на каждом темпе.
PACE_BURST = 12

# Через сколько страниц печатать ход обхода истории (дефект D-10).
PROGRESS_EVERY_PAGES = 10

# Потолок числа страниц на один ряд: зонд обязан завершаться, даже если
# пагинация биржи однажды зациклится.
MAX_PAGES = 3000

# Спотовые инструменты Замера 0. Контракты не участвуют: и теханализ, и будущий
# агент старшего таймфрейма работают по СПОТУ (§5 ТЗ).
DEFAULT_INSTRUMENTS = "BTC-USDT,ETH-USDT,SOL-USDT,XRP-USDT,DOGE-USDT"

# Масштабы: часовой плюс три старших. Часовой здесь для того, чтобы таблица
# глубины была «5 × 4», как требует §13.2 ТЗ.
DEFAULT_BARS = ("1H", *HTF_BARS)

WEEKDAY_NAMES = (
    "понедельник", "вторник", "среда", "четверг",
    "пятница", "суббота", "воскресенье",
)


def to_dt(value: Any) -> datetime:
    return datetime.fromtimestamp(int(value) / 1000, tz=UTC)


def compose_target() -> str:
    """Имя службы и профиля Docker — ИЗ ``docker-compose.yml``, а не из текста.

    Файл в образ НЕ КОПИРУЕТСЯ (backtest/Dockerfile переносит только src,
    backtest и scripts), поэтому внутри контейнера его нет — и об этом
    печатается прямо, а не подставляется «известное» имя. На хосте, где файл
    есть, имена читаются оттуда; их же печатает ``deploy/verify_z0.sh``.

    Разбор намеренно примитивный, без внешней библиотеки YAML: в requirements
    её нет, а вводить зависимость ради двух строк отчёта нельзя.
    """
    here = pathlib.Path(__file__).resolve()
    for parent in [pathlib.Path.cwd(), *here.parents]:
        path = parent / "docker-compose.yml"
        if path.is_file():
            return _parse_compose(path)
    return (
        "docker-compose.yml рядом с процессом НЕ НАЙДЕН — внутри образа его и "
        "нет (в backtest/Dockerfile копируются только src, backtest, scripts). "
        "Имена службы и профиля снимите на ХОСТЕ: их печатает deploy/verify_z0.sh"
    )


def _parse_compose(path: pathlib.Path) -> str:
    """Служба, собираемая из ``backtest/Dockerfile``, и её профили."""
    service: str | None = None
    found: dict[str, list[str]] = {}
    dockerfile_of: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw[:2] == "  " and raw[2:3] not in (" ", "#", "") and raw.rstrip().endswith(":"):
            service = raw.strip().rstrip(":")
            continue
        if service is None:
            continue
        line = raw.strip()
        if line.startswith("dockerfile:"):
            dockerfile_of[service] = line.split(":", 1)[1].strip()
        if line.startswith("profiles:"):
            found[service] = [
                item.strip().strip('"').strip("'")
                for item in line.split(":", 1)[1].strip().strip("[]").split(",")
                if item.strip()
            ]
    for name, dockerfile in dockerfile_of.items():
        if dockerfile.endswith("backtest/Dockerfile"):
            profiles = found.get(name) or ["<профиля нет>"]
            return (
                f"{path}: служба «{name}», профиль(и) {profiles}. "
                f"Запуск: docker compose --profile {profiles[0]} run … {name} …"
            )
    return f"{path}: службы, собираемой из backtest/Dockerfile, не найдено; профили: {found}"


async def call(client: Any, path: str, params: dict[str, Any]) -> dict[str, Any]:
    """Один запрос. Возвращает тело ответа ЛИБО описание отказа — но всегда возвращает.

    Зонд обязан пережить любой исход и напечатать его: он для того и нужен,
    чтобы отличить «истории нет» от «нас не пустили» (D-8). Таймаут задан в
    клиенте (``backtest.loader``), исключение здесь не всплывает наружу.
    """
    started = time.monotonic()
    try:
        response = await client.get(path, params=params)
    except Exception as exc:  # noqa: BLE001 — сетевая ошибка тоже результат
        return {
            "code": "exception", "msg": f"{type(exc).__name__}: {exc}", "data": [],
            "elapsed_s": round(time.monotonic() - started, 3),
        }
    if response.status_code != 200:
        return {
            "code": f"HTTP {response.status_code}",
            "msg": response.text[:200],
            "data": [],
            "elapsed_s": round(time.monotonic() - started, 3),
        }
    body = response.json()
    body["elapsed_s"] = round(time.monotonic() - started, 3)
    return body


async def probe_signature(client: Any, inst_id: str) -> None:
    """Подпись клиента и код ответа биржи. Печатается ПЕРВОЙ.

    Отказ по подписи выглядит как «истории нет», и без этой строки его легко
    принять за отсутствие данных — ложь в обратную сторону.
    """
    print("=== Проверка доступа ===", flush=True)
    print(f"  подпись клиента (User-Agent): {client.headers.get('user-agent', '<нет>')}",
          flush=True)
    body = await call(client, PATH_HISTORY_CANDLES,
                      {"instId": inst_id, "bar": "1H", "limit": "1"})
    print(f"  пробный запрос: код {body.get('code')}, "
          f"{body.get('elapsed_s')} с, строк {len(body.get('data') or [])}", flush=True)
    if str(body.get("code")) != "0":
        print(f"  тело ответа: {str(body.get('msg'))[:200]}", flush=True)
        print("  ВНИМАНИЕ: биржа не пустила. Все пустые результаты ниже — "
              "следствие отказа, а НЕ отсутствия истории.", flush=True)


async def probe_timezone(client: Any, inst_id: str) -> dict[str, Any]:
    """§3 ТЗ: измерить разницу между ``1D`` и ``1Dutc``. ПРОВЕРИТЬ, А НЕ ПОВЕРИТЬ.

    Снимаются оба ряда, печатаются первые метки и разница В ЧАСАХ СО ЗНАКОМ.
    Ожидание — 8 часов, но оно записано как ожидание: если разницы нет,
    поведение биржи изменилось, и это доклад, а не повод править код.
    """
    print("\n=== §3. Часовой пояс дневной свечи: 1D против 1Dutc ===", flush=True)
    out: dict[str, Any] = {}
    stamps: dict[str, datetime | None] = {}
    for bar in ("1D", "1Dutc"):
        body = await call(client, PATH_HISTORY_CANDLES,
                          {"instId": inst_id, "bar": bar, "limit": "3"})
        rows = body.get("data") or []
        if str(body.get("code")) != "0" or not rows:
            print(f"  {inst_id} {bar}: НЕ СНЯТО — код {body.get('code')}, "
                  f"{str(body.get('msg'))[:120]}", flush=True)
            stamps[bar] = None
            continue
        # data идёт от новых к старым; «первая метка» ряда — самая свежая из
        # отданных. Для сравнения поясов важна не глубина, а положение внутри
        # суток, поэтому берётся именно она.
        newest = max(to_dt(row[0]) for row in rows)
        stamps[bar] = newest
        print(f"  {inst_id} {bar}: самая свежая отданная метка "
              f"{newest.isoformat()} (время суток UTC {newest.strftime('%H:%M:%S')})",
              flush=True)
        await asyncio.sleep(0.3)

    plain, utc_bar = stamps.get("1D"), stamps.get("1Dutc")
    out["first_1D"] = plain.isoformat() if plain else None
    out["first_1Dutc"] = utc_bar.isoformat() if utc_bar else None
    if plain is None or utc_bar is None:
        out["shift_hours"] = None
        print("  РАЗНИЦА НЕ ИЗМЕРЕНА: один из рядов не снят.", flush=True)
        return out
    shift = (plain - utc_bar).total_seconds() / 3600.0
    out["shift_hours"] = shift
    print(f"  РАЗНИЦА (1D минус 1Dutc): {shift:+.2f} ч", flush=True)
    if shift == 0:
        print("  ВНИМАНИЕ: разницы НЕТ. Ожидалось 8 часов. Поведение биржи "
              "изменилось — это надо доложить, а не подгонять код под ожидание.",
              flush=True)
    else:
        print(f"  Знак измерен, не выведен: бар 1D открывается "
              f"{'ПОЗЖЕ' if shift > 0 else 'РАНЬШЕ'} бара 1Dutc на "
              f"{abs(shift):.2f} ч.", flush=True)
    print("  ВЫВОД: в загрузке и в контроле используются ТОЛЬКО значения с "
          "суффиксом utc. Значение без суффикса не используется нигде.", flush=True)
    return out


async def probe_calendar(client: Any, inst_id: str) -> dict[str, Any]:
    """§3 ТЗ: ФАКТИЧЕСКИЕ границы недели и месяца. Не предполагать — снять.

    Печатается, какой день недели соответствует началу недельного бара и какое
    число месяца — началу месячного. Эти значения идут в сборку (§7) из
    ЗАМЕРА, а не из текста задания.
    """
    print("\n=== §3. Фактические границы недели и месяца ===", flush=True)
    out: dict[str, Any] = {}
    for bar in ("1Wutc", "1Mutc"):
        body = await call(client, PATH_HISTORY_CANDLES,
                          {"instId": inst_id, "bar": bar, "limit": "12"})
        rows = body.get("data") or []
        if str(body.get("code")) != "0" or not rows:
            print(f"  {inst_id} {bar}: НЕ СНЯТО — код {body.get('code')}, "
                  f"{str(body.get('msg'))[:120]}", flush=True)
            out[bar] = None
            continue
        measured = measure_boundaries(bar, (to_dt(row[0]) for row in rows))
        out[bar] = measured
        if bar == "1Wutc":
            names = [WEEKDAY_NAMES[day] for day in measured["weekday_set"]]
            print(f"  {inst_id} 1Wutc: недельный бар начинается в {names} "
                  f"(по {measured['bars_measured']} барам)", flush=True)
        else:
            print(f"  {inst_id} 1Mutc: месячный бар начинается "
                  f"{measured['month_day_set']}-го числа "
                  f"(по {measured['bars_measured']} барам)", flush=True)
        print(f"    время суток начала бара (UTC): {measured['time_of_day_set']}",
              flush=True)
        await asyncio.sleep(0.3)
    return out


async def probe_limit(client: Any, path: str, base: dict[str, Any]) -> dict[str, Any]:
    """ФАКТИЧЕСКИЙ максимум ``limit``. Измеряется, а не берётся из документации.

    Проверяются ВСЕ кандидаты, и по каждому печатается «запрошено → получено»:
    на завышенный ``limit`` биржа отвечает не ошибкой, а МОЛЧА своим потолком,
    и увидеть потолок можно только по числу отданных строк. Замер 22.08.2026
    дал 300 при ожидавшихся по документации 100.
    """
    table: list[tuple[int, int, str]] = []
    best = 0
    for candidate in LIMIT_CANDIDATES:
        body = await call(client, path, {**base, "limit": str(candidate)})
        got = len(body.get("data") or [])
        table.append((candidate, got, str(body.get("code"))))
        best = max(best, got)
        await asyncio.sleep(0.3)
    return {"table": table, "max_rows": best}


async def probe_pace(client: Any, path: str, base: dict[str, Any]) -> dict[str, Any]:
    """ФАКТИЧЕСКАЯ безопасная пауза: темп наращивается до первого кода 50011.

    Ограничение частоты у биржи назначено на ЭНДПОИНТ, а не на инструмент,
    поэтому измеряется один раз на весь прогон, а не по каждой из двадцати
    пар «инструмент × масштаб»: двадцать замеров дали бы то же число ценой
    двухсот лишних запросов.
    """
    last_ok: int | None = None
    first_fail: int | None = None
    for pause_ms in PACE_CANDIDATES_MS:
        hit = False
        for _ in range(PACE_BURST):
            body = await call(client, path, {**base, "limit": "1"})
            if str(body.get("code")) == RATE_LIMIT_CODE or RATE_LIMIT_CODE in str(
                body.get("msg", "")
            ):
                hit = True
                break
            await asyncio.sleep(pause_ms / 1000.0)
        if hit:
            first_fail = pause_ms
            print(f"  темп: при паузе {pause_ms} мс получен {RATE_LIMIT_CODE} — предел",
                  flush=True)
            break
        last_ok = pause_ms
        print(f"  темп: пауза {pause_ms} мс — {PACE_BURST} запросов без 50011",
              flush=True)
        await asyncio.sleep(1.0)
    safe = (last_ok or PACE_CANDIDATES_MS[0]) * 2
    return {"last_ok_ms": last_ok, "first_fail_ms": first_fail, "safe_ms": safe}


async def probe_unconfirmed(client: Any, inst_id: str) -> None:
    """Показывает НЕЗАКРЫТЫЙ бар живьём — тот, который в хранилище не попадёт.

    ``/market/candles`` отдаёт формирующийся бар с ``confirm = 0``, тогда как
    ``/market/history-candles`` его не отдаёт вовсе. Разница важна для §8 ТЗ:
    если грузить только исторический эндпоинт, счётчик отброшенных незакрытых
    баров будет нулевым ПО ПОСТРОЕНИЮ, и проверка окажется слепой, а выглядеть
    будет как успех.
    """
    print("\n=== §8. Незакрытый бар у биржи: видно ли его вообще ===", flush=True)
    for path, name in ((PATH_CANDLES, "market/candles"),
                       (PATH_HISTORY_CANDLES, "market/history-candles")):
        body = await call(client, path, {"instId": inst_id, "bar": "1Dutc", "limit": "5"})
        rows = body.get("data") or []
        unconfirmed = [row for row in rows if len(row) >= 9 and str(row[8]) != "1"]
        print(f"  {name}: строк {len(rows)}, из них с confirm=0 — {len(unconfirmed)}",
              flush=True)
        for row in unconfirmed:
            print(f"    незакрытый бар {to_dt(row[0]).isoformat()} — в хранилище "
                  "не попадёт ни при каких условиях", flush=True)
        await asyncio.sleep(0.3)


async def walk_depth(
    client: Any, inst_id: str, bar: str, limit: int, pause: float
) -> dict[str, Any]:
    """Глубина одного ряда: самая ранняя метка, самая поздняя закрытая, число баров.

    Идёт назад страницами до пустого ответа. Ход печатается каждые
    ``PROGRESS_EVERY_PAGES`` страниц (D-10): обход часового ряда за четыре года
    занимает минуты, и без строк прогресса зонд неотличим от зависшего.

    ЧИСЛО БАРОВ СЧИТАЕТСЯ ПО ФАКТИЧЕСКИ ОТДАННЫМ СТРОКАМ, а рядом печатается
    «сколько их должно было быть» по длине периода. Разница этих двух чисел —
    и есть пропуски; выдавать расчётное число за фактическое нельзя.
    """
    cursor: int | None = None
    earliest: datetime | None = None
    latest: datetime | None = None
    rows_total = 0
    unconfirmed = 0
    pages = 0
    failure: str | None = None

    while pages < MAX_PAGES:
        params: dict[str, Any] = {"instId": inst_id, "bar": bar, "limit": str(limit)}
        if cursor is not None:
            params["after"] = str(cursor)
        body = await call(client, PATH_HISTORY_CANDLES, params)
        pages += 1
        if str(body.get("code")) != "0":
            failure = f"код {body.get('code')}: {str(body.get('msg'))[:120]}"
            print(f"    [{inst_id} {bar}] страница {pages}: ОТКАЗ — {failure}",
                  flush=True)
            break
        rows = body.get("data") or []
        if not rows:
            break
        rows_total += len(rows)
        unconfirmed += sum(1 for row in rows if len(row) >= 9 and str(row[8]) != "1")
        oldest_ms = min(int(row[0]) for row in rows)
        newest_ms = max(int(row[0]) for row in rows)
        earliest = to_dt(oldest_ms)
        latest = latest or to_dt(newest_ms)
        if pages % PROGRESS_EVERY_PAGES == 0:
            print(f"    [{inst_id} {bar}] страница {pages}: дошли до "
                  f"{earliest.isoformat()}, строк {rows_total}", flush=True)
        if cursor is not None and oldest_ms >= cursor:
            failure = "пагинация не движется"
            print(f"    [{inst_id} {bar}] страница {pages}: {failure} — останов",
                  flush=True)
            break
        cursor = oldest_ms
        await asyncio.sleep(pause)

    expected: int | None = None
    if earliest is not None and latest is not None:
        step = 3600 if bar == "1H" else period_seconds_hint(bar)
        expected = int((latest - earliest).total_seconds() // step) + 1
    return {
        "inst_id": inst_id, "bar": bar, "earliest": earliest, "latest_closed": latest,
        "bars": rows_total, "expected_bars": expected, "pages": pages,
        "unconfirmed_seen": unconfirmed, "failure": failure,
    }


def print_depth_table(rows: list[dict[str, Any]]) -> None:
    """Таблица глубины «инструмент × масштаб» — главный результат этапа (§2.5 ТЗ)."""
    print("\n" + "=" * 100, flush=True)
    print(" ТАБЛИЦА ФАКТИЧЕСКОЙ ГЛУБИНЫ: инструмент × масштаб", flush=True)
    print("=" * 100, flush=True)
    head = (f"{'инструмент':<11} {'масштаб':<8} {'самая ранняя':<22} "
            f"{'самая поздняя закрытая':<24} {'баров':>8} {'расч.':>8}  примечание")
    print(head, flush=True)
    print("-" * 100, flush=True)
    for row in rows:
        note = row["failure"] or ""
        if not note and row["expected_bars"] is not None:
            missing = row["expected_bars"] - row["bars"]
            note = "непрерывен" if missing == 0 else f"пропусков ≈ {missing}"
        print(
            f"{row['inst_id']:<11} {row['bar']:<8} "
            f"{(row['earliest'].isoformat() if row['earliest'] else '—'):<22} "
            f"{(row['latest_closed'].isoformat() if row['latest_closed'] else '—'):<24} "
            f"{row['bars']:>8} "
            f"{(row['expected_bars'] if row['expected_bars'] is not None else 0):>8}"
            f"  {note}",
            flush=True,
        )
    print("-" * 100, flush=True)
    print(" «баров» — фактически отданных биржей; «расч.» — сколько их было бы при "
          "непрерывном ряде.", flush=True)
    print(" НИ ОДИН ИНСТРУМЕНТ ПО ИТОГАМ ЗОНДА ИЗ СОСТАВА НЕ ВЫБРАСЫВАЕТСЯ: малая "
          "глубина — факт для Замера 1.", flush=True)


async def run(args: argparse.Namespace) -> int:
    instruments = [item.strip() for item in args.instruments.split(",") if item.strip()]
    bars = [item.strip() for item in args.bars.split(",") if item.strip()]
    if not instruments or not bars:
        print("Пустой список инструментов или масштабов", flush=True)
        return 2

    print("=" * 100, flush=True)
    print(" ЗОНД ГЛУБИНЫ СТАРШИХ ТАЙМФРЕЙМОВ — ЗАМЕР 0, шаг 1", flush=True)
    print("=" * 100, flush=True)
    print(f" Адрес API:        {OKX_BASE_URL}", flush=True)
    print(f" Время запуска:    {datetime.now(UTC).isoformat()} (UTC)", flush=True)
    print(f" Инструменты:      {instruments} (только СПОТ, контракты не участвуют)",
          flush=True)
    print(f" Масштабы:         {bars}", flush=True)
    print(f" Docker:           {compose_target()}", flush=True)
    print(" Скрипт НИЧЕГО не пишет в базу и не меняет конфигурацию.", flush=True)
    print("", flush=True)

    client = create_http_client()
    probe_rows = 0
    try:
        await probe_signature(client, instruments[0])
        await probe_timezone(client, instruments[0])
        await probe_calendar(client, instruments[0])
        await probe_unconfirmed(client, instruments[0])

        print("\n=== §4. Фактический максимум limit (измеряется, не берётся из "
              "документации) ===", flush=True)
        limits: dict[str, int] = {}
        for bar in bars:
            measured = await probe_limit(
                client, PATH_HISTORY_CANDLES, {"instId": instruments[0], "bar": bar}
            )
            limits[bar] = measured["max_rows"]
            for asked, got, code in measured["table"]:
                print(f"  {bar}: запрошено {asked:>4} → получено {got:>4} (код {code})",
                      flush=True)
            print(f"  {bar}: ФАКТИЧЕСКИЙ МАКСИМУМ = {measured['max_rows']}", flush=True)

        print("\n=== §4. Фактическая безопасная пауза между запросами ===", flush=True)
        pace = await probe_pace(
            client, PATH_HISTORY_CANDLES, {"instId": instruments[0], "bar": "1H"}
        )
        print(f"  без 50011 при паузе {pace['last_ok_ms']} мс; первый отказ при "
              f"{pace['first_fail_ms']} мс", flush=True)
        print(f"  БЕЗОПАСНОЕ значение с запасом ×2: BT_REQUEST_PAUSE_MS="
              f"{pace['safe_ms']}", flush=True)
        pause = pace["safe_ms"] / 1000.0

        print("\n=== §4. Обход глубины по каждой паре «инструмент × масштаб» ===",
              flush=True)
        depth: list[dict[str, Any]] = []
        for inst_id in instruments:
            for bar in bars:
                print(f"  → {inst_id} {bar}", flush=True)
                row = await walk_depth(
                    client, inst_id, bar, limits.get(bar) or 100, pause
                )
                probe_rows += row["bars"]
                depth.append(row)
                print(f"    итог: {row['bars']} баров, самая ранняя "
                      f"{row['earliest'].isoformat() if row['earliest'] else '—'}",
                      flush=True)
        print_depth_table(depth)
    finally:
        await client.aclose()

    print("", flush=True)
    print(f'  z0_probe_rows={probe_rows}', flush=True)
    print("", flush=True)
    print("ЗОНД ЗАВЕРШЁН УСПЕШНО. Перенесите таблицу выше в отчёт этапа и "
          "подставьте BT_REQUEST_PAUSE_MS в backtest/.env.backtest НА СЕРВЕРЕ "
          "(файла нет в репозитории).", flush=True)
    return 0 if probe_rows else 5


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Зонд глубины старших таймфреймов (Замер 0, шаг 1)"
    )
    parser.add_argument("--instruments", default=DEFAULT_INSTRUMENTS,
                        help="спотовые инструменты через запятую")
    parser.add_argument("--bars", default=",".join(DEFAULT_BARS),
                        help="масштабы через запятую; старшие — ТОЛЬКО с суффиксом utc")
    raise SystemExit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
