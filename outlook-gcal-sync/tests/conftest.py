import os
import sys
import tempfile
from pathlib import Path

# The app reads DATA_DIR at import time; point it somewhere disposable.
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="ocsync-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
