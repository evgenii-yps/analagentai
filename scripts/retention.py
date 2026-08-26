#!/usr/bin/env python3
"""Политика хранения данных (§9 ТЗ 6.5, расширено §4 ТЗ 8.1).

Пять токенов увеличивают приток данных впятеро, поэтому сроки заданы явно:

* ``orderbook_snapshots`` — старше ``RETENTION_ORDERBOOK_DAYS`` (по умолчанию 14);
* ``trades``              — старше ``RETENTION_TRADES_DAYS`` (по умолчанию 2);
* ``ohlcv`` с ``timeframe = '1m'`` — старше ``RETENTION_1M_DAYS`` (по умолчанию 30);
* ``agent_outputs`` — старше ``RETENTION_AGENT_OUTPUTS_DAYS`` (по умолчанию 90);
  перед удалением журнал сворачивается в суточные итоги ``agent_outputs_daily``,
  которые не удаляются никогда.

СНАЧАЛА СВЁРТКА, ПОТОМ УДАЛЕНИЕ. Сырая лента сделок живёт трое суток, но её
содержательная часть остаётся навсегда: перед удалением задача сворачивает
завершённые минуты в ``trade_flow_1m`` (число сделок, объёмы и число сделок по
сторонам, VWAP). Порядок обязателен и обеспечен кодом: если свёртка не удалась,
правило удаления ``trades`` ПРОПУСКАЕТСЯ — потерять сырьё, не сохранив итог,
нельзя, а отложить удаление на сутки можно без последствий.

НИКОГДА НЕ УДАЛЯЮТСЯ (§4 ТЗ 8.1): часовые и любые НЕ минутные свечи, funding,
открытый интерес, сигналы, оценки и поминутные итоги сделок. На них держится
весь анализ, и восстановить их неоткуда: биржа отдаёт историю свечей, но не
историю наших решений.

Защита от опечатки сделана кодом, а не внимательностью: список защищённых
таблиц проверяется перед каждым удалением (:func:`_check_protected`), а правило
для ``ohlcv`` обязано нести условие по таймфрейму — без него удаление снесло бы
часовые свечи.

Удаление идёт батчами по 50 000 строк с паузой между батчами, чтобы не
блокировать запись коллектора. После удаления по затронутым таблицам
выполняется ``VACUUM ANALYZE`` (без ``FULL``). Число удалённых строк логируется.

Работает через ``docker compose exec -T postgres psql`` (только стандартная
библиотека Python). Запускается из cron под пользователем ``agent`` ежедневно
в 03:40 UTC.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import UTC, datetime

APP_DIR = os.environ.get("APP_DIR", "/opt/agent-trade")

# Таблицы, которые НЕ УДАЛЯЮТСЯ НИКОГДА (§4 ТЗ 8.1). Список проверяется перед
# каждым удалением: опечатка в правиле обязана остановить задачу, а не стереть
# то, что восстановить неоткуда.
# Перечень взят из §4 ТЗ 8.1 дословно: часовые (и любые не 1m) свечи, funding,
# открытый интерес, сигналы и оценки. К ним добавлены справочники и итоги,
# которые восстановить неоткуда.
#
# ``agent_outputs`` в перечень НЕ входит и защищённым не является: это журнал
# выводов агентов, самая объёмная из «вечных» таблиц (6.6 ГБ в год на пять
# токенов). Мнения, УЧАСТВОВАВШИЕ В РЕШЕНИИ, хранятся навсегда в
# ``signals.agents_payload``; в agent_outputs остаются детальные метрики
# (RSI, EMA, перцентили), нужные для разбора, а не для анализа результатов.
# Поэтому у него свой срок хранения — см. RETENTION_RULES.
PROTECTED_TABLES: frozenset[str] = frozenset(
    {
        "funding",
        "open_interest",
        "signals",
        "signal_evaluations",
        "trade_flow_1m",
        "agent_outputs_daily",
        "instruments",
        "calibration_curves",
        "signal_exports",
        "logic_version_windows",
        # Этап 8.2 §3: цели по вероятности и их заморозка. Объём мал (5
        # инструментов x 4 горизонта x 2 направления = 40 строк в сутки), а
        # ценность вся в истории: удалив signal_targets, нельзя ответить на
        # вопрос «какую цель система назвала в момент сигнала и сбылась ли она».
        "risk_targets",
        "signal_targets",
        # Этап 8.8 §6: исход по границам. Объём того же порядка (сигнал x
        # горизонт), а ценность вся в истории: пересчитать строку задним числом
        # можно только пока живы минутные свечи, а они удаляются через
        # RETENTION_1M_DAYS суток. Удалив таблицу, вторую оценку исхода в
        # прежнем разрешении уже не восстановить.
        "signal_outcomes_barrier",
        # Этап 8.9 §6: исходы базовых стратегий. Линейка бесполезна, если её
        # можно потерять: без неё числа системы снова не с чем сравнивать, а
        # пересчитать её задним числом можно только пока живы минутные свечи.
        "strategy_outcomes",
    }
)


class RetentionRuleError(RuntimeError):
    """Правило хранения нарушает защиту таблиц — задача останавливается."""


def _rule(table: str, days_env: str, default: str, extra_where: str = "") -> tuple:
    """Правило: (таблица, срок в днях, дополнительное условие)."""
    return (table, int(_env_value(days_env, default)), extra_where)


def _check_protected(table: str, extra_where: str) -> None:
    """Останавливает задачу, если правило метит в защищённую таблицу.

    Отдельный случай — ``ohlcv``: сама таблица не защищена целиком (минутные
    свечи удалять можно и нужно), но правило по ней ОБЯЗАНО ограничиваться
    таймфреймом ``1m``. Без этого условия удаление снесло бы часовые свечи, на
    которых работает Market Agent и держится весь исторический анализ.
    """
    if table in PROTECTED_TABLES:
        raise RetentionRuleError(
            f"таблица {table} защищена от удаления (§4 ТЗ 8.1) — правило отклонено"
        )
    if table == "ohlcv" and "timeframe" not in extra_where:
        raise RetentionRuleError(
            "правило для ohlcv без условия по таймфрейму удалило бы часовые "
            "свечи — они не удаляются никогда (§4 ТЗ 8.1)"
        )

BATCH = int(os.environ.get("RETENTION_BATCH", "50000"))
PAUSE_SEC = float(os.environ.get("RETENTION_PAUSE_SEC", "0.5"))

# Отставание свёртки от текущего момента, минут. Минута считается завершённой,
# когда она закончилась И прошёл этот запас: коллектор опрашивает биржу раз в
# 15 секунд, поэтому хвост только что закончившейся минуты может быть ещё не
# записан. Свернуть её раньше времени значит зафиксировать неполный итог —
# а переписать его потом нельзя (см. ниже про идемпотентность).
ROLLUP_LAG_MINUTES = int(os.environ.get("ROLLUP_LAG_MINUTES", "5"))

# Признак «версия логики неизвестна» в agent_outputs_daily. Ставится выводам,
# сделанным РАНЬШЕ самой ранней записанной границы версий: для них верного
# значения нет ни в одной таблице, и взять его неоткуда.
#
# Почему именно ноль, а не отдельная колонка-признак. logic_version уже входит
# в первичный ключ, поэтому сутки «неизвестно → версия 4» разделяются на две
# строки тем же механизмом, что и любая другая граница версий, — без правок
# схемы. И главное: любой существующий отбор вида «WHERE logic_version = 4»
# строки с неизвестной версией НЕ ЗАХВАТИТ. Отдельная колонка-признак вела бы
# себя наоборот: тот же отбор молча включал бы чужие данные у каждого, кто про
# новую колонку не знает, — а смешивание версий в анализе запрещено.
#
# Отличимость от реальной версии обеспечена кодом, а не соглашением: в
# logic_version_windows добавлено ограничение logic_version > 0 (миграция 012),
# поэтому реальной версии 0 не существует и появиться не может.
UNKNOWN_LOGIC_VERSION = 0


def rollup_sql(lag_minutes: int = ROLLUP_LAG_MINUTES) -> str:
    """SQL свёртки ленты сделок в поминутные итоги.

    Свойства, которые обязаны выполняться (§5 решения по §4.3):

    * сворачиваются ТОЛЬКО завершённые минуты: граница —
      ``date_trunc('minute', now()) - запас``; текущая минута не попадает
      никогда;
    * идемпотентность: ``ON CONFLICT DO NOTHING`` — повторный запуск на том же
      интервале не создаёт дублей и НЕ МЕНЯЕТ уже записанные значения. Именно
      «не меняет», а не «пересчитывает»: пересчёт по частично удалённому сырью
      испортил бы верный итог;
    * суммы точные: объём и цена суммируются в ``numeric``, а не в
      ``double precision``.

    Сделки без указанной стороны (``side IS NULL``) считаются в ``trades_n`` и
    участвуют в VWAP, но не попадают ни в ``buy_volume``, ни в ``sell_volume``:
    приписать их произвольной стороне значило бы выдумать данные. На OKX сторона
    приходит всегда, поэтому случай редкий — но он определён, а не оставлен на
    усмотрение.
    """
    return f"""
        INSERT INTO trade_flow_1m (instrument_id, ts, trades_n, buy_volume,
                                   sell_volume, buy_n, sell_n, vwap)
        SELECT t.instrument_id,
               date_trunc('minute', t.ts) AS minute,
               count(*),
               COALESCE(sum(t.amount::numeric) FILTER (WHERE t.side = 'buy'), 0),
               COALESCE(sum(t.amount::numeric) FILTER (WHERE t.side = 'sell'), 0),
               count(*) FILTER (WHERE t.side = 'buy'),
               count(*) FILTER (WHERE t.side = 'sell'),
               CASE WHEN sum(t.amount::numeric) > 0
                    THEN sum(t.price::numeric * t.amount::numeric)
                         / sum(t.amount::numeric)
                    ELSE 0 END
          FROM trades t
         WHERE t.ts < date_trunc('minute', now())
                      - interval '{int(lag_minutes)} minutes'
         GROUP BY t.instrument_id, date_trunc('minute', t.ts)
        ON CONFLICT (instrument_id, ts) DO NOTHING;
    """


def _log(msg: str) -> None:
    print(f"[{datetime.now(UTC):%F %T}] {msg}", flush=True)


def _env_value(key: str, default: str) -> str:
    """Читает значение из окружения или из .env (KEY=value)."""
    if key in os.environ:
        return os.environ[key]
    path = os.path.join(APP_DIR, ".env")
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1].strip()
    return default


PG_USER = _env_value("POSTGRES_USER", "agenttrade")
PG_DB = _env_value("POSTGRES_DB", "agenttrade")

# Правила очистки: (таблица, срок хранения в днях, дополнительное условие).
# Значения читаются из окружения или из .env — сроки задаёт конфигурация, а не
# код: §4 ТЗ 8.1 требует RETENTION_1M_DAYS=30 и RETENTION_ORDERBOOK_DAYS=14.
def rollup_daily_sql() -> str:
    """SQL суточной свёртки выводов агентов в ``agent_outputs_daily``.

    Свойства те же, что у свёртки сделок, и по тем же причинам:

    * сворачиваются ТОЛЬКО завершённые сутки (``ts < date_trunc('day', now())``);
    * идемпотентность через ``ON CONFLICT DO NOTHING``: повторный запуск не
      создаёт дублей и не меняет уже записанные значения;
    * выполняется ДО удаления сырья, и при неудаче удаление пропускается.

    ОТКУДА БЕРЁТСЯ logic_version. В самой таблице ``agent_outputs`` его нет, и
    добавлять его туда задним числом нельзя: у старых строк верного значения
    взять неоткуда. Версия восстанавливается по окнам ``logic_version_windows``
    (§6 ТЗ 8.1): для вывода в момент ``ts`` берётся последнее окно, начавшееся
    не позже ``ts``.

    ВЫВОДЫ РАНЬШЕ САМОГО РАННЕГО ИЗВЕСТНОГО ОКНА ПОЛУЧАЮТ
    ``logic_version = 0`` — признак «версия неизвестна»
    (:data:`UNKNOWN_LOGIC_VERSION`). Раньше им проставлялась минимальная
    известная версия, и это было ошибкой: 33 895 выводов версий 1-3 на сервере
    оказались записаны как версия 4. ``agent_outputs_daily`` — вечная таблица,
    сырьё удаляется через 90 суток, проверить утверждение стало бы нечем, и в
    проекте навсегда осталась бы правдоподобная ложь. Подстановка ближайшей
    версии — это подстановка суррогатных данных вместо честного «неизвестно».
    Ноль отличим от любой реальной версии по построению: ``logic_version_windows``
    несёт ограничение ``logic_version > 0`` (миграция 012), поэтому реальная
    версия нулём быть не может, а фильтр ``WHERE logic_version = 4`` строки с
    неизвестной версией не захватит — молчаливое смешивание исключено.

    СИММЕТРИЧНЫЙ СЛУЧАЙ РЕШАЕТСЯ ИНАЧЕ И ЭТО ВЕРНО. Вывод ПОЗЖЕ последней
    известной границы получает последнюю версию: у последнего окна нет конца
    (``ended_at IS NULL``), оно действует до следующей границы. Здесь версия
    известна — это та, что работает сейчас. Незнание было только «слева», до
    первой записанной границы.

    ОТКУДА НАЧИНАТЬ СЧЁТ. Обычный ход — со следующих суток после последних
    свёрнутых. Но если в итогах есть ДЫРА (сутки, по которым сырьё есть, а
    строки итогов нет), счёт начинается с неё: иначе дыра не закрылась бы
    никогда. Это же свойство закрывает исправление уже записанного (миграция
    012): та удаляет неверные строки, а пересчитывает их ЭТОТ ЖЕ SQL — второй
    реализации расчёта в проекте нет и быть не должно.

    Сутки, на которые пришлась граница версий, дают ДВЕ строки — по одной на
    версию. Это прямо требуется: смешивать версии в анализе запрещено. Сутки
    на границе «неизвестно → версия 4» дают строки 0 и 4 по тому же правилу.

    ``repeat_rate`` — доля ПОЛНЫХ повторов: вывод считается повтором, когда с
    предыдущим выводом того же агента по тому же инструменту совпали И
    направление, И уверенность (до четвёртого знака). Так эта величина
    считалась в Расчёте 4 диагностики 7.1. Сравнение идёт по всему ряду, а не
    внутри суток: первая запись суток сравнивается с последней записью
    предыдущих — иначе каждый день терял бы одно сравнение. Если сравнивать
    не с чем вовсе (самый первый вывод), доля равна нулю: колонка не допускает
    пустого значения, а «повторов не было» — верное утверждение для одной
    записи.

    ``n_total`` считает ВСЕ выводы суток, включая ``insufficient_data``,
    поэтому сумма трёх направлений может быть меньше ``n_total``. Это не
    расхождение: «агенту не хватило данных» — не направление.
    """
    return f"""
        WITH ver AS (
            SELECT logic_version, started_at,
                   lead(started_at) OVER (ORDER BY started_at) AS ended_at
              FROM logic_version_windows
        ), bounds AS (
            SELECT (SELECT min(ts)::date FROM agent_outputs)       AS first_raw_day,
                   (SELECT max(day)      FROM agent_outputs_daily) AS last_daily_day
        ), gap AS (
            -- Самые ранние сутки, по которым сырьё ЕСТЬ, а итогов НЕТ.
            -- Проверка по сырью обязательна: сутки, когда система не работала,
            -- итогов не имеют законно, и без неё счёт упирался бы в них вечно.
            -- Порядок условий важен для стоимости: дыр обычно нет, и тогда
            -- обращения к сырью не происходит вовсе.
            SELECT min(g.d)::date AS day
              FROM bounds b,
                   generate_series(b.first_raw_day::timestamptz,
                                   b.last_daily_day::timestamptz,
                                   interval '1 day') g(d)
             WHERE NOT EXISTS (
                       SELECT 1 FROM agent_outputs_daily x WHERE x.day = g.d::date
                   )
               AND EXISTS (
                       SELECT 1 FROM agent_outputs a
                        WHERE a.ts >= g.d AND a.ts < g.d + interval '1 day'
                   )
        ), target AS (
            SELECT coalesce(
                       (SELECT day FROM gap),
                       (SELECT max(day) + 1 FROM agent_outputs_daily),
                       (SELECT min(ts)::date FROM agent_outputs),
                       CURRENT_DATE
                   ) AS from_day
        ), src AS (
            SELECT a.agent,
                   a.instrument_id,
                   a.ts::date AS day,
                   a.signal,
                   round(a.confidence::numeric, 4) AS conf,
                   -- Нет подходящего окна — версия НЕИЗВЕСТНА. Ближайшая
                   -- известная версия сюда не подставляется: это была бы
                   -- ложная запись в вечной таблице.
                   coalesce(
                       (SELECT v.logic_version FROM ver v
                         WHERE v.started_at <= a.ts
                           AND (v.ended_at IS NULL OR a.ts < v.ended_at)
                         ORDER BY v.started_at DESC LIMIT 1),
                       {UNKNOWN_LOGIC_VERSION}
                   ) AS logic_version,
                   lag(a.signal) OVER w AS prev_signal,
                   lag(round(a.confidence::numeric, 4)) OVER w AS prev_conf
              FROM agent_outputs a
             WHERE a.ts >= (SELECT from_day FROM target)::timestamptz
                            - interval '1 day'
               AND a.ts < date_trunc('day', now())
            WINDOW w AS (PARTITION BY a.agent, a.instrument_id ORDER BY a.ts)
        )
        INSERT INTO agent_outputs_daily
            (day, agent, instrument_id, logic_version, n_total, n_bullish,
             n_bearish, n_neutral, conf_avg, conf_p50, conf_p90, repeat_rate)
        SELECT day, agent, instrument_id, logic_version,
               count(*),
               count(*) FILTER (WHERE signal = 'bullish'),
               count(*) FILTER (WHERE signal = 'bearish'),
               count(*) FILTER (WHERE signal = 'neutral'),
               round(avg(conf), 6),
               -- percentile_cont определён для double precision, поэтому
               -- результат приводится к numeric явно: round(double, int)
               -- в PostgreSQL не существует.
               round((percentile_cont(0.5) WITHIN GROUP (ORDER BY conf))::numeric, 6),
               round((percentile_cont(0.9) WITHIN GROUP (ORDER BY conf))::numeric, 6),
               -- coalesce обязателен: если сравнивать не с чем (единственный
               -- вывод за сутки), доля повторов равна нулю — колонка не
               -- допускает пустого значения, а «повторов не было» верно.
               coalesce(round(
                   count(*) FILTER (
                       WHERE prev_signal IS NOT NULL
                         AND prev_signal = signal AND prev_conf = conf
                   )::numeric
                   / NULLIF(count(*) FILTER (WHERE prev_signal IS NOT NULL), 0),
                   4
               ), 0)
          FROM src
         WHERE day >= (SELECT from_day FROM target)
         GROUP BY day, agent, instrument_id, logic_version
        ON CONFLICT (day, agent, instrument_id, logic_version) DO NOTHING;
    """


RETENTION_RULES: list[tuple[str, int, str]] = [
    _rule("orderbook_snapshots", "RETENTION_ORDERBOOK_DAYS", "14"),
    # Двое суток: сырья хватает на разбор инцидентов, а содержательная часть
    # уже сохранена свёрткой в trade_flow_1m. Срок выбран по бюджету диска —
    # см. §4.5 отчёта 8.1.
    _rule("trades", "RETENTION_TRADES_DAYS", "2"),
    _rule("ohlcv", "RETENTION_1M_DAYS", "30", "AND timeframe = '1m'"),
    # Журнал выводов агентов: 90 суток. Без срока он даёт 6.6 ГБ в год на пять
    # токенов и один делает недостижимым порог §3 (свободного места не меньше
    # 40%) на горизонте года. Значение 0 отключает правило.
    _rule("agent_outputs", "RETENTION_AGENT_OUTPUTS_DAYS", "90"),
]
RETENTION_RULES = [rule for rule in RETENTION_RULES if rule[1] > 0]


def _psql(sql: str) -> str:
    """Выполняет SQL в контейнере postgres, возвращает stdout (обрезанный)."""
    cmd = [
        "docker", "compose", "exec", "-T", "postgres",
        "psql", "-U", PG_USER, "-d", PG_DB, "-t", "-A", "-c", sql,
    ]
    result = subprocess.run(
        cmd, cwd=APP_DIR, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _delete_in_batches(table: str, days: int, extra_where: str = "") -> int:
    """Удаляет из ``table`` записи старше ``days`` дней батчами. Возвращает всего.

    ``extra_where`` — дополнительное условие (``AND timeframe = '1m'``), без
    которого правило по ``ohlcv`` снесло бы часовые свечи.
    """
    _check_protected(table, extra_where)
    total = 0
    # ctid-подзапрос с LIMIT — быстрый батч без блокировки всей таблицы.
    sql = (
        f"DELETE FROM {table} WHERE ctid IN ("
        f"  SELECT ctid FROM {table} "
        f"  WHERE ts < now() - interval '{days} days' {extra_where} "
        f"  LIMIT {BATCH}"
        f");"
    )
    while True:
        out = _psql(sql)  # psql печатает 'DELETE <N>'
        deleted = 0
        if out.startswith("DELETE"):
            try:
                deleted = int(out.split()[-1])
            except (ValueError, IndexError):
                deleted = 0
        total += deleted
        if deleted < BATCH:
            break
        time.sleep(PAUSE_SEC)  # пауза, чтобы не мешать записи коллектора
    return total


def main() -> int:
    """Прогоняет правила хранения. Возвращает 0 при успехе, 1 при ошибке."""
    _log("=== Политика хранения данных (Этап 8.1 §4) ===")
    _log(
        "Никогда не удаляются: часовые (и любые не 1m) свечи, "
        + ", ".join(sorted(PROTECTED_TABLES))
    )
    had_error = False

    # 1. СВЁРТКА ленты сделок — ДО удаления сырья.
    rollup_ok = False
    try:
        before = _psql("SELECT count(*) FROM trade_flow_1m;")
        _psql(rollup_sql())
        after = _psql("SELECT count(*) FROM trade_flow_1m;")
        added = int(after or 0) - int(before or 0)
        unknown_side = _psql(
            "SELECT count(*) FROM trades WHERE side IS NULL "
            "AND ts < date_trunc('minute', now());"
        )
        _log(
            f"trade_flow_1m: свёрнуто минут (новых строк): {added}; "
            f"всего строк итогов: {after}. Запас свёртки: "
            f"{ROLLUP_LAG_MINUTES} мин."
        )
        if int(unknown_side or 0) > 0:
            _log(
                f"ВНИМАНИЕ: сделок без указанной стороны: {unknown_side}. "
                "Они учтены в trades_n и VWAP, но не в объёмах по сторонам."
            )
        rollup_ok = True
    except subprocess.CalledProcessError as exc:
        had_error = True
        _log(f"ОШИБКА свёртки trade_flow_1m: {(exc.stderr or '').strip() or exc}")
    except Exception as exc:  # noqa: BLE001
        had_error = True
        _log(f"ОШИБКА свёртки trade_flow_1m: {exc}")

    # 2. СВЁРТКА журнала выводов агентов — тоже ДО удаления сырья.
    daily_ok = False
    try:
        before_daily = _psql("SELECT count(*) FROM agent_outputs_daily;")
        _psql(rollup_daily_sql())
        after_daily = _psql("SELECT count(*) FROM agent_outputs_daily;")
        unknown_version = _psql(
            "SELECT count(*) FROM agent_outputs a "
            "WHERE a.ts < (SELECT min(started_at) FROM logic_version_windows);"
        )
        unknown_rows = _psql(
            f"SELECT count(*) FROM agent_outputs_daily "
            f"WHERE logic_version = {UNKNOWN_LOGIC_VERSION};"
        )
        _log(
            f"agent_outputs_daily: свёрнуто суток (новых строк): "
            f"{int(after_daily or 0) - int(before_daily or 0)}; "
            f"всего строк итогов: {after_daily}."
        )
        if int(unknown_version or 0) > 0 or int(unknown_rows or 0) > 0:
            _log(
                f"Выводов раньше самой ранней записанной границы версий: "
                f"{unknown_version}; строк итогов с признаком «версия "
                f"неизвестна» (logic_version = {UNKNOWN_LOGIC_VERSION}): "
                f"{unknown_rows}. Ближайшая известная версия им НЕ "
                f"подставляется: неизвестное остаётся неизвестным."
            )
        daily_ok = True
    except subprocess.CalledProcessError as exc:
        had_error = True
        _log(f"ОШИБКА свёртки agent_outputs_daily: {(exc.stderr or '').strip() or exc}")
    except Exception as exc:  # noqa: BLE001
        had_error = True
        _log(f"ОШИБКА свёртки agent_outputs_daily: {exc}")

    # 3. Удаление по правилам. Сырьё удаляется ТОЛЬКО после успешной свёртки:
    # потерять данные, не сохранив итог, нельзя, а отложить удаление на сутки —
    # можно.
    for table, days, extra_where in RETENTION_RULES:
        if table == "agent_outputs" and not daily_ok:
            _log(
                "agent_outputs: удаление ПРОПУЩЕНО — суточная свёртка не "
                "выполнена. Журнал сохранён до следующего запуска."
            )
            continue
        if table == "trades" and not rollup_ok:
            _log(
                "trades: удаление ПРОПУЩЕНО — свёртка в trade_flow_1m не "
                "выполнена. Сырьё сохранено до следующего запуска."
            )
            continue
        try:
            what = f"{table}{' ' + extra_where.strip() if extra_where else ''}"
            deleted = _delete_in_batches(table, days, extra_where)
            _log(f"{what}: удалено строк старше {days} дн.: {deleted}.")
            if deleted > 0:
                _log(f"{table}: выполняю VACUUM (ANALYZE, PARALLEL 0)…")
                # PARALLEL 0 — не украшение, а условие работоспособности.
                # Параллельная чистка индексов раскладывает список мёртвых
                # строк в РАЗДЕЛЯЕМОЙ памяти, а у контейнера postgres это
                # /dev/shm размером 64 МБ по умолчанию. На сервере
                # 22.08.2026 после удаления 3.57 млн строк trades обычный
                # VACUUM ANALYZE упал с «could not resize shared memory
                # segment to 67145248 bytes: No space left on device» —
                # ежесуточная задача завершилась с ошибкой на штатной
                # операции. С PARALLEL 0 та же чистка прошла за минуту.
                # Размер /dev/shm поднят отдельно (shm_size в
                # docker-compose.yml), но полагаться здесь на настройку
                # окружения нельзя: скрипт обязан работать и там, где её
                # не применили.
                _psql(f"VACUUM (ANALYZE, PARALLEL 0) {table};")
                _log(f"{table}: VACUUM (ANALYZE, PARALLEL 0) выполнен.")
            else:
                _log(f"{table}: удалять нечего — VACUUM ANALYZE пропущен.")
        except subprocess.CalledProcessError as exc:
            had_error = True
            _log(f"ОШИБКА при очистке {table}: {(exc.stderr or '').strip() or exc}")
        except Exception as exc:  # noqa: BLE001
            had_error = True
            _log(f"ОШИБКА при очистке {table}: {exc}")

    if had_error:
        _log("Завершено с ошибками (см. выше).")
        return 1
    _log("Готово: политика хранения применена.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
