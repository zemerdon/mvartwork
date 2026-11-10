#!/usr/bin/env python3
"""Root-only, exact-SHA-verified offline mvartwork installer.

No caller-provided HTTP URL is accepted. Installation changes only
/opt/mvartwork, /var/lib/mvartwork, its token, and mvartwork.service.
The MV API binary, IMDb datasets, MV app and media remain untouched.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile

APP_ROOT=Path("/opt/mvartwork")
DATA=Path("/var/lib/mvartwork")
TOKEN=Path("/var/lib/series-vision-metadata/mvartwork-token")
UNIT=Path("/etc/systemd/system/mvartwork.service")
SERVICE_USER="seriesvision-metadata"
FILES={"mvartwork/__init__.py","mvartwork/__main__.py",
       "mvartwork/service.py","deploy/install.py","README.md"}
SERVICE="""[Unit]
Description=Optional Media Vision Artwork Cache
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=seriesvision-metadata
Group=seriesvision-metadata
WorkingDirectory=/opt/mvartwork/current
Environment=PYTHONPATH=/opt/mvartwork/current
Environment=MVARTWORK_DATA_DIR=/var/lib/mvartwork
Environment=MVARTWORK_TOKEN_FILE=/var/lib/series-vision-metadata/mvartwork-token
ExecStart=/usr/bin/python3 -m mvartwork
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
ReadWritePaths=/var/lib/mvartwork
UMask=0077

[Install]
WantedBy=multi-user.target
"""

def verified_files(bundle, expected):
    if not isinstance(expected,str) or len(expected)!=64 or any(c not in "0123456789abcdef" for c in expected):
        raise ValueError("An exact pinned SHA256 is required.")
    if hashlib.sha256(bundle.read_bytes()).hexdigest()!=expected:
        raise ValueError("Addon bundle SHA256 mismatch.")
    entries={}
    with tarfile.open(bundle,"r:gz") as archive:
        for member in archive.getmembers():
            if member.name not in FILES or not member.isfile() or member.size>500000:
                raise ValueError("Unexpected or unsafe addon archive entry.")
            if member.name in entries: raise ValueError("Duplicate addon entry.")
            extracted=archive.extractfile(member)
            if extracted is None: raise ValueError("Missing addon content.")
            entries[member.name]=extracted.read()
    if set(entries)!=FILES: raise ValueError("Incomplete addon bundle.")
    return entries

def run(*args):
    subprocess.run(args,check=True,timeout=90)

def install(bundle,sha):
    entries=verified_files(bundle,sha)
    account=pwd.getpwnam(SERVICE_USER)
    release=APP_ROOT/"releases"/sha[:20]
    APP_ROOT.mkdir(parents=True,exist_ok=True)
    (APP_ROOT/"releases").mkdir(exist_ok=True)
    staging=Path(tempfile.mkdtemp(prefix="mvartwork-",dir=APP_ROOT))
    try:
        for name,data in entries.items():
            target=staging/name
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes(data);os.chmod(target,0o644)
        if not release.exists():
            os.rename(staging,release)
    finally:
        if staging.exists(): shutil.rmtree(staging)
    DATA.mkdir(parents=True,exist_ok=True)
    os.chown(DATA,account.pw_uid,account.pw_gid)
    if not TOKEN.exists():
        TOKEN.parent.mkdir(parents=True,exist_ok=True)
        fd=os.open(TOKEN,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(fd,"w") as f: f.write(secrets.token_urlsafe(48)+"\n")
    os.chmod(TOKEN,0o600)
    os.chown(TOKEN,account.pw_uid,account.pw_gid)
    link=APP_ROOT/"current"
    old=os.readlink(link) if link.is_symlink() else None
    next_link=APP_ROOT/"current.next"
    next_link.unlink(missing_ok=True)
    next_link.symlink_to(release)
    os.replace(next_link,link)
    UNIT.write_text(SERVICE);os.chmod(UNIT,0o644)
    try:
        run("systemctl","daemon-reload")
        run("systemctl","enable","--now","mvartwork.service")
        run("systemctl","restart","mvartwork.service")
        run("systemctl","is-active","--quiet","mvartwork.service")
    except Exception:
        if old is not None:
            next_link.symlink_to(old);os.replace(next_link,link)
            run("systemctl","restart","mvartwork.service")
        else:
            link.unlink(missing_ok=True)
            run("systemctl","stop","mvartwork.service")
        raise
    print("mvartwork installed, running, data retained at",DATA)

def uninstall(purge):
    run("systemctl","disable","--now","mvartwork.service") if UNIT.exists() else None
    UNIT.unlink(missing_ok=True)
    run("systemctl","daemon-reload")
    root=APP_ROOT.resolve()
    if root==Path("/opt/mvartwork") and root.is_dir():
        shutil.rmtree(root)
    if purge:
        if DATA.is_dir() and not DATA.is_symlink(): shutil.rmtree(DATA)
        TOKEN.unlink(missing_ok=True)
    print("mvartwork uninstalled; poster database", "DELETED" if purge else "RETAINED")

def main():
    cli=argparse.ArgumentParser()
    cli.add_argument("--bundle",type=Path)
    cli.add_argument("--sha256")
    cli.add_argument("--uninstall",action="store_true")
    cli.add_argument("--purge-data",action="store_true")
    args=cli.parse_args()
    if os.geteuid()!=0: cli.error("Root is required.")
    if args.uninstall:
        if args.bundle or args.sha256: cli.error("Uninstall does not accept bundles.")
        uninstall(args.purge_data)
    else:
        if args.purge_data or not args.bundle or not args.sha256: cli.error("Verified bundle and SHA256 required.")
        install(args.bundle,args.sha256)

if __name__=="__main__":main()
