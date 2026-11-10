import hashlib
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
import tempfile
from unittest.mock import patch
import unittest
from deploy import install as addon

ROOT=Path(__file__).resolve().parents[1]

class OfflineInstallTests(unittest.TestCase):
    def test_install_retains_data_then_explicitly_purges(self):
        with tempfile.TemporaryDirectory() as root:
            base=Path(root)
            stage=base/"build"
            subprocess.run([sys.executable,str(ROOT/"deploy"/"package.py"),str(stage)],check=True,capture_output=True)
            bundle=next(stage.glob("*.tar.gz"))
            digest=hashlib.sha256(bundle.read_bytes()).hexdigest()
            install_root=base/"opt"/"mvartwork"
            data=base/"var"/"lib"/"mvartwork"
            token=base/"metadata"/"mvartwork-token"
            unit=base/"units"/"mvartwork.service"
            unit.parent.mkdir(parents=True)
            identity=pwd.getpwuid(os.getuid()).pw_name
            calls=[]
            with patch.multiple(addon,APP_ROOT=install_root,DATA=data,TOKEN=token,
                                UNIT=unit,SERVICE_USER=identity), patch.object(addon,"run",
                                  lambda *args:calls.append(args)):
                addon.install(bundle,digest)
                self.assertTrue((install_root/"current"/"mvartwork"/"service.py").is_file())
                self.assertTrue((install_root/"current"/"deploy"/"install.py").is_file())
                self.assertTrue(unit.is_file())
                self.assertGreater(len(token.read_text().strip()),24)
                (data/"sentinel.txt").write_text("poster cache must persist")
                previous=token.read_text()
                addon.install(bundle,digest)
                self.assertEqual(token.read_text(),previous)
                addon.uninstall(False)
                self.assertTrue((data/"sentinel.txt").is_file())
                self.assertTrue(token.exists())
                self.assertFalse(unit.exists())
                addon.uninstall(True)
                self.assertFalse(data.exists())
                self.assertFalse(token.exists())
            self.assertTrue(any(cmd[:2]==("systemctl","restart") for cmd in calls))
