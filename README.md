# fukhushu-sources

Hosts the fuKhushu source catalog: `harvest.py` builds `manifest.json` — the
catalog the app's **Settings → Translations & Tafsirs** page fetches — from
[QUL](https://qul.tarteel.ai). The app reads
`https://raw.githubusercontent.com/Synthesium/fukhushu-sources/main/manifest.json`.

Layout: `harvest.py` (the tool), `manifest.json` (the published catalog),
`compatibility-report.md` (what's included/excluded and why — regenerated each
run), `.env` (your session cookie — **git-ignored**, see `.env.example`).

## Compatibility checking

Not every QUL resource fits fuKhushu's ayah-by-ayah model. Each run classifies
every resource and **only compatible ones go in `manifest.json`**; the rest are
listed in `compatibility-report.md` with the reason. This is decided cheaply
from the export variants each resource offers (no downloads):

- **Translations** need `simple.sqlite` or the *with-footnote-tags* export
  (ayah-level `sura, ayah, text`). Resources that only ship the generic
  `sqlite` are **word-by-word** (a `word_translation` word-level table the app
  can't render) → excluded.
- **Tafsirs** need any `sqlite` export (grouped `tafsir` table or a flat
  `translation`-shaped one — the app converts both).
- Anything with no SQLite export at all (JSON/DOCX only) → excluded.

`--deep` additionally downloads every export and validates its actual SQLite
schema against what the app converts (the same table/column checks as
`qul_converter.dart`) — slower, but catches shape surprises the labels miss.
Run it occasionally as a full audit:

```bash
.venv/bin/python harvest.py --deep -o manifest.json
```

## Why this exists

- QUL's **catalog API is public** (`/api/v1/resources/...`), but **download
  links are login-gated**: the per-file token only appears in HTML while
  logged in.
- The gated `/download` route redirects to a **public S3 URL** anyone can GET
  — but that URL **rotates whenever QUL re-exports** the file.
- So: you run this script occasionally (with your QUL session cookie) to
  resolve the current URLs; the app then downloads files **directly from
  QUL's host** using your published manifest. The only thing you host is one
  JSON file.

## One-time setup

1. Create a free account at qul.tarteel.ai (sign in works in the browser).
2. `python3 -m venv .venv && .venv/bin/pip install requests beautifulsoup4`
   (`.venv/` is git-ignored).
3. `cp .env.example .env`, then paste your session cookie into it (below).

The app already points at this repo — `kQulManifestUrl` in the app's
`lib/services/qul/qul_catalog_service.dart` is the raw `main/manifest.json`
URL. Change both if you ever rename/move the repo.

## Getting your session cookie

1. Log in at qul.tarteel.ai in your browser.
2. DevTools → Application/Storage → Cookies → `https://qul.tarteel.ai`.
3. Copy the value of **`_quran_com-community_session`** into `.env` as
   `QUL_SESSION=<value>`.

`.env` is git-ignored — the cookie never gets committed. It expires like any
web session; grab a fresh one when the harvest reports
`redirect failed — logged in?`.

## How it works

The harvest scrapes QUL's authenticated resource **listing** pages
(`/resources/translation`, `/resources/tafsir`) — each `<li>` carries the
DownloadableResource id, name, language tag, and the real tokened download
links. It picks the best SQLite variant (translations: *with-footnote-tags*
when present, else `simple.sqlite`; tafsirs: grouped `sqlite`, else
`simple.sqlite`), follows each login-gated `/download` redirect to its public
S3 URL, and enriches with the public API (author, record counts, language
direction). Copyright-restricted resources have no download files and are
skipped automatically.

## First run — smoke test

```bash
.venv/bin/python harvest.py --limit 5 -o /tmp/manifest-test.json
.venv/bin/python harvest.py --validate /tmp/manifest-test.json
```

Expect ~5 translations + ~5 tafsirs resolved. If everything is skipped with
`redirect failed — logged in?`, your `.env` cookie is stale — grab a fresh
one. QUL occasionally changes its listing markup; if a run suddenly skips
everything, check the `<li id="downloadable_resource_…">` / language-tag
selectors in `parse_listing()`.

## Full harvest + publish

```bash
.venv/bin/python harvest.py -o manifest.json
.venv/bin/python harvest.py --validate manifest.json   # anonymous URL spot-check
git add manifest.json && git commit -m "Update catalog" && git push
```

## Japanese dictionary catalog

`dictionaries-manifest.json` powers the app's tap-to-define Japanese
dictionary (reader dictionary mode). Unlike the QUL catalogs it is not
harvested: the database is built in the fuKhushu repo by
`scripts/build_japanese_dictionary_db.py` (JMdict + KANJIDIC2 via
[jmdict-simplified](https://github.com/scriptin/jmdict-simplified), EDRDG
licence CC BY-SA 4.0), zipped, and attached to a release here
(`japanese-dictionary-v<N>`). To refresh: download the new jmdict-simplified
release JSONs into fuKhushu `source_data/`, re-run the build script, create
the next `japanese-dictionary-v<N>` release with the new zip, update
`fileUrl`/sizes/counts/`version` in `dictionaries-manifest.json`, then commit
and push.

## Recitations (audio) catalog

`recitations-manifest.json` powers the app's reciter catalog the same way.
Each QUL recitation's sqlite export (a small DB of audio URLs + timings — the
audio itself lives on public CDNs) is downloaded, shape-checked
(`verses` = ayah-by-ayah · `surah_list`+`segments` = gapless), and its audio
host is verified to answer anonymously; recitations whose audio isn't public
are skipped and listed at the end of the run.

```bash
.venv/bin/python harvest.py --recitations --limit 5 -o /tmp/test.json  # smoke test
.venv/bin/python harvest.py --recitations                              # full run (~10 min)
git add recitations-manifest.json && git commit -m "Update recitations" && git push
```

## Refresh workflow

File URLs die when QUL re-exports a resource (users will see downloads fail
with "the catalog may be out of date"). When that happens — or on a monthly
cadence — re-run the harvest and commit the new manifest. The app picks it up
on the next catalog open; entries whose `qulUpdatedAt` changed show an
update button for users who already installed them.

## Licensing note

QUL only exposes download files for resources it may distribute
(copyright-restricted ones have no files and are skipped automatically).
Sources may still carry per-resource attribution obligations — check a
resource's page on QUL before promoting it, and keep author names intact in
the manifest (the app displays them).
