// Регрессионный JS-сценарий: статическая проверка безопасности интерфейса.
// Запуск: node test_ui.cjs
//
// Проверяет:
//   1) блокировку кнопок движения (busy + disabled);
//   2) отказ сохранения макроса по устаревшим/несвежим координатам;
//   3) отсутствие innerHTML с пользовательскими строками.

'use strict';

const fs = require('fs');
const path = require('path');

const root = __dirname;
const appJs = fs.readFileSync(path.join(root, 'static', 'app.js'), 'utf8');
const positionHtml = fs.readFileSync(path.join(root, 'static', 'position.html'), 'utf8');

let failures = 0;
function check(name, ok, detail) {
  if (ok) {
    console.log('ok   - ' + name);
  } else {
    failures += 1;
    console.error('FAIL - ' + name + (detail ? ': ' + detail : ''));
  }
}

// 1) Блокировка кнопок движения на время операции.
check('есть флаг busy', /\blet busy = false\b/.test(appJs));
check('setBusy снимает/ставит disabled', /\bdisabled\s*=/.test(appJs) && /function setBusy/.test(appJs));
check('moveAxis обёрнут в withBusy', /async function moveAxis[\s\S]*?withBusy/.test(appJs));

// 2) addMacro сохраняет макрос только по свежему успешному опросу позиции.
const addMacroSrc = appJs.slice(appJs.indexOf('async function addMacro'));
check(
  'addMacro опрашивает свежую позицию перед сохранением',
  /api\('\/api\/position'\)/.test(addMacroSrc),
);
check(
  'addMacro отказывается сохранять по stale',
  /fresh\.stale/.test(addMacroSrc),
);

// 3) Отсутствие innerHTML с пользовательскими строками в UI.
check('app.js не использует .innerHTML', !/\.innerHTML\s*=/.test(appJs));
check('position.html не использует .innerHTML', !/\.innerHTML\s*=/.test(positionHtml));
check('app.js использует textContent', /\.textContent\s*=/.test(appJs));
check('position.html использует textContent', /\.textContent\s*=/.test(positionHtml));

if (failures > 0) {
  console.error('\n' + failures + ' проверок не пройдено');
  process.exit(1);
}
console.log('\nВсе JS-проверки пройдены');
