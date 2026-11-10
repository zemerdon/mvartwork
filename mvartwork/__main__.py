"""Run mvartwork locally; no public listener."""
import os
from pathlib import Path
from .service import serve
if __name__ == "__main__":
    secret = Path(os.environ.get("MVARTWORK_TOKEN_FILE",
                   "/var/lib/series-vision-metadata/mvartwork-token")).read_text().strip()
    serve(os.environ.get("MVARTWORK_DATA_DIR","/var/lib/mvartwork"),secret,
          int(os.environ.get("MVARTWORK_PORT","9494")))
