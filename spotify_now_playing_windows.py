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
AUTH_TIMEOUT = 60  # seconds
NETWORK_RETRY_LIMIT = 5

# ===================== PKCE FUNCTIONS =====================
def generate_pkce_pair():
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().replace("=", "")
    return verifier, challenge

# Should be generated each new login
code_verifier, code_challenge = generate_pkce_pair()

# ===================== FLASK CALLBACK SERVER =====================
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

# ===================== TOKEN API =====================
def token_request(payload):
    """Auto-retry for network errors."""
    for attempt in range(NETWORK_RETRY_LIMIT):
        try:
            r = requests.post("https://accounts.spotify.com/api/token", data=payload, timeout=5)
            if r.status_code >= 500:  # server error
                time.sleep(1)
                continue
            return r.json()
        except Exception:
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

# ===================== SESSION MANAGEMENT =====================
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
    """Auto-retry for network issues."""
    for attempt in range(NETWORK_RETRY_LIMIT):
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
        except Exception:
            time.sleep(1)
    return {"error": "network"}

# ===================== SEEK DETECTION =====================
def detect_seek(prev_progress, prev_time, current_progress):
    """Improved heuristic-based seek detection."""
    if prev_progress is None:
        return False, 0

    expected = prev_progress + (time.time() - prev_time) * 1000
    delta = current_progress - expected

    if abs(delta) > 1500:  # more strict for better detection
        return True, delta
    return False, delta

# ===================== MAIN TRACKER =====================
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

        # Handle expired token
        if "error" in data:
            if data["error"] == "expired":
                print("[TOKEN] Access expired → Refreshing...")
                new = refresh_token_request(refresh_token_val)
                if not new or "access_token" not in new:
                    print("[TOKEN] Refresh failed → Need authorization again.")
                    return False
                access_token = new["access_token"]
                refresh_token_val = new.get("refresh_token", refresh_token_val)
                save_session(access_token, refresh_token_val)
                continue

        item = data["item"]
        track_id = item["id"]
        name = item["name"]
        artist = item["artists"][0]["name"]

        progress = data["progress_ms"]
        duration = item["duration_ms"]

        # Track changed
        if track_id != last_track_id:
            print(f"[TRACK] {name} — {artist}")
            last_track_id = track_id
            last_progress = progress
            last_timestamp = time.time()

        # Detect seek
        seek_detected, delta = detect_seek(last_progress, last_timestamp, progress)
        if seek_detected:
            print(f"[SEEK] Seek detected! Δ={delta:.0f}ms")

        last_progress = progress
        last_timestamp = time.time()

        # Format time
        def fmt(ms):
            ms = int(ms / 1000)
            return f"{ms//60}:{ms%60:02d}"

        print(f"{name} — {artist} | {fmt(progress)} / {fmt(duration)}")

        time.sleep(0.5)

# ===================== MAIN AUTH FLOW =====================
def authorize_if_needed():
    access_token, refresh_token_val = load_session()

    if access_token and refresh_token_val:
        print("[SESSION] Using saved session.")
        return access_token, refresh_token_val

    print("Launching browser for first-time authorization...")

    threading.Thread(target=start_flask, daemon=True).start()
    webbrowser.open(get_auth_url())

    global received_code
    received_code = None
    start_wait = time.time()

    while received_code is None:
        time.sleep(0.1)
        if time.time() - start_wait > AUTH_TIMEOUT:
            print("[ERROR] Authorization timeout (60 seconds). Try again.")
            exit()

    print("Authorization received. Exchanging for token...")

    token_data = swap_code_for_token(received_code)

    if not token_data or "access_token" not in token_data:
        print("[ERROR] Token exchange failed.")
        exit()

    access_token = token_data["access_token"]
    refresh_token_val = token_data["refresh_token"]

    save_session(access_token, refresh_token_val)

    return access_token, refresh_token_val


# ===================== ENTRY =====================
if __name__ == "__main__":
    while True:
        access_token, refresh_token_val = authorize_if_needed()
        ok = start_tracker(access_token, refresh_token_val)
        if not ok:
            print("Re-running authorization...")
            continue


