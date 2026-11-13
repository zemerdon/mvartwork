import hashlib
import json
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
import unittest
from deploy.install import verified_files

ROOT=Path(__file__).resolve().parents[1]

class PackageTests(unittest.TestCase):
    def test_reproducible_bundle_exact_checksum(self):
        with tempfile.TemporaryDirectory() as a,tempfile.TemporaryDirectory() as b:
            subprocess.run([sys.executable,str(ROOT/"deploy"/"package.py"),a],check=True,capture_output=True)
            subprocess.run([sys.executable,str(ROOT/"deploy"/"package.py"),b],check=True,capture_output=True)
            left=next(Path(a).glob("*.tar.gz"))
            right=next(Path(b).glob("*.tar.gz"))
            self.assertEqual(left.read_bytes(),right.read_bytes())
            manifest=json.loads((Path(a)/"mvartwork-manifest.json").read_text())
            self.assertEqual(manifest["sha256"],hashlib.sha256(left.read_bytes()).hexdigest())
            self.assertEqual(manifest["version"],"0.1.4")
            self.assertTrue(manifest["release"])
            self.assertEqual(left.name,"mvartwork-0.1.4.tar.gz")
            self.assertEqual(len(verified_files(left,manifest["sha256"])),6)
            with self.assertRaisesRegex(ValueError,"SHA256 mismatch"):
                verified_files(left,"0"*64)

if __name__=="__main__": unittest.main()
