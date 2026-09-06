"""Этап 9.1.2.3: занятая метка при создании строки открытия — два разных случая.

ЧТО ЗДЕСЬ ДОКАЗЫВАЕТСЯ.

Приёмник версии 9.1.2.2 отвечает на занятую метку в ``table_append`` ОДНИМ И ТЕМ
ЖЕ образом: строки не создал, метку вернул в ``ambiguous`` вместе с номерами
строк, где она встретилась. Клиент до этого этапа обрабатывал такой ответ тоже
одним способом — отметку не ставил и печатал ``error``. Для случая «строк две и
больше» это верно и остаётся верным. Для случая «строка ровно одна» это тупик:

  * строка открытия в листе УЖЕ ЕСТЬ и метка в ней уникальна — значит, она
    принадлежит именно этой позиции;
  * отметки ``sheet_opened_at`` в базе нет, а без неё позиция не попадёт в
    выборку дозаписи закрытия НИКОГДА (``fetch_positions_pending_close``
    требует ``sheet_opened_at IS NOT NULL``);
  * каждый следующий прогон повторяет ту же ошибку, и выход по сделке не будет
    дописан никогда.

Именно в этом тупике 06.09.2026 оказались шесть позиций (26, 27, 28, 59, 60,
63): партия ``table_append`` дала ``rows=6, created=0, skipped_ambiguous=6``, и
по каждой позиции приёмник вернул РОВНО ОДИН номер строки.

ГРАНИЦА ПРОВЕРОК. Приёмник (``deploy/apps_script.gs``) этим этапом НЕ МЕНЯЕТСЯ,
и его поведение здесь не перепроверяется: оно закреплено стендом
``tests/apps_script/receiver_harness.mjs``, который запускается из
``tests/test_stage_9_1_2_2.py``. Здесь проверяется КЛИЕНТ — и проверяется на
НАСТОЯЩЕЙ базе PostgreSQL (§7 ТЗ), потому что доказываемое утверждение целиком
про состояние базы: какая отметка проставлена, какая нет и кто после этого
попадает в очередь. Двойник базы отвечал бы на этот вопрос своим устройством, а
не устройством ``positions``.

Тесты, которым нужна БАЗА, включаются переменной ``AT_TEST_DSN``. Без неё они
ПРОПУСКАЮТСЯ с явной причиной — они не «зелёные», они не выполнялись.
``AT_TEST_DSN`` обязан указывать на ОДНОРАЗОВУЮ базу.

    AT_TEST_DSN=postgresql://postgres@127.0.0.1:5433/at_test \
        python -m pytest tests/test_stage_9_1_2_3.py
"""

from __future__ import annotations

import os
import pathlib
import re
from datetime import UTC, datetime
from typing import Any

import pytest

import src.export_main as export_main
from src.core.config import settings
from src.export import sheets
from src.export.transform import position_marker

_ROOT = pathlib.Path(__file__).resolve().parents[1]

TEST_DSN = os.environ.get("AT_TEST_DSN", "")
needs_db = pytest.mark.skipif(
    not TEST_DSN,
    reason=(
        "нужна тестовая БД: задайте AT_TEST_DSN "
        "(postgresql://user@host:port/dbname), база должна быть ОДНОРАЗОВОЙ"
    ),
)

# Одноразовый инструмент этих проверок. Отдельный символ нужен затем, чтобы
# уборка в конце теста удаляла ровно свои строки и не трогала чужие.
_SYMBOL = "TEST9123/USDT"


# =============================================================================
# Двойники
# =============================================================================

class _Log:
    """Логгер, запоминающий строки журнала вместе с их полями.

    Проверяется не только состояние базы, но и то, ЧТО написано в журнал:
    уровень и машиночитаемые ключи — часть требований §5 ТЗ, а не оформление.
    По ним владелец считает случаи одной командой, и подменить ``error`` на
    ``info`` там, где сделка потеряна, значило бы спрятать потерю.
    """

    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, Any]]] = []

    def _add(self, level: str, event: str, **kw: Any) -> None:
        self.records.append((level, {"event": event, **kw}))

    def info(self, event: str, **kw: Any) -> None:
        self._add("info", event, **kw)

    def warning(self, event: str, **kw: Any) -> None:
        self._add("warning", event, **kw)

    def error(self, event: str, **kw: Any) -> None:
        self._add("error", event, **kw)

    def bind(self, **_kw: Any) -> _Log:
        return self

    def of(self, level: str) -> list[dict[str, Any]]:
        return [payload for lvl, payload in self.records if lvl == level]

    def field(self, key: str) -> list[Any]:
        return [payload[key] for _, payload in self.records if key in payload]


class _Sheet:
    """Двойник листа с ПРАВИЛОМ ЗАНЯТОЙ МЕТКИ приёмника 9.1.2.2.

    Он повторяет ровно то, что делает ``deploy/apps_script.gs``, и ровно в той
    части, которая для этих проверок существенна:

      * ``table_append`` — метка, уже встречающаяся в столбце заметок, строки НЕ
        получает; метка возвращается в ``ambiguous`` вместе со ВСЕМИ номерами
        строк, где она встретилась (одним, двумя — сколько есть);
      * ``table_update`` — одно совпадение пишется, ноль уходит в ``notFound``,
        два и больше — в ``ambiguous``, и не пишется ничего.

    ЗАЧЕМ ДВОЙНИК, ЕСЛИ ЕСТЬ НАСТОЯЩИЙ ПРИЁМНИК. Настоящий живёт в JavaScript на
    стороне Google, и его поведение проверяет стенд ``receiver_harness.mjs``.
    Здесь доказывается поведение КЛИЕНТА, и двойник нужен затем, чтобы задать
    входное условие опыта — «метка в листе встречается столько-то раз», — а не
    затем, чтобы что-то утверждать про Google.
    """

    def __init__(self, notes: list[str] | None = None) -> None:
        # Каждая строка листа — (значения A.., заметка). Номер строки — индекс
        # плюс два: строка 1 занята заголовком, как в настоящем листе.
        self.rows: list[dict[str, Any]] = [
            {"values": [], "note": note} for note in (notes or [])
        ]
        self.calls: list[dict[str, Any]] = []

    def note_rows(self, marker: str) -> list[int]:
        """Номера строк листа, в заметке которых встречается метка целиком."""
        return [
            index + 2 for index, row in enumerate(self.rows)
            if marker and marker in str(row["note"])
        ]

    @staticmethod
    def _marker_of(note: Any) -> str:
        found = re.match(r"\s*(\[[^\]]*\])", str(note or ""))
        return found.group(1) if found else ""

    async def __call__(
        self, url: str, secret: str, sheet: str, mode: str,
        rows: list[list[Any]], header: list[str] | None = None, **kw: Any,
    ) -> sheets.SheetsResult:
        self.calls.append({"sheet": sheet, "mode": mode, "rows": rows, **kw})
        if mode == "version":
            return sheets.SheetsResult(ok=True, receiver_version="9.1.2.2")
        if mode == "table_append":
            return self._append(rows, kw.get("notes") or [])
        if mode == "table_update":
            return self._update(kw.get("updates") or [])
        raise AssertionError(f"двойник не знает режима {mode!r}")

    def _append(
        self, rows: list[list[Any]], notes: list[str]
    ) -> sheets.SheetsResult:
        ambiguous: list[dict[str, Any]] = []
        start_row: int | None = None
        inserted = 0
        seen_in_batch: set[str] = set()
        for index, values in enumerate(rows):
            note = notes[index] if index < len(notes) else ""
            marker = self._marker_of(note)
            if marker:
                clash = self.note_rows(marker)
                if clash:
                    ambiguous.append({"marker": marker, "rows": clash})
                    continue
                if marker in seen_in_batch:
                    # Повтор ВНУТРИ пачки: строк в листе нет ни одной, поэтому
                    # и номеров нет. Приёмник отвечает пустым списком.
                    ambiguous.append({"marker": marker, "rows": []})
                    continue
                seen_in_batch.add(marker)
            self.rows.append({"values": list(values), "note": note})
            if start_row is None:
                start_row = len(self.rows) + 1
            inserted += 1
        return sheets.SheetsResult(
            ok=True, inserted=inserted, start_row=start_row,
            receiver_version="9.1.2.2", ambiguous=ambiguous,
        )

    def _update(self, updates: list[dict[str, Any]]) -> sheets.SheetsResult:
        not_found: list[str] = []
        ambiguous: list[dict[str, Any]] = []
        updated = 0
        for item in updates:
            marker = str(item.get("marker", ""))
            found = self.note_rows(marker)
            if not found:
                not_found.append(marker)
                continue
            if len(found) > 1:
                ambiguous.append({"marker": marker, "rows": found})
                continue
            row = self.rows[found[0] - 2]
            row["closed"] = list(item.get("values") or [])
            row["note"] = str(row["note"]) + str(item.get("noteAppend") or "")
            updated += 1
        return sheets.SheetsResult(
            ok=True, updated=updated, receiver_version="9.1.2.2",
            not_found=not_found, ambiguous=ambiguous,
        )


def _result(**over: Any) -> sheets.SheetsResult:
    """Ответ приёмника с ok=true и заданными полями — для проверок разбора."""
    fields: dict[str, Any] = {"ok": True, "receiver_version": "9.1.2.2"}
    fields.update(over)
    return sheets.SheetsResult(**fields)


# =============================================================================
# §5. Разбор ответа: три случая по числу возвращённых строк
# =============================================================================

def test_one_row_restores_the_mark_and_says_so_at_info() -> None:
    """§5, случай 1: одна строка — отметка ВОССТАНАВЛИВАЕТСЯ, запись уровня info.

    Уровень выбран не по громкости, а по смыслу: строка открытия в листе есть и
    она одна, дозапись закрытия в неё теперь пойдёт — человеку чинить нечего.
    Признак ``sheets_marker_restored=1`` стоит отдельным полем, и в той же
    записи стоит ``created=False``: строка НЕ создавалась, и отчитаться о ней
    как о записанной значило бы соврать в обе стороны сразу.
    """
    log = _Log()
    recovered, refused = export_main._split_ambiguous_open(
        _result(ambiguous=[{"marker": position_marker(26), "rows": [27]}]),
        [{"id": 26}],
        log,
    )
    assert recovered == {26}
    assert refused == set()
    assert log.of("error") == [], "восстановление отметки не ошибка"
    restored = log.of("info")
    assert len(restored) == 1
    assert restored[0]["sheets_marker_restored"] == 1
    assert restored[0]["position_id"] == 26
    assert restored[0]["row"] == 27
    assert restored[0]["created"] is False
    assert "sheets_ambiguous_marker" not in restored[0]


def test_two_rows_stay_a_refusal_exactly_as_in_9_1_2_2() -> None:
    """§5, случай 2: две строки и больше — поведение не меняется НИ В ЧЁМ.

    Это защита Этапа 9.1.2.2, и трогать её нельзя: какая из строк чья —
    неизвестно, и любой выбор был бы догадкой. Ни отметки, ни новой строки,
    ``error`` с ``sheets_ambiguous_marker=1``.
    """
    log = _Log()
    recovered, refused = export_main._split_ambiguous_open(
        _result(ambiguous=[{"marker": position_marker(11), "rows": [12, 13]}]),
        [{"id": 11}],
        log,
    )
    assert recovered == set(), "отметка восстановлена по неизвестно чьей строке"
    assert refused == {position_marker(11)}
    assert log.of("info") == []
    errors = log.of("error")
    assert len(errors) == 1
    assert errors[0]["sheets_ambiguous_marker"] == 1
    assert errors[0]["position_id"] == 11
    assert errors[0]["rows"] == [12, 13]


def test_no_ambiguous_at_all_is_the_ordinary_case() -> None:
    """§5, случай 3: метки в листе нет — сюда ответ вовсе не приходит.

    Приёмник в этом случае создаёт строку обычным порядком и в ``ambiguous`` её
    не возвращает. Разбор обязан вернуть пусто и ничего не написать в журнал:
    строка журнала на штатном пути — это шум, в котором тонут настоящие.
    """
    log = _Log()
    recovered, refused = export_main._split_ambiguous_open(
        _result(inserted=1, start_row=27), [{"id": 26}], log
    )
    assert (recovered, refused) == (set(), set())
    assert log.records == []


def test_zero_rows_is_a_refusal_and_not_a_recovery() -> None:
    """Пустой список номеров — ОТКАЗ, а не «строка есть».

    Пустой список приходит от приёмника в одном случае: повтор одной метки
    ВНУТРИ пачки (``seenInBatch``). Строки при этом нет ни одной, создана она не
    была, и отмечать нечего. Прочитать «ноль» как «строка уже есть» значило бы
    объявить выгруженной сделку, которой в листе нет вовсе, — и это ровно та
    потеря, ради которой написан этап.
    """
    log = _Log()
    recovered, refused = export_main._split_ambiguous_open(
        _result(ambiguous=[{"marker": position_marker(26), "rows": []}]),
        [{"id": 26}],
        log,
    )
    assert recovered == set()
    assert refused == {position_marker(26)}
    assert log.of("error")[0]["rows"] == []


def test_a_marker_outside_the_batch_is_a_refusal_with_no_position() -> None:
    """Метка, которой нет в пачке, — отказ, и ``position_id`` в журнале пуст.

    Позиции по такой метке мы не знаем, и восстанавливать отметку не у чего.
    Разбирать номер из текста метки нельзя: формат метки — договор двух сторон
    провода, и второй его разборщик однажды разошёлся бы с первым.
    """
    log = _Log()
    recovered, refused = export_main._split_ambiguous_open(
        _result(ambiguous=[{"marker": "[поз. 999]", "rows": [5]}]),
        [{"id": 26}],
        log,
    )
    assert recovered == set()
    assert refused == {"[поз. 999]"}
    assert log.of("error")[0]["position_id"] is None


def test_the_receiver_version_requirement_is_untouched() -> None:
    """§4: требуемая версия приёмника осталась 9.1.2.2, и приёмник не менялся.

    Этап правит ТОЛЬКО клиента. Поднять требование к версии значило бы
    потребовать развернуть в Google скрипт, которого этот этап не писал, и
    остановить выгрузку до тех пор, пока этого не сделают.
    """
    assert export_main._TRADES_RECEIVER_VERSION == "9.1.2.2"
    receiver = (_ROOT / "deploy" / "apps_script.gs").read_text(encoding="utf-8")
    assert "var RECEIVER_VERSION = '9.1.2.2'" in receiver or (
        "RECEIVER_VERSION = '9.1.2.2'" in receiver
    ), "версия приёмника в deploy/apps_script.gs изменилась"


# =============================================================================
# §7. Контрольные опыты на НАСТОЯЩЕМ PostgreSQL
# =============================================================================

async def _connect():
    import asyncpg

    return await asyncpg.connect(dsn=TEST_DSN)


async def _clean(conn) -> None:
    """Убирает строки этого файла. Чужих строк не трогает."""
    await conn.execute(
        "DELETE FROM positions WHERE instrument_id IN "
        "(SELECT id FROM instruments WHERE symbol = $1);", _SYMBOL,
    )
    await conn.execute(
        "DELETE FROM signals WHERE instrument_id IN "
        "(SELECT id FROM instruments WHERE symbol = $1);", _SYMBOL,
    )
    await conn.execute("DELETE FROM instruments WHERE symbol = $1;", _SYMBOL)


async def _empty_queue_or_fail(conn) -> None:
    """Требует пустой очереди выгрузки ДО опыта.

    Чужая строка в очереди попала бы в ту же пачку и в тот же ответ приёмника, и
    опыт проверял бы не то, что задумано. Молча продолжать в этот момент нельзя:
    тест не «прошёл бы», он измерял бы другое.
    """
    from src.export import queries

    to_open, to_close = await queries.count_positions_pending(conn)
    assert (to_open, to_close) == (0, 0), (
        f"в тестовой базе уже есть очередь выгрузки (открытий={to_open}, "
        f"закрытий={to_close}) — опыт проверял бы не свои строки. AT_TEST_DSN "
        "обязан указывать на ОДНОРАЗОВУЮ базу"
    )


async def _make_position(conn, *, status: str = "open") -> int:
    """Одна позиция без отметок выгрузки. Возвращает её ``id``."""
    instrument_id = await conn.fetchval(
        "INSERT INTO instruments (exchange, symbol, base, quote, type) "
        "VALUES ('okx', $1, 'TEST9123', 'USDT', 'spot') "
        "ON CONFLICT (exchange, symbol, type) DO UPDATE "
        "SET symbol = EXCLUDED.symbol RETURNING id;", _SYMBOL,
    )
    signal_id = await conn.fetchval(
        "INSERT INTO signals (instrument_id, ts, decision, logic_version, "
        "probability) VALUES ($1, now(), 'buy', 5, 0.83) RETURNING id;",
        instrument_id,
    )
    closed = status == "closed"
    return int(await conn.fetchval(
        """
        INSERT INTO positions
            (instrument_id, signal_id, logic_version, horizon_h, side, status,
             signal_ts, signal_price, opened_at, entry_price, entry_lag_sec,
             entry_slippage_pct, qty, notional_usd, target_pct, target_price,
             stop_pct, stop_price, cost_pct, deadline_at, resolution,
             closed_at, exit_price, exit_reason, outcome_certain,
             net_pnl_pct, net_pnl_usd)
        VALUES ($1, $2, 5, 24, 'buy', $3, now() - interval '2 hours', 100,
                now() - interval '2 hours', 100, 4, 0, 0.02, 2, 1, 101, 1, 99,
                0.22, now() + interval '22 hours', '1m',
                $4, $5, $6, $7, $8, $9)
        RETURNING id;
        """,
        instrument_id, signal_id, status,
        datetime.now(UTC) if closed else None,
        101.0 if closed else None,
        "target" if closed else None,
        True if closed else None,
        0.78 if closed else None,
        0.0156 if closed else None,
    ))


async def _close(conn, position_id: int) -> None:
    """Закрывает позицию — так же, как это делает служба ведения позиций."""
    await conn.execute(
        "UPDATE positions SET status = 'closed', closed_at = now(), "
        "exit_price = 101, exit_reason = 'target', outcome_certain = TRUE, "
        "net_pnl_pct = 0.78, net_pnl_usd = 0.0156, updated_at = now() "
        "WHERE id = $1;", position_id,
    )


async def _marks(conn, position_id: int) -> tuple[Any, Any]:
    row = await conn.fetchrow(
        "SELECT sheet_opened_at, sheet_closed_at FROM positions WHERE id = $1;",
        position_id,
    )
    return (row["sheet_opened_at"], row["sheet_closed_at"])


async def _run(monkeypatch, conn, sheet: _Sheet, log: _Log) -> tuple[int, int]:
    """Прогоняет НАСТОЯЩИЙ ``_export_trades`` на настоящей базе."""
    monkeypatch.setattr(settings, "SHEETS_TRADES_ENABLED", True)
    monkeypatch.setattr(settings, "SHEETS_WEBAPP_URL", "https://example.invalid")
    monkeypatch.setattr(settings, "SHEETS_SHARED_SECRET", "секрет")
    monkeypatch.setattr(export_main.sheets, "post_rows", sheet)
    return await export_main._export_trades(conn, log)


@needs_db
async def test_experiment_1_one_row_restores_the_mark_and_the_close_follows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§7, опыт 1: метка в ОДНОЙ строке.

    Проверяется всё, что требует ТЗ, и в том порядке, в каком это случается на
    сервере:

      1. отметка открытия проставлена — база приведена в соответствие с листом;
      2. НОВОЙ СТРОКИ НЕ СОЗДАНО — лист не тронут вовсе;
      3. позиция попала в очередь дозаписи, и СЛЕДУЮЩИЙ прогон записал закрытие
         в ТУ ЖЕ строку.

    Пункт 3 — то, ради чего этап написан: до правки эта позиция не попадала в
    очередь закрытий никогда, потому что выборка требует ``sheet_opened_at IS
    NOT NULL``.
    """
    conn = await _connect()
    try:
        await _clean(conn)
        await _empty_queue_or_fail(conn)
        position_id = await _make_position(conn, status="open")

        # Строка открытия в листе УЖЕ ЕСТЬ — ровно одна, с меткой этой позиции.
        # Так выглядел боевой лист 06.09.2026 по каждой из шести позиций.
        sheet = _Sheet([f"{position_marker(position_id)} цель 101.00"])
        log = _Log()
        created, updated = await _run(monkeypatch, conn, sheet, log)

        assert (created, updated) == (0, 0), (
            "строка открытия объявлена созданной, хотя она уже была"
        )
        assert len(sheet.rows) == 1, "в лист дописана вторая строка той же сделки"
        opened_at, closed_at = await _marks(conn, position_id)
        assert opened_at is not None, "отметка открытия не восстановлена"
        assert closed_at is None, "закрытия ещё не было — отметка не за что"
        assert log.of("error") == []
        assert 1 in log.field("sheets_marker_restored")

        # СЛЕДУЮЩИЙ ПРОГОН: позиция закрылась и обязана попасть в дозапись.
        await _close(conn, position_id)
        log2 = _Log()
        created2, updated2 = await _run(monkeypatch, conn, sheet, log2)

        assert (created2, updated2) == (0, 1), "закрытие не дописано"
        assert len(sheet.rows) == 1, "закрытие ушло новой строкой вместо своей"
        assert sheet.rows[0].get("closed"), "столбцы H, I, J остались пустыми"
        opened_at2, closed_at2 = await _marks(conn, position_id)
        assert opened_at2 == opened_at, "отметка открытия переписана заново"
        assert closed_at2 is not None, "отметка закрытия не поставлена"
        assert log2.of("error") == []
    finally:
        await _clean(conn)
        await conn.close()


@needs_db
async def test_experiment_1b_the_same_run_writes_the_close_of_a_closed_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§5: восстановленная отметка попадает в очередь дозаписи В ТОМ ЖЕ прогоне.

    ТЗ допускает и следующий прогон, но не позднее. Порядок внутри прогона
    жёсткий — сначала открытия, потом закрытия, — и он обеспечивает более
    строгое: сделка, закрывшаяся до восстановления отметки, дозаписывается сразу.
    Это свойство порядка, а не удачи, и проверяется оно затем, что порядок можно
    случайно поменять местами и не заметить.
    """
    conn = await _connect()
    try:
        await _clean(conn)
        await _empty_queue_or_fail(conn)
        position_id = await _make_position(conn, status="closed")
        sheet = _Sheet([f"{position_marker(position_id)} цель 101.00"])
        log = _Log()

        created, updated = await _run(monkeypatch, conn, sheet, log)

        assert (created, updated) == (0, 1)
        assert len(sheet.rows) == 1
        opened_at, closed_at = await _marks(conn, position_id)
        assert opened_at is not None and closed_at is not None
        assert log.of("error") == []
    finally:
        await _clean(conn)
        await conn.close()


@needs_db
async def test_experiment_2_two_rows_still_refuse_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§7, опыт 2: метка в ДВУХ строках — отказ, и он обязан остаться отказом.

    ЭТОТ ОПЫТ ОБЯЗАН ПАДАТЬ В ОТКАЗ. Если он проходит — сломана защита Этапа
    9.1.2.2, и цена этому известна: закрытие одной сделки уходит в строку
    другой, лист остаётся правдоподобным и становится неверным.
    """
    conn = await _connect()
    try:
        await _clean(conn)
        await _empty_queue_or_fail(conn)
        position_id = await _make_position(conn, status="closed")
        marker = position_marker(position_id)
        sheet = _Sheet([f"{marker} цель 101.00", f"{marker} и ещё раз"])
        log = _Log()

        created, updated = await _run(monkeypatch, conn, sheet, log)

        assert (created, updated) == (0, 0)
        assert len(sheet.rows) == 2, "к двум спорным строкам добавлена третья"
        opened_at, closed_at = await _marks(conn, position_id)
        assert opened_at is None, "отметка поставлена по неизвестно чьей строке"
        assert closed_at is None
        errors = log.of("error")
        assert [e["sheets_ambiguous_marker"] for e in errors] == [1]
        assert errors[0]["position_id"] == position_id
        assert errors[0]["rows"] == [2, 3]
        # ОЧЕРЕДЬ ОСТАЛАСЬ ПОЛНОЙ: следующий прогон возьмёт позицию снова, и
        # так до тех пор, пока лист не приведут в порядок руками.
        from src.export import queries
        assert await queries.count_positions_pending(conn) == (1, 0)
    finally:
        await _clean(conn)
        await conn.close()


@needs_db
async def test_experiment_3_no_marker_creates_the_row_as_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§7, опыт 3: метки в листе нет — строка создаётся обычным порядком.

    Проверка того, что правка ничего не сломала на штатном пути: он остаётся
    самым частым, и «починили редкий случай, потеряли обычный» — самый дорогой
    из возможных исходов этого этапа.
    """
    conn = await _connect()
    try:
        await _clean(conn)
        await _empty_queue_or_fail(conn)
        position_id = await _make_position(conn, status="open")
        sheet = _Sheet(["[поз. 999] чужая строка"])
        log = _Log()

        created, updated = await _run(monkeypatch, conn, sheet, log)

        assert (created, updated) == (1, 0)
        assert len(sheet.rows) == 2, "строка открытия не создана"
        assert position_marker(position_id) in str(sheet.rows[1]["note"])
        opened_at, _ = await _marks(conn, position_id)
        assert opened_at is not None
        assert log.of("error") == []
        assert log.field("sheets_marker_restored") == [], (
            "штатное создание строки выдано за восстановление отметки"
        )
    finally:
        await _clean(conn)
        await conn.close()


@needs_db
async def test_experiment_4_an_empty_queue_asks_the_receiver_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§7, опыт 4: пустая очередь — НИ ОДНОГО запроса к приёмнику.

    Задача cron идёт каждые 15 минут, и в большинстве прогонов писать нечего.
    Вопрос о версии — тоже запрос: сто обращений в сутки ради ответа «нечего
    делать» это трафик и записи в журнале ради ничего (§15.4 ТЗ 9.1.2).
    """
    conn = await _connect()
    try:
        await _clean(conn)
        await _empty_queue_or_fail(conn)
        sheet = _Sheet()
        log = _Log()

        created, updated = await _run(monkeypatch, conn, sheet, log)

        assert (created, updated) == (0, 0)
        assert sheet.calls == [], (
            f"при пустой очереди сделано {len(sheet.calls)} запросов к приёмнику"
        )
    finally:
        await _clean(conn)
        await conn.close()


@needs_db
async def test_the_restored_mark_keeps_the_moment_of_the_first_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Повторный прогон не переписывает восстановленную отметку.

    ``mark_positions_sheet_opened`` ставит отметку только там, где её нет
    (``WHERE sheet_opened_at IS NULL``). Свойство важно и для восстановленной
    отметки: по колонке по-прежнему должно читаться, когда позиция перестала
    быть невыгруженной, а не когда её последний раз трогали.
    """
    conn = await _connect()
    try:
        await _clean(conn)
        await _empty_queue_or_fail(conn)
        position_id = await _make_position(conn, status="open")
        sheet = _Sheet([f"{position_marker(position_id)} цель 101.00"])

        await _run(monkeypatch, conn, sheet, _Log())
        first, _ = await _marks(conn, position_id)
        # Второй прогон: очередь открытий уже пуста, и трогать нечего.
        await _run(monkeypatch, conn, sheet, _Log())
        second, _ = await _marks(conn, position_id)

        assert first is not None and second == first
        assert len(sheet.rows) == 1
    finally:
        await _clean(conn)
        await conn.close()
