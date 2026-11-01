import json
import os
import re
import secrets
import uuid
from functools import wraps
from urllib.parse import urljoin, urlparse, quote, unquote

import requests
from flask import (
    Flask, Response, abort, jsonify, redirect,
    render_template, request, session, url_for
)

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
# Set SECRET_KEY in environment for production. Fallback generates a random
# key (sessions won't survive restarts — fine for dev).
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)

# Admin credentials — set via environment variables.
# Defaults only for local dev; CHANGE THESE in production.
ADMIN_USERNAME = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASS", "admin123")

DATA_FILE = os.path.join(os.path.dirname(__file__), "data.json")

USER_AGENT = (
    "Mozilla/5.0 (QtEmbedded; U; Linux; C) AppleWebKit/533.3 "
    "(KHTML, like Gecko) MAG200 stbapp ver: 2 rev: 250 Safari/533.3"
)
TIMEOUT = (10, 30)

# Runtime-discovered redirect hosts
_dynamic_hosts: set[str] = set()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("admin_login", next=request.path))
        return f(*args, **kwargs)
    return decorated


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        # Constant-time comparison to prevent timing attacks
        user_ok = secrets.compare_digest(username, ADMIN_USERNAME)
        pass_ok = secrets.compare_digest(password, ADMIN_PASSWORD)
        if user_ok and pass_ok:
            session.clear()
            session["admin_logged_in"] = True
            session.permanent = False
            next_url = request.args.get("next") or url_for("admin_panel")
            # Prevent open redirect
            if not next_url.startswith("/"):
                next_url = url_for("admin_panel")
            return redirect(next_url)
        else:
            error = "Invalid username or password."
    return render_template("login.html", error=error)


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def load_data() -> dict:
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_data(data: dict) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def get_channel_by_slug(slug: str) -> dict | None:
    data = load_data()
    for ch in data["channels"]:
        if ch["slug"] == slug and ch.get("active", True):
            return ch
    return None


def get_provider(provider_id: str) -> dict | None:
    data = load_data()
    for p in data["providers"]:
        if p["id"] == provider_id:
            return p
    return None


def build_stream_url(channel: dict) -> str:
    provider = get_provider(channel["provider_id"])
    if not provider:
        raise ValueError("Provider not found")
    return (
        f"{provider['base_url']}"
        f"?mac={quote(provider['mac'], safe='')}"
        f"&stream={channel['stream_id']}"
        f"&extension={provider.get('extension', 'm3u8')}"
    )


def allowed_hosts() -> set[str]:
    data = load_data()
    hosts: set[str] = set(_dynamic_hosts)
    for p in data["providers"]:
        if p.get("active", True):
            h = urlparse(p["base_url"]).hostname
            if h:
                hosts.add(h)
    return hosts


def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text


# ---------------------------------------------------------------------------
# Proxy core
# ---------------------------------------------------------------------------

def fetch_upstream(url: str, extra_headers=None):
    sess = requests.Session()
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*", "Connection": "keep-alive"}
    if extra_headers:
        headers.update(extra_headers)

    current = url
    for _ in range(10):
        parsed = urlparse(current)
        if parsed.scheme not in ("http", "https"):
            sess.close()
            raise ValueError("Redirect target has disallowed scheme")
        if parsed.hostname:
            _dynamic_hosts.add(parsed.hostname)

        response = sess.get(current, headers=headers, allow_redirects=False, stream=True, timeout=TIMEOUT)

        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("Location")
            response.close()
            if not location:
                sess.close()
                raise ValueError("Redirect without Location header")
            new_url = urljoin(current, location)
            new_host = urlparse(new_url).hostname
            if new_host:
                _dynamic_hosts.add(new_host)
            current = new_url
            continue

        return response, sess

    sess.close()
    raise ValueError("Too many redirects")


def seg_proxy_path(channel_slug: str, seg_url: str) -> str:
    return f"/play/{channel_slug}/seg?u={quote(seg_url, safe='')}"


def rewrite_playlist(content: bytes, final_url: str, channel_slug: str) -> bytes:
    text = content.decode("utf-8-sig", errors="replace")
    rewritten = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            rewritten.append(line)
            continue

        def make_replace_uri(base_url, slug):
            def replace_uri(match):
                absolute = urljoin(base_url, match.group(2))
                return match.group(1) + seg_proxy_path(slug, absolute) + match.group(3)
            return replace_uri

        line = re.sub(r'(URI=")([^"]+)(")', make_replace_uri(final_url, channel_slug), line, flags=re.IGNORECASE)

        if not stripped.startswith("#"):
            absolute = urljoin(final_url, stripped)
            line = seg_proxy_path(channel_slug, absolute)

        rewritten.append(line)
    return ("\n".join(rewritten) + "\n").encode("utf-8")


def make_error(message: str, status: int = 502):
    return Response(message, status=status, content_type="text/plain; charset=utf-8")


def proxy_url(url: str, channel_slug: str | None = None):
    if not url:
        abort(400, description="Missing URL")

    extra_headers = {}
    if request.headers.get("Range"):
        extra_headers["Range"] = request.headers["Range"]

    try:
        upstream, upstream_sess = fetch_upstream(url, extra_headers)
    except requests.RequestException as exc:
        return make_error(f"Could not reach upstream: {exc}")
    except ValueError as exc:
        return make_error(str(exc), 400)

    if upstream.status_code >= 400:
        status = upstream.status_code
        upstream.close()
        upstream_sess.close()
        return make_error(f"Upstream HTTP {status}", status)

    content_type = upstream.headers.get("Content-Type", "application/octet-stream")
    final_url = upstream.url

    try:
        body = upstream.content
    except Exception:
        return make_error("Failed reading upstream response")
    finally:
        upstream.close()
        upstream_sess.close()

    is_playlist = (
        "mpegurl" in content_type.lower()
        or "m3u8" in final_url.lower()
        or body.lstrip()[:7] == b"#EXTM3U"
    )

    if is_playlist and channel_slug:
        try:
            rewritten = rewrite_playlist(body, final_url, channel_slug)
        except Exception as exc:
            return make_error(f"Failed processing playlist: {exc}")
        return Response(
            rewritten, status=200,
            content_type="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Access-Control-Allow-Origin": "*"},
        )

    resp_headers = {"Access-Control-Allow-Origin": "*"}
    for name in ("Content-Length", "Content-Range", "Accept-Ranges", "Cache-Control"):
        if name in upstream.headers:
            resp_headers[name] = upstream.headers[name]

    return Response(body, status=upstream.status_code, content_type=content_type, headers=resp_headers)


# ---------------------------------------------------------------------------
# Stream routes (public — no auth needed)
# ---------------------------------------------------------------------------

@app.route("/play/<slug>/index.m3u8")
def channel_playlist(slug: str):
    channel = get_channel_by_slug(slug)
    if not channel:
        abort(404)
    try:
        stream_url = build_stream_url(channel)
    except ValueError as exc:
        return make_error(str(exc), 500)
    return proxy_url(stream_url, channel_slug=slug)


@app.route("/play/<slug>/seg")
def channel_segment(slug: str):
    channel = get_channel_by_slug(slug)
    if not channel:
        abort(404)
    url = unquote(request.args.get("u", ""))
    if not url:
        abort(400, description="Missing segment URL")
    return proxy_url(url, channel_slug=slug)


# ---------------------------------------------------------------------------
# Admin panel (all routes protected)
# ---------------------------------------------------------------------------

@app.route("/admin")
@login_required
def admin_panel():
    return render_template("admin.html")


# --- Channels API ---

@app.route("/admin/api/channels", methods=["GET"])
@login_required
def api_channels_list():
    return jsonify(load_data()["channels"])


@app.route("/admin/api/channels", methods=["POST"])
@login_required
def api_channels_add():
    body = request.get_json(force=True)
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    data = load_data()
    slug = body.get("slug") or slugify(name)
    existing = {c["slug"] for c in data["channels"]}
    base, counter = slug, 2
    while slug in existing:
        slug = f"{base}-{counter}"; counter += 1
    channel = {
        "id": "ch_" + uuid.uuid4().hex[:8],
        "name": name, "slug": slug,
        "stream_id": str(body.get("stream_id", "")),
        "provider_id": body.get("provider_id", ""),
        "logo": body.get("logo", ""),
        "category": body.get("category", ""),
        "language": body.get("language", ""),
        "active": bool(body.get("active", True)),
    }
    data["channels"].append(channel)
    save_data(data)
    return jsonify(channel), 201


@app.route("/admin/api/channels/<channel_id>", methods=["PUT"])
@login_required
def api_channels_update(channel_id: str):
    body = request.get_json(force=True)
    data = load_data()
    for ch in data["channels"]:
        if ch["id"] == channel_id:
            for key in ("name", "slug", "stream_id", "provider_id", "logo", "category", "language", "active"):
                if key in body:
                    ch[key] = body[key]
            save_data(data)
            return jsonify(ch)
    return jsonify({"error": "not found"}), 404


@app.route("/admin/api/channels/<channel_id>", methods=["DELETE"])
@login_required
def api_channels_delete(channel_id: str):
    data = load_data()
    before = len(data["channels"])
    data["channels"] = [c for c in data["channels"] if c["id"] != channel_id]
    if len(data["channels"]) == before:
        return jsonify({"error": "not found"}), 404
    save_data(data)
    return jsonify({"ok": True})


# --- Providers API ---

@app.route("/admin/api/providers", methods=["GET"])
@login_required
def api_providers_list():
    return jsonify(load_data()["providers"])


@app.route("/admin/api/providers", methods=["POST"])
@login_required
def api_providers_add():
    body = request.get_json(force=True)
    name = (body.get("name") or "").strip()
    base_url = (body.get("base_url") or "").strip()
    if not name or not base_url:
        return jsonify({"error": "name and base_url required"}), 400
    data = load_data()
    provider = {
        "id": "prov_" + uuid.uuid4().hex[:8],
        "name": name, "base_url": base_url,
        "mac": body.get("mac", ""),
        "extension": body.get("extension", "m3u8"),
        "active": bool(body.get("active", True)),
    }
    data["providers"].append(provider)
    save_data(data)
    return jsonify(provider), 201


@app.route("/admin/api/providers/<provider_id>", methods=["PUT"])
@login_required
def api_providers_update(provider_id: str):
    body = request.get_json(force=True)
    data = load_data()
    for p in data["providers"]:
        if p["id"] == provider_id:
            for key in ("name", "base_url", "mac", "extension", "active"):
                if key in body:
                    p[key] = body[key]
            save_data(data)
            return jsonify(p)
    return jsonify({"error": "not found"}), 404


@app.route("/admin/api/providers/<provider_id>", methods=["DELETE"])
@login_required
def api_providers_delete(provider_id: str):
    data = load_data()
    before = len(data["providers"])
    data["providers"] = [p for p in data["providers"] if p["id"] != provider_id]
    if len(data["providers"]) == before:
        return jsonify({"error": "not found"}), 404
    save_data(data)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Error handlers — smart responses
# ---------------------------------------------------------------------------

@app.errorhandler(400)
def bad_request(e):
    return make_error(getattr(e, "description", "Bad request"), 400)


@app.errorhandler(401)
def unauthorized(e):
    return redirect(url_for("admin_login"))


@app.errorhandler(404)
def not_found(e):
    # API requests get JSON, everything else gets a clean HTML page
    if request.path.startswith("/admin/api/"):
        return jsonify({"error": "not found"}), 404
    return render_template("404.html"), 404


@app.errorhandler(405)
def method_not_allowed(e):
    return make_error("Method not allowed", 405)


@app.errorhandler(500)
def server_error(e):
    return make_error("Internal server error", 500)


# Catch-all: redirect any unknown route to 404 page
@app.route("/<path:path>")
def catch_all(path):
    abort(404)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
