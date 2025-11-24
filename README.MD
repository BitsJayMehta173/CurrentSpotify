# 🎧 Spotify Live Lyrics Tracker

A real-time Spotify playback monitor with fully synced lyrics from LRCLIB, powered by PKCE OAuth, async lyric fetch workers, fuzzy matching, and local caching.

This tool displays synchronized lyrics directly in your terminal while you listen on Spotify — no browser or GUI required.

## 📚 Table of Contents

Introduction

Features

Requirements

Project Structure

Installation

Setup & Quick Start

Detailed Explanation

How Lyrics Are Fetched

Spotify Token Handling

Real-Time Optimizations

Troubleshooting


## 🎤 Introduction

Spotify Live Lyrics Tracker monitors your current Spotify track, fetches lyrics from LRCLIB, and displays them in sync with playback.
It intelligently caches results, handles token refresh automatically, and performs fuzzy-matched lyric searches using asynchronous workers for speed and responsiveness.

### 🚀 Features 

#### Authentication & Session

🔑 PKCE Spotify Authorization (no client secret needed)

🔁 Automatic token refresh

💾 Persistent session so you rarely reauthorize

#### Playback Monitoring

🎵 Real-time track polling

🕒 Seek detection

🔄 Instant track-switch handling

#### Lyric Fetching

⚡ Asynchronous LRCLIB worker

🧠 Fuzzy matching (rapidfuzz)

📦 Local JSON lyric cache

🚀 Optimized timestamp lookups

#### Terminal Output

📺 Cross-platform console clear

🎶 Smooth synced line-by-line lyric display

### 📦 Requirements

Install dependencies using:

```sh
pip install -r requirements.txt
```
Libraries:

requests

flask

rapidfuzz


### 📁 Project Structure

```sh
spotify-live-lyrics/
│
├── tem.py
├── requirements.txt
├── README.md
│
└── lyrics_cache/
```

### 🔧 Installation

```sh
git clone <your-repo-url>
cd spotify-live-lyrics
pip install -r requirements.txt
```

### 🟢 Setup & Quick Start

1. Install Dependencies

```sh
pip install -r requirements.txt
```

2. Run the Script
```sh
python spotify_now_playing_windows.py
```

Your browser will open — approve Spotify access (You Must be Logged In with your account).

3. Play Any Song on Spotify

The script will:

detect track changes

load cached lyrics OR fetch new ones

sync them in real time

### 📝 Detailed Explanation
#### 🎵 How Lyrics Are Fetched
1. Local Cache

Looks for:

```sh
lyrics_cache/Artist - Title.json
```

If found → loads instantly.

2. Async Worker

If not cached, a background lyric worker fetches lyrics.
Old searches cancel instantly when tracks change.

3. Direct LRCLIB Query

Attempts perfect match using:
```sh
/api/get?track_name=...&artist_name=...
```

4. LRCLIB Search Fallback

Queries:

"title artist"\
"artist title"\
"title"\
"artist"\
"artist album"

Collects candidates.

5. Fuzzy Matching

Uses rapidfuzz WRatio to choose the best hit.

6. Parsing Lyrics

LRCLIB format:

[mm:ss.xx] text

Converted to JSON:

{
"time": "00:03.19",\
"seconds": 3.19,\
"line": "Some lyric"\
}

### 🔁 Spotify Token Handling

You reauthorize only when:

spotify_session.json is deleted

refresh token fails

Spotify revokes access

### ⚡ Real-Time Optimizations

O(1) timestamp lookup

async worker avoids blocking

instant cancel on track change

zero playback-loop delay

### 🛠 Troubleshooting
Lyrics not found

LRCLIB metadata may differ. Fuzzy matching helps.

Authorization fails

Redirect URI must be:

http://127.0.0.1:8888/callback

Console doesn’t clear

Windows: cls \
Linux/macOS: clear