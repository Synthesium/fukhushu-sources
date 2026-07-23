#!/usr/bin/env python3
"""Build the fuKhushu source-catalog manifest from QUL (qul.tarteel.ai).

QUL's catalog API is public, but its download links are login-gated: the
per-file token appears only in the authenticated resource-listing HTML, and
/download 302-redirects to a *public* S3 URL that anyone can GET — until QUL
re-exports the file and the URL rotates. So this script (run occasionally,
with your QUL session cookie) scrapes the current URLs and merges them with
public API metadata into manifest.json, which the app fetches from this repo.

The app then downloads files directly from QUL's host and unzips them on
device (QUL serves every SQLite export as a ZIP wrapping one `.db`).

Usage:
    # put QUL_SESSION=<cookie value> in a .env file next to this script, then:
    python3 harvest.py -o manifest.json
    python3 harvest.py --validate manifest.json     # anonymous URL spot-check

The cookie is the value of `_quran_com-community_session` from your browser's
devtools while logged in to qul.tarteel.ai. It goes in `.env` (git-ignored),
never on the command line or in a committed file. See README.md.

Requires: requests, beautifulsoup4  (pip install requests beautifulsoup4)

Design note — IDs: the app keys downloaded sources by the QUL *Downloadable
Resource* id (the `downloadable_resource_<N>` in the listing HTML), which is
what the download links belong to. This is deliberately NOT the public API's
ResourceContent id — the two id-spaces differ, and mixing them hands out the
wrong file. Translation and tafsir DownloadableResource ids do not overlap, so
`1000 + id` stays collision-free in the app.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from datetime import datetime, timezone

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    sys.exit("pip install requests beautifulsoup4")

BASE = "https://qul.tarteel.ai"
SESSION_COOKIE_NAME = "_quran_com-community_session"
SCHEMA_VERSION = 1

# QUL exports already shipped in the app's bundled DB → never offered as
# downloads (redundant, and the Japanese ones would lose the app's bundled
# furigana). Keyed by DownloadableResource (listing) id — the precise identity
# of THIS export. NOT by slug/name: QUL has several same-named "Tafsir Ibn
# Kathir" entries (35 English = ours, but 22 Arabic / 30 Urdu / 31 Bengali are
# distinct sources we DO want in the catalog). The two non-QUL bundled sources
# (clear-quran, yasashii) aren't downloadable anyway.
BUNDLED_QUL_IDS = {
    35:  "en-tafisr-ibn-kathir — Tafsir Ibn Kathir (bundled id 4)",
    266: "en-mukhtasar — Abridged Explanation (bundled id 5)",
    265: "ja-mukhtasar — Japanese Mokhtasar tafsir (bundled id 2)",
    202: "ja-saeed — Saeed Sato translation (bundled id 1)",
    189: "ja-ryoichi-mita — Ryoichi Mita translation (bundled id 3)",
}


# ── Listing (authenticated HTML) ──────────────────────────────────────────

def parse_listing(session: requests.Session, kind: str) -> list[dict]:
    """kind: 'translation' | 'tafsir'. One dict per resource with its
    DownloadableResource id, name, language tag(s), and download variants."""
    r = session.get(f"{BASE}/resources/{kind}", timeout=90)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for li in soup.select('li[id^="downloadable_resource_"]'):
        try:
            rid = int(li["id"].rsplit("_", 1)[1])
        except (KeyError, ValueError):
            continue
        h3 = li.select_one("h3")
        name = h3.get_text(" ", strip=True) if h3 else None
        langs = []
        for a in li.select('a[data-url^="/docs/"]'):
            slug = a.get("data-url", "").split("/docs/")[-1]
            if slug in ("translation", "tafsir"):
                continue  # the type tag, not a language
            label = a.get("title", "").replace("About ", "").strip()
            langs.append(label or a.get_text(" ", strip=True))
        variants = {}
        for a in li.find_all("a", href=True):
            if "/download" in a["href"]:
                variants[a.get_text(" ", strip=True)] = a["href"]
        if name and variants:
            out.append(
                {"id": rid, "name": name, "langs": langs, "variants": variants}
            )
    return out


def _find(variants: dict, pred):
    for label, href in variants.items():
        if pred(label.lower()):
            return href
    return None


def classify_translation(variants: dict) -> tuple[str | None, bool, str, str]:
    """Decide which translation variant (if any) the app can consume.

    Returns (href, has_footnotes, status, reason) where status is 'ok' or
    'incompatible'. The app renders ayah-by-ayah text, so it needs the
    *with-footnote-tags* export (footnotes column + `<sup foot_note>` markers)
    or the plain `simple.sqlite`. A resource that only ships the generic
    `sqlite` is word-by-word (a `word_translation` word-level table the app
    can't render); JSON/DOCX-only resources have no SQLite at all."""
    ft = _find(variants, lambda l: "sqlite" in l and "footnote-tags" in l)
    if ft:
        return ft, True, "ok", "translation with footnotes"
    simple = _find(variants, lambda l: "simple.sqlite" in l)
    if simple:
        return simple, False, "ok", "plain translation"
    if _find(variants, lambda l: "sqlite" in l):
        return None, False, "incompatible", \
            "word-by-word data (word-level rows, not ayah-by-ayah)"
    return None, False, "incompatible", "no SQLite export (JSON/other only)"


def classify_tafsir(variants: dict) -> tuple[str | None, bool, str, str]:
    """Decide which tafsir variant the app can consume. Prefers the grouped
    `sqlite` export (passage ranges preserved), else `simple.sqlite` (flat,
    one entry per ayah — the app handles both). No SQLite → incompatible."""
    generic = simple = None
    for label, href in variants.items():
        ll = label.lower()
        if "sqlite" not in ll or "chunk" in ll:
            continue
        if "simple" in ll:
            simple = simple or href
        else:
            generic = generic or href
    href = generic or simple
    if href:
        return href, False, "ok", "tafsir"
    return None, False, "incompatible", "no SQLite export (JSON/other only)"


def resolve_and_size(
    session: requests.Session, href: str
) -> tuple[str | None, int | None]:
    """Follow the login-gated /download redirect (without the body) to the
    public S3 URL, then HEAD it for the size."""
    url = href if href.startswith("http") else BASE + href
    r = session.get(url, allow_redirects=False, timeout=30)
    if r.status_code not in (301, 302, 303, 307, 308):
        return None, None
    location = r.headers.get("Location")
    if not location or "/users/sign_in" in location:
        return None, None
    size = None
    try:
        head = requests.head(location, timeout=30)
        if head.ok:
            size = int(head.headers.get("Content-Length") or 0) or None
    except requests.RequestException:
        pass
    return location, size


# ── Public API enrichment ─────────────────────────────────────────────────

def build_language_index(session: requests.Session) -> dict:
    langs = session.get(
        f"{BASE}/api/v1/resources/languages", timeout=30
    ).json()["languages"]
    return {l["name"].strip().lower(): l for l in langs if l.get("name")}


def build_api_index(session: requests.Session, kind: str) -> dict:
    key = "translations" if kind == "translation" else "tafsirs"
    data = session.get(f"{BASE}/api/v1/resources/{key}", timeout=30).json()[key]
    index = {}
    for r in data:
        if r.get("name"):
            index.setdefault(r["name"].strip().lower(), r)
    return index


def language_entry(lang_index: dict, names: list, api_fallback) -> dict:
    for nm in list(names) + ([api_fallback] if api_fallback else []):
        if not nm:
            continue
        lang = lang_index.get(nm.strip().lower())
        if lang:
            return {
                "iso": lang.get("iso_code") or nm[:2].lower(),
                "name": lang["name"],
                "native": lang.get("native_name") or lang["name"],
                "direction": "rtl" if lang.get("direction") == "rtl" else "ltr",
            }
    nm = (names[0] if names else api_fallback) or "Unknown"
    return {"iso": nm[:2].lower(), "name": nm.capitalize(),
            "native": nm, "direction": "ltr"}


def iso_from_epoch(epoch) -> str | None:
    if not epoch:
        return None
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat()


# ── Harvest ───────────────────────────────────────────────────────────────

def harvest(session: requests.Session, delay: float, limit, deep: bool) -> tuple[dict, list]:
    """Returns (manifest, incompatible). Only compatible resources go in the
    manifest; incompatible ones are collected for the compatibility report."""
    lang_index = build_language_index(session)
    api_index = {
        "translation": build_api_index(session, "translation"),
        "tafsir": build_api_index(session, "tafsir"),
    }

    resources = []
    incompatible = []
    bundled = []
    for kind in ("translation", "tafsir"):
        listing = parse_listing(session, kind)
        if limit:
            listing = listing[:limit]
        print(f"{kind}: {len(listing)} resources in listing")
        for i, item in enumerate(listing, 1):
            name = item["name"]
            print(f"[{kind} {i}/{len(listing)}] {name[:42]} ... ", end="", flush=True)

            api = api_index[kind].get(name.strip().lower(), {})
            # Already in the app bundle → don't offer as a download.
            if item["id"] in BUNDLED_QUL_IDS:
                bundled.append({"type": kind, "qulId": item["id"],
                                "name": name, "slug": api.get("slug")})
                print("BUNDLED (already shipped by default)")
                continue

            classify = classify_translation if kind == "translation" else classify_tafsir
            href, has_footnotes, status, reason = classify(item["variants"])
            api_lang = api.get("language") or api.get("language_name")
            language = language_entry(lang_index, item["langs"], api_lang)

            if status != "ok":
                incompatible.append({
                    "type": kind, "qulId": item["id"], "name": name,
                    "language": language["name"], "reason": reason,
                    "offers": sorted(item["variants"].keys()),
                })
                print(f"INCOMPATIBLE ({reason})")
                continue

            location, size = resolve_and_size(session, href)
            if not location:
                incompatible.append({
                    "type": kind, "qulId": item["id"], "name": name,
                    "language": language["name"],
                    "reason": "download redirect failed (session expired?)",
                    "offers": sorted(item["variants"].keys()),
                })
                print("SKIP (redirect failed)")
                continue

            deep_note = ""
            if deep:
                ok, detail = deep_check(session, kind, location)
                if not ok:
                    incompatible.append({
                        "type": kind, "qulId": item["id"], "name": name,
                        "language": language["name"],
                        "reason": f"deep check: {detail}",
                        "offers": sorted(item["variants"].keys()),
                    })
                    print(f"INCOMPATIBLE (deep: {detail})")
                    continue
                deep_note = f", deep✓ {detail}"

            resources.append({
                "qulId": item["id"],
                "type": kind,
                "name": name,
                "translatedName": (api.get("translated_name") or {}).get("name"),
                "author": api.get("author_name"),
                "slug": api.get("slug"),
                "language": language,
                "recordsCount": api.get("records_count"),
                "qulUpdatedAt": iso_from_epoch(api.get("updated_at")),
                "hasFootnotes": has_footnotes,
                "fileFormat": "sqlite",
                "fileUrl": location,
                "fileSizeBytes": size,
            })
            print(f"ok ({size or '?'}B{', footnotes' if has_footnotes else ''}{deep_note})")
            time.sleep(delay)  # be polite to QUL

    resources.sort(key=lambda r: (r["language"]["iso"], r["type"], r["name"]))
    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": datetime.now(tz=timezone.utc).isoformat(),
        "resources": resources,
    }
    return manifest, incompatible, bundled


def deep_check(session: requests.Session, kind: str, url: str) -> tuple[bool, str]:
    """Download + unzip the export and confirm its SQLite schema is one the app
    actually converts (the same table/column checks as qul_converter.dart).
    Optional (`--deep`) because it downloads every file."""
    import io
    import sqlite3
    import tempfile
    import zipfile
    try:
        blob = requests.get(url, timeout=120).content
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            inner = next(n for n in z.namelist() if n.endswith((".db", ".sqlite")))
            data = z.read(inner)
    except (requests.RequestException, zipfile.BadZipFile, StopIteration) as e:
        return False, f"unreadable export ({type(e).__name__})"
    with tempfile.NamedTemporaryFile(suffix=".db") as tf:
        tf.write(data)
        tf.flush()
        db = sqlite3.connect(tf.name)
        try:
            tables = {r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            def cols(t):
                return {r[1] for r in db.execute(f"PRAGMA table_info({t})")}
            if kind == "translation":
                t = "translation" if "translation" in tables else (
                    "translations" if "translations" in tables else None)
                if not t or not {"sura", "ayah", "text"} <= cols(t):
                    return False, f"schema {sorted(tables)} not ayah-level translation"
                n = db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                return True, f"{n} verses"
            if "tafsir" in tables and {"ayah_key", "text"} <= cols("tafsir"):
                n = db.execute("SELECT COUNT(*) FROM tafsir").fetchone()[0]
                return True, f"{n} grouped rows"
            if "translation" in tables and {"sura", "ayah", "text"} <= cols("translation"):
                n = db.execute("SELECT COUNT(*) FROM translation").fetchone()[0]
                return True, f"{n} flat rows"
            return False, f"schema {sorted(tables)} not a tafsir/translation table"
        finally:
            db.close()


def write_compat_report(
    path: str, manifest: dict, incompatible: list, bundled: list
) -> None:
    """Emit a markdown compatibility report: what's in the catalog, what's
    already shipped by default, and every resource excluded and why.
    Regenerated each run."""
    res = manifest["resources"]
    from collections import Counter
    by_reason = Counter(x["reason"] for x in incompatible)
    lines = [
        "# fuKhushu ↔ QUL compatibility report",
        "",
        f"Generated: {manifest['generatedAt']}",
        "",
        "## Included in the catalog",
        "",
        f"- **{len(res)}** downloadable resources "
        f"({sum(1 for r in res if r['type']=='translation')} translations, "
        f"{sum(1 for r in res if r['type']=='tafsir')} tafsirs)",
        f"- {sum(1 for r in res if r['hasFootnotes'])} translations with footnotes",
        f"- {sum(1 for r in res if r['language']['direction']=='rtl')} right-to-left",
        f"- {len({r['language']['name'] for r in res})} languages",
        "",
        "## Already bundled — shipped by default, not offered as downloads",
        "",
        f"**{len(bundled)}** QUL exports are already in the app's bundled DB "
        "(kept out of the catalog to avoid duplicates; the Japanese ones also "
        "keep their bundled furigana, which the QUL exports lack):" if bundled
        else "None.",
        "",
    ]
    if bundled:
        lines += ["| type | id | slug | name |", "|---|---|---|---|"]
        for x in sorted(bundled, key=lambda r: (r["type"], r["name"])):
            lines.append(f"| {x['type']} | {x['qulId']} | {x.get('slug')} | {x['name']} |")
        lines.append("")
    lines += [
        "## Excluded — incompatible",
        "",
        f"**{len(incompatible)}** resources cannot be used by the app:" if incompatible
        else "None — every other QUL resource is compatible. 🎉",
        "",
    ]
    for reason, count in by_reason.most_common():
        lines.append(f"### {reason} ({count})")
        lines.append("")
        lines.append("| type | id | language | name |")
        lines.append("|---|---|---|---|")
        for x in sorted(incompatible, key=lambda r: (r["type"], r["name"])):
            if x["reason"] == reason:
                lines.append(
                    f"| {x['type']} | {x['qulId']} | {x['language']} | {x['name']} |"
                )
        lines.append("")
    with open(path, "w") as f:
        f.write("\n".join(lines))


def validate(manifest_path: str, sample: int) -> int:
    """Anonymously GET a sample of file URLs (no session needed) to confirm
    they are still live and are real ZIP/SQLite exports. Exit 1 on any failure."""
    with open(manifest_path) as f:
        manifest = json.load(f)
    resources = manifest.get("resources", [])
    if not resources:
        print("Manifest has no resources")
        return 1
    picks = random.sample(resources, min(sample, len(resources)))
    failures = 0
    for res in picks:
        try:
            r = requests.get(res["fileUrl"], stream=True, timeout=30)
            first = next(r.iter_content(16), b"")
            ok = r.ok and (
                first.startswith(b"PK") or first.startswith(b"SQLite format 3")
            )
            r.close()
        except requests.RequestException:
            ok = False
        failures += 0 if ok else 1
        print(f"  {'OK ' if ok else 'FAIL'} {res['type']} {res['qulId']} {res['name'][:40]}")
    print(f"{len(picks) - failures}/{len(picks)} live")
    return 1 if failures else 0


# ── Recitations (audio) ───────────────────────────────────────────────────
#
# QUL recitation exports are small SQLite DBs of audio URLs + timings (the
# audio itself lives on public CDNs — audio.qurancdn.com, audio-cdn.tarteel.ai
# …). Two shapes exist:
#   ayah-by-ayah:  verses(surah_number, ayah_number, audio_url, duration,
#                         segments)                      — 6236 rows
#   gapless:       surah_list(surah_number, audio_url, duration)   — 114 rows
#                  + segments(surah_number, ayah_number, duration_sec,
#                             timestamp_from, timestamp_to, segments)
# Filenames do NOT follow surah:ayah numbering for every reciter (e.g. Sudais
# 2:255 → 002248.mp3), so URLs are never templated — the app downloads the DB
# and stores the URL map verbatim.

def inspect_recitation_db(content: bytes) -> dict:
    """Unzip + open a recitation export and report its shape. Returns
    {cardinality, rows, hasSegments, sampleAudioUrl} or {error}."""
    import io
    import sqlite3
    import tempfile
    import zipfile

    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            names = [n for n in zf.namelist()
                     if n.endswith((".db", ".sqlite")) and not n.endswith("/")]
            if not names:
                return {"error": "zip contains no .db"}
            data = zf.read(names[0])
    except zipfile.BadZipFile:
        if content[:16].startswith(b"SQLite format 3"):
            data = content  # served raw, tolerate it
        else:
            return {"error": "not a zip or sqlite file"}

    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        tmp.write(data)
        tmp.flush()
        try:
            con = sqlite3.connect(f"file:{tmp.name}?mode=ro", uri=True)
            tables = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "verses" in tables:
                rows = con.execute("SELECT COUNT(*) FROM verses").fetchone()[0]
                sample = con.execute(
                    "SELECT audio_url, segments FROM verses "
                    "WHERE audio_url IS NOT NULL LIMIT 1").fetchone()
                return {
                    "cardinality": "ayah",
                    "rows": rows,
                    "hasSegments": bool(sample and (sample[1] or "").strip("[] ")),
                    "sampleAudioUrl": sample[0] if sample else None,
                }
            if "surah_list" in tables:
                rows = con.execute(
                    "SELECT COUNT(*) FROM surah_list").fetchone()[0]
                sample = con.execute(
                    "SELECT audio_url FROM surah_list "
                    "WHERE audio_url IS NOT NULL LIMIT 1").fetchone()
                return {
                    "cardinality": "surah",
                    "rows": rows,
                    "hasSegments": "segments" in tables,
                    "sampleAudioUrl": sample[0] if sample else None,
                }
            return {"error": f"unexpected tables: {sorted(tables)}"}
        finally:
            con.close()


def harvest_recitations(session: requests.Session, delay: float, limit):
    """Returns (manifest, skipped). Every recitation whose sqlite export
    downloads, parses, and whose audio host answers an anonymous HEAD goes in
    the manifest (with its cardinality — the app decides what it supports);
    the rest land in `skipped` with a reason."""
    listing = parse_listing(session, "recitation")
    if limit:
        listing = listing[:limit]
    print(f"Recitation listing: {len(listing)} entries")

    entries, skipped = [], []
    for i, res in enumerate(listing):
        time.sleep(delay + random.uniform(0, 0.3))
        name = res["name"]
        href = _find(res["variants"], lambda l: "sqlite" in l)
        if not href:
            skipped.append({"id": res["id"], "name": name,
                            "reason": "no sqlite export"})
            continue
        url, size = resolve_and_size(session, href)
        if not url:
            skipped.append({"id": res["id"], "name": name,
                            "reason": "download did not resolve"})
            continue
        try:
            body = requests.get(url, timeout=120)
            body.raise_for_status()
        except requests.RequestException as e:
            skipped.append({"id": res["id"], "name": name,
                            "reason": f"file fetch failed: {e}"})
            continue
        info = inspect_recitation_db(body.content)
        if "error" in info:
            skipped.append({"id": res["id"], "name": name,
                            "reason": info["error"]})
            continue
        audio_ok, audio_host = False, None
        sample = info.get("sampleAudioUrl")
        if sample:
            audio_host = sample.split("/")[2] if "://" in sample else None
            try:
                audio_ok = requests.head(
                    sample, timeout=20, allow_redirects=True).ok
            except requests.RequestException:
                audio_ok = False
        if not audio_ok:
            skipped.append({"id": res["id"], "name": name,
                            "reason": f"audio not public ({sample})"})
            continue
        entries.append({
            "qulId": res["id"],
            "name": name,
            "tags": sorted(t for t in res["langs"] if t != "recitation"),
            "cardinality": info["cardinality"],
            "hasSegments": info["hasSegments"],
            "recordsCount": info["rows"],
            "fileUrl": url,
            "fileSizeBytes": size or len(body.content),
            "audioHost": audio_host,
        })
        print(f"  [{i + 1}/{len(listing)}] ok {res['id']:>4} "
              f"{info['cardinality']:>5} {name}")

    entries.sort(key=lambda e: e["name"].lower())
    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "source": "qul.tarteel.ai",
        "recitations": entries,
    }
    return manifest, skipped


def load_dotenv(path: str) -> None:
    """Populate os.environ from a KEY=VALUE .env file next to this script (no
    external dependency). An already-exported variable wins over the file."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(
                key.strip(), value.strip().strip('"').strip("'")
            )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-o", "--output", default="manifest.json")
    ap.add_argument("--report", default="compatibility-report.md",
                    help="where to write the compatibility report")
    ap.add_argument("--delay", type=float, default=0.5,
                    help="seconds between resource downloads (default 0.5)")
    ap.add_argument("--limit", type=int, default=None,
                    help="only process the first N of each type (for testing)")
    ap.add_argument("--deep", action="store_true",
                    help="also download + validate each SQLite schema (slow; "
                         "catches shape surprises the variant labels miss)")
    ap.add_argument("--validate", metavar="MANIFEST",
                    help="spot-check file URLs in an existing manifest and exit")
    ap.add_argument("--sample", type=int, default=5,
                    help="how many URLs --validate checks (default 5)")
    ap.add_argument("--recitations", action="store_true",
                    help="harvest the audio recitations catalog instead of "
                         "translations/tafsirs (writes recitations-manifest.json "
                         "unless -o is given)")
    args = ap.parse_args()

    if args.validate:
        return validate(args.validate, args.sample)

    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    cookie = os.environ.get("QUL_SESSION")
    if not cookie:
        sys.exit("Set QUL_SESSION in a .env file next to this script (or export "
                 f"it) — the {SESSION_COOKIE_NAME} cookie value. See README.md")

    session = requests.Session()
    session.cookies.set(SESSION_COOKIE_NAME, cookie, domain="qul.tarteel.ai")
    session.headers["User-Agent"] = "fukhushu-harvest/1.0"

    if args.recitations:
        out = ("recitations-manifest.json"
               if args.output == "manifest.json" else args.output)
        manifest, skipped = harvest_recitations(
            session, delay=args.delay, limit=args.limit
        )
        with open(out, "w") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=1)
        n = manifest["recitations"]
        ayah = sum(1 for e in n if e["cardinality"] == "ayah")
        print(f"\nWrote {out}: {len(n)} recitations "
              f"({ayah} ayah-by-ayah, {len(n) - ayah} gapless), "
              f"{len(skipped)} skipped")
        for s in skipped:
            print(f"  skip {s['id']:>4}  {s['name']}: {s['reason']}")
        return 0

    manifest, incompatible, bundled = harvest(
        session, delay=args.delay, limit=args.limit, deep=args.deep
    )
    with open(args.output, "w") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    write_compat_report(args.report, manifest, incompatible, bundled)
    print(f"\nWrote {args.output}: {len(manifest['resources'])} downloadable resources")
    print(f"Wrote {args.report}: {len(bundled)} already-bundled, "
          f"{len(incompatible)} incompatible")
    if incompatible:
        from collections import Counter
        for reason, count in Counter(x["reason"] for x in incompatible).most_common():
            print(f"  {count:3}  {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
