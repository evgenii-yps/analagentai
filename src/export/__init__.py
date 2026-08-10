"""Выгрузка закрытых сигналов наружу (Этап 6.6).

Пакет разделён на:

* :mod:`src.export.transform` — чистые функции сборки строк для Google Таблицы
  и свойств страниц Notion (без ввода-вывода, легко тестируются);
* :mod:`src.export.sheets` — клиент приёмника Apps Script (POST с ретраями);
* :mod:`src.export.notion` — клиент REST API Notion (создание страниц);
* :mod:`src.export.queries` — SQL-запросы выборки сигналов и агрегатов.

Оркестратор, запускаемый на хосте, — ``scripts/export_signals.py``.
"""
