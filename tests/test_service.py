import base64
import tempfile
import unittest
from pathlib import Path
from mvartwork.service import Store, safe_url, validated_key, image_type

PNG = b"\x89PNG\r\n\x1a\n" + b"test-fixture-data"
URL = "https://artworks.thetvdb.com/banners/posters/test.jpg"

class StoreTests(unittest.TestCase):
    def setUp(self):
        self.dir=tempfile.TemporaryDirectory()
        self.calls=[]
        self.store=Store(self.dir.name,lambda url:self.calls.append(url) or PNG)
        self.series={"manager":"series","entity":"series","library_id":"897",
                     "imdb_id":"tt112233","tvdb_id":"456","source_url":URL}
        self.movie={"manager":"movie","entity":"movie","library_id":"897",
                    "source_url":URL}
    def tearDown(self): self.dir.cleanup()

    def test_library_only_and_manager_isolation(self):
        self.store.register(self.series)
        self.store.register(self.movie)
        self.assertEqual(len(self.store.items("series")),1)
        self.assertEqual(len(self.store.items("movie")),1)
        self.store.process_next()
        self.assertEqual(self.store.item(("series","series","897",-1))["sha256"] is not None,True)
        self.assertIsNone(self.store.item(("movie","movie","897",-1))["sha256"])
        self.store.process_next()
        self.assertEqual(len(list((Path(self.dir.name)/"blobs").glob("*/*"))),1)

    def test_reconciliation_preserves_image(self):
        self.store.register(self.series);self.store.process_next()
        prior=self.store.item(("series","series","897",-1))["sha256"]
        self.store.register({"manager":"series","entity":"series","library_id":"897",
                             "imdb_id":"tt999999","source_url":URL})
        after=self.store.item(("series","series","897",-1))
        self.assertEqual(after["sha256"],prior)
        self.assertEqual(after["tvdb_id"],"456")
        self.assertEqual(after["imdb_id"],"tt999999")
        self.assertFalse(self.store.process_next())

    def test_manual_poster_lock(self):
        record=self.store.upload({"manager":"series","entity":"series","library_id":"99",
                                  "base64":base64.b64encode(PNG).decode()})
        self.assertEqual(record["pinned"],1)
        self.store.register({**self.series,"library_id":"99"})
        self.assertFalse(self.store.process_next())
        with self.assertRaisesRegex(ValueError,"locked"):
            self.store.enqueue(self.store.item(("series","series","99",-1)))

    def test_pause_resume_and_retries(self):
        self.store.register(self.series)
        self.store.set_paused(True)
        self.assertFalse(self.store.process_next())
        self.store.set_paused(False)
        self.assertTrue(self.store.process_next())
        self.assertEqual(self.calls,[URL])
        self.assertFalse(self.store.paused())

    def test_invalid_url_and_scoping_fail_closed(self):
        for url in ("http://artworks.thetvdb.com/x","https://127.0.0.1/image.jpg",
                    "https://artworks.thetvdb.com.evil.com/a.jpg","file:///etc/shadow",
                    "https://image.tmdb.org:444/a"):
            with self.assertRaises(ValueError): safe_url(url)
        with self.assertRaises(ValueError):
            validated_key({"manager":"movie","entity":"series","library_id":"1"})
        with self.assertRaises(ValueError):
            validated_key({"manager":"series","entity":"season","library_id":"1"})
        with self.assertRaises(ValueError): image_type(b"not a poster")

    def test_failed_refresh_retains_previous_image(self):
        self.store.register(self.series);self.store.process_next()
        key=("series","series","897",-1)
        old=self.store.item(key)["sha256"]
        self.store.downloader=lambda _: (_ for _ in ()).throw(OSError("offline"))
        self.store.enqueue(self.series);self.store.process_next()
        self.assertEqual(self.store.item(key)["sha256"],old)
        self.assertIn("offline",self.store.item(key)["error"])

    def test_season_does_not_overwrite_series(self):
        self.store.register(self.series)
        self.store.register({"manager":"series","entity":"season","library_id":"897","season":1,
                             "source_url":URL})
        self.assertEqual(len(self.store.items("series")),2)

if __name__=="__main__":unittest.main()

class PagedLibraryFixture(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store=Store(self.folder.name,lambda _:PNG)
        for n,name in enumerate(("Alpha & Omega","Beta: Episode","Gamma","Delta"),1):
            self.store.register({"manager":"series","entity":"series","library_id":str(n),
                                 "title":name,"source_url":URL})
        self.store.register({"manager":"series","entity":"season","library_id":"1",
                             "season":1,"title":"Alpha & Omega"})
        self.store.register({"manager":"movie","entity":"movie","library_id":"1",
                             "title":"Movie Alpha","imdb_id":"tt9876543"})

    def test_bounded_paged_browse_has_total_and_no_cross_manager_leakage(self):
        first=self.store.page(manager="series",limit=2)
        self.assertEqual(first["total"],5)
        self.assertEqual(len(first["items"]),2)
        self.assertTrue(first["hasMore"])
        nxt=self.store.page(manager="series",limit=2,offset=2)
        self.assertEqual(nxt["total"],5)
        self.assertEqual(len(nxt["items"]),2)
        last=self.store.page(manager="series",limit=2,offset=4)
        self.assertFalse(last["hasMore"])
        self.assertEqual(len(last["items"]),1)
        self.assertFalse(any(it["manager"]=="movie" for it in first["items"]+nxt["items"]+last["items"]))
        self.assertEqual(self.store.page(manager="movie")["total"],1)

    def test_title_and_provider_id_search_is_scoped_and_parameterized(self):
        data=self.store.page(manager="series",search="Alpha")
        self.assertEqual(data["total"],2)
        self.assertEqual(self.store.page(manager="movie",search="tt9876543")["total"],1)
        self.assertEqual(self.store.page(manager="series",search="tt9876543")["total"],0)
        self.assertEqual(self.store.page(manager="series",search="%' OR 1=1 --")["total"],0)
        self.assertEqual(self.store.page(manager="series",search="_")["total"],0)
        self.assertEqual(self.store.page(manager="series",entity="season")["total"],1)

    def test_status_filters_and_preserve_posters_on_reregistration(self):
        self.store.process_next()
        self.assertEqual(self.store.page(manager="series",status="cached")["total"],1)
        self.assertEqual(self.store.page(manager="series",status="missing")["total"],4)
        self.store.upload({"manager":"movie","entity":"movie","library_id":"1",
                           "base64":base64.b64encode(PNG).decode()})
        locked=self.store.page(manager="movie",status="locked")
        self.assertEqual(locked["total"],1)
        self.assertEqual(locked["items"][0]["title"],"Movie Alpha")
        original=self.store.item(("movie","movie","1",-1))["sha256"]
        self.store.register({"manager":"movie","entity":"movie","library_id":"1",
                             "title":"Updated Movie Alpha"})
        self.assertEqual(self.store.item(("movie","movie","1",-1))["sha256"],original)
        self.assertEqual(self.store.page(manager="movie",search="Updated Movie Alpha")["total"],1)

    def test_v010_database_is_upgraded_additively_and_poster_bytes_remain(self):
        self.store.register({"manager":"series","entity":"series","library_id":"30",
                             "title":"Survivor","source_url":URL})
        self.store.process_next()
        key=("series","series","30",-1)
        expected=self.store.item(key)["sha256"]
        import sqlite3
        # Model an old v0.1.0 DB by removing the new column in a disposable copy.
        self.store=None
        db=sqlite3.connect(self.folder.name+"/artwork.sqlite3")
        db.execute("ALTER TABLE artwork DROP COLUMN title")
        db.commit();db.close()
        reopened=Store(self.folder.name,lambda _:PNG)
        self.assertIsNone(reopened.item(key)["title"])
        self.assertEqual(reopened.item(key)["sha256"],expected)
        self.assertEqual(reopened.page(manager="series")["total"],6)

    def test_invalid_filter_or_search_is_rejected(self):
        for kw in ({"manager":"other"},{"status":"everything"},{"entity":"unrestricted"},
                   {"search":"a"*121},{"limit":10000},{"offset":-1}):
            with self.assertRaises(ValueError):
                self.store.page(**kw)

class LiveActivityFixture(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.started=__import__("threading").Event()
        self.complete=__import__("threading").Event()
        def download(_):
            self.started.set()
            if not self.complete.wait(5):
                raise TimeoutError("Blocked test download exceeded deadline.")
            return PNG
        self.store=Store(self.tmp.name,download)
        self.store.register({"manager":"series","entity":"season","library_id":"2",
                             "season":0,"title":"Example Show","source_url":URL})
        self.store.register({"manager":"movie","entity":"movie","library_id":"5",
                             "title":"Example Movie","source_url":URL})

    def test_live_active_download_and_per_manager_queue_are_reported(self):
        from threading import Thread
        worker=Thread(target=self.store.process_next)
        worker.start()
        try:
            self.assertTrue(self.started.wait(3))
            snapshot=self.store.activity()
            self.assertEqual(len(snapshot["active"]),1)
            self.assertEqual(snapshot["active"][0]["manager"],"series")
            self.assertEqual(snapshot["active"][0]["title"],"Example Show")
            self.assertGreater(snapshot["active"][0]["started_at"],0)
            counts={(row["manager"],row["state"]):row["count"] for row in snapshot["jobs"]}
            self.assertEqual(counts[("series","running")],1)
            self.assertEqual(counts[("movie","pending")],1)
            self.assertEqual(self.store.activity(manager="movie")["active"],[])
            self.assertEqual(self.store.activity(manager="movie")["recent"],[])
        finally:
            self.complete.set()
            worker.join(timeout=6)
        self.assertFalse(worker.is_alive())
        recent=self.store.activity(manager="series")["recent"]
        self.assertEqual(len(recent),1)
        self.assertEqual(recent[0]["outcome"],"downloaded")
        self.assertEqual(recent[0]["bytes"],len(PNG))
        self.assertEqual(recent[0]["title"],"Example Show")
        self.assertIsNone(recent[0]["detail"])
        self.assertNotIn("source_url",recent[0])
        self.assertNotIn("token",recent[0])

    def test_download_error_records_only_exception_category_not_secret_or_url(self):
        self.store.downloader=lambda _: (_ for _ in ()).throw(
            OSError("Do not expose https://artworks.thetvdb.com/secret?token=abc"))
        self.store.process_next()
        data=self.store.activity(manager="series")
        self.assertEqual(data["recent"][0]["outcome"],"retry")
        self.assertEqual(data["recent"][0]["attempts"],1)
        self.assertEqual(data["recent"][0]["detail"],"OSError")
        self.assertNotIn("token",str(data))
        self.assertNotIn("https://",str(data))
        self.assertEqual(self.store.activity(manager="movie")["recent"],[])

    def test_v011_db_migration_keeps_all_queued_jobs_and_existing_poster(self):
        self.complete.set()
        self.store.process_next()
        entry=self.store.item(("series","season","2",0))
        original_hash=entry["sha256"]
        import sqlite3
        db=sqlite3.connect(self.tmp.name+"/artwork.sqlite3")
        db.execute("DROP TABLE events")
        db.execute("ALTER TABLE jobs DROP COLUMN started_at")
        db.commit();db.close()
        reopened=Store(self.tmp.name,lambda _:PNG)
        self.assertEqual(reopened.item(("series","season","2",0))["sha256"],original_hash)
        self.assertEqual(reopened.activity()["recent"],[])
        self.assertEqual(reopened.activity()["jobs"][0]["manager"],"movie")

    def test_manual_upload_wins_against_inflight_download_and_activity_is_truthful(self):
        from threading import Thread
        worker=Thread(target=self.store.process_next)
        worker.start()
        try:
            self.assertTrue(self.started.wait(3))
            art=b"\x89PNG\r\n\x1a\nmanual-upload-replaces-network-data"
            saved=self.store.upload({"manager":"series","entity":"season",
                                     "library_id":"2","season":0,
                                     "base64":base64.b64encode(art).decode()})
            self.assertEqual(saved["pinned"],1)
        finally:
            self.complete.set()
            worker.join(timeout=6)
        self.assertFalse(worker.is_alive())
        after=self.store.item(("series","season","2",0))
        self.assertEqual(after["sha256"],saved["sha256"])
        outcomes=[ev["outcome"] for ev in self.store.activity(manager="series")["recent"]]
        self.assertEqual(outcomes,["uploaded"])

    def test_manual_upload_wins_against_inflight_network_failure(self):
        from threading import Thread
        def fail_download(_):
            self.started.set()
            self.complete.wait(5)
            raise OSError("private-uri-secret")
        self.store.downloader=fail_download
        worker=Thread(target=self.store.process_next)
        worker.start()
        try:
            self.assertTrue(self.started.wait(3))
            saved=self.store.upload({"manager":"series","entity":"season",
                "library_id":"2","season":0,
                "base64":base64.b64encode(PNG).decode()})
            self.assertEqual(saved["pinned"],1)
        finally:
            self.complete.set()
            worker.join(timeout=6)
        self.assertFalse(worker.is_alive())
        entry=self.store.item(("series","season","2",0))
        self.assertIsNone(entry["error"])
        self.assertEqual([ev["outcome"] for ev in
            self.store.activity(manager="series")["recent"]],["uploaded"])

    def test_bounded_event_log_and_invalid_activity_filter(self):
        self.complete.set()
        for n in range(215):
            self.store.register({"manager":"series","entity":"series","library_id":str(100+n),
                                 "source_url":URL})
            self.store.process_next()
        db_rows=__import__("sqlite3").connect(self.tmp.name+"/artwork.sqlite3").execute(
            "SELECT COUNT(*) FROM events").fetchone()[0]
        self.assertEqual(db_rows,200)
        self.assertLessEqual(len(self.store.activity(limit=25)["recent"]),25)
        for settings in ({"manager":"unknown"},{"limit":0},{"limit":26},{"limit":"3"}):
            with self.assertRaises(ValueError):
                self.store.activity(**settings)
