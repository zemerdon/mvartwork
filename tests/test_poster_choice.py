import hashlib
import tempfile
import unittest
from mvartwork.service import Store, safe_url

PNG=b"\x89PNG\r\n\x1a\nselected-poster"
PNG2=b"\x89PNG\r\n\x1a\nother-poster"
URL="https://artworks.thetvdb.com/banners/posters/test.jpg"
URL2="https://image.tmdb.org/t/p/original/poster.png"


class PosterSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.calls=[]
        self.store=Store(self.temp.name,lambda url:self.calls.append(url) or PNG)
        self.old={"manager":"series","entity":"series","library_id":"1489",
                  "title":"Example Series","imdb_id":"tt1234567","tvdb_id":"500",
                  "source_url":URL}
        self.new={"manager":"series","entity":"series","library_id":"1718",
                  "title":"Example Series","imdb_id":"tt1234567"}
        self.store.register(self.old)
        self.assertTrue(self.store.process_next())

    def test_readd_imdb_auto_reuses_exact_id_without_second_download(self):
        old_hash=self.store.item(("series","series","1489",-1))["sha256"]
        self.store.register(self.new)
        target=self.store.item(("series","series","1718",-1))
        self.assertEqual(target["sha256"],old_hash)
        self.assertIsNone(target["source_url"])
        self.assertEqual(self.calls,[URL])
        self.assertFalse(self.store.process_next())

    def test_upgrade_reuses_already_registered_imdb_match_without_rescan(self):
        self.store.register({**self.new,"library_id":"1724","imdb_id":None})
        import sqlite3
        db=sqlite3.connect(self.temp.name+"/artwork.sqlite3")
        db.execute("UPDATE artwork SET imdb_id=? WHERE manager='series' AND library_id='1724'",
                   ("tt1234567",))
        db.commit()
        db.close()
        restored=Store(self.temp.name,self.store.downloader)
        self.assertEqual(restored.item(("series","series","1724",-1))["sha256"],
                         restored.item(("series","series","1489",-1))["sha256"])
        self.assertFalse(restored.process_next())
        self.assertEqual(self.calls,[URL])

    def test_title_only_does_not_trigger_unsafe_auto_match(self):
        self.store.register({**self.new,"library_id":"1719","imdb_id":None})
        self.assertIsNone(self.store.item(("series","series","1719",-1))["sha256"])
        self.store.register({**self.new,"library_id":"1720","imdb_id":"tt7654321"})
        self.assertIsNone(self.store.item(("series","series","1720",-1))["sha256"])

    def test_choose_cached_by_title_after_tvdb_to_imdb_readd(self):
        self.store.register({**self.new,"imdb_id":"tt7654321"})
        choices=self.store.candidates("series","series",search="Example")
        self.assertEqual(len(choices["items"]),1)
        target=self.store.choose_cached({
          "manager":"series","entity":"series","library_id":"1718",
          "source_manager":"series","source_entity":"series","source_library_id":"1489"})
        self.assertEqual(target["sha256"],self.store.item(("series","series","1489",-1))["sha256"])
        self.assertEqual(target["pinned"],1)
        self.assertEqual(len(self.calls),1)
        with self.assertRaisesRegex(ValueError,"locked"):
            self.store.enqueue(self.new)
        self.store.register({**self.new,"source_url":URL2})
        self.assertEqual(self.store.item(("series","series","1718",-1))["sha256"],target["sha256"])

    def test_copy_is_manager_and_season_isolated_and_requires_existing_target(self):
        self.store.register({"manager":"movie","entity":"movie","library_id":"20"})
        self.store.register({"manager":"series","entity":"season","library_id":"1718","season":1})
        for destination in ({"manager":"movie","entity":"movie","library_id":"20"},
                            {"manager":"series","entity":"season","library_id":"1718","season":1},
                            {"manager":"series","entity":"series","library_id":"9999"}):
            with self.assertRaises((ValueError,KeyError)):
                self.store.choose_cached({**destination,"source_manager":"series",
                    "source_entity":"series","source_library_id":"1489"})

    def test_download_selected_approved_url_locks_and_retains_on_failure(self):
        self.store.register({**self.new,"imdb_id":"tt7654321"})
        self.store.downloader=lambda url: self.calls.append(url) or PNG2
        result=self.store.download_selected({**self.new,"url":URL2})
        self.assertEqual(result["sha256"],hashlib.sha256(PNG2).hexdigest())
        self.assertEqual(result["source_url"],URL2)
        self.assertEqual(result["pinned"],1)
        self.assertEqual(self.calls[-1],URL2)
        self.store.downloader=lambda _: (_ for _ in ()).throw(OSError("Network offline"))
        with self.assertRaises(OSError):
            self.store.download_selected({**self.new,"url":URL})
        self.assertEqual(self.store.item(("series","series","1718",-1))["sha256"],result["sha256"])

    def test_unapproved_url_and_sql_wildcard_search_rejected(self):
        self.store.register({**self.new,"imdb_id":None})
        for url in ("https://127.0.0.1/private","http://image.tmdb.org/poster.jpg",
                    "https://image.tmdb.org.evil.net/p.jpg"):
            with self.assertRaises(ValueError):
                self.store.download_selected({**self.new,"url":url})
        self.assertEqual(self.store.candidates("series","series",search="%")["items"],[])
        with self.assertRaises(ValueError):
            self.store.candidates("series","series",search="a"*121)


if __name__=="__main__":
    unittest.main()
