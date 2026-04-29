/* ===== Smart Plug Dashboard – main.js ===== */

const POLL_INTERVAL = 15000; // 15 s (matches ESP32 publish rate)

let chartInstance = null;
let currentHistFeed = 'voltage';

// ---- Toast ----
function showToast(msg, type = 'ok') {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = `toast show ${type}`;
  setTimeout(() => { t.className = 'toast'; }, 3200);
}

// ---- Fetch wrapper ----
async function apiPost(url, body) {
  try {
    const r = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    return await r.json();
  } catch (e) {
    showToast('Network error', 'err');
    return { success: false };
  }
}

async function apiDelete(url) {
  const r = await fetch(url, { method: 'DELETE' });
  return await r.json();
}

// ---- Dashboard polling ----
async function fetchDashboard() {
  try {
    const data = await (await fetch('/api/dashboard')).json();

    setText('valVoltage', data.voltage?.value ?? '--');
    setText('valCurrent', data.current?.value ?? '--');
    setText('valPower',   data.power?.value   ?? '--');
    setText('valEnergy',  data.energy?.value  ?? '--');
    setText('valCost',    data.cost?.value    ?? '--');

    // Status badge
    const status = (data.status?.value || '').toUpperCase();
    const badge = document.getElementById('statusBadge');
    const dot   = badge.querySelector('.pulse-dot');
    const txt   = document.getElementById('statusText');
    if (status === 'ON') {
      dot.className = 'pulse-dot';
      txt.textContent = 'ON – Active';
      badge.style.borderColor = 'rgba(34,197,94,0.4)';
      updateToggle(true);
    } else if (status === 'OFF') {
      dot.className = 'pulse-dot off';
      txt.textContent = 'OFF – Idle';
      badge.style.borderColor = 'rgba(239,68,68,0.4)';
      updateToggle(false);
    } else {
      txt.textContent = 'Unknown';
    }

    // Alert banner
    const alert = data.alerts?.value || 'Normal';
    const banner = document.getElementById('alertBanner');
    if (alert && alert !== 'Normal') {
      banner.textContent = '⚠️ ' + alert;
      banner.style.display = 'block';
    } else {
      banner.style.display = 'none';
    }

    // Last updated
    const ts = data.voltage?.created_at;
    document.getElementById('lastUpdated').textContent =
      ts ? 'Last: ' + new Date(ts).toLocaleTimeString() : '--';

  } catch (e) {
    console.error('Dashboard fetch error', e);
  }
}

function setText(id, val) {
  const el = document.getElementById(id);
  if (!el) return;
  if (el.textContent !== String(val)) {
    el.textContent = val;
    el.classList.add('flash');
    setTimeout(() => el.classList.remove('flash'), 600);
  }
}

function updateToggle(isOn) {
  const toggle = document.getElementById('relayToggle');
  const label  = document.getElementById('toggleLabel');
  toggle.checked = isOn;
  label.textContent = isOn ? 'ON' : 'OFF';
  label.style.color = isOn ? 'var(--green)' : 'var(--red)';
}

// ---- Relay ----
async function setRelay(state) {
  const res = await apiPost('/api/relay', { state });
  if (res.success) {
    showToast(`Relay turned ${state}`, 'ok');
    updateToggle(state === 'ON');
  } else {
    showToast('Failed: ' + (res.error || '?'), 'err');
  }
}

document.getElementById('relayToggle').addEventListener('change', function () {
  setRelay(this.checked ? 'ON' : 'OFF');
});

// ---- Timer ----
async function setTimer() {
  const sec = parseInt(document.getElementById('timerSeconds').value);
  if (!sec || sec < 1) { showToast('Enter valid seconds', 'err'); return; }
  const res = await apiPost('/api/timer', { seconds: sec });
  if (res.success) showToast(`Timer set: ${sec}s`, 'ok');
  else showToast('Timer failed', 'err');
}

function quickTimer(sec) {
  document.getElementById('timerSeconds').value = sec;
  setTimer();
}

async function cancelTimer() {
  const res = await apiPost('/api/timer', { seconds: 0 });
  if (res.success) showToast('Timer cancelled', 'ok');
}

// ---- Safety Reset ----
async function safetyReset() {
  const res = await apiPost('/api/reset', {});
  if (res.success) showToast('Safety reset sent ✓', 'ok');
  else showToast('Reset failed', 'err');
}

// ---- Scheduler ----
async function addSchedule() {
  const label  = document.getElementById('schedLabel').value.trim();
  const time   = document.getElementById('schedTime').value;
  const action = document.getElementById('schedAction').value;
  if (!time) { showToast('Pick a date & time', 'err'); return; }
  const res = await apiPost('/api/schedules', {
    label: label || action + ' scheduled',
    trigger_time: time,
    action
  });
  if (res.success) {
    showToast('Schedule added ✓', 'ok');
    document.getElementById('schedLabel').value = '';
    document.getElementById('schedTime').value = '';
    fetchSchedules();
  } else {
    showToast('Error: ' + (res.error || '?'), 'err');
  }
}

async function deleteSchedule(id) {
  await apiDelete(`/api/schedules/${id}`);
  fetchSchedules();
}

async function clearDoneSchedules() {
  await apiPost('/api/schedules/clear-done', {});
  fetchSchedules();
}

async function fetchSchedules() {
  const list = document.getElementById('scheduleList');
  try {
    const data = await (await fetch('/api/schedules')).json();
    if (!data.length) {
      list.innerHTML = '<div class="empty-msg">No schedules yet.</div>';
      return;
    }
    list.innerHTML = data.map(s => `
      <div class="sched-item ${s.done ? 'done' : ''}">
        <div class="sched-info">
          <div class="sched-label">${escHtml(s.label || s.action)}</div>
          <div class="sched-time">
            ${s.trigger_time}
            ${s.done ? ' — Done at ' + (s.triggered_at || '?') : ''}
          </div>
        </div>
        <div class="sched-right">
          <span class="sched-action-badge ${s.action}">${s.action}</span>
          <button class="sched-del" onclick="deleteSchedule(${s.id})" title="Delete">✕</button>
        </div>
      </div>
    `).join('');
  } catch (e) {
    list.innerHTML = '<div class="empty-msg">Failed to load schedules.</div>';
  }
}

function escHtml(str) {
  return str.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ---- History Chart ----
async function loadHistory(feed, btn) {
  currentHistFeed = feed;
  // Update tab buttons
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');

  const data = await (await fetch(`/api/history/${feed}?limit=20`)).json();
  if (!data.length) return;

  const labels = data.map(d => {
    const dt = new Date(d.created_at);
    return dt.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }).reverse();
  const values = data.map(d => parseFloat(d.value)).reverse();

  const unitMap = {
    voltage: 'V', current: 'A', power: 'W', energy: 'kWh', cost: '₹'
  };

  const ctx = document.getElementById('historyChart').getContext('2d');
  if (chartInstance) chartInstance.destroy();

  chartInstance = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [{
        label: feed.charAt(0).toUpperCase() + feed.slice(1) + ' (' + (unitMap[feed] || '') + ')',
        data: values,
        borderColor: '#6c63ff',
        backgroundColor: 'rgba(108,99,255,0.12)',
        borderWidth: 2.5,
        pointBackgroundColor: '#00d4ff',
        pointRadius: 4,
        tension: 0.4,
        fill: true,
      }]
    },
    options: {
      responsive: true,
      plugins: {
        legend: { labels: { color: '#94a3b8', font: { family: 'Inter' } } },
        tooltip: { backgroundColor: '#1a2235', titleColor: '#e2e8f0', bodyColor: '#94a3b8' }
      },
      scales: {
        x: { ticks: { color: '#64748b' }, grid: { color: 'rgba(255,255,255,0.05)' } },
        y: { ticks: { color: '#64748b' }, grid: { color: 'rgba(255,255,255,0.05)' } }
      }
    }
  });
}

// ---- Flash animation ----
const style = document.createElement('style');
style.textContent = `
  @keyframes flash {
    0%   { opacity: 0.4; transform: scale(1.06); }
    100% { opacity: 1;   transform: scale(1); }
  }
  .flash { animation: flash 0.5s ease; }
`;
document.head.appendChild(style);

// ---- Cost History ----
const COST_PAGE_SIZE = 20;
let costPage = 0;

async function fetchCostHistory() {
  const tbody = document.getElementById('costTableBody');
  const offset = costPage * COST_PAGE_SIZE;
  try {
    const data = await (await fetch(`/api/cost-history?limit=${COST_PAGE_SIZE}&offset=${offset}`)).json();

    if (!data.length && costPage === 0) {
      tbody.innerHTML = '<tr><td colspan="4" class="empty-msg" style="padding:16px;color:var(--muted)">No cost records yet. Data is saved every 60 s when the value changes.</td></tr>';
      document.getElementById('costRowCount').textContent = '0 records';
      document.getElementById('btnPrevPage').disabled = true;
      document.getElementById('btnNextPage').disabled = true;
      return;
    }

    tbody.innerHTML = data.map((row, i) => `
      <tr>
        <td class="td-num">${offset + i + 1}</td>
        <td>${escHtml(row.recorded_at)}</td>
        <td class="td-cost">₹ ${parseFloat(row.cost).toFixed(4)}</td>
        <td class="td-energy">${row.energy !== null ? parseFloat(row.energy).toFixed(4) + ' kWh' : '—'}</td>
      </tr>
    `).join('');

    document.getElementById('pageIndicator').textContent = `Page ${costPage + 1}`;
    document.getElementById('btnPrevPage').disabled = costPage === 0;
    document.getElementById('btnNextPage').disabled = data.length < COST_PAGE_SIZE;

    // Total records hint
    document.getElementById('costRowCount').textContent =
      `Showing ${offset + 1}–${offset + data.length}`;

  } catch (e) {
    tbody.innerHTML = '<tr><td colspan="4" class="empty-msg">Failed to load.</td></tr>';
  }
}

function costPageChange(dir) {
  const next = costPage + dir;
  if (next < 0) return;
  costPage = next;
  fetchCostHistory();
}

async function clearCostHistory() {
  if (!confirm('Delete all saved cost history records?')) return;
  const res = await apiDelete('/api/cost-history');
  if (res.success) {
    showToast('Cost history cleared', 'ok');
    costPage = 0;
    fetchCostHistory();
  } else {
    showToast('Failed to clear', 'err');
  }
}

// ---- Init ----
fetchDashboard();
fetchSchedules();
fetchCostHistory();
loadHistory('voltage', document.querySelector('.tab-btn.active'));

setInterval(fetchDashboard, POLL_INTERVAL);
setInterval(fetchSchedules, 30000);
setInterval(fetchCostHistory, 60000);
setInterval(() => loadHistory(currentHistFeed, null), 60000);
