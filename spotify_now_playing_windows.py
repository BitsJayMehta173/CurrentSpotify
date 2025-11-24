import os
import json
import time
import base64
import hashlib
import secrets
import threading
import webbrowser
import requests
from flask import Flask, request

CLIENT_ID = "6e275fbc81f14f50a9e34de55c7417c0"
REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPES = "user-read-playback-state"

CONFIG_FILE = "spotify_session.json"

# ============================================================
# CONFIG HELPERS
# ============================================================

def save_session(data):
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2)

def load_session():
    if not os.path.exists(CONFIG_FILE):
        return None
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

# ============================================================
# PKCE
# ============================================================

def generate_pkce_pair():
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().replace("=", "")
    return verifier, challenge

# ============================================================
# FLASK CALLBACK
# ============================================================

app = Flask(__name__)
received_code = None

@app.route("/callback")
def callback():
    global received_code
    received_code = request.args.get("code")
    return "Authorization complete! You may close this window."

def start_flask():
    app.run(port=8888, debug=False)

# ============================================================
# SPOTIFY AUTH & TOKENS
# ============================================================

def get_auth_url(code_challenge):
    return (
        "https://accounts.spotify.com/authorize"
        f"?client_id={CLIENT_ID}"
        f"&response_type=code"
        f"&redirect_uri={REDIRECT_URI}"
        f"&scope={SCOPES}"
        f"&code_challenge_method=S256"
        f"&code_challenge={code_challenge}"
    )

def exchange_code_for_token(code, verifier):
    r = requests.post(
        "https://accounts.spotify.com/api/token",
        data={
            "client_id": CLIENT_ID,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
        },
    )
    return r.json()

def refresh_access_token(refresh_token):
    r = requests.post(
        "https://accounts.spotify.com/api/token",
        data={
            "client_id": CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
    )
    return r.json()

# ============================================================
# PLAYBACK API
# ============================================================

def get_playback(token):
    r = requests.get(
        "https://api.spotify.com/v1/me/player/currently-playing",
        headers={"Authorization": f"Bearer {token}"},
    )
    if r.status_code == 204:
        return None
    return r.json()

# ============================================================
# SEEK DETECTION
# ============================================================

def detect_seek(progress_now, progress_prev, realtime_delta_ms):
    expected = progress_prev + realtime_delta_ms
    diff = abs(progress_now - expected)

    # More sensitive seek detection
    return diff > 500  # 0.5 seconds tolerance

# ============================================================
# MAIN TRACKER
# ============================================================

def start_tracker(session):
    access_token = session["access_token"]
    refresh_token_val = session["refresh_token"]
    expires_at = session["expires_at"]

    last_progress = None
    last_timestamp = time.time()

    while True:
        # ===========================
        # AUTO REFRESH BEFORE EXPIRY
        # ===========================
        if time.time() > expires_at - 60:
            print("[AUTO REFRESH] Refreshing access_token...")
            new = refresh_access_token(refresh_token_val)

            if "access_token" not in new:
                print("[ERROR] Token refresh failed. Need re-login.")
                os.remove(CONFIG_FILE)
                return "REAUTH"

            access_token = new["access_token"]
            refresh_token_val = new.get("refresh_token", refresh_token_val)
            expires_at = time.time() + new.get("expires_in", 3600)

            save_session({
                "access_token": access_token,
                "refresh_token": refresh_token_val,
                "expires_at": expires_at,
            })

        # ===========================
        # GET PLAYBACK
        # ===========================
        data = get_playback(access_token)

        if data is None:
            print("No track playing...")
            time.sleep(1)
            continue

        if "error" in data:
            print("[ERROR] Access token expired unexpectedly!")
            return "REAUTH"

        item = data["item"]
        name = item["name"]
        artist = item["artists"][0]["name"]

        progress = data["progress_ms"]
        duration = item["duration_ms"]

        # ===========================
        # IMPROVED SEEK DETECTION
        # ===========================
        if last_progress is not None:
            real_delta = (time.time() - last_timestamp) * 1000
            if detect_seek(progress, last_progress, real_delta):
                print(f"[SEEK] Jump detected → {progress}ms")
        
        last_progress = progress
        last_timestamp = time.time()

        def fmt(ms):
            s = int(ms / 1000)
            return f"{s//60}:{s%60:02d}"

        print(f"{name} — {artist} | {fmt(progress)} / {fmt(duration)}")

        time.sleep(0.5)

# ============================================================
# MAIN FLOW
# ============================================================

if __name__ == "__main__":
    session = load_session()

    # -----------------------------------------------------------
    # CASE 1 — SESSION EXISTS → use it
    # -----------------------------------------------------------
    if session:
        print("[INFO] Loaded saved session. Starting tracker...")
        result = start_tracker(session)

        if result != "REAUTH":
            exit()

        print("[INFO] Session invalid. Needs authorization.")

    # -----------------------------------------------------------
    # CASE 2 — NO SESSION → FULL LOGIN ONCE
    # -----------------------------------------------------------
    print("Starting Spotify authorization server...")
    threading.Thread(target=start_flask, daemon=True).start()

    print("Opening browser for Spotify login...")
    verifier, challenge = generate_pkce_pair()
    webbrowser.open(get_auth_url(challenge))

    print("Waiting for authorization...")
    while received_code is None:
        time.sleep(0.2)

    print("Authorization received. Exchanging for tokens...")
    token_data = exchange_code_for_token(received_code, verifier)

    access_token = token_data["access_token"]
    refresh_token_val = token_data["refresh_token"]
    expires_at = time.time() + token_data.get("expires_in", 3600)

    save_session({
        "access_token": access_token,
        "refresh_token": refresh_token_val,
        "expires_at": expires_at,
    })

    print("Session saved. Starting tracker...")
    start_tracker(load_session())
