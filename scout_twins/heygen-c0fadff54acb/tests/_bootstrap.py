"""Make the repository's ``src`` package visible to isolated test discovery."""

from pathlib import Path
import sys


SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
source = str(SOURCE_ROOT)
if source not in sys.path:
    sys.path.insert(0, source)
