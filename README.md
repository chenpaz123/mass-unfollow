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

This starts the app listening on `127.0.0.1:8000` on your server only — it is
**not** exposed to your LAN or the internet yet. That's what the tunnel is for.

Check it's alive: `curl http://127.0.0.1:8000/api/session` should return
`{"authenticated":false}`.

## 2. Expose it through your existing Cloudflare Tunnel

You said you already have `cloudflared` running with Zero Trust and your own
domain — you just need to add one more hostname to it.

**If you manage the tunnel via the Cloudflare dashboard (Zero Trust →
Networks → Tunnels):**
1. Open your tunnel → **Public Hostname** → **Add a public hostname**.
2. Subdomain: something like `unfollow` (→ `unfollow.yourdomain.com`).
3. Service: **Type** `HTTP`, **URL** `localhost:8000` (or the container's
   internal address if `cloudflared` itself runs in Docker on the same
   network — see note below).
4. Save. Cloudflare updates the tunnel config automatically, no restart
   needed.

**If you manage it via a local `config.yml`**, add an ingress rule *above*
the catch-all `service: http_status:404` line:

```yaml
ingress:
  - hostname: unfollow.yourdomain.com
    service: http://localhost:8000
  - service: http_status:404
```

Then `cloudflared tunnel run` (or restart the `cloudflared` service).

**If `cloudflared` runs in its own Docker container**, `localhost:8000` won't
reach this app from inside that container. Easiest fix: put both containers
on the same Docker network and point cloudflared at
`http://mass-unfollow:8000` instead — e.g. add `cloudflared` to this repo's
`docker-compose.yml` under the same top-level network, or add
`external: true` networking so your existing cloudflared compose file can
join `mass-unfollow`'s network. Tell me how your cloudflared container is set
up (its compose file / network name) and I'll wire the exact config.

## 3. Lock it down with Cloudflare Access

Since this holds your Instagram session, gate the hostname with a Zero Trust
Access policy so only you can reach it:

1. Zero Trust → **Access → Applications → Add an application** → **Self-hosted**.
2. Domain: `unfollow.yourdomain.com`.
3. Add a policy: **Include** → **Emails** → your email address only.
4. Save. Now visiting the URL requires a Cloudflare login (email OTP or
   whatever identity provider you have configured) *before* it ever reaches
   the app. The app's own `APP_PASSWORD` login is a second, independent layer
   behind that.

## 4. First run

1. Visit `https://unfollow.yourdomain.com`, enter your `APP_PASSWORD`.
2. **Connect Instagram** — two options:
   - **Session ID** (recommended, no 2FA hassle): log into Instagram in your
     browser, open DevTools → Application/Storage → Cookies →
     `https://www.instagram.com`, copy the `sessionid` value, paste it in.
     It'll periodically expire (weeks, usually) — just repeat this step when
     it does.
   - **Username/password**: supports 2FA codes. If Instagram throws a
     security checkpoint, the app will tell you to clear it by logging in
     normally via the app/website first, then retry here.
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
