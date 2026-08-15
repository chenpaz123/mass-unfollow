const screens = {};
document.querySelectorAll(".screen").forEach((el) => (screens[el.id] = el));

const TAB_SCREENS = ["screen-swipe", "screen-review", "screen-history", "screen-settings"];
const tabBar = document.getElementById("tab-bar");

function showScreen(id) {
  Object.values(screens).forEach((el) => el.classList.add("hidden"));
  screens[id].classList.remove("hidden");

  const isTabScreen = TAB_SCREENS.includes(id);
  tabBar.classList.toggle("hidden", !isTabScreen);
  document.body.classList.toggle("has-tabbar", isTabScreen);
  if (isTabScreen) {
    document.querySelectorAll(".tab-bar-btn").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.screen === id);
    });
  }
}

// Switches to one of the four persistent tabs, loading/refreshing that
// tab's data and stopping any polling owned by the tab being left.
function switchTab(id) {
  stopWorkerPolling();
  showScreen(id);
  if (id === "screen-swipe") initQueue();
  else if (id === "screen-review") {
    refreshReview();
    workerPollTimer = setInterval(refreshReview, 5000);
  } else if (id === "screen-history") loadHistory(true);
  else if (id === "screen-settings") loadSettingsScreen();
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

let toastEl = null;
let toastTimer = null;
function showToast(message, isError = true) {
  if (!toastEl) {
    toastEl = document.createElement("div");
    toastEl.id = "toast";
    document.body.appendChild(toastEl);
  }
  toastEl.textContent = message;
  toastEl.className = isError ? "toast error visible" : "toast visible";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toastEl.classList.remove("visible"), 4000);
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
  switchTab("screen-swipe");
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

// Instagram's own device-approval challenge (a push notification the user
// has to go tap "Yes, it was me" on) can keep a login request open far
// longer than a normal login — long enough that Cloudflare/the browser drop
// the connection client-side (Safari surfaces this as a generic "Load
// failed") even though the backend thread is still running and completes
// the login successfully moments later. So instead of trusting the single
// POST's response to carry the outcome, fire it and poll the existing
// status endpoint — same pattern as sync — until the backend's real state
// (set from the same thread, independent of whether the HTTP response ever
// made it back) shows up.
async function waitForIgLogin(startStatus) {
  for (let i = 0; i < 100; i++) {
    const snap = await api("/api/ig/status");
    if (snap.status !== startStatus && snap.status !== "logging_in") return snap;
    await new Promise((r) => setTimeout(r, i < 10 ? 300 : 2000));
  }
  return {
    status: "error",
    error: "Still waiting on Instagram — this can take a while if it's waiting for you to approve a login on your phone. Check back in a minute.",
  };
}

document.getElementById("form-password").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = document.getElementById("ig-login-error");
  err.textContent = "";
  const btn = e.target.querySelector("button");
  await withLoading(btn, "Logging in…", async () => {
    try {
      const startStatus = (await api("/api/ig/status")).status;
      api("/api/ig/login/password", {
        method: "POST",
        body: {
          username: document.getElementById("ig-username").value,
          password: document.getElementById("ig-password").value,
        },
      }).catch(() => {});
      const result = await waitForIgLogin(startStatus);
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
      const startStatus = (await api("/api/ig/status")).status;
      api("/api/ig/login/2fa", {
        method: "POST",
        body: { code: document.getElementById("ig-2fa-code").value },
      }).catch(() => {});
      const result = await waitForIgLogin(startStatus);
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
    summary.textContent = state.total_count > state.fetched_count
      ? `Synced ${state.fetched_count} of ${state.total_count} accounts you follow — Instagram's following list cut off early. Try "Sync following list" again to pick up the rest.`
      : `Synced ${state.total_count} accounts you follow.`;
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
  try {
    await api("/api/sync/start", { method: "POST" });
  } catch (e) {
    renderSyncScreen({ status: "error", last_error: e.message });
  }
}

document.getElementById("btn-sync-start").addEventListener("click", startSync);

document.getElementById("btn-go-swipe").addEventListener("click", () => switchTab("screen-swipe"));

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
  el.dataset.userId = card.user_id;
  el.innerHTML = `
    <div class="stamp keep">KEEP</div>
    <div class="stamp remove">UNFOLLOW</div>
    <img class="avatar" />
    <div class="username"></div>
    <div class="full-name"></div>
    <div class="badges"></div>
    <div class="last-post"></div>
    <div class="follows-back"></div>
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
  el.querySelector(".last-post").textContent = "";
  const fb = el.querySelector(".follows-back");
  fb.textContent = "";
  fb.classList.remove("yes", "no", "pending");
}

function timeAgo(unixSeconds) {
  const days = Math.floor(Math.max(0, Date.now() / 1000 - unixSeconds) / 86400);
  if (days < 1) return "today";
  if (days < 30) return `${days}d ago`;
  const months = Math.floor(days / 30.44);
  if (months < 12) return `${months}mo ago`;
  return `${Math.floor(days / 365.25)}y ago`;
}

function renderLastPost(el, state, lastPostAt) {
  const target = el.querySelector(".last-post");
  target.classList.toggle("pending", state === "loading");
  if (state === "loading") target.textContent = "Checking last post…";
  else if (state === "unknown") target.textContent = "Last post: unknown";
  else if (lastPostAt == null) target.textContent = "No posts found";
  else target.textContent = `Last post ${timeAgo(lastPostAt)}`;
}

// Called both as a prefetch (for the buffered card sitting behind the top
// one, so it's likely already resolved by the time it's promoted — no
// "Checking…" flash) and again once a card actually becomes the visible top
// card. The in-flight promise is cached on the card object itself so those
// two calls for the same card share one request instead of firing twice —
// still one Instagram lookup per account, just started earlier.
async function loadLastPost(el, card) {
  if (card.last_post_checked) {
    renderLastPost(el, "value", card.last_post_at);
    return;
  }
  if (!card._lastPostPromise) {
    card._lastPostPromise = api(`/api/last-post/${card.user_id}`)
      .catch(() => null)
      .finally(() => { card._lastPostPromise = null; });
  }
  renderLastPost(el, "loading");
  const result = await card._lastPostPromise;
  if (el.dataset.userId !== card.user_id) return; // card moved on before this resolved
  if (result && result.checked) {
    card.last_post_checked = true;
    card.last_post_at = result.last_post_at;
    renderLastPost(el, "value", result.last_post_at);
  } else {
    renderLastPost(el, "unknown");
  }
}

function renderFollowsBack(el, state, followsBack) {
  const target = el.querySelector(".follows-back");
  target.classList.remove("yes", "no", "pending");
  if (state === "loading") {
    target.classList.add("pending");
    target.textContent = "Checking follow status…";
  } else if (state === "unknown" || followsBack === null) {
    target.textContent = "";
  } else if (followsBack) {
    target.classList.add("yes");
    target.textContent = "Follows you back";
  } else {
    target.classList.add("no");
    target.textContent = "Doesn't follow you back";
  }
}

// Same prefetch-and-share-in-flight-promise pattern as loadLastPost, kept as
// a separate call so each signal is independently cached and one failing
// doesn't block the other.
async function loadFollowsBack(el, card) {
  if (card.follows_back_checked) {
    renderFollowsBack(el, "value", card.follows_back);
    return;
  }
  if (!card._followsBackPromise) {
    card._followsBackPromise = api(`/api/follows-back/${card.user_id}`)
      .catch(() => null)
      .finally(() => { card._followsBackPromise = null; });
  }
  renderFollowsBack(el, "loading");
  const result = await card._followsBackPromise;
  if (el.dataset.userId !== card.user_id) return; // card moved on before this resolved
  if (result && result.checked) {
    card.follows_back_checked = true;
    card.follows_back = result.follows_back;
    renderFollowsBack(el, "value", result.follows_back);
  } else {
    renderFollowsBack(el, "unknown");
  }
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
    // Prefetch for the buffered card while it's still behind the current
    // one, so it's usually already resolved by the time it's promoted —
    // still one lookup per account, just started a swipe earlier.
    loadLastPost(nextEl, nextCard);
    loadFollowsBack(nextEl, nextCard);
  }
  topEl = makeCardEl(topCard, "top");
  topEl.addEventListener("pointerdown", onPointerDown);
  cardStack.appendChild(topEl);
  loadLastPost(topEl, topCard);
  loadFollowsBack(topEl, topCard);
  updateRemaining();
}

// Celebrates every 100 accounts reviewed (keep + remove decisions), with a
// bigger toast on round thousands. Persisted in localStorage so a page
// reload doesn't replay every milestone already crossed, and undoing a
// decision back below a milestone then re-crossing it again doesn't refire
// the same one twice.
const MILESTONE_STEP = 100;
const MILESTONE_STORAGE_KEY = "mu-last-milestone";

function checkMilestone(reviewedCount) {
  const last = parseInt(localStorage.getItem(MILESTONE_STORAGE_KEY) || "0", 10);
  const milestone = Math.floor(reviewedCount / MILESTONE_STEP) * MILESTONE_STEP;
  if (milestone > 0 && milestone > last) {
    localStorage.setItem(MILESTONE_STORAGE_KEY, String(milestone));
    const bigMilestone = milestone % 1000 === 0;
    showToast(`${bigMilestone ? "🏆" : "🎉"} ${milestone.toLocaleString()} accounts reviewed!`, false);
  }
}

async function updateRemaining() {
  const stats = await api("/api/stats");
  document.getElementById("swipe-remaining").textContent = `${stats.pending} remaining`;
  const reviewed = stats.total - stats.pending;
  document.getElementById("swipe-reviewed").textContent = `${reviewed.toLocaleString()} reviewed`;
  checkMilestone(reviewed);
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
      loadLastPost(nextEl, nextCard);
      loadFollowsBack(nextEl, nextCard);
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
    loadLastPost(topEl, topCard);
    loadFollowsBack(topEl, topCard);
  } else {
    swipeEmpty.classList.remove("hidden");
  }

  try {
    // Awaited deliberately: refillNext() below queries the server for the next
    // pending account, and if that query lands before this write commits, the
    // server still sees outgoingCard as pending and can hand it right back —
    // showing the same account again a swipe or two later.
    await api("/api/decision", { method: "POST", body: { user_id: outgoingCard.user_id, decision } });
  } catch (e) {
    console.error("Failed to record decision:", e);
    showToast(`Couldn't save that decision: ${e.message}`);
  }
  updateRemaining();
  refillNext();
}

document.getElementById("btn-keep").addEventListener("click", () => flyOut("keep"));
document.getElementById("btn-remove").addEventListener("click", () => flyOut("remove"));

document.getElementById("btn-undo").addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  await withLoading(btn, "…", async () => {
    try {
      const result = await api("/api/decision/undo", { method: "POST" });
      if (result.user_id) await initQueue();
      else showToast("Nothing to undo", false);
    } catch (err) {
      showToast(err.message);
    }
  });
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

document.getElementById("btn-review").addEventListener("click", () => switchTab("screen-review"));
document.getElementById("btn-go-review-2").addEventListener("click", () => switchTab("screen-review"));
document.getElementById("btn-back-swipe").addEventListener("click", () => switchTab("screen-swipe"));

document.querySelectorAll(".tab-bar-btn").forEach((btn) => {
  btn.addEventListener("click", () => switchTab(btn.dataset.screen));
});

// ---------------------------------------------------------------------------
// Review + unfollow worker
// ---------------------------------------------------------------------------

let workerPollTimer = null;

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
    <div><span class="n">${stats.unfollowed}</span><span class="label">Unfollowed</span></div>
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
  // Rough estimate, not accounting for today's already-used capacity — good
  // enough for "how long is this backlog", not meant to be precise to the day.
  if (wstate.remaining_to_unfollow > 0 && wstate.daily_cap > 0) {
    const daysLeft = Math.ceil(wstate.remaining_to_unfollow / wstate.daily_cap);
    lines.push(`Estimated time left: ~${daysLeft} day${daysLeft === 1 ? "" : "s"} at this pace`);
  }
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

document.getElementById("btn-apply-reorder").addEventListener("click", async (e) => {
  await withLoading(e.currentTarget, "Reordering…", async () => {
    try {
      await api("/api/queue/reorder", {
        method: "POST",
        body: { mode: document.getElementById("cfg-reorder-mode").value },
      });
      showToast("Swipe order updated", false);
    } catch (err) {
      showToast(err.message);
    }
  });
});

document.getElementById("btn-save-worker-config").addEventListener("click", async (e) => {
  await withLoading(e.currentTarget, "Saving…", async () => {
    try {
      await api("/api/worker/config", {
        method: "POST",
        body: {
          daily_cap: parseInt(document.getElementById("cfg-daily-cap").value, 10),
          min_delay_sec: parseInt(document.getElementById("cfg-min-delay").value, 10),
          max_delay_sec: parseInt(document.getElementById("cfg-max-delay").value, 10),
        },
      });
      await refreshReview();
      showToast("Settings saved", false);
    } catch (err) {
      showToast(err.message);
    }
  });
});

document.getElementById("btn-worker-start").addEventListener("click", async (e) => {
  await withLoading(e.currentTarget, "Starting…", async () => {
    try {
      await api("/api/worker/config", { method: "POST", body: { enabled: true } });
      await refreshReview();
    } catch (err) {
      showToast(err.message);
    }
  });
});

document.getElementById("btn-worker-stop").addEventListener("click", async (e) => {
  await withLoading(e.currentTarget, "Pausing…", async () => {
    try {
      await api("/api/worker/config", { method: "POST", body: { enabled: false } });
      await refreshReview();
    } catch (err) {
      showToast(err.message);
    }
  });
});

// ---------------------------------------------------------------------------
// History
// ---------------------------------------------------------------------------

const HISTORY_PAGE_SIZE = 30;
let historyOffset = 0;
let historyQuery = "";
let historyFilter = "";
let historySearchDebounce = null;

function historyWhenText(acc) {
  const date = acc.decided_at ? new Date(acc.decided_at * 1000).toLocaleDateString() : "";
  if (acc.unfollowed_at) return `Unfollowed ${new Date(acc.unfollowed_at * 1000).toLocaleDateString()}`;
  if (acc.decision === "keep") return `Kept ${date}`;
  if (acc.decision === "remove") return `Queued to unfollow ${date}`;
  return "Not yet reviewed";
}

function historyRowHtml(acc) {
  const when = historyWhenText(acc);

  let actions = "";
  if (acc.unfollowed_at) {
    actions = `<button data-action="refollow" data-id="${acc.user_id}">Refollow</button>`;
  } else if (acc.decision === "keep") {
    actions =
      `<button data-action="decide" data-id="${acc.user_id}" data-decision="remove">Queue unfollow</button>` +
      `<button data-action="decide" data-id="${acc.user_id}" data-decision="pending">Reset</button>`;
  } else if (acc.decision === "remove") {
    actions =
      `<button data-action="decide" data-id="${acc.user_id}" data-decision="keep" class="primary">Keep instead</button>` +
      `<button data-action="decide" data-id="${acc.user_id}" data-decision="pending">Reset</button>`;
  } else {
    actions =
      `<button data-action="decide" data-id="${acc.user_id}" data-decision="keep" class="primary">Keep</button>` +
      `<button data-action="decide" data-id="${acc.user_id}" data-decision="remove">Unfollow</button>`;
  }

  return `
    <div class="history-row" data-row-id="${acc.user_id}">
      <img class="history-avatar" src="${acc.avatar_url}" onerror="this.style.visibility='hidden'" />
      <div class="history-info">
        <div class="history-username">@${escapeHtml(acc.username)}</div>
        <div class="history-meta">${when}</div>
      </div>
      <div class="history-actions">${actions}</div>
    </div>
  `;
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function loadHistory(reset) {
  if (reset) {
    historyOffset = 0;
    document.getElementById("history-list").innerHTML = "";
  }
  try {
    const params = new URLSearchParams({
      q: historyQuery,
      decision: historyFilter,
      limit: HISTORY_PAGE_SIZE,
      offset: historyOffset,
    });
    const { accounts, total } = await api(`/api/accounts?${params}`);
    const list = document.getElementById("history-list");
    list.insertAdjacentHTML("beforeend", accounts.map(historyRowHtml).join(""));
    historyOffset += accounts.length;

    document.getElementById("history-empty").classList.toggle("hidden", total > 0);
    document.getElementById("btn-history-more").classList.toggle("hidden", historyOffset >= total);
  } catch (err) {
    showToast(err.message);
  }
}

document.getElementById("history-search").addEventListener("input", (e) => {
  historyQuery = e.target.value.trim();
  clearTimeout(historySearchDebounce);
  historySearchDebounce = setTimeout(() => loadHistory(true), 300);
});

document.getElementById("history-filter").addEventListener("change", (e) => {
  historyFilter = e.target.value;
  loadHistory(true);
});

document.getElementById("btn-history-more").addEventListener("click", () => loadHistory(false));

document.getElementById("history-list").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-action]");
  if (!btn) return;
  const userId = btn.dataset.id;
  const action = btn.dataset.action;
  await withLoading(btn, "…", async () => {
    try {
      if (action === "refollow") {
        await api(`/api/accounts/${userId}/refollow`, { method: "POST" });
        showToast("Refollowed", false);
      } else {
        await api(`/api/accounts/${userId}/decision`, {
          method: "POST",
          body: { decision: btn.dataset.decision },
        });
        showToast("Updated", false);
      }
      loadHistory(true);
    } catch (err) {
      showToast(err.message);
    }
  });
});

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

async function loadSettingsScreen() {
  try {
    const ig = await api("/api/ig/status");
    document.getElementById("settings-ig-status").textContent = ig.authenticated
      ? `Connected as @${ig.username}`
      : "Not connected";
  } catch (err) {
    showToast(err.message);
  }
  updateThemeButtons();
}

document.getElementById("btn-ig-disconnect").addEventListener("click", async (e) => {
  if (!confirm("Disconnect Instagram? You'll need to log in again to sync or unfollow anything.")) return;
  await withLoading(e.currentTarget, "Disconnecting…", async () => {
    try {
      await api("/api/ig/logout", { method: "POST" });
      showToast("Disconnected", false);
      await boot();
    } catch (err) {
      showToast(err.message);
    }
  });
});

document.getElementById("form-change-password").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = e.target.querySelector("button");
  await withLoading(btn, "Saving…", async () => {
    try {
      await api("/api/settings/app-password", {
        method: "POST",
        body: {
          current_password: document.getElementById("settings-current-password").value,
          new_password: document.getElementById("settings-new-password").value,
        },
      });
      e.target.reset();
      showToast("Password changed", false);
    } catch (err) {
      showToast(err.message);
    }
  });
});

document.getElementById("btn-export-csv").addEventListener("click", async (e) => {
  // Not using the api() helper here — it assumes a JSON response, and a CSV
  // download needs the raw blob plus a filename, not a parsed body.
  await withLoading(e.currentTarget, "Exporting…", async () => {
    try {
      const res = await fetch("/api/export/csv", { credentials: "same-origin" });
      if (!res.ok) throw new Error(`Export failed (${res.status})`);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `mass-unfollow-export-${new Date().toISOString().slice(0, 10)}.csv`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      showToast(err.message);
    }
  });
});

document.getElementById("btn-reset-data").addEventListener("click", async (e) => {
  if (!confirm("This deletes every synced account and all keep/unfollow decisions from this app. " +
    "It does NOT refollow anyone on Instagram. This can't be undone. Continue?")) return;
  await withLoading(e.currentTarget, "Resetting…", async () => {
    try {
      await api("/api/settings/reset-data", { method: "POST" });
      showToast("Data reset", false);
      await boot();
    } catch (err) {
      showToast(err.message);
    }
  });
});

// Theme
function applyTheme(theme) {
  if (theme === "dark" || theme === "light") {
    document.documentElement.setAttribute("data-theme", theme);
  } else {
    document.documentElement.removeAttribute("data-theme");
  }
  localStorage.setItem("mu-theme", theme);
  updateThemeButtons();
  updateThemeColorMeta();
}

function updateThemeColorMeta() {
  const isLight = getComputedStyle(document.documentElement).colorScheme.trim() === "light";
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", isLight ? "#f4f5f7" : "#0f1115");
}

function updateThemeButtons() {
  const current = localStorage.getItem("mu-theme") || "system";
  document.querySelectorAll(".theme-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.theme === current);
  });
}

document.querySelectorAll(".theme-btn").forEach((btn) => {
  btn.addEventListener("click", () => applyTheme(btn.dataset.theme));
});
updateThemeColorMeta();
window.matchMedia("(prefers-color-scheme: light)").addEventListener("change", updateThemeColorMeta);

// ---------------------------------------------------------------------------
// PWA install support
// ---------------------------------------------------------------------------

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch((e) => console.error("SW registration failed:", e));
  });
}

// ---------------------------------------------------------------------------
// Update notification: detects when the server is running newer code than
// what this page loaded — the common case being a phone that had the PWA
// open/backgrounded across a deploy and never reloaded on its own.
// ---------------------------------------------------------------------------

let knownVersion = null;
let updateBannerDismissed = false;
let versionSource = null;

function startVersionStream() {
  if (versionSource) versionSource.close();
  versionSource = new EventSource("/api/version/stream");
  versionSource.onmessage = (ev) => {
    const version = ev.data;
    if (knownVersion === null) {
      knownVersion = version;
    } else if (version !== knownVersion && !updateBannerDismissed) {
      document.getElementById("update-banner").classList.remove("hidden");
    }
  };
  // onerror fires on every drop, including ones EventSource will silently
  // auto-reconnect from (network blips) — nothing to do here, that's expected.
}

document.getElementById("btn-update-reload").addEventListener("click", async () => {
  if ("serviceWorker" in navigator) {
    const regs = await navigator.serviceWorker.getRegistrations();
    await Promise.all(regs.map((r) => r.unregister()));
  }
  if ("caches" in window) {
    const keys = await caches.keys();
    await Promise.all(keys.map((k) => caches.delete(k)));
  }
  location.reload();
});

document.getElementById("btn-update-dismiss").addEventListener("click", () => {
  updateBannerDismissed = true;
  document.getElementById("update-banner").classList.add("hidden");
});

startVersionStream();
// Safety net: if the connection got fully torn down while backgrounded
// (mobile browsers can do this more aggressively than a plain network blip),
// make sure it's re-established as soon as the app is foregrounded again.
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && (!versionSource || versionSource.readyState === EventSource.CLOSED)) {
    startVersionStream();
  }
});
