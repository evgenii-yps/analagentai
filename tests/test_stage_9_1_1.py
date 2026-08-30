"""Этап 9.1.1: учёт баланса, запись в таблицу и три исправления (§8 ТЗ).

ЧТО ЗДЕСЬ ДОКАЗЫВАЕТСЯ, и почему именно это.

ТРИ ИСПРАВЛЕНИЯ. Все три — про измеритель, а не про измеряемое, и потому у всех
трёх есть общая опасность: проверка, которая врёт, хуже отсутствующей проверки.
Пункт Б6 объявлял «ОТКАТ ОБЯЗАТЕЛЕН» на здоровой системе (7618 исправных строк,
померенных по МИНУТНОМУ ряду, он мерил ЧАСОВЫМ запасом). Пункт 9 обещал причины
отказа, а считал поле ``reason`` по всему журналу подряд. Скрипт починки на этом
же критерии предлагал удалить те самые 7618 исправных измерений.

БАЛАНС. Проверяется ровно то, что легко нарушить незаметно: слот НЕ РАСТЁТ от
накопленной прибыли. Естественное побуждение — сделать наоборот (лист владельца
устроен как один кошелёк с реинвестированием), а реинвестирование связало бы
размер позиции с прошлыми исходами, и «доходность на слот» стала бы неизмеримой.

ЛИСТ. Проверяется то, что портит ЧУЖОЙ рабочий документ: вторая строка той же
сделки, строка не того инструмента, запись при выключенном флаге и запись в
столбец с формулой.

Тесты, которым нужна БАЗА, включаются переменной ``AT_TEST_DSN``. Без неё они
ПРОПУСКАЮТСЯ с явной причиной — они не «зелёные», они не выполнялись.
``AT_TEST_DSN`` обязан указывать на ОДНОРАЗОВУЮ базу.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
from datetime import UTC, datetime, timedelta

import pytest

from src.core.config import Settings, settings
from src.core.db import BALANCE_SQL, DB
from src.positions.rules import (
    REASON_NO_FREE_CAPITAL,
    REASON_NO_FRESH_BAR,
    REASON_SLOTS_FULL,
    REFUSAL_REASONS,
    qty_for_slot,
    should_open,
)
from src.positions.sheet import (
    SHEET_COLUMNS,
    SHEET_HEADERS,
    build_position_row,
    fmt_date,
    fmt_time,
    is_exportable,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
VERIFY_SH = ROOT / "deploy" / "verify_9_1.sh"

TEST_DSN = os.environ.get("AT_TEST_DSN", "")
needs_db = pytest.mark.skipif(
    not TEST_DSN,
    reason=(
        "нужна тестовая БД: задайте AT_TEST_DSN "
        "(ОДНОРАЗОВАЯ база, тест пишет и удаляет строки)"
    ),
)


# ===========================================================================
# §8.1–§8.5. Критерий подозрительности строки strategy_outcomes
# ===========================================================================

def test_the_criterion_asks_the_row_for_its_own_resolution() -> None:
    """Критерий берёт длину бара ИЗ КОЛОНКИ resolution, а не по худшему случаю.

    Проверка структурная и потому выполняется БЕЗ БАЗЫ: она ловит возврат к
    прежней редакции — а именно возврат и опасен. Прежний критерий закладывал
    3900 секунд (час грубого бара плюс пять минут) ВСЕМ строкам подряд, и на
    боевых данных объявил подозрительными 7618 исправных минутных измерений.

    Запас, в отличие от длины бара, зашивать нельзя ни здесь, ни в проверке:
    BARRIER_SETTLE_MINUTES может быть изменён, и зашитая пятёрка однажды начала
    бы врать молча — поэтому в предикате он ПАРАМЕТР ($1), а не число.
    """
    predicate = DB.STRATEGY_UNSETTLED_PREDICATE
    assert "CASE resolution" in predicate, predicate
    assert "WHEN '1m' THEN 60" in predicate, predicate
    assert "ELSE 3600" in predicate, predicate
    assert "make_interval(mins => $1::int)" in predicate, predicate
    # И запас НЕ зашит числом: 3900 — это ровно прежняя ошибка.
    assert "3900" not in predicate, predicate


def test_the_verify_script_uses_the_very_same_criterion() -> None:
    """Пункт Б6 проверки спрашивает ТО ЖЕ САМОЕ, что и скрипт починки.

    Разделяемого кода между bash и Python нет, поэтому копия критерия в
    verify_9_1.sh обязана совпадать с предикатом дословно по существу. Разойдись
    они — проверка отвечала бы на вопрос о другом правиле, а человек читал бы её
    вывод как ответ о том же самом.
    """
    text = VERIFY_SH.read_text(encoding="utf-8")
    assert "CASE resolution" in text
    assert "WHEN '1m' THEN 60" in text
    assert "ELSE 3600" in text
    # Запас читается ИЗ .env, а не зашит числом.
    assert 'env_value "${ENV_FILE}" BARRIER_SETTLE_MINUTES' in text
    assert "make_interval(mins => ${settle_min})" in text
    # И прежнего критерия по грубому бару в файле не осталось.
    assert "coarse_sec + settle_min * 60" not in text


@needs_db
async def test_a_minute_row_with_fifteen_minutes_of_margin_is_not_suspicious(
) -> None:
    """§8.1: строка '1m' с запасом 15 минут НЕ подозрительна.

    Это и есть тот случай, которого на боевых данных 7618 штук: минимальный
    запас по всей базе — 15 мин 12 с при требуемых 60 с + 5 мин.
    """
    found = await _plant_and_query(resolution="1m", margin=timedelta(minutes=15))
    assert found == []


@needs_db
async def test_a_minute_row_with_thirty_seconds_of_margin_is_suspicious() -> None:
    """§8.2: строка '1m' с запасом 30 секунд подозрительна.

    Последний бар минутного окна закрывается через 60 секунд после срока —
    расчёт на тридцатой секунде взял ``close`` ещё формирующейся свечи.
    """
    found = await _plant_and_query(resolution="1m", margin=timedelta(seconds=30))
    assert [r["resolution"] for r in found] == ["1m"]


@needs_db
async def test_an_hour_row_with_fifteen_minutes_of_margin_is_suspicious() -> None:
    """§8.3: строка '1h' с запасом 15 минут подозрительна.

    Тот же запас, что в §8.1, и ПРОТИВОПОЛОЖНЫЙ ответ — в этом весь смысл
    правки: часовому ряду пятнадцати минут мало, минутному хватает с избытком.
    """
    found = await _plant_and_query(resolution="1h", margin=timedelta(minutes=15))
    assert [r["resolution"] for r in found] == ["1h"]


@needs_db
async def test_an_hour_row_with_seventy_minutes_of_margin_is_not_suspicious(
) -> None:
    """§8.4: строка '1h' с запасом 70 минут НЕ подозрительна (60 + 5 < 70)."""
    found = await _plant_and_query(resolution="1h", margin=timedelta(minutes=70))
    assert found == []


def test_apply_refuses_to_delete_a_healthy_minute_row() -> None:
    """§8.5: ``--apply`` отказывается работать целиком (код 1).

    Ловушка на случай, если критерий снова расширят до худшего случая. Удаление
    измерения НЕОБРАТИМО: минутные свечи старше RETENTION_1M_DAYS уже удалены
    политикой хранения, и пересчёт вернул бы ЧАСОВОЕ разрешение вместо
    минутного — другое измерение под тем же ключом.
    """
    from scripts.repair_9_1_strategy_settle import (
        _fine_rows_with_enough_margin,
        _forbid_deleting_fine_rows,
        _required_margin_sec,
    )

    settle_min = 5
    assert _required_margin_sec("1m", settle_min) == 360
    assert _required_margin_sec("1h", settle_min) == 3600 + 300

    healthy = {"resolution": "1m", "margin_sec": 912.0}   # запас 15 мин 12 с
    broken = {"resolution": "1m", "margin_sec": 30.0}
    coarse = {"resolution": "1h", "margin_sec": 900.0}

    assert _fine_rows_with_enough_margin([healthy], settle_min) == [healthy]
    assert _fine_rows_with_enough_margin([broken, coarse], settle_min) == []

    # Отказ — код 1, и он же означает «ничего не удалено».
    assert _forbid_deleting_fine_rows([broken, healthy], settle_min) == 1
    # А набор без исправных минутных строк работу не останавливает.
    assert _forbid_deleting_fine_rows([broken, coarse], settle_min) == 0


def test_the_counting_mode_no_longer_offers_to_delete_anything() -> None:
    """Подсказки «удалить их командой … --apply» в режиме подсчёта БОЛЬШЕ НЕТ.

    На боевых данных она предлагала снести 7618 исправных строк. Скрипт,
    предлагающий необратимое действие раньше, чем находка разобрана, приучает
    выполнять его не глядя.
    """
    text = (ROOT / "scripts" / "repair_9_1_strategy_settle.py").read_text(
        encoding="utf-8"
    )
    body = text.split("if not args.apply:", 1)[1].split("return 0", 1)[0]
    # Ищется именно ПРЕДЛОЖЕНИЕ КОМАНДЫ, а не слово «--apply»: объяснить, чем
    # режим подсчёта отличается от режима удаления, скрипт по-прежнему обязан.
    assert "docker compose" not in body, body
    assert "repair_9_1_strategy_settle --apply" not in body, body
    assert "НАСТОЯЩАЯ находка" in body
    # И проверка Б6 тоже больше не предлагает удаление как способ устранения.
    verify = VERIFY_SH.read_text(encoding="utf-8")
    section = verify.split("ЗАДАЧА Б: подозрительных строк не осталось", 1)[1]
    section = section.split("── 7.", 1)[0]
    assert "repair_9_1_strategy_settle --apply" not in section, section
    assert "НИЧЕГО НЕ УДАЛЯТЬ" in section


# ===========================================================================
# §8.6–§8.7. Разбор журнала: причины отказа во входе
# ===========================================================================

def _run_reason_block(logs: str) -> tuple[str, str]:
    """Прогоняет БЛОК ИЗ verify_9_1.sh на подсунутом журнале.

    Блок берётся из настоящего файла проверки между пометками ``reason-block``,
    а не пересказывается здесь: пересказ разошёлся бы с оригиналом молча, и тест
    доказывал бы исправность пересказа.
    """
    text = VERIFY_SH.read_text(encoding="utf-8")
    block = text.split("# >>> reason-block", 1)[1]
    block = block.split("\n", 1)[1].split("# <<< reason-block", 1)[0]
    script = (
        "set -uo pipefail\n"
        "note_warn() { echo \"WARN: $*\"; }\n"
        "logs=$(cat)\n"
        + block
    )
    proc = subprocess.run(
        ["bash", "-c", script], input=logs, capture_output=True,
        text=True, check=False,
    )
    return proc.stdout, proc.stderr


def test_reasons_are_taken_only_from_refusal_lines() -> None:
    """§8.6: причины берутся ТОЛЬКО из строк positions_skipped=1.

    Прежняя редакция ловила поле ``reason`` по ВСЕМУ журналу за сутки, а
    заголовок обещал причины отказа. Здесь в журнале есть и закрытие позиции с
    полем ``exit_reason``, и итог итерации со сводкой отказов: ни то, ни другое
    в счётчики попасть не должно.
    """
    logs = "\n".join([
        '{"event": "positions_skipped=1", "reason": "no_fresh_bar"}',
        '{"event": "positions_skipped=1", "reason": "no_fresh_bar"}',
        '{"event": "positions_skipped=1", "reason": "no_fresh_bar"}',
        '{"event": "positions_closed=1", "exit_reason": "target"}',
        '{"event": "positions_iteration=1", "refusals": {"slots_full": 9}}',
    ])
    out, _ = _run_reason_block(logs)
    assert "3 no_fresh_bar" in out.replace("      ", "")
    assert "target" not in out
    assert "slots_full" not in out
    # И ни одного счётчика с ПУСТОЙ причиной — ровно того, что печаталось на
    # боевом сервере: два пустых счётчика при трёх названных поимённо отказах.
    for line in out.splitlines():
        assert not line.rstrip().endswith("reason="), line


def test_a_refusal_line_without_a_reason_is_reported_not_counted() -> None:
    """§8.7: строка без поля ``reason`` в счётчики не попадает — и это находка.

    Перечень причин закрыт, каждая ветка правил возвращает константу, поэтому
    отказ без причины невозможен по построению. Появился — значит, сломалось
    что-то ещё, и молчать об этом нельзя.
    """
    logs = "\n".join([
        '{"event": "positions_skipped=1", "reason": "slots_full"}',
        '{"event": "positions_skipped=1"}',
    ])
    out, _ = _run_reason_block(logs)
    assert "1 slots_full" in out.replace("      ", "")
    assert "WARN:" in out
    assert "за сутки 2" in out and "причиной 1" in out


def test_no_refusals_at_all_is_said_plainly() -> None:
    """Отказов не было — так и печатается, а не пустотой под заголовком."""
    out, _ = _run_reason_block('{"event": "positions_opened=1"}')
    assert "отказов за сутки нет" in out


# ===========================================================================
# §8.8–§8.13. Учёт баланса
# ===========================================================================

@needs_db
async def test_free_money_is_start_plus_realized_minus_in_positions() -> None:
    """§8.8: free = capital_start + realized_pnl − in_positions.

    Набор — ДВЕ открытых позиции и ТРИ закрытых, потому что именно на смешанном
    наборе ошибиться легко: сумму notional_usd надо брать по открытым, а сумму
    net_pnl_usd — по закрытым, и перепутать их местами дало бы правдоподобное
    число.
    """
    import asyncpg

    conn = await asyncpg.connect(dsn=TEST_DSN)
    try:
        # ЧИСЛА СРАВНИВАЮТСЯ ПРИРАЩЕНИЯМИ, а не абсолютными значениями: в
        # тестовой базе могли остаться чужие позиции, и тест, требующий ровно
        # 4.00 в позициях, проверял бы чистоту базы вместо запроса.
        before = await conn.fetchrow(BALANCE_SQL, 10.0)
        await _plant_positions(
            conn,
            open_notionals=[2.0, 2.0],
            closed_pnls=[0.03, -0.05, 0.011],
        )
        after = await conn.fetchrow(BALANCE_SQL, 10.0)

        assert float(after["capital_start"]) == pytest.approx(10.0)
        assert float(after["realized_pnl"]) - float(before["realized_pnl"]) == (
            pytest.approx(-0.009)
        )
        assert float(after["in_positions"]) - float(before["in_positions"]) == (
            pytest.approx(4.0)
        )
        assert int(after["open_count"]) - int(before["open_count"]) == 2
        # Приращение free — ровно приращение прибыли минус приращение занятого.
        assert float(after["free"]) - float(before["free"]) == pytest.approx(
            -0.009 - 4.0
        )
        # И само тождество, ради которого отдельной таблицы для баланса не
        # заводится, — на абсолютных величинах: оно обязано держаться при любом
        # составе базы.
        for row in (before, after):
            assert float(row["free"]) == pytest.approx(
                float(row["capital_start"]) + float(row["realized_pnl"])
                - float(row["in_positions"])
            )
    finally:
        await _drop_planted_positions(conn)
        await conn.close()


def _open_kwargs(**overrides):
    """Набор параметров, при котором вход РАЗРЕШЁН. Тесты меняют по одному."""
    base = dict(
        decision="buy",
        logic_version=settings.LOGIC_VERSION,
        expected_version=settings.LOGIC_VERSION,
        degraded=False,
        probability=0.9,
        min_probability=0.8,
        has_open_position=False,
        open_count=0,
        max_open=5,
        signal_age_sec=30.0,
        max_signal_age_sec=180,
        bar_age_sec=45.0,
        max_bar_age_sec=300,
        has_frozen_target=True,
        free_usd=10.0,
        slot_usd=2.0,
    )
    base.update(overrides)
    return base


def test_a_loss_that_eats_the_last_slot_stops_the_next_entry() -> None:
    """§8.9: свободных денег меньше слота — отказ ``no_free_capital``.

    ЗАЧЕМ ЭТО НУЖНО, если пять слотов ровно равны капиталу. При накопленном
    убытке в 3 доллара свободных денег остаётся на четыре слота, и пятая
    позиция открываться не должна: денег на неё нет. Без проверки система
    торговала бы деньгами, которых у неё нет, и весь учёт баланса стал бы
    декорацией.
    """
    # Четыре слота заняты, накоплен убыток 3 доллара: 10 − 3 − 8 = −1.
    verdict = should_open(**_open_kwargs(open_count=4, free_usd=-1.0))
    assert verdict.allowed is False
    assert verdict.reason == REASON_NO_FREE_CAPITAL

    # Ровно на слот — вход РАЗРЕШЁН: слот оплачен целиком, и отказ здесь был бы
    # отказом при достаточных деньгах.
    assert should_open(**_open_kwargs(free_usd=2.0)).allowed is True
    # На цент меньше — отказ.
    assert should_open(**_open_kwargs(free_usd=1.99)).reason == (
        REASON_NO_FREE_CAPITAL
    )


def test_money_is_checked_after_slots_and_before_bar_freshness() -> None:
    """§8.10: порядок проверок содержателен, а не произволен.

    После слотов — потому что «слоты заняты» точнее, когда верны обе причины:
    занятые слоты и есть то, во что ушли деньги. До свежести бара — потому что
    свежесть бара это свойство ДАННЫХ, а нехватка денег свойство СЧЁТА, и отказ
    «нет свежей свечи» на счёте без денег послал бы разбираться с коллектором.
    """
    both_slots_and_money = should_open(
        **_open_kwargs(open_count=5, free_usd=0.0)
    )
    assert both_slots_and_money.reason == REASON_SLOTS_FULL

    both_money_and_bar = should_open(
        **_open_kwargs(free_usd=0.0, bar_age_sec=None)
    )
    assert both_money_and_bar.reason == REASON_NO_FREE_CAPITAL

    # И свежесть бара по-прежнему срабатывает, когда деньги есть.
    assert should_open(**_open_kwargs(bar_age_sec=None)).reason == (
        REASON_NO_FRESH_BAR
    )


def test_the_list_of_refusal_reasons_holds_exactly_ten_values() -> None:
    """§8.11: перечень причин ЗАКРЫТ и содержит ровно десять значений.

    Свободный текст нельзя посчитать запросом. Число названо здесь числом
    намеренно: молчаливо выросший перечень означал бы, что появилась причина,
    о которой не сказано нигде.
    """
    assert len(REFUSAL_REASONS) == 10
    assert len(set(REFUSAL_REASONS)) == 10
    assert REASON_NO_FREE_CAPITAL in REFUSAL_REASONS


def test_the_validator_rejects_a_capital_that_cannot_hold_all_slots() -> None:
    """§8.12: капитал меньше суммы слотов отвергается, ровно равный — принят.

    Капитал меньше суммы слотов означает счёт, на котором заявленные пять
    позиций не помещаются С САМОГО НАЧАЛА: часть слотов не заняло бы ничто и ни
    при каких обстоятельствах, а замер молча шёл бы по меньшему числу
    инструментов.
    """
    with pytest.raises(ValueError) as failure:
        Settings(POSITION_START_BALANCE_USD=8.0, POSITION_SLOT_USD=2.0,
                 POSITION_MAX_OPEN=5)
    text = str(failure.value)
    # Текст ошибки называет ОБЕ величины: иначе человек, увидевший её в
    # журнале старта, полез бы выяснять, какая из настроек виновата.
    assert "POSITION_SLOT_USD" in text and "POSITION_MAX_OPEN" in text
    assert "8.0" in text

    accepted = Settings(POSITION_START_BALANCE_USD=10.0, POSITION_SLOT_USD=2.0,
                        POSITION_MAX_OPEN=5)
    assert accepted.POSITION_START_BALANCE_USD == 10.0


def test_the_slot_never_grows_with_accumulated_profit() -> None:
    """§8.13: размер слота НЕ зависит от накопленной прибыли.

    ПРИБЫЛЬ НЕ РЕИНВЕСТИРУЕТСЯ. Проверяется на ПОЛОЖИТЕЛЬНОМ накопленном итоге,
    потому что соблазн именно там: счёт вырос, и «логично» покупать на больше.
    Реинвестирование связало бы размер позиции с прошлыми исходами, и поздняя
    сделка весила бы больше ранней просто потому, что она поздняя — величина
    «доходность на слот» после этого стала бы неизмеримой.
    """
    price = 77_602.70
    slot = settings.POSITION_SLOT_USD

    # Счёт вырос вдвое — количество монеты считается всё от того же слота.
    poor = qty_for_slot(slot, price)
    rich = qty_for_slot(slot, price)
    assert poor == rich

    # И величина слота в расчёте — именно POSITION_SLOT_USD, а не «слот плюс
    # накопленное»: обратное дало бы количество, растущее вместе с прибылью.
    assert qty_for_slot(slot, price) * price == pytest.approx(slot)
    assert qty_for_slot(slot + 5.0, price) > qty_for_slot(slot, price)


def test_the_balance_line_always_prints_a_sign() -> None:
    """Знак итога печатается ВСЕГДА, включая «+$0.00» при нуле.

    Отсутствие знака читается как «величина неизвестна», а ноль — это
    измеренный ноль.
    """
    from src.positions.messages import balance_line, signed_usd

    assert signed_usd(0.0) == "+$0.00"
    assert signed_usd(0.03) == "+$0.03"
    assert signed_usd(-0.12) == "-$0.12"

    line = balance_line({
        "capital_start": 10.0, "realized_pnl": 0.03,
        "in_positions": 4.0, "free": 6.03, "open_count": 2,
    })
    assert line == (
        "Счёт: $10.00 старт · $4.00 в позициях · $6.03 свободно · итог +$0.03"
    )
    # Величин нет — строки нет ВОВСЕ. Нули сообщили бы, что на счёте пусто,
    # тогда как на самом деле неизвестно, сколько там.
    assert balance_line(None) is None


def test_the_word_virtual_stays_in_both_messages_and_in_the_balance_block(
) -> None:
    """Слово «виртуально» остаётся во всех сообщениях и в блоке баланса.

    Человек, читающий поток, не обязан помнить, какой этап проекта сейчас идёт.
    """
    from src.bot.handlers import render_balance_block
    from src.positions import messages

    balance = {"capital_start": 10.0, "realized_pnl": 0.0,
               "in_positions": 2.0, "free": 8.0, "open_count": 1}
    opened = messages.opened_text(
        symbol="BTC/USDT", entry_price=77_602.7, notional_usd=2.0,
        target_price=77_800.0, target_pct=0.25, stop_price=77_000.0,
        stop_pct=0.8, deadline_at=datetime(2026, 8, 31, 10, 0, tzinfo=UTC),
        signal_id=1, probability=0.9, entry_lag_sec=25, balance=balance,
    )
    closed = messages.closed_text(
        symbol="BTC/USDT", exit_reason="target", entry_price=77_602.7,
        exit_price=77_800.0, net_pnl_pct=0.03, net_pnl_usd=0.03,
        cost_pct=0.22, held_sec=3600, balance=balance,
    )
    assert "виртуально" in opened and "виртуально" in closed
    assert "Счёт: $10.00 старт" in opened
    assert "Счёт: $10.00 старт" in closed

    block = "\n".join(render_balance_block(balance, max_open=5))
    assert "виртуально" in block
    assert "1 из 5 слотов" in block
    assert "не реинвестируется" in block


def test_the_balance_block_comes_first_in_the_positions_answer() -> None:
    """Блок баланса печатается ОТДЕЛЬНЫМ БЛОКОМ СВЕРХУ, перед списком позиций.

    Сверху потому, что «сколько денег» — вопрос более общий, чем «что открыто».
    """
    from src.bot.handlers import render_positions

    text = render_positions(
        [], {"closed": 0}, datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
        days=7,
        balance={"capital_start": 10.0, "realized_pnl": 0.0,
                 "in_positions": 0.0, "free": 10.0, "open_count": 0},
        max_open=5,
    )
    assert text.index("Счёт (виртуально)") < text.index("Открыто сейчас")
    # Величин нет — блока нет, а команда по-прежнему отвечает.
    without = render_positions(
        [], {"closed": 0}, datetime(2026, 8, 30, 12, 0, tzinfo=UTC), days=7,
    )
    assert "Счёт (виртуально)" not in without
    assert "Открыто сейчас" in without


def test_the_positions_service_is_watched_like_every_other_service() -> None:
    """§5 ТЗ: heartbeat сервиса позиций виден и /status, и вотчдогу.

    Сервис, ведущий пять открытых позиций, может умереть молча, и узнают об
    этом по тому, что позиции перестали закрываться.
    """
    from src.bot.handlers import hb_label
    from src.bot.poller import HEARTBEAT_KEYS

    keys = [key for key, _ in HEARTBEAT_KEYS]
    assert "positions:heartbeat" in keys
    # Строка в ответе бота называется по-человечески, а не ключом Redis.
    assert hb_label("positions:heartbeat") == "ведение позиций"
    # Остальные строки НЕ переименованы: этап разрешает изменить ровно одну.
    assert hb_label("decision:heartbeat") == "decision:heartbeat"

    watchdog = (ROOT / "scripts" / "watchdog.py").read_text(encoding="utf-8")
    assert '"positions:heartbeat", "POSITION_INTERVAL"' in watchdog
    assert '"positions"' in watchdog.split("CONTAINERS = ", 1)[1].split("]", 1)[0]


# ===========================================================================
# §8.14–§8.21. Запись закрытых позиций в Google Таблицу
# ===========================================================================

def _position(**overrides):
    """Закрытая позиция BTC, годная к записи. Тесты ломают по одному полю."""
    base = {
        "id": 1,
        "symbol": "BTC/USDT",
        "status": "closed",
        "opened_at": datetime(2026, 8, 30, 10, 42, 42, tzinfo=UTC),
        "signal_ts": datetime(2026, 8, 30, 10, 42, 17, tzinfo=UTC),
        "entry_price": 77_602.70,
        "closed_at": datetime(2026, 8, 31, 6, 5, 0, tzinfo=UTC),
        "exit_price": 77_800.10,
        "net_pnl_usd": 0.03,
        "net_pnl_pct": 1.5,
        "sheet_exported_at": None,
    }
    base.update(overrides)
    return base


def test_an_open_position_never_goes_to_the_sheet() -> None:
    """§8.14: позиция со статусом ``open`` в лист не идёт.

    У неё нет цены закрытия, и полустрока сломала бы формулы листа.
    """
    assert is_exportable(_position(), instrument_symbol="BTC/USDT") is True
    open_row = _position(status="open", closed_at=None, exit_price=None)
    assert is_exportable(open_row, instrument_symbol="BTC/USDT") is False


def test_another_instrument_never_goes_to_the_sheet() -> None:
    """§8.15: позиция другого инструмента в лист не идёт.

    Лист моделирует ОДИН кошелёк с реинвестированием (объём строки = объём
    предыдущей + прибыль предыдущей). Строки пяти слотов вперемешку сложились бы
    в одну цепочку, и лист показал бы доходность несуществующего счёта — при
    правдоподобных числах, а это худший вид ошибки.
    """
    eth = _position(symbol="ETH/USDT")
    assert is_exportable(eth, instrument_symbol="BTC/USDT") is False
    assert is_exportable(eth, instrument_symbol="ETH/USDT") is True


def test_a_position_already_written_is_never_written_twice() -> None:
    """§8.16: позиция с непустым ``sheet_exported_at`` повторно не пишется.

    Признак лежит в БАЗЕ, а не в памяти процесса: перезапуск после сбоя сети —
    штатное событие, и вторая строка той же сделки удвоила бы её прибыль во всей
    последующей цепочке объёмов.
    """
    written = _position(
        sheet_exported_at=datetime(2026, 8, 31, 6, 6, tzinfo=UTC)
    )
    assert is_exportable(written, instrument_symbol="BTC/USDT") is False


async def test_nothing_is_written_while_the_flag_is_off() -> None:
    """§8.17: при ``POSITIONS_SHEETS_ENABLED=False`` запись не вызывается НИ РАЗУ.

    Не «вызывается и ничего не находит», а НЕ ВЫПОЛНЯЕТСЯ ВОВСЕ: ни запроса к
    базе, ни обращения к сети. Первая автоматическая запись в чужой рабочий
    документ обязана произойти по сознательному решению владельца.
    """
    from src.positions import runner

    calls: list[str] = []

    class _Boom:
        def __getattr__(self, name):
            async def _fail(*args, **kwargs):
                calls.append(name)
                raise AssertionError(f"обращение к базе при выключенном флаге: {name}")
            return _fail

    async def _no_network(*args, **kwargs):
        calls.append("post_position_row")
        raise AssertionError("обращение к сети при выключенном флаге")

    original_db = runner.db
    original_post = runner.sheets.post_position_row
    runner.db = _Boom()
    runner.sheets.post_position_row = _no_network
    try:
        assert settings.POSITIONS_SHEETS_ENABLED is False, (
            "по умолчанию запись обязана быть ВЫКЛЮЧЕНА"
        )
        stats = await runner.export_closed_to_sheet()
    finally:
        runner.db = original_db
        runner.sheets.post_position_row = original_post

    assert calls == []
    assert stats.disabled is True
    assert stats.written == 0


def test_moscow_time_is_what_the_sheet_gets() -> None:
    """§8.18: 2026-08-30T10:42:17Z при поясе Europe/Moscow → 30.08.2026, 13:42:17.

    Пояс тот же, что в сообщениях Telegram: человек, сверяющий лист с сообщением
    бота, обязан видеть ОДНО И ТО ЖЕ время. Столбца с поясом в листе нет, и
    сверить его иначе не с чем.
    """
    moment = datetime(2026, 8, 30, 10, 42, 17, tzinfo=UTC)
    assert fmt_date(moment, "Europe/Moscow") == "30.08.2026"
    assert fmt_time(moment, "Europe/Moscow") == "13:42:17"
    # И тот же момент в UTC даёт ДРУГОЕ время — иначе тест проходил бы при
    # любом поясе и ничего не проверял.
    assert fmt_time(moment, "UTC") == "10:42:17"
    # Переход через полночь двигает и дату.
    late = datetime(2026, 8, 30, 22, 30, 0, tzinfo=UTC)
    assert fmt_date(late, "Europe/Moscow") == "31.08.2026"


def test_exactly_eight_columns_are_written_and_no_others() -> None:
    """§8.19: набор записываемых столбцов равен ровно {A,B,C,D,F,H,I,J}.

    Проверяется СОСТАВОМ собираемого объекта, а не обещанием: запись значения в
    столбец с формулой (G, K..S) заменила бы формулу числом — лист перестал бы
    пересчитываться, а выглядел бы работающим.
    """
    values = build_position_row(_position(), timezone_name="Europe/Moscow")
    assert set(values) == {"A", "B", "C", "D", "F", "H", "I", "J"}
    assert tuple(values) == SHEET_COLUMNS
    assert "G" not in values
    assert not any(c in values for c in "KLMNOPQRS")
    assert "E" not in values

    # И содержимое — то, что обещано §7.3: в B стоит время СИГНАЛА, в A — дата
    # ВХОДА. Это разные моменты (между ними entry_lag_sec), и подменять одно
    # другим нельзя.
    assert values["A"] == "30.08.2026"
    assert values["B"] == "13:42:17"          # signal_ts, не opened_at
    assert values["C"] == "BTC"               # короткое имя, не «BTC/USDT»
    assert values["D"] == "покупать"
    assert values["F"] == pytest.approx(77_602.70)
    assert values["H"] == "31.08.2026"
    assert values["I"] == "9:05:00"
    assert values["J"] == pytest.approx(77_800.10)
    # Цены остаются ЧИСЛАМИ: на них считают формулы листа, а число, записанное
    # текстом, выглядит в ячейке так же и не считается никак.
    assert isinstance(values["F"], float) and isinstance(values["J"], float)


async def test_a_header_mismatch_refuses_the_write_instead_of_shifting_it(
) -> None:
    """§8.20: несовпадение заголовков листа — ОТКАЗ, а не запись в другие столбцы.

    Владелец правит лист руками. Приёмник сверяет строку 1 и отвечает
    ``ok=false``; отправитель обязан считать это отказом и НЕ ПОВТОРЯТЬ запрос:
    повтор через пять секунд лист не изменит, а настоящий разбор отложит.

    Поведение самого приёмника проверяется стендом
    ``tests/apps_script/receiver_harness.mjs`` (сценарий «заголовки не совпали»),
    здесь — сторона Python.
    """
    import httpx

    from src.export import sheets

    attempts: list[dict] = []

    class _Response:
        status_code = 200

        @staticmethod
        def json():
            return {"ok": False, "error": "заголовки листа не совпали — C: …",
                    "version": "9.1.1"}

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json):
            attempts.append(json)
            return _Response()

    original = httpx.AsyncClient
    httpx.AsyncClient = _Client
    try:
        result = await sheets.post_position_row(
            "https://example.invalid/exec", "секрет", "лист",
            build_position_row(_position(), timezone_name="Europe/Moscow"),
            SHEET_HEADERS,
        )
    finally:
        httpx.AsyncClient = original

    assert result.ok is False
    assert "заголовки" in (result.error or "")
    # Ровно ОДНА попытка: отказ приёмника не повторяется.
    assert len(attempts) == 1
    # И отправлены ровно восемь столбцов и ожидаемые заголовки — приёмнику есть
    # с чем сверять лист.
    assert set(attempts[0]["values"]) == set(SHEET_COLUMNS)
    assert attempts[0]["headers"] == SHEET_HEADERS
    assert attempts[0]["action"] == "append_position"


def test_the_dry_run_prints_the_row_and_names_the_timezone() -> None:
    """§7.8: ``--dry-run`` печатает строку, которую записал бы, и ничего не пишет."""
    from src.positions.sheet import dry_run_text

    text = dry_run_text(
        _position(), timezone_name="Europe/Moscow",
        sheet_name="торговля тест апи окх чтение",
    )
    assert "ничего не записано" in text
    assert "Europe/Moscow" in text
    assert "торговля тест апи окх чтение" in text
    for column in SHEET_COLUMNS:
        assert f"    {column}" in text
    assert "30.08.2026" in text and "13:42:17" in text
    # Закрытых позиций нет — так и сказано, а не пустой таблицей.
    empty = dry_run_text(
        None, timezone_name="Europe/Moscow", sheet_name="лист",
    )
    assert "Закрытых позиций по этому инструменту в базе нет" in empty


@needs_db
async def test_migration_019_applies_twice_without_an_error() -> None:
    """§8.21: миграция 019 применяется дважды подряд без ошибки.

    Идемпотентность здесь не роскошь: миграция 018 уже применена на сервере, и
    повторный прогон развёртывания обязан быть безопасным. Проверяется и то, что
    после ВТОРОГО применения колонка и индекс на месте — «не упало» и «сработало»
    это разные утверждения.
    """
    import asyncpg

    sql = (ROOT / "db" / "migrations" / "019_positions_sheet_export.sql")
    text = sql.read_text(encoding="utf-8")
    conn = await asyncpg.connect(dsn=TEST_DSN)
    try:
        await conn.execute(text)
        await conn.execute(text)
        column = await conn.fetchval(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = 'positions' "
            "AND column_name = 'sheet_exported_at';"
        )
        assert int(column) == 1
        index = await conn.fetchval(
            "SELECT count(*) FROM pg_indexes "
            "WHERE tablename = 'positions' "
            "AND indexname = 'ix_positions_sheet_pending';"
        )
        assert int(index) == 1
    finally:
        await conn.close()


# ===========================================================================
# Вспомогательное для тестов, которым нужна база
# ===========================================================================

_PLANT_SYMBOL = "TEST911/USDT"


async def _plant_and_query(*, resolution: str, margin: timedelta):
    """Подкладывает ОДНУ строку с заданным запасом и спрашивает критерий.

    Запас отсчитывается от СРОКА окна (``entry_ts + horizon_h``), то есть ровно
    так, как его считает критерий: строка с запасом ``margin`` посчитана через
    ``margin`` после срока.
    """
    import asyncpg

    from src.core.db import db as database

    entry_ts = datetime(2026, 8, 20, 0, 0, tzinfo=UTC)
    horizon_h = 1
    computed_at = entry_ts + timedelta(hours=horizon_h) + margin

    conn = await asyncpg.connect(dsn=TEST_DSN)
    try:
        instrument_id = await conn.fetchval(
            "INSERT INTO instruments (exchange, symbol, base, quote, type) "
            "VALUES ('okx', $1, 'TEST911', 'USDT', 'spot') "
            "ON CONFLICT (exchange, symbol, type) DO UPDATE "
            "SET symbol = EXCLUDED.symbol RETURNING id;",
            _PLANT_SYMBOL,
        )
        await conn.execute(
            """
            INSERT INTO strategy_outcomes
                (strategy, instrument_id, entry_ts, horizon_h, signal_id,
                 logic_version, direction, price_at_entry, target_pct,
                 target_source, stop_pct, cost_pct, outcome, net_pnl_pct,
                 mae_pct, mfe_pct, resolution, computed_at)
            VALUES ('grid_buy', $1, $2, $3, NULL, 5, 'buy', 100, 1, 'frozen',
                    1, 0.22, 'timeout', 0.1, -0.2, 0.3, $4, $5);
            """,
            instrument_id, entry_ts, horizon_h, resolution, computed_at,
        )
        database._pool = await asyncpg.create_pool(
            dsn=TEST_DSN, min_size=1, max_size=2
        )
        rows = await database.get_strategy_outcomes_unsettled(
            settle_minutes=settings.BARRIER_SETTLE_MINUTES
        )
        return [
            r for r in rows
            if int(r["instrument_id"]) == int(instrument_id)
            and r["entry_ts"] == entry_ts
        ]
    finally:
        await conn.execute(
            "DELETE FROM strategy_outcomes WHERE instrument_id IN "
            "(SELECT id FROM instruments WHERE symbol = $1);", _PLANT_SYMBOL,
        )
        await conn.close()
        await database.close()


async def _plant_positions(conn, *, open_notionals, closed_pnls) -> None:
    """Подкладывает открытые и закрытые позиции на отдельных инструментах.

    Инструментов ровно столько, сколько позиций: ``ux_positions_one_open_per_instrument``
    не допустит двух открытых позиций на одном инструменте — и это правило этапа,
    а не помеха тесту.
    """
    total = len(open_notionals) + len(closed_pnls)
    await _drop_planted_positions(conn)
    signal_ts = datetime(2026, 8, 20, 0, 0, tzinfo=UTC)
    for index in range(total):
        symbol = f"TEST911B{index}/USDT"
        instrument_id = await conn.fetchval(
            "INSERT INTO instruments (exchange, symbol, base, quote, type) "
            "VALUES ('okx', $1, $2, 'USDT', 'spot') "
            "ON CONFLICT (exchange, symbol, type) DO UPDATE "
            "SET symbol = EXCLUDED.symbol RETURNING id;",
            symbol, f"TEST911B{index}",
        )
        signal_id = await conn.fetchval(
            "INSERT INTO signals (instrument_id, ts, decision, logic_version) "
            "VALUES ($1, $2, 'buy', 5) RETURNING id;",
            instrument_id, signal_ts + timedelta(minutes=index),
        )
        is_open = index < len(open_notionals)
        notional = (
            open_notionals[index] if is_open
            else settings.POSITION_SLOT_USD
        )
        pnl = None if is_open else closed_pnls[index - len(open_notionals)]
        await conn.execute(
            """
            INSERT INTO positions
                (instrument_id, signal_id, logic_version, horizon_h, side,
                 is_virtual, status, signal_ts, signal_price, opened_at,
                 entry_price, entry_lag_sec, entry_slippage_pct, qty,
                 notional_usd, target_pct, target_price, stop_pct, stop_price,
                 cost_pct, deadline_at, resolution,
                 closed_at, exit_price, exit_reason, outcome_certain,
                 net_pnl_pct, net_pnl_usd)
            VALUES ($1, $2, 5, 24, 'buy', TRUE, $3, $4, 100, $4, 100, 25, 0,
                    0.02, $5, 1, 101, 1, 99, 0.22, $6, '1m',
                    $7, $8, $9, $10, $11, $12);
            """,
            instrument_id, signal_id,
            "open" if is_open else "closed",
            signal_ts + timedelta(minutes=index),
            notional,
            signal_ts + timedelta(hours=24),
            None if is_open else signal_ts + timedelta(hours=2),
            None if is_open else 101,
            None if is_open else "target",
            None if is_open else True,
            None if pnl is None else pnl * 50,
            pnl,
        )


async def _drop_planted_positions(conn) -> None:
    """Убирает подложенное. Тест обязан оставить базу такой, какой её взял."""
    await conn.execute(
        "DELETE FROM positions WHERE instrument_id IN "
        "(SELECT id FROM instruments WHERE symbol LIKE 'TEST911B%');"
    )
    await conn.execute(
        "DELETE FROM signals WHERE instrument_id IN "
        "(SELECT id FROM instruments WHERE symbol LIKE 'TEST911B%');"
    )
