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
