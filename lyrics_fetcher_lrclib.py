import requests
import json
import sys
import urllib.parse

LRCLIB_API = "https://lrclib.net/api/get"


def fetch_lyrics(artist, title):
    """
    Fetch timed lyrics and plain lyrics from lrclib.net
    
    Returns dict:
    {
        "artist": ...,
        "title": ...,
        "has_timed": True/False,
        "timed_lyrics": [...],
        "plain_lyrics": "...."
    }
    """
    
    params = {
        "artist_name": artist,
        "track_name": title
    }

    url = LRCLIB_API + "?" + urllib.parse.urlencode(params)

    try:
        r = requests.get(url, timeout=10)
    except Exception as e:
        return {"error": f"Network error: {e}"}

    if r.status_code != 200:
        return {"error": f"LRCLib response status {r.status_code}", "artist": artist, "title": title}

    data = r.json()

    # Data returned:
    # {
    #   "id": 123,
    #   "trackName": "...",
    #   "artistName": "...",
    #   "albumName": "...",
    #   "duration": 215000,
    #   "instrumental": false,
    #   "syncedLyrics": " ... LRC text ... ",
    #   "plainLyrics": " ... "
    # }

    synced = data.get("syncedLyrics")
    plain = data.get("plainLyrics")

    result = {
        "artist": artist,
        "title": title,
        "has_timed": False,
        "timed_lyrics": [],
        "plain_lyrics": plain or ""
    }

    if synced:
        timed_lines = parse_lrc(synced)
        result["has_timed"] = True
        result["timed_lyrics"] = timed_lines

    return result


def parse_lrc(lrc_text):
    """
    Parse LRC formatted lyrics:
    [00:15.20] line
    [01:03.10] another line

    Returns:
    [
        {"time": "00:15.20", "seconds": 15.2, "line": "line"},
        ...
    ]
    """

    lines = []
    for raw in lrc_text.split("\n"):
        raw = raw.strip()
        if raw.startswith("[") and "]" in raw:
            try:
                timestamp = raw.split("]")[0].replace("[", "").strip()
                lyric = raw.split("]", 1)[1].strip()

                # convert to seconds
                min_part, sec_part = timestamp.split(":")
                seconds = int(min_part) * 60 + float(sec_part)

                lines.append({
                    "time": timestamp,
                    "seconds": seconds,
                    "line": lyric
                })
            except:
                continue
    return lines


def save_to_json(data, filename):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def main():
    if len(sys.argv) < 3:
        print("Usage:")
        print("  python lyrics_fetcher_lrclib.py \"Artist Name\" \"Song Title\"")
        sys.exit(1)

    artist = sys.argv[1]
    title = sys.argv[2]

    print(f"Searching lyrics for: {artist} - {title}")

    lyrics_data = fetch_lyrics(artist, title)

    if "error" in lyrics_data:
        print("Error:", lyrics_data["error"])
        return

    # Create filename: Artist - Song.json
    safe_artist = artist.replace("/", "_").replace("\\", "_")
    safe_title = title.replace("/", "_").replace("\\", "_")
    filename = f"{safe_artist} - {safe_title}.json"

    save_to_json(lyrics_data, filename)

    if lyrics_data["has_timed"]:
        print(f"✔ TIMED lyrics found and saved to {filename}")
    else:
        print(f"✔ Only plain lyrics available, saved to {filename}")


if __name__ == "__main__":
    main()
