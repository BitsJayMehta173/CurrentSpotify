#!/usr/bin/env python3
"""
spotify_with_lyrics_final.py
Final unified script — Spotify tracker + lyrics fetch/convert + synced console display.

Requirements:
    pip install requests flask rapidfuzz
Run:
    python spotify_with_lyrics_final.py
"""

import os
import re
import time
import json
import base64
import hashlib
import secrets
import threading
import webbrowser
import requests
from flask import Flask, request
from rapidfuzz import process, fuzz
from datetime import datetime

# ========== CONFIG ==========
CLIENT_ID = "6e275fbc81f14f50a9e34de55c7417c0"  # replace if you want your own client id
REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPES = "user-read-playback-state"
SESSION_FILE = "spotify_session.json"
AUTH_TIMEOUT = 60  # seconds
NETWORK_RETRY_LIMIT = 5
POLL_INTERVAL = 0.5  # seconds
SEEK_THRESHOLD_MS = 1500
LYRICS_FOLDER = "lyrics_cache"
LRCLIB_BASE = "https://lrclib.net"

os.makedirs(LYRICS_FOLDER, exist_ok=True)

# ========== PKCE ==========
def generate_pkce_pair():
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).decode().replace("=", "")
    return code_verifier, code_challenge

CODE_VERIFIER, CODE_CHALLENGE = generate_pkce_pair()

# ========== Flask callback ==========
app = Flask(__name__)
received_code = None

@app.route("/callback")
def callback():
    global received_code
    received_code = request.args.get("code")
    return "<html><body><h2>Authorization complete — you can close this window.</h2></body></html>"

def start_flask():
    app.run(port=8888, debug=False)

def get_auth_url():
    return (
        "https://accounts.spotify.com/authorize"
        f"?client_id={CLIENT_ID}"
        f"&response_type=code"
        f"&redirect_uri={REDIRECT_URI}"
        f"&scope={SCOPES}"
        f"&code_challenge_method=S256"
        f"&code_challenge={CODE_CHALLENGE}"
    )

# ========== HTTP helpers ==========
def post_with_retries(url, data, retries=NETWORK_RETRY_LIMIT, timeout=6):
    for attempt in range(retries):
        try:
            r = requests.post(url, data=data, timeout=timeout)
            return r
        except Exception:
            if attempt + 1 == retries:
                raise
            time.sleep(1)
    raise RuntimeError("Unreachable")

def get_with_retries(url, params=None, headers=None, retries=NETWORK_RETRY_LIMIT, timeout=6):
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=timeout)
            return r
        except Exception:
            if attempt + 1 == retries:
                raise
            time.sleep(1)
    raise RuntimeError("Unreachable")

# ========== Token functions ==========
def swap_code_for_token(code):
    payload = {
        "client_id": CLIENT_ID,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": CODE_VERIFIER,
    }
    r = post_with_retries("https://accounts.spotify.com/api/token", payload)
    return r.json()

def refresh_token_request(refresh_token):
    payload = {
        "client_id": CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    r = post_with_retries("https://accounts.spotify.com/api/token", payload)
    return r.json()

# ========== Session ==========
def save_session(access, refresh):
    with open(SESSION_FILE, "w", encoding="utf8") as f:
        json.dump({"access_token": access, "refresh_token": refresh, "saved_at": time.time()}, f)

def load_session():
    if not os.path.exists(SESSION_FILE):
        return None, None
    try:
        with open(SESSION_FILE, "r", encoding="utf8") as f:
            data = json.load(f)
            return data.get("access_token"), data.get("refresh_token")
    except Exception:
        return None, None

# ========== Spotify playback ==========
def get_playback(access_token):
    url = "https://api.spotify.com/v1/me/player/currently-playing"
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        r = get_with_retries(url, headers=headers)
    except Exception:
        return {"error": "network"}
    if r.status_code == 204:
        return None
    if r.status_code == 200:
        return r.json()
    if r.status_code == 401:
        return {"error": "expired"}
    return {"error": f"status {r.status_code}"}

# ========== Utilities ==========
def format_time_ms(ms):
    if ms is None:
        return "0:00"
    total = int(ms // 1000)
    m, s = divmod(total, 60)
    return f"{m}:{s:02d}"

def format_time_seconds(sec):
    if sec is None:
        return "0:00"
    total = int(sec)
    m, s = divmod(total, 60)
    return f"{m}:{s:02d}"

def clear_console():
    os.system('cls' if os.name == 'nt' else 'clear')

def sanitize_filename(s):
    # allow letters, numbers, spaces and safe punctuation
    keep = "-_.()[] "
    return "".join(c for c in s if c.isalnum() or c in keep).strip()

def ensure_json_ext(path):
    if not path.lower().endswith(".json"):
        path = path + ".json"
    return path

def lyrics_filepath_for(track_title, track_artist):
    fname = f"{track_artist} - {track_title}"
    fname = sanitize_filename(fname)
    path = os.path.join(LYRICS_FOLDER, fname)
    path = ensure_json_ext(path)
    return path

# ========== Load lyrics file (expected format) ==========
def load_lyrics_from_file(filepath):
    if not os.path.exists(filepath):
        return None, None
    try:
        with open(filepath, "r", encoding="utf8") as f:
            data = json.load(f)
    except Exception:
        return None, None
    timed = data.get("timed_lyrics") or []
    times = []
    lines = []
    for entry in timed:
        if not isinstance(entry, dict):
            continue
        sec = None
        if "seconds" in entry:
            try:
                sec = float(entry["seconds"])
            except Exception:
                sec = None
        elif "time" in entry:
            t = entry["time"]
            # parse mm:ss.xx or mm:ss
            m = re.match(r"(\d+):(\d+(?:\.\d+)?)", t)
            if m:
                mm = int(m.group(1))
                ss = float(m.group(2))
                sec = mm * 60 + ss
        line = entry.get("line", "")
        if sec is None:
            continue
        times.append(sec)
        lines.append(line)
    if not times:
        return None, None
    combined = sorted(zip(times, lines), key=lambda x: x[0])
    times_sorted, lines_sorted = zip(*combined)
    return list(times_sorted), list(lines_sorted)

# ========== Parse lrclib synced text -> desired JSON format ==========
TS_BRACKET_RE = re.compile(r"\[(\d{2}):(\d{2})(?:\.(\d+))?\]\s*(.*)")

def parse_synced_lyrics_text(synced_text):
    """
    Parse synced lyrics in .lrc style like:
      [00:00.18] We don't talk anymore
    Returns list of dict entries with 'time', 'seconds', 'line'
    """
    lines = []
    for raw in synced_text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        m = TS_BRACKET_RE.match(raw)
        if not m:
            # if not bracketed, skip
            continue
        mm = int(m.group(1))
        ss = int(m.group(2))
        frac = m.group(3) or "0"
        # normalize fractional to seconds as float
        frac_sec = float("0." + frac) if frac.isdigit() else 0.0
        seconds = mm * 60 + ss + frac_sec
        time_str = f"{mm:02d}:{ss:02d}"
        # if fractional exists, include .xx
        if frac and frac != "0":
            # keep two decimals if available
            frac_norm = frac[:2].ljust(2, '0')
            time_str = f"{mm:02d}:{ss:02d}.{frac_norm}"
        line = m.group(4).strip()
        lines.append({"time": time_str, "seconds": round(seconds, 3), "line": line})
    return lines

# ========== lrclib API access & fuzzy search ==========
def lrclib_search(query):
    url = LRCLIB_BASE + "/api/search"
    try:
        r = get_with_retries(url, params={"query": query})
        if r.status_code == 200:
            return r.json()
    except Exception:
        return None
    return None

def lrclib_get(track_name=None, artist_name=None, record_id=None):
    url = LRCLIB_BASE + "/api/get"
    params = {}
    if track_name:
        params["track_name"] = track_name
    if artist_name:
        params["artist_name"] = artist_name
    if record_id:
        params["id"] = record_id
    try:
        r = get_with_retries(url, params=params)
        if r.status_code == 200:
            return r.json()
    except Exception:
        return None
    return None

def convert_and_save_lrclib_result(got_json, save_path):
    """
    Convert lrclib get response to the desired JSON format and save.
    Accepts responses that may contain 'syncedLyrics', 'synced', 'synced_lyrics', or plain 'syncedLyrics' string.
    """
    # Determine where synced lyrics live
    synced_text = None
    # Common keys to try
    for key in ("syncedLyrics", "synced_lyrics", "synced", "synced_text", "synced_lyric"):
        if isinstance(got_json, dict) and got_json.get(key):
            synced_text = got_json.get(key)
            break
    # Some APIs return 'synced' as array lines; handle that
    if not synced_text and isinstance(got_json, dict):
        # try keys like 'synced' being a string or a list
        if "synced" in got_json:
            synced_text = got_json.get("synced")
        elif "lyrics" in got_json and isinstance(got_json["lyrics"], dict):
            # maybe lyrics: { 'synced': '...' }
            synced_text = got_json["lyrics"].get("synced") or got_json["lyrics"].get("syncedLyrics")
    # If synced_text is a list of timestamp/line pairs, convert to lrc-like string
    if isinstance(synced_text, list):
        pieces = []
        for entry in synced_text:
            # entry could be {"time":"00:00.18","line":"..."} or similar
            t = entry.get("time") or entry.get("timestamp") or entry.get("ts")
            l = entry.get("line") or entry.get("text") or entry.get("lyric") or ""
            if t and l is not None:
                pieces.append(f"[{t}] {l}")
        synced_text = "\n".join(pieces) if pieces else None

    if not synced_text:
        # as fallback, look for 'plainLyrics' or 'plain_lyrics'
        plain = None
        for key in ("plainLyrics", "plain_lyrics", "lyrics", "plain"):
            if isinstance(got_json, dict) and got_json.get(key):
                plain = got_json.get(key)
                break
        # If plain lyrics available but no synced, we cannot build timed_lyrics. Return None to indicate failure.
        if plain:
            return None  # caller will know fetched but no timed lyrics
        return None

    # Now synced_text should be a string like "[00:00.18] line..."
    parsed = parse_synced_lyrics_text(synced_text)
    if not parsed:
        return None

    # Build output structure like you required
    out = {
        "artist": got_json.get("artist") or got_json.get("artistName") or got_json.get("artistName") or "",
        "title": got_json.get("trackName") or got_json.get("name") or got_json.get("title") or "",
        "has_timed": True,
        "timed_lyrics": parsed,
        "plain_lyrics": got_json.get("plainLyrics") or got_json.get("plain_lyrics") or got_json.get("lyrics") or ""
    }
    # Ensure save_path ends with .json
    save_path = ensure_json_ext(save_path)
    try:
        with open(save_path, "w", encoding="utf8") as f:
            json.dump(out, f, indent=2, ensure_ascii=False)
        return save_path
    except Exception:
        return None

def find_and_save_lyrics(track_title, track_artist, track_album=None, verbose=True):
    # Try direct get
    if verbose: print("[LYRICS] Attempting direct fetch...")
    try:
        got = lrclib_get(track_name=track_title, artist_name=track_artist)
        if got:
            path = lyrics_filepath_for(track_title, track_artist)
            saved = convert_and_save_lrclib_result(got, path)
            if saved:
                if verbose: print(f"[LYRICS] Direct fetch success -> {saved}")
                return saved
    except Exception:
        pass

    # Do searches with multiple query combos
    queries = [
        f"{track_title} {track_artist}",
        f"{track_artist} {track_title}",
        track_title,
        track_artist,
        f"{track_artist} {track_album or ''}"
    ]
    candidates = []
    for q in queries:
        if not q or len(q.strip()) < 2:
            continue
        if verbose: print(f"[LYRICS] Searching lrclib for '{q}' ...")
        res = lrclib_search(q)
        if not res:
            continue
        # res may be dict with data/results or list
        if isinstance(res, dict):
            arr = res.get("data") or res.get("results") or []
        else:
            arr = res
        for item in arr:
            if not isinstance(item, dict):
                continue
            candidates.append(item)

    if not candidates:
        if verbose: print("[LYRICS] No candidates found in search.")
        return None

    # Build match strings
    choices = []
    mapping = {}
    for cand in candidates:
        cand_title = cand.get("track_name") or cand.get("trackName") or cand.get("name") or cand.get("title") or ""
        cand_artist = cand.get("artist_name") or cand.get("artistName") or cand.get("artist") or ""
        label = f"{cand_title} - {cand_artist}"
        choices.append(label)
        mapping[label] = cand

    target = f"{track_title} - {track_artist}"
    best = process.extractOne(target, choices, scorer=fuzz.WRatio, score_cutoff=65)
    if not best:
        best = process.extractOne(target, choices, scorer=fuzz.WRatio, score_cutoff=50)
    if not best:
        if verbose: print("[LYRICS] No fuzzy match found.")
        return None

    best_label, score, _ = best
    chosen = mapping.get(best_label)
    if verbose: print(f"[LYRICS] Best fuzzy match: {best_label} (score={score})")

    # Try fetch by id if present
    rec_id = chosen.get("id") or chosen.get("record_id") or chosen.get("rid")
    got2 = None
    if rec_id:
        try:
            got2 = lrclib_get(record_id=rec_id)
        except Exception:
            got2 = None
    if not got2:
        try:
            cand_t = chosen.get("track_name") or chosen.get("trackName") or chosen.get("name") or ""
            cand_a = chosen.get("artist_name") or chosen.get("artistName") or chosen.get("artist") or ""
            got2 = lrclib_get(track_name=cand_t, artist_name=cand_a)
        except Exception:
            got2 = None

    if not got2:
        if verbose: print("[LYRICS] Failed to fetch details for selected candidate.")
        return None

    path = lyrics_filepath_for(track_title, track_artist)
    saved = convert_and_save_lrclib_result(got2, path)
    if saved:
        if verbose: print(f"[LYRICS] Fetched & saved -> {saved}")
        return saved
    if verbose: print("[LYRICS] Fetch returned but conversion failed.")
    return None

# ========== Lyric index & lookup ==========
def build_lyric_index(times_list, lines_list):
    return {"times": list(times_list), "lines": list(lines_list), "pos_idx": 0}

def lyric_for_time(index_struct, t_seconds):
    times = index_struct["times"]
    lines = index_struct["lines"]
    if not times:
        return "", 0
    idx = index_struct.get("pos_idx", 0)
    if idx < 0: idx = 0
    if idx >= len(times): idx = max(0, len(times) - 1)
    # forward
    while idx + 1 < len(times) and times[idx + 1] <= t_seconds + 0.05:
        idx += 1
    # backward
    while idx > 0 and times[idx] > t_seconds + 0.05:
        idx -= 1
    index_struct["pos_idx"] = idx
    return lines[idx], idx

# ========== Seek detection ==========
def detect_seek(prev_progress, prev_time, current_progress):
    if prev_progress is None:
        return False, 0
    expected = prev_progress + (time.time() - prev_time) * 1000.0
    delta = current_progress - expected
    if abs(delta) > SEEK_THRESHOLD_MS:
        return True, delta
    return False, delta

# ========== Authorization flow ==========
def authorize_if_needed():
    access_token, refresh_token = load_session()
    if access_token and refresh_token:
        print("[SESSION] Using saved session.")
        return access_token, refresh_token

    print("[SESSION] First-time authorization required. Opening browser...")
    threading.Thread(target=start_flask, daemon=True).start()
    webbrowser.open(get_auth_url())

    global received_code
    received_code = None
    start_wait = time.time()
    while received_code is None:
        time.sleep(0.1)
        if time.time() - start_wait > AUTH_TIMEOUT:
            print("[ERROR] Authorization timeout.")
            return None, None

    token_data = swap_code_for_token(received_code)
    if not token_data or "access_token" not in token_data:
        print("[ERROR] Token exchange failed.")
        return None, None

    access_token = token_data["access_token"]
    refresh_token = token_data.get("refresh_token")
    save_session(access_token, refresh_token)
    print("[SESSION] Saved session.")
    return access_token, refresh_token

# ========== Main loop ==========
def main_loop():
    access_token, refresh_token = authorize_if_needed()
    if not access_token:
        print("[ERROR] Could not obtain access token. Exiting.")
        return

    lyric_index = None
    lyric_loaded_for = None
    lyric_load_status = ""

    prev_progress = None
    prev_time = time.time()
    last_track_id = None

    while True:
        data = get_playback(access_token)
        if isinstance(data, dict) and data.get("error") == "expired":
            print("[TOKEN] Access expired. Refreshing...")
            new = refresh_token_request(refresh_token)
            if not new or "access_token" not in new:
                print("[TOKEN] Refresh failed; need re-auth.")
                try:
                    os.remove(SESSION_FILE)
                except Exception:
                    pass
                return
            access_token = new["access_token"]
            refresh_token = new.get("refresh_token", refresh_token)
            save_session(access_token, refresh_token)
            print("[TOKEN] Refreshed and saved.")
            time.sleep(0.5)
            continue
        elif data is None:
            clear_console()
            print("[SPOTIFY] Not playing.")
            if lyric_load_status:
                print("[LYRICS] " + lyric_load_status)
            time.sleep(1.0)
            continue
        elif isinstance(data, dict) and data.get("error"):
            clear_console()
            print("[ERROR] Playback fetch error:", data["error"])
            if lyric_load_status:
                print("[LYRICS] " + lyric_load_status)
            time.sleep(1.0)
            continue

        item = data.get("item")
        if not item:
            clear_console()
            print("[SPOTIFY] No track item (ad/local).")
            time.sleep(0.8)
            continue

        name = item.get("name", "")
        artist = ", ".join([a.get("name", "") for a in item.get("artists", [])])
        album = item.get("album", {}).get("name", "")
        progress_ms = data.get("progress_ms", 0)
        duration_ms = item.get("duration_ms", 0)
        is_playing = data.get("is_playing", False)
        track_id = item.get("id") or f"{name}|{artist}|{album}"

        if track_id != last_track_id:
            lyric_index = None
            lyric_loaded_for = None
            lyric_load_status = ""
            last_track_id = track_id

            candidate_path = lyrics_filepath_for(name, artist)
            # Try load local first
            times_lines = load_lyrics_from_file(candidate_path)
            if times_lines and times_lines[0]:
                times, lines = times_lines
                lyric_index = build_lyric_index(times, lines)
                lyric_loaded_for = (name, artist)
                lyric_load_status = f"Loaded lyrics from {os.path.basename(candidate_path)}"
            else:
                lyric_load_status = "Searching lrclib.net for timed lyrics..."
                clear_console()
                print(f"{name} — {artist} | {format_time_ms(progress_ms)} / {format_time_ms(duration_ms)}")
                print("[LYRICS] " + lyric_load_status)
                saved = find_and_save_lyrics(name, artist, album, verbose=True)
                if saved:
                    times_lines = load_lyrics_from_file(saved)
                    if times_lines and times_lines[0]:
                        times, lines = times_lines
                        lyric_index = build_lyric_index(times, lines)
                        lyric_loaded_for = (name, artist)
                        lyric_load_status = f"Fetched & loaded ({os.path.basename(saved)})"
                    else:
                        # If fetched file lacked timed lyrics, attempt conversion if possible
                        lyric_load_status = "Fetched file did not contain timed lyrics or conversion failed."
                else:
                    lyric_load_status = "Lyrics not found on lrclib.net."

        # Seek detection
        seek_detected, delta = detect_seek(prev_progress, prev_time, progress_ms)
        if seek_detected and lyric_index:
            # reset pos_idx to near the new progress (binary search optimization)
            new_sec = progress_ms / 1000.0
            # find nearest index
            times = lyric_index["times"]
            # simple binary search
            lo, hi = 0, len(times) - 1
            found_idx = 0
            while lo <= hi:
                mid = (lo + hi) // 2
                if times[mid] <= new_sec:
                    found_idx = mid
                    lo = mid + 1
                else:
                    hi = mid - 1
            lyric_index["pos_idx"] = max(0, found_idx)
        prev_progress = progress_ms
        prev_time = time.time()

        # Output
        clear_console()
        status = "PLAYING" if is_playing else "PAUSED "
        print(f"{name} — {artist} | {format_time_ms(progress_ms)} / {format_time_ms(duration_ms)}  [{status}]")
        if lyric_load_status:
            print(f"[LYRICS] {lyric_load_status}")

        if lyric_index:
            cur_sec = progress_ms / 1000.0
            line, idx = lyric_for_time(lyric_index, cur_sec)
            times = lyric_index["times"]
            lines = lyric_index["lines"]
            context_before = 1
            context_after = 1
            start_i = max(0, idx - context_before)
            end_i = min(len(lines) - 1, idx + context_after)
            print("\nLyrics (synced):")
            for i in range(start_i, end_i + 1):
                prefix = "  "
                if i == idx:
                    prefix = "> "
                print(f"{prefix}{format_time_seconds(times[i])}  {lines[i]}")
        else:
            print("\n[LYRICS] No timed lyrics loaded for this track.")

        if seek_detected:
            print(f"\n[SEEK] Detected seek Δ={delta:.0f}ms")

        time.sleep(POLL_INTERVAL)

# ========== ENTRY ==========
if __name__ == "__main__":
    while True:
        try:
            main_loop()
            print("[MAIN] Authorization required again or loop ended. Restarting...")
            time.sleep(1)
            continue
        except KeyboardInterrupt:
            print("\n[EXIT] User interrupted.")
            break
        except Exception as e:
            print("[ERROR] Unhandled exception:", e)
            print("Restarting in 2s...")
            time.sleep(2)
            continue
