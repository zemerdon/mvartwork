#!/usr/bin/env python3
"""Deterministic versioned addon bundle builder; does not publish releases."""
from pathlib import Path
import gzip
import hashlib
import io
import json
import tarfile
import sys
root = Path(__file__).resolve().parents[1]
paths = [Path("mvartwork/__init__.py"),Path("mvartwork/__main__.py"),
         Path("mvartwork/service.py"),Path("deploy/install.py"),Path("README.md")]
out = Path(sys.argv[1]) if len(sys.argv)>1 else root/"dist"
out.mkdir(parents=True,exist_ok=True)
bundle=out/"mvartwork-0.1.2.tar.gz"
with bundle.open("wb") as raw, gzip.GzipFile(fileobj=raw,mode="wb",mtime=0,filename="") as gz:
    with tarfile.open(fileobj=gz,mode="w") as tar:
        for rel in paths:
            data=(root/rel).read_bytes()
            info=tarfile.TarInfo(rel.as_posix())
            info.size=len(data);info.mtime=0;info.mode=0o644
            info.uid=info.gid=0;info.uname=info.gname=""
            tar.addfile(info,io.BytesIO(data))
digest=hashlib.sha256(bundle.read_bytes()).hexdigest()
manifest={"name":"mvartwork","version":"0.1.2","file":bundle.name,
          "sha256":digest,"size":bundle.stat().st_size,
          "release":True}
(out/"mvartwork-manifest.json").write_text(json.dumps(manifest,indent=2)+"\n")
print("Local-only addon bundle:",bundle.name,"SHA256:",digest)
