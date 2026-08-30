"""Этап 9.1.2: ведение сделок в торговом журнале Google Таблиц (§11 ТЗ).

ЧТО ЗДЕСЬ ДОКАЗЫВАЕТСЯ, и почему именно это.

СТРОКА ЖИВЁТ ДВА ЭТАПА. При открытии пишутся столбцы A–G, при закрытии в ТУ ЖЕ
строку дозаписываются H, I, J. Отсюда четыре сборщика вместо одного и две
отметки в базе вместо одной — и отсюда же главная опасность этапа: вторая строка
той же сделки. Она проверяется прямо: повторный прогон после успешного не
отправляет НИ ОДНОЙ строки.

ЧИСЛА ОБЯЗАНЫ ОСТАВАТЬСЯ ЧИСЛАМИ. Цена, отправленная строкой, ляжет в ячейку
текстом, будет выглядеть точно так же и тихо сломает все формулы, которые на неё
ссылаются. Поэтому тип проверяется, а не подразумевается.

ЛИСТ ЧУЖОЙ. В нём формулы владельца, и он правится руками. Поэтому проверяется
и то, чего код НЕ делает: не пишет правее столбца J, не угадывает строку, когда
метка не нашлась, и не ходит в сеть вовсе, пока флаг выключен.

Стороне приёмника Apps Script (куда именно ложится строка, как растягиваются
формулы, что происходит при нехватке места) посвящён отдельный стенд —
``tests/apps_script/receiver_harness.mjs``, прогоняющий НАСТОЯЩИЙ
``deploy/apps_script.gs`` в Node на двойнике торгового журнала.
"""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime
from typing import Any

import pytest

from src.core.config import settings
from src.export import queries, sheets
from src.export.transform import (
    DATA_GAP_NOTE,
    POSITION_CLOSE_START_COLUMN,
    POSITION_CLOSE_WIDTH,
    POSITION_NOTE_COLUMN,
    POSITION_OPEN_WIDTH,
    build_position_close_note,
    build_position_close_values,
    build_position_full_row,
    build_position_note,
    build_position_open_row,
    build_position_orphan_note,
    position_marker,
    position_token,
)

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_TZ = "Europe/Moscow"


def _position(**overrides: Any) -> dict[str, Any]:
    """Закрытая сделка XRP с круглыми числами: ответ виден глазом."""
    base: dict[str, Any] = {
        "id": 123,
        "signal_id": 73856,
        "symbol": "XRP/USDT",
        "side": "buy",
        "status": "closed",
        # 17:34:12 UTC = 20:34:12 MSK; закрытие 18:33 UTC = 21:33 MSK.
        "signal_ts": datetime(2026, 8, 31, 17, 34, 12, tzinfo=UTC),
        "opened_at": datetime(2026, 8, 31, 17, 34, 16, tzinfo=UTC),
        "entry_price": 1.4161,
        "notional_usd": 2.0,
        "entry_lag_sec": 4,
        "target_price": 1.4300,
        "target_pct": 0.98,
        "stop_price": 1.4019,
        "stop_pct": 1.00,
        "cost_pct": 0.22,
        "closed_at": datetime(2026, 8, 31, 18, 33, 0, tzinfo=UTC),
        "exit_price": 1.4300,
        "exit_reason": "target",
        "net_pnl_pct": 0.76,
        "net_pnl_usd": 0.015,
        "outcome_certain": True,
        "probability": 0.83,
        "sheet_opened_at": None,
        "sheet_closed_at": None,
    }
    base.update(overrides)
    return base


# =============================================================================
# §11.1–§11.4. Состав, порядок, пояс и типы значений
# =============================================================================

def test_the_open_row_is_exactly_seven_values_in_the_given_order() -> None:
    """§11.1: открытие — РОВНО семь значений A–G, закрытие — ровно три.

    Ширина здесь не формальность: пачка шириной восемь залезла бы в столбец H
    (дата выхода) у ещё открытой сделки, а шириной десять — в J. Столбцы K и
    правее не задевает ни один из двух диапазонов, и это единственное, что
    бережёт формулы владельца от перезаписи значением.
    """
    row = build_position_open_row(_position(), _TZ)
    assert len(row) == POSITION_OPEN_WIDTH == 7
    assert row == [
        "31.08.2026",   # A дата входа
        "20:34:12",     # B время СИГНАЛА, не входа
        "XRP",          # C токен
        "покупать",     # D сигнал
        "",             # E разделитель
        1.4161,         # F цена открытия
        2.0,            # G объём входа
    ]

    close = build_position_close_values(_position(), _TZ)
    assert len(close) == POSITION_CLOSE_WIDTH == 3
    assert close == ["31.08.2026", "21:33:00", 1.43]
    # Дозапись начинается с восьмого столбца: 8 + 3 − 1 = 10, то есть J.
    assert POSITION_CLOSE_START_COLUMN + POSITION_CLOSE_WIDTH - 1 == 10


def test_the_column_b_holds_the_signal_time_not_the_entry_time() -> None:
    """B — время СИГНАЛА, A — дата ВХОДА. Это разные моменты.

    Между ними ``entry_lag_sec`` (на боевом сервере — секунды), и заголовки
    листа названы именно так. Подменить одно другим — значит тихо сдвинуть
    момент решения к моменту покупки и потерять саму величину задержки.
    """
    row = build_position_open_row(
        _position(
            signal_ts=datetime(2026, 8, 31, 17, 0, 0, tzinfo=UTC),
            opened_at=datetime(2026, 8, 31, 17, 5, 0, tzinfo=UTC),
        ),
        _TZ,
    )
    assert row[1] == "20:00:00"   # сигнал
    assert row[0] == "31.08.2026"


def test_time_is_moscow_because_telegram_shows_moscow() -> None:
    """§11.2: время переведено в Europe/Moscow, а не оставлено в UTC.

    Строка листа и сообщение бота обязаны говорить об одном событии ОДНИМИ
    числами: иначе сверить их (критерий приёмки §12.6) попросту невозможно.
    """
    row = _position(closed_at=datetime(2026, 8, 31, 18, 33, 0, tzinfo=UTC))
    values = build_position_close_values(row, _TZ)
    assert values[1].startswith("21:33")
    # А тот же момент в UTC даёт ДРУГОЕ время — иначе тест проходил бы при любом
    # поясе и не проверял бы ничего.
    assert build_position_close_values(row, "UTC")[1].startswith("18:33")
    # Переход через полночь двигает и дату.
    late = _position(closed_at=datetime(2026, 8, 31, 22, 30, 0, tzinfo=UTC))
    assert build_position_close_values(late, _TZ)[0] == "01.09.2026"


def test_the_token_is_short_not_the_pair() -> None:
    """§11.3: в листе стоит XRP, а не XRP/USDT."""
    assert position_token("XRP/USDT") == "XRP"
    assert build_position_open_row(_position(), _TZ)[2] == "XRP"
    for pair, token in (
        ("BTC/USDT", "BTC"), ("ETH/USDT", "ETH"), ("SOL/USDT", "SOL"),
        ("DOGE/USDT", "DOGE"),
    ):
        assert build_position_open_row(_position(symbol=pair), _TZ)[2] == token


def test_prices_and_volume_are_numbers_not_strings() -> None:
    """§11.4: цены и объём — ЧИСЛА.

    Запятая, знак доллара и разрядные пробелы — это формат ячейки, а не
    содержимое. Число, отправленное строкой, ляжет текстом, будет выглядеть в
    ячейке точно так же и тихо сломает формулы, которые на него ссылаются.
    """
    row = build_position_open_row(_position(), _TZ)
    assert isinstance(row[5], (int, float)) and not isinstance(row[5], bool)
    assert isinstance(row[6], (int, float)) and not isinstance(row[6], bool)
    close = build_position_close_values(_position(), _TZ)
    assert isinstance(close[2], (int, float)) and not isinstance(close[2], bool)
    # А даты и время — наоборот, строки заданного вида.
    assert isinstance(row[0], str) and isinstance(row[1], str)


def test_the_volume_is_the_slot_not_a_chain_from_the_previous_trade() -> None:
    """Столбец G — фактический размер слота, а не «выход предыдущей сделки».

    В листе-образце там была цепочка: один кошелёк, прибыль реинвестируется.
    Система ведёт ПЯТЬ одновременных слотов по два доллара и прибыль не
    реинвестирует. Позиции по разным токенам идут внахлёст во времени, и сложить
    их в одну цепь значило бы показать доходность несуществующего счёта — числа
    получились бы правдоподобные, а это худший вид ошибки.
    """
    first = build_position_open_row(_position(id=1, net_pnl_usd=5.0), _TZ)
    second = build_position_open_row(_position(id=2, net_pnl_usd=-3.0), _TZ)
    assert first[6] == second[6] == 2.0


def test_an_unknown_side_is_an_error_not_an_empty_cell() -> None:
    """Неизвестное направление — исключение, а не пустая ячейка.

    Пустая ячейка в столбце «сигнал» выглядит как «забыли заполнить» и живёт в
    листе вечно; исключение видно в тот же прогон.
    """
    with pytest.raises(ValueError) as failure:
        build_position_open_row(_position(side="sell"), _TZ)
    assert "сигнал" in str(failure.value)


# =============================================================================
# §11.5–§11.6. Заметка: метка, содержание, особый случай data_gap
# =============================================================================

def test_the_note_starts_with_the_marker_and_carries_the_levels() -> None:
    """§11.5: заметка начинается с ``[поз. <id>]`` и несёт цель, предел, сигнал.

    МЕТКА — ЕДИНСТВЕННЫЙ СПОСОБ НАЙТИ СТРОКУ ПРИ ДОЗАПИСИ. Лист владелец правит
    руками, строки могут переехать, а искать сделку по дате и цене значило бы
    однажды дописать выход не в ту строку.
    """
    note = build_position_note(_position())
    assert note.startswith("[поз. 123] ")
    assert position_marker(123) == "[поз. 123]"
    assert "цель 1.4300 (+0.98%)" in note
    assert "предел 1.4019 (−1.00%)" in note
    assert "сигнал #73856" in note
    assert "вероятность 0.83" in note
    assert "задержка входа 4 с" in note


def test_a_marker_is_never_a_prefix_of_another_marker() -> None:
    """``[поз. 1]`` не находится внутри ``[поз. 12]`` — и это по построению.

    Метка ищется ПОДСТРОКОЙ, и первое, о чём думаешь при виде такого поиска, —
    не захватит ли короткий номер длинный. Не захватит: закрывающая скобка стоит
    сразу за номером, и у соседа на этом месте цифра. Проверяется явно, потому
    что «по построению» — это утверждение, а не доказательство: смени шаблон на
    ``поз. {id}`` без скобки, и дозапись сделки №1 ушла бы в сделку №12.
    """
    for short, long_ in ((1, 12), (2, 21), (7, 77), (13, 130)):
        assert position_marker(short) not in position_marker(long_), (
            f"{position_marker(short)} нашлась внутри {position_marker(long_)}"
        )
    # И метка действительно встречается в собственной заметке — иначе строку
    # не нашли бы вовсе.
    assert position_marker(123) in build_position_note(_position(id=123))
    assert position_marker(123) in build_position_close_note(
        _position(id=123), _TZ
    )


def test_a_missing_probability_is_a_dash_not_a_zero() -> None:
    """Вероятности нет — прочерк, а не ноль.

    Ноль означал бы «система была уверена, что сделка провальная», а на самом
    деле величина просто не записана.
    """
    note = build_position_note(_position(probability=None))
    assert "вероятность —" in note
    assert "вероятность 0.00" not in note


def test_the_close_note_keeps_the_marker_and_adds_reason_and_result() -> None:
    """§11.6: заметка закрытия — причина словами и итог системы.

    Метка остаётся ПЕРВОЙ: по ней строку нашли и по ней же найдут снова, если
    владелец допишет вокруг свой текст.
    """
    note = build_position_close_note(_position(), _TZ)
    assert note.startswith("[поз. 123] ")
    assert "цель достигнута" in note
    assert "итог системы +0.76% ($+0.015) с учётом издержек 0.22%" in note

    words = {
        "stop": "сработал предел убытка",
        "timeout": "истёк срок",
        "ambiguous": "задеты обе границы",
    }
    for reason, expected in words.items():
        assert expected in build_position_close_note(
            _position(exit_reason=reason), _TZ
        )


def test_a_data_gap_close_says_the_price_was_restored_and_gives_no_result(
) -> None:
    """§11.6: у ``data_gap`` вместо итога — оговорка о восстановленной цене.

    ЗАКРЫТИЯ ПО ПРОБЕЛУ ПОПАДАЮТ В ЛИСТ, и это решение владельца от 30.08.2026.
    Строка уже создана при открытии, и «не писать» означало бы вечно открытую
    строку — это хуже, чем восстановленное число с явной пометкой. Но выдавать
    его за измеренный итог нельзя: цена выхода не наблюдалась.
    """
    note = build_position_close_note(
        _position(exit_reason="data_gap", net_pnl_pct=-0.22,
                  net_pnl_usd=-0.0044, outcome_certain=False),
        _TZ,
    )
    assert "пробел в данных" in note
    assert DATA_GAP_NOTE in note
    assert "в статистику точности не идёт" in note
    # ОБЫЧНОГО ИТОГА НЕТ ВОВСЕ: иначе восстановленное число попало бы в сверку
    # с листом наравне с настоящими.
    assert "итог системы" not in note


def test_the_orphan_row_says_what_happened_in_plain_words() -> None:
    """§11.10 (сторона текста): потерянная строка названа прямо.

    Полная строка A–J и заметка, начинающаяся словами о случившемся: человек,
    открывший лист, обязан понять, почему эта сделка стоит отдельно, не
    расследуя.
    """
    orphan = build_position_full_row(_position(), _TZ)
    assert len(orphan) == 10
    assert orphan[:7] == build_position_open_row(_position(), _TZ)
    assert orphan[7:] == build_position_close_values(_position(), _TZ)

    note = build_position_orphan_note(_position(), _TZ)
    assert note.startswith("строка открытия не найдена")
    assert "[поз. 123]" in note
    assert "цель достигнута" in note


# =============================================================================
# §11.7–§11.11. Клиент: очередь, отметки, повторный прогон, выключатель
# =============================================================================

class _FakeConn:
    """Двойник соединения: очередь позиций в памяти, отметки как в базе.

    Двойник нужен затем, чтобы проверять ФАКТ отправки и ФАКТ отметки. Скрипт,
    напечатавший «готово» и ничего не отправивший, прошёл бы проверку по выводу.
    """

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = [dict(row) for row in rows]
        self.marked_open: list[int] = []
        self.marked_closed: list[int] = []

    async def fetchval(self, query: str, *args: Any) -> Any:
        if "to_regclass" in query:
            return True
        return 0

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        # Запросы различаются по своему же условию — тому самому, что стоит в
        # src/export/queries.py. Пересказывать SQL в двойнике нельзя, но выбрать
        # по нему нужную ветку — можно.
        if "sheet_opened_at IS NULL" in query:
            return [r for r in self.rows if r["sheet_opened_at"] is None]
        return [
            r for r in self.rows
            if r["status"] == "closed"
            and r["sheet_closed_at"] is None
            and r["sheet_opened_at"] is not None
        ]

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any]:
        return {"to_open": 0, "to_close": 0}

    async def execute(self, query: str, *args: Any) -> str:
        ids = set(args[0]) if args else set()
        if "sheet_opened_at = now()" in query:
            self.marked_open.extend(sorted(ids))
            for row in self.rows:
                if row["id"] in ids and row["sheet_opened_at"] is None:
                    row["sheet_opened_at"] = datetime.now(UTC)
        elif "sheet_closed_at = now()" in query:
            self.marked_closed.extend(sorted(ids))
            for row in self.rows:
                if row["id"] in ids and row["sheet_closed_at"] is None:
                    row["sheet_closed_at"] = datetime.now(UTC)
        return "UPDATE 0"


class _Recorder:
    """Подменённый клиент листа: запоминает запросы, отвечает по сценарию."""

    def __init__(self, not_found: list[str] | None = None,
                 ok: bool = True) -> None:
        self.calls: list[dict[str, Any]] = []
        self.not_found = not_found or []
        self.ok = ok

    async def __call__(self, url, secret, sheet, mode, rows, header=None, **kw):
        self.calls.append(
            {"sheet": sheet, "mode": mode, "rows": rows, **kw}
        )
        if not self.ok:
            return sheets.SheetsResult(ok=False, error="приёмник отказал")
        # Ненайденные метки возвращаются ОДИН раз: второй запрос (спасательный
        # table_append) обязан пройти начисто.
        not_found, self.not_found = self.not_found, []
        return sheets.SheetsResult(
            ok=True, inserted=len(rows), updated=len(kw.get("updates") or []),
            start_row=2, not_found=not_found,
        )


async def _run_trades(monkeypatch, conn: _FakeConn, recorder: _Recorder,
                      enabled: bool = True) -> tuple[int, int]:
    """Прогоняет НАСТОЯЩИЙ ``_export_trades`` на двойниках."""
    import structlog

    import src.export_main as export_main

    monkeypatch.setattr(settings, "SHEETS_TRADES_ENABLED", enabled)
    monkeypatch.setattr(settings, "SHEETS_WEBAPP_URL", "https://example.invalid")
    monkeypatch.setattr(settings, "SHEETS_SHARED_SECRET", "секрет")
    monkeypatch.setattr(export_main.sheets, "post_rows", recorder)
    return await export_main._export_trades(conn, structlog.get_logger())


async def test_an_open_position_goes_to_opens_and_not_to_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§11.7: открытая позиция попадает в выборку открытий и только в неё.

    Дозаписывать нечего: выхода ещё не было, и строка с датой закрытия у живой
    сделки была бы утверждением о будущем.
    """
    conn = _FakeConn([_position(id=1, status="open", closed_at=None,
                                exit_price=None, exit_reason=None)])
    opens = await queries.fetch_positions_pending_open(conn, 100)
    closes = await queries.fetch_positions_pending_close(conn, 100)
    assert [row["id"] for row in opens] == [1]
    assert closes == []

    recorder = _Recorder()
    created, updated = await _run_trades(monkeypatch, conn, recorder)
    assert (created, updated) == (1, 0)
    assert [call["mode"] for call in recorder.calls] == ["table_append"]
    # Ширина пачки — семь: столбцы H..J у живой сделки не трогаются.
    assert all(len(row) == 7 for row in recorder.calls[0]["rows"])
    assert conn.marked_open == [1]
    assert conn.marked_closed == []


async def test_a_failed_send_leaves_the_mark_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§11.8: ответ ``ok:false`` — отметка НЕ ставится, пачка остаётся в очереди.

    Иначе сбой сети означал бы навсегда потерянную сделку: очередь считает её
    записанной, а в листе её нет.
    """
    conn = _FakeConn([_position(id=1, status="open", closed_at=None)])
    recorder = _Recorder(ok=False)
    with pytest.raises(Exception) as failure:
        await _run_trades(monkeypatch, conn, recorder)
    assert "торговый журнал" in str(failure.value)
    assert conn.marked_open == []
    # И позиция по-прежнему в очереди — следующий прогон возьмёт её снова.
    assert len(await queries.fetch_positions_pending_open(conn, 100)) == 1


async def test_open_then_close_in_one_pass_then_nothing_at_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§11.9: повторный прогон после успешного не отправляет НИ ОДНОЙ строки.

    ЭТО ГЛАВНАЯ ПРОВЕРКА ЭТАПА. Вторая строка той же сделки в чужом рабочем
    листе — необратимая порча: удалять её будет владелец руками, а формулы
    итогов посчитают её наравне с настоящими.

    Заодно проверяется порядок §6: сделка, открытая и закрытая между двумя
    прогонами, получает строку и дозапись ЗА ОДИН ПРОХОД — сначала открытия,
    потом закрытия.
    """
    conn = _FakeConn([_position(id=123)])
    recorder = _Recorder()
    created, updated = await _run_trades(monkeypatch, conn, recorder)
    assert (created, updated) == (1, 1)
    assert [call["mode"] for call in recorder.calls] == [
        "table_append", "table_update",
    ]
    update = recorder.calls[1]["updates"][0]
    assert update["marker"] == "[поз. 123]"
    assert update["startColumn"] == POSITION_CLOSE_START_COLUMN
    assert len(update["values"]) == 3
    assert conn.marked_open == [123] and conn.marked_closed == [123]

    # ВТОРОЙ ПРОГОН — тишина.
    again = _Recorder()
    created2, updated2 = await _run_trades(monkeypatch, conn, again)
    assert (created2, updated2) == (0, 0)
    assert again.calls == []


async def test_a_lost_open_row_becomes_a_full_row_with_a_plain_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§11.10: приёмник вернул ``notFound`` — клиент кладёт полную строку.

    Строку НЕ УГАДЫВАЕМ: дописать выход не в ту строку хуже, чем не дописать
    вовсе. Потерянная сделка хуже лишней строки — поэтому сделка попадает в лист
    целиком, с заметкой, объясняющей, почему она стоит отдельно.
    """
    conn = _FakeConn([
        _position(id=55, sheet_opened_at=datetime(2026, 8, 31, tzinfo=UTC)),
    ])
    recorder = _Recorder(not_found=["[поз. 55]"])
    created, updated = await _run_trades(monkeypatch, conn, recorder)

    modes = [call["mode"] for call in recorder.calls]
    assert modes == ["table_update", "table_append"]
    rescue = recorder.calls[1]
    assert len(rescue["rows"]) == 1
    assert len(rescue["rows"][0]) == 10, "спасательная строка обязана быть A–J"
    assert rescue["notes"][0].startswith("строка открытия не найдена")
    # Сделка отмечена как записанная: следующий прогон её не повторит.
    assert conn.marked_closed == [55]
    assert (created, updated) == (1, 1)


async def test_nothing_touches_the_network_while_the_flag_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§11.11: при ``SHEETS_TRADES_ENABLED=false`` запросов нет ВОВСЕ.

    Не «есть и ничего не находят», а не выполняются: ни запроса к базе, ни
    обращения к сети. Первая запись в чужой рабочий лист обязана произойти по
    сознательному решению владельца.
    """
    assert settings.SHEETS_TRADES_ENABLED is False, (
        "по умолчанию запись в торговый журнал обязана быть ВЫКЛЮЧЕНА"
    )

    class _Explode:
        async def fetch(self, *a: Any, **k: Any) -> Any:
            raise AssertionError("обращение к базе при выключенном флаге")

        async def fetchval(self, *a: Any, **k: Any) -> Any:
            raise AssertionError("обращение к базе при выключенном флаге")

    recorder = _Recorder()
    created, updated = await _run_trades(
        monkeypatch, _Explode(), recorder, enabled=False
    )
    assert (created, updated) == (0, 0)
    assert recorder.calls == []


async def test_the_close_batch_never_carries_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Дозапись адресуется МЕТКАМИ, а не порядком строк.

    В режиме ``table_update`` строк нет вовсе: порядок в листе владельца может
    отличаться от порядка в очереди, и адресация по номеру строки однажды
    дописала бы выход в чужую сделку.
    """
    conn = _FakeConn([
        _position(id=7, sheet_opened_at=datetime(2026, 8, 31, tzinfo=UTC)),
    ])
    recorder = _Recorder()
    await _run_trades(monkeypatch, conn, recorder)
    call = recorder.calls[0]
    assert call["mode"] == "table_update"
    assert call["rows"] == []
    assert call["note_column"] == POSITION_NOTE_COLUMN


def test_the_payload_of_table_update_carries_no_rows_and_no_header() -> None:
    """Клиент не отправляет в чужой лист ни строк, ни заголовка.

    Заголовок в торговом журнале уже есть: послать свой значило бы переписать
    строку 1 чужого рабочего документа.
    """
    import inspect

    source = inspect.getsource(sheets.post_rows)
    body = source.split('if mode == "table_update":', 1)[1].split("else:", 1)[0]
    assert '"updates"' in body
    assert '"rows"' not in body
    assert '"header"' not in body


def test_the_client_and_the_receiver_agree_on_every_field_name() -> None:
    """Имена полей запроса совпадают у клиента (Python) и приёмника (JS).

    ЭТО КЛАССИЧЕСКИЙ СПОСОБ СЛОМАТЬ ДВУЯЗЫЧНЫЙ ДОГОВОР: переименовать поле на
    одной стороне. Компилятор такого не поймает — ни один из двух языков не
    видит другого, — а приёмник просто возьмёт значение по умолчанию и запишет
    строку не туда, куда просили. Здесь имена сверяются буквально.
    """
    client = (_ROOT / "src" / "export" / "sheets.py").read_text(encoding="utf-8")
    receiver = (_ROOT / "deploy" / "apps_script.gs").read_text(encoding="utf-8")

    # Поля верхнего уровня: клиент кладёт — приёмник читает.
    for field, reader in (
        ("notes", "body.notes"),
        ("noteColumn", "body.noteColumn"),
        ("totalsMarker", "body.totalsMarker"),
        ("formulaFromColumn", "body.formulaFromColumn"),
        ("updates", "body.updates"),
    ):
        assert f'"{field}"' in client, f"клиент не отправляет {field}"
        assert reader in receiver, f"приёмник не читает {field}"

    # Поля одной записи дозаписи.
    export_main = (_ROOT / "src" / "export_main.py").read_text(encoding="utf-8")
    for field, reader in (
        ("marker", "item.marker"),
        ("values", "item.values"),
        ("note", "item.note"),
        ("startColumn", "item.startColumn"),
    ):
        assert f'"{field}"' in export_main, f"клиент не отправляет {field}"
        assert reader in receiver, f"приёмник не читает {field}"

    # И ответ: клиент читает ровно те поля, которые приёмник кладёт.
    for field in ("inserted", "updated", "startRow", "warning", "notFound"):
        assert f'"{field}"' in client, f"клиент не читает {field} из ответа"
        assert field in receiver, f"приёмник не возвращает {field}"


def test_the_cron_runs_positions_only_every_fifteen_minutes() -> None:
    """Отдельная задача cron трогает ТОЛЬКО лист сделок и идёт раз в 15 минут.

    Полная выгрузка пересобирает служебные листы целиком (mode=replace): гонять
    её каждые пятнадцать минут значило бы переписывать четыре листа ради пятого.
    """
    cron = (_ROOT / "deploy" / "agent-trade-positions.cron").read_text(
        encoding="utf-8"
    )
    assert "*/15 * * * *" in cron
    assert "--positions-only" in cron
    # Старая задача не тронута и по-прежнему идёт раз в сутки.
    old = (_ROOT / "deploy" / "agent-trade-export.cron").read_text(
        encoding="utf-8"
    )
    assert "20 6 * * *" in old
    assert "--positions-only" not in old


def test_the_receiver_declares_the_new_version_and_both_table_modes() -> None:
    """Приёмник обновлён: версия 9.1.2 и оба режима торгового журнала.

    Версия — не косметика: по ней в журнале выгрузки видно, что владелец
    действительно переразвернул скрипт на стороне Google. Старая версия ответит
    на ``table_append`` ошибкой, и это правильно — видимый отказ лучше тихой
    записи не туда.
    """
    receiver = (_ROOT / "deploy" / "apps_script.gs").read_text(encoding="utf-8")
    assert "const RECEIVER_VERSION = '9.1.2';" in receiver
    assert "function tableAppend(" in receiver
    assert "function tableUpdate(" in receiver
    # Строка ищется СВЕРХУ и не ниже итогов, а не добавляется в конец листа.
    assert "insertRowsBefore" in receiver
    assert "findTotalsRow" in receiver
    # Формулы протягиваются копированием, а не сочиняются.
    assert "PASTE_FORMULA" in receiver


def test_the_migration_020_declares_both_marks_and_the_queue_index() -> None:
    """Миграция 020: две отметки и частичный индекс очереди, идемпотентно."""
    migration = (
        _ROOT / "db" / "migrations" / "020_positions_sheet_export.sql"
    ).read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS sheet_opened_at TIMESTAMPTZ" in migration
    assert "ADD COLUMN IF NOT EXISTS sheet_closed_at TIMESTAMPTZ" in migration
    assert "CREATE INDEX IF NOT EXISTS positions_sheet_pending_idx" in migration

    rollback = (
        _ROOT / "db" / "migrations" / "020_positions_sheet_export_rollback.sql"
    ).read_text(encoding="utf-8")
    assert "DROP COLUMN IF EXISTS sheet_opened_at" in rollback
    assert "DROP COLUMN IF EXISTS sheet_closed_at" in rollback


def test_the_stage_does_not_touch_the_decision_path() -> None:
    """Граница этапа: правила и сервис позиций не изменены ни одним символом.

    Проверка структурная и дешёвая, но именно она ловит случай «поправил заодно»:
    выгрузка обязана только ЧИТАТЬ позиции.
    """
    for path in (
        "src/positions/rules.py", "src/positions/runner.py",
    ):
        text = (_ROOT / path).read_text(encoding="utf-8")
        assert "sheet_opened_at" not in text, path
        assert "sheet_closed_at" not in text, path
        assert "торговля тест апи" not in text, path


def test_a_closed_position_without_an_open_mark_waits_for_its_row(
) -> None:
    """Закрытие не дозаписывается, пока строки открытия нет в листе.

    ЭТО СОЗНАТЕЛЬНОЕ УЖЕСТОЧЕНИЕ §6 ТЗ, и без него возникает вторая строка.
    Позиция, чья строка открытия не записалась (сбой сети), попала бы в дозапись,
    получила бы ``notFound`` и легла бы полной строкой — а следующий прогон
    увидел бы её ещё раз в выборке ОТКРЫТИЙ (отметки-то нет) и создал бы вторую.
    """
    closed_without_row = _position(id=9, sheet_opened_at=None)
    conn = _FakeConn([closed_without_row])

    async def _check() -> None:
        opens = await queries.fetch_positions_pending_open(conn, 100)
        closes = await queries.fetch_positions_pending_close(conn, 100)
        assert [row["id"] for row in opens] == [9]
        assert closes == [], "закрытие ушло раньше строки открытия"

    import asyncio

    asyncio.run(_check())
    # И текст запроса содержит само условие — чтобы правку заметили.
    source = (_ROOT / "src" / "export" / "queries.py").read_text(
        encoding="utf-8"
    )
    assert "p.sheet_opened_at IS NOT NULL" in source


def test_the_full_run_also_handles_the_trade_journal() -> None:
    """Полная выгрузка обрабатывает лист сделок тоже (§8 ТЗ).

    На случай, если отдельная задача cron не будет установлена: тогда сделки
    попадут в лист хотя бы раз в сутки, а не никогда.
    """
    source = (_ROOT / "src" / "export_main.py").read_text(encoding="utf-8")
    run_body = source.split("async def _run(", 1)[1]
    assert "_export_trades(conn, log)" in run_body
    # И этот вызов НЕ спрятан под `if not positions_only` — иначе он выполнялся
    # бы только в кратком режиме или только в полном.
    trades_call = run_body.split("_export_trades(conn, log)", 1)[0]
    tail = trades_call.rsplit("if not positions_only:", 1)
    assert len(tail) == 1 or "_export_sheets" in tail[1], (
        "вызов торгового журнала оказался внутри ветки полного режима"
    )
