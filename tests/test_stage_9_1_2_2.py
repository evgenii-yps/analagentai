"""Этап 9.1.2.2: метка сделки не затирается, дозапись по неоднозначной запрещена.

ЧТО ЗДЕСЬ ДОКАЗЫВАЕТСЯ, и почему именно это.

ДВЕ ПРАВКИ ЭТАПА — ОБ ОДНОМ И ТОМ ЖЕ: чужие данные в своей строке.

  1. ПРОТЯЖКА ФОРМУЛ ЗАТИРАЛА ЗАМЕТКУ. Приёмник копировал диапазон K..последний
     столбец вызовом ``copyTo`` с ``PASTE_FORMULA``, а тот переносит и ячейки
     БЕЗ формул — своим значением. Столбец заметок T лежит внутри диапазона, и
     каждая созданная строка получала заметку строки выше. На боевом листе
     31.08.2026 так вышли три строки подряд с меткой ``[поз. 10]``.

  2. ДОЗАПИСЬ ШЛА В ПЕРВУЮ СТРОКУ С МЕТКОЙ. Когда строк с меткой стало три, цена
     выхода одной сделки ушла бы в строку другой. Это тихая порча данных: лист
     остаётся правдоподобным и становится неверным.

ГРАНИЦА ПРОВЕРОК ЗДЕСЬ ПРОХОДИТ ПО ЯЗЫКУ, А НЕ ПО ВАЖНОСТИ. Логика приёмника
живёт в JavaScript на стороне Google, и проверяется она стендом
``tests/apps_script/receiver_harness.mjs``, который прогоняет НАСТОЯЩИЙ
``deploy/apps_script.gs`` в Node. Здесь этот стенд ЗАПУСКАЕТСЯ (§5.6, §5.7 ТЗ):
пересказывать его логику на Python значило бы завести второй экземпляр той же
логики, и однажды он разошёлся бы с настоящим приёмником.

ЧЕГО ЭТИ ПРОВЕРКИ НЕ ДОКАЗЫВАЮТ. Ни стенд, ни двойники не доказывают поведения
НАСТОЯЩЕГО Google Apps Script: двойник листа написан нами и знает ровно то, что
мы про Google поняли. Прежний двойник, например, был мягче настоящего — он не
переносил литералы при ``PASTE_FORMULA``, и ровно поэтому стенд Этапа 9.1.2 не
увидел дефекта, ради которого написан этот этап. Двойник теперь исправлен, но
единственным настоящим подтверждением остаётся журнал следующей выгрузки на
сервере и вид листа после неё.
"""

from __future__ import annotations

import pathlib
import subprocess
from datetime import UTC, datetime
from typing import Any

import pytest

from src.core.config import settings
from src.export import sheets
from src.export.transform import (
    POSITION_NOTE_COLUMN,
    build_position_orphan_note,
    position_marker,
)

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_TZ = "Europe/Moscow"


def _position(**over: Any) -> dict[str, Any]:
    """Закрытая позиция со всеми полями, которые читают сборщики строк."""
    row: dict[str, Any] = {
        "id": 123,
        "signal_id": 73875,
        "symbol": "ETH/USDT",
        "side": "buy",
        "status": "closed",
        "signal_ts": datetime(2026, 8, 31, 1, 15, 27, tzinfo=UTC),
        "opened_at": datetime(2026, 8, 31, 1, 15, 31, tzinfo=UTC),
        "closed_at": datetime(2026, 8, 31, 2, 24, 0, tzinfo=UTC),
        "entry_price": 2472.80,
        "exit_price": 2448.07,
        "notional_usd": 2.0,
        "target_price": 2535.33,
        "stop_price": 2479.56,
        "target_pct": 1.23,
        "stop_pct": 1.00,
        "probability": 0.83,
        "entry_lag_sec": 4,
        "exit_reason": "stop",
        "outcome_certain": True,
        "net_pnl_pct": -1.22,
        "net_pnl_usd": -0.024,
        "cost_pct": 0.22,
        "sheet_opened_at": datetime(2026, 8, 31, 1, 30, tzinfo=UTC),
        "sheet_closed_at": None,
    }
    row.update(over)
    return row


# =============================================================================
# Двойники клиента: очередь позиций в памяти и подменённый клиент листа
# =============================================================================

class _FakeConn:
    """Двойник соединения: очередь позиций и отметки, как в базе.

    Двойник нужен затем, чтобы проверять ФАКТ отметки, а не вывод в журнал.
    Скрипт, напечатавший «готово» и ничего не отметивший, прошёл бы проверку по
    выводу; ровно эта разница и решает судьбу сделки в следующем прогоне.
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
        if "sheet_opened_at IS NULL" in query:
            return [r for r in self.rows if r["sheet_opened_at"] is None]
        return [
            r for r in self.rows
            if r["status"] == "closed"
            and r["sheet_closed_at"] is None
            and r["sheet_opened_at"] is not None
        ]

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any]:
        return {
            "to_open": sum(1 for r in self.rows if r["sheet_opened_at"] is None),
            "to_close": sum(
                1 for r in self.rows
                if r["status"] == "closed" and r["sheet_closed_at"] is None
                and r["sheet_opened_at"] is not None
            ),
        }

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
    """Подменённый клиент листа: запоминает запросы, отвечает по сценарию.

    ``ambiguous`` и ``not_found`` отдаются РОВНО ОДИН РАЗ — первым записывающим
    запросом. Второй запрос (спасательный ``table_append`` после ``notFound``)
    обязан пройти начисто, иначе проверка «новой строки не создаётся» доказывала
    бы лишь то, что двойник упрямо повторяет один и тот же ответ.
    """

    def __init__(
        self,
        *,
        ambiguous: list[dict[str, Any]] | None = None,
        not_found: list[str] | None = None,
        version: str | None = "9.1.2.2",
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.ambiguous = ambiguous or []
        self.not_found = not_found or []
        self.version = version

    @property
    def writes(self) -> list[dict[str, Any]]:
        """Только ЗАПИСЫВАЮЩИЕ запросы: вопрос о версии ничего не пишет."""
        return [call for call in self.calls if call["mode"] != "version"]

    async def __call__(self, url, secret, sheet, mode, rows, header=None, **kw):
        self.calls.append({"sheet": sheet, "mode": mode, "rows": rows, **kw})
        if mode == "version":
            return sheets.SheetsResult(ok=True, receiver_version=self.version)
        ambiguous, self.ambiguous = self.ambiguous, []
        not_found, self.not_found = self.not_found, []
        return sheets.SheetsResult(
            ok=True,
            inserted=max(0, len(rows) - len(ambiguous)),
            updated=max(0, len(kw.get("updates") or []) - len(ambiguous)),
            start_row=2,
            not_found=not_found,
            ambiguous=ambiguous,
            receiver_version=self.version,
        )


async def _run_trades(monkeypatch, conn: _FakeConn, recorder: _Recorder,
                      ) -> tuple[int, int]:
    """Прогоняет НАСТОЯЩИЙ ``_export_trades`` на двойниках."""
    import structlog

    import src.export_main as export_main

    monkeypatch.setattr(settings, "SHEETS_TRADES_ENABLED", True)
    monkeypatch.setattr(settings, "SHEETS_WEBAPP_URL", "https://example.invalid")
    monkeypatch.setattr(settings, "SHEETS_SHARED_SECRET", "секрет")
    monkeypatch.setattr(export_main.sheets, "post_rows", recorder)
    return await export_main._export_trades(conn, structlog.get_logger())


# =============================================================================
# §5.1–§5.4. Поведение клиента при ambiguous
# =============================================================================

async def test_ambiguous_on_open_leaves_both_marks_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§5.1 (открытие): по неоднозначной метке отметка НЕ ставится.

    Отметка необратима: поставив её, мы объявили бы позицию выгруженной, тогда
    как строки в листе у неё нет — есть чужая строка с её меткой. Позиция должна
    остаться в очереди и повторяться каждым прогоном, пока лист не приведут в
    порядок. Повторяющаяся ошибка в журнале — это цена за то, чтобы сделка не
    потерялась молча.
    """
    conn = _FakeConn([
        _position(id=11, status="open", closed_at=None, exit_price=None,
                  exit_reason=None, sheet_opened_at=None),
    ])
    recorder = _Recorder(
        ambiguous=[{"marker": position_marker(11), "rows": [12, 13]}]
    )
    created, updated = await _run_trades(monkeypatch, conn, recorder)

    assert (created, updated) == (0, 0)
    assert conn.marked_open == [], "отметка открытия поставлена по чужой строке"
    assert conn.marked_closed == []
    # Очередь ОСТАЛАСЬ ПОЛНОЙ: следующий прогон возьмёт позицию снова.
    assert conn.rows[0]["sheet_opened_at"] is None


async def test_ambiguous_on_close_leaves_both_marks_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§5.1 (закрытие): то же самое на дозаписи.

    Приёмник по неоднозначной метке не записал НИЧЕГО — ни столбцов H..J, ни
    заметки. Отметка ``sheet_closed_at`` здесь означала бы «закрытие в листе»,
    которого не произошло, и вернуть позицию в очередь было бы уже нечем.
    """
    conn = _FakeConn([_position(id=10)])
    recorder = _Recorder(
        ambiguous=[{"marker": position_marker(10), "rows": [12, 13, 14]}]
    )
    created, updated = await _run_trades(monkeypatch, conn, recorder)

    assert (created, updated) == (0, 0)
    assert conn.marked_closed == [], "отметка закрытия поставлена мимо строки"
    assert conn.rows[0]["sheet_closed_at"] is None


async def test_ambiguous_never_makes_the_client_create_a_new_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§5.2: при ambiguous клиент НЕ отправляет запроса на создание строки.

    Это главное отличие ``ambiguous`` от ``notFound``, и оно содержательно: при
    ``notFound`` строки НЕТ, и лишняя строка лучше потерянной сделки; при
    ``ambiguous`` строк уже СЛИШКОМ МНОГО, и добавлять к ним ещё одну —
    усугублять. Разбирать лист всё равно придётся руками, но разбирать станет на
    строку больше.
    """
    conn = _FakeConn([_position(id=10)])
    recorder = _Recorder(
        ambiguous=[{"marker": position_marker(10), "rows": [12, 13]}]
    )
    await _run_trades(monkeypatch, conn, recorder)

    modes = [call["mode"] for call in recorder.calls]
    assert modes == ["version", "table_update"], f"лишние запросы: {modes}"
    assert not any(call["mode"] == "table_append" for call in recorder.writes)


async def test_not_found_still_creates_the_full_rescue_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§5.3: при notFound прежнее поведение сохранено полностью.

    Проверка нужна именно рядом с предыдущей: правка, запретившая создавать
    строку при ``ambiguous``, легко могла бы запретить её и при ``notFound`` —
    и тогда потерянная сделка исчезла бы совсем.
    """
    conn = _FakeConn([_position(id=10)])
    recorder = _Recorder(not_found=[position_marker(10)])
    created, updated = await _run_trades(monkeypatch, conn, recorder)

    modes = [call["mode"] for call in recorder.calls]
    assert modes == ["version", "table_update", "table_append"]
    rescue = recorder.calls[-1]
    assert len(rescue["rows"]) == 1
    assert len(rescue["rows"][0]) == 10, "спасательная строка не полная A–J"
    assert rescue["notes"][0].startswith("строка открытия не найдена")
    assert rescue["note_column"] == POSITION_NOTE_COLUMN
    # Сделка ЗАПИСАНА, значит и отмечена: иначе она легла бы в лист дважды.
    assert conn.marked_closed == [10]
    assert (created, updated) == (1, 1)


async def test_one_ambiguous_marker_does_not_block_the_rest_of_the_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§5.4: неоднозначная метка одной позиции не задевает остальные.

    Останавливать всю пачку из-за одной испорченной строки значило бы поставить
    выгрузку в зависимость от скорости, с которой владелец разберёт лист: пока
    он не разберёт, не записалась бы НИ ОДНА сделка, и очередь росла бы.
    """
    conn = _FakeConn([_position(id=10), _position(id=11), _position(id=12)])
    recorder = _Recorder(
        ambiguous=[{"marker": position_marker(11), "rows": [12, 13]}]
    )
    created, updated = await _run_trades(monkeypatch, conn, recorder)

    assert conn.marked_closed == [10, 12], "пострадали соседи по пачке"
    assert 11 not in conn.marked_closed
    assert (created, updated) == (0, 2)


async def test_ambiguous_is_logged_with_machine_readable_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§3 ТЗ: строка журнала уровня error с ключами id, меток и строк.

    Ключ-признак стоит ОТДЕЛЬНЫМ ПОЛЕМ, а не словами в тексте сообщения: такие
    случаи надо уметь посчитать по журналу одной командой, не разбирая русский
    текст. Уровень ``error`` выбран не по громкости, а по смыслу — сделка не
    записана и записана не будет, пока человек не вмешается.
    """
    import structlog

    import src.export_main as export_main

    captured: list[tuple[str, dict[str, Any]]] = []

    class _Log:
        def info(self, event: str, **kw: Any) -> None:
            captured.append(("info", {"event": event, **kw}))

        def warning(self, event: str, **kw: Any) -> None:
            captured.append(("warning", {"event": event, **kw}))

        def error(self, event: str, **kw: Any) -> None:
            captured.append(("error", {"event": event, **kw}))

    result = sheets.SheetsResult(
        ok=True, ambiguous=[{"marker": position_marker(11), "rows": [12, 13]}]
    )
    markers = export_main._report_ambiguous(
        result, [_position(id=11)], _Log(), stage="закрытие"
    )

    assert markers == {position_marker(11)}
    errors = [payload for level, payload in captured if level == "error"]
    assert len(errors) == 1, "неоднозначная метка не попала в журнал"
    assert errors[0]["sheets_ambiguous_marker"] == 1
    assert errors[0]["position_id"] == 11
    assert errors[0]["rows"] == [12, 13]
    assert structlog is not None  # импорт нужен настройке журнала в модуле


def test_the_ambiguous_field_is_parsed_and_never_confused_with_not_found(
) -> None:
    """Клиент разбирает поле ``ambiguous`` и держит его отдельно от ``notFound``.

    Ответ приходит из-за сети, и форма его не гарантирована ничем, кроме нашего
    же кода на другом конце. Неразобранный элемент ПРОПУСКАЕТСЯ, а не роняет
    выгрузку и не превращается в пустую метку, которую потом никто не сопоставит
    с позицией.
    """
    parsed = sheets._parse_ambiguous(
        [
            {"marker": "[поз. 10]", "rows": [12, 13]},
            {"marker": "[поз. 11]", "rows": []},
            {"rows": [1]},          # без метки — сопоставить не с чем
            "мусор",                # не объект вовсе
            {"marker": "[поз. 12]", "rows": [3, "нет"]},
        ]
    )
    assert parsed == [
        {"marker": "[поз. 10]", "rows": [12, 13]},
        {"marker": "[поз. 11]", "rows": []},
        {"marker": "[поз. 12]", "rows": [3]},
    ]
    assert sheets._parse_ambiguous(None) == []
    # Поля РАЗНЫЕ и по умолчанию пустые: смешать их нельзя даже случайно.
    empty = sheets.SheetsResult(ok=True)
    assert empty.ambiguous == [] and empty.not_found == []


def test_the_orphan_note_still_names_what_happened() -> None:
    """§5.3 (сторона текста): заметка потерянной строки не изменилась.

    Она начинается не с метки, а со слов о случившемся — и именно поэтому
    проверка занятых меток в ``table_append`` её не отвергает: метки в начале
    заметки нет, отвергать нечего.
    """
    note = build_position_orphan_note(_position(id=10), _TZ)
    assert note.startswith("строка открытия не найдена")
    assert position_marker(10) in note


# =============================================================================
# §5.5. Скрипт восстановления: подтверждение числом
# =============================================================================

class _FakeDB:
    """Двойник базы для скрипта репарации: считает вызовы UPDATE.

    Проверяется именно ФАКТ вызова, а не текст вывода: скрипт, напечатавший
    отказ и всё-таки сбросивший отметки, прошёл бы проверку по выводу.
    """

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.resets: list[list[int]] = []

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def get_positions_sheet_marks(
        self, position_ids: list[int]
    ) -> list[dict[str, Any]]:
        wanted = {int(i) for i in position_ids}
        return [row for row in self.rows if int(row["id"]) in wanted]

    async def reset_positions_sheet_marks(self, position_ids: list[int]) -> int:
        self.resets.append([int(i) for i in position_ids])
        return len(position_ids)


def _mark_row(position_id: int) -> dict[str, Any]:
    return {
        "id": position_id,
        "symbol": "ETH/USDT",
        "status": "closed",
        "opened_at": datetime(2026, 8, 31, 1, 15, 31, tzinfo=UTC),
        "closed_at": datetime(2026, 8, 31, 2, 24, 0, tzinfo=UTC),
        "exit_reason": "stop",
        "sheet_opened_at": datetime(2026, 8, 31, 1, 30, tzinfo=UTC),
        "sheet_closed_at": datetime(2026, 8, 31, 2, 30, tzinfo=UTC),
    }


async def test_a_mismatched_confirm_count_resets_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§5.5: ``--apply`` с несовпавшим ``--confirm-count`` не делает ни одного UPDATE.

    Расхождение означает, что база изменилась между отчётом и сбросом:
    сбрасываемое множество уже не то, которое видел человек. Молча работать в
    этот момент нельзя.
    """
    import scripts.repair_9_1_2_2_marks as repair

    fake = _FakeDB([_mark_row(11), _mark_row(12), _mark_row(13)])
    monkeypatch.setattr(repair, "db", fake)
    monkeypatch.setattr(
        "sys.argv",
        ["repair", "--ids", "11,12,13", "--apply", "--confirm-count=2"],
    )
    code = await repair.main()

    assert code == 3, "несовпавшее подтверждение не остановило сброс"
    assert fake.resets == [], "UPDATE выполнен вопреки отказу"


async def test_a_matching_confirm_count_resets_exactly_the_named_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Обратная сторона: при совпавшем числе сброс идёт, и ровно по перечню.

    Без этой проверки предыдущая проходила бы и у скрипта, который не сбрасывает
    никогда.
    """
    import scripts.repair_9_1_2_2_marks as repair

    fake = _FakeDB([_mark_row(11), _mark_row(12), _mark_row(13)])
    monkeypatch.setattr(repair, "db", fake)
    monkeypatch.setattr(
        "sys.argv",
        ["repair", "--ids", "11,12,13", "--apply", "--confirm-count=3"],
    )
    code = await repair.main()

    assert code == 0
    assert fake.resets == [[11, 12, 13]]


async def test_a_run_without_apply_changes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Без ``--apply`` скрипт только печатает: ни одного UPDATE."""
    import scripts.repair_9_1_2_2_marks as repair

    fake = _FakeDB([_mark_row(11)])
    monkeypatch.setattr(repair, "db", fake)
    monkeypatch.setattr("sys.argv", ["repair", "--ids", "11"])
    code = await repair.main()

    assert code == 0
    assert fake.resets == []


async def test_an_unknown_id_stops_the_run_with_code_four(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Несуществующий id — остановка (код 4), а не тихий пропуск.

    Перечень набирается руками, и опечатка в нём вероятнее всего остального.
    Сбросить найденные и промолчать про остальные значило бы выполнить не ту
    команду, которую дали, и отчитаться об успехе.
    """
    import scripts.repair_9_1_2_2_marks as repair

    fake = _FakeDB([_mark_row(11)])
    monkeypatch.setattr(repair, "db", fake)
    monkeypatch.setattr(
        "sys.argv",
        ["repair", "--ids", "11,999", "--apply", "--confirm-count=1"],
    )
    code = await repair.main()

    assert code == 4
    assert fake.resets == []


def test_apply_without_confirm_count_is_refused_by_the_parser() -> None:
    """``--apply`` без ``--confirm-count`` не запускается вовсе.

    Подтверждение обязательно ПО ПОСТРОЕНИЮ, а не по дисциплине оператора:
    забыть флаг легче, чем набрать неверное число.
    """
    import scripts.repair_9_1_2_2_marks as repair

    assert repair.parse_ids("11, 12,13") == [11, 12, 13]
    # Повтор — опечатка, а не ошибка: иначе подтверждение проверяло бы длину
    # аргумента вместо факта.
    assert repair.parse_ids("11,11,12") == [11, 12]
    with pytest.raises(ValueError):
        repair.parse_ids("")
    with pytest.raises(ValueError):
        repair.parse_ids("11,двенадцать")

    source = (_ROOT / "scripts" / "repair_9_1_2_2_marks.py").read_text(
        encoding="utf-8"
    )
    assert "--apply без --confirm-count запрещён" in source
    # Меняются РОВНО ДВЕ колонки — ни строки не удаляются, ни решения не правятся.
    db_source = (_ROOT / "src" / "core" / "db.py").read_text(encoding="utf-8")
    reset = db_source.split("async def reset_positions_sheet_marks", 1)[1]
    reset = reset.split("async def ", 1)[0]
    assert "sheet_opened_at = NULL, sheet_closed_at = NULL" in reset
    assert "DELETE" not in reset


# =============================================================================
# §5.6, §5.7. Логика приёмника — на стенде, прогоняющем настоящий apps_script.gs
# =============================================================================

def test_the_receiver_harness_passes_every_scenario() -> None:
    """§5.6 и §5.7: стенд приёмника проходит целиком.

    Стенд прогоняет НАСТОЯЩИЙ ``deploy/apps_script.gs`` в Node на двойнике
    торгового журнала. Среди его сценариев — и подсчёт совпадений по метке
    (1 при одной, 2 при двух, 0 при отсутствии; ``[поз. 1]`` не совпадает со
    строкой, несущей ``[поз. 12]``), и отбор копируемых ячеек (ни одной без
    формулы и никогда столбец заметок).

    ЛОГИКА НЕ ПЕРЕСКАЗЫВАЕТСЯ ЗДЕСЬ НА PYTHON. Второй экземпляр той же логики
    однажды разошёлся бы с настоящим приёмником — и разошёлся бы молча, потому
    что проверять его было бы нечем.
    """
    harness = _ROOT / "tests" / "apps_script" / "receiver_harness.mjs"
    try:
        done = subprocess.run(
            ["node", str(harness)],
            cwd=str(_ROOT), capture_output=True, text=True, timeout=120,
        )
    except FileNotFoundError:
        pytest.skip("node не установлен: стенд приёмника не прогнан")

    assert done.returncode == 0, done.stdout + done.stderr
    # Сценарии этапа ДОЛЖНЫ БЫТЬ В ВЫВОДЕ: «стенд прошёл» на стенде, из которого
    # выпали проверки, — это отчёт ни о чём.
    for scenario in (
        "9.1.2.2: markerRows считает совпадения",
        "9.1.2.2: formulaColumnsToCopy не отдаёт ни литералов",
        "9.1.2.2: заметка НОВОЙ строки своя",
        "9.1.2.2: table_update при ДВУХ строках с меткой не пишет ничего",
    ):
        assert scenario in done.stdout, f"сценарий пропал: {scenario}"


def test_the_receiver_writes_the_note_after_pulling_formulas_down() -> None:
    """§1.3 ТЗ: заметка пишется ПОСЛЕ протяжки формул, а не до.

    Порядок здесь — часть исправления, а не оформление. Пока заметка писалась
    первой, любая ошибка в отборе копируемых ячеек стирала её молча; теперь она
    ложится последней и не зависит от того, что делает протяжка.
    """
    receiver = (_ROOT / "deploy" / "apps_script.gs").read_text(encoding="utf-8")
    append = receiver.split("function tableAppend(", 1)[1]
    append = append.split("\nfunction ", 1)[0]
    pull_at = append.index("pullFormulasDown(")
    note_at = append.index("setValue(keptNotes[i])")
    assert pull_at < note_at, "заметка пишется до протяжки формул"

    # Столбец заметок передаётся В протяжку: без него исключить его нечем.
    assert "pullFormulasDown(sheet, startRow, padded.length, formulaFrom,\n" in append
    assert "copyTo" not in append, "копирование осталось в обход отбора столбцов"


def test_the_receiver_declares_the_new_version_everywhere_it_is_named() -> None:
    """Версия приёмника поднята и совпадает с требуемой на стороне клиента.

    Расхождение этих двух мест — самый тихий из возможных отказов: клиент требует
    одну версию, приёмник объявляет другую, и выгрузка встаёт целиком.
    """
    receiver = (_ROOT / "deploy" / "apps_script.gs").read_text(encoding="utf-8")
    client = (_ROOT / "src" / "export_main.py").read_text(encoding="utf-8")
    assert "const RECEIVER_VERSION = '9.1.2.2';" in receiver
    assert '_TRADES_RECEIVER_VERSION = "9.1.2.2"' in client
    # Инструкция обновления называет ту же версию — по ней владелец проверяет,
    # что переразвернул скрипт.
    assert "receiver_version=9.1.2.2" in receiver
