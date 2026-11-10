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
