# Flask HLS IPTV Proxy + Player

## Files
- `app.py` — Flask backend that fetches the configured source, follows allowlisted redirects, rewrites HLS playlist URLs, and proxies segments.
- `templates/player.html` — Plyr UI with HLS.js playback.
- `requirements.txt` — Python dependencies.

## Setup
```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
# source .venv/bin/activate

pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5000

## Configure
Edit `SOURCE_URL` in `app.py` for your authorized provider stream.
If the provider redirects to another hostname, add that exact hostname to `ALLOWED_HOSTS`.

## Important
- Use only streams you are authorized to access.
- This proxy consumes bandwidth on your Flask server.
- Keep it private or add authentication/rate limiting before public deployment.
- `app.run()` is for local testing. For production, use a WSGI server such as Gunicorn (Linux) or Waitress (Windows).
- Browser playback may still fail if the provider requires special cookies/headers or uses DRM/unsupported codecs.
