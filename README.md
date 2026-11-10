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
