# mvartwork — source-only handoff (2026-11-10)

**No release or deployment is authorized.** This is an independent local Git
repository for the optional add-on, NOT the Media Vision application repository
and NOT the independent Media Vision API repository.

## Implemented

- Python 3 standard-library-only loopback service on `127.0.0.1:9494`.
- Separate durable SQLite store, jobs, content-addressed image blobs,
  checksums, manual selection/lock, safe allowlisted HTTPS image retrieval,
  limited retries, missing-poster queue, pause/resume and cache metrics.
- Keys are `manager + entity + local library ID + season`, with **no Series
  fallback to Movies or vice versa**. IMDb/TVDB/TMDB mappings do not own images.
- Only explicitly registered library records can trigger fetching. No full
  TVDB mirror, no automatic media-file/library scanning, no media writes.
- Bundle and SHA256 manifest built reproducibly with `python3 deploy/package.py dist`.
- Offline native installer `deploy/install.py` requires an exact pinned SHA256,
  rejects unsafe archive entries, manages only its own systemd service and storage,
  supports service rollback and explicit uninstall with data retention or purge.
- `11` unittest fixtures passed, including direct HTTP, security, data
  preservation, deduplication, installer, reproducible archive and uninstall.

## Important remaining integration work

1. The Media Vision application does **not yet register existing/new library
   Series, Season and Movie posters** through the mv-api
   `POST /api/v1/artwork/items` endpoint. Integration must reuse each manager's
   own API base URL/key and real `MediaCover.RemoteUrl` while preserving
   working local/remote cover fallbacks. Avoid the active dirty v1.0.67 worktree;
   coordinate with that main-chat work before merging anything.
2. The caller currently supplies its already-selected preferred poster URL.
   English-first selection/rating across TVDB alternatives, including credible
   language-neutral fallback, remains to be implemented or confirmed in the
   existing metadata source. Do not guess mappings.
3. Choose and prepare a canonical separate GitHub release repository for
   `mvartwork`; the mv-api add-on updater currently anticipates the **proposed**
   `https://github.com/zemerdon/mvartwork` release URL but that release does
   not exist. Never claim it does.
4. Once **separately authorized**, produce v0.1.0 native add-on asset and pin
   exact SHA256 in root-owned `/etc/mv-api-addons/mvartwork.json` using official
   HTTPS release URL. Test the privileged install/uninstall/rollback route in a
   disposable LXC. Do not modify protected installed mv-api/app without approval.
5. Cross-product release compatibility (MV app <> mv-api <> mvartwork), upgrades,
   native installers, service stop/restart and configured poster storage require
   full production-equivalent QA before any publication.

## Resume references

- Independent add-on source: `/home/svrunner/mvartwork-source-20261110`
- Local-only bundle: `dist/mvartwork-0.1.0-dev.tar.gz`
- Companion mv-api isolated worktree:
  `/home/svrunner/mv-api-mvartwork-optin-20261110`
- Isolated QA and disposable combined API-artwork smoke:
  `/home/svrunner/mvartwork-qa-20261110`
- External authority: live Media Vision
  `MV_CURRENT_STATE.md` and `handoffs/ACTIVE.md`, updated source-only.
- SentinelX resume checkpoint `sentinelx_context_sxc_GQPERTEF`.

The installed Media Vision, mv-api, real media, operational databases,
Sonarr/Radarr/Prowlarr, Switch Vision and Library Optimizer were not changed.
