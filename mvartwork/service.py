"""Independent, library-scoped Media Vision artwork cache (stdlib only)."""
from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs, unquote
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError

VERSION = "0.1.2"
ORIGINS = {"artworks.thetvdb.com", "images.thetvdb.com",
           "thetvdb.com", "www.thetvdb.com", "image.tmdb.org"}
MAX_IMAGE = 12 * 1024 * 1024
MAX_JSON = 17 * 1024 * 1024
ID_RE = re.compile(r"^[a-zA-Z0-9_.:-]{1,100}$")
SHA_RE = re.compile(r"^[a-f0-9]{64}$")
KEYS = ("manager", "entity", "library_id", "season")
CREATE = """
CREATE TABLE IF NOT EXISTS artwork (
 manager TEXT NOT NULL CHECK(manager IN ('series','movie')),
 entity TEXT NOT NULL CHECK(entity IN ('series','season','movie')),
 library_id TEXT NOT NULL,
 season INTEGER NOT NULL DEFAULT -1,
 imdb_id TEXT, tvdb_id TEXT, tmdb_id TEXT,
 source_url TEXT, language TEXT, title TEXT,
 sha256 TEXT, mime TEXT, bytes INTEGER NOT NULL DEFAULT 0,
 pinned INTEGER NOT NULL DEFAULT 0,
 updated TEXT, error TEXT,
 PRIMARY KEY(manager,entity,library_id,season)
);
CREATE TABLE IF NOT EXISTS jobs (
 manager TEXT NOT NULL, entity TEXT NOT NULL, library_id TEXT NOT NULL,
 season INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 due REAL NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'pending',
 started_at REAL, PRIMARY KEY(manager,entity,library_id,season)
);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 at TEXT NOT NULL,
 manager TEXT NOT NULL CHECK(manager IN ('series','movie')),
 entity TEXT NOT NULL,
 library_id TEXT NOT NULL,
 season INTEGER NOT NULL,
 title TEXT,
 outcome TEXT NOT NULL CHECK(outcome IN ('downloaded','retry','failed','uploaded')),
 bytes INTEGER NOT NULL DEFAULT 0,
 duration_ms INTEGER NOT NULL DEFAULT 0,
 attempts INTEGER NOT NULL DEFAULT 0,
 detail TEXT
);
CREATE TABLE IF NOT EXISTS options (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT OR IGNORE INTO options(key,value) VALUES('paused','0');
"""

def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def validated_key(d):
    manager, entity, library_id = (d.get(k) for k in KEYS[:3])
    if manager not in ("series","movie") or entity not in ("series","season","movie"):
        raise ValueError("Invalid manager or entity.")
    if (manager == "movie") != (entity == "movie"):
        raise ValueError("Movies and Series artwork must remain independently scoped.")
    if not isinstance(library_id, str) or not ID_RE.fullmatch(library_id):
        raise ValueError("Invalid local library ID.")
    season = d.get("season", -1)
    if type(season) is not int or not (-1 <= season <= 10000):
        raise ValueError("Invalid season number.")
    if entity == "season" and season < 0:
        raise ValueError("Season artwork requires an explicit season number.")
    if entity != "season" and season != -1:
        raise ValueError("Only season artwork accepts season number.")
    return manager, entity, library_id, season

def safe_url(value):
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("Invalid artwork URL.")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.hostname not in ORIGINS
        or parsed.username or parsed.password or parsed.port not in (None,443)
        or not parsed.path or parsed.fragment):
        raise ValueError("Artwork URL must use an approved HTTPS artwork host.")
    return value

def image_type(data):
    if data.startswith(b"\xff\xd8\xff"): return "image/jpeg", ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"): return "image/png", ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP": return "image/webp", ".webp"
    raise ValueError("Unsupported artwork content.")

class SafeRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        safe_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)

def fetch_image(url):
    safe_url(url)
    req = Request(url, headers={"User-Agent":"MediaVisionArtwork/0.1","Accept":"image/jpeg,image/png,image/webp"})
    with build_opener(SafeRedirects()).open(req, timeout=25) as response:
        actual = response.geturl()
        safe_url(actual)
        declared = response.headers.get("Content-Length")
        if declared and int(declared) > MAX_IMAGE:
            raise ValueError("Poster exceeds configured maximum size.")
        data = response.read(MAX_IMAGE + 1)
        if len(data) > MAX_IMAGE: raise ValueError("Poster exceeds configured maximum size.")
        image_type(data)
        return data

class Store:
    def __init__(self, folder, downloader=fetch_image):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.dbpath = self.folder / "artwork.sqlite3"
        self.blobs = self.folder / "blobs"
        self.blobs.mkdir(exist_ok=True)
        self.downloader = downloader
        self.lock = threading.RLock()
        self.closed = False
        with self.connect() as db:
            db.executescript(CREATE)
            # Additive, backward-compatible column for a v0.1.0 catalogue;
            # never rebuild, truncate or replace the user's poster database.
            columns={col["name"] for col in db.execute("PRAGMA table_info(artwork)")}
            if "title" not in columns:
                db.execute("ALTER TABLE artwork ADD COLUMN title TEXT")
            db.execute("CREATE INDEX IF NOT EXISTS idx_artwork_browse ON artwork(manager,entity,library_id,season)")
            job_columns={col["name"] for col in db.execute("PRAGMA table_info(jobs)")}
            if "started_at" not in job_columns:
                db.execute("ALTER TABLE jobs ADD COLUMN started_at REAL")
            # A service crash during download must not strand queue entries.
            db.execute("UPDATE jobs SET state='retry',due=0,started_at=NULL WHERE state='running'")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.dbpath, timeout=20)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=20000")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def option(self, name):
        with self.connect() as db:
            row = db.execute("SELECT value FROM options WHERE key=?", (name,)).fetchone()
            return row["value"] if row else None

    def paused(self):
        return self.option("paused") == "1"

    def set_paused(self, value):
        with self.lock, self.connect() as db:
            db.execute("UPDATE options SET value=? WHERE key='paused'", ("1" if value else "0",))
            db.commit()

    def register(self, data):
        key = validated_key(data)
        url = safe_url(data.get("source_url"))
        language = data.get("language") or None
        if language and (not isinstance(language,str) or not re.fullmatch(r"[A-Za-z-]{2,16}", language)):
            raise ValueError("Invalid poster language.")
        title=data.get("title")
        if title is not None:
            if not isinstance(title,str) or len(title)>300 or any(ord(c)<32 for c in title):
                raise ValueError("Invalid artwork title.")
            title=title.strip() or None
        ids = []
        for k in ("imdb_id","tvdb_id","tmdb_id"):
            v = data.get(k)
            if v is not None and (not isinstance(v,str) or len(v)>80 or not ID_RE.fullmatch(v)):
                raise ValueError("Invalid provider ID.")
            ids.append(v)
        with self.lock, self.connect() as db:
            before = db.execute("SELECT sha256,pinned FROM artwork WHERE manager=? AND entity=? AND library_id=? AND season=?", key).fetchone()
            db.execute("""INSERT INTO artwork(manager,entity,library_id,season,imdb_id,tvdb_id,tmdb_id,source_url,language,title)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(manager,entity,library_id,season) DO UPDATE SET
                 imdb_id=COALESCE(excluded.imdb_id,artwork.imdb_id),
                 tvdb_id=COALESCE(excluded.tvdb_id,artwork.tvdb_id),
                 tmdb_id=COALESCE(excluded.tmdb_id,artwork.tmdb_id),
                 source_url=COALESCE(excluded.source_url,artwork.source_url),
                 language=COALESCE(excluded.language,artwork.language),
                 title=COALESCE(excluded.title,artwork.title)""", (*key,*ids,url,language,title))
            if url and (before is None or before["sha256"] is None) and (before is None or not before["pinned"]):
                self._enqueue(db,key)
            db.commit()
        return self.item(key)

    def _enqueue(self, db, key):
        db.execute("""INSERT INTO jobs(manager,entity,library_id,season,state,due,attempts)
          VALUES(?,?,?,?,'pending',0,0)
          ON CONFLICT(manager,entity,library_id,season) DO UPDATE SET
          state='pending',due=0,attempts=0,started_at=NULL""", key)

    def enqueue(self, data):
        key = validated_key(data)
        with self.lock, self.connect() as db:
            item = db.execute("SELECT source_url,pinned FROM artwork WHERE manager=? AND entity=? AND library_id=? AND season=?", key).fetchone()
            if item is None: raise KeyError("Artwork record was not registered.")
            if item["pinned"]: raise ValueError("Manually selected poster is locked.")
            if not item["source_url"]: raise ValueError("No approved poster URL is available.")
            self._enqueue(db,key)
            db.commit()
        return self.item(key)

    def _blob(self, digest, mime):
        ext = {"image/jpeg":".jpg","image/png":".png","image/webp":".webp"}[mime]
        return self.blobs / digest[:2] / (digest+ext)

    def _save_blob(self, data):
        if not data or len(data)>MAX_IMAGE: raise ValueError("Invalid artwork size.")
        mime,_ = image_type(data)
        digest = hashlib.sha256(data).hexdigest()
        file = self._blob(digest,mime)
        file.parent.mkdir(exist_ok=True)
        if not file.is_file():
            tmp = file.with_name(file.name + "." + str(os.getpid()) + "." + str(threading.get_ident()) + ".tmp")
            try:
                with tmp.open("xb") as f:
                    f.write(data); f.flush(); os.fsync(f.fileno())
                os.replace(tmp,file)
            finally:
                tmp.unlink(missing_ok=True)
        return digest,mime,len(data)

    def upload(self, data):
        key = validated_key(data)
        content = data.get("base64")
        if not isinstance(content,str) or len(content)>MAX_JSON:
            raise ValueError("Invalid manual artwork upload.")
        try: raw = base64.b64decode(content,validate=True)
        except (ValueError,base64.binascii.Error): raise ValueError("Invalid base64 image.")
        digest,mime,length = self._save_blob(raw)
        with self.lock,self.connect() as db:
            db.execute("""INSERT INTO artwork(manager,entity,library_id,season,sha256,mime,bytes,pinned,updated,error)
                VALUES(?,?,?,?,?,?,?,1,?,NULL)
                ON CONFLICT(manager,entity,library_id,season) DO UPDATE SET
                sha256=excluded.sha256,mime=excluded.mime,bytes=excluded.bytes,
                pinned=1,updated=excluded.updated,error=NULL""",(*key,digest,mime,length,now()))
            db.execute("DELETE FROM jobs WHERE manager=? AND entity=? AND library_id=? AND season=?",key)
            art=db.execute("SELECT title FROM artwork WHERE manager=? AND entity=? AND library_id=? AND season=?",key).fetchone()
            self._event(db,key,art["title"] if art else None,"uploaded",length)
            db.commit()
        return self.item(key)

    def item(self,key):
        with self.connect() as db:
            row = db.execute("SELECT * FROM artwork WHERE manager=? AND entity=? AND library_id=? AND season=?",key).fetchone()
        if row is None: raise KeyError("Artwork record not found.")
        return dict(row)

    def items(self, manager=None, missing=False, limit=100, offset=0):
        if manager not in (None,"series","movie"): raise ValueError("Invalid manager.")
        if type(limit)!=int or not (1<=limit<=200) or type(offset)!=int or not (0<=offset<=1000000):
            raise ValueError("Invalid pagination.")
        sql="SELECT * FROM artwork WHERE (? IS NULL OR manager=?) AND (?=0 OR sha256 IS NULL) ORDER BY manager,entity,library_id,season LIMIT ? OFFSET ?"
        with self.connect() as db:
            return [dict(x) for x in db.execute(sql,(manager,manager,1 if missing else 0,limit,offset))]

    def page(self, manager=None, entity="all", status="all", search="", limit=50, offset=0):
        if manager not in (None,"series","movie"):
            raise ValueError("Invalid manager.")
        if entity not in ("all","series","season","movie"):
            raise ValueError("Invalid artwork entity filter.")
        if status not in ("all","cached","missing","failed","locked"):
            raise ValueError("Invalid artwork status filter.")
        if not isinstance(search,str) or len(search)>120 or any(ord(ch)<32 for ch in search):
            raise ValueError("Invalid artwork search text.")
        if type(limit) is not int or not (1<=limit<=100) or type(offset) is not int or not (0<=offset<=1000000):
            raise ValueError("Invalid artwork pagination.")
        clauses=[]; args=[]
        if manager is not None:
            clauses.append("manager=?");args.append(manager)
        if entity!="all":
            clauses.append("entity=?");args.append(entity)
        if status=="cached":
            clauses.append("sha256 IS NOT NULL")
        elif status=="missing":
            clauses.append("sha256 IS NULL")
        elif status=="failed":
            clauses.append("error IS NOT NULL")
        elif status=="locked":
            clauses.append("pinned=1")
        if search.strip():
            # Escaped parameters; '%' and '_' are literal search text, not
            # user-controlled SQL wildcards. Search is scoped to one manager.
            term=search.strip().replace("!","!!").replace("%","!%").replace("_","!_")
            clauses.append("""(COALESCE(title,'') LIKE ? ESCAPE '!' OR
                library_id LIKE ? ESCAPE '!' OR
                COALESCE(imdb_id,'') LIKE ? ESCAPE '!' OR
                COALESCE(tvdb_id,'') LIKE ? ESCAPE '!' OR
                COALESCE(tmdb_id,'') LIKE ? ESCAPE '!')""")
            args.extend(["%"+term+"%"]*5)
        where=" WHERE "+" AND ".join(clauses) if clauses else ""
        with self.connect() as db:
            total=db.execute("SELECT count(*) FROM artwork"+where,args).fetchone()[0]
            rows=db.execute("""SELECT * FROM artwork"""+where+
                """ ORDER BY (title IS NULL),title COLLATE NOCASE,entity,library_id,season LIMIT ? OFFSET ?""",
                (*args,limit,offset)).fetchall()
        return {"items":[dict(row) for row in rows],"total":total,"offset":offset,
                "limit":limit,"hasMore":offset+len(rows)<total}

    def _event(self, db, key, title, outcome, length=0, duration_ms=0, attempts=0, detail=None):
        # No user credentials, provider request URL or raw network exception
        # text is persisted in the activity feed. Retain only bounded history.
        db.execute("""INSERT INTO events(at,manager,entity,library_id,season,title,
                    outcome,bytes,duration_ms,attempts,detail)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                   (now(),*key,title,outcome,int(length),int(duration_ms),int(attempts),detail))
        db.execute("""DELETE FROM events WHERE id <=
                      (SELECT MAX(id)-200 FROM events)""")

    def activity(self, manager=None, limit=12):
        if manager not in (None,"series","movie"):
            raise ValueError("Invalid activity manager.")
        if type(limit) is not int or not (1<=limit<=25):
            raise ValueError("Invalid activity history limit.")
        with self.lock,self.connect() as db:
            active=[dict(row) for row in db.execute("""
                SELECT j.manager,j.entity,j.library_id,j.season,j.attempts,
                       j.started_at,a.title
                FROM jobs j JOIN artwork a USING(manager,entity,library_id,season)
                WHERE j.state='running' AND (? IS NULL OR j.manager=?)
                ORDER BY j.started_at DESC LIMIT 2""",(manager,manager))]
            counts=[dict(row) for row in db.execute("""
                SELECT manager,state,COUNT(*) count FROM jobs
                WHERE (? IS NULL OR manager=?)
                GROUP BY manager,state""",(manager,manager))]
            recent=[dict(row) for row in db.execute("""
                SELECT id,at,manager,entity,library_id,season,title,outcome,
                       bytes,duration_ms,attempts,detail
                FROM events WHERE (? IS NULL OR manager=?)
                ORDER BY id DESC LIMIT ?""",(manager,manager,limit))]
        return {"active":active,"jobs":counts,"recent":recent,"paused":self.paused()}

    def poster(self,key):
        entry=self.item(key)
        digest,mime=entry["sha256"],entry["mime"]
        if not digest or not mime or not SHA_RE.fullmatch(digest):
            raise KeyError("Poster unavailable.")
        path=self._blob(digest,mime)
        if not path.is_file(): raise KeyError("Poster file missing.")
        return path.read_bytes(),mime

    def stats(self):
        with self.connect() as db:
            counts={k:[dict(x) for x in db.execute("""SELECT manager,
              COUNT(*) total, SUM(CASE WHEN sha256 IS NOT NULL THEN 1 ELSE 0 END) cached,
              SUM(CASE WHEN sha256 IS NULL THEN 1 ELSE 0 END) missing,
              SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) failed,
              SUM(pinned) pinned FROM artwork GROUP BY manager""")] for k in ("byManager",)}
            jobs=[dict(x) for x in db.execute("SELECT state,COUNT(*) count FROM jobs GROUP BY state")]
        return {"installed":True,"healthy":True,"version":VERSION,"paused":self.paused(),
                "byManager":counts["byManager"],"jobs":jobs,
                "storageBytes":sum(f.stat().st_size for f in self.blobs.glob("*/*") if f.is_file())}

    def process_next(self):
        if self.paused(): return False
        with self.lock,self.connect() as db:
            row=db.execute("""SELECT j.manager,j.entity,j.library_id,j.season,j.attempts,a.source_url,a.pinned
             FROM jobs j JOIN artwork a USING(manager,entity,library_id,season)
             WHERE j.state IN ('pending','retry') AND j.due<=? ORDER BY j.due LIMIT 1""",(time.time(),)).fetchone()
            if row is None: return False
            key=tuple(row[k] for k in KEYS)
            db.execute("UPDATE jobs SET state='running',started_at=? WHERE manager=? AND entity=? AND library_id=? AND season=?", (time.time(),*key))
            db.commit()
        started=time.monotonic()
        try:
            if row["pinned"] or not row["source_url"]:
                raise ValueError("Poster is locked or missing its source.")
            data=self.downloader(row["source_url"])
            digest,mime,length=self._save_blob(data)
            with self.lock,self.connect() as db:
                # Never replace manual artwork, even if it was uploaded while a job was running.
                updated=db.execute("""UPDATE artwork SET sha256=?,mime=?,bytes=?,updated=?,error=NULL
                   WHERE manager=? AND entity=? AND library_id=? AND season=? AND pinned=0""",
                   (digest,mime,length,now(),*key))
                db.execute("DELETE FROM jobs WHERE manager=? AND entity=? AND library_id=? AND season=?",key)
                if updated.rowcount:
                    # A concurrent manual upload may have pinned this image:
                    # never falsely report the discarded download as saved.
                    title=db.execute("SELECT title FROM artwork WHERE manager=? AND entity=? AND library_id=? AND season=?",key).fetchone()
                    self._event(db,key,title["title"] if title else None,"downloaded",
                                length,round((time.monotonic()-started)*1000),row["attempts"]+1)
                db.commit()
        except Exception as exc:
            attempts=row["attempts"]+1
            detail=str(exc)[:240] or type(exc).__name__
            with self.lock,self.connect() as db:
                updated=db.execute("""UPDATE jobs SET attempts=?,state=?,due=?,started_at=NULL
                  WHERE manager=? AND entity=? AND library_id=? AND season=? AND state='running'""",
                  (attempts,"failed" if attempts>=4 else "retry",
                   time.time()+min(3600,30*2**attempts),*key))
                if updated.rowcount:
                    db.execute("""UPDATE artwork SET error=? WHERE manager=? AND entity=?
                        AND library_id=? AND season=? AND pinned=0""",(detail,*key))
                    title=db.execute("SELECT title FROM artwork WHERE manager=? AND entity=? AND library_id=? AND season=?",key).fetchone()
                    # Only emit exception category. A manual upload that took
                    # ownership meanwhile must not acquire spurious errors.
                    self._event(db,key,title["title"] if title else None,
                                "failed" if attempts>=4 else "retry",0,
                                round((time.monotonic()-started)*1000),attempts,
                                type(exc).__name__)
                db.commit()
        return True

    def work_forever(self, stop):
        while not stop.wait(.8):
            try: self.process_next()
            except Exception: pass

class Handler(BaseHTTPRequestHandler):
    store:Store
    secret:str
    def log_message(self,*args): pass
    def send_json(self,code,obj):
        body=json.dumps(obj,separators=(",",":")).encode()
        self.send_response(code); self.send_header("Content-Type","application/json; charset=utf-8")
        self.send_header("Cache-Control","no-store"); self.send_header("Content-Length",str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def authorized(self):
        import hmac
        supplied=self.headers.get("X-MVArtwork-Key","")
        return bool(self.secret) and hmac.compare_digest(supplied,self.secret)
    def do_GET(self):
        if self.path=="/health": return self.send_json(200,{"status":"ok","service":"mvartwork","version":VERSION})
        if not self.authorized(): return self.send_json(401,{"error":"Unauthorized"})
        p=urlsplit(self.path); path=p.path
        try:
            if path=="/v1/status": return self.send_json(200,self.store.stats())
            if path=="/v1/activity":
                q=parse_qs(p.query)
                return self.send_json(200,self.store.activity(
                    manager=q.get("manager",[None])[0],
                    limit=int(q.get("limit",["12"])[0])))
            if path=="/v1/items":
                q=parse_qs(p.query); manager=q.get("manager",[None])[0]
                status=q.get("status",["missing" if q.get("missing",["0"])[0]=="1" else "all"])[0]
                return self.send_json(200,self.store.page(
                    manager=manager,entity=q.get("entity",["all"])[0],status=status,
                    search=q.get("search",[""])[0],
                    limit=int(q.get("limit",["50"])[0]),
                    offset=int(q.get("offset",["0"])[0])))
            if path.startswith("/v1/image/"):
                key=self.path_key(path,"/v1/image/")
                body,mime=self.store.poster(key)
                self.send_response(200); self.send_header("Content-Type",mime)
                self.send_header("Cache-Control","private,max-age=600")
                self.send_header("Content-Length",str(len(body))); self.end_headers()
                return self.wfile.write(body)
            return self.send_json(404,{"error":"Unknown route"})
        except (ValueError,KeyError) as exc:
            return self.send_json(404 if isinstance(exc,KeyError) else 400,{"error":str(exc)})
    def path_key(self,path,prefix):
        segments=[unquote(x) for x in path[len(prefix):].split("/")]
        if len(segments)!=4: raise ValueError("Expected manager/entity/local-ID/season.")
        return validated_key(dict(zip(KEYS,(segments[0],segments[1],segments[2],int(segments[3])))))
    def do_POST(self):
        if not self.authorized(): return self.send_json(401,{"error":"Unauthorized"})
        length=self.headers.get("Content-Length")
        if not length or not length.isdigit() or int(length)>MAX_JSON:
            return self.send_json(413,{"error":"Request too large."})
        try: body=json.loads(self.rfile.read(int(length)))
        except (ValueError,UnicodeDecodeError): return self.send_json(400,{"error":"Invalid JSON."})
        if not isinstance(body,dict): return self.send_json(400,{"error":"Expected object."})
        try:
            if self.path=="/v1/items": return self.send_json(200,self.store.register(body))
            if self.path=="/v1/upload": return self.send_json(200,self.store.upload(body))
            if self.path=="/v1/refresh": return self.send_json(200,self.store.enqueue(body))
            if self.path=="/v1/pause":
                self.store.set_paused(True); return self.send_json(200,self.store.stats())
            if self.path=="/v1/resume":
                self.store.set_paused(False); return self.send_json(200,self.store.stats())
            if self.path=="/v1/scan":
                with self.store.lock,self.store.connect() as db:
                    db.execute("""INSERT INTO jobs(manager,entity,library_id,season,due,state,attempts)
                     SELECT manager,entity,library_id,season,0,'pending',0 FROM artwork
                     WHERE sha256 IS NULL AND source_url IS NOT NULL AND pinned=0
                     ON CONFLICT(manager,entity,library_id,season) DO UPDATE SET due=0,state='pending',attempts=0""")
                    db.commit()
                return self.send_json(200,self.store.stats())
            return self.send_json(404,{"error":"Unknown route"})
        except (ValueError,KeyError) as exc:
            return self.send_json(404 if isinstance(exc,KeyError) else 400,{"error":str(exc)})

def serve(folder,secret,port=9494):
    if not secret or len(secret)<24:
        raise ValueError("mvartwork requires a private shared token.")
    store=Store(folder)
    handler=type("ArtworkHandler",(Handler,),{"store":store,"secret":secret})
    stop=threading.Event()
    thread=threading.Thread(target=store.work_forever,args=(stop,),daemon=True)
    thread.start()
    httpd=ThreadingHTTPServer(("127.0.0.1",port),handler)
    try: httpd.serve_forever(poll_interval=.2)
    finally: stop.set();httpd.server_close();thread.join(timeout=3)
