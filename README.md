## v0.1.5 — safe library synchronization and stale artwork cleanup

- Complete, manager-scoped library snapshots submitted only by the authenticated Media Vision client after successful full registration.
- Manual Sync Library request, Auto Sync preference and independently verified sync status.
- Operator-triggered Remove Stale Artwork requires fresh, complete snapshots, refuses pending sync and mass-deletion anomalies, deletes only orphan registrations and unreferenced cached image blobs, and never touches real media.
- Series, Season and Movie IDs remain separate; no title or year guessing.

Develop prerelease only. Previous v0.1.4 backups and safe-merge support remain available.

---

## v0.1.4 — portable artwork export and safe import

The independent MVArtwork service now offers two authenticated
loopback-only operations (never on the public client API port):

- GET /v1/export returns a portable ZIP of a consistent
  artwork.sqlite3 snapshot and verified cached poster blobs.
  It includes both independent Series/Seasons and Movies entries,
  cached posters and manual locks. API keys, metadata indexes,
  configuration secrets and software packages are excluded.
- POST /v1/import accepts that ZIP with an explicit safe-merge
  header. A bounded, strongly validated archive must pass
  SHA-256 verification for its catalogue and every poster before
  any catalogue changes. The merge never replaces an existing
  poster or manual lock, and declines conflicted provider
  identities rather than assuming a library-local ID is safe.
  Imported new records retain their poster identity and manual
  lock. New missing entries are registered for later scans.
  Running download jobs and queue history remain local.
  Repeated import is idempotent.

A 4 GiB archive limit, per-image 12 MiB limit, maximum 100k blobs,
strict archive names (no traversal, symlinks or compression), and
atomic SQLite record transactions apply. The API WebUI separately
authenticates the administrator/CSRF session and streams the backup
with no full archive held in memory.

The previous v0.1.3 releases remain available independently for
rollback. Release v0.1.4 is Develop prerelease, not Stable/latest.

# mvartwork — optional Media Vision artwork component

Independent, optional native artwork cache for **library entries only**. No TVDB catalogue mirror.
The component is optional; the main Media Vision and mv-api remain usable if absent.

- Separate SQLite database and content-addressed blob folder under `/var/lib/mvartwork`.
- One preferred poster per Series, Season or Movie entry; distinguished by manager, local ID and season number.
- IMDb, TVDB and TMDB IDs are nullable mappings; changing providers never deletes a valid poster.
- Images fetched from explicit approved HTTPS hosts; fixed max 12 MiB, checksummed, written atomically.
- Manual poster upload locks selection until explicitly replaced; error leaves previous poster in place.
- Bounded retry, pause/resume, missing-art scan; no bulk TVDB crawl, media-file access or automatic deletion.
- Internal HTTP service binds `127.0.0.1:9494`, privileged operations require shared token.
- `mv-api` WebUI on port 9393 remains the only user-facing artwork manager.
- Series and Movies stay separately scoped even though the implementation is shared.

## Development / offline test

```sh
python3 -m unittest discover -s tests -v
MVARTWORK_TOKEN_FILE=/tmp/private-token MVARTWORK_DATA_DIR=/tmp/mvartwork python3 -m mvartwork
```

Token must contain at least 24 random characters. No release or installation is performed by tests.
A client registers actual library artwork with `POST /v1/items` using
`X-MVArtwork-Key` and JSON keys `manager`, `entity`, `library_id`,
`season` (for seasons), `source_url` and optional IMDb/TVDB/TMDB IDs.

The service has no ability to enumerate a TVDB catalogue. Only records
explicitly registered by an authorized Media Vision library client are cached.

## Native release and independent installation

The tagged release archive is built reproducibly with
`python3 deploy/package.py dist`. Its SHA256 and size are recorded in
`dist/mvartwork-manifest.json`; an installer refuses any mismatched bundle.
Media Vision API's separately versioned Develop release pins the public
mvartwork GitHub archive SHA256; an administrator opts in through the API
Artwork page. This add-on is never auto-installed when upgrading Media Vision
or the API, and never touches media files. Its state is retained on routine
upgrades unless the administrator explicitly chooses Delete when uninstalling.

## v0.1.1 — indexed Artwork library browsing

The independent, opt-in MVArtwork component serves bounded, server-paged
library records from its own SQLite catalogue. GET /v1/items accepts
manager=series|movie, entity=all|series|season|movie,
status=all|cached|missing|failed|locked, optional search, limit=1..100
and bounded offset, returning items, total, offset, limit, hasMore.
Title and provider-ID search is SQL-parameterized, manager-scoped, and treats
literal percent and underscore as text, not SQL wildcards.

v0.1.1 additionally accepts an optional title on trusted Series/Movie
registration. This is presentation metadata only, not identity evidence or
a title-matching input. Old v0.1.0 records without titles remain usable and
display their local IDs until registration refresh adds names. The startup
migration adds only the nullable title column and browse index, preserving
cached posters, hashes, manual locks, jobs and independent manager identity.
No bulk provider metadata download occurs and no existing artwork cache is
removed. Existing Media Vision v1.0.67 clients can still register unchanged.

The Media Vision API Artwork WebUI provides the front-end controls and must
be updated independently. Deploying this bundle alone does not alter the
Media Vision app, API software, provider configuration or real media.

## v0.1.2 — live poster download activity and safe title-only backfill

The optional add-on records a small, bounded activity log without logging
download URLs, tokens or HTTP request headers. An authenticated
GET /v1/activity?manager=series|movie&limit=1..25 returns a snapshot of
current running poster downloads, separate Series and Movie pending/retrying/
failed queue counts, and up to 25 recent downloaded/retried/failed/uploaded
events. Each event includes the registered title if available, otherwise the
local ID, its entity/season, duration and successful image bytes, or the
exception category only on retry or failure.

The add-on keeps only the last 200 events, so a large library never produces
unbounded activity storage. The SQLite schema migration from v0.1.1 adds a
nullable job start-time column and a separate activity table without changing
original artwork rows, hashes, blob files, manual locks, registration IDs or
queue records. On process restart any interrupted job is returned to the
existing retry queue and its stale active timestamp is cleared.

The independent Media Vision API UI candidate provides the desktop two-column
artwork list, independent Series+Seasons and Movies missing counters, and
live activity panel with bounded four-second polling. The API does not expose
the activity endpoint on the client metadata port 9292.

MVArtwork v0.1.2 also accepts authenticated POST /v1/titles with manager=
series|movie and 1–200 library ID/title pairs. The server validates the
entire request before a transaction, then updates the optional display title
for EXISTING registered rows only, across all seasons of a Series. An ID
belonging to the other manager never changes. Nothing is registered, queued,
downloaded or relinked; poster hashes, pins, media and original provider IDs
are untouched. This permits the separately updated Media Vision app to fill
historic local-ID labels automatically, in bounded batches, without a button
or forced poster re-download.

The API/Media Vision app remain independent releases. The v0.1.2 component is
installed only by an explicit action in the API Artwork manager.


## v0.1.3 — chosen posters and reused cached identities

New authenticated, bounded private MVArtwork endpoints:

- GET /v1/choices?manager=series&entity=series&season=-1&search=title
  lists at most 30 already-cached records in the same manager/entity/season.
- POST /v1/choose selects an existing cached poster for a registered destination,
  pins the selected bytes without fetching any network content, and retains
  both records and their original media identities.
- POST /v1/download-selected accepts a selected, approved HTTPS artwork URL
  only for an existing registered title/season; validates URL, image bytes and
  12 MiB size before replacing/pinning poster. Errors preserve the previous
  poster and lock without any database changes.
- POST /v1/items can reuse a previous cached poster on library re-addition
  ONLY for the same manager, entity, season and exact IMDb identity with
  unambiguous poster hash. It never matches by title resemblance or crosses
  manager boundaries, and never overwrites a manually pinned poster.

The API administrator Artwork page offers a responsive preview gallery of
existing cached candidates and explicit Use this poster / Download and lock
poster controls. Existing data, identities, poster blobs, manual locks, downloads,
and histories are preserved through the additive upgrade. Upgrade the separate
MVArtwork component explicitly from the API Updates section after updating the
independent API product; no live component auto-update occurs on API software
installation.
