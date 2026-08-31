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
 * receiver_version=9.1.2.2. Старая версия ответит на table_append ошибкой — и
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
 * ЭТАП 9.1.2.2. Две правки, обе — о ЧУЖИХ ДАННЫХ В СВОЕЙ СТРОКЕ.
 *
 *   1. ПРОТЯЖКА ФОРМУЛ БОЛЬШЕ НЕ ТАЩИТ ЛИТЕРАЛЫ. Прежняя редакция копировала
 *      диапазон K..последний столбец целиком, вызовом copyTo с PASTE_FORMULA.
 *      PASTE_FORMULA переносит не только формулы: ячейка без формулы приходит
 *      своим ЗНАЧЕНИЕМ. Столбец заметок T лежит ВНУТРИ этого диапазона, и
 *      каждая созданная строка получала заметку строки выше — вместе с чужой
 *      целью, чужим пределом и чужим номером сигнала. На боевом листе
 *      31.08.2026 так вышли три строки подряд с меткой [поз. 10]. Теперь
 *      копируются ТОЛЬКО ячейки с непустой формулой, столбец заметок исключён
 *      безусловно, а сама заметка пишется ПОСЛЕ протяжки, а не до.
 *
 *   2. ДОЗАПИСЬ ПО НЕОДНОЗНАЧНОЙ МЕТКЕ ЗАПРЕЩЕНА. Прежняя редакция писала в
 *      ПЕРВУЮ строку, содержащую метку. При трёх строках с [поз. 10] цена
 *      выхода одной сделки ушла бы в строку другой — лист остался бы
 *      правдоподобным и стал бы неверным. Теперь считаются ВСЕ совпадения:
 *      одно — пишем, ноль — notFound (как прежде), два и больше — не пишем
 *      НИЧЕГО и возвращаем метку в ambiguous с номерами строк. Симметрично и
 *      в table_append: метка, которая в листе уже есть, второй строки не
 *      получает — дубль строки открытия та же порча, только с другой стороны.
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
const RECEIVER_VERSION = '9.1.2.2';

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
  const noteColumn = body.noteColumn || 20;
  const formulaFrom = body.formulaFromColumn || 11;
  ensureColumns(sheet, Math.max(maxWidth(rows, null), noteColumn));

  // МЕТКА, КОТОРАЯ В ЛИСТЕ УЖЕ ЕСТЬ, ВТОРОЙ СТРОКИ НЕ ПОЛУЧАЕТ (§2 ТЗ 9.1.2.2).
  // Дубль строки открытия — та же порча, что и дозапись по неоднозначной метке,
  // только с другой стороны: после него дозапись становится невозможной уже для
  // ОБЕИХ строк, и сделка застревает в листе навсегда незакрытой.
  const existingNotes = readNoteColumn(sheet, noteColumn);
  const ambiguous = [];
  const keptRows = [];
  const keptNotes = [];
  // Object.create(null), а не {}: у обычного объекта унаследованы ключи вроде
  // «constructor», и проверка занятости срабатывала бы на них ложно. Метка
  // всегда начинается со скобки и столкнуться с ними не может, но полагаться
  // на это незачем — словарь без прототипа стоит тех же двух слов.
  const seenInBatch = Object.create(null);
  for (var k = 0; k < rows.length; k += 1) {
    var marker = markerOfNote(notes[k]);
    if (marker.length > 0) {
      var clash = markerRows(existingNotes, marker);
      if (clash.length > 0) {
        ambiguous.push({ marker: marker, rows: clash });
        continue;
      }
      // Повтор ВНУТРИ одной пачки: строк в листе ещё нет, поэтому и номеров
      // нет — но создавать вторую строку нельзя ровно по той же причине.
      if (seenInBatch[marker]) {
        ambiguous.push({ marker: marker, rows: [] });
        continue;
      }
      seenInBatch[marker] = true;
    }
    keptRows.push(rows[k]);
    keptNotes.push(notes[k]);
  }

  if (keptRows.length === 0) {
    // Создавать нечего — лист не трогаем вовсе: ни вставки строк, ни записи.
    var refused = { ok: true, inserted: 0, updated: 0, sheet: body.sheet,
                    version: RECEIVER_VERSION };
    if (ambiguous.length > 0) refused.ambiguous = ambiguous;
    return json(refused);
  }

  const width = maxWidth(keptRows, null);
  const totalsRow = findTotalsRow(sheet, body.totalsMarker || 'итого:');
  let startRow = firstFreeTableRow(sheet, totalsRow);
  // МЕСТА МОЖЕТ НЕ ХВАТИТЬ, и это штатный случай: строк с формулами в бланке
  // конечное число. Вставка ПЕРЕД строкой итогов растягивает диапазоны их
  // формул сама и уводит блок «баланс / начало» вниз целиком.
  const free = totalsRow - startRow;
  if (free < keptRows.length) {
    sheet.insertRowsBefore(totalsRow, keptRows.length - free);
  }

  const padded = keptRows.map(function (row) { return pad(row, width); });
  sheet.getRange(startRow, 1, padded.length, width).setValues(padded);

  // ПОРЯДОК ЗДЕСЬ — ЧАСТЬ ИСПРАВЛЕНИЯ, А НЕ ОФОРМЛЕНИЕ (§1 ТЗ 9.1.2.2). Сначала
  // протяжка формул, и только потом заметки: пока заметка писалась ПЕРВОЙ, любая
  // ошибка в отборе копируемых ячеек стирала её молча. Теперь заметка ложится
  // последней и не зависит от того, что делает протяжка.
  var warning = pullFormulasDown(sheet, startRow, padded.length, formulaFrom,
                                 noteColumn);
  for (var i = 0; i < padded.length; i += 1) {
    if (keptNotes[i] !== undefined && keptNotes[i] !== null) {
      sheet.getRange(startRow + i, noteColumn).setValue(keptNotes[i]);
    }
  }

  var answer = { ok: true, inserted: padded.length, updated: 0,
                 sheet: body.sheet, startRow: startRow,
                 version: RECEIVER_VERSION };
  if (warning) answer.warning = warning;
  if (ambiguous.length > 0) answer.ambiguous = ambiguous;
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
 * МЕТКА НАШЛАСЬ ДВАЖДЫ — ТОЖЕ НЕ УГАДЫВАТЬ (§2 ТЗ 9.1.2.2). Прежняя редакция
 * писала в ПЕРВУЮ найденную строку. При трёх строках с меткой [поз. 10] цена
 * выхода одной сделки ушла бы в строку другой: лист остался бы правдоподобным и
 * стал бы неверным — это тихая порча данных, худший из возможных исходов.
 * Теперь считаются ВСЕ совпадения, и при двух и более не пишется НИЧЕГО: ни
 * столбцы H..J, ни заметка. Метка уходит в ambiguous вместе с номерами строк,
 * чтобы владелец знал, какие именно строки листа надо разобрать руками.
 *
 * ОТЛИЧИЕ ambiguous ОТ notFound СОДЕРЖАТЕЛЬНО, а не техническое: при notFound
 * строки НЕТ, и лишняя строка лучше потерянной сделки; при ambiguous строк уже
 * СЛИШКОМ МНОГО, и добавлять к ним ещё одну — усугублять. Поэтому клиент по
 * ambiguous новой строки не создаёт никогда.
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
  const ambiguous = [];
  var updated = 0;
  if (updates.length === 0) {
    return json({ ok: true, inserted: 0, updated: 0, sheet: body.sheet,
                  version: RECEIVER_VERSION });
  }

  // Столбец заметок читается ОДИН РАЗ на весь запрос: чтение ячейки в Apps
  // Script стоит сетевого обращения, и поиск по одной ячейке на метку
  // превратил бы дозапись десяти сделок в тысячи обращений.
  const noteValues = readNoteColumn(sheet, noteColumn);

  for (var i = 0; i < updates.length; i += 1) {
    var item = updates[i];
    var found = markerRows(noteValues, item.marker);
    if (found.length === 0) {
      notFound.push(item.marker);
      continue;
    }
    if (found.length > 1) {
      // НЕ ПИШЕТСЯ НИЧЕГО. Выбрать «первую подходящую» здесь — значит записать
      // цену выхода одной сделки в строку другой.
      ambiguous.push({ marker: String(item.marker), rows: found });
      continue;
    }
    var row = found[0];
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
  if (ambiguous.length > 0) answer.ambiguous = ambiguous;
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

/**
 * Столбец заметок целиком, ОДНИМ обращением. Пустой лист даёт пустой массив.
 *
 * Читается один раз на запрос и в table_append, и в table_update: чтение ячейки
 * в Apps Script стоит сетевого обращения, и поиск по одной ячейке на метку
 * превратил бы работу с десятком сделок в тысячи обращений.
 */
function readNoteColumn(sheet, noteColumn) {
  var lastRow = Math.min(sheet.getLastRow(), TABLE_SCAN_LIMIT);
  if (lastRow <= 0) return [];
  return sheet.getRange(1, noteColumn, lastRow, 1).getDisplayValues();
}

/**
 * Метка в начале заметки — «[...]» первым, что стоит в тексте. '' — метки нет.
 *
 * ФОРМАТ МЕТКИ ЗДЕСЬ НЕ ЗАШИТ. Приёмнику незачем знать слово «поз.»: метка — это
 * то, что клиент поставил в НАЧАЛО заметки в квадратных скобках, и таков её
 * договор с самого Этапа 9.1.2. Зашитый здесь русский текст пришлось бы держать
 * согласованным с POSITION_MARKER_TEMPLATE на другой стороне провода — два
 * места, знающих одно и то же, однажды разойдутся.
 */
function markerOfNote(note) {
  var text = String(note === null || note === undefined ? '' : note);
  var match = /^\s*(\[[^\]]*\])/.exec(text);
  return match ? match[1] : '';
}

/**
 * ВСЕ номера строк, в заметке которых встречается метка. Пусто — не найдена.
 *
 * СЧИТАЮТСЯ ИМЕННО ВСЕ, а не первая (§2 ТЗ 9.1.2.2): вызывающий обязан отличить
 * «одна строка» от «две и больше», и вернуть ему первую попавшуюся значило бы
 * скрыть от него ровно ту разницу, ради которой он спрашивает.
 *
 * Совпадением считается вхождение метки ЦЕЛИКОМ, вместе с обеими скобками.
 * Закрывающая скобка здесь не украшение: без неё «[поз. 1]» совпало бы с
 * «[поз. 12]», и закрытие первой позиции ушло бы в строку двенадцатой. Владелец
 * волен дописывать вокруг метки свой текст — сама метка должна остаться цела.
 */
function markerRows(noteValues, marker) {
  var needle = String(marker === null || marker === undefined ? '' : marker);
  var found = [];
  if (needle.length === 0) return found;
  for (var i = 0; i < noteValues.length; i += 1) {
    var text = String(noteValues[i][0] === null ? '' : noteValues[i][0]);
    if (text.indexOf(needle) >= 0) found.push(i + 1);
  }
  return found;
}

/**
 * Какие столбцы ПРОТЯГИВАТЬ вниз: номера, с единицы (§1 ТЗ 9.1.2.2).
 *
 * ``formulas`` — одна строка из getFormulas() диапазона, начинающегося со
 * столбца ``fromColumn``. Ячейка без формулы отдаёт оттуда пустую строку.
 *
 * ДВА ОГРАЖДЕНИЯ, И ВТОРОЕ ПОВЕРХ ПЕРВОГО:
 *
 *  1. КОПИРУЕТСЯ ТОЛЬКО ЯЧЕЙКА С НЕПУСТОЙ ФОРМУЛОЙ. Копирование литерала — это
 *     перенос ЧУЖИХ ДАННЫХ в новую строку, а не продолжение расчёта, и заметка
 *     лишь самый заметный его случай: точно так же переехали бы вниз любой
 *     комментарий, пометка и число, набранное руками. Прежняя редакция звала
 *     copyTo на весь диапазон разом, а PASTE_FORMULA переносит и литералы —
 *     отсюда три строки подряд с чужой меткой на боевом листе 31.08.2026.
 *  2. СТОЛБЕЦ ЗАМЕТОК ИСКЛЮЧЁН БЕЗУСЛОВНО — даже если в нём вдруг окажется
 *     формула. Заметка — ЕДИНСТВЕННЫЙ ключ поиска строки при дозаписи, и её
 *     потеря стоит дороже потери формулы: формулу владелец протянет заново,
 *     а потерянную привязку сделки к строке — уже ничем.
 */
function formulaColumnsToCopy(formulas, fromColumn, noteColumn) {
  var columns = [];
  for (var i = 0; i < formulas.length; i += 1) {
    var column = fromColumn + i;
    if (column === noteColumn) continue;
    var text = formulas[i];
    if (String(text === null || text === undefined ? '' : text).length === 0) {
      continue;
    }
    columns.push(column);
  }
  return columns;
}

/**
 * Протянуть формулы столбцов ``fromColumn``..конец на созданные строки.
 *
 * Источник — строка НАД первой созданной. Формул там нет (или это заголовок) —
 * ФОРМУЛЫ НЕ ВЫДУМЫВАЮТСЯ: возвращается текст предупреждения, и клиент печатает
 * его в журнал. Сочинённая формула — это чужая модель денег, написанная за
 * владельца, и заметить подмену по виду листа было бы невозможно.
 *
 * КОПИРУЮТСЯ ТОЛЬКО ЯЧЕЙКИ С ФОРМУЛАМИ, И КАЖДАЯ ОТДЕЛЬНО (§1 ТЗ 9.1.2.2).
 * Прежняя редакция звала copyTo ОДИН раз на весь диапазон K..последний столбец.
 * Это дешевле — одно обращение вместо десятка, — но PASTE_FORMULA переносит и
 * ячейки БЕЗ формул, своим значением, а столбец заметок лежит внутри диапазона.
 * Размен сделан осознанно: десяток обращений на пачку против переноса чужих
 * данных в новую строку. Отбор столбцов вынесен в
 * :func:`formulaColumnsToCopy` — его можно проверить отдельно от Google.
 *
 * Возвращает '' при успехе либо текст предупреждения.
 */
function pullFormulasDown(sheet, startRow, count, fromColumn, noteColumn) {
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
  var formulas = sheet.getRange(sourceRow, fromColumn, 1, width).getFormulas()[0];
  var columns = formulaColumnsToCopy(formulas, fromColumn, noteColumn);
  if (columns.length === 0) {
    return 'формулы не протянуты: в строке ' + sourceRow
           + ' столбцы ' + fromColumn + '..' + lastCol + ' без формул';
  }
  for (var i = 0; i < columns.length; i += 1) {
    sheet.getRange(sourceRow, columns[i], 1, 1).copyTo(
      sheet.getRange(startRow, columns[i], count, 1),
      SpreadsheetApp.CopyPasteType.PASTE_FORMULA, false);
  }
  return '';
}
