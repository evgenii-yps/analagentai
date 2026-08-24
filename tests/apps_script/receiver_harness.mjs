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
    maxColumns: 26,          // столько колонок у нового листа Google Таблицы
    frozen: 0,
    clear() { this.grid = []; },
    getLastRow() { return this.grid.length; },
    getMaxColumns() { return this.maxColumns; },
    insertColumnsAfter(after, howMany) { this.maxColumns += howMany; },
    setFrozenRows(n) { this.frozen = n; },
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
          for (let i = 0; i < numRows; i += 1) {
            self.grid[row - 1 + i] = values[i].slice();
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
    SpreadsheetApp: { getActiveSpreadsheet: () => spreadsheet },
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

console.log(failed === 0 ? '\nВсе сценарии стенда прошли'
                         : `\nПровалено сценариев: ${failed}`);
process.exit(failed === 0 ? 0 : 1);
