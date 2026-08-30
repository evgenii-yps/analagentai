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
 * ЭТАП 9.1.1. Добавлены сценарии действия "append_position": лист владельца с
 * формулами в столбцах G и K..S, протяжка формул в новую строку, отказ при
 * несовпадении заголовков и отказ при отсутствующем листе. Двойник умеет
 * формулы ровно настолько, насколько это нужно приёмнику: хранит текст формулы
 * отдельно от значения и сдвигает номера строк в относительных ссылках при
 * PASTE_FORMULA — иначе проверять протяжку было бы нечем.
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

/** ``A1`` → ``{row: 1, col: 1}``; ``K7:S7`` → диапазон. Только то, что нужно. */
function parseA1(ref) {
  const m = /^([A-Z]+)(\d+)(?::([A-Z]+)(\d+))?$/.exec(String(ref));
  if (!m) throw new Error(`двойник не понимает ссылку «${ref}»`);
  const colNum = (letters) => letters.split('')
    .reduce((acc, ch) => acc * 26 + (ch.charCodeAt(0) - 64), 0);
  const row = Number(m[2]);
  const col = colNum(m[1]);
  const lastRow = m[4] ? Number(m[4]) : row;
  const lastCol = m[3] ? colNum(m[3]) : col;
  return { row, col, numRows: lastRow - row + 1, numCols: lastCol - col + 1 };
}

/** Двойник листа. Ошибки повторяют формулировки Google. */
function makeSheet(name) {
  return {
    name,
    grid: [],
    // Формулы живут ОТДЕЛЬНО от значений, как в Google: ячейка с формулой
    // возвращает из getValue() посчитанное значение, а из getFormula() — текст.
    // Двойник ничего не считает: значение такой ячейки для приёмника не важно,
    // важно лишь, что формула есть.
    formulas: new Map(),
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
      // Ссылка вида 'A1' / 'K7:S7' — вторая форма getRange, которой пользуется
      // действие append_position.
      if (typeof row === 'string') {
        const box = parseA1(row);
        return {
          box,
          getValue() { return self.getCell(box.row, box.col); },
          setValue(value) { self.setCell(box.row, box.col, value); },
          getFormula() { return self.getFormulaAt(box.row, box.col); },
          clearContent() {
            for (let r = 0; r < box.numRows; r += 1) {
              for (let c = 0; c < box.numCols; c += 1) {
                self.setCell(box.row + r, box.col + c, '');
                self.setFormulaAt(box.row + r, box.col + c, '');
              }
            }
          },
          copyTo(target, type) {
            if (type !== 'PASTE_FORMULA') {
              throw new Error(`двойник поддерживает только PASTE_FORMULA, а не ${type}`);
            }
            const to = target.box;
            for (let r = 0; r < box.numRows; r += 1) {
              for (let c = 0; c < box.numCols; c += 1) {
                const text = self.getFormulaAt(box.row + r, box.col + c);
                if (!text) continue;
                // Относительные ссылки сдвигаются на разницу строк — ровно так
                // же, как это делает Google. Без сдвига цепочка G повторяла бы
                // чужую строку, и проверка протяжки ничего не проверяла бы.
                const shift = to.row - box.row;
                const moved = text.replace(/(\$?)([A-Z]+)(\$?)(\d+)/g,
                  (whole, dollarCol, letters, dollarRow, digits) => (
                    dollarRow ? whole
                              : `${dollarCol}${letters}${Number(digits) + shift}`
                  ));
                self.setFormulaAt(to.row + r, to.col + c, moved);
              }
            }
          },
        };
      }
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


// --- Этап 9.1.1 §7: действие append_position ------------------------------

const SHEET = 'торговля тест апи окх чтение';
const HEADERS = {
  A: 'дата вход', B: 'время сигнала', C: 'токен', D: 'сигнал',
  F: 'цена открытия', H: 'дата выход', I: 'время выхода', J: 'цена закрытия',
};
const VALUES = {
  A: '30.08.2026', B: '13:42:17', C: 'BTC', D: 'покупать',
  F: 77602.7, H: '31.08.2026', I: '9:05:00', J: 77750.1,
};

/**
 * Двойник ЖИВОГО листа владельца: заголовки, две заполненные строки и формулы
 * в G и K..S, заведённые на несколько строк вперёд. Именно так лист и устроен:
 * формулы кончаются раньше данных, и в этом весь смысл протяжки.
 */
function makeOwnerSheet(ctx, { rows = 2, formulasUntil = 4 } = {}) {
  const sheet = ctx.SpreadsheetApp.getActiveSpreadsheet().insertSheet(SHEET);
  const headerRow = [];
  const letters = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J'];
  letters.forEach((c, i) => { headerRow[i] = HEADERS[c] || ''; });
  headerRow[6] = 'вход - объем';
  sheet.appendRow(headerRow);
  for (let r = 2; r <= 1 + rows; r += 1) {
    ['A', 'B', 'C', 'D', 'F', 'H', 'I', 'J'].forEach((c) => {
      sheet.getRange(c + r).setValue(`старое_${c}${r}`);
    });
  }
  // Первая строка объёма задана вручную ($10,00000), дальше — цепочка.
  sheet.setFormulaAt(2, 7, '=10');
  for (let r = 3; r <= formulasUntil; r += 1) sheet.setFormulaAt(r, 7, `=G${r - 1}+K${r - 1}`);
  for (let r = 2; r <= formulasUntil; r += 1) {
    for (let col = 11; col <= 19; col += 1) sheet.setFormulaAt(r, col, `=F${r}*2`);
  }
  return sheet;
}

const appendBody = (ctx, over = {}) => Object.assign({
  secret: ctx.SECRET, action: 'append_position', sheet: SHEET,
  values: VALUES, headers: HEADERS,
}, over);

check('append_position: строка ложится в первую свободную, формулы протянуты', () => {
  const ctx = makeContext();
  const sheet = makeOwnerSheet(ctx, { rows: 2, formulasUntil: 3 });
  const res = post(ctx, appendBody(ctx));
  assert(res.ok === true, `ok=false: ${res.error}`);
  assert(res.row === 4, `строка ${res.row}, ожидалась 4`);
  assert(sheet.getCell(4, 1) === VALUES.A, 'столбец A не записан');
  assert(sheet.getCell(4, 10) === VALUES.J, 'столбец J не записан');
  // G протянут ЦЕПОЧКОЙ, а не скопирован как есть.
  assert(sheet.getFormulaAt(4, 7) === '=G3+K3',
         `формула G4 «${sheet.getFormulaAt(4, 7)}», ожидалась «=G3+K3»`);
  assert(sheet.getFormulaAt(4, 19) === '=F4*2', 'формула S4 не протянута');
  assert(sheet.getCell(4, 7) === '', 'в столбец G записано значение — формула затёрта');
  assert(sheet.getCell(4, 5) === '', 'записан столбец-разделитель E');
});

check('append_position: седьмая строка за пределом формул — формулы протягиваются', () => {
  // Ровно случай §7.4: формулы кончились раньше данных. Строка без формул
  // выглядит записанной и не считается никак — протяжка обязана её спасти.
  const ctx = makeContext();
  const sheet = makeOwnerSheet(ctx, { rows: 5, formulasUntil: 3 });
  const res = post(ctx, appendBody(ctx));
  assert(res.ok === true, `ok=false: ${res.error}`);
  assert(res.row === 7, `строка ${res.row}, ожидалась 7`);
  assert(sheet.getFormulaAt(7, 7) === '=G6+K6',
         `формула G7 «${sheet.getFormulaAt(7, 7)}»`);
  assert(sheet.getFormulaAt(7, 11) === '=F7*2', 'формула K7 не протянута');
});

check('append_position: заголовки не совпали — отказ, в лист не записано ничего', () => {
  const ctx = makeContext();
  // formulasUntil: 3 — чтобы строка 4 была ПУСТА и по ней было видно, что
  // приёмник не оставил в ней ни значений, ни протянутых формул.
  const sheet = makeOwnerSheet(ctx, { rows: 2, formulasUntil: 3 });
  sheet.getRange('C1').setValue('монета');       // владелец переименовал столбец
  const res = post(ctx, appendBody(ctx));
  assert(res.ok === false, 'запись прошла при несовпавших заголовках');
  assert(/заголовки/.test(res.error), `неожиданная причина: ${res.error}`);
  assert(sheet.getCell(4, 1) === '', 'строка всё-таки записана');
  assert(sheet.getFormulaAt(4, 7) === '', 'в листе остались следы протяжки');
});

check('append_position: листа с таким именем нет — отказ, лист не создаётся', () => {
  const ctx = makeContext();
  const res = post(ctx, appendBody(ctx, { sheet: 'нет такого листа' }));
  assert(res.ok === false, 'отказа не было');
  assert(/лист не найден/.test(res.error), `неожиданная причина: ${res.error}`);
  assert(ctx.sheets.get('нет такого листа') === undefined, 'лист создан');
});

check('append_position: значение в столбец с формулой не принимается', () => {
  const ctx = makeContext();
  const sheet = makeOwnerSheet(ctx);
  const res = post(ctx, appendBody(ctx, {
    values: Object.assign({}, VALUES, { G: 10.5 }),
  }));
  assert(res.ok === false, 'запись в столбец G прошла');
  assert(/вне перечня/.test(res.error), `неожиданная причина: ${res.error}`);
  assert(sheet.getCell(4, 1) === '', 'строка записана несмотря на отказ');
});

check('append_position: образца с формулой нет — отказ, а не строка без формул', () => {
  const ctx = makeContext();
  const sheet = ctx.SpreadsheetApp.getActiveSpreadsheet().insertSheet(SHEET);
  const letters = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J'];
  sheet.appendRow(letters.map((c) => HEADERS[c] || ''));
  const res = post(ctx, appendBody(ctx));
  assert(res.ok === false, 'строка записана без формул');
  assert(/формулы не протянулись/.test(res.error), `неожиданная причина: ${res.error}`);
  assert(sheet.getCell(2, 1) === '', 'строка всё-таки записана');
});

check('append_position: секрет проверяется тот же самый', () => {
  const ctx = makeContext();
  makeOwnerSheet(ctx);
  const res = post(ctx, appendBody(ctx, { secret: 'не тот' }));
  assert(res.ok === false && res.error === 'forbidden', 'секрет не проверен');
});

check('append_position: выгрузка Этапа 6.6 работает по-прежнему (без action)', () => {
  const ctx = makeContext();
  const res = post(ctx, {
    secret: ctx.SECRET, sheet: 'Сигналы', mode: 'replace',
    header: ['a', 'b'], rows: [['x', 'y']],
  });
  assert(res.ok === true, `ok=false: ${res.error}`);
  assert(res.inserted === 1, `inserted=${res.inserted}`);
});

console.log(failed === 0 ? '\nВсе сценарии стенда прошли'
                         : `\nПровалено сценариев: ${failed}`);
process.exit(failed === 0 ? 0 : 1);
