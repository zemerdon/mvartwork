"""Disposable full backup/restore contract and HTTP authorization tests."""
import base64
from http.server import ThreadingHTTPServer
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
from urllib.request import Request,build_opener,ProxyHandler
import zipfile

from mvartwork.service import Handler,Store
from mvartwork.backup import create_backup,merge_backup

PNG=b"\x89PNG\r\n\x1a\nfixture-artwork"
WEBP=b"RIFF"+(22).to_bytes(4,"little")+b"WEBPVP8X"+b"fixturewebpdata"
URL="https://artworks.thetvdb.com/banners/posters/test.jpg"

class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.original=Store(Path(self.tmp.name)/"old",lambda _:PNG)
        self.target=Store(Path(self.tmp.name)/"new",lambda _:PNG)
        self.series={"manager":"series","entity":"series","library_id":"4",
                     "title":"Source Series","imdb_id":"tt1122334","source_url":URL}
        self.movie={"manager":"movie","entity":"movie","library_id":"4",
                    "title":"Source Movie","imdb_id":"tt9988776","source_url":URL}
        self.season={"manager":"series","entity":"season","library_id":"4",
                     "season":3,"title":"Source Series",
                     "imdb_id":"tt1122334","source_url":URL}
    def archive(self):
        file,size,counts=create_backup(self.original)
        self.addCleanup(file.close)
        self.assertEqual(size,len(file.read()))
        file.seek(0)
        p=Path(self.tmp.name)/("backup-"+str(len(list(Path(self.tmp.name).glob("backup-*"))))+".zip")
        p.write_bytes(file.read())
        return p,counts
    def populate(self):
        self.original.register(self.series)
        self.original.register(self.movie)
        self.original.register(self.season)
        self.original.process_next()
        self.original.upload({"manager":"movie","entity":"movie","library_id":"4",
             "base64":base64.b64encode(PNG).decode()})
    def test_portable_export_and_merge_preserves_series_movie_season_and_locks(self):
        self.populate()
        path,counts=self.archive()
        self.assertEqual(counts,{"series":2,"movie":1})
        with zipfile.ZipFile(path) as zf:
            self.assertEqual(zf.read("manifest.json")[:1],b"{")
            names=zf.namelist()
            self.assertIn("catalogue.sqlite3",names)
            self.assertTrue(any(x.startswith("blobs/") for x in names))
            self.assertFalse(any("token" in n or "secret" in n for n in names))
        result=merge_backup(self.target,path)
        self.assertEqual(result["mode"],"safe-merge")
        self.assertEqual(result["imported"],3)
        self.assertEqual(result["postersRestored"],2)
        self.assertEqual(self.target.item(("movie","movie","4",-1))["pinned"],1)
        self.assertEqual(self.target.item(("series","season","4",3))["title"],"Source Series")
        for key in (("movie","movie","4",-1),("series","series","4",-1)):
            self.assertEqual(self.target.poster(key)[0],PNG)
        repeat=merge_backup(self.target,path)
        self.assertEqual(repeat["imported"],0)
        self.assertEqual(repeat["existingPreserved"],3)
    def test_existing_manual_lock_and_cached_bytes_never_overwritten(self):
        self.populate()
        path,_=self.archive()
        self.target.upload({"manager":"movie","entity":"movie","library_id":"4",
             "base64":base64.b64encode(WEBP).decode()})
        original=self.target.item(("movie","movie","4",-1))
        result=merge_backup(self.target,path)
        current=self.target.item(("movie","movie","4",-1))
        self.assertEqual(current["sha256"],original["sha256"])
        self.assertEqual(current["pinned"],1)
        self.assertEqual(result["existingPreserved"],1)
    def test_mismatched_provider_identity_never_gets_old_poster(self):
        self.populate()
        path,_=self.archive()
        self.target.register({**self.movie,
             "imdb_id":"tt0000001","title":"Changed Movie"})
        result=merge_backup(self.target,path)
        self.assertEqual(result["conflictsSkipped"],1)
        self.assertIsNone(self.target.item(("movie","movie","4",-1))["sha256"])
    def test_import_rejects_corruption_path_traversal_and_missing_blobs_without_writes(self):
        self.populate()
        good,_=self.archive()
        original=good.read_bytes()
        bad=Path(self.tmp.name)/"corrupted.zip"
        bad.write_bytes(original[:-25]+b"bad-zip")
        with self.assertRaises((ValueError,zipfile.BadZipFile)):
            merge_backup(self.target,bad)
        malicious=Path(self.tmp.name)/"malicious.zip"
        with zipfile.ZipFile(good) as zf,zipfile.ZipFile(malicious,"w") as attack:
            for name in zf.namelist():attack.writestr(name,zf.read(name))
            attack.writestr("../escaped.txt",b"no")
        with self.assertRaises(ValueError):merge_backup(self.target,malicious)
        missing=Path(self.tmp.name)/"missing.zip"
        with zipfile.ZipFile(good) as zf,zipfile.ZipFile(missing,"w") as attack:
            for name in zf.namelist():
                if name.startswith("blobs/"):continue
                attack.writestr(name,zf.read(name))
        with self.assertRaises(ValueError):merge_backup(self.target,missing)
        self.assertEqual(self.target.page(manager="movie")["total"],0)
        self.assertEqual(self.target.page(manager="series")["total"],0)
    def test_import_rejects_tampered_content_and_bogus_identifiers(self):
        self.populate()
        good,_=self.archive()
        with zipfile.ZipFile(good) as zf:
            p=Path(self.tmp.name)/"tampered.zip"
            with zipfile.ZipFile(p,"w") as attack:
                for name in zf.namelist():
                    data=zf.read(name)
                    if name.startswith("blobs/"):data=data[:-2]+b"xx"
                    attack.writestr(name,data)
        with self.assertRaisesRegex(ValueError,"checksum"):
            merge_backup(self.target,p)
        self.assertEqual(self.target.page()["total"],0)

class BackupHTTPTests(BackupTests):
    def test_authenticated_download_and_merge_only_upload(self):
        self.populate()
        token="fixture-local-token-very-long"
        server=ThreadingHTTPServer(("127.0.0.1",0),
              type("TestHandler",(Handler,),{"store":self.original,"secret":token}))
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        self.addCleanup(lambda:(server.shutdown(),server.server_close(),thread.join(3)))
        opener=build_opener(ProxyHandler({}))
        url="http://127.0.0.1:"+str(server.server_port)
        with self.assertRaises(urllib.error.HTTPError) as exc:
            opener.open(url+"/v1/export")
        self.assertEqual(exc.exception.code,401)
        with opener.open(Request(url+"/v1/export",headers={"X-MVArtwork-Key":token}),timeout=10) as resp:
            self.assertEqual(resp.headers["Content-Type"],"application/zip")
            payload=resp.read()
        self.assertGreater(len(payload),100)
        missing=Request(url+"/v1/import",data=payload,
              headers={"X-MVArtwork-Key":token,"Content-Type":"application/zip"},
              method="POST")
        with self.assertRaises(urllib.error.HTTPError) as exc:
            opener.open(missing,timeout=10)
        self.assertEqual(exc.exception.code,400)
        req=Request(url+"/v1/import",data=payload,
              headers={"X-MVArtwork-Key":token,"Content-Type":"application/zip",
                       "X-MVArtwork-Import-Mode":"safe-merge"},method="POST")
        with opener.open(req,timeout=10) as resp:
            result=json.load(resp)
        self.assertEqual(result["mode"],"safe-merge")
        self.assertEqual(result["imported"],0)
        self.assertEqual(result["existingPreserved"],3)

if __name__=="__main__":unittest.main()
