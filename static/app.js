// Логика интерфейса юстировки: общение с Flask-бэкендом по fetch.
// Все пользовательские строки выводятся через textContent/DOM-свойства (без innerHTML).
let config = null;
let position = {};
let busy = false;

async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || 'GET',
    headers: { 'Content-Type': 'application/json' },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let data;
  try { data = await res.json(); } catch (e) { data = {}; }
  if (!res.ok || data.ok === false) {
    throw new Error(data.error || ('HTTP ' + res.status));
  }
  return data;
}

function toast(msg, isError = false) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = isError ? 'show error' : 'show';
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { t.className = ''; }, 2500);
}

function setBusy(value) {
  busy = value;
  document.querySelectorAll('button[data-axis], #btn-home, #btn-set-ref, #btn-shutdown-home, #btn-shutdown-keep, #btn-macro-add, #btn-connect')
    .forEach(b => { b.disabled = value; });
}

async function withBusy(fn) {
  if (busy) return;
  setBusy(true);
  try {
    await fn();
  } finally {
    setBusy(false);
  }
}

async function loadConfig() {
  config = await api('/api/config');
  document.getElementById('ip').value = config.board.ip || '';
  document.getElementById('password').value = config.board.password || '';
  document.getElementById('mock').checked = !!config.board.mock;
  document.getElementById('feedrate').value = config.defaults.feedrate_mm_min;
  renderAxes();
  renderSettings();
  loadMacros();
}
function fmt(v) { return Number(v).toString(); }

function makeStepButton(axis, dir, step, label, cls) {
  const b = document.createElement('button');
  b.className = cls;
  b.dataset.axis = axis;
  b.dataset.dir = String(dir);
  b.dataset.step = String(step);
  b.textContent = label;
  return b;
}

function makeStepInput(axis, slot, dir, value) {
  const inp = document.createElement('input');
  inp.type = 'number';
  inp.step = 'any';
  inp.min = '0.0001';
  inp.dataset.axis = axis;
  inp.dataset.slot = String(slot);
  inp.dataset.dir = dir;
  inp.value = value;
  return inp;
}

function renderAxes() {
  const box = document.getElementById('axes');
  box.replaceChildren();
  for (const ax of config.axes) {
    const card = document.createElement('div');
    card.className = 'axis';

    const title = document.createElement('div');
    title.className = 'axis-title';
    title.textContent = ax.letter + ' — ' + (ax.name || ('ось ' + ax.letter));
    if (ax.has_endstop) {
      title.appendChild(document.createTextNode(' '));
      const tag = document.createElement('span');
      tag.className = 'tag';
      tag.textContent = 'концевик';
      title.appendChild(tag);
    }
    card.appendChild(title);

    const posEl = document.createElement('div');
    posEl.className = 'axis-pos';
    posEl.id = 'pos-' + ax.letter;
    posEl.textContent = '—';
    card.appendChild(posEl);

    const steps = (ax.steps && ax.steps.length ? ax.steps : [{ plus: 0.1, minus: 0.1 }]);
    for (const s of steps) {
      const group = document.createElement('div');
      group.className = 'step-group';
      group.appendChild(makeStepButton(ax.letter, -1, s.minus, '−' + fmt(s.minus), 'minus'));
      group.appendChild(makeStepButton(ax.letter, 1, s.plus, '+' + fmt(s.plus), 'plus'));
      card.appendChild(group);
    }
    box.appendChild(card);
  }
  box.querySelectorAll('button[data-axis]').forEach(b => {
    b.addEventListener('click', () => moveAxis(b.dataset.axis, Number(b.dataset.dir), Number(b.dataset.step)));
  });
}

async function moveAxis(axis, direction, step) {
  await withBusy(async () => {
    try {
      const feedrate = Number(document.getElementById('feedrate').value) || 600;
      await api('/api/move', { method: 'POST', body: { axis, direction, step, feedrate } });
      await pollPosition();
    } catch (e) {
      toast('Ошибка: ' + e.message, true);
    }
  });
}

async function pollPosition() {
  try {
    const data = await api('/api/position');
    if (data.stale) return; // устаревшие данные не считаем актуальными
    position = data.position || {};
    renderPosition();
  } catch (e) {
    /* не спамим ошибками — оставляем последние координаты */
  }
}

function renderPosition() {
  for (const [axis, val] of Object.entries(position)) {
    const el = document.getElementById('pos-' + axis);
    if (el) el.textContent = Number(val).toFixed(3) + ' мм';
  }
  const box = document.getElementById('position');
  box.replaceChildren();
  for (const [k, v] of Object.entries(position)) {
    const span = document.createElement('span');
    span.textContent = k + ': ';
    const b = document.createElement('b');
    b.textContent = Number(v).toFixed(3);
    span.appendChild(b);
    span.appendChild(document.createTextNode(' мм'));
    box.appendChild(span);
    box.appendChild(document.createTextNode(' '));
  }
}
function renderSettings() {
  const box = document.getElementById('settings');
  box.replaceChildren();

  const table = document.createElement('table');
  const thead = document.createElement('tr');
  ['Ось', 'Название', 'Ток (мА)', 'Скорость (мм/мин)', 'Концевик'].forEach(h => {
    const th = document.createElement('th');
    th.textContent = h;
    thead.appendChild(th);
  });
  table.appendChild(thead);

  config.axes.forEach((ax, i) => {
    const tr = document.createElement('tr');
    const tdLetter = document.createElement('td');
    tdLetter.textContent = ax.letter;
    tr.appendChild(tdLetter);

    const tdName = document.createElement('td');
    const nameInput = document.createElement('input');
    nameInput.dataset.i = String(i);
    nameInput.dataset.field = 'name';
    nameInput.value = ax.name || '';
    tdName.appendChild(nameInput);
    tr.appendChild(tdName);

    const tdCurrent = document.createElement('td');
    const curInput = document.createElement('input');
    curInput.type = 'number';
    curInput.dataset.i = String(i);
    curInput.dataset.field = 'current_mA';
    curInput.value = ax.current_mA || '';
    tdCurrent.appendChild(curInput);
    tr.appendChild(tdCurrent);

    const tdSpeed = document.createElement('td');
    const spdInput = document.createElement('input');
    spdInput.type = 'number';
    spdInput.dataset.i = String(i);
    spdInput.dataset.field = 'speed_mm_min';
    spdInput.value = ax.speed_mm_min || '';
    tdSpeed.appendChild(spdInput);
    tr.appendChild(tdSpeed);

    const tdEnd = document.createElement('td');
    const endInput = document.createElement('input');
    endInput.type = 'checkbox';
    endInput.dataset.i = String(i);
    endInput.dataset.field = 'has_endstop';
    endInput.checked = !!ax.has_endstop;
    tdEnd.appendChild(endInput);
    tr.appendChild(tdEnd);

    table.appendChild(tr);
  });
  box.appendChild(table);

  const stepsWrap = document.createElement('div');
  stepsWrap.className = 'steps-editor';
  const h3 = document.createElement('h3');
  h3.style.margin = '14px 0 8px';
  h3.textContent = 'Шаги ± (мм)';
  stepsWrap.appendChild(h3);

  const grid = document.createElement('div');
  grid.className = 'steps-grid';
  config.axes.forEach((ax) => {
    const steps = (ax.steps && ax.steps.length ? ax.steps : [{ plus: 0.1, minus: 0.1 }]);
    const row = document.createElement('div');
    row.className = 'step-axis-row';

    const label = document.createElement('span');
    label.className = 'step-axis-label';
    label.textContent = ax.letter;
    row.appendChild(label);

    steps.forEach((s, j) => {
      const pair = document.createElement('span');
      pair.className = 'step-pair';

      const minusSign = document.createElement('span');
      minusSign.className = 'sign';
      minusSign.textContent = '−';
      pair.appendChild(minusSign);
      pair.appendChild(makeStepInput(ax.letter, j, 'minus', s.minus));

      const plusSign = document.createElement('span');
      plusSign.className = 'sign';
      plusSign.textContent = '+';
      pair.appendChild(plusSign);
      pair.appendChild(makeStepInput(ax.letter, j, 'plus', s.plus));

      row.appendChild(pair);
    });
    grid.appendChild(row);
  });
  stepsWrap.appendChild(grid);
  box.appendChild(stepsWrap);

  stepsWrap.querySelectorAll('input[data-dir]').forEach(inp => {
    inp.addEventListener('input', () => onStepInput(inp));
  });
}

function onStepInput(inp) {
  const sym = document.getElementById('sym');
  if (!sym || !sym.checked) return;
  const axis = inp.dataset.axis;
  const slot = inp.dataset.slot;
  const otherDir = inp.dataset.dir === 'plus' ? 'minus' : 'plus';
  const other = document.querySelector(`input[data-axis="${axis}"][data-slot="${slot}"][data-dir="${otherDir}"]`);
  if (other) other.value = inp.value;
}

function collectSettings() {
  const axes = config.axes.map((ax, i) => {
    const name = document.querySelector(`[data-i="${i}"][data-field="name"]`).value;
    const current_mA = Number(document.querySelector(`[data-i="${i}"][data-field="current_mA"]`).value);
    const speed_mm_min = Number(document.querySelector(`[data-i="${i}"][data-field="speed_mm_min"]`).value);
    const has_endstop = document.querySelector(`[data-i="${i}"][data-field="has_endstop"]`).checked;
    const steps = (ax.steps || [{ plus: 0.1, minus: 0.1 }]).map((s, j) => {
      const plus = Number(document.querySelector(`input[data-axis="${ax.letter}"][data-slot="${j}"][data-dir="plus"]`).value);
      const minus = Number(document.querySelector(`input[data-axis="${ax.letter}"][data-slot="${j}"][data-dir="minus"]`).value);
      return { plus, minus };
    });
    return { ...ax, name, current_mA, speed_mm_min, has_endstop, steps };
  });
  return {
    axes,
    currents: Object.fromEntries(axes.map(a => [a.letter, a.current_mA])),
    speeds: Object.fromEntries(axes.map(a => [a.letter, a.speed_mm_min])),
  };
}

async function applySettings() {
  await withBusy(async () => {
    try {
      const s = collectSettings();
      await api('/api/settings/apply', { method: 'POST', body: { currents: s.currents, speeds: s.speeds } });
      toast('Токи и скорости применены');
    } catch (e) { toast('Ошибка: ' + e.message, true); }
  });
}

async function saveSettings() {
  await withBusy(async () => {
    try {
      const s = collectSettings();
      config.axes = s.axes;
      await api('/api/config', { method: 'POST', body: config });
      toast('Сохранено в config.json');
      renderAxes();
    } catch (e) { toast('Ошибка: ' + e.message, true); }
  });
}
async function loadMacros() {
  try {
    renderMacros(await api('/api/macros'));
  } catch (e) { /* ignore */ }
}

function renderMacros(list) {
  const ul = document.getElementById('macros');
  ul.replaceChildren();
  if (!list.length) {
    const empty = document.createElement('li');
    empty.className = 'empty';
    empty.textContent = 'Макросов пока нет — добавьте первый ниже.';
    ul.appendChild(empty);
    return;
  }
  for (const m of list) {
    const li = document.createElement('li');

    const span = document.createElement('span');
    span.textContent = m.name;
    const small = document.createElement('small');
    small.textContent = ' (' + Object.entries(m.position || {}).map(([k, v]) => `${k}=${v}`).join(' ') + ')';
    span.appendChild(small);
    li.appendChild(span);

    const actions = document.createElement('span');
    const runBtn = document.createElement('button');
    runBtn.textContent = '▶';
    runBtn.title = 'Запустить макрос';
    runBtn.dataset.run = m.name;
    actions.appendChild(runBtn);
    actions.appendChild(document.createTextNode(' '));
    const delBtn = document.createElement('button');
    delBtn.textContent = '✕';
    delBtn.className = 'secondary';
    delBtn.title = 'Удалить макрос';
    delBtn.dataset.del = m.name;
    actions.appendChild(delBtn);
    li.appendChild(actions);

    ul.appendChild(li);
  }
  ul.querySelectorAll('button[data-run]').forEach(b => b.addEventListener('click', () => runMacro(b.dataset.run)));
  ul.querySelectorAll('button[data-del]').forEach(b => b.addEventListener('click', () => deleteMacro(b.dataset.del)));
}

async function runMacro(name) {
  await withBusy(async () => {
    try {
      const ok = window.confirm(`Запустить макрос «${name}» (переход в сохранённые координаты)? Убедитесь, что начальная точка задана корректно.`);
      if (!ok) return;
      await api('/api/macros/run', { method: 'POST', body: { name, confirm: true } });
      toast('Макрос «' + name + '» запущен');
      setTimeout(pollPosition, 500);
    } catch (e) { toast('Ошибка: ' + e.message, true); }
  });
}

async function deleteMacro(name) {
  await withBusy(async () => {
    try {
      const list = await api('/api/macros');
      const next = list.filter(m => m.name !== name);
      await api('/api/macros', { method: 'POST', body: next });
      renderMacros(next);
    } catch (e) { toast('Ошибка: ' + e.message, true); }
  });
}

async function addMacro() {
  await withBusy(async () => {
    try {
      const name = document.getElementById('macro-name').value.trim();
      if (!name) { toast('Введите название макроса', true); return; }
      // Сохраняем макрос только по свежему успешному опросу позиции.
      const fresh = await api('/api/position');
      if (fresh.stale) {
        toast('Координаты устарели — макрос не сохранён', true);
        return;
      }
      position = fresh.position || {};
      renderPosition();
      const list = await api('/api/macros');
      list.push({ name, position });
      await api('/api/macros', { method: 'POST', body: list });
      renderMacros(list);
      toast('Макрос «' + name + '» сохранён с текущими координатами');
    } catch (e) { toast('Ошибка: ' + e.message, true); }
  });
}

async function connect() {
  await withBusy(async () => {
    try {
      const body = { ip: document.getElementById('ip').value };
      const mock = document.getElementById('mock').checked;
      config.board.ip = body.ip;
      config.board.mock = mock;
      config.board.password = document.getElementById('password').value;
      await api('/api/config', { method: 'POST', body: config });
      const res = await api('/api/connect', { method: 'POST', body });
      const badge = document.getElementById('status');
      badge.textContent = res.mock ? 'эмуляция (без платы)' : ('RRF ' + res.firmware + '.x');
      badge.className = 'badge ok';
      toast('Подключено');
      pollPosition();
    } catch (e) {
      document.getElementById('status').textContent = 'ошибка';
      document.getElementById('status').className = 'badge err';
      toast('Ошибка подключения: ' + e.message, true);
    }
  });
}

async function goHome() {
  await withBusy(async () => {
    try {
      const res = await api('/api/home', { method: 'POST', body: {} });
      const end = res.endstop_axes || [];
      const goto = res.goto_axes || [];
      const msg = (end.length ? 'Команда Home (G28): ' + end.join(' ') + '. ' : '') + (goto.length ? 'Переход в сохранённую позицию: ' + goto.join(' ') : '');
      toast(msg || 'Команда Home выполнена');
      setTimeout(pollPosition, 500);
    } catch (e) { toast('Ошибка: ' + e.message, true); }
  });
}

async function setReference() {
  if (!window.confirm('Задать текущую позицию как нулевую точку (0) для осей без концевика? Это не команда Home.')) return;
  await withBusy(async () => {
    try {
      const axes = (config.axes || []).filter(a => !a.has_endstop).map(a => a.letter);
      if (!axes.length) { toast('Нет осей без концевика', true); return; }
      await api('/api/set_position', { method: 'POST', body: { confirm: true, axes } });
      toast('Нулевая точка задана (G92 → 0): ' + axes.join(' '));
      pollPosition();
    } catch (e) { toast('Ошибка: ' + e.message, true); }
  });
}

async function shutdown(mode) {
  const label = mode === 'go_home'
    ? 'Вернуть двигатели в начальную точку и подготовить к выключению?'
    : 'Оставить двигатели на месте и сделать текущую позицию новой начальной точкой?';
  if (!window.confirm(label)) return;
  await withBusy(async () => {
    try {
      await api('/api/shutdown', { method: 'POST', body: { mode } });
      toast(mode === 'go_home'
        ? 'Возврат в начальную точку выполнен, двигатели отключены'
        : 'Новая начальная точка сохранена, старая — в макрос «предыдущая начальная точка»');
      setTimeout(pollPosition, 500);
    } catch (e) { toast('Ошибка: ' + e.message, true); }
  });
}

document.getElementById('btn-connect').addEventListener('click', connect);
document.getElementById('btn-apply').addEventListener('click', applySettings);
document.getElementById('btn-save').addEventListener('click', saveSettings);
document.getElementById('btn-macro-add').addEventListener('click', addMacro);
document.getElementById('macro-name').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') addMacro();
});
document.getElementById('btn-home').addEventListener('click', goHome);
document.getElementById('btn-set-ref').addEventListener('click', setReference);
document.getElementById('btn-shutdown-home').addEventListener('click', () => shutdown('go_home'));
document.getElementById('btn-shutdown-keep').addEventListener('click', () => shutdown('keep_position'));
document.getElementById('btn-popup').addEventListener('click', () => {
  window.open('/static/position.html', 'coords', 'width=380,height=460');
});

loadConfig().catch(e => toast('Ошибка загрузки конфига: ' + e.message, true));
setInterval(pollPosition, 1000);



