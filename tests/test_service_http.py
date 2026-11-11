import base64
import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from mvartwork.service import Handler, Store

PNG=b"\x89PNG\r\n\x1a\nlocal-test"
TOKEN="test-internal-token-1234567890abc"

class HttpFixture(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(self.tmp.name, lambda _:PNG)
        handler=type("LocalTestHandler",(Handler,),{"store":self.store,"secret":TOKEN})
        self.server=ThreadingHTTPServer(("127.0.0.1",0),handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=3)
        self.tmp.cleanup()
    def req(self,method,path,payload=None,token=TOKEN):
        conn=http.client.HTTPConnection("127.0.0.1",self.server.server_address[1],timeout=3)
        headers={"X-MVArtwork-Key":token}
        if payload is not None:
            payload=json.dumps(payload).encode()
            headers["Content-Type"]="application/json"
        conn.request(method,path,body=payload,headers=headers)
        res=conn.getresponse();body=res.read()
        out=(res.status,dict(res.getheaders()),body)
        conn.close()
        return out
    def test_http_registration_auth_separation_and_image(self):
        status,_,_=self.req("GET","/v1/status",token="wrong")
        self.assertEqual(status,401)
        status,_,body=self.req("GET","/health",token="")
        self.assertEqual(status,200)
        self.assertEqual(json.loads(body)["service"],"mvartwork")
        item={"manager":"series","entity":"season","library_id":"4","season":1,
              "source_url":"https://artworks.thetvdb.com/banners/a.jpg"}
        status,_,body=self.req("POST","/v1/items",item)
        self.assertEqual(status,200)
        self.assertEqual(json.loads(body)["season"],1)
        self.store.process_next()
        status,headers,body=self.req("GET","/v1/image/series/season/4/1")
        self.assertEqual(status,200)
        self.assertEqual(headers["Content-Type"],"image/png")
        self.assertEqual(body,PNG)
        status,_,body=self.req("GET","/v1/items?manager=series&limit=1")
        self.assertEqual(status,200)
        self.assertEqual(len(json.loads(body)["items"]),1)
        status,_,body=self.req("POST","/v1/pause",{})
        self.assertEqual(status,200)
        self.assertTrue(json.loads(body)["paused"])
        status,_,body=self.req("POST","/v1/resume",{})
        self.assertEqual(status,200)
        self.assertFalse(json.loads(body)["paused"])
    def test_paginated_http_filters_and_total(self):
        for i in range(4):
            status,_,body=self.req("POST","/v1/items",
                {"manager":"movie","entity":"movie","library_id":str(i+1),
                 "title":"Movie "+str(i+1)})
            self.assertEqual(status,200)
        status,_,body=self.req("GET","/v1/items?manager=movie&search=Movie&limit=2&offset=2")
        self.assertEqual(status,200)
        page=json.loads(body)
        self.assertEqual(page["total"],4)
        self.assertEqual(len(page["items"]),2)
        self.assertFalse(page["hasMore"])
        status,_,body=self.req("GET","/v1/items?manager=movie&status=unknown")
        self.assertEqual(status,400)
        status,_,body=self.req("GET","/v1/items?manager=movie&search=%25")
        self.assertEqual(status,200)
        self.assertEqual(json.loads(body)["total"],0)

    def test_activity_requires_private_token_and_lists_recent_per_manager(self):
        item={"manager":"series","entity":"series","library_id":"47",
              "title":"Test Show","source_url":"https://artworks.thetvdb.com/banners/x.jpg"}
        self.assertEqual(self.req("POST","/v1/items",item)[0],200)
        self.assertEqual(self.req("GET","/v1/activity",token="invalid")[0],401)
        self.store.process_next()
        status,_,payload=self.req("GET","/v1/activity?manager=series&limit=4")
        self.assertEqual(status,200)
        body=json.loads(payload)
        self.assertEqual(len(body["recent"]),1)
        self.assertEqual(body["recent"][0]["title"],"Test Show")
        self.assertEqual(body["recent"][0]["outcome"],"downloaded")
        self.assertEqual(self.req("GET","/v1/activity?manager=unknown")[0],400)
        self.assertEqual(self.req("GET","/v1/activity?limit=500")[0],400)
        self.assertEqual(json.loads(self.req("GET","/v1/activity?manager=movie")[2])["recent"],[])

    def test_bulk_title_backfill_private_api_requires_auth_and_preserves_local_id(self):
        status,_,_=self.req("POST","/v1/items",
            {"manager":"series","entity":"season","season":0,"library_id":"1002"})
        self.assertEqual(status,200)
        payload={"manager":"series","titles":[{"library_id":"1002","title":"The Show"}]}
        self.assertEqual(self.req("POST","/v1/titles",payload,token="wrong")[0],401)
        status,_,body=self.req("POST","/v1/titles",payload)
        self.assertEqual(status,200)
        self.assertEqual(json.loads(body)["updated"],1)
        status,_,body=self.req("GET","/v1/items?manager=series&search=The%20Show")
        self.assertEqual(status,200)
        self.assertEqual(json.loads(body)["items"][0]["title"],"The Show")
        self.assertEqual(self.req("POST","/v1/titles",{"manager":"movie","titles":[]})[0],400)

    def test_upload_and_invalid_cross_manager(self):
        status,_,body=self.req("POST","/v1/upload",{
            "manager":"movie","entity":"movie","library_id":"19",
            "base64":base64.b64encode(PNG).decode()})
        self.assertEqual(status,200)
        self.assertEqual(json.loads(body)["pinned"],1)
        status,_,body=self.req("POST","/v1/items",{
            "manager":"movie","entity":"series","library_id":"19"})
        self.assertEqual(status,400)
        self.assertIn("independently scoped",json.loads(body)["error"])
