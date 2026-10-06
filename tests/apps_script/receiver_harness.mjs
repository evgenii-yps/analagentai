/**
 * Стенд приёмника Apps Script (Этап 8.4.1).
 *
 * Прогоняет НАСТОЯЩИЙ файл deploy/apps_script.gs в Node с двойником Google
 * Sheets. Двойник намеренно строг там, где строг Google: setValues бросает ту
 * же ошибку о несовпадении числа колонок, которую владелец увидел на живом
 * прогоне 24.08.2026 15:35 UTC. Без этой строгости стенд ничего не доказывал бы.
 *
 * Это стенд ЛОГИКИ приёмника, а не доказательство поведения на стороне Google:
 * подтверждением служит журнал следующей выгрузки на сервере.
 *
 * ЭТАП 9.1.2. Добавлены сценарии режимов table_append и table_update на
 * двойнике ТОРГОВОГО ЖУРНАЛА — бланка со строкой заголовков, пустыми строками с
 * формулами, блоком «итого:»/«средние:» и «баланс / начало» под ним. Двойник
 * умеет формулы ровно настолько, насколько это нужно приёмнику: хранит текст
 * формулы отдельно от значения и сдвигает номера строк в относительных ссылках
 * при PASTE_FORMULA — иначе проверять протяжку было бы нечем.
 *
 * Запуск: node tests/apps_script/receiver_harness.mjs
 * Код возврата 0 — все сценарии прошли, 1 — есть провалившийся.
 */
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const GS_PATH = join(ROOT, 'deploy', 'apps_script.gs');

/** Двойник листа. Ошибки повторяют формулировки Google. */
function makeSheet(name) {
  return {
    name,
    grid: [],
    // Формулы живут ОТДЕЛЬНО от значений, как в Google: ячейка с формулой
    // отдаёт из getDisplayValues() посчитанное значение, а из getFormulas() —
    // текст. Двойник ничего не считает: приёмнику важно лишь, есть ли формула.
    formulas: new Map(),
    // ЧИСЛОВОЙ ФОРМАТ ЖИВЁТ ОТДЕЛЬНО ОТ ЗНАЧЕНИЯ, как в Google (Этап 9.2
    // §6.1). Без него стенд не смог бы проверить починку столбца времени —
    // а именно на нём владелец и поймал ошибку.
    numberFormats: new Map(),
    maxColumns: 26,          // столько колонок у нового листа Google Таблицы
    maxRows: 1000,           // столько строк у листа владельца (Этап 9.5.2)
    // ПРОВЕРКИ ДАННЫХ живут отдельно от значений, как в Google (Этап 9.5.2): запись
    // из скрипта их не применяет и не снимает. Ключ — «строка:столбец», значение —
    // список допустимых текстов. Двойник хранит их, чтобы стенд мог убедиться, что
    // приёмник после записи и очистки не тронул ни одной.
    validations: new Map(),
    frozen: 0,
    clear() {
      this.grid = []; this.formulas.clear(); this.numberFormats.clear();
    },
    getLastRow() { return this.grid.length; },
    getLastColumn() {
      let width = 0;
      for (const line of this.grid) if (line && line.length > width) width = line.length;
      for (const key of this.formulas.keys()) {
        const col = Number(key.split(':')[1]);
        if (col > width) width = col;
      }
      return width;
    },
    getMaxColumns() { return this.maxColumns; },
    getMaxRows() { return this.maxRows; },
    getName() { return this.name; },
    insertColumnsAfter(after, howMany) { this.maxColumns += howMany; },
    setFrozenRows(n) { this.frozen = n; },
    key(row, col) { return `${row}:${col}`; },
    getFormulaAt(row, col) { return this.formulas.get(this.key(row, col)) || ''; },
    getFormatAt(row, col) {
      // Google на ячейке без заданного формата отдаёт 'Automatic', а не пустоту.
      return this.numberFormats.get(this.key(row, col)) || 'Automatic';
    },
    setFormatAt(row, col, text) { this.numberFormats.set(this.key(row, col), text); },
    setFormulaAt(row, col, text) {
      if (text) this.formulas.set(this.key(row, col), text);
      else this.formulas.delete(this.key(row, col));
    },
    getCell(row, col) {
      const line = this.grid[row - 1];
      const value = line ? line[col - 1] : undefined;
      return value === undefined || value === null ? '' : value;
    },
    setCell(row, col, value) {
      while (this.grid.length < row) this.grid.push([]);
      const line = this.grid[row - 1];
      while (line.length < col) line.push('');
      line[col - 1] = value;
    },
    /** Вставка строк ПЕРЕД указанной: всё ниже съезжает, формулы тоже. */
    insertRowsBefore(before, howMany) {
      for (let i = 0; i < howMany; i += 1) this.grid.splice(before - 1, 0, []);
      const moved = new Map();
      for (const [key, text] of this.formulas.entries()) {
        const [row, col] = key.split(':').map(Number);
        const target = row >= before ? row + howMany : row;
        moved.set(`${target}:${col}`, text);
      }
      this.formulas = moved;
      // ФОРМАТ НОВОЙ СТРОКИ НАСЛЕДУЕТСЯ ОТ СТРОКИ ВЫШЕ — так делает Google при
      // insertRowsBefore, и ровно поэтому переполняющийся формат столбца
      // времени приезжал в каждую созданную строку. Двойник, который этого не
      // делает, объявил бы дефект несуществующим.
      const movedFormats = new Map();
      for (const [key, text] of this.numberFormats.entries()) {
        const [row, col] = key.split(':').map(Number);
        const target = row >= before ? row + howMany : row;
        movedFormats.set(`${target}:${col}`, text);
      }
      for (let i = 0; i < howMany; i += 1) {
        for (const [key, text] of this.numberFormats.entries()) {
          const [row, col] = key.split(':').map(Number);
          if (row === before - 1) movedFormats.set(`${before + i}:${col}`, text);
        }
      }
      this.numberFormats = movedFormats;
      this.maxRows += howMany;
    },
    appendRow(values) {
      if (values.length > this.maxColumns) this.maxColumns = values.length;
      this.grid.push(values.slice());
    },
    getRange(row, col, numRows, numCols) {
      const self = this;
      // A1-нотация (Этап 9.5.2): «A3» или «A6:D40». Столбцы AA.. не нужны ни одному
      // сценарию, но разбор честный — по буквам, а не по первой.
      if (typeof row === 'string') {
        const m = /^([A-Z]+)(\d+)(?::([A-Z]+)(\d+))?$/.exec(row);
        if (!m) throw new Error(`Der Bereich wurde nicht gefunden: ${row}`);
        const letters = (t) => [...t].reduce((n, ch) => n * 26 + ch.charCodeAt(0) - 64, 0);
        const c1 = letters(m[1]); const r1 = Number(m[2]);
        const c2 = m[3] ? letters(m[3]) : c1; const r2 = m[4] ? Number(m[4]) : r1;
        return self.getRange(r1, c1, r2 - r1 + 1, c2 - c1 + 1);
      }
      if (col + numCols - 1 > self.maxColumns) {
        throw new Error('Those columns are out of bounds.');
      }
      return {
        box: { row, col, numRows, numCols },
        getNumRows() { return numRows; },
        getNumColumns() { return numCols; },
        getLastRow() { return row + numRows - 1; },
        getLastColumn() { return col + numCols - 1; },
        // clearContent снимает ЗНАЧЕНИЯ и формулы, но не формат и не проверки данных.
        clearContent() {
          for (let r = 0; r < numRows; r += 1) {
            for (let c = 0; c < numCols; c += 1) {
              if (self.grid[row + r - 1] && self.grid[row + r - 1][col + c - 1] !== undefined) {
                self.grid[row + r - 1][col + c - 1] = '';
              }
              self.formulas.delete(self.key(row + r, col + c));
            }
          }
        },
        // Любой другой способ очистки двойник не поддерживает: приёмник 9.5.2 обязан
        // звать именно clearContent, и вызов clear/clearFormat тут — провал сценария.
        clear() { throw new Error('приёмник вызвал clear() вместо clearContent()'); },
        clearFormat() { throw new Error('приёмник вызвал clearFormat()'); },
        setDataValidation() { throw new Error('приёмник тронул проверку данных'); },
        getValue() { return self.getCell(row, col); },
        setValue(value) { self.setCell(row, col, value); },
        getNumberFormat() { return self.getFormatAt(row, col); },
        setNumberFormat(text) { self.setFormatAt(row, col, text); },
        getDisplayValues() {
          const out = [];
          for (let r = 0; r < numRows; r += 1) {
            const line = [];
            for (let c = 0; c < numCols; c += 1) {
              // Ячейка с формулой отображает ЗНАЧЕНИЕ, а не текст формулы —
              // как в Google. Двойник ничего не считает и отдаёт пусто:
              // приёмник по столбцу A формул не ищет, он ищет данные.
              line.push(String(self.getCell(row + r, col + c)));
            }
            out.push(line);
          }
          return out;
        },
        getFormulas() {
          const out = [];
          for (let r = 0; r < numRows; r += 1) {
            const line = [];
            for (let c = 0; c < numCols; c += 1) {
              line.push(self.getFormulaAt(row + r, col + c));
            }
            out.push(line);
          }
          return out;
        },
        copyTo(target, type) {
          if (type !== 'PASTE_FORMULA') {
            throw new Error(`двойник поддерживает только PASTE_FORMULA, а не ${type}`);
          }
          const to = target.box;
          for (let r = 0; r < to.numRows; r += 1) {
            for (let c = 0; c < to.numCols; c += 1) {
              // Источник шириной в одну строку размножается вниз — так же,
              // как это делает Google при copyTo на диапазон большей высоты.
              const text = self.getFormulaAt(row + (r % numRows), col + c);
              if (!text) {
                // ЭТАП 9.1.2.2. PASTE_FORMULA ПЕРЕНОСИТ И ЛИТЕРАЛЫ. Ячейка
                // без формулы копируется своим ЗНАЧЕНИЕМ — так делает Google,
                // и прежний двойник этого НЕ делал: он молча пропускал такие
                // ячейки. Из-за этого стенд Этапа 9.1.2 не увидел дефекта,
                // ради которого написан Этап 9.1.2.2 — протяжка формул из
                // строки выше затирала заметку в столбце T, лежащем ВНУТРИ
                // копируемого диапазона K..последний. Двойник, который мягче
                // настоящего Google, доказывает не работоспособность кода, а
                // собственную снисходительность.
                self.setCell(to.row + r, to.col + c,
                             self.getCell(row + (r % numRows), col + c));
                continue;
              }
              // Относительные ссылки сдвигаются на разницу строк. Без сдвига
              // протянутая формула повторяла бы чужую строку, и проверка
              // протяжки ничего не проверяла бы.
              const shift = (to.row + r) - (row + (r % numRows));
              const moved = text.replace(/(\$?)([A-Z]+)(\$?)(\d+)/g,
                (whole, dollarCol, letters, dollarRow, digits) => (
                  dollarRow ? whole
                            : `${dollarCol}${letters}${Number(digits) + shift}`
                ));
              self.setFormulaAt(to.row + r, to.col + c, moved);
            }
          }
        },
        setValues(values) {
          if (values.length !== numRows) {
            throw new Error(
              'Die Zeilenzahl in den Daten stimmt nicht mit der Zeilenzahl im '
              + `Bereich überein. In den Daten sind es ${values.length}, im `
              + `Bereich jedoch ${numRows}.`);
          }
          for (const value of values) {
            if (value.length !== numCols) {
              throw new Error(
                'Die Spaltenzahl in den Daten stimmt nicht mit der Spaltenzahl '
                + `im Bereich überein. In den Daten sind es ${value.length}, im `
                + `Bereich jedoch ${numCols}.`);
            }
          }
          // Запись идёт СО СМЕЩЕНИЕМ ПО СТОЛБЦУ, а не заменой строки целиком:
          // диапазон закрытия начинается с восьмого столбца, и замена строки
          // затирала бы столбцы открытия. Двойник, который так делает, «ловил»
          // бы несуществующие дефекты приёмника.
          for (let i = 0; i < numRows; i += 1) {
            for (let c = 0; c < numCols; c += 1) {
              self.setCell(row + i, col + c, values[i][c]);
            }
          }
        },
      };
    },
  };
}

function makeContext() {
  const sheets = new Map();
  const spreadsheet = {
    getSheetByName: (n) => sheets.get(n) || null,
    insertSheet: (n) => { const s = makeSheet(n); sheets.set(n, s); return s; },
    getSheets: () => [...sheets.values()],
  };
  const sandbox = {
    sheets,
    SpreadsheetApp: {
      getActiveSpreadsheet: () => spreadsheet,
      CopyPasteType: { PASTE_FORMULA: 'PASTE_FORMULA' },
    },
    ContentService: {
      MimeType: { JSON: 'application/json' },
      createTextOutput: (text) => ({ text, setMimeType() { return this; } }),
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(readFileSync(GS_PATH, 'utf8'), sandbox, { filename: GS_PATH });
  // Объявления const в скрипте vm живут в лексической области, а не в глобальном
  // объекте, поэтому секрет читаем вычислением выражения в том же контексте.
  sandbox.SECRET = vm.runInContext('SECRET', sandbox);
  // Версия приёмника читается ИЗ САМОГО ФАЙЛА, а не повторяется здесь числом:
  // повторённая, она превращала бы каждый подъём версии в правку стенда, и
  // однажды стенд проверял бы версию, которой уже нет.
  sandbox.RECEIVER_VERSION = vm.runInContext('RECEIVER_VERSION', sandbox);
  sandbox.elapsedTimeFormat = vm.runInContext('elapsedTimeFormat', sandbox);
  return sandbox;
}

function post(ctx, body) {
  const out = ctx.doPost({ postData: { contents: JSON.stringify(body) } });
  return JSON.parse(out.text);
}

/** Прежняя редакция строки 36: ширина берётся у ПЕРВОЙ строки пачки. */
function postOldWay(ctx, body) {
  const sheet = ctx.SpreadsheetApp.getActiveSpreadsheet().insertSheet(body.sheet);
  if (body.header && sheet.getLastRow() === 0) sheet.appendRow(body.header);
  const rows = body.rows || [];
  sheet.getRange(sheet.getLastRow() + 1, 1, rows.length, rows[0].length)
       .setValues(rows);
}

const DISCLAIMER = ['ВНИМАНИЕ: пять токенов НЕ дают пятикратного роста мощности.'];
const HEADER15 = Array.from({ length: 15 }, (_, i) => `колонка_${i + 1}`);
const row15 = (tag) => Array.from({ length: 15 }, (_, i) => `${tag}_${i + 1}`);

let failed = 0;
function check(name, fn) {
  try {
    fn();
    console.log(`  ok   ${name}`);
  } catch (err) {
    failed += 1;
    console.log(`  FAIL ${name}: ${err.message}`);
  }
}
function assert(cond, msg) { if (!cond) throw new Error(msg); }

console.log('Стенд приёмника Apps Script (Этап 8.4.1)');

check('оговорка шириной 1 и строки шириной 15 в одной пачке — запись проходит', () => {
  const ctx = makeContext();
  const res = post(ctx, {
    secret: ctx.SECRET, sheet: 'Независимые окна', mode: 'replace',
    header: HEADER15, rows: [DISCLAIMER, row15('a'), row15('b'), row15('c')],
  });
  assert(res.ok === true, `ok=false: ${res.error}`);
  assert(res.width === 15, `ширина ${res.width}, ожидалась 15`);
  const grid = ctx.sheets.get('Независимые окна').grid;
  assert(grid.length === 5, `строк ${grid.length}, ожидалось 5 (заголовок + 4)`);
  assert(grid.every((r) => r.length === 15), 'не все строки листа шириной 15');
  assert(grid[1][0] === DISCLAIMER[0], 'оговорка не первой строкой данных');
  assert(grid[1].slice(1).every((c) => c === ''), 'хвост оговорки не пустой');
});

check('строки РАЗНОЙ длины вперемешку — запись проходит, ширина по максимуму', () => {
  const ctx = makeContext();
  const res = post(ctx, {
    secret: ctx.SECRET, sheet: 'Разное', mode: 'replace',
    header: ['a', 'b'], rows: [['одна'], ['две', 'штуки'], ['три', 'штуки', 'ровно']],
  });
  assert(res.ok === true, `ok=false: ${res.error}`);
  assert(res.width === 3, `ширина ${res.width}, ожидалась 3`);
  const grid = ctx.sheets.get('Разное').grid;
  assert(grid.every((r) => r.length === 3), 'строки листа не выровнены по 3');
});

check('пачка шире 26 колонок по умолчанию — лист расширяется', () => {
  const ctx = makeContext();
  const wide = Array.from({ length: 33 }, (_, i) => `c${i}`);
  const res = post(ctx, {
    secret: ctx.SECRET, sheet: 'Сигналы', mode: 'replace',
    header: wide, rows: [wide, ['коротко']],
  });
  assert(res.ok === true, `ok=false: ${res.error}`);
  assert(res.width === 33, `ширина ${res.width}, ожидалась 33`);
  assert(ctx.sheets.get('Сигналы').getMaxColumns() >= 33, 'лист не расширен');
});

check('пустая пачка с заголовком — не отказ', () => {
  const ctx = makeContext();
  const res = post(ctx, {
    secret: ctx.SECRET, sheet: 'Пусто', mode: 'replace',
    header: HEADER15, rows: [],
  });
  assert(res.ok === true, `ok=false: ${res.error}`);
  assert(res.inserted === 0, `inserted=${res.inserted}`);
});

check('чужой секрет отвергается', () => {
  const ctx = makeContext();
  const res = post(ctx, { secret: 'не тот', sheet: 'X', mode: 'replace', rows: [] });
  assert(res.ok === false && res.error === 'forbidden', 'секрет не проверен');
});

check('приёмник сообщает свою версию', () => {
  const ctx = makeContext();
  const res = post(ctx, {
    secret: ctx.SECRET, sheet: 'V', mode: 'replace', header: ['a'], rows: [['x']],
  });
  assert(typeof res.version === 'string' && res.version.length > 0,
         'версия не возвращена');
});

check('выровненная пачка принимается и ПРЕЖНЕЙ редакцией приёмника', () => {
  // Приёмник живёт на стороне Google и обновляется вручную. До обновления
  // работает старая редакция, берущая ширину у первой строки. Отправитель уже
  // выравнивает пачку, поэтому запись обязана проходить и на ней.
  const ctx = makeContext();
  const width = HEADER15.length;
  const padTo = (row) => row.concat(Array(width - row.length).fill(''));
  postOldWay(ctx, {
    secret: ctx.SECRET, sheet: 'Старый приёмник', mode: 'replace',
    header: HEADER15, rows: [padTo(DISCLAIMER), row15('a'), row15('b')],
  });
  const grid = ctx.sheets.get('Старый приёмник').grid;
  assert(grid.length === 4, `строк ${grid.length}, ожидалось 4`);
  assert(grid.every((r) => r.length === 15), 'строки листа не шириной 15');
  assert(grid[1][0] === DISCLAIMER[0], 'оговорка не первой строкой данных');
});

check('КОНТРОЛЬ: прежняя редакция на той же пачке падает с той же ошибкой', () => {
  const ctx = makeContext();
  let message = '';
  try {
    postOldWay(ctx, {
      secret: ctx.SECRET, sheet: 'Контроль', mode: 'replace',
      header: HEADER15, rows: [DISCLAIMER, row15('a')],
    });
  } catch (err) {
    message = err.message;
  }
  assert(message.includes('In den Daten sind es 15, im Bereich jedoch 1'),
         `ожидалась исходная ошибка, получено: «${message}»`);
});


// --- Этап 9.1.2: торговый журнал ------------------------------------------

const TRADES = 'торговля тест апи окх чтение';
const NOTE_COL = 20;
const FORMULA_FROM = 11;

/**
 * Двойник ЖИВОГО торгового журнала: бланк, а не журнал.
 *
 * Строка 1 — заголовки; строки 2..(1+blank) — ПУСТЫЕ строки с формулами в
 * K..S; ниже «итого:» и «средние:», ещё ниже «баланс / начало». Именно так лист
 * и устроен, и именно поэтому appendRow тут не годится: он положил бы строку
 * ниже слова «начало», вне таблицы и вне всех формул.
 */
function makeTradesSheet(ctx, { blank = 3, filled = 0 } = {}) {
  const sheet = ctx.SpreadsheetApp.getActiveSpreadsheet().insertSheet(TRADES);
  const header = ['дата вход', 'время сигнала', 'токен', 'сигнал', '',
                  'цена открытия', 'вход - объем', 'дата выход', 'время выхода',
                  'цена закрытия'];
  for (let c = 0; c < header.length; c += 1) sheet.setCell(1, c + 1, header[c]);
  sheet.setCell(1, NOTE_COL, 'заметка');
  const lastBlank = 1 + blank;
  for (let r = 2; r <= lastBlank; r += 1) {
    for (let c = FORMULA_FROM; c <= 19; c += 1) sheet.setFormulaAt(r, c, `=F${r}*2`);
  }
  for (let r = 2; r <= 1 + filled; r += 1) {
    ['31.08.2026', '10:00:00', 'BTC', 'покупать', '', 100, 2]
      .forEach((v, i) => sheet.setCell(r, i + 1, v));
    sheet.setCell(r, NOTE_COL, `[поз. ${r}] старая сделка`);
  }
  sheet.setCell(lastBlank + 1, 1, 'итого:');
  sheet.setCell(lastBlank + 2, 1, 'средние:');
  sheet.setCell(lastBlank + 4, 1, 'баланс / начало');
  return sheet;
}

const openRow = (token, price) =>
  ['31.08.2026', '20:34:12', token, 'покупать', '', price, 2];


check('table_append: строка ложится В ТАБЛИЦУ, а не под «баланс / начало»', () => {
  const ctx = makeContext();
  const sheet = makeTradesSheet(ctx, { blank: 3, filled: 1 });
  const res = post(ctx, {
    secret: ctx.SECRET, sheet: TRADES, mode: 'table_append',
    rows: [openRow('XRP', 1.4161)], notes: ['[поз. 77] цель 1.43'],
    noteColumn: NOTE_COL, totalsMarker: 'итого:', formulaFromColumn: FORMULA_FROM,
  });
  assert(res.ok === true, `ok=false: ${res.error}`);
  assert(res.startRow === 3, `строка ${res.startRow}, ожидалась 3`);
  assert(sheet.getCell(3, 3) === 'XRP', 'токен не записан');
  assert(sheet.getCell(3, 6) === 1.4161, 'цена открытия не записана');
  assert(sheet.getCell(3, 7) === 2, 'объём не записан');
  assert(sheet.getCell(3, 5) === '', 'столбец-разделитель E заполнен');
  assert(sheet.getCell(3, NOTE_COL) === '[поз. 77] цель 1.43', 'заметка не записана');
  // Столбцы H..J при ОТКРЫТИИ пусты: сделка ещё идёт.
  assert(sheet.getCell(3, 8) === '' && sheet.getCell(3, 10) === '',
         'при открытии заполнены столбцы закрытия');
  // Блок итогов остался НИЖЕ таблицы и не затёрт.
  assert(sheet.getCell(5, 1) === 'итого:', 'строка итогов уехала или затёрта');
});

check('table_append: свободных строк не хватает — вставляются перед итогами', () => {
  const ctx = makeContext();
  const sheet = makeTradesSheet(ctx, { blank: 2, filled: 2 });
  // Свободных строк нет вовсе: обе заняты, следом «итого:» в строке 4.
  assert(sheet.getCell(4, 1) === 'итого:', 'стенд собран не так, как задумано');
  const res = post(ctx, {
    secret: ctx.SECRET, sheet: TRADES, mode: 'table_append',
    rows: [openRow('SOL', 200.5), openRow('DOGE', 0.2143)],
    notes: ['[поз. 78]', '[поз. 79]'],
    noteColumn: NOTE_COL, totalsMarker: 'итого:', formulaFromColumn: FORMULA_FROM,
  });
  assert(res.ok === true, `ok=false: ${res.error}`);
  assert(res.startRow === 4, `строка ${res.startRow}, ожидалась 4`);
  assert(sheet.getCell(4, 3) === 'SOL' && sheet.getCell(5, 3) === 'DOGE',
         'строки легли не подряд');
  // Итоги СЪЕХАЛИ вниз, а не затёрты — ради этого вставка и делается перед ними.
  assert(sheet.getCell(6, 1) === 'итого:',
         `«итого:» оказалось в строке ${sheet.getCell(6, 1) ? 6 : '?'}, а не 6`);
  assert(sheet.getCell(9, 1) === 'баланс / начало',
         'блок «баланс / начало» не съехал вместе с итогами');
});

check('table_append: формулы протянуты из строки выше со сдвигом ссылок', () => {
  const ctx = makeContext();
  const sheet = makeTradesSheet(ctx, { blank: 3, filled: 1 });
  const res = post(ctx, {
    secret: ctx.SECRET, sheet: TRADES, mode: 'table_append',
    rows: [openRow('ETH', 3000)], notes: ['[поз. 80]'],
    noteColumn: NOTE_COL, totalsMarker: 'итого:', formulaFromColumn: FORMULA_FROM,
  });
  assert(res.ok === true, `ok=false: ${res.error}`);
  assert(res.warning === undefined, `неожиданное предупреждение: ${res.warning}`);
  assert(sheet.getFormulaAt(3, FORMULA_FROM) === '=F3*2',
         `формула K3 «${sheet.getFormulaAt(3, FORMULA_FROM)}», ожидалась «=F3*2»`);
  assert(sheet.getFormulaAt(3, 19) === '=F3*2', 'формула S3 не протянута');
});

check('table_append: протягивать неоткуда — warning, а не выдуманная формула', () => {
  const ctx = makeContext();
  // Лист без единой формулы: над первой созданной строкой только заголовок.
  const sheet = ctx.SpreadsheetApp.getActiveSpreadsheet().insertSheet(TRADES);
  ['дата вход', 'время сигнала', 'токен'].forEach((v, i) => sheet.setCell(1, i + 1, v));
  sheet.setCell(1, NOTE_COL, 'заметка');
  sheet.setCell(2, 1, 'итого:');
  const res = post(ctx, {
    secret: ctx.SECRET, sheet: TRADES, mode: 'table_append',
    rows: [openRow('BTC', 77000)], notes: ['[поз. 81]'],
    noteColumn: NOTE_COL, totalsMarker: 'итого:', formulaFromColumn: FORMULA_FROM,
  });
  assert(res.ok === true, `ok=false: ${res.error}`);
  assert(typeof res.warning === 'string' && res.warning.length > 0,
         'предупреждения нет — значит формулу могли выдумать');
  assert(sheet.getCell(2, 3) === 'BTC', 'строка всё-таки не записана');
});

check('table_append: листа нет — отказ, лист не создаётся', () => {
  const ctx = makeContext();
  const res = post(ctx, {
    secret: ctx.SECRET, sheet: 'нет такого листа', mode: 'table_append',
    rows: [openRow('BTC', 77000)], notes: ['[поз. 82]'],
    noteColumn: NOTE_COL, totalsMarker: 'итого:', formulaFromColumn: FORMULA_FROM,
  });
  assert(res.ok === false, 'отказа не было');
  assert(/лист не найден/.test(res.error), `неожиданная причина: ${res.error}`);
  assert(ctx.sheets.get('нет такого листа') === undefined, 'лист создан');
});

check('table_update: закрытие дописано В ТУ ЖЕ строку по метке', () => {
  const ctx = makeContext();
  const sheet = makeTradesSheet(ctx, { blank: 4, filled: 0 });
  post(ctx, {
    secret: ctx.SECRET, sheet: TRADES, mode: 'table_append',
    rows: [openRow('BTC', 77000), openRow('XRP', 1.4161)],
    notes: ['[поз. 90] цель', '[поз. 91] цель'],
    noteColumn: NOTE_COL, totalsMarker: 'итого:', formulaFromColumn: FORMULA_FROM,
  });
  const before = sheet.getFormulaAt(3, FORMULA_FROM);
  const res = post(ctx, {
    secret: ctx.SECRET, sheet: TRADES, mode: 'table_update', noteColumn: NOTE_COL,
    updates: [{
      marker: '[поз. 91]', startColumn: 8,
      values: ['31.08.2026', '21:33:00', 1.43],
      noteAppend: ' · цель достигнута · итог системы +0.76%',
    }],
  });
  assert(res.ok === true, `ok=false: ${res.error}`);
  assert(res.updated === 1, `updated=${res.updated}`);
  assert(res.notFound === undefined, `notFound=${JSON.stringify(res.notFound)}`);
  // Новая строка НЕ создана: та же самая достроена.
  assert(sheet.getCell(3, 3) === 'XRP', 'дозапись ушла не в ту строку');
  assert(sheet.getCell(3, 8) === '31.08.2026', 'дата выхода не записана');
  assert(sheet.getCell(3, 10) === 1.43, 'цена закрытия не записана');
  assert(/цель достигнута/.test(sheet.getCell(3, NOTE_COL)), 'заметка не обновлена');
  // Соседняя строка не тронута, формулы целы.
  assert(sheet.getCell(2, 3) === 'BTC', 'затронута чужая строка');
  assert(sheet.getFormulaAt(3, FORMULA_FROM) === before, 'дозапись стёрла формулы');
});

check('table_update: метки нет — строка НЕ угадывается, метка уходит в notFound', () => {
  const ctx = makeContext();
  const sheet = makeTradesSheet(ctx, { blank: 3, filled: 1 });
  const res = post(ctx, {
    secret: ctx.SECRET, sheet: TRADES, mode: 'table_update', noteColumn: NOTE_COL,
    updates: [
      { marker: '[поз. 2]', startColumn: 8, values: ['a', 'b', 1], note: 'есть' },
      { marker: '[поз. 999]', startColumn: 8, values: ['x', 'y', 2], note: 'нет' },
    ],
  });
  assert(res.ok === true, 'ok=false при частично найденных метках');
  assert(res.updated === 1, `updated=${res.updated}, ожидался 1`);
  assert(JSON.stringify(res.notFound) === JSON.stringify(['[поз. 999]']),
         `notFound=${JSON.stringify(res.notFound)}`);
  // Ненайденная метка не привела к записи НИ В ОДНУ строку.
  assert(sheet.getCell(3, 8) === '' && sheet.getCell(4, 8) === '',
         'запись ушла в угаданную строку');
});

check('version: приёмник называет версию и НИЧЕГО не пишет', () => {
  // Клиент спрашивает версию ПЕРЕД первой записью. Вопрос обязан быть
  // безвредным: если он что-нибудь меняет в листе, им нельзя пользоваться
  // именно тогда, когда он нужнее всего — перед первой записью в чужой
  // рабочий документ.
  const ctx = makeContext();
  const sheet = makeTradesSheet(ctx, { blank: 3, filled: 1 });
  const before = JSON.stringify(sheet.grid);
  const res = post(ctx, { secret: ctx.SECRET, sheet: TRADES, mode: 'version' });
  assert(res.ok === true, `ok=false: ${res.error}`);
  assert(res.version === ctx.RECEIVER_VERSION, `version=${res.version}`);
  assert(JSON.stringify(sheet.grid) === before, 'вопрос о версии изменил лист');
  assert(res.inserted === undefined && res.updated === undefined,
         'вопрос о версии отчитался о записи');
});

check('version: чужой секрет отвергается и здесь', () => {
  const ctx = makeContext();
  const res = post(ctx, { secret: 'не тот', sheet: TRADES, mode: 'version' });
  assert(res.ok === false && res.error === 'forbidden', 'секрет не проверен');
});

check('table_update: ручной текст владельца СОХРАНЯЕТСЯ, хвост в конце', () => {
  // Столбец заметок — единственное место строки, куда человек пишет руками.
  // Пока сделка шла, владелец мог занести туда своё наблюдение; замена ячейки
  // целиком стёрла бы его молча и безвозвратно.
  const ctx = makeContext();
  const sheet = makeTradesSheet(ctx, { blank: 4, filled: 0 });
  post(ctx, {
    secret: ctx.SECRET, sheet: TRADES, mode: 'table_append',
    rows: [openRow('XRP', 1.4161)], notes: ['[поз. 91] цель 1.43'],
    noteColumn: NOTE_COL, totalsMarker: 'итого:', formulaFromColumn: FORMULA_FROM,
  });
  // Владелец дописал своё, пока сделка шла.
  sheet.setCell(2, NOTE_COL, '[поз. 91] цель 1.43 — ЖДУ ОТСКОКА, следить');

  const tail = ' · цель достигнута · итог системы +0.76%';
  const res = post(ctx, {
    secret: ctx.SECRET, sheet: TRADES, mode: 'table_update', noteColumn: NOTE_COL,
    updates: [{
      marker: '[поз. 91]', startColumn: 8,
      values: ['31.08.2026', '21:33:00', 1.43], noteAppend: tail,
    }],
  });
  assert(res.ok === true && res.updated === 1, `updated=${res.updated}`);
  const note = String(sheet.getCell(2, NOTE_COL));
  assert(note.indexOf('ЖДУ ОТСКОКА, следить') >= 0,
         `текст владельца стёрт: «${note}»`);
  assert(note.endsWith(tail), `хвост не в конце: «${note}»`);
  assert(note.indexOf('[поз. 91]') === 0, 'метка перестала быть первой');
});

check('table_update: повторный тот же хвост не удваивает текст', () => {
  // Запись могла удаться, а ответ — не дойти (обрыв сети), и следующий прогон
  // пришлёт тот же хвост. Заметка с дважды повторённым итогом выглядела бы как
  // две сделки в одной строке.
  const ctx = makeContext();
  const sheet = makeTradesSheet(ctx, { blank: 4, filled: 0 });
  post(ctx, {
    secret: ctx.SECRET, sheet: TRADES, mode: 'table_append',
    rows: [openRow('XRP', 1.4161)], notes: ['[поз. 92] цель'],
    noteColumn: NOTE_COL, totalsMarker: 'итого:', formulaFromColumn: FORMULA_FROM,
  });
  const tail = ' · цель достигнута · итог системы +0.76%';
  const body = {
    secret: ctx.SECRET, sheet: TRADES, mode: 'table_update', noteColumn: NOTE_COL,
    updates: [{ marker: '[поз. 92]', startColumn: 8,
                values: ['31.08.2026', '21:33:00', 1.43], noteAppend: tail }],
  };
  post(ctx, body);
  const once = String(sheet.getCell(2, NOTE_COL));
  post(ctx, body);
  const twice = String(sheet.getCell(2, NOTE_COL));
  assert(once === twice, `хвост дописан дважды: «${twice}»`);
  assert(twice.split('цель достигнута').length - 1 === 1,
         `причина выхода встречается больше одного раза: «${twice}»`);
});

// --- Этап 9.1.2.2: заметка не затирается, неоднозначная метка не пишется ----

check('9.1.2.2: заметка НОВОЙ строки своя, а не унаследованная от строки выше', () => {
  // Тот самый дефект, найденный на боевом листе 31.08.2026: строка выше несёт
  // заметку [поз. 10], протяжка формул тянет вниз ВЕСЬ диапазон K..последний
  // столбец — а столбец заметок T лежит внутри него.
  const ctx = makeContext();
  const sheet = makeTradesSheet(ctx, { blank: 4, filled: 1 });
  sheet.setCell(2, NOTE_COL, '[поз. 10] цель 2535.33 · сигнал #73875');
  const res = post(ctx, {
    secret: ctx.SECRET, sheet: TRADES, mode: 'table_append',
    rows: [openRow('ETH', 2472.8)], notes: ['[поз. 11] цель 2535.33 · сигнал #73999'],
    noteColumn: NOTE_COL, totalsMarker: 'итого:', formulaFromColumn: FORMULA_FROM,
  });
  assert(res.ok === true, `ok=false: ${res.error}`);
  assert(res.startRow === 3, `строка ${res.startRow}, ожидалась 3`);
  assert(sheet.getCell(3, NOTE_COL) === '[поз. 11] цель 2535.33 · сигнал #73999',
         `заметка чужая: «${sheet.getCell(3, NOTE_COL)}»`);
  // Формулы при этом протянуты: исправление не отменило протяжку.
  assert(sheet.getFormulaAt(3, FORMULA_FROM) === '=F3*2',
         `формула K3 «${sheet.getFormulaAt(3, FORMULA_FROM)}»`);
});

check('9.1.2.2: литерал в столбце БЕЗ формулы вниз не переносится', () => {
  // Заметка — самый заметный случай, но не единственный: любой набранный
  // руками комментарий или число переехало бы в новую строку точно так же.
  const ctx = makeContext();
  const sheet = makeTradesSheet(ctx, { blank: 4, filled: 1 });
  sheet.setFormulaAt(2, 12, '');            // столбец L формулы лишён
  sheet.setCell(2, 12, 'пометка владельца'); // и содержит литерал
  const res = post(ctx, {
    secret: ctx.SECRET, sheet: TRADES, mode: 'table_append',
    rows: [openRow('SOL', 105.35)], notes: ['[поз. 12]'],
    noteColumn: NOTE_COL, totalsMarker: 'итого:', formulaFromColumn: FORMULA_FROM,
  });
  assert(res.ok === true, `ok=false: ${res.error}`);
  assert(sheet.getCell(3, 12) === '',
         `литерал перенесён вниз: «${sheet.getCell(3, 12)}»`);
  assert(sheet.getFormulaAt(3, FORMULA_FROM) === '=F3*2', 'формула не протянута');
});

check('9.1.2.2: формула в столбце заметок вниз НЕ протягивается', () => {
  // Второе ограждение поверх первого: заметка — ключ поиска строки, и её
  // потеря стоит дороже потери формулы.
  const ctx = makeContext();
  const sheet = makeTradesSheet(ctx, { blank: 4, filled: 1 });
  sheet.setFormulaAt(2, NOTE_COL, '=A2');
  const res = post(ctx, {
    secret: ctx.SECRET, sheet: TRADES, mode: 'table_append',
    rows: [openRow('BTC', 78080.6)], notes: ['[поз. 13] цель'],
    noteColumn: NOTE_COL, totalsMarker: 'итого:', formulaFromColumn: FORMULA_FROM,
  });
  assert(res.ok === true, `ok=false: ${res.error}`);
  assert(sheet.getFormulaAt(3, NOTE_COL) === '',
         `в столбец заметок протянута формула «${sheet.getFormulaAt(3, NOTE_COL)}»`);
  assert(sheet.getCell(3, NOTE_COL) === '[поз. 13] цель', 'заметка не записана');
});

check('9.1.2.2: table_update при ДВУХ строках с меткой не пишет ничего', () => {
  const ctx = makeContext();
  const sheet = makeTradesSheet(ctx, { blank: 5, filled: 0 });
  // Две строки с одной и той же меткой — ровно то, что вышло на боевом листе.
  sheet.setCell(2, 3, 'ETH'); sheet.setCell(2, NOTE_COL, '[поз. 10] цель');
  sheet.setCell(3, 3, 'ETH'); sheet.setCell(3, NOTE_COL, '[поз. 10] цель');
  const res = post(ctx, {
    secret: ctx.SECRET, sheet: TRADES, mode: 'table_update', noteColumn: NOTE_COL,
    updates: [{ marker: '[поз. 10]', startColumn: 8,
                values: ['31.08.2026', '2:24:00', 2448.07],
                noteAppend: ' · сработал предел убытка' }],
  });
  assert(res.ok === true, `ok=false: ${res.error}`);
  assert(res.updated === 0, `updated=${res.updated}, ожидался 0`);
  assert(res.notFound === undefined, 'неоднозначная метка попала в notFound');
  assert(JSON.stringify(res.ambiguous) === JSON.stringify([{ marker: '[поз. 10]', rows: [2, 3] }]),
         `ambiguous=${JSON.stringify(res.ambiguous)}`);
  assert(sheet.getCell(2, 8) === '' && sheet.getCell(3, 8) === '',
         'дозапись всё-таки состоялась');
  assert(String(sheet.getCell(2, NOTE_COL)).indexOf('предел') < 0,
         'заметка всё-таки дописана');
});

check('9.1.2.2: одна метка неоднозначна — остальные пачки дозаписываются', () => {
  const ctx = makeContext();
  const sheet = makeTradesSheet(ctx, { blank: 5, filled: 0 });
  sheet.setCell(2, 3, 'ETH'); sheet.setCell(2, NOTE_COL, '[поз. 10] цель');
  sheet.setCell(3, 3, 'ETH'); sheet.setCell(3, NOTE_COL, '[поз. 10] цель');
  sheet.setCell(4, 3, 'XRP'); sheet.setCell(4, NOTE_COL, '[поз. 11] цель');
  const res = post(ctx, {
    secret: ctx.SECRET, sheet: TRADES, mode: 'table_update', noteColumn: NOTE_COL,
    updates: [
      { marker: '[поз. 10]', startColumn: 8, values: ['a', 'b', 1] },
      { marker: '[поз. 11]', startColumn: 8, values: ['31.08.2026', '21:31:00', 1.43] },
    ],
  });
  assert(res.ok === true, `ok=false: ${res.error}`);
  assert(res.updated === 1, `updated=${res.updated}, ожидался 1`);
  assert(res.ambiguous.length === 1, `ambiguous=${JSON.stringify(res.ambiguous)}`);
  assert(sheet.getCell(4, 10) === 1.43, 'однозначная метка не дозаписана');
  assert(sheet.getCell(2, 8) === '' && sheet.getCell(3, 8) === '',
         'неоднозначная метка всё-таки записана');
});

check('9.1.2.2: [поз. 1] не совпадает со строкой, несущей [поз. 12]', () => {
  const ctx = makeContext();
  const sheet = makeTradesSheet(ctx, { blank: 5, filled: 0 });
  sheet.setCell(2, 3, 'BTC'); sheet.setCell(2, NOTE_COL, '[поз. 12] цель');
  const res = post(ctx, {
    secret: ctx.SECRET, sheet: TRADES, mode: 'table_update', noteColumn: NOTE_COL,
    updates: [{ marker: '[поз. 1]', startColumn: 8, values: ['a', 'b', 1] }],
  });
  assert(res.ok === true, `ok=false: ${res.error}`);
  assert(JSON.stringify(res.notFound) === JSON.stringify(['[поз. 1]']),
         `notFound=${JSON.stringify(res.notFound)}`);
  assert(sheet.getCell(2, 8) === '', 'запись ушла в строку с меткой [поз. 12]');
});

check('9.1.2.2: table_append не создаёт вторую строку с уже занятой меткой', () => {
  const ctx = makeContext();
  const sheet = makeTradesSheet(ctx, { blank: 5, filled: 0 });
  // Строка 2 ЗАНЯТА: столбец A заполнен, иначе она считалась бы свободной.
  openRow('ETH', 2472.8).forEach((v, i) => sheet.setCell(2, i + 1, v));
  sheet.setCell(2, NOTE_COL, '[поз. 20] цель');
  const res = post(ctx, {
    secret: ctx.SECRET, sheet: TRADES, mode: 'table_append',
    rows: [openRow('ETH', 2472.8), openRow('XRP', 1.4161)],
    notes: ['[поз. 20] цель', '[поз. 21] цель'],
    noteColumn: NOTE_COL, totalsMarker: 'итого:', formulaFromColumn: FORMULA_FROM,
  });
  assert(res.ok === true, `ok=false: ${res.error}`);
  assert(res.inserted === 1, `inserted=${res.inserted}, ожидался 1`);
  assert(JSON.stringify(res.ambiguous) === JSON.stringify([{ marker: '[поз. 20]', rows: [2] }]),
         `ambiguous=${JSON.stringify(res.ambiguous)}`);
  assert(sheet.getCell(3, 3) === 'XRP', 'создана не та строка');
  assert(sheet.getCell(3, NOTE_COL) === '[поз. 21] цель', 'заметка не та');
  assert(sheet.getCell(4, 3) === '', 'создана лишняя строка');
});

check('9.1.2.2: вся пачка занята — лист не трогается вовсе', () => {
  const ctx = makeContext();
  const sheet = makeTradesSheet(ctx, { blank: 5, filled: 0 });
  openRow('ETH', 2472.8).forEach((v, i) => sheet.setCell(2, i + 1, v));
  sheet.setCell(2, NOTE_COL, '[поз. 30] цель');
  const before = JSON.stringify(sheet.grid);
  const res = post(ctx, {
    secret: ctx.SECRET, sheet: TRADES, mode: 'table_append',
    rows: [openRow('ETH', 2472.8)], notes: ['[поз. 30] цель'],
    noteColumn: NOTE_COL, totalsMarker: 'итого:', formulaFromColumn: FORMULA_FROM,
  });
  assert(res.ok === true, `ok=false: ${res.error}`);
  assert(res.inserted === 0, `inserted=${res.inserted}`);
  assert(res.startRow === undefined, 'назван номер строки, которой нет');
  assert(JSON.stringify(sheet.grid) === before, 'лист изменён');
});

check('9.1.2.2: заметка без метки создаётся как прежде (проверка не мешает)', () => {
  // Заметка потерянной строки начинается не с метки, а со слов «строка
  // открытия не найдена» — такие строки создаются по-прежнему.
  const ctx = makeContext();
  const sheet = makeTradesSheet(ctx, { blank: 4, filled: 1 });
  const note = 'строка открытия не найдена — сделка записана целиком новой '
             + 'строкой; [поз. 40] цель 1.43';
  const res = post(ctx, {
    secret: ctx.SECRET, sheet: TRADES, mode: 'table_append',
    rows: [openRow('XRP', 1.4161)], notes: [note],
    noteColumn: NOTE_COL, totalsMarker: 'итого:', formulaFromColumn: FORMULA_FROM,
  });
  assert(res.ok === true, `ok=false: ${res.error}`);
  assert(res.inserted === 1, `inserted=${res.inserted}`);
  assert(res.ambiguous === undefined, `ambiguous=${JSON.stringify(res.ambiguous)}`);
  assert(sheet.getCell(3, NOTE_COL) === note, 'заметка не записана');
});

check('9.1.2.2: markerRows считает совпадения — 1, 2 и 0', () => {
  // Логика поиска вынесена в отдельную функцию именно затем, чтобы её можно
  // было проверить без Google и без листа.
  const ctx = makeContext();
  const notes = [['[поз. 5] цель'], ['[поз. 12] цель'], ['[поз. 12] и ещё раз']];
  assert(JSON.stringify(ctx.markerRows(notes, '[поз. 5]')) === '[1]', 'одна метка');
  assert(JSON.stringify(ctx.markerRows(notes, '[поз. 12]')) === '[2,3]', 'две метки');
  assert(JSON.stringify(ctx.markerRows(notes, '[поз. 99]')) === '[]', 'нет метки');
  assert(JSON.stringify(ctx.markerRows(notes, '[поз. 1]')) === '[]',
         '[поз. 1] совпало с [поз. 12]');
});

check('9.1.2.2: formulaColumnsToCopy не отдаёт ни литералов, ни столбца заметок', () => {
  const ctx = makeContext();
  // Столбцы 11..20; формулы в 11, 13 и 20 (столбец заметок).
  const formulas = ['=A1', '', '=B1', '', '', '', '', '', '', '=C1'];
  const got = ctx.formulaColumnsToCopy(formulas, 11, 20);
  assert(JSON.stringify(got) === '[11,13]', `отобрано ${JSON.stringify(got)}`);
  assert(got.indexOf(20) < 0, 'столбец заметок отобран');
  assert(JSON.stringify(ctx.formulaColumnsToCopy(['', '', ''], 11, 20)) === '[]',
         'формул нет, а столбцы отобраны');
});

check('выгрузка Этапа 6.6 работает по-прежнему (режимы append/replace)', () => {
  const ctx = makeContext();
  const res = post(ctx, {
    secret: ctx.SECRET, sheet: 'Сигналы', mode: 'replace',
    header: ['a', 'b'], rows: [['x', 'y']],
  });
  assert(res.ok === true, `ok=false: ${res.error}`);
  assert(res.inserted === 1, `inserted=${res.inserted}`);
  assert(res.version === ctx.RECEIVER_VERSION, `version=${res.version}`);
});


// =============================================================================
// ЭТАП 9.2 §6.1. СТОЛБЕЦ ВРЕМЕНИ НЕ ПЕРЕПОЛНЯЕТСЯ НА СУТКАХ
// =============================================================================
//
// СТЕНД СЧИТАЕТ ДЛИТЕЛЬНОСТЬ САМ, потому что двойник формул не считает, а
// проверять надо именно ПОКАЗАННОЕ значение. Google хранит длительность долей
// суток и показывает её по числовому формату; обе ветки формата воспроизведены
// здесь дословно — с переполнением (часы по модулю 24) и без него.
function renderDuration(days, format) {
  const total = Math.round(days * 24 * 3600);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  const two = (n) => String(n).padStart(2, '0');
  const shown = /\[h+\]/i.test(format) ? hours : hours % 24;
  return format.indexOf(':ss') >= 0
    ? `${shown}:${two(minutes)}:${two(seconds)}`
    : `${shown}:${two(minutes)}`;
}
const asDays = (h, m, sec) => (h * 3600 + m * 60 + sec) / 86400;

// Четыре значения §11.6 ТЗ: до суток, находка владельца, почти двое суток и
// ровно двое. Первое обязано не сломаться, остальные три — почина́ться.
const DURATION_CASES = [
  { label: '23:59:00', days: asDays(23, 59, 0), shown: '23:59:00' },
  { label: '24:00:37', days: asDays(24, 0, 37), shown: '24:00:37' },
  { label: '47:59:00', days: asDays(47, 59, 0), shown: '47:59:00' },
  { label: '48:00:00', days: asDays(48, 0, 0), shown: '48:00:00' },
];

check('9.2 §6.1: часовой формат ВРЁТ на сутках — дефект воспроизведён', () => {
  // Сначала показывается сам дефект, и показывается ЧИСЛОМ ВЛАДЕЛЬЦА. Правка,
  // не воспроизводящая ошибку, лечит неизвестно что.
  const broken = 'h:mm:ss';
  assert(renderDuration(DURATION_CASES[1].days, broken) === '0:00:37',
         'формат h:mm:ss обязан показать 24:00:37 как 0:00:37 — иначе стенд '
         + 'не воспроизводит находку владельца от 07.09.2026');
  assert(renderDuration(DURATION_CASES[0].days, broken) === '23:59:00',
         'до суток прежний формат врать не обязан');
});

check('9.2 §6.1: исправленный формат верен на 23:59, 24:00:37, 47:59 и 48:00', () => {
  const ctx = makeContext();
  const fixed = ctx.elapsedTimeFormat('h:mm:ss');
  assert(fixed === '[h]:mm:ss', `получен формат ${fixed}, ожидался [h]:mm:ss`);
  for (const item of DURATION_CASES) {
    const shown = renderDuration(item.days, fixed);
    assert(shown === item.shown,
           `${item.label}: показано ${shown}, ожидалось ${item.shown}`);
  }
});

check('9.2 §6.1: формат даты и уже исправленный формат НЕ трогаются', () => {
  const ctx = makeContext();
  const f = ctx.elapsedTimeFormat;
  assert(f('[h]:mm:ss') === '', 'уже исправленный формат правится второй раз');
  assert(f('dd.MM.yyyy h:mm:ss') === '', 'формат ДАТЫ обёрнут в скобки');
  assert(f('Automatic') === '', 'формат по умолчанию объявлен длительностью');
  assert(f('0.00') === '', 'числовой формат объявлен длительностью');
  assert(f('') === '', 'пустой формат объявлен длительностью');
  assert(f('h:mm') === '[h]:mm', 'формат без секунд не исправлен');
  assert(f('hh:mm:ss') === '[hh]:mm:ss', 'двузначные часы не исправлены');
});

check('9.2 §6.1: созданная строка получает формат без переполнения', () => {
  const ctx = makeContext();
  const sheet = makeTradesSheet(ctx, { blank: 3, filled: 1 });
  // Столбец L (12) — «время в сделке» с переполняющимся форматом. В боевом
  // листе формат стоит на ВСЕХ строках бланка, а не только на первой: бланк
  // размечен заранее, и созданная строка садится в уже размеченную клетку.
  for (let r = 2; r <= 4; r += 1) sheet.setFormatAt(r, 12, 'h:mm:ss');
  const res = post(ctx, {
    secret: ctx.SECRET, sheet: TRADES, mode: 'table_append',
    rows: [openRow('BTC', 77602.7)],
    notes: ['[поз. 71] цель 78000 (+0.50%) · без предела · правило v6'],
    noteColumn: NOTE_COL, totalsMarker: 'итого:', formulaFromColumn: FORMULA_FROM,
  });
  assert(res.ok === true, `ok=false: ${res.error}`);
  assert(sheet.getFormatAt(res.startRow, 12) === '[h]:mm:ss',
         `формат созданной строки ${sheet.getFormatAt(res.startRow, 12)}, `
         + 'ожидался [h]:mm:ss');
  assert(res.formatsFixed >= 1, 'приёмник не сообщил о починке формата');
});

check('9.2 §6.1: дозапись закрытия чинит формат УЖЕ существующей строки', () => {
  const ctx = makeContext();
  const sheet = makeTradesSheet(ctx, { blank: 3, filled: 2 });
  sheet.setCell(3, NOTE_COL, '[поз. 71] цель 78000');
  sheet.setFormatAt(3, 12, 'h:mm:ss');
  const res = post(ctx, {
    secret: ctx.SECRET, sheet: TRADES, mode: 'table_update', noteColumn: NOTE_COL,
    updates: [{
      marker: '[поз. 71]', startColumn: 8, formulaFromColumn: FORMULA_FROM,
      values: ['07.09.2026', '08:08:00', 77650.0],
      noteAppend: ' · истёк срок 48 ч · итог системы −0.44%',
    }],
  });
  assert(res.ok === true, `ok=false: ${res.error}`);
  assert(res.updated === 1, `updated=${res.updated}`);
  assert(sheet.getFormatAt(3, 12) === '[h]:mm:ss',
         `формат строки закрытия ${sheet.getFormatAt(3, 12)}, ожидался [h]:mm:ss`);
});


// ===========================================================================
// ЭТАП 9.5.2. cells_values: запись значений в ячейки листа владельца
// ===========================================================================

// Имя листа владельца — С ПРОБЕЛОМ В КОНЦЕ, как в боевой книге.
const OWNER_NAME = 'торговля демо апи окх ';
const OWNER_ASKED = 'торговля демо апи окх';
// Колонки с формулами владельца: E, L, N, O, P, Q, R, S, U, V.
const OWNER_FORMULA_COLS = [5, 12, 14, 15, 16, 17, 18, 19, 21, 22];

/** Двойник листа владельца: шапка (строки 1–5), формулы до строки 1000, форматы, проверка D. */
function makeOwnerSheet(ctx, name = OWNER_NAME) {
  const sheet = ctx.SpreadsheetApp.getActiveSpreadsheet().insertSheet(name);
  sheet.maxColumns = 26;
  sheet.setCell(1, 1, 'ДЕМО-счёт OKX');
  sheet.setCell(2, 1, 'стартовый капитал');
  sheet.setCell(4, 1, 'дата'); sheet.setCell(4, 14, 'прибыль $');
  for (let r = 6; r <= 1000; r += 1) {
    for (const c of OWNER_FORMULA_COLS) sheet.setFormulaAt(r, c, `=K${r}-G${r}`);
    sheet.setFormatAt(r, 1, 'dd.MM.yyyy');
    sheet.setFormatAt(r, 2, '[h]:mm:ss');
    sheet.setFormatAt(r, 7, '0.00');
    sheet.validations.set(`${r}:4`, ['покупать', 'пропущено']);
  }
  sheet.setFormatAt(3, 1, '#,##0.00');
  return sheet;
}

const ownerBlocks = (n, from = 6) => ({
  writes: [
    { range: 'A3', values: [[1000]] },
    { range: `A${from}:D${from + n - 1}`,
      values: Array.from({ length: n }, (_, i) => [46300 + i, 0.5, 'BTC', 'покупать']) },
    { range: `F${from}:K${from + n - 1}`,
      values: Array.from({ length: n }, () => [60000, 2, '', '', '', '']) },
    { range: `M${from}:M${from + n - 1}`, values: Array.from({ length: n }, () => [0.001]) },
    { range: `T${from}:T${from + n - 1}`,
      values: Array.from({ length: n }, (_, i) => [`[поз. ${i + 1}] заметка`]) },
  ],
  clears: [
    { range: `A${from + n}:D1000` }, { range: `F${from + n}:K1000` },
    { range: `M${from + n}:M1000` }, { range: `T${from + n}:T1000` },
  ],
});

const postCells = (ctx, body) => post(ctx, {
  secret: ctx.SECRET, sheet: OWNER_ASKED, mode: 'cells_values', ...body,
});

const snapshot = (sheet) => JSON.stringify([
  sheet.grid, [...sheet.formulas], [...sheet.numberFormats], [...sheet.validations],
]);

console.log('\nЭтап 9.5.2: режим cells_values');

check('9.5.2: версия приёмника — 9.5.2', () => {
  const ctx = makeContext();
  const res = post(ctx, { secret: ctx.SECRET, sheet: OWNER_ASKED, mode: 'version' });
  assert(res.ok === true && res.version === '9.5.2', `version=${res.version}`);
});

check('9.5.2: лист с пробелом в конце имени находится по имени без пробела', () => {
  const ctx = makeContext();
  const sheet = makeOwnerSheet(ctx);
  const res = postCells(ctx, ownerBlocks(2));
  assert(res.ok === true, `ok=false: ${res.error}`);
  assert(sheet.getCell(3, 1) === 1000, 'A3 не записана');
  assert(sheet.getCell(6, 3) === 'BTC' && sheet.getCell(7, 3) === 'BTC', 'токены не записаны');
  // Пробелы с ОБЕИХ сторон запрошенного имени тоже не мешают.
  const res2 = post(ctx, {
    secret: ctx.SECRET, sheet: '  торговля демо апи окх  ', mode: 'cells_values',
    writes: [{ range: 'A3', values: [[1234]] }], clears: [],
  });
  assert(res2.ok === true && sheet.getCell(3, 1) === 1234, 'trim с обеих сторон не работает');
});

check('9.5.2: ответ — written и cleared считают ячейки', () => {
  const ctx = makeContext();
  makeOwnerSheet(ctx);
  const res = postCells(ctx, ownerBlocks(2));
  // A3 (1) + A:D 2×4 + F:K 2×6 + M 2 + T 2 = 25
  assert(res.written === 25, `written=${res.written}, ожидалось 25`);
  // Очистка со строки 8 по 1000: 993 строки × (4 + 6 + 1 + 1) колонок
  assert(res.cleared === 993 * 12, `cleared=${res.cleared}, ожидалось ${993 * 12}`);
});

check('9.5.2: лист не найден — sheet_not_found, лист НЕ создан, ничего не записано', () => {
  const ctx = makeContext();
  const other = ctx.SpreadsheetApp.getActiveSpreadsheet().insertSheet('другой лист');
  const before = snapshot(other);
  const count = ctx.sheets.size;
  const res = postCells(ctx, ownerBlocks(1));
  assert(res.ok === false && res.error === 'sheet_not_found', `ответ ${JSON.stringify(res)}`);
  assert(ctx.sheets.size === count, 'лист был создан');
  assert(snapshot(other) === before, 'чужой лист изменён');
});

check('9.5.2: два листа с одинаковым trim-именем — sheet_ambiguous, ничего не записано', () => {
  const ctx = makeContext();
  const first = makeOwnerSheet(ctx, 'торговля демо апи окх');
  const second = makeOwnerSheet(ctx, ' торговля демо апи окх ');
  const before = snapshot(first) + snapshot(second);
  const res = postCells(ctx, ownerBlocks(1));
  assert(res.ok === false && res.error === 'sheet_ambiguous', `ответ ${JSON.stringify(res)}`);
  assert(snapshot(first) + snapshot(second) === before, 'какой-то из листов изменён');
});

check('9.5.2: формулы, форматы и проверка данных владельца на месте после записи и очистки', () => {
  const ctx = makeContext();
  const sheet = makeOwnerSheet(ctx);
  // Хвост от прежнего прогона: строки 8–10 заполнены, должны очиститься.
  for (let r = 8; r <= 10; r += 1) {
    sheet.setCell(r, 1, 46300); sheet.setCell(r, 3, 'ETH'); sheet.setCell(r, 20, 'старая');
  }
  const formulasBefore = JSON.stringify([...sheet.formulas]);
  const formatsBefore = JSON.stringify([...sheet.numberFormats]);
  const validationsBefore = JSON.stringify([...sheet.validations]);
  const res = postCells(ctx, ownerBlocks(2));
  assert(res.ok === true, `ok=false: ${res.error}`);
  assert(JSON.stringify([...sheet.formulas]) === formulasBefore, 'формулы владельца изменены');
  assert(JSON.stringify([...sheet.numberFormats]) === formatsBefore,
         'форматы изменены: очистка сняла оформление');
  assert(JSON.stringify([...sheet.validations]) === validationsBefore,
         'проверка данных изменена');
  for (let r = 8; r <= 10; r += 1) {
    assert(sheet.getCell(r, 1) === '' && sheet.getCell(r, 3) === '' && sheet.getCell(r, 20) === '',
           `строка ${r} не очищена`);
  }
  // Колонки-формулы не записаны: ни одного значения в E, L, N–S, U, V.
  for (const c of OWNER_FORMULA_COLS) {
    for (let r = 6; r <= 12; r += 1) {
      assert(sheet.getCell(r, c) === '', `в колонку ${c} строки ${r} что-то записано`);
    }
  }
  // Строки 1, 2, 4 шапки не тронуты.
  assert(sheet.getCell(1, 1) === 'ДЕМО-счёт OKX' && sheet.getCell(2, 1) === 'стартовый капитал'
         && sheet.getCell(4, 1) === 'дата', 'шапка листа изменена');
});

check('9.5.2: «ошибка» и «потеряна» записываются в колонку D с проверкой данных «покупать, пропущено»', () => {
  // ГРАНИЦА ЭТОГО СЦЕНАРИЯ. Двойник повторяет документированное поведение Google:
  // setValues из скрипта проверку данных НЕ применяет (она работает на ввод руками),
  // и приёмник нигде не зовёт API проверки данных (setDataValidation в двойнике
  // бросает ошибку). Поведение самого Google на живой книге подтверждает только
  // первая запись на сервере — это пункт инструкции владельцу.
  const ctx = makeContext();
  const sheet = makeOwnerSheet(ctx);
  const res = postCells(ctx, {
    writes: [{ range: 'D6:D8', values: [['ошибка'], ['потеряна'], ['пропущено']] }],
    clears: [],
  });
  assert(res.ok === true, `ok=false: ${res.error}`);
  assert(sheet.getCell(6, 4) === 'ошибка' && sheet.getCell(7, 4) === 'потеряна', 'D не записана');
  assert(sheet.validations.get('6:4').join() === 'покупать,пропущено', 'проверка данных тронута');
});

check('9.5.2: формула в целевом диапазоне — formula_in_target и НИ ОДНОЙ записанной ячейки', () => {
  const ctx = makeContext();
  const sheet = makeOwnerSheet(ctx);
  // Формула попадает в ТРЕТИЙ по счёту диапазон (M): записи A3, A:D, F:K, M — всё до неё
  // уже могло бы быть записано, а приёмник обязан отказать, не тронув ничего.
  sheet.setFormulaAt(7, 13, '=K7-G7');
  const before = snapshot(sheet);
  const res = postCells(ctx, ownerBlocks(3));
  assert(res.ok === false && res.error === 'formula_in_target', `ответ ${JSON.stringify(res)}`);
  assert(res.range === 'M6:M8', `range=${res.range}`);
  assert(snapshot(sheet) === before, 'часть ячеек записана несмотря на отказ');
});

check('9.5.2: формула в диапазоне ОЧИСТКИ — тоже отказ без единой записи', () => {
  const ctx = makeContext();
  const sheet = makeOwnerSheet(ctx);
  sheet.setFormulaAt(500, 20, '=A500');            // T500 — внутри clears T8:T1000
  const before = snapshot(sheet);
  const res = postCells(ctx, ownerBlocks(2));
  assert(res.ok === false && res.error === 'formula_in_target', `ответ ${JSON.stringify(res)}`);
  assert(snapshot(sheet) === before, 'часть ячеек записана несмотря на отказ');
});

check('9.5.2: чужой адрес, неверный размер, «=…» в значении, выход за лист — отказ без записи', () => {
  const cases = [
    ['bad_range', { writes: [{ range: 'Лист2!A1', values: [[1]] }] }],
    ['bad_range', { writes: [{ range: 'A:A', values: [[1]] }] }],
    ['bad_range', { clears: [{ range: 'A6:D' }] }],
    ['values_shape', { writes: [{ range: 'A6:D7', values: [[1, 2, 3, 4]] }] }],
    ['values_shape', { writes: [{ range: 'A6:D6', values: [[1, 2, 3]] }] }],
    ['formula_in_values', { writes: [{ range: 'T6', values: [['=IMPORTXML("x")']] }] }],
    ['range_out_of_sheet', { clears: [{ range: 'A6:D1001' }] }],
  ];
  for (const [code, extra] of cases) {
    const ctx = makeContext();
    const sheet = makeOwnerSheet(ctx);
    const before = snapshot(sheet);
    const res = postCells(ctx, { writes: [{ range: 'A3', values: [[1]] }], ...extra });
    assert(res.ok === false && res.error === code,
           `ожидался ${code}, ответ ${JSON.stringify(res)}`);
    assert(snapshot(sheet) === before, `${code}: лист изменён`);
  }
});

check('9.5.2: повторный запрос с теми же данными ничего не меняет (идемпотентность)', () => {
  const ctx = makeContext();
  const sheet = makeOwnerSheet(ctx);
  postCells(ctx, ownerBlocks(3));
  const first = snapshot(sheet);
  const res = postCells(ctx, ownerBlocks(3));
  assert(res.ok === true, `ok=false: ${res.error}`);
  assert(snapshot(sheet) === first, 'второй прогон изменил лист');
});

check('9.5.2: строк стало меньше — хвост очищается, строки выше остаются', () => {
  const ctx = makeContext();
  const sheet = makeOwnerSheet(ctx);
  postCells(ctx, ownerBlocks(10));
  assert(sheet.getCell(15, 3) === 'BTC', 'исходное состояние: 10 строк не записаны');
  const res = postCells(ctx, ownerBlocks(8));
  assert(res.ok === true, `ok=false: ${res.error}`);
  for (const r of [14, 15]) {
    assert(sheet.getCell(r, 1) === '' && sheet.getCell(r, 3) === '' && sheet.getCell(r, 20) === ''
           && sheet.getCell(r, 7) === '' && sheet.getCell(r, 13) === '', `строка ${r} не очищена`);
    assert(sheet.getFormulaAt(r, 14) !== '', `формула N${r} стёрта`);
    assert(sheet.getFormatAt(r, 1) === 'dd.MM.yyyy', `формат A${r} снят`);
  }
  assert(sheet.getCell(13, 3) === 'BTC', 'строка 13 затёрта');
});

check('9.5.2: чужой секрет отвергается и в новом режиме', () => {
  const ctx = makeContext();
  const sheet = makeOwnerSheet(ctx);
  const before = snapshot(sheet);
  const res = post(ctx, { secret: 'неверный', sheet: OWNER_ASKED, mode: 'cells_values',
                          writes: [{ range: 'A3', values: [[1]] }], clears: [] });
  assert(res.ok === false && res.error === 'forbidden', `ответ ${JSON.stringify(res)}`);
  assert(snapshot(sheet) === before, 'лист изменён при чужом секрете');
});

check('9.5.2: общий путь по-прежнему создаёт лист, а cells_values — нет', () => {
  const ctx = makeContext();
  post(ctx, { secret: ctx.SECRET, sheet: 'Новый', mode: 'replace', rows: [['a']] });
  assert(ctx.sheets.has('Новый'), 'режим replace перестал создавать лист');
  const res = post(ctx, { secret: ctx.SECRET, sheet: 'Новый2', mode: 'cells_values',
                          writes: [{ range: 'A1', values: [['x']] }], clears: [] });
  assert(res.ok === false && !ctx.sheets.has('Новый2'), 'cells_values создал лист');
});

console.log(failed === 0 ? '\nВсе сценарии стенда прошли'

                         : `\nПровалено сценариев: ${failed}`);
process.exit(failed === 0 ? 0 : 1);
