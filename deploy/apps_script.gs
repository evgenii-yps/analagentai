/**
 * Приёмник выгрузки сигналов Agent Trade для Google Таблицы (Этап 6.6, §8.2).
 *
 * Установка и ОБНОВЛЕНИЕ (делает заказчик — этот код живёт на стороне Google и
 * НЕ собирается вместе с образом контейнера; подробно — reports/8_4_1_report.md):
 *   1. https://sheets.new → назвать «Agent Trade — Сигналы».
 *   2. Расширения → Apps Script, удалить весь код, вставить этот.
 *   3. Заменить ВСТАВЬ_СЮДА_СЕКРЕТ на строку 20+ символов (латиница/цифры);
 *      её же положить в .env как SHEETS_SHARED_SECRET.
 *   4. Сохранить → Развернуть → Управление развёртываниями → карандаш →
 *      Версия: «Новая версия» → Развернуть. URL /exec НЕ меняется.
 *
 * Защита — секрет ниже (проверяется на каждом запросе). URL и секрет не публиковать.
 *
 * ЭТАП 8.4.1. Ширина диапазона записи берётся по САМОЙ ШИРОКОЙ строке пачки, а
 * не по первой. Прежний код брал rows[0].length: на листе «Независимые окна»
 * первой строкой идёт оговорка из ОДНОГО элемента, и запись пятнадцати колонок
 * обрывалась ошибкой «In den Daten sind es 15, im Bereich jedoch 1». Подгонять
 * оговорку под пятнадцать колонок было бы лечением симптома: следующее
 * изменение состава колонок сломало бы запись снова. Короткие строки
 * дополняются пустыми ячейками, поэтому набор строк РАЗНОЙ длины — штатный
 * случай, а не отказ.
 */

const SECRET = 'ВСТАВЬ_СЮДА_СЕКРЕТ';

// Версия приёмника. Возвращается в ответе и попадает в журнал выгрузки —
// по ней видно, что развёрнутая версия действительно обновилась.
const RECEIVER_VERSION = '8.4.1';

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

    const rows = body.rows || [];
    // Ширина листа — максимум по заголовку и ВСЕМ строкам пачки. Заголовок
    // участвует наравне: он тоже задаёт число колонок.
    const width = maxWidth(rows, body.header);
    if (width === 0) {
      return json({ ok: true, inserted: 0, sheet: body.sheet, width: 0,
                    version: RECEIVER_VERSION });
    }
    ensureColumns(sheet, width);

    if (body.header && sheet.getLastRow() === 0) {
      sheet.appendRow(pad(body.header, width));
      sheet.setFrozenRows(1);
    }
    if (rows.length > 0) {
      const padded = rows.map(function (row) { return pad(row, width); });
      sheet.getRange(sheet.getLastRow() + 1, 1, padded.length, width)
           .setValues(padded);
    }
    return json({ ok: true, inserted: rows.length, sheet: body.sheet,
                  width: width, version: RECEIVER_VERSION });
  } catch (err) {
    return json({ ok: false, error: String(err), version: RECEIVER_VERSION });
  }
}

/** Самая широкая строка пачки с учётом заголовка. Пустая пачка даёт 0. */
function maxWidth(rows, header) {
  var width = (header && header.length) ? header.length : 0;
  for (var i = 0; i < rows.length; i++) {
    var row = rows[i];
    var n = (row && row.length) ? row.length : 0;
    if (n > width) width = n;
  }
  return width;
}

/** Строка ровно нужной ширины: короткая дополняется пустыми ячейками. */
function pad(row, width) {
  var out = (row || []).slice(0, width);
  while (out.length < width) out.push('');
  return out;
}

/** Лист обязан иметь не меньше колонок, чем ширина пачки. */
function ensureColumns(sheet, width) {
  var have = sheet.getMaxColumns();
  if (width > have) sheet.insertColumnsAfter(have, width - have);
}

function json(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
