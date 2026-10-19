import base64
import json
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
from pypresence import Presence, ActivityType

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")

CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
IMGBB_KEY = os.getenv("IMGBB_KEY")
if not CLIENT_ID or not IMGBB_KEY:
    raise SystemExit("Missing DISCORD_CLIENT_ID or IMGBB_KEY in .env")

BASE = "http://localhost:8880/api"
COLS = "%artist%,%title%,%album%"
POLL = 3
DEBUG = False  # set True to see logs when running from a terminal

CACHE_FILE = HERE / "art_cache.json"

SIGS = [
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"GIF8", "gif", "image/gif"),
]


def log(*a):
    if DEBUG:
        print(*a)


# ---------- cover cache ----------
try:
    art_cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
except Exception:
    art_cache = {}
art_failed = set()  # keys that failed this session, so we don't retry on every seek


def save_cache():
    try:
        CACHE_FILE.write_text(json.dumps(art_cache), encoding="utf-8")
    except OSError:
        pass


# ---------- helpers ----------
def upload_imgbb(data):
    try:
        up = requests.post(
            "https://api.imgbb.com/1/upload",
            params={"key": IMGBB_KEY},
            data={"image": base64.b64encode(data).decode()},
            timeout=20,
        )
        if up.ok:
            return up.json()["data"]["url"]
        log("imgbb failed:", up.status_code, up.text[:100])
    except (requests.RequestException, KeyError, ValueError) as e:
        log("imgbb error:", e)
    return None


def fix(s):
    if not s or s.strip() == "?":
        return None
    s = s.strip()[:128]
    return s if len(s) >= 2 else s + "\u200b"


def clean_title(t):
    return t.rsplit(" - ", 1)[0] if " - " in t else t


def sniff(b):
    head = b[:64]
    for sig, ext, mime in SIGS:
        i = head.find(sig)
        if i != -1:
            return ext, mime, b[i:]
    return None, None, b


def get_art(item, artist, album, title):
    key = f"{artist}||{album if album != '?' else title}"
    if key in art_cache:
        return art_cache[key]
    if key in art_failed:
        return None
    try:
        r = requests.get(
            f"{BASE}/artwork/{item['playlistId']}/{item['index']}", timeout=5
        )
        if r.status_code == 200:
            ext, mime, data = sniff(r.content)
            if ext:
                url = upload_imgbb(data)
                if url:
                    art_cache[key] = url
                    save_cache()
                    return url
    except (requests.RequestException, KeyError, ValueError) as e:
        log("art error:", e)
    art_failed.add(key)
    return None


# ---------- discord connection ----------
rpc = None


def connect():
    global rpc
    while True:
        try:
            rpc = Presence(CLIENT_ID)
            rpc.connect()
            log("connected to Discord")
            return
        except Exception:
            time.sleep(10)  # Discord not open yet, retry


connect()

# last = what we last sent: (mode, title, artist, start)
last = None


def clear_status():
    global last
    if last is not None:
        try:
            rpc.clear()
        except Exception:
            pass
    last = None


# ---------- main loop ----------
while True:
    # 1) ask foobar what's going on
    try:
        p = requests.get(
            f"{BASE}/player", params={"columns": COLS}, timeout=3
        ).json()["player"]
    except (requests.RequestException, ValueError, KeyError):
        clear_status()  # foobar closed
        time.sleep(POLL)
        continue

    state = p.get("playbackState")
    item = p.get("activeItem") or {}

    # 2) push to Discord
    try:
        if state in ("playing", "paused") and item.get("duration", 0) > 0:
            artist, title, album = item["columns"]
            art = None

            if state == "playing":
                start = int(time.time() - item["position"])
                end = int(start + item["duration"])
                changed = (
                    last is None
                    or last[0] != "playing"
                    or last[1] != title
                    or last[2] != artist
                    or abs(last[3] - start) > 3  # seek or repeat
                )
                if changed:
                    art = get_art(item, artist, album, title)
                    rpc.update(
                        activity_type=ActivityType.LISTENING,
                        details=fix(title),
                        state=fix(artist),
                        large_image=art,
                        large_text=fix(album),
                        small_image="logo",
                        small_text="foobar2000",
                        start=start,
                        end=end,
                    )
                    last = ("playing", title, artist, start)
            else:  # paused
                changed = (
                    last is None
                    or last[0] != "paused"
                    or last[1] != title
                    or last[2] != artist
                )
                if changed:
                    art = get_art(item, artist, album, title)
                    rpc.update(
                        activity_type=ActivityType.LISTENING,
                        details=fix(title),
                        state=fix(artist),
                        large_image=art,
                        large_text=fix(album),
                        small_image="stopped",
                        small_text="Paused"
                    )
                    last = ("paused", title, artist, 0)
        else:
            clear_status()  # stopped / nothing playing

    except Exception as e:
        # Discord closed or the pipe broke: reconnect and resend next loop
        log("discord error:", e)
        last = None
        try:
            rpc.close()
        except Exception:
            pass
        connect()

    time.sleep(POLL)