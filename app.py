import datetime
import math
import os
import random
import sqlite3
import time
from flask import Flask, jsonify, redirect, render_template, request, send_from_directory, session
import pylast

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "lastfm-player-secret-key-12345")

# --- Environment Configuration ---
API_KEY = os.environ.get("LASTFM_API_KEY", "")
API_SECRET = os.environ.get("LASTFM_API_SECRET", "")
SESSION_KEY = os.environ.get("LASTFM_SESSION_KEY", "")
USERNAME = os.environ.get("LASTFM_USERNAME", "")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
DB_PATH = os.path.join(BASE_DIR, "library.db")

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024  # 64 MB max upload


# --- SQLite Database Setup ---
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tracks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                artist TEXT NOT NULL,
                title TEXT NOT NULL,
                album TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()


init_db()


def get_network():
    if not (API_KEY and API_SECRET and SESSION_KEY):
        raise ValueError(
            "Missing Last.fm credentials. Ensure LASTFM_API_KEY, LASTFM_API_SECRET, and LASTFM_SESSION_KEY are set."
        )
    return pylast.LastFMNetwork(
        api_key=API_KEY,
        api_secret=API_SECRET,
        session_key=SESSION_KEY,
        username=USERNAME,
    )


# --- Static / UI Routes ---
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/stream/<path:filename>")
def stream_audio(filename):
    return send_from_directory(
        app.config["UPLOAD_FOLDER"], filename, as_attachment=False
    )


# --- Authentication Helper Routes ---
@app.route("/auth/lastfm")
def auth_lastfm():
    # Detect https behind Render's reverse proxy
    scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
    host = request.headers.get("X-Forwarded-Host", request.host)
    callback_url = f"{scheme}://{host}/auth/callback"
    
    auth_url = f"https://www.last.fm/api/auth/?api_key={API_KEY}&cb={callback_url}"
    return redirect(auth_url)


@app.route("/auth/callback")
def auth_callback():
    token = request.args.get("token")
    if not token:
        return "No token received from Last.fm", 400

    try:
        network = pylast.LastFMNetwork(api_key=API_KEY, api_secret=API_SECRET)
        sg = pylast.SessionKeyGenerator(network)

        # Exchange token directly for session key
        session_key = sg.get_web_auth_session_key(request.url)

        # Retrieve username attached to this session
        user_network = pylast.LastFMNetwork(
            api_key=API_KEY, api_secret=API_SECRET, session_key=session_key
        )
        username = user_network.get_authenticated_user().get_name()

        return f"""
        <html>
        <body style="font-family: sans-serif; padding: 2rem; background: #121212; color: #fff;">
            <h2 style="color: #00ff88;">Authorization Successful!</h2>
            <p>Add these two values to your <strong>Render Dashboard &rarr; Environment</strong>:</p>
            <p><strong>LASTFM_SESSION_KEY:</strong> <code>{session_key}</code></p>
            <p><strong>LASTFM_USERNAME:</strong> <code>{username}</code></p>
            <br>
            <a href="/" style="color: #40c4ff;">Return to Player</a>
        </body>
        </html>
        """
    except Exception as e:
        return f"Error exchanging token: {str(e)}", 500


# --- Library & Upload Endpoints ---
@app.route("/api/upload", methods=["POST"])
def upload_track():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    audio_file = request.files["file"]
    if audio_file.filename == "":
        return jsonify({"error": "Empty filename"}), 400

    artist = request.form.get("artist", "").strip() or "Unknown Artist"
    title = request.form.get("title", "").strip() or "Untitled Track"
    album = request.form.get("album", "").strip()

    clean_name = "".join(
        c for c in audio_file.filename if c.isalnum() or c in "._-"
    )
    filename = f"{int(time.time())}_{clean_name}"
    save_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    audio_file.save(save_path)

    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO tracks (filename, artist, title, album) VALUES (?, ?, ?, ?)",
            (filename, artist, title, album),
        )
        track_id = cursor.lastrowid
        conn.commit()

    return jsonify(
        {
            "id": track_id,
            "filename": filename,
            "artist": artist,
            "title": title,
            "album": album,
            "url": f"/stream/{filename}",
        }
    )


@app.route("/api/library", methods=["GET"])
def list_library():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM tracks ORDER BY id DESC"
        ).fetchall()
        tracks = [
            {
                "id": r["id"],
                "filename": r["filename"],
                "artist": r["artist"],
                "title": r["title"],
                "album": r["album"] or "",
                "url": f"/stream/{r['filename']}",
            }
            for r in rows
        ]
    return jsonify(tracks)


# --- Radio Endpoint ---
@app.route("/api/radio/next", methods=["GET"])
def radio_next():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM tracks").fetchall()
        if not rows:
            return jsonify({"error": "No tracks uploaded yet"}), 404
        chosen = random.choice(rows)
        return jsonify(
            {
                "id": chosen["id"],
                "filename": chosen["filename"],
                "artist": chosen["artist"],
                "title": chosen["title"],
                "album": chosen["album"] or "",
                "url": f"/stream/{chosen['filename']}",
            }
        )


# --- Last.fm Operations ---
@app.route("/api/now-playing", methods=["POST"])
def update_now_playing():
    data = request.get_json() or {}
    artist = data.get("artist", "").strip()
    title = data.get("title", "").strip()
    album = data.get("album", "").strip()

    if not artist or not title:
        return jsonify({"error": "Artist and title required"}), 400

    try:
        net = get_network()
        net.update_now_playing(artist=artist, title=title, album=album)
        return jsonify({"status": "Now playing set"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/scrobble", methods=["POST"])
def scrobble_track():
    data = request.get_json() or {}
    artist = data.get("artist", "").strip()
    title = data.get("title", "").strip()
    album = data.get("album", "").strip()

    if not artist or not title:
        return jsonify({"error": "Artist and title required"}), 400

    try:
        net = get_network()
        timestamp = int(time.time())
        net.scrobble(
            artist=artist,
            title=title,
            timestamp=timestamp,
            album=album if album else None,
        )
        return jsonify(
            {
                "status": "Scrobbled successfully",
                "artist": artist,
                "track": title,
                "timestamp": timestamp,
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- Billboard Movement Feature ---
@app.route("/api/billboard", methods=["GET"])
def get_billboard():
    try:
        net = get_network()
        user = net.get_user(USERNAME)

        all_time = user.get_top_tracks(period=pylast.PERIOD_OVERALL, limit=25)
        weekly = user.get_top_tracks(period=pylast.PERIOD_7DAYS, limit=50)

        weekly_map = {}
        for rank, item in enumerate(weekly, 1):
            key = f"{item.item.artist.name.strip().lower()} - {item.item.title.strip().lower()}"
            weekly_map[key] = rank

        billboard = []
        for rank, item in enumerate(all_time, 1):
            art = item.item.artist.name
            trk = item.item.title
            key = f"{art.strip().lower()} - {trk.strip().lower()}"

            if key in weekly_map:
                w_rank = weekly_map[key]
                if w_rank < rank:
                    delta = rank - w_rank
                    status = f"▲ HOT (+{delta})"
                    badge_class = "up"
                elif w_rank > rank:
                    delta = w_rank - rank
                    status = f"▼ DOWN (-{delta})"
                    badge_class = "down"
                else:
                    status = "▶ SAME"
                    badge_class = "steady"
            else:
                status = "— COLD"
                badge_class = "cold"

            billboard.append(
                {
                    "rank": rank,
                    "artist": art,
                    "track": trk,
                    "plays": int(item.weight),
                    "status": status,
                    "badge_class": badge_class,
                }
            )

        return jsonify(billboard)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- /pace Milestone Predictor ---
@app.route("/api/pace", methods=["GET"])
def get_pace():
    target_raw = request.args.get("target", "100000")
    try:
        target = int(target_raw)
    except ValueError:
        return jsonify({"error": "Target must be an integer"}), 400

    try:
        net = get_network()
        user = net.get_user(USERNAME)

        total_scrobbles = user.get_playcount()
        registered_time = int(user.get_registered())

        elapsed_days = max((time.time() - registered_time) / 86400.0, 1.0)
        daily_pace = total_scrobbles / elapsed_days

        if total_scrobbles >= target:
            return jsonify(
                {
                    "current": total_scrobbles,
                    "target": target,
                    "achieved": True,
                    "message": "Milestone already reached!",
                }
            )

        needed = target - total_scrobbles
        days_left = needed / daily_pace
        target_timestamp = datetime.datetime.now() + datetime.timedelta(
            days=days_left
        )

        return jsonify(
            {
                "current": total_scrobbles,
                "target": target,
                "achieved": False,
                "needed": needed,
                "daily_pace": round(daily_pace, 1),
                "days_left": math.ceil(days_left),
                "estimated_date": target_timestamp.strftime("%B %d, %Y"),
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
