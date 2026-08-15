# Mass Unfollow

A self-hosted, Tinder-style tool for reviewing everyone you follow on Instagram
one at a time — swipe right to keep, left to unfollow — then let a slow,
throttled background worker actually do the unfollows over time so the
account doesn't get flagged.

**Important:** Instagram has no official API for managing follows on a
personal account. This app talks to Instagram's private mobile API via
[instagrapi](https://github.com/subzeroid/instagrapi), the same way most
"unfollow" tools work. That's against Instagram's Terms of Service and carries
a real risk of a temporary action block or ban if you unfollow too fast. The
built-in daily cap and randomized delay exist to keep the risk low — don't
turn them off or push them to extremes (see [Safe pacing](#safe-pacing) below).

## How it works

- **Backend**: Python/FastAPI, talks to Instagram via instagrapi, stores your
  following list and your keep/unfollow decisions in a local SQLite database
  (`./data/app.db`).
- **Frontend**: a single-page app with a persistent bottom tab bar — **Swipe**,
  **Queue**, **History**, **Settings** — served by the same app. Installable
  to a phone's home screen as a PWA, with light/dark/system theming and a
  banner that shows up automatically when you're viewing a stale version after
  a deploy.
- **Unfollow worker**: a background loop that only processes the "unfollow"
  pile, one account every 45–180 seconds (configurable), capped at 150/day by
  default. Runs continuously server-side across as many days as it takes to
  clear the backlog — pause/resume any time from the Queue tab.
- Everything — your Instagram session, the database, cached avatars — stays
  on your own server, in `./data`.

## The four tabs

- **Swipe** — the main flow. Drag a card (or use the ♥ / ✕ buttons, or ← / →
  arrow keys) to decide keep or unfollow, one account at a time. The next card
  peeks from behind the current one; deciding promotes it instantly with no
  loading delay. Each card also shows **last post date** (fetched lazily and
  cached the first time you see that card, so it's one Instagram lookup per
  account you actually view — a quick way to spot dead/inactive accounts) and
  a **View on Instagram** link to double-check before deciding. Undo reverts
  your last decision.
- **Queue** — stats (total / not yet reviewed / kept / queued to unfollow),
  a **Resync following list** button to pick up new follows later, and the
  unfollow worker controls (daily cap, delay range, start/pause, live
  progress).
- **History** — every account you've made a decision on, searchable and
  filterable by decision. Lets you flip a decision back and forth (kept →
  queue for unfollow → reset to pending) for anything not yet actually
  unfollowed, and **Refollow** for anything this app already unfollowed (calls
  Instagram's follow API for real and clears the record).
- **Settings** — see your connected Instagram username and disconnect it,
  change the app password, switch theme, and a danger-zone **Reset all data**
  (wipes this app's local records only — never touches Instagram, already-
  unfollowed accounts stay unfollowed).

## 1. Run it on your server

```bash
cp .env.example .env
# edit .env: set APP_PASSWORD and SECRET_KEY (the file explains how)
docker compose up -d --build
```

This publishes the app on port `8000` on the server itself (same pattern this
deployment already uses for n8n/portainer/jenkins) — reachable at
`http://<server-lan-ip>:8000` on your home network, and at
`http://172.17.0.1:8000` from inside any container on the default `bridge`
network (including `cloudflared`, going by the routes already configured for
`n8n` and `home`). It is **not** exposed to the internet directly — only
through the tunnel, once you add the route below — but note it's reachable
by anything on your LAN as-is, so get `APP_PASSWORD` and Cloudflare Access set
before leaving it running.

Check it's alive: `curl http://127.0.0.1:8000/api/session` (run on the
server) should return `{"authenticated":false}`.

## 2. Add the route in Cloudflare

In Zero Trust → Networks → Tunnels → your tunnel → **Published application
routes**:

1. **+ Add a published application route**.
2. Domain: `unfollow.chenpaz.cc` (or whatever subdomain you want).
3. Service: **Type** `HTTP`, **URL** `172.17.0.1:8000` — same pattern as your
   `n8n.chenpaz.cc` and `home.chenpaz.cc` routes.
4. Save. No restart needed, cloudflared picks it up automatically.

## 3. Lock it down with Cloudflare Access

Since this holds your Instagram session, gate the hostname with a Zero Trust
Access policy so only you can reach it:

1. Zero Trust → **Access → Applications → Add an application** → **Self-hosted
   and private** → **Public DNS** (this hostname is a public DNS record via
   the tunnel route above, so that's the right application type — not
   "Private destinations").
2. Select `unfollow.chenpaz.cc` from your zone.
3. Add a policy: **Include** → **Emails** → your email address only.
4. Save. Now visiting the URL requires a Cloudflare login (email OTP or
   whatever identity provider you have configured) *before* it ever reaches
   the app. The app's own `APP_PASSWORD` login is a second, independent layer
   behind that.

## 4. First run

1. Visit `https://unfollow.chenpaz.cc`, enter your `APP_PASSWORD`.
2. **Connect Instagram** — use **Username / Password** (the default tab):
   supports 2FA via an authenticator app (Google Authenticator, Authy, etc —
   TOTP codes). If prompted, open the app and enter the current 6-digit code.
   SMS-delivered codes are not supported — if your account only has SMS 2FA,
   add an authenticator app under Instagram → Settings → Two-factor
   authentication first. If Instagram throws a security checkpoint (different
   from 2FA), the app will tell you to clear it by logging in normally via
   the app/website first, then retry here.

   The **Session ID** tab exists but generally doesn't work: a `sessionid`
   cookie copied from your browser was issued for a *web* login, while this
   app talks to Instagram's *mobile* private API — Instagram usually rejects
   browser-issued sessions there (shows up as a "467" error). Stick to
   Username/Password; the session it creates is saved locally afterward so
   you won't need to log in again unless it expires.
3. **Sync following list** — pulls everyone you follow, with a live progress
   count. Takes a few minutes for a large following count; only needs to be
   done once (use **Resync following list** on the Queue tab anytime to pick
   up new follows — existing decisions are preserved).
4. **Swipe** through your following list — see [The four tabs](#the-four-tabs)
   above.
5. On the **Queue** tab, tune the daily cap / delay if you want, and hit
   **Start unfollow queue**. It runs server-side even if you close the
   browser or your phone locks — come back anytime to check progress or
   pause it.

## Safe pacing

Instagram doesn't publish official limits, but community-tested consensus
(as of 2026) for an **established account**:

- **Daily cap**: 100–150/day is generally safe; ~200/day is where reports of
  action blocks climb sharply. The app's default (150) sits at the top of
  that range — reasonable, but not much margin. For a large backlog cleared
  over weeks, consider starting at 100/day for the first few days and raising
  it only if nothing goes wrong.
- **Delay between unfollows**: 40–60 seconds minimum; the app's default range
  (45–180s, randomized) already respects that with good variance, which
  matters as much as the average — a metronome-steady interval looks more
  automated than natural jitter.
- If the account is newer (under ~3 months old), stay much lower — 20–50/day.
- Unfollowing in bulk is watched more closely than following, since a mass-
  follow-then-mass-unfollow pattern is a textbook bot signature.

## Notes / limits

- The unfollow worker is intentionally slow. At the 150/day default, clearing
  a large "unfollow" pile takes days — that's by design, not a bug. Marking
  accounts to unfollow (swiping, or from History) has no cap at all; only the
  worker that actually executes them is throttled.
- Avatars are fetched from Instagram's CDN on first view and cached to
  `./data/avatars`. Last-post dates are cached permanently once fetched too
  (a "no posts" result is cached; a failed/rate-limited lookup is not, so it
  retries next time you see that card).
- Changing the app password from Settings replaces the `.env` value — it's
  hashed and stored in the database from that point on.
- If you ever want to start over, stop the container and delete `./data`
  (this forgets your Instagram session and all decisions) — or use
  **Reset all data** in Settings for just the account records, keeping your
  Instagram session connected.

## Updating

```bash
cd ~/mass-unfollow
git pull
docker compose up -d --build
```

An in-app banner will tell you when a newer version is running on the server
than what your browser/phone currently has loaded (pushed instantly via a
persistent connection, not a polling delay) — tap **Reload** on it rather than
just refreshing, since it also clears the PWA's cached shell files first. If
you deploy and don't see the update reflected even after that, see the
Cloudflare caching note below.

## Adding a second person

This app has **no multi-user support** — one shared Instagram session, one
shared database, one shared app password. Letting someone else log in with
their own Instagram on your existing instance would mix their following list
into yours and risk the background worker acting on the wrong account. The
safe way to let someone else use it: a second, fully independent deployment,
same code and server, nothing shared.

`docker-compose.yml` already defines a second service (`mass-unfollow-martin`
in this deployment — rename as needed for a different person) that reuses the
same image but gets its own container, data volume, `.env` file, and port. To
bring it up:

```bash
cd ~/mass-unfollow
cp .env.example .env.martin
python3 -c "import secrets; print(secrets.token_hex(32))"   # paste into .env.martin's SECRET_KEY
nano .env.martin   # set APP_PASSWORD (a different one than the primary instance) and SECRET_KEY
docker compose up -d --build mass-unfollow-martin
```

This publishes it on port `8001` (reachable at `172.17.0.1:8001` from
cloudflared, same pattern as the primary instance on `8000`). Then in
Cloudflare:

1. **Published application route**: domain `unfollow-martin.chenpaz.cc`,
   service `http://172.17.0.1:8001`.
2. **Zero Trust Access application** (Public DNS type) on that hostname, with
   its own policy — **Include → Emails** → that person's email only. Don't
   add them to the primary instance's policy; that's a separate app now.

`docker compose up -d --build` (no service name) rebuilds and restarts both
instances together on future updates, since they share the same image.

## Troubleshooting: "Expecting value: line 1 column 1 (char 0)" / random 4xx errors

This means Instagram sent back an empty or non-JSON response to a private API
call — usually because [instagrapi](https://github.com/subzeroid/instagrapi)
has fallen behind Instagram's current app version and is being treated as an
outdated/unsupported client. Instagram updates its API fairly often; when it
does, instagrapi needs a matching release. Fix: bump the pin in
`backend/requirements.txt` to the latest version from
[PyPI](https://pypi.org/project/instagrapi/#history) (and bump the `requests`
pin too if pip reports a dependency conflict — instagrapi's minimum version
of it moves over time), then `docker compose up -d --build`. If errors
persist after upgrading, it may instead be a temporary soft rate-limit — wait
a while before retrying.

## Troubleshooting: deployed an update but the site still looks old

Your hostname is proxied through Cloudflare, which caches static assets
(HTML/JS/CSS) at its edge independently of your origin server — so
`docker compose up -d --build` on the server doesn't guarantee the browser
gets the new files. To confirm: `curl -s http://127.0.0.1:8000/ | grep -c
"tab-bar"` on the server checks the origin directly; if that looks right but
an **incognito** tab on the live domain still shows the old version, it's
Cloudflare's cache, not the deploy. Fix: Cloudflare dashboard → your zone →
**Caching → Configuration → Purge Cache → Purge Everything** (safe, just
forces a fresh fetch for everything, including your other subdomains). While
actively iterating on changes, toggling **Development Mode** on the same page
bypasses the edge cache entirely for 3 hours so every reload reflects the
latest deploy — remember to turn it back off afterward.
