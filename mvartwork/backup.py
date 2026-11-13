"""Portable MVArtwork catalogue/poster backups, independent of API software.

Exports a SQLite *consistent snapshot* + SHA256 verified poster blobs.
Import is a non-destructive MERGE, never overwrites an existing cached
poster, manual lock, or conflicting Series/Movie provider identity.
No API credentials, token file, systemd files, arbitrary filesystem paths,
or configuration from other products are ever included.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import zipfile

FORMAT = "mvartwork-backup"
SCHEMA = 1
MAX_BACKUP = 4 * 1024**3
MAX_CATALOGUE = 512 * 1024**2
MAX_MANIFEST = 24 * 1024**2
MAX_BLOBS = 100_000
MAX_POSTER = 12 * 1024**2
EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
BLOB_NAME = re.compile(r"^blobs/([0-9a-f]{2})/([0-9a-f]{64})(\.jpg|\.png|\.webp)$")
SHA = re.compile(r"^[0-9a-f]{64}$")
COLUMNS = ("manager","entity","library_id","season","imdb_id","tvdb_id",
           "tmdb_id","source_url","language","title","sha256","mime",
           "bytes","pinned","updated","error")

def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024*1024), b""):
            h.update(block)
    return h.hexdigest()

def _snapshot(store, database):
    with sqlite3.connect(store.dbpath.resolve().as_uri() + "?mode=ro", uri=True) as source:
        source.execute("PRAGMA query_only=ON")
        with sqlite3.connect(database) as backup:
            source.backup(backup)
            if backup.execute("PRAGMA quick_check(1)").fetchone()[0] != "ok":
                raise ValueError("Artwork catalogue snapshot failed integrity verification.")

def _referenced(database, store):
    result = {}
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.execute("PRAGMA query_only=ON")
        rows = db.execute("SELECT sha256,mime,bytes FROM artwork WHERE sha256 IS NOT NULL")
        for digest,mime,size in rows:
            if not isinstance(digest,str) or not SHA.fullmatch(digest) or mime not in EXT:
                raise ValueError("Invalid cached poster identity in catalogue.")
            if not isinstance(size,int) or not (0 < size <= MAX_POSTER):
                raise ValueError("Invalid cached poster size in catalogue.")
            relative = f"blobs/{digest[:2]}/{digest}{EXT[mime]}"
            f = store.folder / relative
            if f.is_symlink() or not f.is_file() or f.stat().st_size != size:
                raise ValueError("Referenced artwork poster is absent or invalid.")
            if relative not in result:
                actual = _sha(f)
                if actual != digest:
                    raise ValueError("Cached artwork checksum mismatch; export refused.")
                result[relative] = {"path":relative,"sha256":digest,"bytes":size}
                if len(result) > MAX_BLOBS:
                    raise ValueError("Too many artwork blobs for one backup.")
    return [result[k] for k in sorted(result)]

def create_backup(store):
    """Return positioned temporary ZIP file, archive byte count and record totals."""
    archive = tempfile.TemporaryFile(dir=store.folder)
    try:
        with tempfile.TemporaryDirectory(prefix=".mvartwork-backup-", dir=store.folder) as staging:
            catalogue = Path(staging) / "catalogue.sqlite3"
            # Exports must never see a half-written catalogue and its blobs.
            with store.lock:
                _snapshot(store, catalogue)
                if catalogue.stat().st_size > MAX_CATALOGUE:
                    raise ValueError("Artwork catalogue exceeds portable backup limit.")
                blobs = _referenced(catalogue, store)
                with sqlite3.connect(catalogue.resolve().as_uri()+"?mode=ro", uri=True) as db:
                    counts = dict(db.execute("SELECT manager,COUNT(*) FROM artwork GROUP BY manager"))
                manifest = {
                    "format":FORMAT,"schema":SCHEMA,
                    "version":__import__("mvartwork.service",fromlist=["VERSION"]).VERSION,
                    "database":{"name":"catalogue.sqlite3",
                                "bytes":catalogue.stat().st_size,
                                "sha256":_sha(catalogue)},
                    "blobs":blobs,
                    "counts":{"series":counts.get("series",0),"movie":counts.get("movie",0)}
                }
                raw=json.dumps(manifest,sort_keys=True,separators=(",",":")).encode()
                if len(raw)>MAX_MANIFEST:
                    raise ValueError("Artwork manifest exceeds size limit.")
                total = catalogue.stat().st_size + len(raw) + sum(i["bytes"] for i in blobs)
                if total > MAX_BACKUP:
                    raise ValueError("Artwork backup exceeds 4 GiB limit.")
                with zipfile.ZipFile(archive, mode="w",compression=zipfile.ZIP_STORED,
                                     allowZip64=True) as zf:
                    zf.write(catalogue, "catalogue.sqlite3")
                    for item in blobs:
                        zf.write(store.folder/item["path"],item["path"])
                    zf.writestr("manifest.json",raw)
        size=archive.tell()
        if not 0<size<=MAX_BACKUP:
            raise ValueError("Artwork backup exceeds configured archive limit.")
        archive.seek(0)
        return archive,size,manifest["counts"]
    except Exception:
        archive.close()
        raise

def _copy_verified_file(source, dest, expected_size, expected_sha):
    h=hashlib.sha256()
    actual=0
    with dest.open("xb") as target:
        while True:
            part=source.read(min(1024*1024, expected_size-actual+1))
            if not part:
                break
            actual+=len(part)
            if actual>expected_size:
                raise ValueError("Archive member exceeds declared size.")
            target.write(part)
            h.update(part)
    if actual!=expected_size or h.hexdigest()!=expected_sha:
        raise ValueError("Artwork archive member checksum mismatch.")

def _parse_archive(path, staging):
    if path.stat().st_size>MAX_BACKUP or path.stat().st_size<100:
        raise ValueError("Invalid artwork backup archive size.")
    zf=zipfile.ZipFile(path,mode="r",allowZip64=True)
    try:
        infos=zf.infolist()
        if not infos or len(infos)>MAX_BLOBS+2:
            raise ValueError("Unexpected number of archive members.")
        names={}
        total=0
        for info in infos:
            name=info.filename
            if (name in names or name.startswith("/") or
                "\\" in name or ".." in name.split("/") or info.is_dir() or
                (name not in ("manifest.json","catalogue.sqlite3") and
                 not BLOB_NAME.fullmatch(name))):
                raise ValueError("Unexpected or unsafe artwork archive member.")
            mode=(info.external_attr>>16)
            if stat.S_IFMT(mode) not in (0,stat.S_IFREG):
                raise ValueError("Artwork archive may contain regular files only.")
            if info.compress_type!=zipfile.ZIP_STORED or info.file_size<0:
                raise ValueError("Artwork archive compression is unsupported.")
            allowed=(MAX_MANIFEST if name=="manifest.json" else
                     MAX_CATALOGUE if name=="catalogue.sqlite3" else MAX_POSTER)
            if info.file_size>allowed:
                raise ValueError("Oversized artwork archive member.")
            total+=info.file_size
            if total>MAX_BACKUP:
                raise ValueError("Expanded artwork archive exceeds size limit.")
            names[name]=info
        if "manifest.json" not in names or "catalogue.sqlite3" not in names:
            raise ValueError("Artwork backup requires catalogue and manifest.")
        manifest=json.loads(zf.read("manifest.json"))
        if not isinstance(manifest,dict) or manifest.get("format")!=FORMAT or manifest.get("schema")!=SCHEMA:
            raise ValueError("Unsupported artwork backup format.")
        dbmeta=manifest.get("database")
        if not isinstance(dbmeta,dict) or dbmeta.get("name")!="catalogue.sqlite3":
            raise ValueError("Invalid artwork catalogue descriptor.")
        blobs=manifest.get("blobs")
        if not isinstance(blobs,list) or len(blobs)>MAX_BLOBS:
            raise ValueError("Invalid artwork blob catalogue.")
        described={"manifest.json","catalogue.sqlite3"}
        expected={};
        for item in blobs:
            if not isinstance(item,dict):
                raise ValueError("Invalid artwork blob manifest entry.")
            name=item.get("path")
            m=BLOB_NAME.fullmatch(name or "")
            if not m or m[1]!=m[2][:2] or name in described:
                raise ValueError("Invalid artwork backup blob path.")
            if item.get("sha256")!=m[2] or type(item.get("bytes")) is not int:
                raise ValueError("Artwork blob checksum or size is invalid.")
            described.add(name);expected[name]=item
        if set(names)!=described:
            raise ValueError("Artwork archive members differ from manifest.")
        if type(dbmeta.get("bytes")) is not int or dbmeta["bytes"]!=names["catalogue.sqlite3"].file_size:
            raise ValueError("Artwork catalogue size mismatch.")
        if not isinstance(dbmeta.get("sha256"),str) or not SHA.fullmatch(dbmeta["sha256"]):
            raise ValueError("Invalid artwork catalogue digest.")
        database=Path(staging)/"catalogue.sqlite3"
        with zf.open("catalogue.sqlite3") as part:
            _copy_verified_file(part,database,dbmeta["bytes"],dbmeta["sha256"])
        for name,item in expected.items():
            if names[name].file_size!=item["bytes"] or not (0<item["bytes"]<=MAX_POSTER):
                raise ValueError("Artwork blob size mismatch.")
            dest=Path(staging)/Path(name).name
            with zf.open(name) as src:
                _copy_verified_file(src,dest,item["bytes"],item["sha256"])
            mime={".jpg":"image/jpeg",".png":"image/png",".webp":"image/webp"}[Path(name).suffix]
            from mvartwork.service import image_type
            with dest.open("rb") as f:
                sample=f.read(16)
            if image_type(sample)[0]!=mime:
                raise ValueError("Artwork blob MIME mismatch.")
        return database,manifest,expected
    finally:
        zf.close()

def merge_backup(store,path):
    """Validate entire archive, then merge; existing artwork and locks always win."""
    from mvartwork.service import validated_key,safe_url,ID_RE,MAX_IMAGE
    path=Path(path)
    with tempfile.TemporaryDirectory(prefix=".mvartwork-import-",dir=store.folder) as staging:
        dbpath,manifest,blobs=_parse_archive(path,staging)
        rows=[]
        with sqlite3.connect(dbpath.resolve().as_uri()+"?mode=ro",uri=True) as src:
            src.execute("PRAGMA trusted_schema=OFF")
            src.execute("PRAGMA query_only=ON")
            if src.execute("PRAGMA quick_check(1)").fetchone()[0]!="ok":
                raise ValueError("Artwork backup catalogue failed integrity check.")
            columns={r[1] for r in src.execute("PRAGMA table_info('artwork')")}
            if not set(COLUMNS).issubset(columns):
                raise ValueError("Artwork backup catalogue schema incomplete.")
            for record in src.execute("SELECT "+",".join(COLUMNS)+" FROM artwork"):
                if len(rows)>=MAX_BLOBS:
                    raise ValueError("Too many artwork entries.")
                row=dict(zip(COLUMNS,record))
                validated_key(row)
                for key in ("imdb_id","tvdb_id","tmdb_id"):
                    v=row[key]
                    if v is not None and (not isinstance(v,str) or len(v)>80 or not ID_RE.fullmatch(v)):
                        raise ValueError("Invalid imported provider ID.")
                safe_url(row["source_url"])
                if row["language"] is not None and (not isinstance(row["language"],str) or
                        not re.fullmatch(r"[A-Za-z-]{2,16}",row["language"])):
                    raise ValueError("Invalid artwork language.")
                if row["title"] is not None and (not isinstance(row["title"],str) or
                        len(row["title"])>300 or any(ord(c)<32 for c in row["title"])):
                    raise ValueError("Invalid artwork title.")
                if row["pinned"] not in (0,1):
                    raise ValueError("Invalid artwork manual lock.")
                if row["sha256"]:
                    mime=row["mime"]
                    if mime not in EXT or not SHA.fullmatch(row["sha256"]):
                        raise ValueError("Invalid artwork reference.")
                    name=f"blobs/{row['sha256'][:2]}/{row['sha256']}{EXT[mime]}"
                    if name not in blobs or row["bytes"]!=blobs[name]["bytes"]:
                        raise ValueError("Missing verified poster in backup.")
                elif row["mime"] is not None or row["bytes"] not in (0,None):
                    raise ValueError("Orphaned poster metadata in backup.")
                rows.append(row)
        preserved=0;imported=0;restored=0;conflicts=0
        with store.lock,store.connect() as target:
            for row in rows:
                key=validated_key(row)
                current=target.execute("""SELECT sha256,pinned,imdb_id,tvdb_id,tmdb_id,title
                    FROM artwork WHERE manager=? AND entity=? AND library_id=? AND season=?""",key).fetchone()
                if current is not None:
                    if current["pinned"] or current["sha256"] or not row["sha256"]:
                        # Merge has no work to do for an already registered
                        # uncached row; never count a no-op as an import.
                        preserved+=1
                        continue
                    # No guessed re-link: same library-local ID might have been
                    # recycled for a different provider identity or series title.
                    different=any(current[k] and row[k] and current[k]!=row[k]
                                  for k in ("imdb_id","tvdb_id","tmdb_id"))
                    if (not any(current[k] and row[k] for k in ("imdb_id","tvdb_id","tmdb_id")) and
                        current["title"] and row["title"] and
                        current["title"].casefold()!=row["title"].casefold()):
                        different=True
                    if different:
                        conflicts+=1
                        continue
                if row["sha256"]:
                    name=f"blobs/{row['sha256'][:2]}/{row['sha256']}{EXT[row['mime']]}"
                    raw=(Path(staging)/Path(name).name).read_bytes()
                    assert len(raw)<=MAX_IMAGE
                    destination=store._blob(row["sha256"],row["mime"])
                    if destination.is_symlink() or (
                        destination.is_file() and _sha(destination)!=row["sha256"]):
                        raise ValueError("Existing artwork blob is unsafe or has mismatched checksum.")
                    digest,mime,size=store._save_blob(raw)
                    if (digest,mime,size)!=(row["sha256"],row["mime"],row["bytes"]):
                        raise ValueError("Validated poster changed during import.")
                    restored+=1
                if current is None:
                    target.execute("""INSERT INTO artwork
                      (manager,entity,library_id,season,imdb_id,tvdb_id,tmdb_id,
                       source_url,language,title,sha256,mime,bytes,pinned,updated,error)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      tuple(row[x] for x in COLUMNS))
                else:
                    target.execute("""UPDATE artwork SET sha256=?,mime=?,bytes=?,pinned=?,
                         updated=?,error=NULL WHERE manager=? AND entity=? AND
                         library_id=? AND season=? AND sha256 IS NULL AND pinned=0""",
                         (row["sha256"],row["mime"],row["bytes"],row["pinned"],
                          row["updated"],*key))
                if row["sha256"]:
                    target.execute("""DELETE FROM jobs WHERE manager=? AND entity=? AND
                                      library_id=? AND season=?""",key)
                imported+=1
        return {"imported":imported,"postersRestored":restored,
                "existingPreserved":preserved,"conflictsSkipped":conflicts,
                "archiveEntries":len(rows),"sourceVersion":manifest.get("version"),
                "mode":"safe-merge"}
