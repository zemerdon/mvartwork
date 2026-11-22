# MVArtwork v0.2.2 Stable

- Provides independent Series+Seasons and Movies synchronization requests, with validated per-manager request IDs.
- Exposes exact processed/total registration counts, stages, terminal states, failures, and elapsed timing for real sync progress bars in Media Vision Core.
- Adds an explicit Stop Sync control that cancels pending work or requests safe in-flight cancellation, without deleting cached posters or media.
- Prevents cancelled or stale syncs from publishing incomplete or superseded library snapshots.
- Preserves existing snapshot counts, differences, non-destructive stale-artwork preview, optional auto-sync and manager isolation.
- All client writes use the independent Media Vision API authenticated gateway; admin control stays on its trusted management interface.
- Independent release and recovery. Installing this add-on does not install or upgrade Core, the API, or any installed user instance automatically.
