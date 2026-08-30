/**
 * Приёмник выгрузки сигналов Agent Trade для Google Таблицы (Этап 6.6, §8.2).
 *
 * ОБНОВЛЕНИЕ РАЗВЁРНУТОЙ ВЕРСИИ (делает владелец — этот код живёт на стороне
 * Google и НЕ собирается вместе с образом контейнера):
 *   1. Открыть книгу «Analitik Agent».
 *   2. Расширения → Apps Script, удалить весь код, вставить этот.
 *   3. ЗАМЕНИТЬ ВСТАВЬ_СЮДА_СЕКРЕТ на ТОТ ЖЕ секрет, что был (он же лежит в
 *      .env в SHEETS_SHARED_SECRET). Забытый секрет — самая частая ошибка при
 *      обновлении: выгрузка начнёт отвечать forbidden, а причина будет
 *      выглядеть как поломка сети.
 *   4. Сохранить → Развернуть → Управление развёртываниями → карандаш →
 *      Версия: «Новая версия» → Развернуть. URL /exec НЕ меняется, .env
 *      трогать не нужно.
 *
 * ПРОВЕРКА ПОСЛЕ ОБНОВЛЕНИЯ: в журнале выгрузки обязана появиться строка с
 * receiver_version=9.1.2. Старая версия ответит на table_append ошибкой — и
 * это правильно: видимый отказ лучше тихой записи не туда.
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
 * ЭТАП 9.1.2. Добавлены два режима для ТОРГОВОГО ЖУРНАЛА — листа, который
 * владелец правит руками и в котором живут его формулы:
 *
 *   table_append — создать строки сделок. Лист устроен не как журнал, а как
 *     БЛАНК: строка 1 — заголовки, дальше пустые строки с формулами, ниже блок
 *     «итого:» / «средние:», ещё ниже «баланс / начало». Обычный appendRow
 *     добавляет после ПОСЛЕДНЕЙ заполненной строки листа, то есть ниже слова
 *     «начало» — за пределами таблицы и вне всех формул. Поэтому строки ищутся
 *     сверху: первая свободная по столбцу A, но не ниже строки «итого:», а если
 *     свободных не хватает — вставляются новые ПЕРЕД строкой итогов
 *     (insertRowsBefore), чтобы диапазоны формул итогов растянулись сами.
 *
 *   table_update — дозаписать закрытие в УЖЕ СУЩЕСТВУЮЩУЮ строку по метке
 *     [поз. <id>] в столбце заметок. Метку не нашли — строку НЕ УГАДЫВАТЬ:
 *     дописать выход не в ту строку хуже, чем не дописать вовсе. Такие метки
 *     возвращаются в notFound, и клиент кладёт сделку отдельной полной строкой.
 *
 * ФОРМУЛЫ ВЛАДЕЛЬЦА НЕ ПЕРЕПИСЫВАЮТСЯ НИ ОДНОЙ ЯЧЕЙКОЙ. Открытие пишет A–G,
 * закрытие — H..J, а столбцы K и правее только ПРОТЯГИВАЮТСЯ копированием из
 * строки выше. Если протянуть неоткуда — формулы не выдумываются: ответ
 * остаётся ok:true, но несёт warning. Сочинённая формула — это чужая модель
 * денег, написанная за владельца.
 */

const SECRET = 'ВСТАВЬ_СЮДА_СЕКРЕТ';

// Версия приёмника. Возвращается в ответе и попадает в журнал выгрузки —
// по ней видно, что развёрнутая версия действительно обновилась.
const RECEIVER_VERSION = '9.1.2';

// Сколько строк листа просматривать в поисках строки итогов и свободного места
// (Этап 9.1.2). Торговый журнал на тысячу сделок не рассчитан по другим
// причинам, а бесконечный поиск в пустом листе — это таймаут Apps Script
// вместо внятного ответа.
const TABLE_SCAN_LIMIT = 2000;

function doPost(e) {
  try {
    const body = JSON.parse(e.postData.contents);
    if (body.secret !== SECRET) {
      return json({ ok: false, error: 'forbidden' });
    }
    // ВОПРОС О ВЕРСИИ ОБРАБАТЫВАЕТСЯ ПЕРВЫМ И НИЧЕГО НЕ ДЕЛАЕТ. Клиент
    // спрашивает её ПЕРЕД первой записью в торговый журнал: старая версия
    // приёмника новых режимов не знает и обрабатывает их общим путём — то есть
    // МОЛЧА пишет не туда (table_append уходит в конец листа, ниже блока
    // «баланс / начало») или молча не делает ничего (table_update возвращает
    // inserted:0, а клиент считает закрытие записанным и ставит отметку —
    // закрытие сделки теряется навсегда). Инструкция «сначала обновите скрипт»
    // защищает от этого только словами; этот режим позволяет защититься кодом.
    if (body.mode === 'version') {
      return json({ ok: true, version: RECEIVER_VERSION });
    }

    // РЕЖИМЫ ТОРГОВОГО ЖУРНАЛА ОБРАБАТЫВАЮТСЯ ДО ОБЩЕГО ПУТИ. Они пишут в
    // ЧУЖОЙ живой лист и потому ведут себя осторожнее: лист не создают,
    // формулы не трогают, при непонятной ситуации отказываются работать.
    if (body.mode === 'table_append') return tableAppend(body);
    if (body.mode === 'table_update') return tableUpdate(body);

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

// ===========================================================================
// ЭТАП 9.1.2. Торговый журнал: создание строк и дозапись закрытия
// ===========================================================================

/**
 * Создать строки сделок В ТАБЛИЦЕ, а не в конце листа (§3 ТЗ 9.1.2).
 *
 * Поля запроса: sheet, rows (пачка строк), notes (по строке текста на каждую
 * строку пачки), noteColumn, totalsMarker, formulaFromColumn.
 *
 * ЛИСТ НЕ СОЗДАЁТСЯ. Опечатка в имени должна выглядеть как отказ, а не как
 * пустой одноимённый лист, в котором «всё работает».
 */
function tableAppend(body) {
  const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(body.sheet);
  if (!sheet) {
    return json({ ok: false, error: 'лист не найден: ' + body.sheet,
                  version: RECEIVER_VERSION });
  }
  const rows = body.rows || [];
  const notes = body.notes || [];
  if (rows.length === 0) {
    return json({ ok: true, inserted: 0, updated: 0, sheet: body.sheet,
                  version: RECEIVER_VERSION });
  }
  const width = maxWidth(rows, null);
  const noteColumn = body.noteColumn || 20;
  const formulaFrom = body.formulaFromColumn || 11;
  ensureColumns(sheet, Math.max(width, noteColumn));

  const totalsRow = findTotalsRow(sheet, body.totalsMarker || 'итого:');
  let startRow = firstFreeTableRow(sheet, totalsRow);
  // МЕСТА МОЖЕТ НЕ ХВАТИТЬ, и это штатный случай: строк с формулами в бланке
  // конечное число. Вставка ПЕРЕД строкой итогов растягивает диапазоны их
  // формул сама и уводит блок «баланс / начало» вниз целиком.
  const free = totalsRow - startRow;
  if (free < rows.length) {
    sheet.insertRowsBefore(totalsRow, rows.length - free);
  }

  const padded = rows.map(function (row) { return pad(row, width); });
  sheet.getRange(startRow, 1, padded.length, width).setValues(padded);
  for (var i = 0; i < padded.length; i += 1) {
    if (notes[i] !== undefined && notes[i] !== null) {
      sheet.getRange(startRow + i, noteColumn).setValue(notes[i]);
    }
  }

  var warning = pullFormulasDown(sheet, startRow, padded.length, formulaFrom);
  var answer = { ok: true, inserted: padded.length, updated: 0,
                 sheet: body.sheet, startRow: startRow,
                 version: RECEIVER_VERSION };
  if (warning) answer.warning = warning;
  return json(answer);
}

/**
 * Дозаписать закрытие в СУЩЕСТВУЮЩУЮ строку по метке в столбце заметок.
 *
 * Поля: sheet, noteColumn, updates — массив {marker, values, note, startColumn}.
 *
 * МЕТКУ НЕ НАШЛИ — СТРОКУ НЕ УГАДЫВАТЬ. Дописать выход не в ту строку хуже, чем
 * не дописать вовсе: числа выглядели бы настоящими. Ненайденные метки уходят в
 * notFound, и клиент кладёт такие сделки отдельными полными строками.
 *
 * ЗАМЕТКА ДОПИСЫВАЕТСЯ, А НЕ ПЕРЕЗАПИСЫВАЕТСЯ (§16 ТЗ). Столбец заметок —
 * ЕДИНСТВЕННОЕ место строки, куда человек пишет руками: пока сделка шла,
 * владелец мог занести туда своё наблюдение. Замена ячейки целиком стёрла бы
 * его, причём молча и безвозвратно. Клиент присылает только ХВОСТ
 * (``noteAppend``), приёмник читает текущее содержимое и дописывает к нему.
 *
 * ПОВТОРНОЕ ДОПИСЫВАНИЕ НЕ ДЕЛАЕТСЯ. Запись могла удаться, а ответ — не дойти
 * (обрыв сети), и следующий прогон пришлёт тот же хвост. Если он уже есть в
 * ячейке, второй раз он не добавляется: заметка с дважды повторённым итогом
 * выглядела бы как две сделки в одной строке.
 */
function tableUpdate(body) {
  const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(body.sheet);
  if (!sheet) {
    return json({ ok: false, error: 'лист не найден: ' + body.sheet,
                  version: RECEIVER_VERSION });
  }
  const updates = body.updates || [];
  const noteColumn = body.noteColumn || 20;
  const notFound = [];
  var updated = 0;
  if (updates.length === 0) {
    return json({ ok: true, inserted: 0, updated: 0, sheet: body.sheet,
                  version: RECEIVER_VERSION });
  }

  // Столбец заметок читается ОДИН РАЗ на весь запрос: чтение ячейки в Apps
  // Script стоит сетевого обращения, и поиск по одной ячейке на метку
  // превратил бы дозапись десяти сделок в тысячи обращений.
  const lastRow = Math.min(sheet.getLastRow(), TABLE_SCAN_LIMIT);
  const noteValues = lastRow > 0
    ? sheet.getRange(1, noteColumn, lastRow, 1).getDisplayValues()
    : [];

  for (var i = 0; i < updates.length; i += 1) {
    var item = updates[i];
    var row = findRowByMarker(noteValues, item.marker);
    if (row === 0) {
      notFound.push(item.marker);
      continue;
    }
    var values = item.values || [];
    var startColumn = item.startColumn || 8;
    if (values.length > 0) {
      ensureColumns(sheet, startColumn + values.length - 1);
      sheet.getRange(row, startColumn, 1, values.length).setValues([values]);
    }
    // Хвост заметки ДОПИСЫВАЕТСЯ к тому, что в ячейке уже есть.
    var tail = item.noteAppend;
    if (tail !== undefined && tail !== null && String(tail).length > 0) {
      var cell = sheet.getRange(row, noteColumn);
      var current = String(cell.getValue() === null ? '' : cell.getValue());
      // Уже дописано — не дописываем второй раз (повтор после сбоя сети).
      if (current.indexOf(String(tail)) < 0) {
        cell.setValue(current + String(tail));
      }
    }
    updated += 1;
  }

  var answer = { ok: true, inserted: 0, updated: updated, sheet: body.sheet,
                 version: RECEIVER_VERSION };
  if (notFound.length > 0) answer.notFound = notFound;
  return json(answer);
}

/**
 * Номер строки итогов — первой, где есть ячейка с текстом маркера.
 *
 * Сравнение без учёта регистра и краевых пробелов. Строки итогов нет вовсе —
 * границей считается строка сразу за последней заполненной: тогда таблица
 * просто растёт вниз, и это тоже рабочий случай, а не отказ.
 */
function findTotalsRow(sheet, marker) {
  var wanted = String(marker).trim().toLowerCase();
  var lastRow = Math.min(sheet.getLastRow(), TABLE_SCAN_LIMIT);
  var lastCol = sheet.getLastColumn();
  if (lastRow === 0 || lastCol === 0) return 2;
  var grid = sheet.getRange(1, 1, lastRow, lastCol).getDisplayValues();
  for (var r = 0; r < grid.length; r += 1) {
    for (var c = 0; c < grid[r].length; c += 1) {
      var text = String(grid[r][c] === null ? '' : grid[r][c]).trim().toLowerCase();
      if (text === wanted || (wanted.length > 0 && text.indexOf(wanted) === 0)) {
        return r + 1;
      }
    }
  }
  return lastRow + 1;
}

/**
 * Первая свободная строка ТАБЛИЦЫ: со второй, у которой пуст столбец A.
 *
 * Ищется сверху и НЕ НИЖЕ строки итогов: ниже неё живут «средние» и
 * «баланс / начало», и запись туда испортила бы чужие числа.
 */
function firstFreeTableRow(sheet, totalsRow) {
  var limit = Math.max(2, totalsRow - 1);
  var height = limit - 2 + 1;
  if (height <= 0) return totalsRow;
  var column = sheet.getRange(2, 1, height, 1).getDisplayValues();
  for (var i = 0; i < column.length; i += 1) {
    var text = String(column[i][0] === null ? '' : column[i][0]).trim();
    if (text.length === 0) return i + 2;
  }
  return totalsRow;
}

/** Номер строки, в заметке которой встречается метка. 0 — не найдена. */
function findRowByMarker(noteValues, marker) {
  var needle = String(marker);
  if (needle.length === 0) return 0;
  for (var i = 0; i < noteValues.length; i += 1) {
    var text = String(noteValues[i][0] === null ? '' : noteValues[i][0]);
    if (text.indexOf(needle) >= 0) return i + 1;
  }
  return 0;
}

/**
 * Протянуть формулы столбцов ``fromColumn``..конец на созданные строки.
 *
 * Источник — строка НАД первой созданной. Формул там нет (или это заголовок) —
 * ФОРМУЛЫ НЕ ВЫДУМЫВАЮТСЯ: возвращается текст предупреждения, и клиент печатает
 * его в журнал. Сочинённая формула — это чужая модель денег, написанная за
 * владельца, и заметить подмену по виду листа было бы невозможно.
 *
 * Возвращает '' при успехе либо текст предупреждения.
 */
function pullFormulasDown(sheet, startRow, count, fromColumn) {
  var lastCol = sheet.getLastColumn();
  if (lastCol < fromColumn) {
    return 'в листе нет столбцов с формулами правее ' + fromColumn;
  }
  var sourceRow = startRow - 1;
  if (sourceRow < 2) {
    return 'формулы не протянуты: над строкой ' + startRow
           + ' нет строки с формулами (это заголовок)';
  }
  var width = lastCol - fromColumn + 1;
  var source = sheet.getRange(sourceRow, fromColumn, 1, width);
  var formulas = source.getFormulas()[0];
  var hasAny = false;
  for (var i = 0; i < formulas.length; i += 1) {
    if (String(formulas[i]).length > 0) { hasAny = true; break; }
  }
  if (!hasAny) {
    return 'формулы не протянуты: в строке ' + sourceRow
           + ' столбцы ' + fromColumn + '..' + lastCol + ' без формул';
  }
  source.copyTo(sheet.getRange(startRow, fromColumn, count, width),
                SpreadsheetApp.CopyPasteType.PASTE_FORMULA, false);
  return '';
}
