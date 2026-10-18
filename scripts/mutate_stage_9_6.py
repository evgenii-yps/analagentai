"""Мутационная проверка этапа 9.6 (ТЗ 9.6, §10): каждая мутация обязана уронить тест.

Запуск: ``python scripts/mutate_stage_9_6.py`` (нужна PostgreSQL для тестов, см.
``tests/demo_support.py``). Мутации 1–5 — обязательные из ТЗ, 6–7 — дополнительные. Исходник
после каждой мутации возвращается; код выхода 1, если хоть одна мутация НЕ поймана.
"""
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
MUTATIONS = [
    ("1. убрать вычитание резерва", "src/demo/rules.py",
     "reserve_room = free_cash - equity * reserve_pct / 100",
     "reserve_room = free_cash"),
    ("2. потолок по текущей стоимости вместо cost_usd", "src/demo/ledger.py",
     "COALESCE(b.cost_usd, b.requested_cost_usd)",
     "b.filled_qty * COALESCE((SELECT o.close FROM ohlcv o WHERE o.instrument_id = "
     "b.instrument_id AND o.timeframe = '1m' ORDER BY o.ts DESC LIMIT 1), b.avg_price)"),
    ("3. округлять вверх", "src/demo/rules.py",
     "cost = min(step, token_room, reserve_room).quantize(_CENT, rounding=ROUND_FLOOR)",
     "cost = min(step, token_room, reserve_room).quantize(_CENT, rounding='ROUND_CEILING')"),
    ("4. не обновлять exposure после покупки", "src/demo/runner.py",
     "                mm.exposure[instrument_id] = "
     "mm.exposure.get(instrument_id, Decimal(0)) + cost\n",
     ""),
    ("5. поменять местами причины в п.7 §4", "src/demo/rules.py",
     "return Decimal(0), SKIP_RESERVE if reserve_room <= token_room else SKIP_TOKEN_CAP",
     "return Decimal(0), SKIP_TOKEN_CAP if reserve_room <= token_room else SKIP_RESERVE"),
    ("6. (доп.) не уменьшать свободные деньги после покупки", "src/demo/runner.py",
     "                mm.free_cash -= cost\n", ""),
    ("7. (доп.) не вычитать деньги под ордерами с неизвестным исходом", "src/demo/runner.py",
     "free_cash=state.cash - await ledger.fetch_reserved(ctx.pool),",
     "free_cash=state.cash,"),
]
bad = 0
for name, rel, old, new in MUTATIONS:
    path = ROOT / rel
    original = path.read_text(encoding="utf-8")
    assert original.count(old) == 1, (name, original.count(old))
    path.write_text(original.replace(old, new), encoding="utf-8")
    try:
        res = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-x",
             "tests/test_stage_9_6.py"], cwd=ROOT, capture_output=True, text=True)
    finally:
        path.write_text(original, encoding="utf-8")
    failed = [ln for ln in res.stdout.splitlines() if ln.startswith("FAILED")]
    caught = res.returncode != 0
    bad += not caught
    print(("ЛОВИТСЯ " if caught else "НЕ ЛОВИТСЯ"), name, "->", failed[:1])
print("не пойманных:", bad)
sys.exit(1 if bad else 0)
