/**
 * Приёмник выгрузки сигналов Agent Trade для Google Таблицы (Этап 6.6, §8.2).
 *
 * Установка (делает заказчик, подробно — в EXPORT_REPORT.md, блок §8.1):
 *   1. https://sheets.new → назвать «Agent Trade — Сигналы».
 *   2. Расширения → Apps Script, удалить весь код, вставить этот.
 *   3. Заменить ВСТАВЬ_СЮДА_СЕКРЕТ на строку 20+ символов (латиница/цифры);
 *      её же положить в .env как SHEETS_SHARED_SECRET.
 *   4. Сохранить → Развернуть → Новое развёртывание → Веб-приложение
 *      («Запуск от имени: Я», «Доступ: Все»). Скопировать URL /exec → SHEETS_WEBAPP_URL.
 *
 * Защита — секрет ниже (проверяется на каждом запросе). URL и секрет не публиковать.
 */

const SECRET = 'ВСТАВЬ_СЮДА_СЕКРЕТ';

function doPost(e) {
  try {
    const body = JSON.parse(e.postData.contents);
    if (body.secret !== SECRET) {
      return json({ ok: false, error: 'forbidden' });
    }
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    let sheet = ss.getSheetByName(body.sheet);
    if (!sheet) sheet = ss.insertSheet(body.sheet);

    if (body.mode === 'replace') {
      sheet.clear();
    }
    if (body.header && sheet.getLastRow() === 0) {
      sheet.appendRow(body.header);
      sheet.setFrozenRows(1);
    }
    const rows = body.rows || [];
    if (rows.length > 0) {
      sheet.getRange(sheet.getLastRow() + 1, 1, rows.length, rows[0].length)
           .setValues(rows);
    }
    return json({ ok: true, inserted: rows.length, sheet: body.sheet });
  } catch (err) {
    return json({ ok: false, error: String(err) });
  }
}

function json(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
