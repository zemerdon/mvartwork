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

    def test_scoped_sync_progress_and_stale_request_are_independent(self):
        import datetime
        start=datetime.datetime.now(datetime.timezone.utc).isoformat()
        initial=self.store.library_status()
        request=self.store.request_library_sync({"manager":"series"})
        self.assertEqual(request["requested"]["series"],1)
        self.assertEqual(request["requested"]["movie"],0)
        progress=self.store.publish_library_progress({
            "manager":"series","requestId":1,"runId":"a"*32,
            "state":"running","processed":1,"total":2,"failures":0,
            "stage":"Registering seasons","startedAt":start})
        self.assertEqual(progress["processed"],1)
        self.assertEqual(self.store.library_status()["progress"]["series"]["total"],2)
        self.assertEqual(self.store.library_status()["progress"]["movie"],{})
        self.register("series",17)
        self.assertEqual(self.snapshot("series",[17])["count"],1)
        self.assertEqual(self.store.library_status()["completed"]["series"],1)
        request=self.store.request_library_sync({"manager":"movie"})
        self.assertEqual(request["requested"]["series"],1)
        self.assertEqual(request["requested"]["movie"],2)
        with self.assertRaisesRegex(ValueError,"superseded"):
            self.store.receive_library_snapshot({
                "manager":"movie","requestId":1,"complete":True,
                "records":[{"entity":"movie","library_id":"4","season":-1}]})
        self.assertEqual(self.store.library_status()["completed"]["series"],1)

    def test_stop_sync_and_invalid_progress_preserve_library(self):
        import datetime
        start=datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.register("movie",4)
        self.store.request_library_sync({"manager":"movie"})
        self.store.publish_library_progress({
            "manager":"movie","requestId":1,"runId":"b"*32,
            "state":"running","processed":1,"total":5,"failures":0,
            "stage":"Registering movies","startedAt":start})
        stopped=self.store.stop_library_sync()
        self.assertEqual(stopped["stopId"],1)
        self.assertIsNotNone(stopped["stopAt"])
        self.assertIn("+00:00",stopped["stopAt"])
        self.assertEqual(stopped["progress"]["movie"]["state"],"stopping")
        self.assertEqual(len(self.store.items("movie")),1)
        with self.assertRaisesRegex(ValueError,"Invalid library synchronization progress"):
            self.store.publish_library_progress({
                "manager":"movie","requestId":1,"runId":"b"*32,
                "state":"running","processed":9,"total":5,"failures":0,
                "stage":"Registering movies","startedAt":start})

    def test_stopping_sync_rejects_late_snapshot_and_other_manager_still_completes(self):
        import datetime
        start=datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.store.request_library_sync()
        self.register("series",12)
        self.register("movie",7)
        self.store.publish_library_progress({
            "manager":"series","requestId":1,"runId":"c"*32,
            "state":"running","processed":1,"total":1,"failures":0,
            "stage":"Publishing Series snapshot","startedAt":start})
        self.store.stop_library_sync()
        with self.assertRaisesRegex(ValueError,"Cancelled sync"):
            self.store.receive_library_snapshot({
                "manager":"series","requestId":1,"complete":True,
                "records":[{"entity":"series","library_id":"12","season":-1}]})
        info=self.store.library_status()
        self.assertEqual(info["completed"]["series"],0)
        self.assertEqual(info["snapshots"]["series"],0)
        # A separately scoped subsequent request still works.
        scoped=self.store.request_library_sync({"manager":"movie"})
        self.assertEqual(scoped["requested"]["movie"],2)
        self.assertEqual(self.store.receive_library_snapshot({
            "manager":"movie","requestId":2,"complete":True,
            "records":[{"entity":"movie","library_id":"7","season":-1}]}),{"manager":"movie","count":1,"complete":True})
        self.assertEqual(self.store.library_status()["completed"]["movie"],2)

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
