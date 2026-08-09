import pathlib
import sys

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# 让打真实供应商的测试能自动拿到 key（环境变量仍然优先）
from cowork.config import load_env  # noqa: E402

load_env(pathlib.Path(__file__).resolve().parents[1] / ".env")
