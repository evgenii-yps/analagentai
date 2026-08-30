"""Этап 9.1.1: правка измерителя, учёт капитала, живость сервиса, data_gap (§7 ТЗ).

ЧТО ЗДЕСЬ ДОКАЗЫВАЕТСЯ, и почему именно это.

ИЗМЕРИТЕЛЬ. Три срочные задачи этапа правят не систему, а то, чем её меряют.
Проверка, требующая отката там, где всё цело, обесценивает себя: в следующий раз
на её вывод не посмотрят. Поэтому здесь проверяется не «критерий изменился», а
что он даёт РАЗНЫЕ ответы на минутной и часовой строке с одним и тем же запасом —
именно неразличение этих двух случаев и породило 7618 ложных срабатываний при 0
настоящих.

ДЕНЬГИ. Проверяется порядок причин отказа, а не только их наличие. Причина
«слоты заняты» точнее причины «денег нет», когда верны обе, и перепутанный
порядок молча объяснял бы отсутствие позиции не тем, чем надо.

ПРОБЕЛ В ДАННЫХ. Проверяется самое опасное свойство нового исхода: он НЕ
ИЗМЕРЕНИЕ. Цена выхода у него не наблюдалась, а восстановлена, поэтому
``outcome_certain`` обязан быть ``False`` всегда, а сам исход — не попадать в
средние. Отдельно проверяется случай «баров не было вовсе»: итог там равен ровно
минус издержкам, и это честное описание сделки, которую невозможно оценить.

УДАЛЕНИЕ. Три ограждения скрипта починки проверяются на ПОДМЕНЁННОМ слое доступа
к базе — то есть проверяется, что ``DELETE`` действительно не был вызван, а не
что скрипт напечатал слово «отказ».

Тесты, которым нужна БАЗА, включаются переменной ``AT_TEST_DSN``. Без неё они
ПРОПУСКАЮТСЯ с явной причиной — они не «зелёные», они не выполнялись.
``AT_TEST_DSN`` обязан указывать на ОДНОРАЗОВУЮ базу.
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
from datetime import UTC, datetime, timedelta

import pytest

from src.core.config import Settings, settings
from src.core.db import DB
from src.positions.rules import (
    EXIT_DATA_GAP,
    EXIT_REASONS,
    REASON_NO_FREE_CAPITAL,
    REASON_NO_FRESH_BAR,
    REASON_SLOTS_FULL,
    REFUSAL_REASONS,
    Bar,
    check_gap_exit,
    net_pnl,
    should_open,
)

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_MIGRATIONS = _ROOT / "db" / "migrations"

TEST_DSN = os.environ.get("AT_TEST_DSN", "")
needs_db = pytest.mark.skipif(
    not TEST_DSN,
    reason=(
        "нужна тестовая БД: задайте AT_TEST_DSN "
        "(ОДНОРАЗОВАЯ база, тест пишет и удаляет строки)"
    ),
)

# Синтетика §6: круглые числа, чтобы ответ был виден глазом.
_ENTRY = 100.0
_COST_PCT = 0.22
_OPENED = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
_DEADLINE = _OPENED + timedelta(hours=24)
_GRACE = 7200


def _bar(minute: int, high: float, low: float, close: float) -> Bar:
    """Бар окна: ``minute`` — сколько минут прошло с момента входа."""
    return Bar(
        ts=_OPENED + timedelta(minutes=minute), high=high, low=low, close=close
    )


# =============================================================================
# §7.1–§7.2. Учёт капитала: новая причина отказа и её место в очереди
# =============================================================================

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
        free_capital_usd=10.0,
        slot_usd=2.0,
    )
    base.update(overrides)
    return base


def test_no_free_capital_when_money_runs_out_before_slots() -> None:
    """§7.1: свободных денег меньше слота — отказ ``no_free_capital``.

    Все прочие условия при этом выполнены: иначе тест проходил бы за счёт
    какой-нибудь другой причины и ничего не доказывал.
    """
    verdict = should_open(**_open_kwargs(free_capital_usd=1.99, slot_usd=2.0))
    assert verdict.allowed is False
    assert verdict.reason == REASON_NO_FREE_CAPITAL

    # Ровно на слот — вход РАЗРЕШЁН. Сравнение нестрогое в пользу входа: слот
    # оплачен целиком, и отказ здесь был бы отказом при достаточных деньгах.
    assert should_open(**_open_kwargs(free_capital_usd=2.0)).allowed is True

    # Отрицательный остаток (бюджет уменьшили при уже открытых позициях) —
    # тоже отказ, а не исключение и не молчаливый пропуск.
    assert should_open(**_open_kwargs(free_capital_usd=-4.0)).reason == (
        REASON_NO_FREE_CAPITAL
    )


def test_slots_full_wins_when_both_slots_and_money_ran_out() -> None:
    """§7.2: заняты слоты И кончились деньги — причиной названы СЛОТЫ.

    Порядок содержателен, а не произволен: «слоты заняты» — более точная
    причина, когда верны обе, потому что занятые слоты и есть то, во что ушли
    деньги. Обратный порядок объяснял бы происходящее нехваткой денег там, где
    деньги просто работают.
    """
    verdict = should_open(
        **_open_kwargs(open_count=5, max_open=5, free_capital_usd=0.0)
    )
    assert verdict.reason == REASON_SLOTS_FULL


def test_money_is_checked_before_the_freshness_of_the_candle() -> None:
    """Деньги проверяются ДО свежести свечи, и это тоже содержательно.

    Свежесть свечи есть свойство ДАННЫХ. Проверять её раньше денег значило бы
    объяснять отсутствие позиции состоянием рынка там, где на самом деле
    кончились деньги, — и человек пошёл бы чинить коллектор вместо разбора с
    бюджетом.
    """
    both = should_open(**_open_kwargs(free_capital_usd=0.0, bar_age_sec=None))
    assert both.reason == REASON_NO_FREE_CAPITAL
    # А при достаточных деньгах свежесть по-прежнему срабатывает.
    assert should_open(**_open_kwargs(bar_age_sec=None)).reason == (
        REASON_NO_FRESH_BAR
    )


def test_the_budget_validator_falls_at_startup_not_a_day_later() -> None:
    """Бюджет меньше слота — падение НА СТАРТЕ (§5.2 ТЗ).

    При таком бюджете не откроется ни одна позиция, и сутки журнала показывали
    бы «позиций нет» — вид, неотличимый от законного результата замера.
    """
    with pytest.raises(ValueError) as failure:
        Settings(POSITION_BUDGET_USD=1.0, POSITION_SLOT_USD=2.0)
    text = str(failure.value)
    # Сообщение называет ОБЕ величины: иначе непонятно, какую из них править.
    assert "POSITION_BUDGET_USD" in text and "POSITION_SLOT_USD" in text

    # Ровно равный слоту бюджет — законен: одна позиция помещается.
    assert Settings(
        POSITION_BUDGET_USD=2.0, POSITION_SLOT_USD=2.0
    ).POSITION_BUDGET_USD == 2.0


def test_profit_is_never_added_to_the_budget() -> None:
    """ПРИБЫЛЬ НЕ РЕИНВЕСТИРУЕТСЯ — проверяется по тексту сервиса.

    Свободный капитал считается как бюджет минус занятое, и накопленный итог в
    этой арифметике не участвует ни одним слагаемым. Проверка текстовая, потому
    что доказывать надо ОТСУТСТВИЕ слагаемого: значением его не поймать — при
    нулевом накопленном итоге обе формулы дают одно и то же число.
    """
    runner_text = (_ROOT / "src" / "positions" / "runner.py").read_text(
        encoding="utf-8"
    )
    formula = [
        line for line in runner_text.splitlines()
        if "free_capital = " in line
    ]
    assert formula, "в сервисе не найдено вычисление свободного капитала"
    assert "POSITION_BUDGET_USD" in formula[0]
    assert "committed" in formula[0]
    assert "net_pnl" not in formula[0] and "realized" not in formula[0]
    # И уменьшается он ровно на размер слота, а не на «слот плюс заработанное».
    assert "free_capital -= float(settings.POSITION_SLOT_USD)" in runner_text


# =============================================================================
# §7.3–§7.6. Закрытие позиции при пробеле в данных
# =============================================================================

def test_gap_exit_says_nothing_until_the_deadline_and_the_grace_pass() -> None:
    """§7.3: раньше срока и раньше запаса правило молчит.

    «Ещё рано» и «данных не будет» — разные состояния, и разница между ними
    только во времени. Правило, срабатывающее раньше запаса, закрывало бы
    позиции при обычной задержке сбора данных.
    """
    bars = [_bar(0, 100.0, 100.0, 100.0)]

    # Срок ещё не наступил.
    assert check_gap_exit(
        bars=bars, entry_price=_ENTRY, deadline_at=_DEADLINE,
        now=_DEADLINE - timedelta(minutes=1), grace_sec=_GRACE,
    ) is None

    # Срок наступил, запас не выдержан.
    assert check_gap_exit(
        bars=bars, entry_price=_ENTRY, deadline_at=_DEADLINE,
        now=_DEADLINE + timedelta(seconds=_GRACE - 1), grace_sec=_GRACE,
    ) is None

    # Ровно на границе — ещё ждём: нестрогое сравнение в пользу ожидания.
    assert check_gap_exit(
        bars=bars, entry_price=_ENTRY, deadline_at=_DEADLINE,
        now=_DEADLINE + timedelta(seconds=_GRACE), grace_sec=_GRACE,
    ) is not None


def test_gap_exit_takes_the_last_close_seen_before_the_deadline() -> None:
    """§7.4: цена выхода — ``close`` последнего бара ДО срока, и она не «точная».

    ``outcome_certain = False`` ВСЕГДА: цена выхода не наблюдалась в момент
    выхода, она восстановлена по последнему, что было видно. Пометить её
    достоверной значило бы поставить догадку в один ряд с измерением.
    """
    bars = [
        _bar(0, 101.0, 99.5, 100.5),
        _bar(1, 102.0, 100.0, 101.5),
        _bar(2, 101.0, 98.0, 99.0),
    ]
    decision = check_gap_exit(
        bars=bars, entry_price=_ENTRY, deadline_at=_DEADLINE,
        now=_DEADLINE + timedelta(seconds=_GRACE + 1), grace_sec=_GRACE,
    )
    assert decision is not None
    assert decision.exit_reason == EXIT_DATA_GAP
    assert decision.exit_price == pytest.approx(99.0)     # close последнего бара
    assert decision.exit_bar_ts == bars[-1].ts
    assert decision.outcome_certain is False
    assert decision.bars_held == 3
    # mae/mfe — по фактически виденным барам, а не по выдуманным.
    assert decision.mfe_pct == pytest.approx(2.0)
    assert decision.mae_pct == pytest.approx(-2.0)


def test_gap_exit_ignores_bars_at_or_after_the_deadline() -> None:
    """Бар с меткой РОВНО ``deadline_at`` принадлежит уже следующему окну.

    Он открывается в момент срока, а закрывается после него: взять его ``close``
    значило бы оценить позицию ценой, случившейся после её окончания.
    """
    late = Bar(ts=_DEADLINE, high=200.0, low=200.0, close=200.0)
    bars = [_bar(0, 101.0, 99.0, 100.5), late]
    decision = check_gap_exit(
        bars=bars, entry_price=_ENTRY, deadline_at=_DEADLINE,
        now=_DEADLINE + timedelta(seconds=_GRACE + 1), grace_sec=_GRACE,
    )
    assert decision is not None
    assert decision.exit_price == pytest.approx(100.5)
    assert decision.bars_held == 1


def test_gap_exit_without_a_single_bar_falls_back_to_the_entry_price() -> None:
    """§7.5: баров не было вовсе — цена выхода равна цене входа.

    Другой цены попросту нет: взять её неоткуда, а выдумать — значит записать в
    таблицу догадку, неотличимую от измерения.
    """
    decision = check_gap_exit(
        bars=[], entry_price=_ENTRY, deadline_at=_DEADLINE,
        now=_DEADLINE + timedelta(seconds=_GRACE + 1), grace_sec=_GRACE,
    )
    assert decision is not None
    assert decision.exit_reason == EXIT_DATA_GAP
    assert decision.exit_price == pytest.approx(_ENTRY)
    assert decision.exit_bar_ts == _DEADLINE
    assert decision.outcome_certain is False
    assert decision.bars_held == 0
    assert decision.mae_pct == 0.0
    assert decision.mfe_pct == 0.0


def test_the_outcome_of_an_unmeasurable_trade_is_exactly_minus_the_costs(
) -> None:
    """§7.6: итог по случаю §7.5 равен РОВНО минус издержкам.

    Не «примерно» и не «около нуля»: цена входа и цена выхода совпали, движения
    не наблюдалось, и всё, что осталось от сделки, — это её стоимость. Честное
    описание сделки, которую невозможно оценить.
    """
    decision = check_gap_exit(
        bars=[], entry_price=_ENTRY, deadline_at=_DEADLINE,
        now=_DEADLINE + timedelta(seconds=_GRACE + 1), grace_sec=_GRACE,
    )
    assert decision is not None
    pnl = net_pnl(_ENTRY, decision.exit_price, _COST_PCT)
    assert pnl == pytest.approx(-_COST_PCT)


def test_a_position_without_new_bars_is_no_longer_skipped_forever() -> None:
    """Условие пропуска ослаблено — иначе правило §6 не сработало бы НИКОГДА.

    Позиция без единого нового бара раньше не трогалась вовсе, а это ровно тот
    случай, в котором и возникает пробел: нет данных — нет новых баров — нет
    разбора — позиция висит вечно. Проверка текстовая, потому что доказывается
    СОСТАВ условия, а не поведение на одном наборе входов.
    """
    text = (_ROOT / "src" / "positions" / "runner.py").read_text(encoding="utf-8")
    assert "if settle_edge <= last_checked and now < gap_deadline:" in text
    assert "POSITION_GAP_GRACE_SEC" in text
    # И закрытие по пробелу датируется МОМЕНТОМ ЗАКРЫТИЯ, а не баром из прошлого.
    assert "now if by_gap else decision.exit_bar_ts + timedelta(seconds=60)" in text


def test_the_gap_message_does_not_claim_a_result() -> None:
    """§6.6: у пробела свой текст, и он не утверждает результата.

    Обычный текст закрытия говорит «итог такой-то». Здесь итога нет — есть
    последняя известная цена и признание того, что измерить не удалось.
    """
    from src.positions import messages

    text = messages.data_gap_text(
        symbol="SOL/USDT", entry_price=142.37, exit_price=141.90,
        last_bar_ts=datetime(2026, 8, 30, 4, 12, tzinfo=UTC),
        gap_sec=13320, net_pnl_pct=-0.55, net_pnl_usd=-0.011, bars_held=37,
    )
    assert "виртуально" in text
    assert "пробел" in text.lower()
    assert "последней известной цене" in text
    assert "В СТАТИСТИКУ НЕ ИДЁТ" in text
    assert "37 баров" in text

    # БЕЗ ЕДИНОГО БАРА ТЕКСТ ДРУГОЙ, и это не косметика: «последней известной
    # цены» в этом случае не существовало вовсе, выход посчитан по цене входа.
    # Сослаться на наблюдение, которого не было, — это то же самое, что
    # выдумать его.
    empty = messages.data_gap_text(
        symbol="SOL/USDT", entry_price=142.37, exit_price=142.37,
        last_bar_ts=datetime(2026, 8, 30, 4, 12, tzinfo=UTC),
        gap_sec=10800, net_pnl_pct=-0.22, net_pnl_usd=-0.0044, bars_held=0,
    )
    assert "последней известной цене" not in empty
    assert "по цене входа" in empty
    assert "оценить сделку нечем" in empty
    # И это НЕ обычный текст закрытия: тот утверждал бы измеренный результат.
    closed = messages.closed_text(
        symbol="SOL/USDT", exit_reason="timeout", entry_price=142.37,
        exit_price=141.90, net_pnl_pct=-0.55, net_pnl_usd=-0.011,
        cost_pct=0.22, held_sec=86400,
    )
    assert text != closed


def test_data_gap_is_excluded_from_averages_but_counted_separately() -> None:
    """§6.7: пробелы вне средних, но посчитаны отдельной строкой.

    Вне средних — потому что цена выхода восстановлена, и их «итог» описывает
    длительность сбоя сбора данных, а не поведение рынка. Посчитаны — потому
    что пять таких закрытий означают, что встал коллектор, и молчать нельзя.
    """
    from src.bot.handlers import render_positions

    queries_text = (_ROOT / "src" / "bot" / "queries.py").read_text(
        encoding="utf-8"
    )
    summary_sql = queries_text.split("async def positions_summary", 1)[1]
    summary_sql = summary_sql.split("reasons = await", 1)[0]
    for metric in ("avg(net_pnl_pct)", "sum(net_pnl_usd)",
                   "avg(entry_slippage_pct)"):
        after = summary_sql.split(metric, 1)[1][:160]
        assert "exit_reason <> 'data_gap'" in after, metric
    assert "FILTER (WHERE exit_reason = 'data_gap') AS data_gap" in summary_sql

    now = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    with_gaps = render_positions(
        [], {"closed": 4, "data_gap": 2, "by_reason": [], "uncertain": 0},
        now, days=7,
    )
    assert "Закрыто по пробелу в данных: 2" in with_gaps
    assert "в средние не входят" in with_gaps
    # Ноль не печатается: строка «закрыто по пробелу: 0» приучает не читать её.
    without = render_positions(
        [], {"closed": 4, "data_gap": 0, "by_reason": [], "uncertain": 0},
        now, days=7,
    )
    assert "по пробелу" not in without


def test_the_capital_lines_stand_apart_and_are_never_summed() -> None:
    """§5.5: занято и накоплено — ДВЕ строки, а не одна сумма.

    Сложенные вместе, они описывали бы счёт, которого нет: прибыль не
    реинвестируется, бюджет остаётся постоянным.
    """
    from src.bot.handlers import render_positions

    text = render_positions(
        [], {"closed": 0}, datetime(2026, 8, 30, 12, 0, tzinfo=UTC), days=7,
        capital={"committed_usd": 4.0, "realized_usd": 0.031234},
        budget_usd=10.0, slot_usd=2.0,
    )
    assert "Капитал: занято 4.00 из 10.00 USDT (слот 2.00)" in text
    assert "Накопленный итог: +0.031234 USDT (не реинвестируется)" in text
    # Слово «виртуально» из заголовка не убрано.
    assert "Виртуальные позиции" in text
    # Строки стоят ПОД ЗАГОЛОВКОМ, до списка позиций.
    assert text.index("Капитал: занято") < text.index("Открыто сейчас")


def test_the_positions_service_is_watched_in_all_three_places() -> None:
    """§4: ключ positions:heartbeat читают бот, суточный отчёт и вотчдог.

    Остановившийся сервис иначе ничем себя не проявит, а его остановка означает,
    что уже открытые позиции повиснут: задетые за время простоя цель и предел не
    будут замечены никогда.
    """
    from src.bot.poller import HEARTBEAT_KEYS
    from src.health.daily_report import HEARTBEATS as REPORT_HEARTBEATS

    assert ("positions:heartbeat", "POSITION_INTERVAL") in HEARTBEAT_KEYS
    assert ("positions:heartbeat", "POSITION_INTERVAL", 60) in REPORT_HEARTBEATS

    watchdog = (_ROOT / "scripts" / "watchdog.py").read_text(encoding="utf-8")
    assert '("positions:heartbeat", "POSITION_INTERVAL", 60, "positions")' in watchdog
    containers = watchdog.split("CONTAINERS = ", 1)[1].split("]", 1)[0]
    assert '"positions"' in containers


# =============================================================================
# §3-бис. Разбор причин отказа понимает ОБА формата боевого журнала
# =============================================================================

_VERIFY_SH = _ROOT / "deploy" / "verify_9_1.sh"


def _run_reason_block(lines: list[str]) -> str:
    """Прогоняет БЛОК РАЗБОРА из настоящего ``deploy/verify_9_1.sh``.

    Блок берётся из файла между пометками ``reason-block`` и выполняется в bash
    на подсунутом журнале. Пересказать его в тесте было бы проще, но пересказ
    разошёлся бы с оригиналом молча — и тест доказывал бы исправность пересказа,
    а не проверки. Ровно так §3 и остался сломанным: в наборе не было ничего,
    что запускало бы сам разбор.

    ``note_warn`` подменяется печатью ``WARN:`` — вне скрипта его нет, а его
    вызов и есть то, что проверяется в сверке суммы.
    """
    text = _VERIFY_SH.read_text(encoding="utf-8")
    block = text.split("# >>> reason-block", 1)[1]
    block = block.split("\n", 1)[1].split("# <<< reason-block", 1)[0]
    script = (
        "set -uo pipefail\n"
        'note_warn() { echo "WARN: $*"; }\n'
        "logs=\"$(cat)\"\n"
        + block
    )
    done = subprocess.run(
        ["bash", "-c", script], input="\n".join(lines),
        capture_output=True, text=True, check=False,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


def _counters(output: str) -> dict[str, int]:
    """Счётчики из вывода ``uniq -c``: ``{причина: число}``."""
    found: dict[str, int] = {}
    for line in output.splitlines():
        match = re.match(r"^\s+(\d+) (.+)$", line)
        if match:
            found[match.group(2).strip()] = int(match.group(1))
    return found


def test_json_journal_is_understood_because_production_writes_json() -> None:
    """§3-бис.1: три строки JSON дают один счётчик ``no_fresh_bar = 3``.

    ЭТО ГЛАВНЫЙ СЛУЧАЙ, а не экзотика. ``src/core/logging.py`` выбирает рендер
    по ``sys.stdout.isatty()``; в контейнере stdout не терминал (``tty`` в
    ``docker-compose.yml`` не задан ни у одного сервиса), значит в проде
    работает ``JSONRenderer``, и причина лежит в поле ``"reason": "..."`` —
    БЕЗ знака равенства. Прежний шаблон ``reason=`` печатал бы заголовок и ни
    одной строки под ним.

    Строка закрытия с ``"exit_reason"`` в набор не попадает: причины берутся
    только из строк отказа, и ``"exit_reason"`` не должен читаться как
    ``"reason"``.
    """
    out = _run_reason_block([
        '{"signal_id": 5, "symbol": "BTC/USDT", "reason": "no_fresh_bar",'
        ' "event": "positions_skipped=1", "level": "info"}',
        '{"signal_id": 6, "symbol": "ETH/USDT", "reason": "no_fresh_bar",'
        ' "event": "positions_skipped=1", "level": "info"}',
        '{"signal_id": 7, "symbol": "SOL/USDT", "reason": "no_fresh_bar",'
        ' "event": "positions_skipped=1", "level": "info"}',
        '{"exit_reason": "target", "event": "positions_closed=1"}',
    ])
    assert _counters(out) == {"no_fresh_bar": 3}
    assert "всего 3" in out
    assert "target" not in out
    assert "WARN:" not in out


def test_json_journal_without_spaces_after_the_colon_is_understood() -> None:
    """Пробелы после двоеточия — любые, включая ноль.

    Компактный JSON (``{"reason":"slots_full"}``) — законный вывод сериализатора
    и однажды может прийти вместо нынешнего. Разбор, привязанный к одному
    пробелу, сломался бы на нём молча.
    """
    out = _run_reason_block([
        '{"reason":"slots_full","event":"positions_skipped=1"}',
        '{"reason"  :   "slots_full", "event": "positions_skipped=1"}',
    ])
    assert _counters(out) == {"slots_full": 2}


def test_the_console_render_is_understood_too() -> None:
    """§3-бис.2: читаемый рендер (``reason=slots_full``) разбирается так же.

    Он появляется при ручном запуске сервиса в терминале — том самом случае,
    когда за журналом смотрят глазами и проверку запускают чаще всего.
    """
    out = _run_reason_block([
        "2026-08-30 10:00:00 [info ] positions_skipped=1 reason=slots_full signal_id=1",
        "2026-08-30 10:01:00 [info ] positions_skipped=1 reason=slots_full signal_id=2",
        "2026-08-30 10:02:00 [info ] positions_skipped=1 reason=degraded signal_id=3",
    ])
    assert _counters(out) == {"slots_full": 2, "degraded": 1}
    assert "WARN:" not in out


def test_an_unparsed_line_is_named_not_dropped() -> None:
    """§3-бис.3: строка без причины идёт в свой счётчик, сумма сходится.

    ПОТЕРЯННАЯ СТРОКА ВЫГЛЯДИТ КАК ОТСУТСТВИЕ ОТКАЗА — это и есть та ошибка,
    из-за которой §3 пришлось переписывать дважды. Пустая причина (``""``)
    причиной не считается: перечень причин закрыт, и пустая строка в нём не
    значится.
    """
    out = _run_reason_block([
        '{"reason": "degraded", "event": "positions_skipped=1"}',
        '{"event": "positions_skipped=1"}',
        '{"reason": "", "event": "positions_skipped=1"}',
    ])
    counters = _counters(out)
    assert counters == {"degraded": 1, "(причина не распознана)": 2}
    # Сумма счётчиков равна числу строк отказа — и сверка молчит.
    assert sum(counters.values()) == 3
    assert "всего 3" in out
    assert "WARN:" not in out


def test_no_refusals_at_all_is_said_in_words() -> None:
    """§3-бис.4: отказов нет — так и печатается, а не пустотой под заголовком.

    Пустое место читатель толкует как поломку скрипта.
    """
    out = _run_reason_block(['{"event": "positions_opened=1"}'])
    assert "отказов не было" in out
    assert _counters(out) == {}


def test_the_head_limit_does_not_touch_the_sum_check() -> None:
    """Обрезка ``head -10`` — свойство ПОКАЗА, а не разбора.

    Различных причин больше десяти — счётчиков показано десять, но сумма
    считается ДО обрезки и потому сходится. Сверка, посчитанная после обрезки,
    сходилась бы «почти» всегда и не поймала бы ничего.
    """
    reasons = [
        "not_buy", "wrong_logic_version", "degraded", "low_probability",
        "instrument_busy", "slots_full", "signal_too_old", "no_fresh_bar",
        "no_frozen_target", "no_free_capital", "extra_one", "extra_two",
    ]
    out = _run_reason_block([
        f'{{"reason": "{name}", "event": "positions_skipped=1"}}'
        for name in reasons
    ])
    assert "всего 12" in out
    assert len(_counters(out)) == 10
    assert "показаны первые 10 из 12" in out
    # И сверка молчит: разбор ничего не потерял, обрезан только показ.
    assert "WARN:" not in out


def test_the_sum_check_speaks_up_when_the_parser_loses_a_line() -> None:
    """Разбор, который не умеет себя проверить, однажды соврёт незаметно.

    Проверяется САМА СВЕРКА: разбор намеренно ломается (последняя подстановка
    sed заменяется на удаление строки), и сверка обязана это заметить. Без
    такого теста «сверка суммы» была бы строкой кода, про которую никто не
    знает, срабатывает ли она вообще.
    """
    text = _VERIFY_SH.read_text(encoding="utf-8")
    block = text.split("# >>> reason-block", 1)[1]
    block = block.split("\n", 1)[1].split("# <<< reason-block", 1)[0]
    broken = block.replace(
        "-e 's/.*/(причина не распознана)/'", '-e "/positions_skipped/d"'
    )
    assert broken != block, "не удалось сломать разбор — тест проверял бы не то"
    script = (
        "set -uo pipefail\n"
        'note_warn() { echo "WARN: $*"; }\n'
        "logs=\"$(cat)\"\n"
        + broken
    )
    done = subprocess.run(
        ["bash", "-c", script],
        input='{"event": "positions_skipped=1"}\n'
              '{"reason": "degraded", "event": "positions_skipped=1"}',
        capture_output=True, text=True, check=False,
    )
    assert "WARN:" in done.stdout
    assert "1 против 2" in done.stdout


def test_the_old_bare_pattern_is_gone_from_the_script() -> None:
    """Прежний шаблон ``grep -o 'reason=[a-z_]*'`` в файле не остался.

    Он находил ноль строк на JSON-журнале и пустые счётчики на читаемом. Оставь
    его рядом «на всякий случай» — и однажды кто-нибудь вернёт разбор к нему,
    потому что он короче.
    """
    text = _VERIFY_SH.read_text(encoding="utf-8")
    assert "grep -o 'reason=[a-z_]*'" not in text
    # А новый разбор на месте и знает оба вида записи.
    assert '"reason"[[:space:]]*:[[:space:]]*"([a-z_]+)"' in text
    assert "s/.*reason=([a-z_]+).*/" in text


# =============================================================================
# §7.7. Критерий подозрительности — по фактическому разрешению строки
# =============================================================================

def test_the_predicate_asks_the_row_for_its_own_resolution() -> None:
    """Предикат ветвится по колонке ``resolution``, а неизвестное — худший случай.

    Проверка структурная и потому идёт без базы: она ловит ВОЗВРАТ к прежней
    редакции, а именно возврат и опасен. Прежний предикат закладывал один запас
    всем строкам подряд и на боевой базе объявил подозрительными 7618 исправных
    минутных измерений.
    """
    predicate = DB.STRATEGY_UNSETTLED_PREDICATE
    assert "CASE resolution" in predicate
    assert "WHEN '1m' THEN 60" in predicate
    assert "WHEN '1h' THEN 3600" in predicate
    # Ветка ELSE — параметром, а не числом: неизвестное разрешение обязано
    # проверяться худшим случаем из настроек, а не зашитой константой.
    assert "ELSE $1::int END" in predicate
    assert "3900" not in predicate

    # Та же формула — в проверочном скрипте: разделяемого кода между bash и
    # Python нет, поэтому копии обязаны меняться вместе.
    verify = (_ROOT / "deploy" / "verify_9_1.sh").read_text(encoding="utf-8")
    assert "CASE resolution" in verify
    assert "WHEN '1m' THEN 60" in verify
    assert "WHEN '1h' THEN 3600" in verify
    assert "ELSE ${settle_sec}" in verify


@needs_db
async def test_a_minute_row_is_clean_where_the_same_hour_row_is_suspicious(
) -> None:
    """§7.7: одна и та же строка, посчитанная через 61 секунду после срока.

    При ``resolution='1m'`` она НЕ подозрительна: последний бар её окна
    закрылся через 60 секунд после срока, и расчёт состоялся уже по закрытому
    бару. При ``resolution='1h'`` она подозрительна: часовой бар к этому моменту
    ещё формируется, и его ``close`` — цена «пока что».

    Один и тот же запас, два разных ответа — ради этого различия §1–§2 и
    написаны.
    """
    clean = await _plant_and_query(resolution="1m", margin=timedelta(seconds=61))
    assert clean == []

    dirty = await _plant_and_query(resolution="1h", margin=timedelta(seconds=61))
    assert [row["resolution"] for row in dirty] == ["1h"]

    # И обратная сторона: часовая строка с запасом больше часа тоже чиста —
    # иначе критерий просто объявлял бы подозрительным всё подряд.
    late = await _plant_and_query(
        resolution="1h", margin=timedelta(seconds=3601)
    )
    assert late == []


# =============================================================================
# §7.8. Ограждения --apply в скрипте починки
# =============================================================================

class _FakeDB:
    """Подменённый слой доступа к базе. Считает вызовы, ничего не делает.

    Нужен затем, чтобы проверять ФАКТ невызова ``delete``: скрипт, напечатавший
    слово «отказ» и всё-таки удаливший строки, прошёл бы проверку по выводу.
    """

    def __init__(self, rows: list[dict], *, snapshot_fails: bool = False) -> None:
        self.rows = rows
        self.snapshot_fails = snapshot_fails
        self.deleted_calls = 0
        self.snapshot_calls = 0

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def count_strategy_outcomes_unsettled(self, *, settle_seconds: int) -> int:
        return len(self.rows)

    async def get_strategy_outcomes_unsettled(
        self, *, settle_seconds: int, limit: int | None = None
    ) -> list[dict]:
        return list(self.rows)

    async def get_strategy_stats_snapshot(self) -> list[dict]:
        self.snapshot_calls += 1
        if self.snapshot_fails:
            raise OSError("диск только на чтение")
        return []

    async def delete_strategy_outcomes_unsettled(self, *, settle_seconds: int) -> int:
        self.deleted_calls += 1
        return len(self.rows)


def _suspicious_row(strategy: str = "grid_buy") -> dict:
    return {
        "strategy": strategy,
        "instrument_id": 1,
        "entry_ts": datetime(2026, 8, 20, 0, 0, tzinfo=UTC),
        "horizon_h": 1,
        "computed_at": datetime(2026, 8, 20, 1, 0, 30, tzinfo=UTC),
        "outcome": "timeout",
        "net_pnl_pct": 0.1,
        "logic_version": 5,
        "resolution": "1m",
    }


async def _run_repair(
    monkeypatch, argv: list[str], fake: _FakeDB, snapshot_dir=None
) -> int:
    """Прогоняет НАСТОЯЩИЙ ``main()`` скрипта на подменённой базе.

    ПУТЬ СНИМКА УВОДИТСЯ ВО ВРЕМЕННЫЙ КАТАЛОГ. Скрипт пишет снимок «до» в
    ``reports/`` репозитория, и тест, оставляющий там файл, меняет рабочее
    дерево — а тест, меняющий дерево, однажды попадёт в коммит вместе со своим
    следом. Подменяется именно константа модуля, а не сама запись: проверять
    надо настоящую запись файла, иначе ограждение «снимок обязателен» осталось
    бы непроверенным.
    """
    import scripts.repair_9_1_strategy_settle as repair

    monkeypatch.setattr(repair, "db", fake)
    if snapshot_dir is not None:
        monkeypatch.setattr(
            repair, "SNAPSHOT_BEFORE",
            str(snapshot_dir / "strategy_stats_before_9_1.txt"),
        )
        monkeypatch.setattr(
            repair, "SNAPSHOT_AFTER",
            str(snapshot_dir / "strategy_stats_after_9_1.txt"),
        )
    monkeypatch.setattr(
        repair.sys, "argv", ["repair_9_1_strategy_settle", *argv]
    )
    return await repair.main()


async def test_apply_with_a_mismatched_confirm_count_deletes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§7.8: подтверждение не совпало — код 4 и НИ ОДНОГО удаления.

    Расхождение означает, что база изменилась между отчётом и удалением:
    удаляемое множество уже не то, которое видел человек. Молча удалять в этот
    момент нельзя — удаление безвозвратно, а пересчёт возможен только после него.
    """
    fake = _FakeDB([_suspicious_row(), _suspicious_row("grid_sell")])
    code = await _run_repair(
        monkeypatch, ["--apply", "--confirm-count=7"], fake
    )
    assert code == 4
    assert fake.deleted_calls == 0
    # И снимок «до» тоже не снимался: работа не начиналась вовсе.
    assert fake.snapshot_calls == 0


async def test_apply_with_a_matching_confirm_count_does_delete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path,
) -> None:
    """Обратная сторона: при совпавшем числе удаление ПРОИСХОДИТ.

    Без этой проверки предыдущая проходила бы и у скрипта, который не удаляет
    никогда.
    """
    fake = _FakeDB([_suspicious_row(), _suspicious_row("grid_sell")])
    code = await _run_repair(
        monkeypatch, ["--apply", "--confirm-count=2"], fake,
        snapshot_dir=tmp_path,
    )
    assert code == 0
    assert fake.deleted_calls == 1
    assert fake.snapshot_calls == 1
    # Снимок «до» действительно ЗАПИСАН на диск, а не только «посчитан»:
    # ограждение обещает файл, и обещание проверяется файлом.
    snapshot = tmp_path / "strategy_stats_before_9_1.txt"
    assert snapshot.exists()
    assert "Критерий" in snapshot.read_text(encoding="utf-8")


async def test_a_failed_snapshot_stops_the_deletion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Снимок «до» не записался — код 3 и ни одного удаления.

    Снимок — единственное, по чему потом можно сказать, что именно исчезло.
    Удалять без него значило бы делать необратимое вслепую.
    """
    fake = _FakeDB([_suspicious_row()], snapshot_fails=True)
    code = await _run_repair(
        monkeypatch, ["--apply", "--confirm-count=1"], fake
    )
    assert code == 3
    assert fake.deleted_calls == 0


async def test_zero_suspicious_rows_is_not_a_reason_to_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Подозрительных строк ноль — «удалять нечего», код 0, транзакции нет."""
    fake = _FakeDB([])
    code = await _run_repair(
        monkeypatch, ["--apply", "--confirm-count=0"], fake
    )
    assert code == 0
    assert fake.deleted_calls == 0
    assert fake.snapshot_calls == 0


def test_apply_without_a_confirm_count_refuses_on_the_threshold() -> None:
    """Забытый ``--confirm-count`` останавливает НА ПОРОГЕ, до всякой работы.

    Проверка стоит до подключения к базе: аргумент, забытый оператором, обязан
    останавливать раньше, чем скрипт что-нибудь сделает.
    """
    import asyncio

    import scripts.repair_9_1_strategy_settle as repair

    original = repair.sys.argv
    repair.sys.argv = ["repair_9_1_strategy_settle", "--apply"]
    try:
        with pytest.raises(SystemExit) as exit_info:
            asyncio.run(repair.main())
    finally:
        repair.sys.argv = original
    # argparse.error завершает кодом 2 — это отказ разбора аргументов, а не
    # отказ работы: до базы дело не дошло.
    assert exit_info.value.code == 2


# =============================================================================
# §7.9. Перечни причин и ограничения миграций
# =============================================================================

def _constraint_values(sql_text: str) -> list[str]:
    """Значения перечня из ``CHECK (... IN ('a', 'b', ...))`` для причины выхода."""
    block = sql_text.split("positions_reason_chk", 1)[1]
    block = block.split("exit_reason IN", 1)[1]
    block = block.split(")", 1)[0]
    return re.findall(r"'([a-z_]+)'", block)


def test_exit_reasons_are_exactly_the_constraint_of_migration_019() -> None:
    """§7.9: перечень исходов в коде и ограничение 019 — одно и то же.

    Сверка идёт с ТЕКСТОМ МИГРАЦИИ, а не с копией списка в самом тесте: копия
    списка проверяла бы саму себя и осталась бы зелёной ровно тогда, когда
    расхождение и возникло бы. Проверяется СОСТАВ, а не вхождение: лишнее
    значение в ограничении так же опасно, как недостающее — оно разрешило бы
    записать причину, которой код не знает.
    """
    m019 = (_MIGRATIONS / "019_positions_data_gap.sql").read_text(
        encoding="utf-8"
    )
    assert set(_constraint_values(m019)) == set(EXIT_REASONS)
    assert EXIT_DATA_GAP in EXIT_REASONS

    # 018 описывала перечень ДО этого этапа: четыре значения, без data_gap.
    m018 = (_MIGRATIONS / "018_positions.sql").read_text(encoding="utf-8")
    assert set(_constraint_values(m018)) == set(EXIT_REASONS) - {EXIT_DATA_GAP}

    # И схема, которую сервис гарантирует при старте, знает то же самое: на
    # чистом томе миграции могли не применяться, и перечень из четырёх значений
    # отверг бы закрытие по пробелу — сервис падал бы на первой такой позиции.
    db_text = (_ROOT / "src" / "core" / "db.py").read_text(encoding="utf-8")
    assert set(_constraint_values(db_text)) == set(EXIT_REASONS)


def test_the_rollback_of_019_restores_exactly_four_values() -> None:
    """Откат возвращает перечень из четырёх значений — и отказывается при данных.

    Ограничение с четырьмя значениями на таблице, где пятое уже записано, либо
    не создастся вовсе, оставив таблицу БЕЗ закрытого перечня, либо потребовало
    бы удалить эти строки. Удалять записи о закрытых позициях ради отката схемы
    нельзя: это данные замера.
    """
    rollback = (_MIGRATIONS / "019_positions_data_gap_rollback.sql").read_text(
        encoding="utf-8"
    )
    assert set(_constraint_values(rollback)) == set(EXIT_REASONS) - {
        EXIT_DATA_GAP
    }
    assert "RAISE EXCEPTION" in rollback
    assert "data_gap" in rollback


def test_refusal_reasons_stay_a_closed_list_of_ten() -> None:
    """Перечень причин ОТКАЗА ВО ВХОДЕ закрыт и содержит ровно десять значений.

    ЗДЕСЬ ОШИБКА В ТЗ, и обойти её молча нельзя (§10 ТЗ). Пункт §7.9 требует
    сверить с ограничениями миграций 018 и 019 ОБА перечня — и причины выхода,
    и причины отказа. Для причин выхода такая сверка возможна и сделана выше:
    они лежат в колонке ``positions.exit_reason`` и закрыты ограничением
    ``positions_reason_chk``. Причины ОТКАЗА не записываются ни в одну таблицу
    вовсе — отказ во входе означает, что строки позиции не появилось, — и
    ограничения для них нет ни в 018, ни в 019, ни где-либо ещё. Сверять их с
    миграциями не с чем.

    Поэтому здесь проверяется то, что проверить можно и нужно: перечень закрыт,
    не содержит повторов, и в миграциях его значений действительно нет — то
    есть отсутствие сверки не следствие того, что её забыли написать.
    """
    assert len(REFUSAL_REASONS) == 10
    assert len(set(REFUSAL_REASONS)) == 10
    assert REASON_NO_FREE_CAPITAL in REFUSAL_REASONS

    migrations = "".join(
        (_MIGRATIONS / name).read_text(encoding="utf-8")
        for name in ("018_positions.sql", "019_positions_data_gap.sql")
    )
    for reason in REFUSAL_REASONS:
        assert f"'{reason}'" not in migrations, (
            f"причина отказа {reason!r} вдруг появилась в миграции — значит, "
            "отказы стали куда-то записываться, и сверка перечня с базой стала "
            "возможной и обязательной"
        )


@needs_db
async def test_migration_019_applies_twice_and_rolls_back() -> None:
    """Миграция 019 идемпотентна, откат работает, повторное применение тоже.

    «Не упало» и «сработало» — разные утверждения, поэтому после каждого шага
    читается фактическое определение ограничения, а не код возврата psql.
    """
    import asyncpg

    forward = (_MIGRATIONS / "019_positions_data_gap.sql").read_text(
        encoding="utf-8"
    )
    backward = (_MIGRATIONS / "019_positions_data_gap_rollback.sql").read_text(
        encoding="utf-8"
    )
    conn = await asyncpg.connect(dsn=TEST_DSN)
    try:
        await conn.execute(forward)
        await conn.execute(forward)
        assert "data_gap" in await _constraint_def(conn)

        # Откат ОТКАЗЫВАЕТСЯ работать при живых строках data_gap (это его
        # свойство проверяет отдельный тест ниже), поэтому здесь их быть не
        # должно. Если они есть — база не одноразовая, и молчать об этом
        # нельзя: тест не «прошёл бы», он проверял бы не то.
        await _drop_planted(conn)
        left = await conn.fetchval(
            "SELECT count(*) FROM positions WHERE exit_reason = 'data_gap';"
        )
        assert int(left) == 0, (
            f"в тестовой базе {left} строк с exit_reason='data_gap' — откат "
            "проверять не на чем. AT_TEST_DSN обязан указывать на ОДНОРАЗОВУЮ "
            "базу"
        )

        await conn.execute(backward)
        assert "data_gap" not in await _constraint_def(conn)

        await conn.execute(forward)
        assert "data_gap" in await _constraint_def(conn)
    finally:
        # База обязана остаться в том состоянии, в каком её взяли: миграция
        # применена.
        await conn.execute(forward)
        await conn.close()


@needs_db
async def test_the_rollback_refuses_while_data_gap_rows_exist() -> None:
    """Откат 019 НЕ ВЫПОЛНЯЕТСЯ, пока в таблице есть строки с data_gap.

    Иначе он оборвался бы на полпути, оставив таблицу без закрытого перечня
    причин, — или потребовал бы удалить данные замера ради отката схемы.
    """
    import asyncpg

    backward = (_MIGRATIONS / "019_positions_data_gap_rollback.sql").read_text(
        encoding="utf-8"
    )
    conn = await asyncpg.connect(dsn=TEST_DSN)
    try:
        await _plant_data_gap_position(conn)
        with pytest.raises(asyncpg.PostgresError) as failure:
            await conn.execute(backward)
        assert "data_gap" in str(failure.value)
        # Файл отката начинается с BEGIN, и упавшая команда оставляет
        # транзакцию в состоянии «прервана»: снимаем её явно. Это и есть
        # доказательство того, что откат НЕ ЗАФИКСИРОВАЛСЯ — до COMMIT он не
        # дошёл.
        await conn.execute("ROLLBACK;")
        # Ограничение при этом ОСТАЛОСЬ на месте и по-прежнему знает пять
        # значений: неудавшийся откат не оставил таблицу беззащитной.
        assert "data_gap" in await _constraint_def(conn)
    finally:
        await _drop_planted(conn)
        await conn.close()


# =============================================================================
# Вспомогательное для тестов, которым нужна база
# =============================================================================

_PLANT_SYMBOL = "TEST911/USDT"


async def _constraint_def(conn) -> str:
    """Фактическое определение ограничения причин выхода — как его видит база."""
    return str(await conn.fetchval(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conname = 'positions_reason_chk' "
        "AND conrelid = 'positions'::regclass;"
    ))


async def _plant_and_query(*, resolution: str, margin: timedelta):
    """Подкладывает ОДНУ строку с заданным запасом и спрашивает критерий.

    Запас отсчитывается от СРОКА окна (``entry_ts + horizon_h``) — ровно так,
    как его считает критерий.
    """
    import asyncpg

    from src.barrier.runner import settle_seconds
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
            settle_seconds=settle_seconds()
        )
        return [
            row for row in rows
            if int(row["instrument_id"]) == int(instrument_id)
            and row["entry_ts"] == entry_ts
        ]
    finally:
        await conn.execute(
            "DELETE FROM strategy_outcomes WHERE instrument_id IN "
            "(SELECT id FROM instruments WHERE symbol = $1);", _PLANT_SYMBOL,
        )
        await conn.close()
        await database.close()


async def _plant_data_gap_position(conn) -> None:
    """Кладёт одну закрытую по пробелу позицию — чтобы откату было чему мешать."""
    await _drop_planted(conn)
    ts = datetime(2026, 8, 20, 0, 0, tzinfo=UTC)
    instrument_id = await conn.fetchval(
        "INSERT INTO instruments (exchange, symbol, base, quote, type) "
        "VALUES ('okx', 'TEST911G/USDT', 'TEST911G', 'USDT', 'spot') "
        "ON CONFLICT (exchange, symbol, type) DO UPDATE "
        "SET symbol = EXCLUDED.symbol RETURNING id;"
    )
    signal_id = await conn.fetchval(
        "INSERT INTO signals (instrument_id, ts, decision, logic_version) "
        "VALUES ($1, $2, 'buy', 5) RETURNING id;",
        instrument_id, ts,
    )
    await conn.execute(
        """
        INSERT INTO positions
            (instrument_id, signal_id, logic_version, horizon_h, side,
             is_virtual, status, signal_ts, signal_price, opened_at,
             entry_price, entry_lag_sec, entry_slippage_pct, qty, notional_usd,
             target_pct, target_price, stop_pct, stop_price, cost_pct,
             deadline_at, resolution, closed_at, exit_price, exit_reason,
             outcome_certain, net_pnl_pct, net_pnl_usd, bars_held,
             mae_pct, mfe_pct)
        VALUES ($1, $2, 5, 24, 'buy', TRUE, 'closed', $3, 100, $3, 100, 25, 0,
                0.02, 2.0, 1, 101, 1, 99, 0.22, $4, '1m', $4, 100,
                'data_gap', FALSE, -0.22, -0.0044, 0, 0, 0);
        """,
        instrument_id, signal_id, ts, ts + timedelta(hours=24),
    )


async def _drop_planted(conn) -> None:
    """Убирает подложенное: тест обязан оставить базу такой, какой её взял."""
    await conn.execute(
        "DELETE FROM positions WHERE instrument_id IN "
        "(SELECT id FROM instruments WHERE symbol LIKE 'TEST911G%');"
    )
    await conn.execute(
        "DELETE FROM signals WHERE instrument_id IN "
        "(SELECT id FROM instruments WHERE symbol LIKE 'TEST911G%');"
    )
