"""Разделение рынков: свечи — спот, funding — контракт (поправка к §5.2 ТЗ).

Почему этот файл существует. Прогон на BTC-USDT-SWAP дал сверку §13.2 с
результатом market 0/200: сравнивались выводы, посчитанные на РАЗНЫХ РЫНКАХ.
Прогон на BTC-USDT упал на ``funding-rate-history`` с кодом 51000 «Parameter
instId error»: у спота ставок финансирования не существует. Обе поломки —
следствие одного допущения: «инструмент один». Здесь проверяется, что этого
допущения в коде больше нет:

  * инструмент задаётся ПАРОЙ и разбирается только явно (достраивание имени
    контракта запрещено);
  * при ``BT_AGENTS=market`` запрос funding не выполняется ВООБЩЕ;
  * снимок берёт свечи со спота, а ставки — с контракта;
  * сверка §13.2 сравнивает каждого агента на ЕГО рынке.

Тесты, не требующие БД, работают всегда. Проверки на реальных рядах включаются
переменной ``BT_TEST_DSN`` — без неё они ПРОПУСКАЮТСЯ, а не «зеленеют».
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from helpers import PAIR, SPOT, SWAP, T0, make_config, requires_db, seed_candles, seed_funding

from backtest import run as run_mod
from backtest.config import (
    BacktestConfig,
    ConfigError,
    InstrumentPair,
    load_config,
    parse_agents,
    parse_instruments,
)

# --- Разбор пары «спот:контракт» ------------------------------------------


def test_pair_is_parsed_explicitly() -> None:
    pair = InstrumentPair.parse("BTC-USDT:BTC-USDT-SWAP")
    assert pair.spot == "BTC-USDT"
    assert pair.swap == "BTC-USDT-SWAP"
    assert pair.key == "BTC-USDT"


def test_contract_name_is_never_derived_from_spot() -> None:
    """Одиночное имя остаётся спотом: «-SWAP» к нему не дописывается."""
    pair = InstrumentPair.parse("BTC-USDT")
    assert pair.spot == "BTC-USDT"
    assert pair.swap is None


def test_empty_contract_after_separator_is_an_error() -> None:
    with pytest.raises(ConfigError, match="достраивать"):
        InstrumentPair.parse("BTC-USDT:")


def test_more_than_one_separator_is_an_error() -> None:
    with pytest.raises(ConfigError, match="разделител"):
        InstrumentPair.parse("BTC-USDT:BTC-USDT-SWAP:X")


def test_duplicate_spot_is_rejected() -> None:
    with pytest.raises(ConfigError, match="дважды"):
        parse_instruments("BTC-USDT:BTC-USDT-SWAP,BTC-USDT:OTHER", with_futures=True)


def test_futures_requires_a_contract_for_every_pair() -> None:
    """С futures пара без контракта не проходит: иначе запрос уйдёт со спотом."""
    with pytest.raises(ConfigError, match="51000"):
        parse_instruments("BTC-USDT:BTC-USDT-SWAP,ETH-USDT", with_futures=True)


def test_bare_spot_is_allowed_without_futures() -> None:
    pairs = parse_instruments("BTC-USDT,ETH-USDT", with_futures=False)
    assert [p.spot for p in pairs] == ["BTC-USDT", "ETH-USDT"]
    assert all(p.swap is None for p in pairs)


# --- BT_AGENTS -------------------------------------------------------------


def test_agents_market_only() -> None:
    assert parse_agents("market") == ("market",)


def test_agents_market_and_futures_order_is_fixed() -> None:
    """Порядок не зависит от того, как список записали в конфигурации."""
    assert parse_agents("futures,market") == ("market", "futures")


def test_agents_without_market_is_rejected() -> None:
    with pytest.raises(ConfigError, match="market"):
        parse_agents("futures")


def test_liquidity_is_rejected_with_its_reason() -> None:
    with pytest.raises(ConfigError, match="стакана"):
        parse_agents("market,liquidity")


def test_unknown_agent_is_rejected() -> None:
    with pytest.raises(ConfigError, match="неизвестные"):
        parse_agents("market,oracle")


def test_repeated_agent_is_rejected() -> None:
    with pytest.raises(ConfigError, match="повторы"):
        parse_agents("market,market")


# --- Конфигурации прогона --------------------------------------------------


def test_market_only_runs_configuration_a_alone() -> None:
    cfg = make_config(agents=("market",))
    assert cfg.with_futures is False
    assert [name for name, _ in cfg.agent_sets()] == ["A"]
    assert cfg.agent_sets()[0][1] == ["market"]


def test_market_and_futures_run_both_configurations() -> None:
    cfg = make_config(agents=("market", "futures"))
    assert [name for name, _ in cfg.agent_sets()] == ["A", "B"]
    assert cfg.agent_sets()[1][1] == ["market", "futures"]


def test_config_json_records_both_markets() -> None:
    """Рынки попадают в config_json: по отчёту видно, что на чём считалось."""
    data = make_config().as_dict()
    assert data["instruments"] == [{"spot": SPOT, "swap": SWAP}]
    assert data["agents"] == ["market", "futures"]
    restored = BacktestConfig.from_dict(data)
    assert restored.instruments == (PAIR,)
    assert restored.agents == ("market", "futures")


# --- Файл конфигурации -----------------------------------------------------

_ENV_TEMPLATE = """
BT_INSTRUMENTS={instruments}
BT_AGENTS={agents}
BT_BAR=1H
BT_PERIOD_FROM=2022-02-01T00:00:00Z
BT_PERIOD_TO=2026-07-31T00:00:00Z
BT_STEP_HOURS=1
BT_HORIZONS=1,4,12,24
BT_FEE_ROUNDTRIP_PCT=0.10
BT_SLIPPAGE_PCT=0.01
BT_OOS_MONTHS=6
BT_REQUEST_PAUSE_MS=400
"""


def _write_env(tmp_path: Path, instruments: str, agents: str) -> Path:
    path = tmp_path / ".env.backtest"
    path.write_text(
        _ENV_TEMPLATE.format(instruments=instruments, agents=agents), encoding="utf-8"
    )
    return path


def test_config_file_with_pairs_and_market_only(tmp_path: Path) -> None:
    cfg = load_config(
        _write_env(tmp_path, "BTC-USDT:BTC-USDT-SWAP,ETH-USDT:ETH-USDT-SWAP", "market")
    )
    assert cfg.spot_ids == ("BTC-USDT", "ETH-USDT")
    assert cfg.swap_ids == ("BTC-USDT-SWAP", "ETH-USDT-SWAP")
    assert cfg.with_futures is False
    assert cfg.fee_roundtrip_pct == Decimal("0.10")


def test_config_file_without_agents_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / ".env.backtest"
    path.write_text(
        _ENV_TEMPLATE.format(instruments="BTC-USDT:BTC-USDT-SWAP", agents=""),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="BT_AGENTS"):
        load_config(path)


def test_directory_instead_of_config_file_is_named_as_such(tmp_path: Path) -> None:
    """Дефект D-9: docker создаёт каталог на месте отсутствующего файла."""
    directory = tmp_path / ".env.backtest"
    directory.mkdir()
    with pytest.raises(ConfigError, match="КАТАЛОГ"):
        load_config(directory)


# --- Загрузка: какой ряд с какого рынка ------------------------------------


class _RecordingLoader:
    """Запоминает, какие ряды и по каким инструментам запрашивались."""

    def __init__(self) -> None:
        self.candles: list[str] = []
        self.funding: list[str] = []

    async def backfill_candles(self, inst_id, bar, since, until, **kwargs):
        self.candles.append(inst_id)
        return 0

    async def backfill_funding(self, inst_id, since, until, **kwargs):
        self.funding.append(inst_id)
        return 0


class _DummyHttpClient:
    async def aclose(self) -> None:
        return None


@pytest.fixture
def recording_loader(monkeypatch):
    recorder = _RecordingLoader()
    monkeypatch.setattr(run_mod.loader, "create_http_client", lambda: _DummyHttpClient())
    monkeypatch.setattr(run_mod.loader, "OkxHistory", lambda client, pause_ms: object())
    monkeypatch.setattr(run_mod.loader, "backfill_candles", recorder.backfill_candles)
    monkeypatch.setattr(run_mod.loader, "backfill_funding", recorder.backfill_funding)
    return recorder


async def test_candles_are_loaded_from_spot_and_funding_from_contract(
    recording_loader,
) -> None:
    await run_mod._load_history(make_config(agents=("market", "futures")))
    assert recording_loader.candles == [SPOT]
    assert recording_loader.funding == [SWAP]


async def test_market_only_never_requests_funding(recording_loader) -> None:
    """Требование 2 поправки: при BT_AGENTS=market запроса funding нет вовсе."""
    await run_mod._load_history(make_config(agents=("market",)))
    assert recording_loader.candles == [SPOT]
    assert recording_loader.funding == []


def test_parity_pair_is_loaded_up_to_the_live_window() -> None:
    """Ряды пары сверки догружаются до текущего момента, а не до BT_PERIOD_TO.

    Вторая причина наблюдавшегося market 0/200: моменты сверки лежат в живом
    окне (16.08.2026+), позже конца периода прогона, и на свечах, обрывающихся
    на BT_PERIOD_TO, Market в реплее считал бы совсем другое — независимо от
    того, на правильном ли он рынке.
    """
    cfg = make_config()
    now = cfg.period_to + timedelta(days=21)
    assert run_mod.parity_load_until(cfg, now) == now
    # Если «сейчас» раньше конца периода, граница периода остаётся главной.
    assert run_mod.parity_load_until(cfg, cfg.period_to - timedelta(days=1)) == cfg.period_to


# --- Снимок и сверка на реальных рядах -------------------------------------


@requires_db
async def test_snapshot_takes_each_series_from_its_own_market(bt_db, pool) -> None:
    """Свечи приходят со спота, ставки — с контракта, и наоборот не работает."""
    from backtest.clock import build_snapshot

    await seed_candles(pool, inst_id=SPOT, hours=24 * 40)
    await seed_funding(pool, inst_id=SWAP, points=120)

    cfg = make_config(agents=("market", "futures"))
    ts = T0 + timedelta(hours=24 * 30)
    snapshot = await build_snapshot(PAIR, ts, cfg)

    assert not snapshot.candles.empty, "свечи спота не прочитаны"
    assert not snapshot.funding.empty, "ставки контракта не прочитаны"
    assert snapshot.spot_id == SPOT
    assert snapshot.swap_id == SWAP
    # Цена — со спота; цена контракта отсутствует, как и в продакшне.
    assert snapshot.price is not None
    assert snapshot.swap_price is None

    # Перепутанная пара обязана дать пустые ряды, а не «почти те же» числа.
    swapped = InstrumentPair(spot=SWAP, swap=SPOT)
    wrong = await build_snapshot(swapped, ts, cfg)
    assert wrong.candles.empty
    assert wrong.funding.empty


@requires_db
async def test_market_only_snapshot_does_not_read_funding(bt_db, pool) -> None:
    from backtest.clock import build_snapshot

    await seed_candles(pool, inst_id=SPOT, hours=24 * 40)
    await seed_funding(pool, inst_id=SWAP, points=120)

    cfg = make_config(agents=("market",))
    snapshot = await build_snapshot(PAIR, T0 + timedelta(hours=24 * 30), cfg)
    assert not snapshot.candles.empty
    assert snapshot.funding.empty, "ряд funding прочитан, хотя Futures не участвует"


@requires_db
async def test_parity_compares_each_agent_on_its_own_market(bt_db, pool, monkeypatch) -> None:
    """Требование 3 поправки: при верной паре market совпадает, а не даёт ноль.

    Живые моменты подставляются: мнения считаются продакшн-функциями по рядам
    СВОИХ рынков — Market по свечам спота, Futures по ставкам контракта. При
    верной конфигурации сверка обязана дать полное совпадение; при перепутанных
    рынках — не дать его.
    """
    from backtest import parity
    from backtest.clock import build_snapshot

    await seed_candles(pool, inst_id=SPOT, hours=24 * 40)
    await seed_funding(pool, inst_id=SWAP, points=120)
    cfg = make_config(agents=("market", "futures"))

    moments = []
    for hour in range(24 * 29, 24 * 29 + 20):
        ts = T0 + timedelta(hours=hour)
        snapshot = await build_snapshot(PAIR, ts, cfg)
        values = parity._replay_agent_values(snapshot, ("market", "futures"))
        moments.append(
            {
                "ts": ts,
                "payload": [
                    {"agent": name, "signal": signal, "confidence": confidence}
                    for name, (signal, confidence) in values.items()
                ]
                # liquidity в живом окне присутствует и обязан быть исключён.
                + [{"agent": "liquidity", "signal": "bullish", "confidence": 0.9}],
            }
        )

    async def fake_moments(limit: int = 200):
        return moments[:limit]

    monkeypatch.setattr(parity, "production_moments", fake_moments)

    result = await parity.check_parity(PAIR, cfg)
    assert result.moments == 20
    assert result.agents["market"].compared == 20
    assert result.agents["market"].direction_match == 20
    assert result.agents["market"].confidence_match == 20
    assert result.blocking_ok is True
    assert result.markets == {"market": SPOT, "futures": SWAP}
    assert "liquidity" not in result.agents

    # Перепутанные рынки: сравнивать становится нечего (свечей у контракта нет),
    # и это НЕ считается пройденной сверкой.
    swapped = await parity.check_parity(InstrumentPair(spot=SWAP, swap=SPOT), cfg)
    assert swapped.agents["market"].compared == 0
    assert swapped.blocking_ok is False


@requires_db
async def test_parity_skips_futures_when_it_is_not_in_agents(bt_db, pool, monkeypatch) -> None:
    """При BT_AGENTS=market Futures не сверяется: ряда funding в прогоне нет."""
    from backtest import parity
    from backtest.clock import build_snapshot

    await seed_candles(pool, inst_id=SPOT, hours=24 * 40)
    cfg = make_config(agents=("market",))

    moments = []
    for hour in range(24 * 29, 24 * 29 + 5):
        ts = T0 + timedelta(hours=hour)
        snapshot = await build_snapshot(PAIR, ts, cfg)
        values = parity._replay_agent_values(snapshot, ("market",))
        moments.append(
            {
                "ts": ts,
                "payload": [
                    {"agent": "market", "signal": values["market"][0],
                     "confidence": values["market"][1]},
                    {"agent": "futures", "signal": "bullish", "confidence": 0.7},
                ],
            }
        )

    async def fake_moments(limit: int = 200):
        return moments[:limit]

    monkeypatch.setattr(parity, "production_moments", fake_moments)
    result = await parity.check_parity(PAIR, cfg)

    assert set(result.agents) == {"market"}
    assert result.agents["market"].compared == 5
    assert result.blocking_ok is True


# --- Граница периода при загруженных «свежих» свечах ------------------------
#
# Загрузчик догружает ряд пары сверки ДО ТЕКУЩЕГО МОМЕНТА (иначе сверку §13.2
# не с чем выполнять: её моменты лежат в живом окне, позже BT_PERIOD_TO).
# Значит, в таблице заведомо есть свечи позже границы периода. Проверки ниже
# требуют, чтобы за границу не «протекли» ни решения, ни исходы: иначе защита
# §5.3 ТЗ (конец периода отодвинут назад намеренно) исчезает молча.


def test_decision_times_never_leave_the_period() -> None:
    """Моменты решения ограничены [BT_PERIOD_FROM, BT_PERIOD_TO] по построению."""
    from backtest.clock import decision_times

    cfg = make_config()
    moments = decision_times(cfg)
    assert moments, "моментов решения не оказалось вовсе"
    assert min(moments) >= cfg.period_from
    assert max(moments) <= cfg.period_to


@requires_db
async def test_replay_and_outcomes_stop_at_period_to(bt_db, pool) -> None:
    """Свечи ЗА границей загружены, но ни решений, ни исходов за ней не появилось."""
    from backtest import evaluate, replay

    # Конфигурация с двумя агентами: при продакшновом MIN_AGENTS=2 один Market
    # не набирает кворума и все решения были бы wait, а тогда исходов не будет
    # вовсе и проверка стала бы холостой.
    cfg = make_config(agents=("market", "futures"))
    # Ряд заведомо длиннее периода прогона: как после догрузки для сверки.
    await seed_candles(pool, inst_id=SPOT, hours=24 * 45)
    await seed_funding(pool, inst_id=SWAP, points=140)
    beyond = await pool.fetchval(
        "SELECT count(*) FROM backtest.candles WHERE inst_id=$1 AND close_time > $2;",
        SPOT, cfg.period_to,
    )
    assert beyond > max(cfg.horizons), "тест бессмыслен: свечей за границей нет"

    run_id = await replay.start_run(
        cfg, ["market", "futures"], {"описание": "тест границы"}
    )
    await replay.replay_instrument(run_id, PAIR, cfg, ["market", "futures"])
    written = await evaluate.evaluate_run(run_id, cfg)
    assert written > 0, "исходов не оказалось вовсе — проверка была бы холостой"

    last_decision = await pool.fetchval(
        "SELECT max(ts) FROM backtest.decisions WHERE run_id=$1;", run_id
    )
    assert last_decision is None or last_decision <= cfg.period_to

    # Ни один исход не смотрит за границу: ts + горизонт <= BT_PERIOD_TO.
    leaking = await pool.fetchval(
        "SELECT count(*) FROM backtest.outcomes "
        "WHERE run_id=$1 AND ts + make_interval(hours => horizon_h) > $2;",
        run_id, cfg.period_to,
    )
    assert leaking == 0, "исход посчитан по свечам позже BT_PERIOD_TO"


@requires_db
async def test_outcome_count_does_not_depend_on_how_much_is_loaded(bt_db, pool) -> None:
    """Догрузка свежих свечей НЕ добавляет исходов: прогон воспроизводим.

    Если бы наблюдения у конца периода досчитывались свежими данными, число
    исходов зависело бы от дня запуска, а результаты двух прогонов одной и той
    же конфигурации перестали бы сравниваться между собой.
    """
    from backtest import evaluate, replay

    cfg = make_config(agents=("market", "futures"))
    criterion = {"описание": "тест воспроизводимости"}

    # 1. Ряд обрывается ровно на границе периода.
    hours_to_period_to = int((cfg.period_to - T0).total_seconds() // 3600) + 1
    await seed_candles(pool, inst_id=SPOT, hours=hours_to_period_to)
    await seed_funding(pool, inst_id=SWAP, points=140)
    run_short = await replay.start_run(cfg, ["market", "futures"], criterion)
    await replay.replay_instrument(run_short, PAIR, cfg, ["market", "futures"])
    short_rows = await evaluate.evaluate_run(run_short, cfg)

    # 2. Тот же прогон, но ряд догружен на десять суток вперёд.
    await seed_candles(pool, inst_id=SPOT, hours=hours_to_period_to + 24 * 10)
    run_long = await replay.start_run(cfg, ["market", "futures"], criterion)
    await replay.replay_instrument(run_long, PAIR, cfg, ["market", "futures"])
    long_rows = await evaluate.evaluate_run(run_long, cfg)

    assert short_rows == long_rows, (
        "число исходов изменилось от догрузки свежих свечей — "
        "граница периода протекает"
    )
