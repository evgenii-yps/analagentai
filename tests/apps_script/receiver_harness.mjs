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
    maxColumns: 26,          // столько колонок у нового листа Google Таблицы
    frozen: 0,
    clear() { this.grid = []; this.formulas.clear(); },
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
    insertColumnsAfter(after, howMany) { this.maxColumns += howMany; },
    setFrozenRows(n) { this.frozen = n; },
    key(row, col) { return `${row}:${col}`; },
    getFormulaAt(row, col) { return this.formulas.get(this.key(row, col)) || ''; },
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
    },
    appendRow(values) {
      if (values.length > this.maxColumns) this.maxColumns = values.length;
      this.grid.push(values.slice());
    },
    getRange(row, col, numRows, numCols) {
      const self = this;
      if (col + numCols - 1 > self.maxColumns) {
        throw new Error('Those columns are out of bounds.');
      }
      return {
        box: { row, col, numRows, numCols },
        getValue() { return self.getCell(row, col); },
        setValue(value) { self.setCell(row, col, value); },
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
              if (!text) continue;
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
      note: '[поз. 91] цель · цель достигнута · итог системы +0.76%',
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

check('выгрузка Этапа 6.6 работает по-прежнему (режимы append/replace)', () => {
  const ctx = makeContext();
  const res = post(ctx, {
    secret: ctx.SECRET, sheet: 'Сигналы', mode: 'replace',
    header: ['a', 'b'], rows: [['x', 'y']],
  });
  assert(res.ok === true, `ok=false: ${res.error}`);
  assert(res.inserted === 1, `inserted=${res.inserted}`);
  assert(res.version === '9.1.2', `version=${res.version}`);
});

console.log(failed === 0 ? '\nВсе сценарии стенда прошли'

                         : `\nПровалено сценариев: ${failed}`);
process.exit(failed === 0 ? 0 : 1);
