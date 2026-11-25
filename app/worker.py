#!/usr/bin/env python3
"""独立渲染子进程：只依赖标准库 + Pillow，全部配置经命令行参数传入。

协议（stdout 每行一个 JSON）：
  {"event":"plan","total":N,"remaining":M}
  {"event":"start_tile","z":z,"x":x,"y":y}
  {"event":"tile","z":z,"x":x,"y":y,"published":k}
  {"event":"error","message":"..."}
  {"event":"done","published":k}

发布语义：先写 .tmp 再原子发布到目标路径；若目标已存在（导入包先发布或前一轮已出图）
则一律跳过、不覆盖，并发 {"event":"skip"} 事件。已存在的 PNG 直接跳过 => 已发布瓦片不重算。

中止协议：父进程通过 stdin 写入任意字节（或关闭 stdin）即要求立即中止，
worker 以退出码 42 退出；渲染耗时用 select 模拟，可被 stdin 立即唤醒，
不依赖信号（某些沙箱环境禁止发信号）。父进程也可能直接 SIGTERM。
"""
import argparse
import json
import os
import select
import shutil
import sys
from pathlib import Path

from PIL import Image

ABORT_RC = 42  # 被父进程中止时的约定退出码


def emit(**kw) -> None:
    sys.stdout.write(json.dumps(kw) + "\n")
    sys.stdout.flush()


def _link_or_copy(tmp: Path, final: Path) -> bool:
    """原子发布；目标已存在时返回 False（不覆盖）。"""
    try:
        os.link(tmp, final)  # 原子；目标已存在时抛 FileExistsError
        return True
    except FileExistsError:
        return False
    except OSError:
        try:
            with open(final, "xb") as dst, open(tmp, "rb") as src:
                shutil.copyfileobj(src, dst)
            return True
        except FileExistsError:
            return False
        except OSError:
            try:
                final.unlink(missing_ok=True)
            except OSError:
                pass
            raise
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _abort_requested(timeout: float) -> bool:
    """等待 timeout 秒；期间 stdin 出现数据或 EOF（父进程要求中止/已退出）返回 True。"""
    r, _, _ = select.select([sys.stdin], [], [], max(timeout, 0))
    return bool(r)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--tile", type=int, default=256)
    ap.add_argument("--zooms", default="0,1,2,3")
    ap.add_argument("--delay", type=float, default=0.0, help="每张瓦片的模拟渲染耗时(秒)")
    ap.add_argument("--fail-after", type=int, default=0, help="发布 N 张后模拟失败退出(自测用)")
    ap.add_argument("--input-dir", default="", help="上传包暂存目录（顺带清理残留 .tmp）")
    a = ap.parse_args()

    zooms = [int(z) for z in a.zooms.split(",") if z.strip()]
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for tmp in out.rglob("*.tmp"):  # 清理上次被中止留下的半成品
        tmp.unlink(missing_ok=True)
    if a.input_dir:
        try:
            for tmp in Path(a.input_dir).glob("*.tmp"):
                tmp.unlink(missing_ok=True)
        except OSError:
            pass

    src = Image.open(a.src).convert("RGB")
    plan = [(z, x, y) for z in zooms for x in range(2 ** z) for y in range(2 ** z)]

    def final_of(t):
        z, x, y = t
        return out / str(z) / str(x) / f"{y}.png"

    remaining = [t for t in plan if not final_of(t).is_file()]
    emit(event="plan", total=len(plan), remaining=len(remaining))

    published = 0
    pyramids: dict[int, Image.Image] = {}
    for (z, x, y) in remaining:
        if _abort_requested(0):
            emit(event="aborted", published=published)
            return ABORT_RC
        # 本轮开始后才导入的坐标：直接跳过，不重复渲染（导入与切片并发时）
        if final_of((z, x, y)).is_file():
            emit(event="skip", z=z, x=x, y=y, published=published)
            continue
        emit(event="start_tile", z=z, x=x, y=y)
        if z not in pyramids:
            side = a.tile * (2 ** z)
            pyramids[z] = src if src.size == (side, side) else src.resize((side, side), Image.LANCZOS)
        img = pyramids[z].crop((x * a.tile, y * a.tile, (x + 1) * a.tile, (y + 1) * a.tile))
        if _abort_requested(a.delay):  # 模拟渲染耗时；此时被中止则该瓦片未发布
            emit(event="aborted", published=published)
            return ABORT_RC
        final = final_of((z, x, y))
        final.parent.mkdir(parents=True, exist_ok=True)
        tmp = final.parent / f".{y}.render-{os.getpid()}.tmp"
        img.save(tmp, "PNG")
        if _link_or_copy(tmp, final):
            published += 1
            emit(event="tile", z=z, x=x, y=y, published=published)
        else:
            # 该坐标已有先发布的完整瓦片（导入包或前一轮），保留原文件、不覆盖
            emit(event="skip", z=z, x=x, y=y, published=published)
        if a.fail_after and published >= a.fail_after:
            emit(event="error", message=f"simulated render failure after {published} tiles")
            return 3
    emit(event="done", published=published)
    return 0


if __name__ == "__main__":
    sys.exit(main())
