# Hendon Mob Photo Audit

Checks whether the profile photos of the top Hendon Mob players are images that also appear on PokerNews player profiles, and publishes the results as a web page.

## Run it

Hendon Mob pages are behind a Cloudflare bot check that blocks GitHub runners (photo files are not blocked). The player list is therefore committed as `players.csv` (rank,hendon_id,name,image_url), collected from a normal browser session. Refresh it before each audit.

Actions tab, **Photo audit**, **Run workflow**. Defaults: top 1,000 players, 4 parallel runners. A full run takes roughly 1.5 to 3 hours. The results page updates when the run finishes, and the link appears on the run summary.

## How it works

1. **list** builds the top-N list from the Hendon Mob all-time money list. If a `players.csv` is committed to the repo (columns `hendon_id,name,image_url[,pokernews_slug]`, from the Hendon Mob database), it is used instead of scraping.
2. **audit** splits the list across parallel runners. For each player it takes the Hendon Mob profile photo, finds the PokerNews profile at `/poker-players/{first-last}/`, and compares the photo with the PokerNews profile image and gallery (up to 15 images).
   * Perceptual hash: catches the same photo resized or recompressed.
   * Feature matching with geometric check: catches headshots cropped from a wider PokerNews photo.
3. **publish** merges the results and deploys `index.html` and `results.csv` to GitHub Pages.

Downloads are cached between runs, so a rerun after a failure resumes instead of starting over.

## Results

| Label | Meaning |
|---|---|
| Same photo | Identical image, possibly resized or recompressed |
| Cropped | Crop or edit of a PokerNews image |
| Possible | Weak similarity, check by eye |
| No match | PokerNews profile found, none of its images match |
| No PokerNews profile | Name did not resolve to a profile. Add `pokernews_slug` in `players.csv` |
| No Hendon photo | Player has no photo on Hendon Mob |

The page is public but marked `noindex`, so search engines will not list it.

## Tuning

Constants at the top of `photo_audit.py`: request delays, `MAX_PN_IMAGES`, match thresholds, and `HENDON_RANKING` (the ranking page URL pattern).

## Local run

```
pip install -r requirements.txt
python photo_audit.py --top 20 --verbose
```
