"""生成内置样包：常规源图局部瓦片（z0–z3，行号按 MBTiles 南向北）。

用法：python scripts/make_samples.py [REGULAR_PNG] [OUT_DIR]
默认用 data/sources/regular.png（缺失时先生成内置源图），输出到 samples/。
服务启动时也会做同样的事（样包与源图哈希不符则自动重建），所以通常无需手工执行。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.samplepack import SAMPLE_TILES, build_sample, SAMPLE_NAME  # noqa: E402
from app.sources import ensure_sources  # noqa: E402


def main() -> int:
    args = sys.argv[1:]
    if args:
        src = Path(args[0])
    else:
        ensure_sources(ROOT / "data")
        src = ROOT / "data" / "sources" / "regular.png"
    out_dir = Path(args[1]) if len(args) > 1 else ROOT / "samples"
    p = build_sample(src, out_dir, SAMPLE_NAME)
    print(f"已生成 {p}（{len(SAMPLE_TILES)} 张，{p.stat().st_size} 字节）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
