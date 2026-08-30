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
 *
 * ЭТАП 9.1.1. Добавлено действие "append_position" — одна строка закрытой
 * виртуальной позиции в РАБОЧИЙ лист владельца. Оно живёт рядом с выгрузкой
 * Этапа 6.6 и пользуется ТЕМ ЖЕ секретом: второй секрет пришлось бы хранить,
 * менять и однажды забыть поменять в одном из двух мест.
 *
 * ЧЕМ ЭТО ДЕЙСТВИЕ ОТЛИЧАЕТСЯ ОТ ВЫГРУЗКИ 6.6, и почему оно осторожнее:
 *   1. Лист НЕ СОЗДАЁТСЯ. Нет листа с таким именем — отказ. Выгрузка 6.6 пишет
 *      в свои служебные листы, а здесь лист чужой и живой; создать вместо него
 *      пустой одноимённый значило бы спрятать опечатку в имени.
 *   2. ЗАГОЛОВКИ СТРОКИ 1 СВЕРЯЮТСЯ. Владелец правит лист руками, и молчаливая
 *      запись в переименованные столбцы — это порча данных.
 *   3. ПИШУТСЯ РОВНО ВОСЕМЬ СТОЛБЦОВ: A, B, C, D, F, H, I, J. Столбец G
 *      («вход - объем») и столбцы K..S — формулы; запись значения в такой
 *      столбец заменила бы формулу числом, и лист перестал бы пересчитываться,
 *      выглядя при этом работающим.
 *   4. ФОРМУЛЫ ПРОТЯГИВАЮТСЯ ДО ЗАПИСИ. Они заведены на конечное число строк
 *      (в образце — до 17-й, то есть на 16 позиций). Строка без формул хуже
 *      отсутствующей строки: она выглядит записанной и не считается никак.
 *      Не удалось протянуть — строка НЕ пишется, действие возвращает ошибку.
 */

const SECRET = 'ВСТАВЬ_СЮДА_СЕКРЕТ';

// Версия приёмника. Возвращается в ответе и попадает в журнал выгрузки —
// по ней видно, что развёрнутая версия действительно обновилась.
const RECEIVER_VERSION = '9.1.1';

// Столбцы, в которые пишет система (Этап 9.1.1 §7.1). Перечень ЗАКРЫТ и
// повторяет src/positions/sheet.py: SHEET_COLUMNS. Всё остальное — формулы.
const POSITION_COLUMNS = ['A', 'B', 'C', 'D', 'F', 'H', 'I', 'J'];

// Столбцы формул, которые протягиваются в новую строку. G — цепочка объёма
// («объём строки = объём предыдущей + прибыль предыдущей»), K..S — расчёты
// листа. Значения в них НЕ ПИШУТСЯ никогда.
const FORMULA_COLUMNS = ['G'];
const FORMULA_RANGE_FIRST = 'K';
const FORMULA_RANGE_LAST = 'S';

// Докуда искать конец блока данных. Больше тысячи строк позиций лист не
// переживёт по другим причинам, а бесконечный поиск в пустом листе — это
// таймаут Apps Script вместо внятного отказа.
const MAX_DATA_ROW = 1000;

function doPost(e) {
  try {
    const body = JSON.parse(e.postData.contents);
    if (body.secret !== SECRET) {
      return json({ ok: false, error: 'forbidden' });
    }
    // Действие по умолчанию — выгрузка Этапа 6.6: старые запросы поля action
    // не присылают, и молчаливая смена их поведения была бы поломкой.
    if (body.action === 'append_position') {
      return appendPosition(body);
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

/**
 * Одна строка закрытой виртуальной позиции в рабочий лист владельца (9.1.1 §7).
 *
 * ПОРЯДОК ШАГОВ СОДЕРЖАТЕЛЕН И ИМЕННО ТАКОЙ:
 *   1. найти лист (нет — отказ);
 *   2. сверить заголовки строки 1 (не совпали — отказ);
 *   3. найти строку для записи (занята — отказ);
 *   4. протянуть формулы (не удалось — отказ, следы протяжки убираются);
 *   5. и только теперь записать восемь значений.
 * Любой отказ означает, что лист устроен не так, как ждёт код. Записать «пока
 * что» и разобраться потом здесь нельзя: разбираться пришлось бы в чужом
 * рабочем документе, уже испорченном.
 */
function appendPosition(body) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheet = ss.getSheetByName(body.sheet);
  if (!sheet) {
    // ЛИСТ НЕ СОЗДАЁТСЯ. Пустой одноимённый лист спрятал бы опечатку в имени,
    // и владелец увидел бы «всё работает» при пустом настоящем листе.
    return json({ ok: false, error: 'лист не найден: ' + body.sheet,
                  version: RECEIVER_VERSION });
  }

  const values = body.values || {};
  const headers = body.headers || {};
  const missing = POSITION_COLUMNS.filter(function (c) {
    return !(c in values);
  });
  if (missing.length) {
    return json({ ok: false,
                  error: 'нет значений для столбцов: ' + missing.join(', '),
                  version: RECEIVER_VERSION });
  }
  const extra = Object.keys(values).filter(function (c) {
    return POSITION_COLUMNS.indexOf(c) < 0;
  });
  if (extra.length) {
    // Лишний столбец — это столбец с формулой: перечень закрыт.
    return json({ ok: false,
                  error: 'запись в столбцы вне перечня: ' + extra.join(', '),
                  version: RECEIVER_VERSION });
  }

  // 2. Заголовки строки 1. Сверяются ТОЛЬКО те восемь, в которые пишем: чужие
  // столбцы владелец волен называть как угодно.
  const mismatched = [];
  for (var i = 0; i < POSITION_COLUMNS.length; i += 1) {
    var column = POSITION_COLUMNS[i];
    var expected = headers[column];
    if (expected === undefined) continue;
    var actual = sheet.getRange(column + '1').getValue();
    if (normalizeHeader(actual) !== normalizeHeader(expected)) {
      mismatched.push(column + ': ожидалось «' + expected + '», в листе «'
                      + actual + '»');
    }
  }
  if (mismatched.length) {
    return json({ ok: false,
                  error: 'заголовки листа не совпали — ' + mismatched.join('; '),
                  version: RECEIVER_VERSION });
  }

  // 3. Строка для записи — ПЕРВАЯ СВОБОДНАЯ В БЛОКЕ ДАННЫХ, а не конец листа.
  // Ниже блока данных живут «итого», «средние» и «баланс / начало»: запись в
  // конец листа легла бы под них, а то и на них.
  var targetRow = firstFreeDataRow(sheet);
  if (targetRow === null) {
    return json({ ok: false,
                  error: 'не найдена свободная строка данных до строки '
                         + MAX_DATA_ROW,
                  version: RECEIVER_VERSION });
  }
  var occupied = POSITION_COLUMNS.filter(function (c) {
    return String(sheet.getRange(c + targetRow).getValue()).length > 0;
  });
  if (occupied.length) {
    return json({ ok: false,
                  error: 'строка ' + targetRow + ' занята в столбцах '
                         + occupied.join(', ') + ' — похоже, блок данных дошёл '
                         + 'до строк «итого»/«средние»',
                  version: RECEIVER_VERSION });
  }

  // 4. Протяжка формул. ДО записи значений и с уборкой следов при неудаче:
  // строка без формул хуже отсутствующей строки, а строка с формулами и без
  // значений — это мусор, оставленный неудавшейся попыткой.
  var pulled;
  try {
    pulled = pullFormulas(sheet, targetRow);
  } catch (err) {
    clearFormulas(sheet, targetRow);
    return json({ ok: false, error: 'формулы не протянулись: ' + String(err),
                  version: RECEIVER_VERSION });
  }
  if (!pulled) {
    clearFormulas(sheet, targetRow);
    return json({ ok: false,
                  error: 'формулы не протянулись: не найдена строка-образец с '
                         + 'формулой в столбце ' + FORMULA_COLUMNS[0],
                  version: RECEIVER_VERSION });
  }

  // 5. Значения — по одной ячейке на столбец. Диапазоном писать нельзя: между
  // F и H стоит G с формулой, и непрерывный диапазон затёр бы её.
  for (var k = 0; k < POSITION_COLUMNS.length; k += 1) {
    var col = POSITION_COLUMNS[k];
    sheet.getRange(col + targetRow).setValue(values[col]);
  }
  return json({ ok: true, inserted: 1, row: targetRow, sheet: body.sheet,
                version: RECEIVER_VERSION });
}

/** Заголовок к сравнимому виду: регистр, края и повторные пробелы не считаются. */
function normalizeHeader(value) {
  return String(value === null || value === undefined ? '' : value)
    .replace(/\s+/g, ' ')
    .trim()
    .toLowerCase();
}

/**
 * Первая строка блока данных без даты входа (столбец A).
 *
 * ПОИСК ИДЁТ ДО ПЕРВОГО ПРОБЕЛА, а не до последней непустой строки листа: ниже
 * блока данных стоят «итого» и «средние», у которых столбец A тоже заполнен, и
 * «последняя непустая» указала бы на них.
 */
function firstFreeDataRow(sheet) {
  for (var row = 2; row <= MAX_DATA_ROW; row += 1) {
    var value = sheet.getRange('A' + row).getValue();
    if (String(value === null || value === undefined ? '' : value).length === 0) {
      return row;
    }
  }
  return null;
}

/**
 * Копирует формулы из последней строки, где они есть, в строку ``targetRow``.
 *
 * Копируются ТОЛЬКО формулы (PASTE_FORMULA): относительные ссылки при этом
 * сдвигаются сами, и цепочка G («объём строки = объём предыдущей + прибыль
 * предыдущей») продолжается, а не повторяет чужую строку.
 *
 * Возвращает true, если образец найден и формула в G после копирования есть.
 */
function pullFormulas(sheet, targetRow) {
  var source = null;
  for (var row = targetRow - 1; row >= 2; row -= 1) {
    if (String(sheet.getRange(FORMULA_COLUMNS[0] + row).getFormula()).length > 0) {
      source = row;
      break;
    }
  }
  if (source === null) return false;

  for (var i = 0; i < FORMULA_COLUMNS.length; i += 1) {
    var c = FORMULA_COLUMNS[i];
    sheet.getRange(c + source).copyTo(sheet.getRange(c + targetRow),
                                      SpreadsheetApp.CopyPasteType.PASTE_FORMULA,
                                      false);
  }
  sheet.getRange(FORMULA_RANGE_FIRST + source + ':' + FORMULA_RANGE_LAST + source)
       .copyTo(sheet.getRange(FORMULA_RANGE_FIRST + targetRow + ':'
                              + FORMULA_RANGE_LAST + targetRow),
               SpreadsheetApp.CopyPasteType.PASTE_FORMULA, false);

  // ПРОВЕРКА, А НЕ НАДЕЖДА: копирование могло пройти и ничего не оставить
  // (образец без формулы, защищённый диапазон). Строка без формул выглядит
  // записанной и не считается никак — это и есть тот случай, ради которого
  // проверка здесь стоит.
  return String(sheet.getRange(FORMULA_COLUMNS[0] + targetRow).getFormula())
           .length > 0;
}

/** Убирает следы неудавшейся протяжки: строка обязана остаться как была. */
function clearFormulas(sheet, targetRow) {
  try {
    for (var i = 0; i < FORMULA_COLUMNS.length; i += 1) {
      sheet.getRange(FORMULA_COLUMNS[i] + targetRow).clearContent();
    }
    sheet.getRange(FORMULA_RANGE_FIRST + targetRow + ':'
                   + FORMULA_RANGE_LAST + targetRow).clearContent();
  } catch (err) {
    // Убрать не удалось — молчим: сообщать об этом поверх настоящей причины
    // отказа значило бы спрятать её.
  }
}
