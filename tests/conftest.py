import pathlib
import sys

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from cowork.config import load_env  # noqa: E402

load_env(pathlib.Path(__file__).resolve().parents[1] / ".env")
