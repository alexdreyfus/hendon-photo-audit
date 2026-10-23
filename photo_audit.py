#!/usr/bin/env python3
"""
Hendon Mob x PokerNews photo audit.

For the top N players on The Hendon Mob all-time money list, finds each player's
PokerNews profile and checks whether the Hendon Mob profile photo is the same
image (or a crop/resize of an image) shown on PokerNews.

This is image-provenance matching, not face recognition: it flags reused
photographs, not "photos of the same person".

Usage:
  pip install -r requirements.txt
  python photo_audit.py --top 1000
  python photo_audit.py --players players.csv      # use a DB export instead of scraping
  python photo_audit.py --top 20 --verbose         # quick test run

players.csv columns (from your own DB, preferred): hendon_id,name,image_url[,pokernews_slug]
"""

import argparse
import base64
import csv
import hashlib
import html
import json
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, asdict
from io import BytesIO
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from PIL import Image
import imagehash

try:
    import cv2
    import numpy as np
    HAVE_CV = True
except ImportError:
    HAVE_CV = False

# ---------------------------------------------------------------- config

HENDON_BASE = "https://pokerdb.thehendonmob.com"
# Ranking page template. {page} is 1-based. Adjust if the live pagination differs.
HENDON_RANKING = HENDON_BASE + "/ranking/all-time-money-list/?page={page}"
HENDON_PLAYER = HENDON_BASE + "/player.php?a=r&n={id}"

PN_BASE = "https://www.pokernews.com"
PN_PROFILE = PN_BASE + "/poker-players/{slug}/"
PN_GALLERY = PN_BASE + "/poker-players/{slug}/gallery/"

USER_AGENT = "HendonMob-PhotoAudit/1.0 (internal provenance check)"
DELAY = 1.5            # seconds between requests per host, be polite
CDN_DELAY = 0.3        # image CDNs (pnimg.net) handle more load
MAX_PN_IMAGES = 15     # images checked per PokerNews profile
CACHE_DIR = ".cache"

# Match thresholds (Hamming distance on 64-bit hashes; lower = more similar)
PHASH_STRONG = 8
PHASH_WEAK = 14
ORB_MIN_INLIERS = 25   # geometric inliers for a crop/resize match

# Images that are site furniture, never player photos
PN_IGNORE = re.compile(r"(favicon|/svg/|/flags/|/logos/|fog-logo|lr-sponsors|\.svg)", re.I)
HENDON_PLACEHOLDER = re.compile(r"(no[-_]?photo|placeholder|default|silhouette|blank)", re.I)


# ---------------------------------------------------------------- http

class Http:
    def __init__(self, verbose=False):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = USER_AGENT
        self.last = {}
        self.verbose = verbose
        os.makedirs(CACHE_DIR, exist_ok=True)

    def _wait(self, url):
        host = urlparse(url).netloc
        wait = CDN_DELAY if host.endswith("pnimg.net") else DELAY
        dt = time.time() - self.last.get(host, 0)
        if dt < wait:
            time.sleep(wait - dt)
        self.last[host] = time.time()

    def get(self, url, binary=False):
        key = hashlib.sha1(url.encode()).hexdigest()
        path = os.path.join(CACHE_DIR, key + (".bin" if binary else ".html"))
        if os.path.exists(path):
            with open(path, "rb") as f:
                data = f.read()
            if data == b"__404__":
                return None
            return data if binary else data.decode("utf-8", "replace")
        self._wait(url)
        for attempt in range(3):
            try:
                r = self.s.get(url, timeout=30)
                if r.status_code == 404:
                    open(path, "wb").write(b"__404__")
                    return None
                if r.status_code == 429 or r.status_code >= 500:
                    time.sleep(10 * (attempt + 1))
                    continue
                r.raise_for_status()
                open(path, "wb").write(r.content)
                if self.verbose:
                    print(f"  GET {url} [{r.status_code}]", file=sys.stderr)
                return r.content if binary else r.text
            except requests.RequestException as e:
                if self.verbose:
                    print(f"  ! {url}: {e}", file=sys.stderr)
                time.sleep(5)
        return None


# ---------------------------------------------------------------- helpers

def slugify(name):
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    n = re.sub(r"[^A-Za-z0-9]+", "-", n).strip("-").lower()
    return n


def slug_candidates(name):
    """PokerNews slugs are usually first-last. Try a few variants."""
    base = slugify(name)
    out = [base]
    parts = base.split("-")
    if len(parts) > 2:
        out.append(f"{parts[0]}-{parts[-1]}")
    # Hendon sometimes stores "Last, First"
    if "," in name:
        last, first = [p.strip() for p in name.split(",", 1)]
        out.append(slugify(f"{first} {last}"))
    seen, uniq = set(), []
    for s in out:
        if s and s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq


def unwrap_pnimg(url):
    """
    s.pnimg.net/<sig>/pr:<preset>/<base64 chunks>.webp  ->  original pnimg.net URL.
    The original URL is URL-safe base64 split into path segments.
    """
    p = urlparse(url)
    if p.netloc != "s.pnimg.net":
        return url
    segs = p.path.strip("/").split("/")
    try:
        i = next(k for k, s in enumerate(segs) if s.startswith("pr:"))
    except StopIteration:
        return url
    b64 = "".join(segs[i + 1:])
    b64 = re.sub(r"\.(webp|jpe?g|png|avif)$", "", b64)
    b64 += "=" * (-len(b64) % 4)
    try:
        dec = base64.urlsafe_b64decode(b64).decode()
        if dec.startswith("http"):
            return dec
    except Exception:
        pass
    return url


def load_image(data):
    try:
        im = Image.open(BytesIO(data))
        im.load()
        return im.convert("RGB")
    except Exception:
        return None


# ---------------------------------------------------------------- hendon mob

def hendon_top_players(http, top):
    players, page = [], 1
    while len(players) < top and page <= 200:
        html_ = http.get(HENDON_RANKING.format(page=page))
        if not html_:
            break
        soup = BeautifulSoup(html_, "html.parser")
        found = 0
        for a in soup.select('a[href*="player.php"]'):
            m = re.search(r"n=(\d+)", a["href"])
            name = a.get_text(" ", strip=True)
            if not m or not name:
                continue
            pid = m.group(1)
            if any(p["hendon_id"] == pid for p in players):
                continue
            players.append({"hendon_id": pid, "name": name, "image_url": ""})
            found += 1
            if len(players) >= top:
                break
        if found == 0:
            break
        page += 1
    return players


def hendon_image(http, pid):
    html_ = http.get(HENDON_PLAYER.format(id=pid))
    if not html_:
        return ""
    soup = BeautifulSoup(html_, "html.parser")
    # Full-size photo link on the profile header. (og:image on Hendon Mob uses
    # '+' for spaces and 404s, so it is only a last resort.)
    a = soup.select_one("a.player-profile-photo")
    if a and a.get("href") and "/pictures/" in a["href"]:
        return urljoin(HENDON_BASE, a["href"])
    box = soup.select_one(".player-profile-layout__photo")
    if box:
        m = re.search(r"url\(['\"]?([^)'\"]+)", box.get("style", ""))
        if m:
            return urljoin(HENDON_BASE, m.group(1))
    og = soup.find("meta", property="og:image")
    if og and og.get("content") and not HENDON_PLACEHOLDER.search(og["content"]):
        return urljoin(HENDON_BASE, og["content"].replace("+", "%20"))
    return ""


# ---------------------------------------------------------------- pokernews

def name_from_picture(url):
    """Hendon photo files are named '<Player Name> Profile Picture.jpg'."""
    from urllib.parse import unquote
    fn = unquote(urlparse(url).path.rsplit("/", 1)[-1])
    m = re.match(r"(.+?)\s+Profile\s+Picture", fn, re.I)
    return m.group(1) if m else ""


def pokernews_profile(http, name, slug_hint=None, alt_name=""):
    slugs = [slug_hint] if slug_hint else []
    slugs += slug_candidates(name)
    if alt_name:
        slugs += [s for s in slug_candidates(alt_name) if s not in slugs]
    for slug in slugs:
        html_ = http.get(PN_PROFILE.format(slug=slug))
        if html_ and "poker-players" in html_:
            return slug, html_
    return None, None


def pokernews_images(http, slug, profile_html, gallery_pages=2):
    urls = []
    soup = BeautifulSoup(profile_html, "html.parser")
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        urls.append(og["content"])
    # Header image and photos block on the profile
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if "pnimg.net" in src and not PN_IGNORE.search(src):
            urls.append(src)
    # Gallery
    for page in range(1, gallery_pages + 1):
        g = PN_GALLERY.format(slug=slug) + (f"?page={page}" if page > 1 else "")
        gh = http.get(g)
        if not gh:
            break
        gs = BeautifulSoup(gh, "html.parser")
        before = len(urls)
        for img in gs.find_all("img"):
            src = img.get("src") or img.get("data-src") or ""
            if "pnimg.net" in src and not PN_IGNORE.search(src):
                urls.append(src)
        if len(urls) == before:
            break
    # Unwrap resized variants to originals, dedupe
    out, seen = [], set()
    for u in urls:
        o = unwrap_pnimg(u)
        if o not in seen and not PN_IGNORE.search(o):
            seen.add(o)
            out.append(o)
    return out[:MAX_PN_IMAGES]


# ---------------------------------------------------------------- matching

@dataclass
class Match:
    pn_url: str
    phash_dist: int
    dhash_dist: int
    orb_inliers: int
    verdict: str


def hashes(im):
    return imagehash.phash(im), imagehash.dhash(im)


def orb_inliers(a, b):
    """Feature match that survives crops, resizes and recompression."""
    if not HAVE_CV:
        return 0
    def prep(im):
        g = cv2.cvtColor(np.array(im), cv2.COLOR_RGB2GRAY)
        h, w = g.shape
        s = 800 / max(h, w)
        if s < 1:
            g = cv2.resize(g, (int(w * s), int(h * s)))
        return g
    ga, gb = prep(a), prep(b)
    orb = cv2.ORB_create(2000)
    ka, da = orb.detectAndCompute(ga, None)
    kb, db = orb.detectAndCompute(gb, None)
    if da is None or db is None or len(ka) < 10 or len(kb) < 10:
        return 0
    bf = cv2.BFMatcher(cv2.NORM_HAMMING)
    pairs = bf.knnMatch(da, db, k=2)
    good = [m for m, n in (p for p in pairs if len(p) == 2) if m.distance < 0.75 * n.distance]
    if len(good) < 8:
        return 0
    src = np.float32([ka[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
    dst = np.float32([kb[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
    # Similarity transform: rotation, uniform scale, translation (covers crop+resize)
    M, mask = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC, ransacReprojThreshold=5.0)
    return int(mask.sum()) if mask is not None else 0


def compare(hm_img, pn_img):
    hp, hd = hashes(hm_img)
    pp, pd = hashes(pn_img)
    pdist, ddist = int(hp - pp), int(hd - pd)
    inl = 0
    if pdist <= PHASH_STRONG and ddist <= PHASH_STRONG:
        verdict = "EXACT"
    else:
        inl = orb_inliers(hm_img, pn_img)
        if inl >= ORB_MIN_INLIERS:
            verdict = "CROP/EDIT"
        elif pdist <= PHASH_WEAK:
            verdict = "POSSIBLE"
        else:
            verdict = "NO"
    return pdist, ddist, inl, verdict


# ---------------------------------------------------------------- main loop

@dataclass
class Row:
    rank: int
    hendon_id: str
    name: str
    hendon_image: str = ""
    pokernews_slug: str = ""
    pokernews_images_checked: int = 0
    verdict: str = ""
    best_pn_image: str = ""
    phash_dist: int = -1
    orb_inliers: int = 0
    note: str = ""


RANK = {"EXACT": 3, "CROP/EDIT": 2, "POSSIBLE": 1, "NO": 0}


def audit(players, http, gallery_pages, out_prefix):
    rows = []
    done_path = out_prefix + "_progress.jsonl"
    done = {}
    if os.path.exists(done_path):
        for line in open(done_path):
            r = json.loads(line)
            done[r["hendon_id"]] = r
    prog = open(done_path, "a")

    for i, p in enumerate(players, 1):
        if p["hendon_id"] in done:
            rows.append(Row(**done[p["hendon_id"]]))
            continue
        row = Row(rank=int(p.get("rank") or i), hendon_id=p["hendon_id"], name=p["name"])
        print(f"[{i}/{len(players)}] {p['name']}", file=sys.stderr)

        img_url = (p.get("image_url") or "").strip() or hendon_image(http, p["hendon_id"])
        row.hendon_image = img_url
        if not img_url:
            row.verdict, row.note = "NO_HENDON_PHOTO", "no photo on Hendon profile"
        else:
            data = http.get(img_url, binary=True)
            hm = load_image(data) if data else None
            if hm is None:
                row.verdict, row.note = "ERROR", "could not load Hendon image"
            else:
                slug, phtml = pokernews_profile(http, p["name"], p.get("pokernews_slug") or None,
                                                name_from_picture(img_url))
                if not slug:
                    row.verdict, row.note = "NO_PN_PROFILE", "no PokerNews profile found"
                else:
                    row.pokernews_slug = slug
                    best = None
                    urls = pokernews_images(http, slug, phtml, gallery_pages)
                    for u in urls:
                        d = http.get(u, binary=True)
                        pn = load_image(d) if d else None
                        if pn is None:
                            continue
                        row.pokernews_images_checked += 1
                        pd_, dd, inl, v = compare(hm, pn)
                        cand = Match(u, pd_, dd, inl, v)
                        if best is None or (RANK[v], inl, -pd_) > (RANK[best.verdict], best.orb_inliers, -best.phash_dist):
                            best = cand
                        if v == "EXACT":
                            break
                    if best:
                        row.verdict = best.verdict
                        row.best_pn_image = best.pn_url
                        row.phash_dist = best.phash_dist
                        row.orb_inliers = best.orb_inliers
                    else:
                        row.verdict, row.note = "NO", "no PokerNews images loaded"

        rows.append(row)
        prog.write(json.dumps(asdict(row)) + "\n")
        prog.flush()
    prog.close()
    return rows


def write_reports(rows, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    rows = sorted(rows, key=lambda r: r.rank)
    csv_path = os.path.join(out_dir, "results.csv")
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(asdict(rows[0]).keys()))
        w.writeheader()
        for r in rows:
            w.writerow(asdict(r))
    counts = {}
    for r in rows:
        counts[r.verdict] = counts.get(r.verdict, 0) + 1
    data = json.dumps([asdict(r) for r in rows]).replace("</", "<\\/")
    stamp = time.strftime("%d %B %Y, %H:%M UTC", time.gmtime())
    tpl = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "report_template.html")).read()
    page = (tpl.replace("__DATA__", data)
               .replace("__STAMP__", stamp)
               .replace("__HENDON_PLAYER__", HENDON_PLAYER.replace("{id}", ""))
               .replace("__PN_PROFILE__", PN_PROFILE.replace("{slug}/", "")))
    html_path = os.path.join(out_dir, "index.html")
    with open(html_path, "w") as f:
        f.write(page)
    return csv_path, html_path, counts


def load_progress(paths):
    rows, seen = [], set()
    for p in paths:
        for line in open(p):
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if r["hendon_id"] in seen:
                continue
            seen.add(r["hendon_id"])
            rows.append(Row(**r))
    return rows


def main():
    import glob
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--top", type=int, default=1000)
    ap.add_argument("--players", help="CSV: hendon_id,name,image_url[,pokernews_slug]")
    ap.add_argument("--gallery-pages", type=int, default=1)
    ap.add_argument("--out", default="photo_audit", help="prefix for progress file")
    ap.add_argument("--site", default="site", help="output folder for index.html and results.csv")
    ap.add_argument("--shard", help="k/n: process every n-th player starting at k (0-based)")
    ap.add_argument("--list-only", metavar="CSV", help="fetch the top list, write it to CSV, exit")
    ap.add_argument("--report-from", metavar="GLOB", help="build the site from progress .jsonl files, no fetching")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    if a.report_from:
        files = sorted(glob.glob(a.report_from))
        rows = load_progress(files)
        if not rows:
            sys.exit(f"No results in {a.report_from}")
        _, html_path, counts = write_reports(rows, a.site)
        print(f"Report built from {len(files)} files, {len(rows)} players: {counts}\n  {html_path}")
        return

    if not HAVE_CV:
        print("Note: opencv not installed, crop detection disabled.", file=sys.stderr)

    http = Http(a.verbose)
    if a.players:
        with open(a.players, newline="") as f:
            players = list(csv.DictReader(f))[: a.top]
    else:
        players = hendon_top_players(http, a.top)
    if not players:
        sys.exit("No players loaded. Check HENDON_RANKING in the config, or use --players.")
    for n, p in enumerate(players, 1):
        p.setdefault("rank", n)
    print(f"Loaded {len(players)} players", file=sys.stderr)

    if a.list_only:
        with open(a.list_only, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["rank", "hendon_id", "name", "image_url", "pokernews_slug"],
                               extrasaction="ignore")
            w.writeheader()
            for p in players:
                w.writerow(p)
        print(f"Wrote {a.list_only}")
        return

    if a.shard:
        k, n = map(int, a.shard.split("/"))
        players = players[k::n]
        print(f"Shard {k}/{n}: {len(players)} players", file=sys.stderr)

    rows = audit(players, http, a.gallery_pages, a.out)
    _, html_path, counts = write_reports(rows, a.site)
    print(f"\nDone. {counts}\n  {html_path}")


if __name__ == "__main__":
    main()
