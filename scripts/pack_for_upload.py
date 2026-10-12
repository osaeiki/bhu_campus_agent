# -*- coding: utf-8 -*-
"""把项目打包成一个用于上传到服务器的 zip（自动排除不该传的东西）。

为什么需要它：
    本地 `.venv`、`cache` 这类目录动辄上千个文件、几百 MB，而且只对本地有效；
    直接用 SFTP 拖整个文件夹时，客户端要一个个传小文件，面板会长时间"卡住"，
    还常常中途断线。压成一个包只传一个文件，几秒钟就完。

用法（在任意目录下执行都可以）：
    # 打包脚本自己所在的项目
    .venv\\Scripts\\python scripts\\pack_for_upload.py

    # 打包另一个项目：把下面这行的路径换成你自己的
    <项目目录>/scripts/pack_for_upload.py --project <另一个项目目录>



    # 需要把某个默认被排除的目录也打进去（例如 data）
    ... --project <另一个项目目录> --include data

产出：项目上级目录下的 <项目名>_upload.zip，压缩包内的路径以项目根为起点，
      解压时直接解到服务器上的项目目录即可。
"""
import argparse
import os
import sys
import time
import zipfile
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent

# 以下目录默认不进压缩包（都是"本地生成、服务器上能重建"的东西）
EXCLUDE_DIRS = {
    ".venv", "venv", "env", "__pycache__",
    "data", "logs", "cache", "exports", "output", "outputs",
    "node_modules", "dist", "build", ".next",
    ".git", ".idea", ".vscode",
    ".pytest_cache", ".mypy_cache", ".ruff_cache",
}
EXCLUDE_SUFFIX = {".pyc", ".pyo", ".zip", ".log"}
EXCLUDE_FILES = {".env", ".env.local", ".DS_Store", "Thumbs.db"}

# 由 --include 覆盖：列在这里的目录即使命中 EXCLUDE_DIRS 也会被打包
KEEP_DIRS: set = set()


def should_skip(rel: Path) -> bool:
    if set(rel.parts) & EXCLUDE_DIRS:
        return True
    if rel.name in EXCLUDE_FILES:
        return True
    if rel.suffix.lower() in EXCLUDE_SUFFIX:
        return True
    return False


def build_zip(project: Path, output: Path):
    files = []
    skipped = []
    excluded_top = set()
    for root, dirs, names in os.walk(project):
        keep_dirs = []
        for d in dirs:
            if d in EXCLUDE_DIRS and d not in KEEP_DIRS:
                rel_dir = (Path(root) / d).relative_to(project)
                if len(rel_dir.parts) == 1:
                    excluded_top.add(d)
                continue
            keep_dirs.append(d)
        dirs[:] = keep_dirs
        for name in names:
            full = Path(root) / name
            rel = full.relative_to(project)
            if should_skip(rel):
                skipped.append(rel)
                continue
            files.append((full, rel))

    files.sort(key=lambda x: str(x[1]))
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()

    total = 0
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for full, rel in files:
            # Linux 只认正斜杠，Windows 的 os.sep 是反斜杠，必须转换
            arcname = str(rel).replace(os.sep, "/")
            zf.write(full, arcname)
            total += full.stat().st_size

    return len(files), total, len(skipped), sorted(excluded_top)


def main() -> int:
    parser = argparse.ArgumentParser(description="打包项目用于上传到服务器")
    parser.add_argument("--project", default="", help="要打包的项目目录（默认用本脚本所在的项目）")
    parser.add_argument("--output", default="", help="输出 zip 路径（默认放在项目的上级目录）")
    parser.add_argument("--include", default="", help="强制打包的目录名，逗号分隔，如 data,cache")
    args = parser.parse_args()

    project = Path(args.project).resolve() if args.project else PROJECT_DIR
    if not project.is_dir():
        print("项目目录不存在：", project)
        return 1
    output = Path(args.output).resolve() if args.output else project.parent / (project.name + "_upload.zip")

    global KEEP_DIRS
    KEEP_DIRS = {x.strip() for x in args.include.split(",") if x.strip()}

    print("项目目录:", project)
    print("正在打包（自动排除 .venv / data / cache / exports / __pycache__ / .env 等）...")
    t0 = time.time()
    count, total, skipped, excluded = build_zip(project, output)
    cost = time.time() - t0

    size_mb = output.stat().st_size / 1024 / 1024
    print()
    print("完成：%d 个文件，原始 %.2f MB，压缩后 %.2f MB，耗时 %.1fs"
          % (count, total / 1024 / 1024, size_mb, cost))
    print("跳过：%d 个文件；整个目录跳过：%s"
          % (skipped, ", ".join(excluded) if excluded else "无"))
    if KEEP_DIRS:
        print("强制包含：", ", ".join(sorted(KEEP_DIRS)))
    print("输出：", output)
    print()
    name = project.name
    print("下一步（在 XTerminal 里）：")
    print("  1) 把这个 zip 上传到服务器的 /project 目录")
    print("  2) 服务器上执行：")
    print("       cd /project")
    print("       cp -r %s %s.bak.$(date +%%Y%%m%%d)        # 先备份（可选）" % (name, name))
    print("       python3 -m zipfile -e /project/%s_upload.zip /project/%s/" % (name, name))
    print("       ls %s                              # 确认文件到位" % name)
    print("  3) 再构建镜像、重建容器（见项目的 dockerfile 和部署文档）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
