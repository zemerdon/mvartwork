import hashlib
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
import tempfile
from unittest.mock import patch, Mock
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
                                  lambda *args:calls.append(args)), patch.object(
                                  addon,"wait_for_service_health",return_value=None):
                addon.install(bundle,digest)
                self.assertTrue((install_root/"current"/"mvartwork"/"service.py").is_file())
                self.assertTrue((install_root/"current"/"deploy"/"install.py").is_file())
                self.assertTrue(unit.is_file())
                # Reproduce the real LXC: root updater UMask 0077 must not
                # leave any source directory inaccessible to service group.
                for directory in (
                    install_root,install_root/"releases",
                    (install_root/"current").resolve(),
                    install_root/"current"/"mvartwork",
                    install_root/"current"/"deploy",
                ):
                    self.assertEqual(directory.stat().st_mode & 0o777,0o750)
                self.assertEqual((install_root/"current"/"mvartwork"/"service.py").stat().st_mode & 0o777,0o644)
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


class NativeInstallerHealthRegression(unittest.TestCase):
    def test_installer_health_check_rejects_transient_systemctl_active(self):
        calls=[]
        def active(*command):
            calls.append(command)
            return None
        class FakeResponse:
            status=200
            def __enter__(self):return self
            def __exit__(self,*args):return None
            def read(self,size):return b'{"status":"ok"}'
        fake_opener=Mock()
        fake_opener.open.return_value=FakeResponse()
        with patch.object(addon,"run",side_effect=active),patch.object(
                addon.urllib.request,"build_opener",return_value=fake_opener),patch.object(
                addon.time,"sleep",return_value=None):
            addon.wait_for_service_health(max_attempts=2)
        self.assertEqual(len(calls),2)
        self.assertEqual(fake_opener.open.call_count,2)

    def test_service_start_failure_never_reports_installed_running(self):
        attempts=0
        def briefly_active(*command):
            nonlocal attempts
            attempts+=1
            if attempts >= 2:
                raise subprocess.CalledProcessError(3,command)
        fake_opener=Mock()
        response=Mock()
        response.status=200
        response.read.return_value=b'{"status":"ok"}'
        response.__enter__=Mock(return_value=response)
        response.__exit__=Mock(return_value=False)
        fake_opener.open.return_value=response
        with patch.object(addon,"run",side_effect=briefly_active),patch.object(
                addon.urllib.request,"build_opener",return_value=fake_opener),patch.object(
                addon.time,"sleep",return_value=None):
            with self.assertRaisesRegex(RuntimeError,"failed service and HTTP health"):
                addon.wait_for_service_health(max_attempts=4)

    def test_unreadable_existing_release_is_normalized_for_service_group(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/"opt"/"mvartwork"
            releases=root/"releases"
            actual=releases/"existing"
            (actual/"mvartwork").mkdir(parents=True,mode=0o700)
            (actual/"deploy").mkdir(mode=0o700)
            for directory in (root,releases,actual,actual/"mvartwork",actual/"deploy"):
                directory.chmod(0o700)
            identity=pwd.getpwuid(os.getuid())
            with patch.object(addon,"APP_ROOT",root):
                addon.ensure_release_readable(actual,identity)
            for directory in (root,releases,actual,actual/"mvartwork",actual/"deploy"):
                self.assertEqual(directory.stat().st_mode & 0o777,0o750)

    def test_symlink_release_is_not_accepted_for_permission_repair(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/"opt"/"mvartwork"
            releases=root/"releases"
            releases.mkdir(parents=True)
            releases.joinpath("real").mkdir()
            releases.joinpath("redirect").symlink_to(releases/"real")
            with patch.object(addon,"APP_ROOT",root):
                with self.assertRaisesRegex(RuntimeError,"Unsafe"):
                    addon.ensure_release_readable(releases/"redirect",pwd.getpwuid(os.getuid()))
