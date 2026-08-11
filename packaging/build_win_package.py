"""打 Windows 发布包：embeddable Python + 预构建前端 + 项目代码 → 一个解压即用的 zip。

用法（在仓库根目录）：

    python packaging/build_win_package.py

产出：dist/agent-cowork-win-<版本>.zip。解压后双击 start.bat 即用
（自动起 serve、开浏览器），不需要对方装 Python / node / 任何依赖，也无需联网。

原理与几个已知的坑（embeddable Python 的）：

- python.org 的 Windows embeddable 包不带 pip、不带证书、默认不加载
  site-packages。三个都要手动处理：
  1. 改 python311._pth：追加 Lib/site-packages 路径并取消 #import site 注释
     （不改的话，后面 pip 装进去的 fastapi 等全部 import 不到）。
  2. 下载 cacert.pem 并通过 SSL_CERT_FILE 喂给 pip——embeddable 没有系统证书库，
     直接跑 get-pip.py 会 SSL 验证失败。
  3. get-pip.py 装 pip，再用 pip 装依赖（全是 wheel，纯本机操作）。
- 项目代码不 pip install，直接拷 src/ 进包 + start.bat 里设 PYTHONPATH——
  项目是零必需依赖 + 全部延迟导入，拷目录就够，绕开在 embeddable 里跑 setuptools。
- ui/dist 需要 node 构建；构建产物打进包，对方机器不需要 node。
- start.bat 故意不写中文（bat 在中文 Windows 上的编码是历史雷区），提示语用英文。

这个脚本本身也只依赖标准库（urllib / zipfile / subprocess），延续项目的零依赖约定。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY_VERSION = "3.11.9"  # 3.11 系列最后一个 bugfix；embeddable 只发布主次版本号
EMBED_URL = f"https://www.python.org/ftp/python/{PY_VERSION}/python-{PY_VERSION}-embed-amd64.zip"
GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py"
CACERT_URL = "https://curl.se/ca/cacert.pem"
# 与 pyproject.toml 的可选 extra 对齐：真实模型 + 服务层
DEPS = [
    "fastapi>=0.115",
    "uvicorn>=0.30",
    "httpx>=0.27",
    "openai>=1.40",
    "anthropic>=0.40",
]
PTH_NAME = "python" + "".join(PY_VERSION.split(".")[:2]) + "._pth"  # python311._pth


def version() -> str:
    """从 pyproject.toml 读版本号。"""
    for line in (REPO / "pyproject.toml").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("version ="):
            return line.split("=", 1)[1].strip().strip('"')
    return "0.0.0"


def download(url: str, dest: Path) -> None:
    if dest.exists() and dest.stat().st_size > 0:
        print(f"  已存在 {dest.name}，跳过下载")
        return
    print(f"  下载 {url}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url) as resp, open(dest, "wb") as f:
        shutil.copyfileobj(resp, f)


def build_frontend() -> None:
    """调 npm run build 产出 ui/dist。node 不可用时报错（可用 --skip-frontend 绕过）。"""
    dist = REPO / "ui" / "dist"
    if dist.is_dir() and any(dist.iterdir()):
        print(f"  ui/dist 已存在（{dist}），跳过构建")
        return
    npm = shutil.which("npm")
    if npm is None:
        sys.exit("ui/dist 不存在且找不到 npm——请先手动 cd ui && npm run build，"
                 "或用 --skip-frontend 使用已有构建产物")
    print("  构建前端（npm run build）……")
    subprocess.run([npm, "run", "build"], cwd=REPO / "ui", check=True)


def setup_python(stage: Path, skip_python: bool) -> Path:
    """解压 embeddable python，修 _pth、装 pip、装依赖。返回 python.exe 路径。"""
    py_dir = stage / "python"
    if skip_python and (py_dir / "python.exe").exists():
        print(f"  --skip-python：复用 {py_dir}")
        return py_dir / "python.exe"

    embed_zip = stage / "downloads" / "python-embed.zip"
    download(EMBED_URL, embed_zip)
    print(f"  解压 embeddable python 到 {py_dir}")
    if py_dir.exists():
        shutil.rmtree(py_dir)
    py_dir.mkdir(parents=True)
    with zipfile.ZipFile(embed_zip) as z:
        z.extractall(py_dir)

    # 修 _pth：加载 Lib/site-packages 并启用 site（embeddable 默认都不做）
    pth = py_dir / PTH_NAME
    lines = pth.read_text(encoding="utf-8").splitlines()
    fixed = []
    for line in lines:
        if line.strip() == "#import site":
            fixed.append("import site")
        elif line.strip().startswith("Lib\\site-packages"):
            continue  # 旧行防重
        else:
            fixed.append(line)
    if "Lib/site-packages" not in fixed:
        fixed.append("Lib/site-packages")
    # embeddable python 有 ._pth 时忽略 PYTHONPATH 环境变量（start.bat 里 set 无效），
    # 项目代码路径必须写死在 _pth 里：相对 python.exe，指向包内 app/src
    if "../app/src" not in fixed:
        fixed.append("../app/src")
    pth.write_text("\n".join(fixed) + "\n", encoding="utf-8")

    python = py_dir / "python.exe"
    cacert = stage / "downloads" / "cacert.pem"
    download(CACERT_URL, cacert)
    env = {**os.environ, "SSL_CERT_FILE": str(cacert)}

    print("  装 pip（get-pip.py）……")
    pip_py = stage / "downloads" / "get-pip.py"
    download(GET_PIP_URL, pip_py)
    subprocess.run([str(python), str(pip_py), "--no-warn-script-location"],
                   env=env, check=True)

    print(f"  装依赖：{' '.join(DEPS)}")
    subprocess.run([str(python), "-m", "pip", "install", "--no-warn-script-location",
                    *DEPS], env=env, check=True)
    return python


def assemble_app(stage: Path) -> None:
    """拷项目代码与预构建前端进包。"""
    app = stage / "app"
    if app.exists():
        shutil.rmtree(app)
    app.mkdir(parents=True)

    # src/cowork：零依赖 + 延迟导入，拷目录 + PYTHONPATH 即可
    shutil.copytree(REPO / "src" / "cowork", app / "src" / "cowork",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    # 预构建前端
    shutil.copytree(REPO / "ui" / "dist", app / "ui" / "dist")
    # 文档与配置（GPL：源码随包走）
    for name in ["README.md", "LICENSE", "schema.sql",
                 "多Agent协作系统-开发文档.md", "M6-界面层接口.md",
                 ".env.example"]:
        src = REPO / name
        if src.exists():
            shutil.copy2(src, app / name)


def write_launcher(stage: Path) -> None:
    """start.bat：起 serve（产出与库都留在包内）+ 延迟开浏览器。故意全英文。"""
    bat = stage / "start.bat"
    # chcp 65001 让 serve 的中文日志在 cmd 里正常显示；bat 本身无中文，无编码雷区
    bat.write_text(
        "@echo off\r\n"
        "rem agent-cowork launcher (built by packaging/build_win_package.py)\r\n"
        "chcp 65001 >nul\r\n"
        "cd /d \"%~dp0\"\r\n"
        "set \"PYTHONPATH=%~dp0app\\src\"\r\n"
        "if not exist \"app\\workspace\" mkdir \"app\\workspace\"\r\n"
        "echo Starting agent-cowork ...\r\n"
        "echo The browser will open at http://127.0.0.1:8000\r\n"
        "start \"\" /b cmd /c \"timeout /t 3 /nobreak >nul & start http://127.0.0.1:8000\"\r\n"
        "\"%~dp0python\\python.exe\" -m cowork.cli serve "
        "--workspace \"%~dp0app\\workspace\" --db \"%~dp0app\\cowork.sqlite\"\r\n"
        "pause\r\n",
        encoding="ascii",
    )

    readme = stage / "使用说明.txt"
    readme.write_text(
        "agent-cowork 使用说明\n"
        "====================\n"
        "\n"
        "1. 双击 start.bat 启动，浏览器会自动打开 http://127.0.0.1:8000\n"
        "2. 首次使用：打开右上角设置页，填入模型 key（如 DeepSeek 或 Kimi 的），\n"
        "   也可以不填——demo 任务用内置脚本后端就能跑，不需要任何 key。\n"
        "3. 在界面里发一个任务目标（如「把 CSV 转成带图表的周报」），\n"
        "   系统会自动拆解、并行执行；跑偏时会停下来问你怎么处理。\n"
        "4. 关掉窗口即退出；任务数据保存在 app\\ 目录里，重开不丢。\n"
        "\n"
        "项目主页与源码：https://github.com/ （见 README.md）\n"
        "本项目只监听 127.0.0.1，不会对外网开放。\n",
        encoding="utf-8",
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--version", default=None, help="发布包版本（默认读 pyproject.toml）")
    ap.add_argument("--skip-frontend", action="store_true", help="不构建前端，用现有 ui/dist")
    ap.add_argument("--skip-python", action="store_true", help="复用已解压的 staging/python")
    ap.add_argument("--stage", default=str(REPO / "packaging" / "staging"),
                    help="中间目录（默认 packaging/staging，可复用加速迭代）")
    args = ap.parse_args()

    ver = args.version or version()
    stage = Path(args.stage)
    stage.mkdir(parents=True, exist_ok=True)

    print(f"[1/5] 构建前端（{'跳过，用现有产物' if args.skip_frontend else 'npm run build'}）")
    if not args.skip_frontend:
        build_frontend()

    print("[2/5] 准备 embeddable Python + 依赖")
    setup_python(stage, args.skip_python)

    print("[3/5] 组装 app/")
    assemble_app(stage)

    print("[4/5] 写 start.bat 与使用说明")
    write_launcher(stage)

    print(f"[5/5] 打包 dist/agent-cowork-win-{ver}.zip")
    out_dir = REPO / "dist"
    out_dir.mkdir(exist_ok=True)
    out_zip = out_dir / f"agent-cowork-win-{ver}.zip"
    if out_zip.exists():
        out_zip.unlink()
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(stage):
            dirs[:] = [d for d in dirs if d not in ("__pycache__", "downloads")]
            for name in files:
                if name.endswith(".pyc"):
                    continue
                full = Path(root) / name
                # zip 里第一层是包名目录，staging 目录本身是包根
                arc = full.relative_to(stage)
                z.write(full, f"agent-cowork-win-{ver}/{arc}")
    size_mb = out_zip.stat().st_size / 1024 / 1024
    print(f"完成：{out_zip}（{size_mb:.1f} MB）")


if __name__ == "__main__":
    main()
