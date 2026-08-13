# Этап 7.1 — как воспроизвести числа

Диагностика **только читает** БД ролью `agenttrade_ro` (право записи отсутствует).
Ни `.env`, ни `src/`, ни контейнеры не затрагиваются — используется лишь
`docker compose exec -T postgres psql …` для чтения.

## Запуск (на сервере, где поднят стек)

```bash
cd /path/to/project
bash analysis/run_7_1.sh
```

Результаты каждого файла — в `analysis/results/<имя>.out`.

Отдельный запрос можно выполнить так (многострочный SQL подаётся через stdin,
флаг `-T` обязателен):

```bash
docker compose exec -T postgres psql -U agenttrade_ro -d agenttrade \
  < analysis/sql/01_calc1_independent.sql
```

## Состав

| Файл | Расчёт |
|------|--------|
| `sql/00_schema.sql` | Сверка схемы (§3 ТЗ): `\dt`, `\d`, распределение `logic_version` |
| `sql/01_calc1_independent.sql` | Расчёт 1: N окон, доля успеха, pnl (независимые окна) |
| `sql/01b_calc1_baselines_ci.sql` | Расчёт 1: три базовые линии + Wilson/Wald 95% ДИ |
| `sql/02_calc1_fullsample.sql` | Расчёт 1: полная выборка (наблюдения зависимы) |
| `sql/03_calc2_agents.sql` | Расчёт 2: разбор каждого агента |
| `sql/04_calc2_market_missing.sql` | Расчёт 2: пропуски Market по суткам |
| `sql/05_calc3_score_consensus_prob.sql` | Расчёт 3: |балл| / согласованность / probability vs исход |
| `sql/06_calc3_calibration.sql` | Расчёт 3: калибровочная таблица |
| `sql/07_calc4_runlengths.sql` | Расчёт 4: серии неизменных confidence |
| `sql/08_calc5_pairwise.sql` | Расчёт 5: попарная согласованность агентов |

После прогона вставьте содержимое `analysis/results/*.out` в соответствующие
таблицы `analysis/REPORT_7_1.md` (ячейки помечены ⏳).
