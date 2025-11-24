import os
import base64
import hashlib
import secrets
import threading
import webbrowser
import time
import json
import requests
from flask import Flask, request

# ===================== CONFIG =====================
CLIENT_ID = "6e275fbc81f14f50a9e34de55c7417c0"
REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPES = "user-read-playback-state"

SESSION_FILE = "spotify_session.json"
AUTH_TIMEOUT = 60
NETWORK_RETRY_LIMIT = 5

# HARDCODED LYRICS FILE for "We Don't Talk Anymore"
LYRICS_FILE = "Charlie Puth - We Don't Talk Anymore.json"


# ===================== PKCE =====================
def generate_pkce_pair():
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().replace("=", "")
    return verifier, challenge

code_verifier, code_challenge = generate_pkce_pair()


# ===================== FLASK CALLBACK =====================
app = Flask(__name__)
received_code = None

@app.route("/callback")
def callback():
    global received_code
    received_code = request.args.get("code")
    return "Authorization complete! You can close this window."

def start_flask():
    app.run(port=8888, debug=False)


# ===================== AUTH URL =====================
def get_auth_url():
    return (
        "https://accounts.spotify.com/authorize"
        f"?client_id={CLIENT_ID}"
        f"&response_type=code"
        f"&redirect_uri={REDIRECT_URI}"
        f"&scope={SCOPES}"
        f"&code_challenge_method=S256"
        f"&code_challenge={code_challenge}"
    )


# ===================== TOKEN REQUEST HELPERS =====================
def token_request(payload):
    for _ in range(NETWORK_RETRY_LIMIT):
        try:
            response = requests.post(
                "https://accounts.spotify.com/api/token",
                data=payload,
                timeout=5
            )
            if response.status_code >= 500:
                time.sleep(1)
                continue
            return response.json()
        except:
            time.sleep(1)
    return None


def swap_code_for_token(code):
    return token_request({
        "client_id": CLIENT_ID,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": code_verifier,
    })


def refresh_token_request(refresh_token):
    return token_request({
        "client_id": CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    })


# ===================== SESSION SAVE/LOAD =====================
def save_session(access, refresh):
    with open(SESSION_FILE, "w") as f:
        json.dump({"access_token": access, "refresh_token": refresh}, f)


def load_session():
    if not os.path.exists(SESSION_FILE):
        return None, None
    with open(SESSION_FILE, "r") as f:
        data = json.load(f)
        return data.get("access_token"), data.get("refresh_token")


# ===================== PLAYER API =====================
def get_playback(token):
    for _ in range(NETWORK_RETRY_LIMIT):
        try:
            r = requests.get(
                "https://api.spotify.com/v1/me/player/currently-playing",
                headers={"Authorization": f"Bearer {token}"},
                timeout=5
            )

            if r.status_code == 204:
                return None
            if r.status_code == 200:
                return r.json()
            if r.status_code == 401:
                return {"error": "expired"}

            if r.status_code >= 500:
                time.sleep(1)
                continue

            return {"error": f"status {r.status_code}"}
        except:
            time.sleep(1)
    return {"error": "network"}


# ===================== SEEK DETECTION =====================
def detect_seek(prev_progress, prev_timestamp, current_progress):
    if prev_progress is None:
        return False, 0

    expected = prev_progress + (time.time() - prev_timestamp) * 1000
    delta = current_progress - expected

    if abs(delta) > 1500:
        return True, delta
    return False, delta


# ===================== LYRICS HANDLING =====================
def load_lyrics(filepath):
    """Loads LRClib formatted JSON."""
    with open(filepath, "r", encoding="utf8") as f:
        data = json.load(f)

    times = []
    lines = []

    for entry in data["timed_lyrics"]:
        times.append(float(entry["seconds"]))   # ALREADY numeric
        lines.append(entry["line"])

    return times, lines


def get_lyric_at_timestamp(times, lines, current_sec):
    """Binary-search for closest lyric <= current time."""
    import bisect
    idx = bisect.bisect_right(times, current_sec) - 1
    if idx < 0:
        return ""
    return lines[idx]


# Load lyrics once
if os.path.exists(LYRICS_FILE):
    LYR_TIMES, LYR_LINES = load_lyrics(LYRICS_FILE)
else:
    LYR_TIMES, LYR_LINES = [], []


# ===================== TRACKER =====================
def start_tracker(access_token, refresh_token_val):
    last_progress = None
    last_timestamp = time.time()
    last_track_id = None

    while True:
        data = get_playback(access_token)

        if data is None:
            print("No track playing...")
            time.sleep(1)
            continue

        if "error" in data:
            if data["error"] == "expired":
                print("[TOKEN] Expired → Refreshing token...")
                new = refresh_token_request(refresh_token_val)
                if not new or "access_token" not in new:
                    print("[TOKEN] Refresh failed → re-auth needed.")
                    return False
                access_token = new["access_token"]
                refresh_token_val = new.get("refresh_token", refresh_token_val)
                save_session(access_token, refresh_token_val)
                continue

        item = data["item"]
        name = item["name"]
        artist = item["artists"][0]["name"]
        track_id = item["id"]

        progress = data["progress_ms"]
        duration = item["duration_ms"]

        # New song
        if track_id != last_track_id:
            print(f"\n[TRACK] {name} — {artist}")
            last_track_id = track_id
            last_progress = progress
            last_timestamp = time.time()

        # Seek detection
        seek_detected, delta = detect_seek(last_progress, last_timestamp, progress)
        if seek_detected:
            print(f"[SEEK] Seek detected Δ={delta:.0f}ms")

        last_progress = progress
        last_timestamp = time.time()

        # Format
        def fmt(ms):
            sec = int(ms / 1000)
            return f"{sec//60}:{sec%60:02d}"

        now_sec = progress / 1000

        print(f"{name} — {artist} | {fmt(progress)} / {fmt(duration)}")

        # ===================== LYRIC DISPLAY =====================
        if "We Don't Talk Anymore" in name:
            lyric = get_lyric_at_timestamp(LYR_TIMES, LYR_LINES, now_sec)
            if lyric.strip() != "":
                print("♪  " + lyric)

        time.sleep(0.5)


# ===================== AUTH LOGIC =====================
def authorize_if_needed():
    access, refresh = load_session()
    if access and refresh:
        print("[SESSION] Loaded saved session.")
        return access, refresh

    print("Opening browser for Spotify login...")

    threading.Thread(target=start_flask, daemon=True).start()
    webbrowser.open(get_auth_url())

    global received_code
    received_code = None
    start_wait = time.time()

    while received_code is None:
        time.sleep(0.1)
        if time.time() - start_wait > AUTH_TIMEOUT:
            print("[ERROR] Authorization timeout.")
            exit()

    data = swap_code_for_token(received_code)
    if not data or "access_token" not in data:
        print("[ERROR] Token exchange failed.")
        exit()

    save_session(data["access_token"], data["refresh_token"])
    return data["access_token"], data["refresh_token"]


# ===================== PROGRAM ENTRY =====================
if __name__ == "__main__":
    while True:
        access, refresh = authorize_if_needed()
        ok = start_tracker(access, refresh)
        if not ok:
            print("[AUTH] Re-authorizing...")
            continue
