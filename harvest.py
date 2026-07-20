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


def pick_translation_variant(variants: dict) -> tuple[str | None, bool]:
    """Prefer the SQLite *with-footnote-tags* export (footnotes column +
    `<sup foot_note>` markers); else the plain simple/generic SQLite.

    Returns (href, has_footnotes). Skips word-by-word `chunk` files and the
    `inline-footnote` variant (a different, unsupported footnote encoding)."""
    def find(pred):
        for label, href in variants.items():
            if pred(label.lower()):
                return href
        return None

    ft = find(lambda l: "sqlite" in l and "footnote-tags" in l)
    if ft:
        return ft, True
    plain = find(
        lambda l: "sqlite" in l and "chunk" not in l and "inline-footnote" not in l
    )
    return plain, False


def pick_tafsir_variant(variants: dict) -> str | None:
    """Prefer the grouped `sqlite` export (passage ranges preserved); fall
    back to `simple.sqlite` (flat, one entry per ayah — the app handles both)."""
    generic = simple = None
    for label, href in variants.items():
        ll = label.lower()
        if "sqlite" not in ll or "chunk" in ll:
            continue
        if "simple" in ll:
            simple = simple or href
        else:
            generic = generic or href
    return generic or simple


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

def harvest(session: requests.Session, delay: float, limit) -> dict:
    lang_index = build_language_index(session)
    api_index = {
        "translation": build_api_index(session, "translation"),
        "tafsir": build_api_index(session, "tafsir"),
    }

    resources = []
    skipped = []
    for kind in ("translation", "tafsir"):
        listing = parse_listing(session, kind)
        if limit:
            listing = listing[:limit]
        print(f"{kind}: {len(listing)} resources in listing")
        for i, item in enumerate(listing, 1):
            name = item["name"]
            print(f"[{kind} {i}/{len(listing)}] {name[:42]} ... ", end="", flush=True)

            if kind == "translation":
                href, has_footnotes = pick_translation_variant(item["variants"])
            else:
                href, has_footnotes = pick_tafsir_variant(item["variants"]), False
            if not href:
                skipped.append((kind, item["id"], name, "no sqlite variant"))
                print("SKIP (no sqlite)")
                continue

            location, size = resolve_and_size(session, href)
            if not location:
                skipped.append((kind, item["id"], name, "redirect failed — logged in?"))
                print("SKIP (redirect failed)")
                continue

            api = api_index[kind].get(name.strip().lower(), {})
            api_lang = api.get("language") or api.get("language_name")
            resources.append({
                "qulId": item["id"],
                "type": kind,
                "name": name,
                "translatedName": (api.get("translated_name") or {}).get("name"),
                "author": api.get("author_name"),
                "slug": api.get("slug"),
                "language": language_entry(lang_index, item["langs"], api_lang),
                "recordsCount": api.get("records_count"),
                "qulUpdatedAt": iso_from_epoch(api.get("updated_at")),
                "hasFootnotes": has_footnotes,
                "fileFormat": "sqlite",
                "fileUrl": location,
                "fileSizeBytes": size,
            })
            print(f"ok ({size or '?'}B{', footnotes' if has_footnotes else ''})")
            time.sleep(delay)  # be polite to QUL

    resources.sort(key=lambda r: (r["language"]["iso"], r["type"], r["name"]))
    if skipped:
        print(f"\nSkipped {len(skipped)}:")
        for kind, rid, name, reason in skipped:
            print(f"  - {kind} {rid} {name[:40]}: {reason}")

    return {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": datetime.now(tz=timezone.utc).isoformat(),
        "resources": resources,
    }


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
    ap.add_argument("--delay", type=float, default=0.5,
                    help="seconds between resource downloads (default 0.5)")
    ap.add_argument("--limit", type=int, default=None,
                    help="only process the first N of each type (for testing)")
    ap.add_argument("--validate", metavar="MANIFEST",
                    help="spot-check file URLs in an existing manifest and exit")
    ap.add_argument("--sample", type=int, default=5,
                    help="how many URLs --validate checks (default 5)")
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

    manifest = harvest(session, delay=args.delay, limit=args.limit)
    with open(args.output, "w") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    print(f"\nWrote {args.output}: {len(manifest['resources'])} resources")
    return 0


if __name__ == "__main__":
    sys.exit(main())
