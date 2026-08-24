"""Тесты Этапа 8.4.1: ширина диапазона не зависит от первой строки пачки.

Дефект, который стерегут эти тесты. Приёмник Apps Script брал ширину диапазона
записи как ``rows[0].length``. Первой строкой листа «Независимые окна» идёт
оговорка из ОДНОГО элемента, а строки данных — из пятнадцати, и запись падала:
«Die Spaltenzahl in den Daten stimmt nicht … In den Daten sind es 15, im
Bereich jedoch 1» (живой прогон 24.08.2026 15:35 UTC).

Проверяется обе стороны:
  * отправитель (:func:`src.export.sheets.normalize_batch`) выравнивает пачку
    ДО отправки — это снимает отказ и на ещё не обновлённом приёмнике;
  * приёмник (``deploy/apps_script.gs``) считает ширину сам — его логика
    прогоняется отдельным стендом на Node, см. ``test_receiver_harness``.

Ни один тест не подгоняет оговорку под пятнадцать колонок: ширина везде
считается ПО НАБОРУ, поэтому смена состава колонок запись не ломает.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from src.export.sheets import SheetsResult, normalize_batch, post_rows
from src.export.transform import (
    CORRELATION_HEADER,
    INDEPENDENT_DISCLAIMER,
    INDEPENDENT_HEADER,
    SIGNALS_HEADER,
    SUMMARY_HEADER,
)

_ROOT = Path(__file__).resolve().parents[1]


def _row(width: int, tag: str) -> list[Any]:
    return [f"{tag}{i}" for i in range(width)]


# --- отправитель: выравнивание пачки ---

def test_ragged_batch_is_padded_to_widest_row() -> None:
    """Строки РАЗНОЙ длины в одном наборе — штатный случай, а не отказ."""
    rows = [["одна"], ["две", "штуки"], ["три", "штуки", "ровно"]]
    padded, header = normalize_batch(rows, ["a", "b"])
    assert [len(r) for r in padded] == [3, 3, 3]
    assert header is not None and len(header) == 3
    assert padded[0] == ["одна", "", ""]


def test_header_participates_in_width() -> None:
    """Заголовок задаёт ширину наравне со строками, а не отдельно от них."""
    padded, header = normalize_batch([["x"]], ["a", "b", "c", "d"])
    assert header is not None and len(header) == 4
    assert padded == [["x", "", "", ""]]


def test_disclaimer_stays_first_row() -> None:
    """Оговорка обязана остаться видимой первой строкой листа."""
    data = [_row(15, "a"), _row(15, "b")]
    padded, _ = normalize_batch([INDEPENDENT_DISCLAIMER, *data], INDEPENDENT_HEADER)
    assert padded[0][0] == INDEPENDENT_DISCLAIMER[0]
    assert all(cell == "" for cell in padded[0][1:])


def test_independent_sheet_batch_is_uniform() -> None:
    """Пачка листа «Независимые окна» собирается ровно как в export_main."""
    data = [_row(len(INDEPENDENT_HEADER), "r") for _ in range(3)]
    padded, header = normalize_batch(
        [INDEPENDENT_DISCLAIMER, *data], INDEPENDENT_HEADER
    )
    widths = {len(r) for r in padded}
    assert widths == {len(INDEPENDENT_HEADER)}
    assert header is not None and len(header) == len(INDEPENDENT_HEADER)


@pytest.mark.parametrize(
    "header", [SIGNALS_HEADER, SUMMARY_HEADER, INDEPENDENT_HEADER, CORRELATION_HEADER]
)
def test_every_sheet_header_width_is_preserved(header: list[str]) -> None:
    """Выравнивание не сужает и не расширяет лист сверх его заголовка."""
    padded, padded_header = normalize_batch([_row(len(header), "x")], header)
    assert padded_header is not None and len(padded_header) == len(header)
    assert len(padded[0]) == len(header)


def test_empty_batch_is_not_an_error() -> None:
    """Пустой набор без заголовка не должен ломать отправку."""
    padded, header = normalize_batch([], None)
    assert padded == []
    assert header is None


def test_normalize_does_not_mutate_input() -> None:
    """Исходные строки не меняются: их читает и вызывающий код."""
    original = [["одна"], ["две", "штуки"]]
    snapshot = [list(r) for r in original]
    normalize_batch(original, None)
    assert original == snapshot


# --- отправитель: что реально уходит в запрос ---

class _FakeResponse:
    status_code = 200

    def __init__(self, captured: dict[str, Any]) -> None:
        self._captured = captured

    def json(self) -> dict[str, Any]:
        return {"ok": True, "inserted": len(self._captured["rows"]),
                "version": "8.4.1"}


class _FakeClient:
    def __init__(self, captured: dict[str, Any], **kwargs: Any) -> None:
        self._captured = captured

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def post(self, url: str, json: dict[str, Any]) -> _FakeResponse:
        self._captured.update(json)
        return _FakeResponse(self._captured)


async def test_post_rows_sends_uniform_widths(monkeypatch: pytest.MonkeyPatch) -> None:
    """В запрос уходит пачка одной ширины — рваной она приёмника не достигает."""
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        "src.export.sheets.httpx.AsyncClient",
        lambda **kwargs: _FakeClient(captured, **kwargs),
    )
    result = await post_rows(
        "https://example.test/exec", "секрет", "Независимые окна", "replace",
        [INDEPENDENT_DISCLAIMER, _row(15, "a"), _row(15, "b")],
        header=INDEPENDENT_HEADER,
    )
    assert isinstance(result, SheetsResult) and result.ok
    widths = {len(r) for r in captured["rows"]}
    assert widths == {15}, f"в запрос ушли строки разной ширины: {widths}"
    assert len(captured["header"]) == 15
    assert captured["rows"][0][0] == INDEPENDENT_DISCLAIMER[0]


async def test_post_rows_reports_receiver_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Версия приёмника попадает в результат — по ней видно, что он обновлён."""
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        "src.export.sheets.httpx.AsyncClient",
        lambda **kwargs: _FakeClient(captured, **kwargs),
    )
    result = await post_rows(
        "https://example.test/exec", "секрет", "Лист", "replace",
        [["x"]], header=["a"],
    )
    assert result.receiver_version == "8.4.1"


# --- приёмник: стенд на Node ---

def test_receiver_harness() -> None:
    """Логика deploy/apps_script.gs на двойнике Google Sheets.

    Стенд строг там же, где строг Google: setValues бросает ту же ошибку о
    несовпадении числа колонок. Контрольный сценарий внутри стенда показывает,
    что ПРЕЖНЯЯ редакция на той же пачке падает — то есть стенд действительно
    ловит исходный дефект, а не проходит мимо него.
    """
    if shutil.which("node") is None:
        pytest.skip("node не установлен — стенд приёмника не выполняется")
    harness = _ROOT / "tests" / "apps_script" / "receiver_harness.mjs"
    proc = subprocess.run(
        ["node", str(harness)], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Все сценарии стенда прошли" in proc.stdout
