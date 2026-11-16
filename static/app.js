// Логика интерфейса юстировки: общение с Flask-бэкендом по fetch.
let config = null;
let position = {};

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

function renderAxes() {
  const box = document.getElementById('axes');
  box.innerHTML = '';
  for (const ax of config.axes) {
    const card = document.createElement('div');
    card.className = 'axis';
    const steps = (ax.steps && ax.steps.length ? ax.steps : [{ plus: 0.1, minus: 0.1 }]);
    const stepBtns = steps.map(s => `
      <div class="step-group">
        <button class="minus" data-axis="${ax.letter}" data-dir="-1" data-step="${s.minus}">−${fmt(s.minus)}</button>
        <button class="plus" data-axis="${ax.letter}" data-dir="1" data-step="${s.plus}">+${fmt(s.plus)}</button>
      </div>`).join('');
    card.innerHTML = `
      <div class="axis-title">${ax.letter} — ${ax.name || 'ось ' + ax.letter}${ax.has_endstop ? ' <span class="tag">концевик</span>' : ''}</div>
      <div class="axis-pos" id="pos-${ax.letter}">—</div>
      <div class="axis-steps">${stepBtns}</div>`;
    box.appendChild(card);
  }
  box.querySelectorAll('button[data-axis]').forEach(b => {
    b.addEventListener('click', () => moveAxis(b.dataset.axis, Number(b.dataset.dir), Number(b.dataset.step)));
  });
}

function fmt(v) { return Number(v).toString(); }

async function moveAxis(axis, direction, step) {
  try {
    const feedrate = Number(document.getElementById('feedrate').value) || 600;
    await api('/api/move', { method: 'POST', body: { axis, direction, step, feedrate } });
    await pollPosition();
  } catch (e) {
    toast('Ошибка: ' + e.message, true);
  }
}

async function pollPosition() {
  try {
    const data = await api('/api/position');
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
  box.innerHTML = Object.entries(position)
    .map(([k, v]) => `${k}: <b>${Number(v).toFixed(3)}</b> мм`)
    .join(' &nbsp; ');
}

function renderSettings() {
  const box = document.getElementById('settings');
  box.innerHTML = '<table><tr><th>Ось</th><th>Название</th><th>Ток (мА)</th><th>Скорость (мм/мин)</th><th>Концевик</th></tr></table>';
  const table = box.querySelector('table');
  config.axes.forEach((ax, i) => {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${ax.letter}</td>
      <td><input data-i="${i}" data-field="name" value="${ax.name || ''}"></td>
      <td><input data-i="${i}" data-field="current_mA" type="number" value="${ax.current_mA || ''}"></td>
      <td><input data-i="${i}" data-field="speed_mm_min" type="number" value="${ax.speed_mm_min || ''}"></td>
      <td><input data-i="${i}" data-field="has_endstop" type="checkbox" ${ax.has_endstop ? 'checked' : ''}></td>`;
    table.appendChild(tr);
  });
  const stepsWrap = document.createElement('div');
  stepsWrap.className = 'steps-editor';
  stepsWrap.innerHTML = '<h3 style="margin:14px 0 8px;">Шаги (мм, в + и в −)</h3>';
  config.axes.forEach((ax) => {
    const steps = (ax.steps && ax.steps.length ? ax.steps : [{ plus: 0.1, minus: 0.1 }]);
    const rows = steps.map((s, j) => `
      <div class="step-row" data-axis="${ax.letter}">
        <span>−</span><input type="number" step="any" min="0.0001" data-axis="${ax.letter}" data-slot="${j}" data-dir="minus" value="${s.minus}">
        <span>+</span><input type="number" step="any" min="0.0001" data-axis="${ax.letter}" data-slot="${j}" data-dir="plus" value="${s.plus}">
      </div>`).join('');
    stepsWrap.innerHTML += `<div class="step-axis"><b>Ось ${ax.letter}</b>${rows}</div>`;
  });
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
  try {
    const s = collectSettings();
    await api('/api/settings/apply', { method: 'POST', body: { currents: s.currents, speeds: s.speeds } });
    toast('Токи и скорости применены');
  } catch (e) { toast('Ошибка: ' + e.message, true); }
}

async function saveSettings() {
  try {
    const s = collectSettings();
    config.axes = s.axes;
    await api('/api/config', { method: 'POST', body: config });
    toast('Сохранено в config.json');
    renderAxes();
  } catch (e) { toast('Ошибка: ' + e.message, true); }
}

async function loadMacros() {
  try {
    renderMacros(await api('/api/macros'));
  } catch (e) { /* ignore */ }
}

function renderMacros(list) {
  const ul = document.getElementById('macros');
  ul.innerHTML = '';
  for (const m of list) {
    const posText = Object.entries(m.position || {}).map(([k, v]) => `${k}=${v}`).join(' ');
    const li = document.createElement('li');
    li.innerHTML = `<span>${m.name} <small>(${posText})</small></span>
      <span><button data-run="${m.name}">▶</button> <button data-del="${m.name}" class="secondary">✕</button></span>`;
    ul.appendChild(li);
  }
  ul.querySelectorAll('button[data-run]').forEach(b => b.addEventListener('click', () => runMacro(b.dataset.run)));
  ul.querySelectorAll('button[data-del]').forEach(b => b.addEventListener('click', () => deleteMacro(b.dataset.del)));
}

async function runMacro(name) {
  try {
    const ok = window.confirm(`Запустить абсолютный макрос "${name}"? Убедись, что нули и homing корректны.`);
    if (!ok) return;
    await api('/api/macros/run', { method: 'POST', body: { name, confirm: true } });
    toast('Макрос запущен: ' + name);
    setTimeout(pollPosition, 500);
  } catch (e) { toast('Ошибка: ' + e.message, true); }
}

async function deleteMacro(name) {
  const list = await api('/api/macros');
  const next = list.filter(m => m.name !== name);
  await api('/api/macros', { method: 'POST', body: next });
  renderMacros(next);
}

async function addMacro() {
  const name = document.getElementById('macro-name').value.trim();
  if (!name) { toast('Введите название макроса', true); return; }
  await pollPosition();
  const list = await api('/api/macros');
  list.push({ name, position });
  await api('/api/macros', { method: 'POST', body: list });
  renderMacros(list);
  toast('Макрос сохранён как абсолютная позиция: ' + name);
}

async function connect() {
  try {
    const body = { ip: document.getElementById('ip').value };
    const mock = document.getElementById('mock').checked;
    config.board.ip = body.ip;
    config.board.mock = mock;
    config.board.password = document.getElementById('password').value;
    await api('/api/config', { method: 'POST', body: config });
    const res = await api('/api/connect', { method: 'POST', body });
    const badge = document.getElementById('status');
    badge.textContent = res.mock ? 'mock-режим' : ('RRF ' + res.firmware + '.x');
    badge.className = 'badge ok';
    toast('Подключено');
    pollPosition();
  } catch (e) {
    document.getElementById('status').textContent = 'ошибка';
    document.getElementById('status').className = 'badge err';
    toast('Ошибка подключения: ' + e.message, true);
  }
}

async function goHome() {
  try {
    const res = await api('/api/home', { method: 'POST', body: {} });
    const end = res.endstop_axes || [];
    const soft = res.soft_axes || [];
    const msg = (end.length ? 'Физический homing (G28): ' + end.join(' ') + '. ' : '') + (soft.length ? 'Soft home: ' + soft.join(' ') : '');
    toast(msg || 'Хоуминг выполнен');
    setTimeout(pollPosition, 500);
  } catch (e) { toast('Ошибка: ' + e.message, true); }
}

async function shutdown(mode) {
  const label = mode === 'go_home'
    ? 'Вернуть двигатели в хоум и подготовить к выключению?'
    : 'Оставить двигатели на месте и сделать текущую позицию новым хоумом?';
  if (!window.confirm(label)) return;
  try {
    await api('/api/shutdown', { method: 'POST', body: { mode } });
    toast(mode === 'go_home'
      ? 'Возврат в хоум выполнен, двигатели отключены'
      : 'Новый хоум сохранён, старый — в макрос «предыдущий хоум»');
    setTimeout(pollPosition, 500);
  } catch (e) { toast('Ошибка: ' + e.message, true); }
}

document.getElementById('btn-connect').addEventListener('click', connect);
document.getElementById('btn-apply').addEventListener('click', applySettings);
document.getElementById('btn-save').addEventListener('click', saveSettings);
document.getElementById('btn-macro-add').addEventListener('click', addMacro);
document.getElementById('btn-home').addEventListener('click', goHome);
document.getElementById('btn-shutdown-home').addEventListener('click', () => shutdown('go_home'));
document.getElementById('btn-shutdown-keep').addEventListener('click', () => shutdown('keep_position'));
document.getElementById('btn-popup').addEventListener('click', () => {
  window.open('/static/position.html', 'coords', 'width=380,height=460');
});

loadConfig().catch(e => toast('Ошибка загрузки конфига: ' + e.message, true));
setInterval(pollPosition, 1000);

