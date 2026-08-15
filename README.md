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
turn them off or push them to extremes.

## How it works

- **Backend**: Python/FastAPI, talks to Instagram via instagrapi, stores your
  following list + your keep/unfollow decisions in a local SQLite database.
- **Frontend**: a single-page swipe UI served by the same app.
- **Unfollow worker**: a background loop that only processes the "unfollow"
  pile, one account every 45–180 seconds (configurable), capped at 150/day by
  default. You can pause/resume it any time from the Review screen.
- Everything — your Instagram session, the database, cached avatars — stays
  on your own server, in `./data`.

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
routes** (the screen you already have open):

1. **+ Add a published application route**.
2. Domain: `unfollow.chenpaz.cc` (or whatever subdomain you want).
3. Service: **Type** `HTTP`, **URL** `172.17.0.1:8000` — same pattern as your
   `n8n.chenpaz.cc` and `home.chenpaz.cc` routes.
4. Save. No restart needed, cloudflared picks it up automatically.

## 3. Lock it down with Cloudflare Access

Since this holds your Instagram session, gate the hostname with a Zero Trust
Access policy so only you can reach it:

1. Zero Trust → **Access → Applications → Add an application** → **Self-hosted**.
2. Domain: `unfollow.chenpaz.cc`.
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
3. **Sync following list** — pulls all ~7,300 accounts you follow. Takes a
   few minutes; only needs to be done once (re-run any time to pick up new
   follows).
4. **Swipe** — drag right (or press → / tap ♥) to keep, left (← / ✕) to mark
   for unfollow. Undo button reverts the last decision.
5. **Review** — see counts, tune the daily cap / delay range, and hit **Start
   unfollow queue**. Leave the tab; it runs server-side even if you close the
   browser. Come back anytime to check progress or pause it.

## Notes / limits

- The unfollow worker is intentionally slow. At the 150/day default, clearing
  a large "unfollow" pile takes days — that's by design, not a bug.
- Avatars are fetched from Instagram's CDN on first view and cached to
  `./data/avatars`.
- If you ever want to start over, stop the container and delete `./data`
  (this forgets your Instagram session and all decisions).

## Troubleshooting: "Expecting value: line 1 column 1 (char 0)" / random 4xx errors

This means Instagram sent back an empty or non-JSON response to a private API
call — usually because [instagrapi](https://github.com/subzeroid/instagrapi)
has fallen behind Instagram's current app version and is being treated as an
outdated/unsupported client. Instagram updates its API fairly often; when it
does, instagrapi needs a matching release. Fix: bump the pin in
`backend/requirements.txt` to the latest version from
[PyPI](https://pypi.org/project/instagrapi/#history), then
`docker compose up -d --build`. If errors persist after upgrading, it may
instead be a temporary soft rate-limit — wait a while before retrying.

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
