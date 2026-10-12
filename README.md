# IGDown — Instagram Video / Reels / Photo Downloader

A small web app (like indown.io) that downloads **public** Instagram content in
original quality: feed videos, Reels, photos, carousels and IGTV.

- Paste a **link** or an **@username** — both inputs are accepted.
- No login, no cookies, no signup. Works on phone, tablet and desktop.
- FastAPI backend + a single-file frontend. Deploys to **Vercel** in minutes.

> Public content only. Private posts, Stories, Highlights and full profile grids
> need an account login, which this tool deliberately does not do.

---

## What's inside

```
igdown/
├── api/
│   └── ig.py          # FastAPI app — all backend logic (one Vercel function)
├── index.html         # The whole frontend (HTML + CSS + JS, no build step)
├── requirements.txt   # Python dependencies
├── vercel.json        # Vercel function settings
├── Dockerfile         # For Render / Railway / any VPS
└── README.md
```

The backend exposes a single endpoint, `/api/ig`:

| Call | Purpose |
| --- | --- |
| `POST /api/ig` `{"action":"fetch","url":"<link or @handle>"}` | Returns media info + download options |
| `GET /api/ig?action=download&u=<token>&name=<file>` | Streams the file to the browser |
| `GET /api/ig?action=health` | Health check |

---

## Run it locally

```bash
cd igdown
python3 -m venv .venv && source .venv/bin/activate   # optional
pip install -r requirements.txt
uvicorn api.ig:app --reload
```

Open <http://127.0.0.1:8000> — the UI is served from the same origin.

---

## Deploy to Vercel

### Option A — Vercel CLI (fastest)

```bash
npm i -g vercel
cd igdown
vercel            # first run: log in, accept defaults
vercel --prod     # deploy to production
```

### Option B — GitHub

1. Push this folder to a GitHub repo.
2. In Vercel → **Add New → Project → Import** your repo.
3. Framework preset: **Other**. Leave build/output settings empty.
4. **Deploy.**

Vercel auto-detects `api/ig.py` as a Python function and serves `index.html` at `/`.

### Vercel limits to know

- **Function timeout** is set to 60 s in `vercel.json`. On the Hobby plan the
  maximum is 60 s; large/older links may need a retry. The Pro plan allows more
  (raise `maxDuration`).
- **Response size cap ~4.5 MB.** Serverless functions can only stream back a
  limited body, so **large videos may fail to save through Vercel**. Small Reels
  and all photos are fine. For unlimited size, deploy the same code on a host
  without that cap (see below) — or use the **Copy link** button and download the
  direct URL with a download manager.

---

## Deploy on Render / Railway / a VPS (no size cap)

The included `Dockerfile` runs the same app with full streaming:

```bash
docker build -t igdown .
docker run -p 8000:8000 igdown
```

Or on Render/Railway: create a new **Web Service** from the repo, choose the
Docker environment, and deploy. Set the port to `8000` (or use the platform's
`PORT`). This is the recommended route if you expect large videos.

---

## Notes & limitations

- Extraction is done with [yt-dlp]. Instagram changes its site often, so keep
  `yt-dlp` updated (`pip install -U yt-dlp`) — it is the single biggest factor in
  reliability.
- Only public content works. If Instagram returns a login/rate-limit wall, the
  app shows a friendly message instead of failing silently.
- Instagram CDNs are blocked from some datacenter IPs. If fetching works but
  downloads don't, that is usually the cause — a different host or a retry helps.
- Use this only for content you own or have permission to save, and respect
  creators' rights. Not affiliated with Instagram or Meta.

[yt-dlp]: https://github.com/yt-dlp/yt-dlp
