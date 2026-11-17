"""Complete-library sync, orphan cleanup and fail-closed invariants."""
import base64
from pathlib import Path
import tempfile
import unittest
from mvartwork.service import Store

PNG=bytes.fromhex("89504e470d0a1a0a")+b"fixture-artwork"
class LibrarySyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store=Store(self.tmp.name)
    def register(self,manager,id,entity=None,season=-1):
        entity=entity or ("movie" if manager=="movie" else "series")
        return self.store.register({"manager":manager,"entity":entity,
                                   "library_id":str(id),"season":season})
    def snapshot(self,manager,ids):
        records=[{"entity":"movie" if manager=="movie" else "series",
                  "library_id":str(id),"season":-1} for id in ids]
        return self.store.receive_library_snapshot(
            {"manager":manager,"complete":True,"records":records})

    def test_opt_in_manual_request_auto_toggle_and_complete_snapshot(self):
        info=self.store.library_status()
        self.assertTrue(info["autoSync"])
        self.assertEqual(info["requestId"],0)
        self.store.set_auto_sync(False)
        self.assertFalse(self.store.library_status()["autoSync"])
        request=self.store.request_library_sync()
        self.assertEqual(request["requestId"],1)
        self.register("movie",4)
        self.assertEqual(self.snapshot("movie",[4])["count"],1)
        info=self.store.library_status()
        self.assertEqual(info["completed"]["movie"],1)
        self.assertIsNotNone(info["lastSynced"]["movie"])
        with self.assertRaises(ValueError):
            self.store.set_auto_sync("false")

    def test_snapshot_never_guesses_or_cleans_an_incomplete_collection(self):
        self.register("movie",4)
        for value in ({},{"manager":"movie","complete":True,"records":[]},
                      {"manager":"movie","complete":False,
                       "records":[{"entity":"movie","library_id":"4","season":-1}]},
                      {"manager":"movie","complete":True,"records":[
                       {"entity":"movie","library_id":"4","season":-1}]*2}):
            with self.assertRaises(ValueError):
                self.store.receive_library_snapshot(value)
        with self.assertRaisesRegex(ValueError,"Sync Library"):
            self.store.remove_stale_artwork()
        self.assertEqual(len(self.store.items("movie")),1)

    def test_explicit_no_confirm_cleanup_only_deletes_unreferenced_blobs(self):
        for id in (4,5,6):
            self.register("movie",id)
        self.store.upload({"manager":"movie","entity":"movie","library_id":"4",
          "base64":base64.b64encode(PNG).decode()})
        self.store.upload({"manager":"movie","entity":"movie","library_id":"5",
          "base64":base64.b64encode(PNG).decode()})
        self.snapshot("movie",[4,6])
        result=self.store.remove_stale_artwork()
        self.assertEqual(result["removedRecords"],1)
        self.assertEqual(result["removedBlobs"],0)
        self.assertEqual(len(self.store.items("movie")),2)
        self.assertEqual(self.store.item(("movie","movie","4",-1))["pinned"],1)
        self.snapshot("movie",[6])
        result=self.store.remove_stale_artwork()
        self.assertEqual(result["removedRecords"],1)
        self.assertEqual(result["removedBlobs"],1)
        self.assertEqual(len(self.store.items("movie")),1)

    def test_manager_and_season_scoping(self):
        self.register("series",10)
        self.register("series",10,"season",1)
        self.register("series",10,"season",2)
        self.register("movie",10)
        self.store.receive_library_snapshot({"manager":"series","complete":True,
           "records":[{"entity":"series","library_id":"10","season":-1},
                      {"entity":"season","library_id":"10","season":2}]})
        result=self.store.remove_stale_artwork()
        self.assertEqual(result["removedRecords"],1)
        self.assertEqual(len(self.store.items("movie")),1)
        self.assertEqual(self.store.item(("series","season","10",2))["season"],2)

    def test_stale_preview_requires_complete_independent_snapshots(self):
        self.register("movie",101)
        self.register("series",201)
        info=self.store.library_stale_preview()
        self.assertFalse(info["complete"])
        self.assertIsNone(info["byManager"]["movie"]["staleCount"])
        self.assertEqual(info["byManager"]["series"]["state"],"awaitingSnapshot")
        self.store.request_library_sync()
        self.snapshot("movie",[202])
        info=self.store.library_stale_preview()
        self.assertEqual(info["byManager"]["movie"]["staleCount"],1)
        self.assertEqual(info["byManager"]["movie"]["items"][0]["libraryId"],"101")
        self.assertEqual(info["byManager"]["series"]["state"],"awaitingSnapshot")
        self.assertFalse(info["complete"])
        self.snapshot("series",[201])
        info=self.store.library_stale_preview()
        self.assertTrue(info["complete"])
        self.assertEqual(info["staleTotal"],1)
        self.assertEqual(info["byManager"]["series"]["staleCount"],0)
        self.assertEqual(info["byManager"]["movie"]["staleCount"],1)

    def test_sync_delta_only_reports_database_membership(self):
        self.register("series",1)
        self.register("series",2)
        self.snapshot("series",[1,2])
        first=self.store.library_status()["changes"]["series"]
        self.assertEqual((first["added"],first["removed"],first["unchanged"]),(2,0,0))
        self.snapshot("series",[2,3])
        next=self.store.library_status()["changes"]["series"]
        self.assertEqual((next["added"],next["removed"],next["unchanged"]),(1,1,1))
        self.assertIsNone(self.store.library_stale_preview()["byManager"]["movie"]["staleCount"])

    def test_preview_does_not_remove_stale_and_reports_safety_guard(self):
        for id in range(50):self.register("movie",id)
        self.snapshot("movie",[0])
        preview=self.store.library_stale_preview()["byManager"]["movie"]
        self.assertEqual(preview["staleCount"],49)
        self.assertFalse(preview["cleanupGuardPassed"])
        self.assertEqual(len(preview["items"]),49)
        self.assertEqual(len(self.store.items("movie")),50)

    def test_mass_deletion_guard(self):
        for id in range(100):self.register("movie",id)
        self.snapshot("movie",[0])
        with self.assertRaisesRegex(ValueError,"safe cleanup"):
            self.store.remove_stale_artwork()
        self.assertEqual(len(self.store.items("movie")),100)

if __name__=="__main__":unittest.main()
