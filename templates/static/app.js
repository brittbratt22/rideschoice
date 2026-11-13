let csrfToken = '';
let currentUser = null;
let bookingsEnabled = false;
let deferredInstallPrompt = null;
let pollTimer = null;
let knownRideStates = null;

const $ = (id) => document.getElementById(id);

if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(() => {});
  });
}

window.addEventListener('beforeinstallprompt', (e) => {
  e.preventDefault();
  deferredInstallPrompt = e;
  $('btn-install').classList.remove('hidden');
});

$('btn-install').addEventListener('click', async () => {
  if (!deferredInstallPrompt) return;
  deferredInstallPrompt.prompt();
  await deferredInstallPrompt.userChoice;
  deferredInstallPrompt = null;
  $('btn-install').classList.add('hidden');
});

function updateNotifyButton() {
  if (!('Notification' in window) || !currentUser) {
    $('btn-notify').classList.add('hidden');
    return;
  }
  if (Notification.permission === 'default') {
    $('btn-notify').classList.remove('hidden');
  } else {
    $('btn-notify').classList.add('hidden');
  }
}

$('btn-notify').addEventListener('click', async () => {
  if (!('Notification' in window)) return;
  const perm = await Notification.requestPermission();
  updateNotifyButton();
  if (perm === 'granted') {
    showAlert('Device notifications enabled.', false);
  }
});

function notifyDevice(title, body) {
  showAlert(`${title}: ${body}`, false);
  if ('Notification' in window && Notification.permission === 'granted') {
    try {
      new Notification(title, { body, icon: '/static/icon.svg' });
    } catch (_) {}
  }
}

function showAlert(msg, isError = true) {
  const box = $('alert-box');
  box.textContent = msg;
  box.className = isError ? 'alert alert-error' : 'alert alert-success';
  box.classList.remove('hidden');
  setTimeout(() => box.classList.add('hidden'), 5000);
}

async function api(path, method = 'GET', body = null) {
  const opts = { method, headers: {} };
  if (method !== 'GET') {
    opts.headers['Content-Type'] = 'application/json';
    opts.headers['X-CSRF-Token'] = csrfToken;
    if (body) opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || 'Request failed');
  return data;
}

function startPolling() {
  stopPolling();
  $('live-dot').classList.remove('hidden');
  pollTimer = setInterval(() => {
    if (document.hidden || !currentUser) return;
    loadRides(true);
    if (currentUser.role === 'passenger') {
      loadDrivers(true);
    } else if (currentUser.role === 'admin') {
      loadAdminDrivers(true);
    }
  }, 5000);
}

function stopPolling() {
  $('live-dot').classList.add('hidden');
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

document.addEventListener('visibilitychange', () => {
  if (!document.hidden && currentUser) {
    loadRides(true);
    if (currentUser.role === 'passenger') loadDrivers(true);
  }
});

async function loadSession() {
  try {
    const s = await api('/api/session');
    csrfToken = s.csrf;
    currentUser = s.user;
    bookingsEnabled = s.bookings_enabled;
    knownRideStates = null;
    renderApp();
  } catch (err) {
    showAlert(err.message);
  }
}

function renderApp() {
  ['auth-section', 'passenger-section', 'driver-section', 'admin-section', 'rides-section', 'nav-user', 'booking-banner']
    .forEach(id => $(id).classList.add('hidden'));

  updateNotifyButton();

  if (!currentUser) {
    stopPolling();
    $('auth-section').classList.remove('hidden');
    return;
  }

  $('nav-user').classList.remove('hidden');
  $('user-badge').textContent = `${currentUser.name} (${currentUser.role})`;
  $('rides-section').classList.remove('hidden');

  if (!bookingsEnabled && currentUser.role === 'passenger') {
    $('booking-banner').classList.remove('hidden');
    $('btn-book').disabled = true;
  } else {
    $('btn-book').disabled = false;
  }

  if (currentUser.role === 'passenger') {
    $('passenger-section').classList.remove('hidden');
    loadDrivers();
  } else if (currentUser.role === 'driver') {
    $('driver-section').classList.remove('hidden');
    const msg = $('driver-approval-msg');
    const btn = $('btn-toggle-avail');
    if (!currentUser.approved) {
      msg.textContent = `Vehicle: ${currentUser.vehicle} — Pending operator approval.`;
      btn.disabled = true;
      btn.textContent = 'Awaiting Approval';
    } else {
      msg.textContent = `Vehicle: ${currentUser.vehicle} — Status: ${currentUser.available ? 'Online' : 'Offline'}`;
      btn.disabled = false;
      btn.textContent = currentUser.available ? 'Go Offline' : 'Go Online';
    }
  } else if (currentUser.role === 'admin') {
    $('admin-section').classList.remove('hidden');
    loadAdminDrivers();
  }

  loadRides();
  startPolling();
}

async function loadDrivers(silent = false) {
  try {
    const data = await api('/api/drivers');
    const sel = $('book-driver');
    const previousSelection = sel.value;
    sel.innerHTML = '';
    if (!data.drivers.length) {
      sel.innerHTML = '<option value="">No drivers available right now</option>';
      return;
    }
    data.drivers.forEach(d => {
      const opt = document.createElement('option');
      opt.value = d.id;
      opt.textContent = `${d.name} (${d.vehicle})`;
      sel.appendChild(opt);
    });
    if (previousSelection && [...sel.options].some(o => o.value === previousSelection)) {
      sel.value = previousSelection;
    }
  } catch (err) {
    if (!silent) showAlert(err.message);
  }
}

async function loadAdminDrivers(silent = false) {
  try {
    const data = await api('/api/admin/drivers');
    const list = $('admin-drivers-list');
    list.innerHTML = '';
    if (!data.drivers.length) {
      list.innerHTML = '<p class="muted">No drivers registered.</p>';
      return;
    }
    data.drivers.forEach(d => {
      const item = document.createElement('div');
      item.className = 'list-item flex-between';
      const info = document.createElement('div');
      info.innerHTML = `<strong>${escapeHtml(d.name)}</strong> (${escapeHtml(d.email)})<br><span class="muted">Vehicle: ${escapeHtml(d.vehicle)} | Status: ${d.approved ? 'Approved' : 'Pending'}</span>`;
      const btn = document.createElement('button');
      btn.className = d.approved ? 'btn-danger' : 'btn-primary';
      btn.textContent = d.approved ? 'Revoke' : 'Approve';
      btn.onclick = async () => {
        try {
          await api(`/api/admin/drivers/${d.id}/approve`, 'POST', { approved: !d.approved });
          showAlert('Driver status updated.', false);
          loadAdminDrivers();
        } catch (err) {
          showAlert(err.message);
        }
      };
      item.append(info, btn);
      list.appendChild(item);
    });
  } catch (err) {
    if (!silent) showAlert(err.message);
  }
}

function checkRideNotifications(rides) {
  const nextMap = new Map();
  rides.forEach(r => nextMap.set(r.id, r.status));

  if (knownRideStates !== null) {
    rides.forEach(r => {
      const prev = knownRideStates.get(r.id);
      if (prev === undefined && r.status === 'requested' && currentUser.role === 'driver') {
        notifyDevice('New Ride Request', `${r.passenger_name}: ${r.pickup} -> ${r.destination}`);
      } else if (prev !== undefined && prev !== r.status) {
        const label = r.status.replace('_', ' ');
        notifyDevice('Ride Updated', `Trip to ${r.destination} is now ${label}.`);
      }
    });
  }
  knownRideStates = nextMap;
}

async function loadRides(silent = false) {
  try {
    const data = await api('/api/rides');
    checkRideNotifications(data.rides);

    const list = $('rides-list');
    list.innerHTML = '';
    if (!data.rides.length) {
      list.innerHTML = '<p class="muted">No rides found.</p>';
      return;
    }
    data.rides.forEach(r => {
      const item = document.createElement('div');
      item.className = 'list-item flex-between';

      const info = document.createElement('div');
      info.innerHTML = `
        <span class="badge status-${r.status}">${r.status.replace('_', ' ').toUpperCase()}</span>
        <strong>${escapeHtml(r.pickup)}</strong> &rarr; <strong>${escapeHtml(r.destination)}</strong><br>
        <span class="muted">Passenger: ${escapeHtml(r.passenger_name)} | Driver: ${escapeHtml(r.driver_name)} (${escapeHtml(r.vehicle)})</span>
      `;

      const actions = document.createElement('div');
      actions.className = 'btn-group';

      const addActBtn = (label, actionName, cls = 'btn-primary') => {
        const b = document.createElement('button');
        b.className = cls;
        b.textContent = label;
        b.onclick = async () => {
          try {
            await api(`/api/rides/${r.id}/action`, 'POST', { action: actionName });
            showAlert(`Ride ${actionName}ed.`, false);
            loadRides();
            if (currentUser.role === 'passenger') loadDrivers();
          } catch (err) {
            showAlert(err.message);
          }
        };
        actions.appendChild(b);
      };

      if (currentUser.role === 'driver') {
        if (r.status === 'requested') {
          addActBtn('Accept', 'accept', 'btn-primary');
          addActBtn('Decline', 'decline', 'btn-danger');
        } else if (r.status === 'accepted') {
          addActBtn('Start Trip', 'start', 'btn-primary');
          addActBtn('Cancel', 'cancel', 'btn-danger');
        } else if (r.status === 'in_progress') {
          addActBtn('Complete Trip', 'complete', 'btn-primary');
        }
      } else if (currentUser.role === 'passenger' || currentUser.role === 'admin') {
        if (r.status === 'requested' || r.status === 'accepted') {
          addActBtn('Cancel Ride', 'cancel', 'btn-danger');
        }
      }

      item.append(info, actions);
      list.appendChild(item);
    });
  } catch (err) {
    if (!silent) showAlert(err.message);
  }
}

function escapeHtml(str) {
