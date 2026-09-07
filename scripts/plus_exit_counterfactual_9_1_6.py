#!/usr/bin/env python3
"""ЭТАП 9.1.6: замер правила «без предела убытка, ожидание возврата в плюс».

НА КАКОЙ ВОПРОС ЭТО ОТВЕЧАЕТ, И НА КАКОЙ — НЕТ.

Владелец 06.09.2026 предложил заменить правило выхода: предела убытка нет;
сутки на достижение цели; если к суткам позиция в минусе — ждать возврата в
плюс ещё до суток; вернулась — закрыть в плюс; не вернулась за 48 часов —
закрыть по рынку.

ЭТАП 9.1.4 ИЗМЕРИЛ БЛИЗКИЙ, НО НЕ ТОТ ВАРИАНТ: «предел снят, закрытие по сроку
24 часа». Он ответил на вопрос «нужен ли предел», и ответ был неутешительный —
0 из 6 дошли до цели, средний итог −2.59% против −1.22%. Но вариант С
ОЖИДАНИЕМ ВОЗВРАТА В ПЛЮС не измерялся ни разу, и приписывать ему числа
9.1.4 нельзя: ожидание меняет и исход, и занятость слота.

ЗДЕСЬ НЕ ОТВЕЧАЕТСЯ на вопрос «а что было бы дальше с теми входами, которые не
состоялись бы, пока слот занят вторые сутки». Чтобы узнать их итог, пришлось бы
проиграть целиком ДРУГУЮ историю позиций, где каждый вход меняет занятость
следующих. Это другой этап; здесь заблокированные входы только СЧИТАЮТСЯ, и
считаются ВЕРХНЕЙ ГРАНИЦЕЙ (§6 ТЗ, ЧИСЛО 5).

ПЯТЬ ВАРИАНТОВ, А НЕ ДВА. Владелец просит ДВЕ ПЕРЕМЕНЫ СРАЗУ — снять предел и
ждать плюса до 48 часов, — и разницу между «как есть» и «как предложено» нельзя
приписать ни одной из них по отдельности. Поэтому считаются:

  control                — фактическое правило: предел −1%, срок 24 часа;
  A_nostop_24            — повторение варианта ``no_stop`` Этапа 9.1.4 на новой,
                           большей выборке (снята только первая перемена);
  B_nostop_plus_48       — правило, предложенное владельцем (обе перемены);
  C_stop_plus_48         — предел ОСТАВЛЕН, ожидание есть (только вторая
                           перемена). Он и отделяет вторую от первой;
  D_nostop_plus_48_gross — то же, что B, но «плюс» ВАЛОВОЙ (Pbe = P0). Нужен
                           ровно для одного: показать, сколько сделок разделяет
                           ставка круговых издержек.

ПРАВИЛО ВЫХОДА ЗДЕСЬ НЕ ПЕРЕПИСАНО НИ ОДНОЙ СТРОКОЙ (§1.2 ТЗ). Все пять
вариантов и обе фазы каждого считаются вызовами ``src/positions/rules.check_exit``
— ровно той функции, которой закрыты настоящие позиции, — с разными
``stop_price``, ``target_price`` и ``deadline_at``. Своё правило писать было
нельзя: тогда контроль перестал бы быть контролем, потому что сверял бы факт не
с тем расчётом, которым считаются варианты.

КАК ОЖИДАНИЕ ПЛЮСА ВЫРАЖЕНО ЧЕРЕЗ ``check_exit``, И ПОЧЕМУ ЭТО НЕ ФОКУС.
Правило §3.4–3.7 ТЗ на отрезке [t0+24ч, t0+48ч] проверяет на каждом баре два
условия: ``high >= T`` (цель) и ``high >= Pbe`` (плюс), и при одновременной
выполнимости выбирает цель. Но ``high >= X`` — это ровно условие касания ЦЕЛИ в
``check_exit``. Значит фаза ожидания считается ДВУМЯ вызовами того же правила по
тому же ряду: один с ``target_price = T``, другой с ``target_price = Pbe``, оба
без предела. Первый называет бар цели, второй — бар плюса; сравнение двух меток
и даёт правило §3.7 «при совпадении — цель». Третьего исхода у пары нет: если
ни один из вызовов не задел уровень, оба доходят до срока t0+48ч и дают
``timeout`` по закрытию последнего бара, открывшегося раньше срока, — то есть
ровно то, что требует §3.5 ТЗ («тем же способом, каким боевой сервис берёт цену
на 24-часовой отметке»).

ЦЕНА ВЫХОДА ПРИ ``plus_exit`` — ЦЕНА БЕЗУБЫТКА, А НЕ ЦЕНА ЗАКРЫТИЯ БАРА (§3.4
ТЗ). Разница не косметическая: закрытие бара, на котором цена задела безубыток,
может быть и выше, и НИЖЕ его, и подстановка закрытия превратила бы «выход в
ноль» в лотерею. Следствие проверяемое: итог сделки ``plus_exit`` равен НУЛЮ по
построению (у варианта D — ровно минус издержкам, там «плюс» валовой), и расчёт
роняется при нарушении (:func:`assert_plus_exit_is_flat`).

ЧТО ТАКОЕ «ПЛЮС», ВЗЯТО ИЗ КОДА, А НЕ ИЗ ТЕКСТА ЗАДАНИЯ (§3.6 ТЗ прямо этого
требует). Ставка круговых издержек — ``positions.cost_pct`` КАЖДОЙ ПОЗИЦИИ,
то есть то, чем итог этой позиции посчитан на самом деле; фактическое значение
печатается отдельной строкой в выводе. Подставить сюда константу настроек
сегодняшнего дня значило бы считать «плюс» по ставке, по которой сделка не
велась.

КОНТРОЛЬ БЛОКИРУЮЩИЙ (§9.1 ТЗ). Вариант ``control`` обязан воспроизвести
записанное в ``positions`` — причину, момент закрытия, цену и итог. Не
воспроизвёл хотя бы по одной позиции — СРАВНЕНИЕ ВАРИАНТОВ НЕ ПУБЛИКУЕТСЯ
ВОВСЕ, код возврата 2, таблица не пишется. Если расчёт не умеет повторить УЖЕ
СЛУЧИВШЕЕСЯ, его числа по НЕ случившимся вариантам не стоят ничего.

ЭТАП ЗАМЕРНЫЙ, И ГРАНИЦА ЖЁСТКАЯ (§1 ТЗ). Ни одно правило системы не меняется:
LOGIC_VERSION не поднимается, ``BARRIER_STOP_PCT`` не трогается, боевое правило
выхода остаётся прежним. РЕКОМЕНДАЦИЯ ВНЕДРИТЬ ВАРИАНТ ЭТИМ ЭТАПОМ ЗАПРЕЩЕНА
и скриптом не печатается ни в каком виде: выбор делает владелец по числам.
Пишется ровно одна таблица — ``position_plus_exit_shadow``.

КОДЫ ВОЗВРАТА (§8.3 ТЗ):
  0 — расчёт дошёл до конца (в том числе когда выборка мала и об этом сказано);
  2 — сработала защита: не совпал контроль, обнаружено подглядывание вперёд,
      нарушена посылка расчёта, превышен потолок памяти, нет таблицы при
      ``--apply``. Ни одной строки при этом не пишется;
  3 — данных не хватило: после отбраковки не осталось ни одной позиции.

ЗАПУСК ВНУТРИ КОНТЕЙНЕРА. Каталог ``scripts/`` попадает только в образ
``backtest``, поэтому запускать через него:

    # 1. вхолостую — ни одной записи в базу:
    docker compose --profile tools run --rm --no-deps \\
        backtest python scripts/plus_exit_counterfactual_9_1_6.py

    # 2. если контроль совпал (код 0) — с записью:
    docker compose --profile tools run --rm --no-deps \\
        backtest python scripts/plus_exit_counterfactual_9_1_6.py --apply

Отдельным cron НЕ оформляется: это разовый замер, а не ночной расчёт.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402
import structlog  # noqa: E402

# ПЕРЕИСПОЛЬЗУЕТСЯ, А НЕ ПЕРЕПИСЫВАЕТСЯ (§2.1 ТЗ). ``assert_no_stop`` и
# ``NO_STOP_PRICE`` — это доказанная на Этапе 9.1.4 эквивалентность «предел
# отключён» ≡ ``stop_price = 0`` вместе с её единственной посылкой. Вторая
# копия того же рассуждения однажды разошлась бы с первой, и разошлась бы
# молча; ``peak_rss_mb`` — та же мерка памяти, что печатал 9.1.4.
from scripts.stop_counterfactual_9_1_4 import (  # noqa: E402
    NO_STOP_PRICE,
    assert_no_stop,
    peak_rss_mb,
)
from src.barrier.outcomes import BAR_SECONDS, OUTCOME_NO_DATA  # noqa: E402
from src.barrier.runner import settle_seconds  # noqa: E402
from src.core.config import settings  # noqa: E402
from src.core.db import db  # noqa: E402
from src.core.logging import setup_logging  # noqa: E402
from src.positions import rules as position_rules  # noqa: E402
from src.shadow.trailing import bar_step, closing_moment  # noqa: E402

_log = structlog.get_logger().bind(component="plus_exit_counterfactual")

# --- Границы правила ---------------------------------------------------------
#
# ПЕРВЫЕ СУТКИ И ВТОРЫЕ. §3.3 ТЗ: на [t0, t0+24ч] других выходов, кроме цели и
# предела, нет. §3.4: с t0+24ч ВКЛЮЧИТЕЛЬНО проверяется условие плюса. §3.1:
# цель активна на всём [t0, t0+48ч].
WAIT_FIRST_HOURS = 24
TOTAL_HOURS = 48

# --- Имена вариантов (§4 ТЗ). Перечень закрыт и повторён ограничением БД -----
CONTROL_VARIANT = "control"
A_NOSTOP_24 = "A_nostop_24"
B_NOSTOP_PLUS_48 = "B_nostop_plus_48"
C_STOP_PLUS_48 = "C_stop_plus_48"
D_NOSTOP_PLUS_48_GROSS = "D_nostop_plus_48_gross"


@dataclass(frozen=True)
class VariantSpec:
    """Вариант — это ТРИ свойства сразу, а не одно.

    ``keeps_stop`` — проверяется ли предел убытка на первых сутках;
    ``waits_plus`` — ждать ли возврата в плюс вторые сутки вместо закрытия по
    рынку на 24 часах;
    ``gross_plus`` — считать ли «плюс» ВАЛОВЫМ (``Pbe = P0``) вместо чистого.

    Разложить их по отдельным колонкам таблицы значило бы разрешить сочетания,
    которых этап не считал.
    """

    name: str
    keeps_stop: bool
    waits_plus: bool
    gross_plus: bool


# ПОЧЕМУ У ВАРИАНТА C ПРЕДЕЛ ДЕЙСТВУЕТ ТОЛЬКО ПЕРВЫЕ СУТКИ. Так сказано в §4 ТЗ
# дословно: «Если предел сработал — сделка закрыта и ожидания нет; выжившие к 24
# часам идут по правилу 3.4–3.6», — а в правиле 3.4–3.6 предела нет вовсе
# (§3.2). Это и делает C чистым отделением второй перемены от первой: до 24
# часов C совпадает с ``control`` бар в бар, после — ведёт себя как B. Читать
# «предел оставлен» как «предел действует все 48 часов» значило бы смешать в
# одном варианте обе перемены сразу — то, ради разделения чего C и заведён.
VARIANTS: tuple[VariantSpec, ...] = (
    VariantSpec(CONTROL_VARIANT, keeps_stop=True, waits_plus=False, gross_plus=False),
    VariantSpec(A_NOSTOP_24, keeps_stop=False, waits_plus=False, gross_plus=False),
    VariantSpec(B_NOSTOP_PLUS_48, keeps_stop=False, waits_plus=True, gross_plus=False),
    VariantSpec(C_STOP_PLUS_48, keeps_stop=True, waits_plus=True, gross_plus=False),
    VariantSpec(
        D_NOSTOP_PLUS_48_GROSS, keeps_stop=False, waits_plus=True, gross_plus=True
    ),
)
VARIANTS_BY_NAME: dict[str, VariantSpec] = {spec.name: spec for spec in VARIANTS}


def variant_names() -> tuple[str, ...]:
    """Пять вариантов в порядке печати."""
    return tuple(spec.name for spec in VARIANTS)


# --- Исходы ------------------------------------------------------------------
#
# ПРАВИЛО ВОЗВРАЩАЕТ ОДНО ИМЯ 'timeout' ДЛЯ ДВУХ РАЗНЫХ СОБЫТИЙ, и здесь они
# разделены: «закрыт по рынку через сутки» и «прождал вторые сутки и всё равно
# закрыт по рынку» — разные сделки с разной занятостью слота. Разделение живёт
# ТОЛЬКО в замере; сверка контроля сводит 'timeout_24' обратно к записанному
# 'timeout' (:func:`control_reason_of_fact`).
EXIT_PLUS = "plus_exit"
EXIT_TIMEOUT_24 = "timeout_24"
EXIT_TIMEOUT_48 = "timeout_48"

# Исход, которого НЕ БЫВАЕТ у правила: признание того, что исход не измерен.
# ``check_exit`` возвращает None, когда бар срока не предъявлен и ни один
# уровень не задет. Имя то же, что в ``src/barrier/outcomes`` и в миграциях 021
# и 022, — второго словаря для одного и того же состояния не заводится.
UNMEASURED = OUTCOME_NO_DATA

SHADOW_REASONS: tuple[str, ...] = (
    position_rules.EXIT_TARGET,
    position_rules.EXIT_STOP,
    position_rules.EXIT_AMBIGUOUS,
    EXIT_PLUS,
    EXIT_TIMEOUT_24,
    EXIT_TIMEOUT_48,
    UNMEASURED,
)

# --- Пороги и мерки ----------------------------------------------------------
#
# НИЖЕ ЭТОГО ЧИСЛА ВЫВОД О РАЗЛИЧИИ ВАРИАНТОВ НЕ ДЕЛАЕТСЯ ВООБЩЕ (§5.5 ТЗ), и
# скрипт обязан сказать это ВСЛУХ, а не подразумевать. Молчаливая выдача
# таблицы на пяти сделках — то, чем закончился бы этап, если бы порог был
# «на глаз».
MIN_SAMPLE = 15

# Доверительный интервал среднего (§6.2 ТЗ). Число повторов задано ТЗ; зерно
# закреплено, потому что §7.5 требует, чтобы повторный прогон не менял ЗНАЧЕНИЙ,
# а плавающее зерно меняло бы границы интервала каждым запуском.
BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_SEED = 20260906
CONFIDENCE = 0.95

# ПОТОЛОК ПАМЯТИ (§8.4 ТЗ). На Этапе 9.1.3 часть А не досчиталась именно
# из-за памяти: ядро убило процесс, и это выглядело как «расчёт не вернул
# чисел», а не как «расчёт превысил потолок».
MEMORY_CAP_MB = 1024.0

# ТОЧНОСТЬ СВЕРКИ КОНТРОЛЯ (§9.1 ТЗ) — И ЧЕСТНОЕ ОБЪЯСНЕНИЕ, ПОЧЕМУ ЗНАКОВ ДВА
# ЧИСЛА, А НЕ ОДНО.
#
# §9.1 ТЗ требует допуск по итогу в восемь знаков. Взять его буквально —
# сравнить пересчитанный ``float`` с записанным числом до 1e-8 — НЕЛЬЗЯ, и не
# из-за строгости, а из-за того, что сравнивались бы разные величины: колонка
# ``positions.net_pnl_pct`` имеет тип NUMERIC(12,6) и хранит ШЕСТЬ знаков после
# запятой. Итог −1.2200004567 лежит в базе как −1.220000, и «расхождение»
# в восьмом знаке было бы не ошибкой расчёта, а ОКРУГЛЕНИЕМ ХРАНЕНИЯ — тем же
# самым видом ложного расхождения, что стоило Этапу 9.1.3 одиннадцати
# несовпадений по 60 секунд.
#
# Поэтому пересчитанное значение сперва приводится к точности ХРАНЕНИЯ колонки,
# и уже приведённое сверяется с записанным С ДОПУСКОМ 1e-8 — то есть ровно с
# восемью знаками §9.1. Допуск не расширен ни на один знак: он поставлен туда,
# на что отвечает, а приведение отвечает на вопрос «что с чем сравнивается».
# ПРАВКА ЭТАПА 9.2 §2.2: точность хранения цены и всё, что от неё зависит,
# теперь живёт в БОЕВОМ модуле правил, а сюда ввозится. Значение то же —
# positions.exit_price — NUMERIC(20,8), — но определение у него ОДНО.
PRICE_STORAGE_PLACES = position_rules.PRICE_STORAGE_PLACES
PCT_STORAGE_PLACES = 6  # positions.net_pnl_pct — NUMERIC(12,6)
CONTROL_TOLERANCE = 1e-8

SECONDS_IN_HOUR = 3600.0


# ОКРУГЛЕНИЕ ХРАНЕНИЯ ПЕРЕЕХАЛО В БОЕВОЙ КОД (Этап 9.2 §2.2), И ЭТО ВАЖНЕЕ,
# ЧЕМ КАЖЕТСЯ. Правка §А от 06.09.2026 установила воспроизведением: живой сервис
# при закрытии позиции берёт цель НЕ ИЗ ФОРМУЛЫ, а из строки, где она уже
# округлена колонкой NUMERIC(20,8); пересчёт же считал её в ``float`` и на
# монете по 0.08 доллара расходился с фактом в шестом знаке ``net_pnl_pct``.
# Лечится это одинаковым округлением — и способ округления взят из БАЗЫ:
# PostgreSQL округляет NUMERIC половиной ОТ НУЛЯ, а ``round()`` Python —
# половиной к чётному, то есть расходится с базой ровно на половинах.
#
# Пока правило было замерным, копия жила здесь. Этап 9.2 вводит его в бой, и
# ЦЕНА БЕЗУБЫТКА теперь считается боевым сервисом; две копии одной и той же
# формулы с одним и тем же округлением однажды разошлись бы молча. Поэтому
# определения переехали в ``src/positions/rules.py``, а здесь остались ИМЕНА,
# ссылающиеся ровно на них: замер и бой считают одной функцией, а не двумя
# похожими.
to_storage = position_rules.to_storage
storage_tick = position_rules.storage_tick

# Порог «плоскости» выхода в безубыток. Величина сравнивается с нулём после
# арифметики на числах порядка 10^5 (цена) — точность double здесь около 1e-11
# относительно, и 1e-9 в процентах на порядки выше любого настоящего
# расхождения, но на порядки ниже цены одного тика.
FLAT_EPS = 1e-9


# =============================================================================
# ЧИСТЫЕ ФУНКЦИИ ПРАВИЛА. Ни базы, ни сети, ни ``datetime.now()`` внутри: всё
# состояние приходит параметрами — это условие проверяемости на придуманных
# рядах с заранее известным ответом.
# =============================================================================


# ЦЕНА БЕЗУБЫТКА — ТОЖЕ ОДНА ФУНКЦИЯ НА ЗАМЕР И НА БОЙ (Этап 9.2 §2.2).
# ``Pbe = P0 × (1 + C)`` для покупки, ``P0 × (1 − C)`` для продажи, при
# ``gross=True`` — сама цена входа (вариант D §4 ТЗ 9.1.6). Округление —
# то же ``to_storage``. Разбор, почему округлять здесь обязательно, стоит в
# докстринге самой функции.
breakeven_price = position_rules.breakeven_price


def phase_deadlines(
    opened_at: datetime, fact_deadline_at: datetime
) -> tuple[datetime, datetime]:
    """Границы двух фаз: ``(конец первых суток, конец вторых)``.

    КОНЕЦ ПЕРВЫХ СУТОК БЕРЁТСЯ ИЗ САМОЙ ПОЗИЦИИ, а не считается заново от
    ``opened_at``. Причина простая: контроль обязан воспроизвести ФАКТ, а факт
    закрыт по ``positions.deadline_at``. Свой пересчёт срока разошёлся бы с
    записанным на любой позиции, у которой горизонт отличается от суток, — и
    разошёлся бы молча, потому что оба числа выглядят одинаково правдоподобно.

    Что взятый из позиции срок действительно равен t0+24ч, проверяет
    :func:`assert_phase_boundary`, и она роняет расчёт при расхождении.
    """
    return fact_deadline_at, opened_at + timedelta(hours=TOTAL_HOURS)


def assert_phase_boundary(
    opened_at: datetime, deadline_24: datetime, deadline_48: datetime
) -> None:
    """ПОСЫЛКА РАСЧЁТА: граница фаз стоит ровно на 24 часах, а срок — на 48.

    Проверяется, а не предполагается. Смещение границы хотя бы на один бар
    переносит бары из фазы, где условие плюса НЕ проверяется, в фазу, где оно
    проверяется, и наоборот, — то есть меняет исход, оставляя числа
    правдоподобными. Позиция с горизонтом не в сутки роняет расчёт здесь же:
    правило §3 ТЗ описано для суток, и посчитать по нему шестичасовую позицию
    значило бы ответить на вопрос, которого никто не задавал.
    """
    expected_24 = opened_at + timedelta(hours=WAIT_FIRST_HOURS)
    if deadline_24 != expected_24:
        raise ValueError(
            "граница первых суток не совпадает с t0+"
            f"{WAIT_FIRST_HOURS}ч: срок позиции {deadline_24.isoformat()}, "
            f"ожидалось {expected_24.isoformat()}"
        )
    expected_48 = opened_at + timedelta(hours=TOTAL_HOURS)
    if deadline_48 != expected_48:
        raise ValueError(
            f"срок ожидания не совпадает с t0+{TOTAL_HOURS}ч: "
            f"{deadline_48.isoformat()}, ожидалось {expected_48.isoformat()}"
        )


def bar_is_closed(bar_ts: datetime, moment: datetime, resolution: str) -> bool:
    """Закрылся ли бар СТРОГО РАНЬШЕ момента (§5.4 ТЗ).

    ДЛИНА БАРА БЕРЁТСЯ ИЗ КОНСТАНТЫ ДЛИНЫ БАРА (``BAR_SECONDS`` через
    ``src.shadow.trailing.bar_step``), А НЕ ИЗ ``settle_seconds()`` — это прямой
    УРОК ЭТАПА 9.1.5. ``settle_seconds()`` отвечает на другой вопрос: «перестал
    ли коллектор перезаписывать этот бар», и построена на длине ГРУБОГО бара
    (час) плюс запас, то есть равна 3900 секундам. Поставленная на границу окна,
    она обезвредила бы проверку: любой бар в пределах часа с лишним до границы
    объявлялся бы незакрытым, и запрет на подглядывание перестал бы что-либо
    запрещать ровно там, где он нужен.
    """
    return bar_ts + bar_step(resolution) <= moment


def closed_bars(
    bars: list[position_rules.Bar], now: datetime, resolution: str
) -> list[position_rules.Bar]:
    """Только те бары, что закрылись строго раньше ``now``. Отбор, не проверка."""
    return [bar for bar in bars if bar_is_closed(bar.ts, now, resolution)]


def assert_closed_bars(
    bars: list[position_rules.Bar], now: datetime, resolution: str
) -> None:
    """ЗАПРЕТ НА ПОДГЛЯДЫВАНИЕ, ВЫРАЖЕННЫЙ ОТДЕЛЬНО ОТ ОТБОРА.

    ВЕЛИЧИНА ЗДЕСЬ ПОСЧИТАНА ЗАНОВО, А НЕ ВЗЯТА У :func:`bar_is_closed`, И ЭТО
    НЕ НЕДОСМОТР. Отбор, который проверяет сам себя, проверяет ровно то, что
    делает, и ломается вместе с собой: испорти условие отбора — и «проверка»
    подтвердит испорченное, потому что спрашивает у него же. Второе, отдельно
    написанное сравнение стоит одной строки и ловит порчу первого — тот же приём, каким
    ограничения миграции повторяют утверждения кода.
    """
    step = timedelta(seconds=BAR_SECONDS[resolution])
    for bar in bars:
        if bar.ts + step > now:
            raise ValueError(
                "в расчёт попал НЕЗАКРЫТЫЙ бар — это подглядывание вперёд: "
                f"ts={bar.ts.isoformat()}, now={now.isoformat()}, "
                f"длина бара {BAR_SECONDS[resolution]} с"
            )


def covers_full_window(opened_at: datetime, now: datetime, resolution: str) -> bool:
    """Покрыт ли отрезок [t0, t0+48ч] ЦЕЛИКОМ, с запасом закрытия бара (§5.2 ТЗ).

    Правилу нужен ПРЕДЪЯВЛЕННЫЙ бар срока: ``check_exit`` объявляет ``timeout``
    только тогда, когда бар с меткой ``deadline_at`` уже показан, — иначе
    неизвестно, кончилось окно или данные не подъехали. Значит окно покрыто, лишь
    когда закрылся сам бар t0+48ч.

    ЭТО ГЛАВНЫЙ ОГРАНИЧИТЕЛЬ ВЫБОРКИ (§5.3 ТЗ): все позиции, открытые позже чем
    48 часов назад, не покрыты и в замер не идут. Их число печатается явно —
    выборка, из которой что-то молча выпало, неотличима от выборки, в которой
    этого не было.
    """
    return bar_is_closed(
        opened_at + timedelta(hours=TOTAL_HOURS), now, resolution
    )


def assert_sample_covered(
    opened_at: datetime, now: datetime, resolution: str
) -> None:
    """Та же величина, что в :func:`covers_full_window`, но ПРОВЕРКОЙ и ЗАНОВО.

    Порознь и с заново написанным сравнением — по той же причине, что и в
    :func:`assert_closed_bars`: снятая отбраковка обязана уронить НЕЗАВИСИМУЮ
    проверку, а не спросить разрешения у той же испорченной функции и пройти
    молча.
    """
    end = opened_at + timedelta(hours=TOTAL_HOURS)
    if end + timedelta(seconds=BAR_SECONDS[resolution]) > now:
        raise ValueError(
            "ряд баров не покрывает t0+"
            f"{TOTAL_HOURS}ч с запасом закрытия бара: открытие "
            f"{opened_at.isoformat()}, now={now.isoformat()}"
        )


@dataclass(frozen=True)
class PlusOutcome:
    """Исход одной позиции при одном варианте — строка таблицы замера.

    ``closed_at`` — момент ЗАКРЫТИЯ бара выхода, то есть то, что живой сервис
    пишет в ``positions.closed_at``, а не время открытия бара, которое
    возвращает правило. Разница ровно в один бар, и на Этапе 9.1.3 она стоила
    одиннадцати ложных расхождений (см. ``src.shadow.trailing.closing_moment``).
    """

    variant: str
    exit_reason: str
    exit_bar_ts: datetime | None
    closed_at: datetime | None
    exit_price: float | None
    net_pnl_pct: float | None
    net_pnl_usd: float | None
    held_hours: float | None
    bars_used: int
    resolution: str

    @property
    def measured(self) -> bool:
        """Исход измерен, а не признан неизмеримым."""
        return self.exit_reason != UNMEASURED


def _unmeasured(variant: str, resolution: str, bars_used: int) -> PlusOutcome:
    """Строка «исход НЕ ИЗМЕРЕН»: ни одного числа, которое можно принять за замер."""
    return PlusOutcome(
        variant=variant,
        exit_reason=UNMEASURED,
        exit_bar_ts=None,
        closed_at=None,
        exit_price=None,
        net_pnl_pct=None,
        net_pnl_usd=None,
        held_hours=None,
        bars_used=bars_used,
        resolution=resolution,
    )


def flat_tolerance(entry_price: float) -> float:
    """Насколько итог выхода в безубыток вправе отличаться от нуля.

    ПРАВКА §А. Цена безубытка теперь ПРИВОДИТСЯ К ТОЧНОСТИ ХРАНЕНИЯ, и потому
    отличается от точной цены безубытка не больше чем на ПОЛОВИНУ ТИКА. В
    процентах это ``100 × полтика / цена входа``: у монеты по 0.08 доллара —
    около 6e-6 процента, у BTC — около 6e-12. Требовать здесь ноль значило бы
    требовать цены, которой не бывает в базе; допускать больше — перестать
    ловить подмену.

    Величина СЧИТАЕТСЯ ОТ ЦЕНЫ ВХОДА, а не берётся константой: константа,
    подобранная по BTC, пропустила бы DOGE, а подобранная по DOGE — не поймала
    бы на BTC ничего.
    """
    half_tick = 0.5 * storage_tick(PRICE_STORAGE_PLACES)
    return 100.0 * half_tick / float(entry_price) + FLAT_EPS


def assert_plus_exit_is_flat(
    outcome: PlusOutcome, *, entry_price: float, cost_pct: float, gross: bool
) -> None:
    """СЛЕДСТВИЕ ПРАВИЛА, ПРОВЕРЯЕМОЕ НА КАЖДОЙ СДЕЛКЕ (§3.6 ТЗ).

    Выход по цене безубытка даёт итог НОЛЬ — так эта цена и определена; с
    точностью до половины тика хранения цены (см. :func:`flat_tolerance`).
    У варианта D «плюс» валовой, и итог там равен ровно минус издержкам: цена
    входа уже лежит в точности хранения, и округлять её нечего.

    Проверка ловит сразу две подмены, каждая из которых оставляет числа
    правдоподобными: цену выхода, взятую по закрытию бара вместо цены
    безубытка, и валовой «плюс», подставленный там, где обещан чистый.
    """
    if outcome.exit_reason != EXIT_PLUS or outcome.net_pnl_pct is None:
        return
    expected = -float(cost_pct) if gross else 0.0
    limit = FLAT_EPS if gross else flat_tolerance(entry_price)
    if abs(float(outcome.net_pnl_pct) - expected) > limit:
        raise ValueError(
            f"выход в безубыток у варианта {outcome.variant} дал итог "
            f"{outcome.net_pnl_pct:.10f}%, а обязан был дать {expected:.10f}% "
            f"с точностью {limit:.3e}: цена выхода не равна цене безубытка"
        )


def _finish(
    *,
    variant: str,
    exit_reason: str,
    exit_bar_ts: datetime,
    exit_price: float,
    entry_price: float,
    cost_pct: float,
    notional_usd: float,
    opened_at: datetime,
    resolution: str,
    bars_used: int,
) -> PlusOutcome:
    """Общий хвост всех исходов: итог, момент закрытия, занятость слота.

    Итог считается ТОЙ ЖЕ ``rules.net_pnl``, которой посчитан итог настоящей
    позиции, и издержки в ней вычитаются ровно один раз.
    """
    net_pct = position_rules.net_pnl(entry_price, exit_price, cost_pct)
    closed_at = closing_moment(exit_bar_ts, resolution)
    return PlusOutcome(
        variant=variant,
        exit_reason=exit_reason,
        exit_bar_ts=exit_bar_ts,
        closed_at=closed_at,
        exit_price=float(exit_price),
        net_pnl_pct=net_pct,
        # Итог в долларах — от ФАКТИЧЕСКОГО слота позиции, а не от константы
        # этапа: слот мог отличаться, и константа превратила бы замер в оценку.
        # Размер слота печатается отдельной строкой, чтобы «при слоте $2» из
        # §6 ТЗ читалось как проверяемое утверждение, а не как допущение.
        net_pnl_usd=float(notional_usd) * net_pct / 100.0,
        held_hours=(closed_at - opened_at).total_seconds() / SECONDS_IN_HOUR,
        bars_used=bars_used,
        resolution=resolution,
    )


def assert_levels_match_row(
    *,
    entry_price: float,
    target_pct: float,
    stop_pct: float,
    fact_target_price: float,
    fact_stop_price: float,
) -> None:
    """ТА ЖЕ ``rules.levels`` — но ПРОВЕРКОЙ, а не источником (§А 2.4, §9.2 ТЗ).

    ПОЧЕМУ УРОВНИ БЕРУТСЯ ИЗ СТРОКИ ПОЗИЦИИ, А НЕ СЧИТАЮТСЯ ЗАНОВО. Живой
    сервис при закрытии передаёт в ``check_exit`` именно
    ``float(row["target_price"])`` и ``float(row["stop_price"])``
    (``src/positions/runner.py``) — числа, уже округлённые колонкой
    NUMERIC(20,8). Это САМЫЙ ТОЧНЫЙ способ повторить случившееся, и §А 2.4 его
    прямо предпочитает.

    ПОЧЕМУ ПЕРЕСЧЁТ ЧЕРЕЗ ``levels`` ВСЁ РАВНО ДЕЛАЕТСЯ. Взять число из строки
    и не спросить, откуда оно, значило бы принять на веру, что позицию открыло
    ТО ЖЕ правило, которое лежит в репозитории. Здесь это утверждение
    проверяется.

    ПОЧЕМУ ДОПУСК — ОДИН ТИК ХРАНЕНИЯ, А НЕ НОЛЬ, И ПОЧЕМУ ЭТО НЕ ПОБЛАЖКА.
    При ОТКРЫТИИ позиции ``levels`` считалась от ``float(bar["close"])``, а
    ``ohlcv.close`` имеет тип DOUBLE PRECISION — полная точность double. В
    ``positions.entry_price`` то же число легло уже округлённым до восьми
    знаков (NUMERIC(20,8)). Пересчёт видит только округлённую цену входа,
    поэтому обязан отличаться, и отличие ОГРАНИЧЕНО СВЕРХУ: сдвиг цены входа не
    больше половины тика, множитель ``(1 ± pct/100)`` близок к единице, значит
    сдвиг уровня не больше одного тика после округления. Разница БОЛЬШЕ тика
    объяснения не имеет и означает, что уровень посчитан другим правилом, — и
    тогда расчёт падает. Допуск сверки контроля (1e-8) при этом не тронут: это
    другая величина и другой вопрос.
    """
    tick = storage_tick(PRICE_STORAGE_PLACES)
    computed_target, computed_stop = position_rules.levels(
        entry_price, target_pct, stop_pct
    )
    for name, computed, fact in (
        ("цели", computed_target, fact_target_price),
        ("предела", computed_stop, fact_stop_price),
    ):
        stored = to_storage(computed, PRICE_STORAGE_PLACES)
        if abs(stored - float(fact)) > tick + CONTROL_TOLERANCE:
            raise ValueError(
                f"уровень {name} в строке позиции ({fact}) расходится с "
                f"пересчётом той же rules.levels ({stored}) больше чем на один "
                f"тик хранения ({tick}): уровень посчитан не тем правилом"
            )


def resolve_variant(
    bars: list[position_rules.Bar],
    *,
    spec: VariantSpec,
    entry_price: float,
    target_pct: float,
    stop_pct: float,
    target_price: float,
    fact_stop_price: float,
    cost_pct: float,
    notional_usd: float,
    side: str,
    opened_at: datetime,
    deadline_24: datetime,
    deadline_48: datetime,
    resolution: str,
) -> PlusOutcome:
    """Исход одной позиции при одном варианте. ЧИСТАЯ функция.

    ``bars`` — ЗАКРЫТЫЕ бары окна по возрастанию времени открытия. Что такое
    «закрытый», знает только вызывающий, и правило об этом не спрашивает.

    ДВЕ ФАЗЫ, И ОБЕ — ВЫЗОВЫ ``check_exit``:

     1. [t0, t0+24ч): цель и (если вариант его хранит) предел. Всё, что не
        ``timeout``, — окончательный исход. ``timeout`` у варианта без ожидания
        становится ``timeout_24``, у варианта с ожиданием — входом во вторую
        фазу.
     2. [t0+24ч, t0+48ч]: ДВА вызова того же ``check_exit`` по тому же ряду —
        один с уровнем цели, другой с уровнем безубытка, оба без предела.
        Первый называет бар цели, второй — бар плюса; §3.7 ТЗ («при
        одновременной выполнимости — цель») выражается сравнением двух меток.
    """
    if side != position_rules.SIDE_BUY:
        # Спот: позиции только на покупку (ограничение positions_side_chk).
        # Продажа сюда попасть не может, и посчитать её «как-нибудь» нельзя:
        # и цель, и предел, и цена безубытка у продажи лежат по другую сторону
        # цены входа, а ``check_exit`` написан для покупки.
        raise ValueError(f"позиции ведутся только на покупку, получено: {side}")

    # УРОВНИ ЦЕЛИ И ПРЕДЕЛА ПРИХОДЯТ ИЗ СТРОКИ ПОЗИЦИИ — ровно те числа, что
    # живой сервис передаёт в ``check_exit`` при закрытии, уже округлённые
    # колонкой NUMERIC(20,8) (§А 2.4). Что они посчитаны той же
    # ``rules.levels``, проверяется здесь же, НА ПУТИ РАСЧЁТА КАЖДОГО ВАРИАНТА,
    # а не рядом с ним: проверка, стоящая в стороне от расчёта, однажды
    # перестаёт вызываться и об этом никто не узнаёт.
    assert_levels_match_row(
        entry_price=entry_price,
        target_pct=target_pct,
        stop_pct=stop_pct,
        fact_target_price=target_price,
        fact_stop_price=fact_stop_price,
    )
    # Отключается предел не уровнем, а ценой предела: ``levels`` отвергает
    # ``stop_pct <= 0`` — и правильно делает (см. NO_STOP_PRICE и разбор в
    # скрипте 9.1.4).
    stop_price = float(fact_stop_price) if spec.keeps_stop else NO_STOP_PRICE
    plus_price = breakeven_price(
        entry_price, cost_pct, side=side, gross=spec.gross_plus
    )

    # ПЕРВАЯ ФАЗА ПОЛУЧАЕТ БАР СРОКА, А НЕ ТОЛЬКО БАРЫ ДО НЕГО, и это не
    # мелочь. ``check_exit`` объявляет ``timeout`` лишь тогда, когда бар с
    # меткой ``deadline_at`` ПРЕДЪЯВЛЕН: пока его нет, неизвестно, кончилось
    # окно или данные не подъехали, и правило честно отвечает «исхода нет».
    # Сам бар срока в окно не входит — цикл правила прерывается на нём.
    first = [bar for bar in bars if bar.ts <= deadline_24]
    if not spec.keeps_stop:
        assert_no_stop(first)

    decision = position_rules.check_exit(
        bars=first,
        target_price=target_price,
        stop_price=stop_price,
        entry_price=entry_price,
        deadline_at=deadline_24,
        cost_pct=cost_pct,
    )
    if decision is None:
        return _unmeasured(
            spec.name, resolution,
            sum(1 for bar in first if bar.ts < deadline_24),
        )

    if decision.exit_reason != position_rules.EXIT_TIMEOUT:
        return _finish(
            variant=spec.name,
            exit_reason=decision.exit_reason,
            exit_bar_ts=decision.exit_bar_ts,
            exit_price=decision.exit_price,
            entry_price=entry_price,
            cost_pct=cost_pct,
            notional_usd=notional_usd,
            opened_at=opened_at,
            resolution=resolution,
            bars_used=decision.bars_held,
        )

    if not spec.waits_plus:
        return _finish(
            variant=spec.name,
            exit_reason=EXIT_TIMEOUT_24,
            exit_bar_ts=decision.exit_bar_ts,
            exit_price=decision.exit_price,
            entry_price=entry_price,
            cost_pct=cost_pct,
            notional_usd=notional_usd,
            opened_at=opened_at,
            resolution=resolution,
            bars_used=decision.bars_held,
        )

    # --- Вторая фаза: ожидание возврата в плюс (§3.4–3.7 ТЗ) -----------------
    second = [bar for bar in bars if bar.ts >= deadline_24]
    assert_no_stop(second)

    def _run(level: float) -> position_rules.ExitDecision | None:
        return position_rules.check_exit(
            bars=second,
            target_price=level,
            stop_price=NO_STOP_PRICE,
            entry_price=entry_price,
            deadline_at=deadline_48,
            cost_pct=cost_pct,
        )

    target_run = _run(target_price)
    plus_run = _run(plus_price)
    bars_used = decision.bars_held + max(
        0 if run is None else run.bars_held for run in (target_run, plus_run)
    )

    def _hit(run: position_rules.ExitDecision | None) -> datetime | None:
        if run is None or run.exit_reason != position_rules.EXIT_TARGET:
            return None
        return run.exit_bar_ts

    target_ts = _hit(target_run)
    plus_ts = _hit(plus_run)

    # §3.7 ТЗ: на одном баре выполнимы и цель, и плюс — исход ``target``.
    # Нестрогое сравнение и есть эта строчка задания.
    if target_ts is not None and (plus_ts is None or target_ts <= plus_ts):
        return _finish(
            variant=spec.name,
            exit_reason=position_rules.EXIT_TARGET,
            exit_bar_ts=target_ts,
            exit_price=target_price,
            entry_price=entry_price,
            cost_pct=cost_pct,
            notional_usd=notional_usd,
            opened_at=opened_at,
            resolution=resolution,
            bars_used=bars_used,
        )
    if plus_ts is not None:
        # ЦЕНА ВЫХОДА — ЦЕНА БЕЗУБЫТКА, А НЕ ЗАКРЫТИЕ БАРА (§3.4 ТЗ).
        outcome = _finish(
            variant=spec.name,
            exit_reason=EXIT_PLUS,
            exit_bar_ts=plus_ts,
            exit_price=plus_price,
            entry_price=entry_price,
            cost_pct=cost_pct,
            notional_usd=notional_usd,
            opened_at=opened_at,
            resolution=resolution,
            bars_used=bars_used,
        )
        assert_plus_exit_is_flat(
            outcome, entry_price=entry_price, cost_pct=cost_pct,
            gross=spec.gross_plus,
        )
        return outcome

    # Ни цель, ни плюс не достигнуты. Срок t0+48ч наступил — цена берётся по
    # закрытию последнего бара, открывшегося раньше срока, ТЕМ ЖЕ ``check_exit``,
    # каким боевой сервис берёт её на 24-часовой отметке (§3.5 ТЗ).
    if target_run is None or target_run.exit_reason != position_rules.EXIT_TIMEOUT:
        return _unmeasured(spec.name, resolution, bars_used)
    return _finish(
        variant=spec.name,
        exit_reason=EXIT_TIMEOUT_48,
        exit_bar_ts=target_run.exit_bar_ts,
        exit_price=target_run.exit_price,
        entry_price=entry_price,
        cost_pct=cost_pct,
        notional_usd=notional_usd,
        opened_at=opened_at,
        resolution=resolution,
        bars_used=bars_used,
    )


# ПОРЯДОК УДЕРЖАНИЯ — СЛЕДСТВИЕ УСТРОЙСТВА ВАРИАНТОВ, А НЕ НАБЛЮДЕНИЕ.
# Пара ``(узкий, широкий)``: удержание при широком не может быть КОРОЧЕ.
#   control ≤ A — снятие предела способно только убрать выход по пределу;
#   A ≤ B       — B на 24 часах не закрывается, а ждёт;
#   control ≤ C — C совпадает с control до суток и ждёт после;
#   C ≤ B       — C отличается от B ровно возможностью выйти по пределу раньше;
#   D ≤ B       — валовой безубыток лежит НИЖЕ чистого, значит достигается не
#                 позже.
# Нарушение означает, что расчёт разошёлся с правилом, — и тогда падаем, а не
# печатаем правдоподобные числа.
HOLDING_ORDER: tuple[tuple[str, str], ...] = (
    (CONTROL_VARIANT, A_NOSTOP_24),
    (A_NOSTOP_24, B_NOSTOP_PLUS_48),
    (CONTROL_VARIANT, C_STOP_PLUS_48),
    (C_STOP_PLUS_48, B_NOSTOP_PLUS_48),
    (D_NOSTOP_PLUS_48_GROSS, B_NOSTOP_PLUS_48),
)


def check_holding_order(outcomes: dict[str, PlusOutcome]) -> None:
    """Проверяет пять неравенств выше на одной позиции. Неизмеренные пропускает."""
    for narrow, wide in HOLDING_ORDER:
        left, right = outcomes.get(narrow), outcomes.get(wide)
        if left is None or right is None:
            continue
        if left.held_hours is None or right.held_hours is None:
            continue
        if right.held_hours < left.held_hours - FLAT_EPS:
            raise AssertionError(
                f"{wide} держит {right.held_hours:.4f} ч, а {narrow} — "
                f"{left.held_hours:.4f} ч: по устройству вариантов это "
                "невозможно"
            )


def assert_variant_contract() -> None:
    """ТАБЛИЦА ВАРИАНТОВ §4 ТЗ, ИЗЛОЖЕННАЯ ВТОРОЙ РАЗ И СВЕРЕННАЯ С ПЕРВОЙ.

    ЗАЧЕМ ПОВТОРЯТЬ ТО, ЧТО УЖЕ НАПИСАНО В ``VARIANTS``. Затем, что подмена
    одного флага в этой таблице НЕ ВИДНА НИ ПО ОДНОМУ ЧИСЛУ. Поставь варианту
    ``B_nostop_plus_48`` признак ``gross_plus`` — и он молча превратится в
    ``D_nostop_plus_48_gross``: исходы останутся правдоподобными, проверка
    «выход в безубыток даёт ноль» подтвердит нуль по ВАЛОВОЙ мерке, потому что
    спросит у той же испорченной строки, а в отчёт уйдут два одинаковых
    варианта под разными именами. Ровно об этом §9.3 ТЗ, опыт 4.

    Здесь записано то же, что в §4 ТЗ, но словами про свойства:

      * предел убытка ОСТАВЛЕН ровно у двух вариантов — control и C;
      * ожидания плюса НЕТ ровно у двух — control и A;
      * валовой плюс считает РОВНО ОДИН вариант, и это D;
      * control — единственный, кто и хранит предел, и не ждёт: он обязан
        совпасть с фактом бар в бар.
    """
    keeps = sorted(spec.name for spec in VARIANTS if spec.keeps_stop)
    if keeps != sorted((CONTROL_VARIANT, C_STOP_PLUS_48)):
        raise ValueError(f"предел оставлен не у тех вариантов: {keeps}")
    waits = sorted(spec.name for spec in VARIANTS if not spec.waits_plus)
    if waits != sorted((CONTROL_VARIANT, A_NOSTOP_24)):
        raise ValueError(f"ожидание плюса снято не у тех вариантов: {waits}")
    gross = [spec.name for spec in VARIANTS if spec.gross_plus]
    if gross != [D_NOSTOP_PLUS_48_GROSS]:
        raise ValueError(f"валовой плюс считает не тот вариант: {gross}")
    control = VARIANTS_BY_NAME[CONTROL_VARIANT]
    if not control.keeps_stop or control.waits_plus or control.gross_plus:
        raise ValueError(
            "control обязан быть фактическим правилом: предел на месте, "
            "ожидания нет, плюс не считается вовсе"
        )


def resolve_position(
    bars: list[position_rules.Bar],
    *,
    entry_price: float,
    target_pct: float,
    stop_pct: float,
    fact_target_price: float,
    fact_stop_price: float,
    cost_pct: float,
    notional_usd: float,
    side: str,
    opened_at: datetime,
    fact_deadline_at: datetime,
    resolution: str,
) -> dict[str, PlusOutcome]:
    """Пять исходов одной позиции на ОДНОМ и том же ряде свечей. ЧИСТАЯ функция.

    ``fact_target_price`` и ``fact_stop_price`` — уровни ИЗ СТРОКИ ПОЗИЦИИ
    (§А 2.4): именно их живой сервис передаёт в ``check_exit`` при закрытии, и
    именно они уже округлены колонкой NUMERIC(20,8). ``target_pct`` и
    ``stop_pct`` при этом всё равно нужны — по ним
    :func:`assert_levels_match_row` проверяет, что уровни в строке посчитаны той
    же ``rules.levels``, а не взялись неизвестно откуда.
    """
    assert_variant_contract()
    deadline_24, deadline_48 = phase_deadlines(opened_at, fact_deadline_at)
    assert_phase_boundary(opened_at, deadline_24, deadline_48)
    window = [
        bar for bar in bars if opened_at <= bar.ts <= deadline_48
    ]
    outcomes = {
        spec.name: resolve_variant(
            window,
            spec=spec,
            entry_price=entry_price,
            target_pct=target_pct,
            stop_pct=stop_pct,
            target_price=float(fact_target_price),
            fact_stop_price=float(fact_stop_price),
            cost_pct=cost_pct,
            notional_usd=notional_usd,
            side=side,
            opened_at=opened_at,
            deadline_24=deadline_24,
            deadline_48=deadline_48,
            resolution=resolution,
        )
        for spec in VARIANTS
    }
    check_holding_order(outcomes)
    return outcomes


# =============================================================================
# КОНТРОЛЬ (§9.1 ТЗ)
# =============================================================================


def control_reason_of_fact(fact_reason: str) -> str:
    """Имя исхода факта на языке замера.

    Правило возвращает одно ``timeout`` для двух разных событий, и замер их
    разделяет (см. заголовок). У ``control`` окно кончается на 24 часах, значит
    записанный ``timeout`` — это и есть ``timeout_24``. Приведение имён — не
    поблажка: допуск сверки остаётся нулевым, меняется только то, ЧТО с чем
    сравнивается.
    """
    if fact_reason == position_rules.EXIT_TIMEOUT:
        return EXIT_TIMEOUT_24
    return fact_reason


def _round(value: Any, places: int) -> float | None:
    """Приведение к точности ХРАНЕНИЯ колонки. ``None`` остаётся ``None``.

    ПРАВКА §А: приведение идёт через :func:`to_storage`, то есть тем же
    способом, каким округляет база (половина ОТ НУЛЯ). Встроенный ``round()``
    Python округляет половиной К ЧЁТНОМУ и разошёлся бы с базой ровно на
    половинах — то есть дал бы ложное расхождение контроля там, где всё верно.
    """
    if value is None:
        return None
    return to_storage(value, places)


def _differs(computed: Any, fact: Any, places: int) -> bool:
    """Разошлись ли числа после приведения к точности хранения (см. CONTROL_TOLERANCE)."""
    left = _round(computed, places)
    if left is None or fact is None:
        return left is not fact
    return abs(left - float(fact)) > CONTROL_TOLERANCE


def compare_control(
    position: dict[str, Any], control: PlusOutcome | None
) -> list[str]:
    """Расхождения контроля с фактом. Пустой список — совпало (§9.1 ТЗ).

    Сверяются четыре величины: причина выхода, МОМЕНТ ЗАКРЫТИЯ, цена выхода и
    итог в процентах.

    ГРАБЛИ 9.1.3, ПОВТОРЯТЬ КОТОРЫЕ НЕЛЬЗЯ. Колонки ``exit_bar_ts`` в
    ``positions`` НЕТ. Живой сервис пишет ``closed_at`` = время ЗАКРЫТИЯ бара
    выхода (``src/positions/runner.py``), а правило возвращает время его
    ОТКРЫТИЯ. Приведение делает ``src.shadow.trailing.closing_moment`` —
    ПЕРЕИСПОЛЬЗУЕТСЯ готовая, вторая такая же здесь не пишется.

    ``None`` вместо контроля — тоже расхождение, и названное словами: живое
    правило на этом ряде исхода не дало, а в базе исход есть.
    """
    if control is None or not control.measured:
        return ["живое правило не дало исхода на прочитанном ряде свечей"]
    problems: list[str] = []
    expected_reason = control_reason_of_fact(str(position["exit_reason"]))
    if control.exit_reason != expected_reason:
        problems.append(
            f"exit_reason: факт {position['exit_reason']} "
            f"(на языке замера {expected_reason}), пересчёт {control.exit_reason}"
        )
    if control.closed_at != position["closed_at"]:
        problems.append(
            f"closed_at: факт {position['closed_at']}, "
            f"пересчёт {control.closed_at} (бар выхода {control.exit_bar_ts})"
        )
    if _differs(control.exit_price, position["exit_price"], PRICE_STORAGE_PLACES):
        problems.append(
            f"exit_price: факт {position['exit_price']}, "
            f"пересчёт {_round(control.exit_price, PRICE_STORAGE_PLACES)}"
        )
    if _differs(control.net_pnl_pct, position["net_pnl_pct"], PCT_STORAGE_PLACES):
        problems.append(
            f"net_pnl_pct: факт {position['net_pnl_pct']}, "
            f"пересчёт {_round(control.net_pnl_pct, PCT_STORAGE_PLACES)}"
        )
    return problems


# =============================================================================
# ЧИСЛА (§6 ТЗ)
# =============================================================================


def outcome_table(
    shadows: dict[int, dict[str, PlusOutcome]],
    ids: list[int],
) -> dict[str, dict[str, int]]:
    """ЧИСЛО 1: распределение исходов по вариантам."""
    table = {
        variant: dict.fromkeys(SHADOW_REASONS, 0) for variant in variant_names()
    }
    for position_id in ids:
        for variant, row in shadows.get(position_id, {}).items():
            table[variant][row.exit_reason] += 1
    return table


def bootstrap_mean_ci(values: list[float]) -> tuple[float, float] | None:
    """ЧИСЛО §6.2 ТЗ: интервал среднего бутстрэпом, 10 000 повторов.

    ПОЧЕМУ ЧЕРЕЗ ЧИСЛА ПОВТОРОВ, А НЕ ЧЕРЕЗ ИНДЕКСЫ — тот же приём, что в
    ``scripts/trailing_stats.bootstrap_means``: пересборка с возвращением
    полностью описывается тем, СКОЛЬКО РАЗ каждое наблюдение в неё попало, и
    это ровно полиномиальное распределение. Матрица повторов на вектор значений
    — одно умножение, и памяти оно требует на порядки меньше, чем массив
    индексов.

    ``None`` при пустой выборке: интервал от нуля наблюдений — не «широкий
    интервал», а отсутствие величины.
    """
    if not values:
        return None
    data = np.asarray(values, dtype=float)
    n = data.size
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    counts = rng.multinomial(
        n, np.full(n, 1.0 / n), size=BOOTSTRAP_RESAMPLES
    )
    cloud = counts @ data / float(n)
    lo_q = (1.0 - CONFIDENCE) / 2.0 * 100.0
    hi_q = (1.0 + CONFIDENCE) / 2.0 * 100.0
    lo, hi = np.percentile(cloud, [lo_q, hi_q])
    return float(lo), float(hi)


def intervals_overlap(
    first: tuple[float, float] | None, second: tuple[float, float] | None
) -> bool | None:
    """Пересекаются ли два интервала. ``None`` — хотя бы одного из них нет."""
    if first is None or second is None:
        return None
    return not (first[1] < second[0] or second[1] < first[0])


def money_summary(
    shadows: dict[int, dict[str, PlusOutcome]], ids: list[int]
) -> list[dict[str, Any]]:
    """ЧИСЛА 2 и 3: итог в процентах и накопленный итог в долларах.

    ХУДШАЯ СДЕЛКА СЧИТАЕТСЯ ОБЯЗАТЕЛЬНО. Смысл предела убытка — ограничить
    хвост, а не поднять среднее; среднее без крайнего случая скрыло бы ровно то,
    ради чего предел стоит.

    НЕИЗМЕРЕННЫЕ ИСХОДЫ В СРЕДНЕЕ НЕ ВХОДЯТ и считаются отдельно. Подставить им
    ноль значило бы утверждать «вариант не изменил результат», чего никто не
    измерял.
    """
    out: list[dict[str, Any]] = []
    for variant in variant_names():
        values: list[float] = []
        usd = 0.0
        unmeasured = 0
        wins = 0
        worst: tuple[float, int] | None = None
        for position_id in ids:
            row = shadows.get(position_id, {}).get(variant)
            if row is None or row.net_pnl_pct is None:
                unmeasured += 1
                continue
            value = float(row.net_pnl_pct)
            values.append(value)
            usd += float(row.net_pnl_usd or 0.0)
            if value > 0.0:
                wins += 1
            if worst is None or value < worst[0]:
                worst = (value, position_id)
        interval = bootstrap_mean_ci(values)
        out.append({
            "variant": variant,
            "n": len(values),
            "mean_pct": (sum(values) / len(values)) if values else None,
            "median_pct": statistics.median(values) if values else None,
            "worst_pct": worst[0] if worst else None,
            "worst_position_id": worst[1] if worst else None,
            "win_share": (wins / len(values)) if values else None,
            "sum_usd": usd if values else None,
            "unmeasured": unmeasured,
            "ci_low": None if interval is None else interval[0],
            "ci_high": None if interval is None else interval[1],
        })
    return out


def slot_summary(
    shadows: dict[int, dict[str, PlusOutcome]], ids: list[int]
) -> list[dict[str, Any]]:
    """ЧИСЛО 4: занятость слота и лишние часы против control."""
    out: list[dict[str, Any]] = []
    for variant in variant_names():
        held: list[float] = []
        extra: list[float] = []
        for position_id in ids:
            row = shadows.get(position_id, {}).get(variant)
            base = shadows.get(position_id, {}).get(CONTROL_VARIANT)
            if row is None or row.held_hours is None:
                continue
            held.append(float(row.held_hours))
            if base is not None and base.held_hours is not None:
                extra.append(float(row.held_hours) - float(base.held_hours))
        out.append({
            "variant": variant,
            "n": len(held),
            "mean_held_h": (sum(held) / len(held)) if held else None,
            "max_held_h": max(held) if held else None,
            "mean_extra_h": (sum(extra) / len(extra)) if extra else None,
            "max_extra_h": max(extra) if extra else None,
        })
    return out


def recovery_summary(
    shadows: dict[int, dict[str, PlusOutcome]],
    ids: list[int],
    *,
    deadlines: dict[int, datetime],
) -> dict[str, Any]:
    """ЧИСЛО 6 — ГЛАВНОЕ (§6 ТЗ). Только позиции, бывшие в минусе на 24 часах.

    КТО ПОПАДАЕТ В ЭТУ ГРУППУ. Позиции, которые к отметке t0+24ч ЕЩЁ ЖИВЫ при
    снятом пределе и стоят в минусе. «Ещё жива на 24 часах» — это в точности
    исход ``timeout_24`` варианта ``A_nostop_24``: дошедшая до цели раньше
    закрыта раньше, и спрашивать про её положение на 24 часах бессмысленно.
    «В минусе» считается ЧИСТЫМ, после издержек, — той же меркой, что и «плюс»
    в §3.6 ТЗ; иначе группа «в минусе» и условие возврата мерились бы разными
    линейками.

    ЧТО СЧИТАЕТСЯ ВОЗВРАТОМ. Исход варианта B: ``plus_exit`` — вернулась в
    чистый плюс, ``target`` — дошла до цели (это тем более возврат). Время
    возврата отсчитывается ОТ ОТМЕТКИ 24 ЧАСОВ, а не от открытия: вопрос
    владельца — «сколько ждать», а не «сколько всего висела сделка».

    ПОСЛЕДНЕЕ ПОДЧИСЛО отвечает на вопрос, дорого ли обходится ожидание тем,
    кому оно не помогло: средний итог не вернувшихся на 48-часовой отметке
    против их же итога на 24-часовой.
    """
    losers: list[int] = []
    for position_id in ids:
        row = shadows.get(position_id, {}).get(A_NOSTOP_24)
        if row is None or row.net_pnl_pct is None:
            continue
        if row.exit_reason != EXIT_TIMEOUT_24:
            continue
        if float(row.net_pnl_pct) < 0.0:
            losers.append(position_id)

    returned: list[float] = []
    returned_ids: list[int] = []
    stayed_24: list[float] = []
    stayed_48: list[float] = []
    unmeasured = 0
    for position_id in losers:
        row = shadows[position_id].get(B_NOSTOP_PLUS_48)
        mark_24 = shadows[position_id][A_NOSTOP_24].net_pnl_pct
        if row is None or not row.measured or row.closed_at is None:
            unmeasured += 1
            continue
        if row.exit_reason in (EXIT_PLUS, position_rules.EXIT_TARGET):
            hours = (
                row.closed_at - deadlines[position_id]
            ).total_seconds() / SECONDS_IN_HOUR
            returned.append(hours)
            returned_ids.append(position_id)
        elif row.net_pnl_pct is not None and mark_24 is not None:
            stayed_24.append(float(mark_24))
            stayed_48.append(float(row.net_pnl_pct))

    def _quantile(values: list[float], q: float) -> float | None:
        if not values:
            return None
        return float(np.percentile(np.asarray(values, dtype=float), q * 100.0))

    return {
        "in_minus_at_24h": len(losers),
        "returned": len(returned),
        "unmeasured": unmeasured,
        "return_hours_median": _quantile(returned, 0.50),
        "return_hours_p25": _quantile(returned, 0.25),
        "return_hours_p75": _quantile(returned, 0.75),
        "return_hours_max": max(returned) if returned else None,
        "stayed": len(stayed_48),
        "stayed_mean_at_24h": (
            sum(stayed_24) / len(stayed_24) if stayed_24 else None
        ),
        "stayed_mean_at_48h": (
            sum(stayed_48) / len(stayed_48) if stayed_48 else None
        ),
        "returned_ids": returned_ids,
    }


# =============================================================================
# ПЕЧАТЬ
# =============================================================================

REGIME_WARNING = (
    "ОБЯЗАТЕЛЬНАЯ ОГОВОРКА (§6.3 ТЗ). ВСЯ ВЫБОРКА ЦЕЛИКОМ ПРИХОДИТСЯ НА ОДИН "
    "РЕЖИМ РЫНКА."
)


def _fmt(value: float | None, places: int = 4, sign: bool = True) -> str:
    if value is None:
        return "—"
    return f"{value:+.{places}f}" if sign else f"{value:.{places}f}"


def print_regime_warning() -> None:
    """§6.3 ТЗ: оговорка печатается ТЕКСТОМ, а не подразумевается."""
    print()
    print("  " + REGIME_WARNING)
    print("  Все позиции открыты в один сравнительно короткий отрезок времени,")
    print("  и рынок в нём вёл себя одним образом. Числа ниже описывают, что")
    print("  случилось С ЭТИМИ сделками В ЭТОМ режиме, и не переносятся на")
    print("  режим, которого в выборке нет. Правило, выигравшее на падении,")
    print("  может проиграть на росте, и наоборот — данных, чтобы это")
    print("  различить, здесь нет вовсе.")


def print_small_sample(n: int) -> None:
    """§5.5 ТЗ: при выборке меньше порога скрипт обязан сказать это ВСЛУХ."""
    print()
    print("=" * 78)
    print(" ВЫБОРКА МАЛА, РАЗЛИЧИТЬ ВАРИАНТЫ НЕЛЬЗЯ")
    print("=" * 78)
    print(f"  В выборке {n} позиций при пороге {MIN_SAMPLE}. При таком числе")
    print("  наблюдений доверительный интервал разницы ШИРЕ любой из наблюдаемых")
    print("  разниц, и сравнительная степень была бы утверждением, которого")
    print("  данные не несут. Числа ниже — описание того, что случилось с этими")
    print("  конкретными сделками, и ничего более: они не предсказывают")
    print("  следующие и не позволяют назвать вариант лучше или хуже другого.")


def print_outcome_table(table: dict[str, dict[str, int]], title: str) -> None:
    """ЧИСЛО 1."""
    print()
    print(f"  — {title} —")
    header = f"  {'вариант':<24}"
    for reason in SHADOW_REASONS:
        header += f"{reason:>12}"
    print(header)
    print("  " + "-" * (24 + 12 * len(SHADOW_REASONS)))
    for variant in variant_names():
        line = f"  {variant:<24}"
        for reason in SHADOW_REASONS:
            line += f"{table[variant][reason]:>12}"
        print(line)


def print_money(rows: list[dict[str, Any]], title: str) -> None:
    """ЧИСЛА 2 и 3 вместе: проценты и доллары читаются только рядом."""
    print()
    print(f"  — {title} —")
    print(f"  {'вариант':<24}{'N':>4}{'ср.,%':>10}{'медиана,%':>11}"
          f"{'худшая,%':>11}{'в плюс':>9}{'сумма,$':>10}{'не изм.':>9}")
    print("  " + "-" * 88)
    for row in rows:
        share = (
            "—" if row["win_share"] is None else f"{row['win_share'] * 100:.0f}%"
        )
        print(
            f"  {row['variant']:<24}{row['n']:>4}"
            f"{_fmt(row['mean_pct'], 3):>10}{_fmt(row['median_pct'], 3):>11}"
            f"{_fmt(row['worst_pct'], 3):>11}{share:>9}"
            f"{_fmt(row['sum_usd'], 4):>10}{row['unmeasured']:>9}"
        )


def print_intervals(rows: list[dict[str, Any]]) -> None:
    """§6.2 ТЗ: интервал среднего и ДВА явных ответа по каждому варианту."""
    control = next(
        (r for r in rows if r["variant"] == CONTROL_VARIANT), None
    )
    base = (
        None if control is None or control["ci_low"] is None
        else (control["ci_low"], control["ci_high"])
    )
    print()
    print(f"  {'вариант':<24}{'ср.,%':>10}{'интервал 95%':>26}"
          f"{'ноль':>16}{'с control':>16}")
    print("  " + "-" * 92)
    for row in rows:
        if row["ci_low"] is None:
            span = "—"
            zero = "—"
            overlap = "—"
        else:
            pair = (row["ci_low"], row["ci_high"])
            span = f"[{pair[0]:+.3f}; {pair[1]:+.3f}]"
            zero = "пересекает" if pair[0] <= 0.0 <= pair[1] else "не пересекает"
            crossing = intervals_overlap(pair, base)
            overlap = (
                "—" if crossing is None
                else ("пересекается" if crossing else "не пересекается")
            )
        print(
            f"  {row['variant']:<24}{_fmt(row['mean_pct'], 3):>10}"
            f"{span:>26}{zero:>16}{overlap:>16}"
        )
    print()
    print(f"  Интервал — бутстрэп среднего, {BOOTSTRAP_RESAMPLES} повторов, "
          f"зерно {BOOTSTRAP_SEED}.")
    print("  Зерно закреплено, чтобы повторный прогон не менял границ (§7.5 ТЗ).")
    print("  ПЕРЕСЕКАЮЩИЕСЯ ИНТЕРВАЛЫ — ЭТО «РАЗЛИЧИТЬ НЕЛЬЗЯ», а не «одинаково».")


def print_recovery(block: dict[str, Any], title: str) -> None:
    """ЧИСЛО 6 — главное."""
    print()
    print(f"  — {title} —")
    print(f"  Были в минусе на отметке 24 ч:        {block['in_minus_at_24h']}")
    print(f"  Из них вернулись в чистый плюс за 24 ч: {block['returned']}")
    if block["unmeasured"]:
        print(f"  Исход не измерен (пробел в ряду):     {block['unmeasured']}")
    print("  Время возврата, часов от отметки 24 ч:")
    print(f"      медиана       {_fmt(block['return_hours_median'], 2, sign=False)}")
    print(f"      25-й проц.    {_fmt(block['return_hours_p25'], 2, sign=False)}")
    print(f"      75-й проц.    {_fmt(block['return_hours_p75'], 2, sign=False)}")
    print(f"      максимум      {_fmt(block['return_hours_max'], 2, sign=False)}")
    print(f"  Не вернулись:                         {block['stayed']}")
    print(f"      их средний итог на 24 ч:  {_fmt(block['stayed_mean_at_24h'], 3)}%")
    print(f"      их средний итог на 48 ч:  {_fmt(block['stayed_mean_at_48h'], 3)}%")
    if (
        block["stayed_mean_at_24h"] is not None
        and block["stayed_mean_at_48h"] is not None
    ):
        delta = block["stayed_mean_at_48h"] - block["stayed_mean_at_24h"]
        print(f"      цена ожидания для них:    {_fmt(delta, 3)} п.п.")


# =============================================================================
# ЧТЕНИЕ И ПРОГОН
# =============================================================================


async def _load_bars(
    position: dict[str, Any], now: datetime
) -> list[position_rules.Bar]:
    """Ряд свечей позиции: от бара входа до бара срока t0+48ч, ТОЛЬКО ЗАКРЫТЫЕ.

    ВЕРХНЯЯ ГРАНИЦА ЧТЕНИЯ — САМ ОТРЕЗОК, А НЕ ``settle_seconds()``. Запас
    коллектора отвечает на вопрос «перестал ли бар перезаписываться» и равен
    3900 секундам; поставленный на границу окна, он обезвредил бы проверку на
    подглядывание (§5.4 ТЗ, урок Этапа 9.1.5). Закрытость бара относительно
    ``now`` проверяется отдельно и по длине бара — :func:`closed_bars`.
    """
    window_end = position["opened_at"] + timedelta(hours=TOTAL_HOURS)
    raw = await db.get_ohlcv_bars(
        int(position["instrument_id"]),
        str(position["resolution"]),
        position["opened_at"],
        window_end,
    )
    bars = [
        position_rules.Bar(
            ts=item["ts"], high=float(item["high"]),
            low=float(item["low"]), close=float(item["close"]),
        )
        for item in raw
    ]
    kept = closed_bars(bars, now, str(position["resolution"]))
    assert_closed_bars(kept, now, str(position["resolution"]))
    return kept


async def _count_blocked(
    positions: list[dict[str, Any]],
    shadows: dict[int, dict[str, PlusOutcome]],
) -> dict[tuple[int, str], int]:
    """ЧИСЛО 5: годные входы, попавшие в окно ЛИШНЕГО удержания слота.

    Окно — от закрытия варианта ``control`` (включительно) до закрытия
    рассматриваемого варианта (не включительно). Границы и перечень условий
    годности разобраны в ``db.count_blocked_signals``; там же сказано, почему
    это ВЕРХНЯЯ ГРАНИЦА, а не само число.
    """
    by_id = {int(p["id"]): p for p in positions}
    out: dict[tuple[int, str], int] = {}
    for position_id, rows in sorted(shadows.items()):
        position = by_id[position_id]
        base = rows.get(CONTROL_VARIANT)
        if base is None or base.closed_at is None:
            continue
        for variant, row in rows.items():
            if not row.measured or row.closed_at is None:
                continue
            out[(position_id, variant)] = await db.count_blocked_signals(
                instrument_id=int(position["instrument_id"]),
                min_probability=float(settings.POSITION_MIN_PROBABILITY),
                ts_from=base.closed_at,
                ts_to=row.closed_at,
            )
    return out


def pick_logic_version(
    counts: list[dict[str, Any]], requested: int | None
) -> int | None:
    """Версия логики выборки: названная в аргументе или единственная в базе.

    СМЕШИВАТЬ ВЕРСИИ ЗАПРЕЩЕНО (§5.1 ТЗ), и «взять ту, которых больше» —
    это то же смешение, только молчаливое. Поэтому при нескольких версиях без
    явного аргумента возвращается ``None``, и скрипт отказывается считать,
    назвав версии, между которыми надо выбрать.
    """
    if requested is not None:
        return int(requested)
    versions = [int(row["logic_version"]) for row in counts
                if int(row["closed_total"] or 0) > 0]
    if len(versions) == 1:
        return versions[0]
    return None


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Замер правила «без предела убытка, ожидание возврата в плюс до "
            "48 часов» (Этап 9.1.6). Без --apply — только печать."
        )
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="записать результаты в position_plus_exit_shadow",
    )
    parser.add_argument(
        "--logic-version", type=int, default=None, metavar="N",
        help=(
            "версия логики выборки; по умолчанию берётся единственная в базе, "
            "а при нескольких скрипт отказывается считать (§5.1 ТЗ)"
        ),
    )
    parser.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help="считать не более N позиций — ТОЛЬКО ДЛЯ ОТЛАДКИ",
    )
    parser.add_argument(
        "--json", default=None, metavar="PATH",
        help="дополнительно сохранить сводку машиночитаемо",
    )
    args = parser.parse_args()

    setup_logging()
    now = datetime.now(UTC)
    await db.connect()
    try:
        return await _run(args, now)
    finally:
        await db.close()


async def _run(args: argparse.Namespace, now: datetime) -> int:
    version_counts = await db.count_positions_by_logic_version()
    logic_version = pick_logic_version(version_counts, args.logic_version)

    print("=" * 78)
    print(" ЭТАП 9.1.6. ОЖИДАНИЕ ВОЗВРАТА В ПЛЮС ДО 48 ЧАСОВ — ЗАМЕР")
    print("=" * 78)
    print("  ЭТО ЗАМЕР, А НЕ ВНЕДРЕНИЕ. Боевое правило выхода не меняется,")
    print("  LOGIC_VERSION не поднимается, ни одно решение системы от этих")
    print("  чисел не зависит. Выбор варианта делает владелец по числам;")
    print("  скрипт вариант не рекомендует и рекомендовать не может (§1.4 ТЗ).")
    print()
    print("  Версии логики в таблице позиций:")
    for row in version_counts:
        print(f"      v{row['logic_version']}: закрытых "
              f"{row['closed_total']}, из них data_gap {row['data_gap']}, "
              f"открытых {row['still_open']}")
    if logic_version is None:
        print()
        print("  ОТКАЗ: версий логики в базе несколько, а смешивать их запрещено")
        print("  (§5.1 ТЗ). Назовите версию явно: --logic-version N.")
        _log.warning("Замер плюса: версия логики не выбрана")
        return 2

    chosen = next(
        (r for r in version_counts if int(r["logic_version"]) == logic_version),
        None,
    )
    print()
    print(f"  Версия логики выборки:        {logic_version}")
    print(f"  Закрытых позиций этой версии: "
          f"{0 if chosen is None else chosen['closed_total']}")
    print(f"  Из них исключено (data_gap):  "
          f"{0 if chosen is None else chosen['data_gap']}")
    print(f"  Открытых (в замер не идут):   "
          f"{0 if chosen is None else chosen['still_open']}")

    everything = await db.get_positions_for_shadow()
    sample = [
        p for p in everything if int(p["logic_version"]) == logic_version
    ]

    # --- ОТБРАКОВКА (§5.2–5.3 ТЗ) -----------------------------------------
    #
    # ГЛАВНЫЙ ОГРАНИЧИТЕЛЬ ПЕЧАТАЕТСЯ ЯВНО. Позиция, открытая позже чем 48
    # часов назад, не может быть посчитана по правилу, у которого срок 48
    # часов: у неё попросту нет будущего. Молча выкинуть её значило бы выдать
    # выборку «по тем, кто успел», за выборку по всем.
    short: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    for position in sample:
        if covers_full_window(
            position["opened_at"], now, str(position["resolution"])
        ):
            kept.append(position)
        else:
            short.append(position)
    if args.limit is not None:
        kept = kept[: int(args.limit)]

    print(f"  Не покрыты t0+{TOTAL_HOURS}ч "
          f"(insufficient_future_bars): {len(short)}")
    print(f"  В выборке замера:             {len(kept)}")
    print(f"  Длина бара:                   "
          f"{BAR_SECONDS[str(kept[0]['resolution'])] if kept else '—'} с")
    print(f"  Запас коллектора (settle_seconds): {settle_seconds()} с — "
          "НЕ используется как граница окна")
    if args.limit is not None:
        print(f"  !! --limit {args.limit}: это ОТЛАДОЧНЫЙ прогон, "
              "числа неполны")

    _log.info(
        "Замер плюса: выборка",
        plusexit_logic_version=logic_version,
        plusexit_sample=len(kept),
        plusexit_insufficient_future_bars=len(short),
        settle_seconds=settle_seconds(),
    )

    if not kept:
        print()
        print("  ДАННЫХ НЕ ХВАТИЛО: после отбраковки не осталось ни одной")
        print("  позиции. Считать нечего, и таблица с одним столбцом нулей")
        print("  была бы хуже прямого ответа.")
        return 3

    # --- Пересчёт ---------------------------------------------------------
    shadows: dict[int, dict[str, PlusOutcome]] = {}
    deadlines: dict[int, datetime] = {}
    mismatches: list[tuple[int, list[str]]] = []
    failures: list[tuple[int, str]] = []
    no_data_ids: list[int] = []
    for position in kept:
        position_id = int(position["id"])
        try:
            assert_sample_covered(
                position["opened_at"], now, str(position["resolution"])
            )
            bars = await _load_bars(position, now)
            rows = resolve_position(
                bars,
                entry_price=float(position["entry_price"]),
                target_pct=float(position["target_pct"]),
                stop_pct=float(position["stop_pct"]),
                fact_target_price=float(position["target_price"]),
                fact_stop_price=float(position["stop_price"]),
                cost_pct=float(position["cost_pct"]),
                notional_usd=float(position["notional_usd"]),
                side=str(position["side"]),
                opened_at=position["opened_at"],
                fact_deadline_at=position["deadline_at"],
                resolution=str(position["resolution"]),
            )
        except (ValueError, AssertionError) as exc:
            # Расчёт ОТКАЗАЛСЯ считать: ряд негоден, посылка нарушена или
            # проходы разошлись. Это то же по смыслу, что расхождение
            # контроля, и молча пропустить такую позицию нельзя.
            failures.append((position_id, f"{type(exc).__name__}: {exc}"))
            continue
        shadows[position_id] = rows
        deadlines[position_id] = position["deadline_at"]
        problems = compare_control(position, rows.get(CONTROL_VARIANT))
        if problems:
            mismatches.append((position_id, problems))
        if any(not row.measured for row in rows.values()):
            no_data_ids.append(position_id)

    compared = len(kept)
    mismatched = len(mismatches) + len(failures)
    if mismatched > compared:
        # Печатать невозможное число хуже, чем упасть: увидев «11 и 12»,
        # человек начинает сомневаться во ВСЁМ выводе, и правильно делает.
        raise AssertionError(
            f"расхождений {mismatched} при {compared} сверенных — счётчики "
            "разошлись; печатать такой отчёт нельзя"
        )

    print()
    print("-" * 78)
    print(" КОНТРОЛЬ: живое правило при фактическом пределе против записанного")
    print("-" * 78)
    print(f"  Сошлось {compared - mismatched} из {compared}, "
          f"разошлось {mismatched}")

    if mismatched:
        print()
        print("=" * 78)
        print(" ОТКАЗ: КОНТРОЛЬ НЕ СОВПАЛ — СРАВНЕНИЕ ВАРИАНТОВ НЕ ПУБЛИКУЕТСЯ")
        print("=" * 78)
        for position_id, reason in failures:
            print(f"  позиция {position_id}: {reason}")
        for position_id, problems in mismatches:
            print(f"  позиция {position_id}:")
            for problem in problems:
                print(f"      {problem}")
        print()
        print("  Если пересчёт не умеет повторить УЖЕ СЛУЧИВШЕЕСЯ, его числа по")
        print("  НЕ случившимся вариантам не стоят ничего. Расширять допуск")
        print("  сравнения запрещено (§9.1 ТЗ): расхождение надо расследовать.")
        print("  Ни одной строки не записано.")
        _log.warning(
            "Замер плюса: контроль не совпал",
            plusexit_control_compared=compared,
            plusexit_control_mismatched=mismatched,
            plusexit_rows_written=0,
        )
        return 2

    # ПОЗИЦИИ С ПРОБЕЛОМ ВНУТРИ ОТРЕЗКА (§5.2 ТЗ) ИСКЛЮЧАЮТСЯ ИЗ ЧИСЕЛ, НО НЕ
    # ИЗ ТАБЛИЦЫ. Из чисел — потому что вариант, у которого исход не измерен,
    # нельзя сравнивать с вариантом, у которого измерен: разные выборки
    # молча дали бы разные средние. Из таблицы не исключаются: строка
    # 'no_data' — это запись о том, что измерить не удалось, и она полезнее
    # отсутствия строки.
    measured_ids = [
        int(p["id"]) for p in kept
        if int(p["id"]) in shadows and int(p["id"]) not in no_data_ids
    ]
    print(f"  Пробел внутри отрезка (no_data), исключено из чисел: "
          f"{len(no_data_ids)}")
    print(f"  Считается по:                 {len(measured_ids)} позициям")

    blocked = await _count_blocked(kept, shadows)

    print_regime_warning()
    if len(measured_ids) < MIN_SAMPLE:
        print_small_sample(len(measured_ids))

    by_id = {int(p["id"]): p for p in kept}
    sides = sorted({str(by_id[i]["side"]) for i in measured_ids})
    cuts: list[tuple[str, list[int]]] = [("вся выборка", measured_ids)]
    for side in sides:
        cuts.append((
            f"только {side}",
            [i for i in measured_ids if str(by_id[i]["side"]) == side],
        ))

    # --- ЧИСЛО 1 ----------------------------------------------------------
    print()
    print("-" * 78)
    print(" ЧИСЛО 1. РАСПРЕДЕЛЕНИЕ ИСХОДОВ")
    print("-" * 78)
    tables: dict[str, dict[str, dict[str, int]]] = {}
    for title, ids in cuts:
        tables[title] = outcome_table(shadows, ids)
        print_outcome_table(tables[title], title)
    print()
    print("  Столбец no_data показан отдельно, а не растворён в timeout:")
    print("  неизмеренное не должно выглядеть измеренным.")

    # --- ЧИСЛА 2 и 3 ------------------------------------------------------
    print()
    print("-" * 78)
    print(" ЧИСЛА 2 И 3. ИТОГ В ПРОЦЕНТАХ И НАКОПЛЕННЫЙ ИТОГ В ДОЛЛАРАХ")
    print("-" * 78)
    slots = sorted({float(by_id[i]["notional_usd"]) for i in measured_ids})
    print(f"  Размер слота по факту позиций: "
          f"{', '.join(f'${value:g}' for value in slots) or '—'}")
    print("  Доллары считаются от ФАКТИЧЕСКОГО слота позиции, без")
    print("  реинвестирования: подставленная константа превратила бы замер в")
    print("  оценку, а строка выше делает «при слоте $2» проверяемым.")
    print("  ХУДШАЯ СДЕЛКА ПЕЧАТАЕТСЯ ОБЯЗАТЕЛЬНО: смысл предела убытка —")
    print("  ограничить хвост, а не поднять среднее.")
    money: dict[str, list[dict[str, Any]]] = {}
    for title, ids in cuts:
        money[title] = money_summary(shadows, ids)
        print_money(money[title], title)

    # --- §6.2 ДОВЕРИЕ -----------------------------------------------------
    print()
    print("-" * 78)
    print(" ДОВЕРИЕ К СРЕДНЕМУ (§6.2 ТЗ)")
    print("-" * 78)
    print_intervals(money["вся выборка"])

    # --- ЧИСЛО 4 ----------------------------------------------------------
    slot_rows = slot_summary(shadows, measured_ids)
    print()
    print("-" * 78)
    print(" ЧИСЛО 4. ЗАНЯТОСТЬ СЛОТА")
    print("-" * 78)
    print(f"  {'вариант':<24}{'N':>4}{'ср. в сделке,ч':>17}{'максимум,ч':>13}"
          f"{'ср. лишних,ч':>15}{'макс. лишних,ч':>17}")
    print("  " + "-" * 90)
    for row in slot_rows:
        print(
            f"  {row['variant']:<24}{row['n']:>4}"
            f"{_fmt(row['mean_held_h'], 2, sign=False):>17}"
            f"{_fmt(row['max_held_h'], 2, sign=False):>13}"
            f"{_fmt(row['mean_extra_h'], 2):>15}"
            f"{_fmt(row['max_extra_h'], 2):>17}"
        )

    # --- ЧИСЛО 5 ----------------------------------------------------------
    print()
    print("-" * 78)
    print(" ЧИСЛО 5. ЗАБЛОКИРОВАННЫЕ ВХОДЫ — ВЕРХНЯЯ ГРАНИЦА")
    print("-" * 78)
    blocked_rows: list[dict[str, Any]] = []
    for variant in variant_names():
        total = sum(
            blocked.get((position_id, variant), 0) for position_id in measured_ids
        )
        blocked_rows.append({
            "variant": variant,
            "blocked_signals": total,
            "per_position": (
                total / len(measured_ids) if measured_ids else None
            ),
        })
    print(f"  {'вариант':<24}{'входов':>9}{'на позицию':>13}")
    print("  " + "-" * 46)
    for row in blocked_rows:
        print(f"  {row['variant']:<24}{row['blocked_signals']:>9}"
              f"{_fmt(row['per_position'], 2, sign=False):>13}")
    print()
    print("  ЭТО ВЕРХНЯЯ ГРАНИЦА, А НЕ САМО ЧИСЛО — та же оговорка, что на")
    print("  9.1.4. Годным входом здесь считается: тот же инструмент,")
    print(f"  decision='buy', probability >= {settings.POSITION_MIN_PROBABILITY}, "
          "degraded=false, момент в окне")
    print("  [закрытие control, закрытие варианта). Живой отбор проверяет сверх")
    print("  этого версию логики, замороженную цель, свежесть свечи и свободный")
    print("  слот, поэтому настоящих входов было бы НЕ БОЛЬШЕ. Читать это число")
    print("  как «столько-то прибыли потеряно» НЕЛЬЗЯ: чтобы узнать их итог,")
    print("  пришлось бы проиграть целиком другую историю позиций.")

    # --- ЧИСЛО 6 ----------------------------------------------------------
    print()
    print("-" * 78)
    print(" ЧИСЛО 6 (ГЛАВНОЕ). ВОЗВРАТ В ПЛЮС У ТЕХ, КТО БЫЛ В МИНУСЕ НА 24 Ч")
    print("-" * 78)
    print("  «В минусе» — ЧИСТЫМ, после издержек: той же меркой, что и «плюс»")
    print("  в §3.6 ТЗ. Иначе группа и условие возврата мерились бы разными")
    print("  линейками.")
    recovery: dict[str, dict[str, Any]] = {}
    for title, ids in cuts:
        recovery[title] = recovery_summary(shadows, ids, deadlines=deadlines)
        print_recovery(recovery[title], title)

    print()
    print("  ВЫБОР ПРАВИЛА ВЫХОДА ДЛЯ ВНЕДРЕНИЯ НЕ ДЕЛАЕТСЯ И НЕ ПРЕДЛАГАЕТСЯ")
    print("  (§1.4 ТЗ). Числа выше описывают эти конкретные сделки в этом")
    print("  конкретном режиме рынка.")

    # --- Запись -----------------------------------------------------------
    written = 0
    if args.apply:
        if not await db.position_plus_exit_shadow_exists():
            print()
            print("  ОТКАЗ: таблицы position_plus_exit_shadow нет.")
            print("  Примените db/migrations/024_position_plus_exit_shadow.sql")
            return 2
        rows_to_save = [
            {
                "position_id": position_id,
                "variant": row.variant,
                "logic_version": logic_version,
                "exit_reason": row.exit_reason,
                "exit_bar_ts": row.exit_bar_ts,
                "closed_at": row.closed_at,
                "exit_price": row.exit_price,
                "net_pnl_pct": row.net_pnl_pct,
                "net_pnl_usd": row.net_pnl_usd,
                "held_hours": row.held_hours,
                "bars_used": row.bars_used,
                "resolution": row.resolution,
            }
            for position_id, rows in sorted(shadows.items())
            for row in (rows[name] for name in variant_names())
        ]
        written = await db.save_position_plus_exit_shadow(rows_to_save)
        print()
        print(f"  Записано строк: {written}")
    else:
        print()
        print("  Ничего не записано: без --apply скрипт только считает.")

    peak = peak_rss_mb()
    print(f"  Пиковая память: {peak:,.0f} МБ (потолок {MEMORY_CAP_MB:,.0f} МБ)")
    _log.info(
        "Замер плюса: расчёт завершён",
        plusexit_rows_written=written,
        plusexit_control_compared=compared,
        plusexit_control_mismatched=0,
        peak_rss_mb=round(peak, 1),
    )

    if args.json:
        summary = {
            "generated_at": now.isoformat(timespec="seconds"),
            "logic_version": logic_version,
            "sample": len(kept),
            "measured": len(measured_ids),
            "insufficient_future_bars": len(short),
            "no_data_positions": len(no_data_ids),
            "control_compared": compared,
            "control_mismatched": 0,
            "min_sample": MIN_SAMPLE,
            "outcomes": tables,
            "money": money,
            "slots": slot_rows,
            "blocked": blocked_rows,
            "recovery": recovery,
            "regime_warning": REGIME_WARNING,
            "rows_written": written,
            "peak_rss_mb": round(peak, 1),
        }
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, ensure_ascii=False, indent=2, default=str)
        print(f"  Сводка сохранена: {args.json}")

    if peak > MEMORY_CAP_MB:
        # ПОТОЛОК ПАМЯТИ — ЗАЩИТА, А НЕ ПОЖЕЛАНИЕ (§8.4 ТЗ). На Этапе 9.1.3
        # расчёт был убит ядром, и выглядело это как «чисел нет», а не как
        # «превышен потолок».
        print()
        print(f"  ОТКАЗ: пиковая память {peak:,.0f} МБ превысила потолок "
              f"{MEMORY_CAP_MB:,.0f} МБ (§8.4 ТЗ).")
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
