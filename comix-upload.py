#!/usr/bin/env python3
"""
comix-upload — chapter uploader for comix.to

Logs in with the comix.to cookies from your Firefox profile (or a cookies.txt),
then uploads chapter archives through the site's own 3-step upload API
(init -> chunked PUT -> finalize). New uploads land in the site's review queue.

Usage:
    py comix-upload.py --whoami
    py comix-upload.py --group-titles
    py comix-upload.py --series "My Series" --zip "K:\\path\\ch012.zip" --chapter 12 --volume 2 --title "Title"
    py comix-upload.py --manga abc12 --zip "K:\\path\\ch012.zip" --chapter 12
    py comix-upload.py --series "My Series" --folder "K:\\path\\chapters" --start 1 --end 20
    py comix-upload.py --list-uploads
    py comix-upload.py --delete 11543681
    py comix-upload.py --dry-run --series "My Series" --folder "K:\\path\\chapters"

--manga takes a title's hash id (the "abc12" in comix.to/title/abc12-some-name)
or the full title URL. --series looks the hash id up in series.json.
"""

import argparse
import configparser
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import zipfile

import requests


# ── Constants ─────────────────────────────────────────────────────────────────

SITE           = "https://comix.to"
COOKIE_DOMAIN  = "comix.to"
SCRIPT_DIR     = os.path.dirname(os.path.abspath(__file__))
IMAGE_EXTS     = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif"}
ARCHIVE_EXTS   = {".zip", ".cbz", ".cbr", ".rar", ".7z"}
MAX_BYTES      = 1 << 30            # site limit: 1 GB per archive
MAX_RETRIES    = 3
RETRY_BACKOFF  = [5, 15, 45]        # seconds between retries
DEFAULT_UA     = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) "
                  "Gecko/20100101 Firefox/140.0")
API_HEADERS    = {
    "Accept":           "application/json",
    "X-Requested-With": "XMLHttpRequest",
    "X-Rt":             "0",
}


# ── Terminal helpers ───────────────────────────────────────────────────────────

def bold(t):   return f"\033[1m{t}\033[0m"
def dim(t):    return f"\033[2m{t}\033[0m"
def green(t):  return f"\033[32m{t}\033[0m"
def red(t):    return f"\033[31m{t}\033[0m"
def yellow(t): return f"\033[33m{t}\033[0m"
def cyan(t):   return f"\033[36m{t}\033[0m"


def format_size(n):
    if n >= 1 << 30: return f"{n/(1<<30):.1f} GB"
    if n >= 1 << 20: return f"{n/(1<<20):.1f} MB"
    if n >= 1 << 10: return f"{n/(1<<10):.0f} KB"
    return f"{n} B"


def num_str(n):
    """12.0 -> '12', 12.5 -> '12.5'"""
    return f"{n:g}"


def api_number(n):
    """The API takes whole chapters as ints and half chapters as floats."""
    return int(n) if float(n).is_integer() else float(n)


# ── Cookies ────────────────────────────────────────────────────────────────────

def _find_firefox_profile():
    """Profile folder that actually has a cookies.sqlite, preferring default-release."""
    firefox_dir  = os.path.expandvars(r"%APPDATA%\Mozilla\Firefox")
    profiles_ini = os.path.join(firefox_dir, "profiles.ini")
    if not os.path.isfile(profiles_ini):
        return None
    cfg = configparser.ConfigParser()
    cfg.read(profiles_ini, encoding="utf-8")
    candidates = []
    for section in cfg.sections():
        if not section.startswith("Profile"):
            continue
        path = cfg.get(section, "Path", fallback="")
        if cfg.getint(section, "IsRelative", fallback=1):
            path = os.path.join(firefox_dir, path)
        if os.path.isfile(os.path.join(path, "cookies.sqlite")):
            candidates.append(path)
    for c in candidates:
        if "default-release" in c:
            return c
    return candidates[0] if candidates else None


def read_firefox_cookies(profile=None):
    """
    Read comix.to cookies straight from Firefox's cookies.sqlite (Firefox doesn't
    encrypt them). Works with Firefox open: the db and its -wal file are copied first,
    since a fresh login sits in the -wal file until Firefox checkpoints it.
    """
    profile = profile or _find_firefox_profile()
    if not profile:
        raise RuntimeError("No Firefox profile with cookies.sqlite found.")
    db_path = os.path.join(profile, "cookies.sqlite")
    tmp_dir = tempfile.mkdtemp(prefix="comix-cookies-")
    tmp_db  = os.path.join(tmp_dir, "cookies.sqlite")
    try:
        shutil.copy2(db_path, tmp_db)
        for ext in ("-wal", "-shm"):
            if os.path.exists(db_path + ext):
                shutil.copy2(db_path + ext, tmp_db + ext)
        conn = sqlite3.connect(tmp_db)
        rows = conn.execute(
            "SELECT name, value FROM moz_cookies WHERE host LIKE ?",
            (f"%{COOKIE_DOMAIN}",),
        ).fetchall()
        conn.close()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    if not rows:
        raise RuntimeError(
            f"No {COOKIE_DOMAIN} cookies in Firefox profile {profile}.\n"
            f"Log in to {SITE} in Firefox (tick 'remember me') and try again."
        )
    return dict(rows)


def read_cookies_file(path):
    """Netscape cookies.txt, or a JSON export ([{name, value, domain}, ...] or {name: value})."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if text.lstrip().startswith(("[", "{")):
        data = json.loads(text)
        if isinstance(data, dict):
            return data
        return {c["name"]: c["value"] for c in data
                if COOKIE_DOMAIN in c.get("domain", COOKIE_DOMAIN)}
    cookies = {}
    for line in text.splitlines():
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7 and COOKIE_DOMAIN in parts[0]:
            cookies[parts[5]] = parts[6].strip()
    if not cookies:
        raise RuntimeError(f"No {COOKIE_DOMAIN} cookies found in {path}")
    return cookies


# ── Session / API ──────────────────────────────────────────────────────────────

class ComixError(Exception):
    pass


def make_session(cfg, need_login=True):
    s = requests.Session()
    s.headers["User-Agent"] = cfg.get("user_agent") or DEFAULT_UA
    if not need_login:
        return s
    source = (cfg.get("cookie_source") or "firefox").lower()
    if source == "file":
        path = cfg.get("cookies_file") or os.path.join(SCRIPT_DIR, "cookies.txt")
        if not os.path.isabs(path):
            path = os.path.join(SCRIPT_DIR, path)
        cookies = read_cookies_file(path)
    else:
        cookies = read_firefox_cookies(cfg.get("firefox_profile") or None)
    for k, v in cookies.items():
        s.cookies.set(k, v, domain=COOKIE_DOMAIN)
    return s


def api(s, method, path, *, retries=MAX_RETRIES, **kw):
    """Call a comix.to JSON endpoint and return its "result". Retries network/5xx errors."""
    headers = {**API_HEADERS, **kw.pop("headers", {})}
    for attempt in range(retries + 1):
        try:
            r = s.request(method, SITE + path, headers=headers, timeout=kw.pop("timeout", 60), **kw)
        except requests.RequestException as e:
            err = f"network error: {e}"
        else:
            if r.status_code < 500:
                try:
                    j = r.json()
                except ValueError:
                    raise ComixError(
                        f"{method} {path} -> HTTP {r.status_code}, not JSON "
                        f"(Cloudflare page? open {SITE} in Firefox once).")
                if r.status_code >= 400 or j.get("status") not in (None, "ok"):
                    raise ComixError(f"{method} {path} -> HTTP {r.status_code}: "
                                     f"{j.get('error') or j.get('message') or j}")
                return j.get("result", j)
            err = f"HTTP {r.status_code}"
        if attempt < retries:
            wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
            print(yellow(f"  {method} {path}: {err}, retrying in {wait}s..."))
            time.sleep(wait)
    raise ComixError(f"{method} {path} failed: {err}")


def whoami(s):
    user = api(s, "GET", "/api/v1/user")
    if not user:
        raise ComixError(
            "Not logged in: the cookies didn't give a comix.to session.\n"
            f"Log in to {SITE} in Firefox (or refresh cookies.txt) and try again.")
    return user


def title_info(s, hid):
    """Numeric id, url, title and latest chapter for a title hash id (no login needed)."""
    r = s.get(f"{SITE}/title/{hid}", timeout=60)
    m = re.search(r'<script type="application/json" id="initial-data">(.*?)</script>', r.text, re.S)
    if r.status_code != 200 or not m:
        raise ComixError(f"Title {hid!r} not found on {SITE} (HTTP {r.status_code}).")
    queries = json.loads(m.group(1)).get("queries", {})
    for key, val in queries.items():
        if key.startswith('["manga","detail"') and isinstance(val, dict) and val.get("id"):
            return val
    raise ComixError(f"Couldn't read the title data for {hid!r}.")


def group_titles(s, group_id):
    items, page = [], 1
    while True:
        res = api(s, "GET", f"/api/v1/groups/{group_id}/titles",
                  params={"page": page, "limit": 28})
        items += res.get("items", [])
        if not res.get("meta", {}).get("hasNext"):
            return items
        page += 1


def my_uploads(s):
    items, page = [], 1
    while True:
        res = api(s, "GET", "/api/v1/user/uploads", params={"page": page, "limit": 50})
        items += res.get("items", [])
        if not res.get("meta", {}).get("hasNext"):
            return items
        page += 1


# ── Upload ─────────────────────────────────────────────────────────────────────

def upload_archive(s, manga_id, group_id, number, volume, title, official,
                   data, replace_id=None):
    """
    data: bytes, or a path to an archive. Returns the finalize result.
    1. POST /api/v1/uploads/init -> upload_id, chunk_url, chunk_size
    2. PUT chunk_url with Content-Range for each chunk
    3. POST /api/v1/uploads/{upload_id}/finalize
    """
    total = len(data) if isinstance(data, (bytes, bytearray)) else os.path.getsize(data)
    if total > MAX_BYTES:
        raise ComixError(f"Archive is {format_size(total)}; comix.to allows up to 1 GB.")

    body = {
        "manga_id":    manga_id,
        "group_id":    group_id or 0,
        "number":      api_number(number),
        "is_official": bool(official),
        "total_bytes": total,
    }
    if volume is not None:
        body["volume"] = int(volume)
    if title:
        body["title"] = title[:140]
    if replace_id:
        body["replace_chapter_id"] = replace_id

    init       = api(s, "POST", "/api/v1/uploads/init", json=body)
    upload_id  = init["upload_id"]
    chunk_url  = init["chunk_url"]
    chunk_size = int(init.get("chunk_size") or 3 << 20)

    stream = io.BytesIO(data) if isinstance(data, (bytes, bytearray)) else open(data, "rb")
    try:
        offset, started, shown = 0, time.time(), -1
        while offset < total:
            stream.seek(offset)
            chunk = stream.read(chunk_size)
            end   = offset + len(chunk) - 1
            for attempt in range(MAX_RETRIES + 1):
                try:
                    r = s.put(SITE + chunk_url, data=chunk, timeout=300, headers={
                        "Accept":        "application/json, text/plain, */*",
                        "Content-Type":  "application/octet-stream",
                        "Content-Range": f"bytes {offset}-{end}/{total}",
                    })
                    if r.status_code < 400:
                        break
                    err = f"HTTP {r.status_code}: {r.text[:200]}"
                    if r.status_code < 500:
                        raise ComixError(f"Chunk upload refused: {err}")
                except requests.RequestException as e:
                    err = f"network error: {e}"
                if attempt == MAX_RETRIES:
                    raise ComixError(f"Chunk at byte {offset} failed: {err}")
                wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
                print(yellow(f"\n  chunk {offset}: {err}, retrying in {wait}s..."))
                time.sleep(wait)
            # The server reports how much it has; trust that over our own count.
            try:
                offset = int(r.json().get("received", end + 1))
            except ValueError:
                offset = end + 1
            pct   = offset * 100 // total
            speed = offset / max(time.time() - started, 0.001)
            line  = (f"  uploading {pct:3d}%  {format_size(offset)} / {format_size(total)}"
                     f"  {format_size(speed)}/s")
            if sys.stdout.isatty():
                print("\r" + line + "   ", end="", flush=True)
            elif pct // 20 != shown or offset >= total:
                # piped (e.g. release-updater.py reads line by line): one line per 20%
                shown = pct // 20
                print(line, flush=True)
        if sys.stdout.isatty():
            print()
    finally:
        stream.close()

    return api(s, "POST", f"/api/v1/uploads/{upload_id}/finalize")


def zip_folder(path):
    """Zip a folder of page images in name order (stored, no recompression)."""
    images = sorted(f.name for f in os.scandir(path)
                    if f.is_file() and os.path.splitext(f.name)[1].lower() in IMAGE_EXTS)
    if not images:
        raise ComixError(f"No images in {path}")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for name in images:
            z.write(os.path.join(path, name), name)
    return buf.getvalue(), len(images)


def parse_chapter_name(name):
    """
    "V01 Ch001 Some Title" -> (1, 1.0, "Some Title"). Also "Vol.1 Ch.5", "Ch003.5 Half".
    Raises ValueError when there's no chapter number.
    """
    stem     = os.path.splitext(name)[0] if os.path.splitext(name)[1].lower() in ARCHIVE_EXTS else name
    vol_m    = re.search(r"[Vv](?:ol\.?\s*)?(\d+)", stem)
    ch_m     = re.search(r"[Cc]h(?:apter)?\.?\s*(\d+(?:\.\d+)?)", stem)
    if not ch_m:
        raise ValueError(f"No chapter number found in: {name!r}")
    title = stem[ch_m.end():].strip(" _-–") or None
    return (int(vol_m.group(1)) if vol_m else None), float(ch_m.group(1)), title


def collect_folder(path, start=None, end=None, exclude=(), only=None):
    """Chapter sub-folders and/or archives in path, sorted by chapter number."""
    if not os.path.isdir(path):
        raise ComixError(f"Folder not found: {path}")
    out, unparsed = [], []
    for e in os.scandir(path):
        if not (e.is_dir() or os.path.splitext(e.name)[1].lower() in ARCHIVE_EXTS):
            continue
        try:
            vol, ch, title = parse_chapter_name(e.name)
        except ValueError:
            unparsed.append(e.name)
            continue
        if (start is not None and ch < start) or (end is not None and ch > end) or ch in exclude:
            continue
        if only is not None and ch not in only:
            continue
        out.append({"path": e.path, "is_dir": e.is_dir(), "volume": vol,
                    "chapter": ch, "title": title})
    if unparsed:
        print(dim(f"  ignored (no chapter number): {', '.join(unparsed)}"))
    return sorted(out, key=lambda c: c["chapter"])


# ── Config ─────────────────────────────────────────────────────────────────────

def load_config(path):
    cfg = configparser.ConfigParser()
    if os.path.isfile(path):
        cfg.read(path, encoding="utf-8")
    return dict(cfg["comix"]) if cfg.has_section("comix") else {}


def load_library(path):
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def resolve_hid(args, library):
    """Title hash id from --manga (hid or URL) or --series (series.json)."""
    if args.manga:
        m = re.search(r"/title/([a-z0-9]+)", args.manga)
        return m.group(1) if m else args.manga.strip()
    if args.series:
        entry = library.get(args.series)
        if entry is None:
            lower = {k.lower(): v for k, v in library.items()}
            entry = lower.get(args.series.lower())
        if entry is None:
            raise ComixError(f"{args.series!r} isn't in series.json. "
                             f"Known: {', '.join(library) or '(none)'}")
        if isinstance(entry, str):
            return entry
        args.folder = args.folder or entry.get("folder")
        return entry["hid"]
    raise ComixError("Give --manga <hash id or title URL> or --series <name>.")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="Upload chapters to comix.to")
    p.add_argument("--config",  default=os.path.join(SCRIPT_DIR, "config.ini"))
    p.add_argument("--library", default=os.path.join(SCRIPT_DIR, "series.json"),
                   help="series.json: {\"Series name\": {\"hid\": \"abc12\", \"folder\": \"...\"}}")
    p.add_argument("--whoami",       action="store_true", help="check the login and exit")
    p.add_argument("--group-titles", action="store_true", help="list your group's titles with hash ids")
    p.add_argument("--list-uploads", action="store_true", help="list your uploads and their review status")
    p.add_argument("--delete",       type=int, metavar="UPLOAD_ID", help="delete one of your uploads")
    p.add_argument("--series",  help="series name from series.json")
    p.add_argument("--manga",   help="title hash id (abc12) or title URL")
    p.add_argument("--zip",     help="one chapter archive (.zip/.cbz/.cbr/.rar/.7z)")
    p.add_argument("--chapter", type=float, help="chapter number for --zip")
    p.add_argument("--volume",  type=int,   help="volume number for --zip")
    p.add_argument("--title",   help="chapter title for --zip")
    p.add_argument("--folder",  help="folder of chapter sub-folders/archives named like 'V01 Ch001 Title'")
    p.add_argument("--start",   type=float, help="--folder: first chapter")
    p.add_argument("--end",     type=float, help="--folder: last chapter")
    p.add_argument("--exclude", type=float, nargs="+", default=[], help="--folder: chapters to skip")
    p.add_argument("--chapters", type=float, nargs="+", default=None,
                   help="--folder: upload only these chapter numbers")
    p.add_argument("--group",   type=int, help="group id to credit (default: group_id in config.ini, 0 = none)")
    p.add_argument("--official", action="store_true", help="mark as an official release")
    p.add_argument("--reupload", action="store_true",
                   help="replace the file of a chapter you already uploaded instead of skipping it")
    p.add_argument("--dry-run", action="store_true", help="show what would be uploaded, upload nothing")
    args = p.parse_args()

    cfg      = load_config(args.config)
    library  = load_library(args.library)
    group_id = args.group if args.group is not None else int(cfg.get("group_id") or 0)

    try:
        if args.group_titles:
            if not group_id:
                raise ComixError("Set group_id in config.ini or pass --group.")
            s = make_session(cfg, need_login=False)
            for t in group_titles(s, group_id):
                latest = t.get("latestChapter")
                print(f"  {t['hid']:<8} id {t['id']:<7} latest {latest if latest is not None else '-':<7} {t['title']}")
            return

        s    = make_session(cfg)
        user = whoami(s)
        print(dim(f"Logged in as {user['username']}"))
        want = [v for v in (cfg.get("username"), cfg.get("user_id")) if v]
        if want and not any(v in (user["username"], user["hashId"]) for v in want):
            raise ComixError(f"Logged in as {user['username']} ({user['hashId']}), but config.ini "
                             f"is set up for {' / '.join(want)}. Log in to the right account in Firefox.")
        if args.whoami:
            can = user.get("userAbilities", {}).get("createChapter")
            print(green("OK") + f"  {user['username']} ({user['hashId']}), can upload chapters: {can}")
            if group_id:
                group = api(s, "GET", f"/api/v1/groups/{group_id}")
                print(f"  group {group_id}: {group['name']}"
                      + (yellow(f"  (config.ini says {cfg['group_name']!r})")
                         if cfg.get("group_name") and cfg["group_name"] != group["name"] else ""))
            print(f"  {len(library)} series in {os.path.basename(args.library)}")
            return

        if args.list_uploads:
            for u in my_uploads(s):
                vol = f"Vol.{u['volume']} " if u.get("volume") is not None else ""
                grp = (u.get("group") or {}).get("name", "-")
                print(f"  {u['id']:<10} {u['status']:<9} {u['manga']['title']}  "
                      f"{vol}Ch.{num_str(u['number'])} {u.get('name') or ''}  [{grp}]"
                      + (red(f"  errors: {u['errors']}") if u.get("errors") else ""))
            return

        if args.delete:
            mine = {u["id"]: u for u in my_uploads(s)}
            u = mine.get(args.delete)
            if not u:
                raise ComixError(f"Upload {args.delete} isn't in your uploads.")
            api(s, "DELETE", f"/api/v1/user/uploads/{args.delete}")
            print(green("Deleted") + f" {u['manga']['title']} Ch.{num_str(u['number'])} ({args.delete})")
            return

        hid   = resolve_hid(args, library)
        info  = title_info(s, hid)
        latest = info.get("latestChapter")
        print(bold(f"{info['title']}") + dim(f"  ({hid}, id {info['id']}, latest Ch.{latest})"))

        if args.zip:
            if args.chapter is None:
                vol, ch, title = parse_chapter_name(os.path.basename(args.zip))
                args.chapter = ch
                args.volume  = args.volume if args.volume is not None else vol
                args.title   = args.title or title
            if not os.path.isfile(args.zip):
                raise ComixError(f"File not found: {args.zip}")
            jobs = [{"path": args.zip, "is_dir": False, "volume": args.volume,
                     "chapter": args.chapter, "title": args.title}]
        elif args.folder:
            jobs = collect_folder(args.folder, args.start, args.end, set(args.exclude),
                                  set(args.chapters) if args.chapters else None)
        else:
            raise ComixError("Give --zip <archive> or --folder <chapters folder>.")
        if not jobs:
            print(yellow("Nothing to upload."))
            return

        existing = {}
        for u in my_uploads(s):
            if u["mangaId"] == info["id"] and u["status"] != "rejected":
                existing[float(u["number"])] = u

        done = failed = skipped = 0
        for job in jobs:
            ch    = job["chapter"]
            label = (f"Vol.{job['volume']} " if job["volume"] is not None else "") + f"Ch.{num_str(ch)}"
            if job["title"]:
                label += f" {job['title']}"
            prev = existing.get(ch)
            if prev and prev["status"] == "initialized" and not args.dry_run:
                # an upload that was cut off before finalize: remove it and start over
                print(yellow(f"  {label}: removing unfinished upload {prev['id']}"))
                api(s, "DELETE", f"/api/v1/user/uploads/{prev['id']}")
                prev = None
            if prev and not args.reupload:
                print(dim(f"  skip {label}: already uploaded ({prev['status']}, id {prev['id']}); "
                          f"--reupload replaces it"))
                skipped += 1
                continue
            if not prev and latest is not None and ch <= float(latest):
                print(yellow(f"  note: comix.to already lists up to Ch.{latest}; "
                             f"Ch.{num_str(ch)} may already be on the site from an earlier import"))
            action = f"replace upload {prev['id']}" if prev else "upload"
            if args.dry_run:
                print(f"  [dry-run] would {action}: {label}  <- {job['path']}")
                continue

            print(cyan(f"  {action}: {label}"))
            try:
                if job["is_dir"]:
                    data, pages = zip_folder(job["path"])
                    print(dim(f"  zipped {pages} pages, {format_size(len(data))}"))
                else:
                    data = job["path"]
                res = upload_archive(s, info["id"], group_id, ch, job["volume"], job["title"],
                                     args.official, data, replace_id=prev["id"] if prev else None)
            except ComixError as e:
                print(red(f"  FAILED {label}: {e}"))
                failed += 1
                continue
            cid = res.get("chapter_id")
            print(green(f"  done: {label}") + dim(f"  (upload {cid}, {res.get('status')})"))
            print(f"FINAL_URL: {SITE}{info['url']}/{cid}-chapter-{num_str(ch)}")
            done += 1

        if not args.dry_run:
            print(bold(f"\n{done} uploaded, {skipped} skipped, {failed} failed.")
                  + dim("  New uploads wait for comix.to review."))
        sys.exit(1 if failed else 0)

    except ComixError as e:
        print(red(f"Error: {e}"))
        sys.exit(1)


if __name__ == "__main__":
    main()
