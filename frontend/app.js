const screens = {};
document.querySelectorAll(".screen").forEach((el) => (screens[el.id] = el));

function showScreen(id) {
  Object.values(screens).forEach((el) => el.classList.add("hidden"));
  screens[id].classList.remove("hidden");
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || "GET",
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let data = null;
  try {
    data = await res.json();
  } catch (_) {}
  if (!res.ok) {
    const msg = (data && data.detail) || `Request failed (${res.status})`;
    throw new Error(msg);
  }
  return data;
}

// ---------------------------------------------------------------------------
// Boot sequence
// ---------------------------------------------------------------------------

async function boot() {
  const session = await api("/api/session");
  if (!session.authenticated) {
    showScreen("screen-app-login");
    return;
  }
  const igStatus = await api("/api/ig/status");
  if (!igStatus.authenticated) {
    showScreen("screen-ig-login");
    return;
  }
  const sync = await api("/api/sync/status");
  if (sync.status !== "done" || sync.total_count === 0) {
    showScreen("screen-sync");
    renderSyncScreen(sync);
    return;
  }
  showScreen("screen-swipe");
  initQueue();
}

boot().catch((e) => console.error(e));

// ---------------------------------------------------------------------------
// App password gate
// ---------------------------------------------------------------------------

// Disables a submit button and swaps its label while an async action runs,
// so slow requests (Instagram logins can take several seconds) never look
// like the page just froze.
async function withLoading(button, loadingText, fn) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = loadingText;
  try {
    await fn();
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

document.getElementById("form-app-login").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = document.getElementById("app-login-error");
  err.textContent = "";
  const btn = e.target.querySelector("button");
  await withLoading(btn, "Unlocking…", async () => {
    try {
      await api("/api/login", { method: "POST", body: { password: document.getElementById("app-password").value } });
      await boot();
    } catch (e2) {
      err.textContent = e2.message;
    }
  });
});

// ---------------------------------------------------------------------------
// Instagram login
// ---------------------------------------------------------------------------

document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab-content").forEach((c) => c.classList.add("hidden"));
    btn.classList.add("active");
    document.getElementById(btn.dataset.tab).classList.remove("hidden");
  });
});

document.getElementById("form-sessionid").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = document.getElementById("ig-login-error");
  err.textContent = "";
  const btn = e.target.querySelector("button");
  await withLoading(btn, "Connecting…", async () => {
    try {
      const result = await api("/api/ig/login/session", {
        method: "POST",
        body: { sessionid: document.getElementById("sessionid").value },
      });
      if (result.status === "authenticated") await boot();
      else err.textContent = result.error || "Login failed.";
    } catch (e2) {
      err.textContent = e2.message;
    }
  });
});

document.getElementById("form-password").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = document.getElementById("ig-login-error");
  err.textContent = "";
  const btn = e.target.querySelector("button");
  await withLoading(btn, "Logging in…", async () => {
    try {
      const result = await api("/api/ig/login/password", {
        method: "POST",
        body: {
          username: document.getElementById("ig-username").value,
          password: document.getElementById("ig-password").value,
        },
      });
      if (result.status === "authenticated") { await boot(); return; }
      if (result.status === "need_2fa") { document.getElementById("form-2fa").classList.remove("hidden"); return; }
      err.textContent = result.error || "Login failed.";
    } catch (e2) {
      err.textContent = e2.message;
    }
  });
});

document.getElementById("form-2fa").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = document.getElementById("ig-login-error");
  err.textContent = "";
  const btn = e.target.querySelector("button");
  await withLoading(btn, "Verifying…", async () => {
    try {
      const result = await api("/api/ig/login/2fa", {
        method: "POST",
        body: { code: document.getElementById("ig-2fa-code").value },
      });
      if (result.status === "authenticated") await boot();
      else err.textContent = result.error || "Code rejected.";
    } catch (e2) {
      err.textContent = e2.message;
    }
  });
});

// ---------------------------------------------------------------------------
// Sync
// ---------------------------------------------------------------------------

let syncPollTimer = null;

function renderSyncScreen(state) {
  const summary = document.getElementById("sync-summary");
  const progress = document.getElementById("sync-progress");
  const fill = document.getElementById("sync-progress-fill");
  const text = document.getElementById("sync-progress-text");
  const goBtn = document.getElementById("btn-go-swipe");

  if (state.status === "running") {
    summary.textContent = "";
    progress.classList.remove("hidden");
    if (state.total_count > 0) {
      fill.classList.remove("indeterminate");
      fill.style.width = `${Math.min(100, (state.fetched_count / state.total_count) * 100)}%`;
      text.textContent = `Fetched ${state.fetched_count} of ${state.total_count} so far…`;
    } else {
      fill.classList.add("indeterminate");
      text.textContent = state.fetched_count > 0
        ? `Fetched ${state.fetched_count} so far…`
        : "Starting…";
    }
    goBtn.classList.add("hidden");
    if (!syncPollTimer) syncPollTimer = setInterval(pollSync, 2000);
  } else if (state.status === "done") {
    clearInterval(syncPollTimer);
    syncPollTimer = null;
    progress.classList.add("hidden");
    fill.classList.remove("indeterminate");
    summary.textContent = `Synced ${state.total_count} accounts you follow.`;
    goBtn.classList.remove("hidden");
  } else if (state.status === "error") {
    clearInterval(syncPollTimer);
    syncPollTimer = null;
    progress.classList.add("hidden");
    summary.textContent = `Sync failed: ${state.last_error}`;
    goBtn.classList.add("hidden");
  } else {
    summary.textContent = "Not synced yet.";
    progress.classList.add("hidden");
    goBtn.classList.add("hidden");
  }
}

async function pollSync() {
  const state = await api("/api/sync/status");
  renderSyncScreen(state);
}

async function startSync() {
  showScreen("screen-sync");
  renderSyncScreen({ status: "running" });
  await api("/api/sync/start", { method: "POST" });
}

document.getElementById("btn-sync-start").addEventListener("click", startSync);

document.getElementById("btn-go-swipe").addEventListener("click", () => {
  showScreen("screen-swipe");
  initQueue();
});

// ---------------------------------------------------------------------------
// Swipe
// ---------------------------------------------------------------------------

const cardStack = document.getElementById("card-stack");
const swipeEmpty = document.getElementById("swipe-empty");

// Two-card buffer: topEl/topCard is the live, draggable card; nextEl/nextCard
// sits visually behind it (Tinder-style peek) and is promoted instantly when
// the top card is swiped, so there's never a fetch delay for the reveal.
let topEl = null, topCard = null;
let nextEl = null, nextCard = null;
let dragState = null;
let refilling = false;

function makeCardEl(card, roleClass) {
  const el = document.createElement("div");
  el.className = `swipe-card ${roleClass}`;
  el.innerHTML = `
    <div class="stamp keep">KEEP</div>
    <div class="stamp remove">UNFOLLOW</div>
    <img class="avatar" />
    <div class="username"></div>
    <div class="full-name"></div>
    <div class="badges"></div>
    <a class="profile-link" target="_blank" rel="noopener noreferrer">View on Instagram ↗</a>
  `;
  fillCardEl(el, card);
  return el;
}

function fillCardEl(el, card) {
  el.querySelector(".avatar").src = card.avatar_url;
  el.querySelector(".avatar").style.visibility = "visible";
  el.querySelector(".avatar").onerror = (e) => (e.target.style.visibility = "hidden");
  el.querySelector(".username").textContent = "@" + card.username;
  el.querySelector(".full-name").textContent = card.full_name || "";
  const badges = el.querySelector(".badges");
  badges.innerHTML = "";
  if (card.is_private) badges.innerHTML += '<span class="badge">Private</span>';
  if (card.is_verified) badges.innerHTML += '<span class="badge">Verified</span>';
  el.querySelector(".profile-link").href = `https://www.instagram.com/${encodeURIComponent(card.username)}/`;
}

async function initQueue() {
  const { cards } = await api("/api/queue/peek?limit=2");
  if (topEl) topEl.remove();
  if (nextEl) nextEl.remove();
  topEl = nextEl = null;
  topCard = cards[0] || null;
  nextCard = cards[1] || null;

  swipeEmpty.classList.toggle("hidden", !!topCard);
  if (!topCard) { updateRemaining(); return; }

  if (nextCard) {
    nextEl = makeCardEl(nextCard, "behind");
    cardStack.appendChild(nextEl);
  }
  topEl = makeCardEl(topCard, "top");
  topEl.addEventListener("pointerdown", onPointerDown);
  cardStack.appendChild(topEl);
  updateRemaining();
}

async function updateRemaining() {
  const stats = await api("/api/stats");
  document.getElementById("swipe-remaining").textContent = `${stats.pending} remaining`;
}

async function refillNext() {
  if (refilling || !topCard || nextCard) return;
  refilling = true;
  try {
    const { cards } = await api("/api/queue/peek?limit=2");
    const fresh = cards.find((c) => c.user_id !== topCard.user_id) || null;
    if (fresh && topCard && !nextCard) {
      nextCard = fresh;
      nextEl = makeCardEl(fresh, "behind");
      cardStack.insertBefore(nextEl, topEl);
    }
  } finally {
    refilling = false;
  }
}

async function flyOut(decision) {
  if (!topEl || !topCard) return;
  const outgoingEl = topEl;
  const outgoingCard = topCard;
  outgoingEl.removeEventListener("pointerdown", onPointerDown);

  const dx = decision === "keep" ? 600 : -600;
  outgoingEl.style.transition = "transform 0.35s ease, opacity 0.35s ease";
  outgoingEl.style.transform = `translate(${dx}px, -40px) rotate(${dx / 12}deg)`;
  outgoingEl.style.opacity = "0";
  setTimeout(() => outgoingEl.remove(), 360);

  // Promote the buffered next card instantly — no fetch, no delay.
  topEl = nextEl;
  topCard = nextCard;
  nextEl = null;
  nextCard = null;
  if (topEl) {
    topEl.style.transition = "transform 0.25s ease, opacity 0.25s ease";
    topEl.classList.remove("behind");
    topEl.classList.add("top");
    topEl.addEventListener("pointerdown", onPointerDown);
  } else {
    swipeEmpty.classList.remove("hidden");
  }

  api("/api/decision", { method: "POST", body: { user_id: outgoingCard.user_id, decision } }).catch((e) =>
    console.error("Failed to record decision:", e)
  );
  updateRemaining();
  refillNext();
}

document.getElementById("btn-keep").addEventListener("click", () => flyOut("keep"));
document.getElementById("btn-remove").addEventListener("click", () => flyOut("remove"));

document.getElementById("btn-undo").addEventListener("click", async () => {
  const result = await api("/api/decision/undo", { method: "POST" });
  if (result.user_id) await initQueue();
});

document.addEventListener("keydown", (e) => {
  if (screens["screen-swipe"].classList.contains("hidden")) return;
  if (e.key === "ArrowRight") flyOut("keep");
  if (e.key === "ArrowLeft") flyOut("remove");
});

function onPointerDown(e) {
  if (!topEl) return;
  if (e.target.closest("a")) return; // let the "View on Instagram" link work normally
  topEl.setPointerCapture(e.pointerId);
  dragState = { startX: e.clientX, startY: e.clientY };
  topEl.classList.add("dragging");
  topEl.style.transition = "none";
  topEl.addEventListener("pointermove", onPointerMove);
  topEl.addEventListener("pointerup", onPointerUp);
}

function onPointerMove(e) {
  if (!dragState || !topEl) return;
  const dx = e.clientX - dragState.startX;
  const dy = e.clientY - dragState.startY;
  topEl.style.transform = `translate(${dx}px, ${dy}px) rotate(${dx / 20}deg)`;
  topEl.querySelector(".stamp.keep").style.opacity = Math.max(0, Math.min(1, dx / 100));
  topEl.querySelector(".stamp.remove").style.opacity = Math.max(0, Math.min(1, -dx / 100));
}

function onPointerUp(e) {
  if (!dragState || !topEl) return;
  const dx = e.clientX - dragState.startX;
  topEl.classList.remove("dragging");
  topEl.removeEventListener("pointermove", onPointerMove);
  topEl.removeEventListener("pointerup", onPointerUp);
  dragState = null;
  const threshold = 110;
  if (dx > threshold) flyOut("keep");
  else if (dx < -threshold) flyOut("remove");
  else {
    topEl.style.transition = "transform 0.25s ease";
    topEl.style.transform = "translate(0,0) rotate(0)";
    topEl.querySelector(".stamp.keep").style.opacity = 0;
    topEl.querySelector(".stamp.remove").style.opacity = 0;
  }
}

document.getElementById("btn-review").addEventListener("click", () => openReview());
document.getElementById("btn-go-review-2").addEventListener("click", () => openReview());
document.getElementById("btn-back-swipe").addEventListener("click", () => {
  stopWorkerPolling();
  showScreen("screen-swipe");
  initQueue();
});

// ---------------------------------------------------------------------------
// Review + unfollow worker
// ---------------------------------------------------------------------------

let workerPollTimer = null;

function openReview() {
  showScreen("screen-review");
  refreshReview();
  if (!workerPollTimer) workerPollTimer = setInterval(refreshReview, 5000);
}

function stopWorkerPolling() {
  clearInterval(workerPollTimer);
  workerPollTimer = null;
}

async function refreshReview() {
  const stats = await api("/api/stats");
  const grid = document.getElementById("stats-grid");
  const toUnfollow = stats.remove - stats.unfollowed;
  grid.innerHTML = `
    <div><span class="n">${stats.total}</span><span class="label">Total following</span></div>
    <div><span class="n">${stats.pending}</span><span class="label">Not yet reviewed</span></div>
    <div><span class="n">${stats.keep}</span><span class="label">Kept</span></div>
    <div><span class="n">${toUnfollow}</span><span class="label">Queued to unfollow</span></div>
  `;

  const wstate = await api("/api/worker/status");
  document.getElementById("cfg-daily-cap").value = wstate.daily_cap;
  document.getElementById("cfg-min-delay").value = wstate.min_delay_sec;
  document.getElementById("cfg-max-delay").value = wstate.max_delay_sec;

  const statusEl = document.getElementById("worker-status");
  const lines = [
    wstate.enabled ? "Status: running" : "Status: paused",
    `Unfollowed today: ${wstate.unfollowed_today} / ${wstate.daily_cap}`,
    `Remaining in queue: ${wstate.remaining_to_unfollow}`,
  ];
  if (wstate.last_error) lines.push(`Last error: ${wstate.last_error}`);
  statusEl.innerHTML = lines.map((l) => `<div>${l}</div>`).join("");

  document.getElementById("btn-worker-start").classList.toggle("hidden", wstate.enabled);
  document.getElementById("btn-worker-stop").classList.toggle("hidden", !wstate.enabled);

  const sync = await api("/api/sync/status");
  const lastSyncedEl = document.getElementById("last-synced");
  lastSyncedEl.textContent = sync.last_synced_at
    ? `Last synced ${new Date(sync.last_synced_at * 1000).toLocaleString()}`
    : "";
}

document.getElementById("btn-resync").addEventListener("click", () => {
  stopWorkerPolling();
  startSync();
});

document.getElementById("btn-save-worker-config").addEventListener("click", async () => {
  await api("/api/worker/config", {
    method: "POST",
    body: {
      daily_cap: parseInt(document.getElementById("cfg-daily-cap").value, 10),
      min_delay_sec: parseInt(document.getElementById("cfg-min-delay").value, 10),
      max_delay_sec: parseInt(document.getElementById("cfg-max-delay").value, 10),
    },
  });
  refreshReview();
});

document.getElementById("btn-worker-start").addEventListener("click", async () => {
  await api("/api/worker/config", { method: "POST", body: { enabled: true } });
  refreshReview();
});

document.getElementById("btn-worker-stop").addEventListener("click", async () => {
  await api("/api/worker/config", { method: "POST", body: { enabled: false } });
  refreshReview();
});
