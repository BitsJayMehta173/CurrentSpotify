#!/usr/bin/env python3
"""
spotify_with_lyrics_final.py
Unified Spotify tracker + lyrics fetcher + converter + synced console display.

Requirements:
    pip install requests flask rapidfuzz

Notes:
 - Configure CLIENT_ID (Spotify app) before use.
 - Uses PKCE auth with local Flask redirect (http://127.0.0.1:8888/callback).
 - Saves session to spotify_session.json so users won't reauthorize frequently.
 - Lyrics saved to lyrics_cache/<Artist> - <Title>.json
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

# ---------------- CONFIG ----------------
CLIENT_ID = "6e275fbc81f14f50a9e34de55c7417c0"  # put your Spotify app client id
REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPES = "user-read-playback-state"
SESSION_FILE = "spotify_session.json"
AUTH_TIMEOUT = 60  # seconds to wait for user authorization
NETWORK_RETRY_LIMIT = 5
POLL_INTERVAL = 0.5  # seconds
SEEK_THRESHOLD_MS = 1500
LYRICS_FOLDER = "lyrics_cache"
LRCLIB_BASE = "https://lrclib.net"

os.makedirs(LYRICS_FOLDER, exist_ok=True)

# ---------------- PKCE ----------------
def generate_pkce_pair():
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().replace("=", "")
    return verifier, challenge

CODE_VERIFIER, CODE_CHALLENGE = generate_pkce_pair()

# ---------------- Flask callback ----------------
app = Flask(__name__)
_received_code = None

@app.route("/callback")
def callback():
    global _received_code
    _received_code = request.args.get("code")
    return "<html><body><h2>Authorization complete — you can close this window.</h2></body></html>"

def start_flask():
    # Flask built-in dev server is fine for local redirect
    app.run(port=8888, debug=False)

# ---------------- HTTP helpers with retry ----------------
def get_with_retries(url, params=None, headers=None, timeout=6, retries=NETWORK_RETRY_LIMIT):
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=timeout)
            return r
        except Exception:
            if attempt + 1 == retries:
                raise
            time.sleep(1)
    raise RuntimeError("Unreachable")

def post_with_retries(url, data, timeout=6, retries=NETWORK_RETRY_LIMIT):
    for attempt in range(retries):
        try:
            r = requests.post(url, data=data, timeout=timeout)
            return r
        except Exception:
            if attempt + 1 == retries:
                raise
            time.sleep(1)
    raise RuntimeError("Unreachable")

# ---------------- Token exchange / refresh ----------------
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

# ---------------- Session persistence ----------------
def save_session(access_token, refresh_token):
    with open(SESSION_FILE, "w", encoding="utf8") as f:
        json.dump({"access_token": access_token, "refresh_token": refresh_token, "saved_at": time.time()}, f)

def load_session():
    if not os.path.exists(SESSION_FILE):
        return None, None
    try:
        with open(SESSION_FILE, "r", encoding="utf8") as f:
            data = json.load(f)
            return data.get("access_token"), data.get("refresh_token")
    except Exception:
        return None, None

# ---------------- Spotify Player API ----------------
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

# ---------------- Utilities ----------------
def sanitize_filename(s):
    # allow simple characters, replace problematic characters
    s = s.strip()
    s = s.replace("/", "-").replace("\\", "-")
    s = re.sub(r"[:<>\"|?*]", "", s)
    # collapse whitespace
    s = re.sub(r"\s+", " ", s)
    return s

def lyrics_filepath_for(title, artist):
    fname = f"{artist} - {title}"
    fname = sanitize_filename(fname)
    if not fname.lower().endswith(".json"):
        fname = fname + ".json"
    return os.path.join(LYRICS_FOLDER, fname)

def clear_console():
    os.system("cls" if os.name == "nt" else "clear")

def format_time_ms(ms):
    if ms is None:
        return "0:00"
    total = int(ms // 1000)
    m, s = divmod(total, 60)
    return f"{m}:{s:02d}"

def format_time_seconds(sec):
    total = int(sec)
    m, s = divmod(total, 60)
    return f"{m}:{s:02d}"

# ---------------- LRCLIB helpers ----------------
def lrclib_search(query):
    try:
        r = get_with_retries(LRCLIB_BASE + "/api/search", params={"query": query})
        if r.status_code == 200:
            return r.json()
    except Exception:
        return None
    return None

def lrclib_get(track_name=None, artist_name=None, record_id=None):
    try:
        params = {}
        if record_id:
            params["id"] = record_id
        if track_name:
            params["track_name"] = track_name
        if artist_name:
            params["artist_name"] = artist_name
        r = get_with_retries(LRCLIB_BASE + "/api/get", params=params)
        if r.status_code == 200:
            return r.json()
    except Exception:
        return None
    return None

# parse synced lyrics strings like "[00:00.18] Line text\n[00:02.76] ...\n"
SYNCED_LINE_RE = re.compile(r"\[?(\d{2}):(\d{2}(?:\.\d+)?)\]?")

def convert_lrclib_to_target_json(got_json):
    """
    Input: the raw lrclib 'get' response (various fields).
    Output: normalized dict matching your desired format.
    """
    # Attempt to extract artist/title
    title = got_json.get("name") or got_json.get("trackName") or got_json.get("track_name") or got_json.get("title") or got_json.get("name")
    artist = got_json.get("artistName") or got_json.get("artist") or got_json.get("artist_name") or got_json.get("artistName")
    if isinstance(artist, list):
        artist = ", ".join(artist)
    if title is None:
        title = ""
    if artist is None:
        artist = ""

    # plain lyrics: attempt many keys
    plain = got_json.get("plainLyrics") or got_json.get("plain_lyrics") or got_json.get("plainLyricsText") or got_json.get("lyrics") or ""
    # synced lyrics either as string ('syncedLyrics') or as array
    synced_raw = got_json.get("syncedLyrics") or got_json.get("synced_lyrics") or got_json.get("lrc") or None

    timed = []
    # If synced_raw is a single string with [mm:ss.xx] tags:
    if isinstance(synced_raw, str) and synced_raw.strip():
        lines = synced_raw.splitlines()
        for ln in lines:
            m = SYNCED_LINE_RE.search(ln)
            if not m:
                continue
            mm = int(m.group(1))
            ss = float(m.group(2))
            seconds = mm * 60 + ss
            # rest of the line after the timestamp
            # find position of the timestamp substring then take the remainder
            # handle multiple tags per line by taking substring after last tag
            last_tag_end = 0
            for tag in re.finditer(r"\[\d{2}:\d{2}(?:\.\d+)?\]", ln):
                last_tag_end = tag.end()
            text = ln[last_tag_end:].strip()
            timed.append({"time": f"{mm:02d}:{ss:05.2f}", "seconds": seconds, "line": text})
    # If synced_raw is present but in different shape:
    elif isinstance(synced_raw, list):
        for entry in synced_raw:
            # attempt to accept {time: "...", text: "..."}, or string tuple
            if isinstance(entry, dict):
                t = None
                if "seconds" in entry:
                    try:
                        t = float(entry["seconds"])
                    except Exception:
                        t = None
                elif "time" in entry:
                    try:
                        parts = entry["time"].split(":")
                        mm = int(parts[0])
                        ss = float(parts[1])
                        t = mm * 60 + ss
                    except Exception:
                        t = None
                text = entry.get("line") or entry.get("text") or entry.get("lyric") or ""
                if t is not None:
                    minutes = int(t // 60)
                    sec_frac = t - minutes * 60
                    timed.append({"time": f"{minutes:02d}:{sec_frac:05.2f}", "seconds": round(t, 3), "line": text})
            elif isinstance(entry, str):
                # try parse like "[00:05.03] text"
                m = SYNCED_LINE_RE.search(entry)
                if not m:
                    continue
                mm = int(m.group(1))
                ss = float(m.group(2))
                seconds = mm*60 + ss
                last_tag_end = 0
                for tag in re.finditer(r"\[\d{2}:\d{2}(?:\.\d+)?\]", entry):
                    last_tag_end = tag.end()
                text = entry[last_tag_end:].strip()
                timed.append({"time": f"{mm:02d}:{ss:05.2f}", "seconds": seconds, "line": text})

    # If no timed lyrics parsed from synced_raw, attempt to parse from other fields in fallback
    if not timed and plain:
        # we can't time plain lyrics; just create a single entry at 0
        timed = [{"time": "00:00.00", "seconds": 0.0, "line": plain.splitlines()[0] if plain.splitlines() else ""}]

    result = {
        "artist": artist,
        "title": title,
        "has_timed": bool(timed),
        "timed_lyrics": timed,
        "plain_lyrics": plain or ""
    }
    return result

# ---------------- Lyrics loader & converter ----------------
def load_lyrics_from_file(filepath):
    """
    Loads lyrics saved in the standardized JSON format.
    Returns (times_list, lines_list) or (None, None) on failure.
    """
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
        # defensive parsing
        if isinstance(entry, dict):
            sec = None
            if "seconds" in entry:
                try:
                    sec = float(entry["seconds"])
                except Exception:
                    sec = None
            elif "time" in entry:
                try:
                    mm, rest = entry["time"].split(":")
                    sec = int(mm) * 60 + float(rest)
                except Exception:
                    sec = None
            text = entry.get("line", "")
            if sec is not None:
                times.append(float(sec))
                lines.append(text)
    if not times:
        return None, None
    # ensure sorted
    combined = sorted(zip(times, lines), key=lambda x: x[0])
    times_sorted, lines_sorted = zip(*combined)
    return list(times_sorted), list(lines_sorted)

# ---------------- Lyric-finder worker (threaded & cancellable) ----------------
class LyricWorker(threading.Thread):
    """
    Worker thread that performs lyric search/fetch/convert/save for a given track.
    It updates status via a shared dict and can be cancelled by setting worker.cancel_event.
    """
    def __init__(self, title, artist, album, status_dict):
        super().__init__(daemon=True)
        self.title = title
        self.artist = artist
        self.album = album
        self.status = status_dict  # dict with keys: 'state', 'message', 'result_path'
        self.cancel_event = threading.Event()

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        # step 1: try direct GET
        self.status['state'] = 'searching'
        self.status['message'] = 'Trying direct lrclib get...'
        self.status['result_path'] = None
        if self.cancel_event.is_set(): return
        try:
            got = lrclib_get(track_name=self.title, artist_name=self.artist)
        except Exception:
            got = None
        if got:
            converted = convert_lrclib_to_target_json(got)
            # save
            path = lyrics_filepath_for(self.title, self.artist)
            try:
                with open(path, "w", encoding="utf8") as f:
                    json.dump(converted, f, ensure_ascii=False, indent=2)
                self.status['state'] = 'done'
                self.status['message'] = 'Found (direct). Saved.'
                self.status['result_path'] = path
                return
            except Exception as e:
                self.status['state'] = 'error'
                self.status['message'] = f"Failed saving direct result: {e}"
                return

        if self.cancel_event.is_set(): return

        # step 2: search variants and fuzzy-match
        self.status['message'] = 'Direct missing — searching lrclib.net...'
        queries = [
            f"{self.title} {self.artist}",
            f"{self.artist} {self.title}",
            self.title,
            self.artist,
            f"{self.artist} {self.album or ''}"
        ]
        candidates = []
        seen_ids = set()
        for q in queries:
            if self.cancel_event.is_set(): return
            try:
                res = lrclib_search(q)
            except Exception:
                res = None
            if not res:
                continue
            # responses vary; normalize to list of dicts
            arr = []
            if isinstance(res, dict):
                # try keys 'data', 'results'
                if 'data' in res and isinstance(res['data'], list):
                    arr = res['data']
                elif 'results' in res and isinstance(res['results'], list):
                    arr = res['results']
                else:
                    # maybe it's a single record dict
                    arr = [res]
            elif isinstance(res, list):
                arr = res
            else:
                continue
            for rec in arr:
                if not isinstance(rec, dict):
                    continue
                # avoid duplicates by id or title/artist
                rec_id = rec.get("id") or rec.get("rid") or rec.get("record_id")
                key = rec_id or (rec.get("track_name") or rec.get("name", "")) + "|" + (rec.get("artist_name") or rec.get("artist", ""))
                if key in seen_ids:
                    continue
                seen_ids.add(key)
                candidates.append(rec)
        if not candidates:
            self.status['state'] = 'notfound'
            self.status['message'] = 'No candidates from lrclib search.'
            return

        # build choice labels
        choices = []
        mapping = {}
        for cand in candidates:
            cand_title = cand.get("track_name") or cand.get("title") or cand.get("name") or ""
            cand_artist = cand.get("artist_name") or cand.get("artist") or cand.get("artistName") or ""
            label = f"{cand_title} - {cand_artist}"
            choices.append(label)
            mapping[label] = cand

        target = f"{self.title} - {self.artist}"
        # fuzzy match with rapidfuzz
        best = process.extractOne(target, choices, scorer=fuzz.WRatio, score_cutoff=60)
        if not best:
            # try title-only matching
            best = process.extractOne(self.title, choices, scorer=fuzz.partial_ratio, score_cutoff=50)
        if not best:
            self.status['state'] = 'notfound'
            self.status['message'] = 'No fuzzy match found.'
            return

        best_label, score, _ = best
        best_item = mapping.get(best_label)
        self.status['message'] = f"Best candidate: {best_label} (score={score})"
        if self.cancel_event.is_set(): return

        rec_id = best_item.get("id") or best_item.get("rid") or best_item.get("record_id")
        got2 = None
        if rec_id:
            try:
                got2 = lrclib_get(record_id=rec_id)
            except Exception:
                got2 = None
        if not got2:
            try:
                cand_title = best_item.get("track_name") or best_item.get("title") or best_item.get("name") or ""
                cand_artist = best_item.get("artist_name") or best_item.get("artist") or best_item.get("artistName") or ""
                got2 = lrclib_get(track_name=cand_title, artist_name=cand_artist)
            except Exception:
                got2 = None
        if not got2:
            self.status['state'] = 'failed'
            self.status['message'] = 'Failed to fetch lyric details for candidate.'
            return

        # convert and save
        try:
            converted = convert_lrclib_to_target_json(got2)
            path = lyrics_filepath_for(self.title, self.artist)
            with open(path, "w", encoding="utf8") as f:
                json.dump(converted, f, ensure_ascii=False, indent=2)
            self.status['state'] = 'done'
            self.status['message'] = 'Fetched & saved lyrics.'
            self.status['result_path'] = path
            return
        except Exception as e:
            self.status['state'] = 'error'
            self.status['message'] = f"Failed to save converted lyrics: {e}"
            return

# ---------------- Lyric index & sync utilities ----------------
def build_lyric_index(times_list, lines_list):
    return {"times": times_list, "lines": lines_list, "pos_idx": 0}

def lyric_for_time(index_struct, t_seconds):
    times = index_struct["times"]
    lines = index_struct["lines"]
    idx = index_struct.get("pos_idx", 0)
    if idx < 0: idx = 0
    if idx >= len(times): idx = max(0, len(times)-1)
    # advance forward
    while idx + 1 < len(times) and times[idx + 1] <= t_seconds + 0.05:
        idx += 1
    # backtrack if needed
    while idx > 0 and times[idx] > t_seconds + 0.05:
        idx -= 1
    index_struct["pos_idx"] = idx
    return lines[idx] if lines else "", idx

# ---------------- Seek detection ----------------
def detect_seek(prev_progress, prev_time, current_progress):
    if prev_progress is None:
        return False, 0
    expected = prev_progress + (time.time() - prev_time) * 1000.0
    delta = current_progress - expected
    if abs(delta) > SEEK_THRESHOLD_MS:
        return True, delta
    return False, delta

# ---------------- Authorization flow ----------------
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

def authorize_if_needed():
    access_token, refresh_token = load_session()
    if access_token and refresh_token:
        print("[SESSION] Using saved session.")
        return access_token, refresh_token

    print("[SESSION] First-time authorization required. Opening browser...")
    threading.Thread(target=start_flask, daemon=True).start()
    webbrowser.open(get_auth_url())

    global _received_code
    _received_code = None
    start_wait = time.time()
    while _received_code is None:
        time.sleep(0.1)
        if time.time() - start_wait > AUTH_TIMEOUT:
            print(f"[ERROR] Authorization timeout ({AUTH_TIMEOUT}s).")
            return None, None

    token_data = swap_code_for_token(_received_code)
    if not token_data or "access_token" not in token_data:
        print("[ERROR] Token exchange failed.")
        return None, None
    access_token = token_data["access_token"]
    refresh_token = token_data.get("refresh_token")
    save_session(access_token, refresh_token)
    print("[SESSION] Saved session.")
    return access_token, refresh_token

# ---------------- Main loop with non-blocking lyric worker ----------------
def main_loop():
    access_token, refresh_token = authorize_if_needed()
    if not access_token:
        print("[ERROR] Could not obtain access token.")
        return

    lyric_index = None
    lyric_for_track = None
    lyric_status = {"state": None, "message": "", "result_path": None}
    current_worker = None

    prev_progress = None
    prev_time = time.time()
    last_track_id = None

    while True:
        data = get_playback(access_token)
        if isinstance(data, dict) and data.get("error") == "expired":
            # refresh token
            try:
                new = refresh_token_request(refresh_token)
            except Exception:
                new = None
            if not new or "access_token" not in new:
                print("[TOKEN] Refresh failed. Clearing session and forcing re-auth.")
                try:
                    os.remove(SESSION_FILE)
                except Exception:
                    pass
                return  # outer loop will restart
            access_token = new["access_token"]
            refresh_token = new.get("refresh_token", refresh_token)
            save_session(access_token, refresh_token)
            print("[TOKEN] Refreshed session.")
            time.sleep(0.5)
            continue

        if data is None:
            clear_console()
            print("[SPOTIFY] Not playing anything.")
            if lyric_status["message"]:
                print("[LYRICS] " + lyric_status["message"])
            time.sleep(1.0)
            continue
        if isinstance(data, dict) and data.get("error"):
            clear_console()
            print("[ERROR] Playback fetch error:", data["error"])
            if lyric_status["message"]:
                print("[LYRICS] " + lyric_status["message"])
            time.sleep(1.0)
            continue

        item = data.get("item")
        if not item:
            clear_console()
            print("[SPOTIFY] No track info (ad/local).")
            time.sleep(0.8)
            continue

        name = item.get("name", "")
        artist = ", ".join([a.get("name","") for a in item.get("artists", [])])
        album = item.get("album", {}).get("name", "")
        progress_ms = data.get("progress_ms", 0)
        duration_ms = item.get("duration_ms", 0)
        is_playing = data.get("is_playing", False)
        track_id = item.get("id") or f"{name}|{artist}|{album}"

        # immediate track change handling:
        if track_id != last_track_id:
            # If a worker is running, cancel it and let it stop quickly
            if current_worker and hasattr(current_worker, "cancel"):
                current_worker.cancel()
            lyric_index = None
            lyric_for_track = None
            lyric_status = {"state": "idle", "message": "Looking for local lyrics...", "result_path": None}
            last_track_id = track_id

            # First try to load local file
            candidate = lyrics_filepath_for(name, artist)
            if os.path.exists(candidate):
                times, lines = load_lyrics_from_file(candidate)
                if times and lines:
                    lyric_index = build_lyric_index(times, lines)
                    lyric_for_track = (name, artist)
                    lyric_status = {"state": "loaded", "message": f"Loaded local lyrics: {os.path.basename(candidate)}", "result_path": candidate}
                else:
                    lyric_status = {"state": "invalid_local", "message": "Local lyrics file invalid.", "result_path": candidate}
            else:
                # Launch worker to find & save lyrics asynchronously
                lyric_status = {"state": "searching", "message": "Searching lrclib.net...", "result_path": None}
                worker = LyricWorker(name, artist, album, lyric_status)
                current_worker = worker
                worker.start()

        # seek detection
        seek_detected, delta = detect_seek(prev_progress, prev_time, progress_ms)
        if seek_detected and lyric_index:
            # reset small index; lyric_for_time will optimize forward
            lyric_index["pos_idx"] = 0

        prev_progress = progress_ms
        prev_time = time.time()

        # If worker finished and saved a result, load it
        if lyric_status.get("state") == "done" and lyric_status.get("result_path"):
            path = lyric_status["result_path"]
            times, lines = load_lyrics_from_file(path)
            if times and lines:
                lyric_index = build_lyric_index(times, lines)
                lyric_for_track = (name, artist)
                lyric_status["message"] = f"Loaded fetched lyrics: {os.path.basename(path)}"
            else:
                lyric_status["state"] = "invalid_fetched"
                lyric_status["message"] = "Fetched lyrics file invalid."

        # UI output
        clear_console()
        state_label = "PLAYING" if is_playing else "PAUSED "
        print(f"{name} — {artist} | {format_time_ms(progress_ms)} / {format_time_ms(duration_ms)}  [{state_label}]")
        if lyric_status.get("message"):
            print("[LYRICS] " + lyric_status["message"])

        # display synced lyrics if available
        if lyric_index:
            current_seconds = progress_ms / 1000.0
            line, idx = lyric_for_time(lyric_index, current_seconds)
            times = lyric_index["times"]
            lines = lyric_index["lines"]
            # show context lines (-1, current, +1)
            start_i = max(0, idx - 1)
            end_i = min(len(lines) - 1, idx + 1)
            print("\nLyrics (synced):")
            for i in range(start_i, end_i + 1):
                prefix = "> " if i == idx else "  "
                print(f"{prefix}{format_time_seconds(times[i])}  {lines[i]}")
        else:
            print("\n[LYRICS] No synced lyrics loaded for this track.")

        # show seek detection if occurred
        if seek_detected:
            print(f"\n[SEEK] Detected seek Δ={delta:.0f}ms")

        # small sleep
        time.sleep(POLL_INTERVAL)

# ---------------- Entry point ----------------
if __name__ == "__main__":
    # Outer loop to re-run auth if needed
    while True:
        try:
            main_loop()
            print("[MAIN] main_loop exited — restarting authorization flow...")
            time.sleep(1)
            continue
        except KeyboardInterrupt:
            print("\n[EXIT] Interrupted by user.")
            break
        except Exception as e:
            print("[ERROR] Unhandled exception:", e)
            print("Restarting in 2s...")
            time.sleep(2)
            continue
