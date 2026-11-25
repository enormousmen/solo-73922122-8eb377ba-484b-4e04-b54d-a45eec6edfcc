"""内置样包的生成逻辑（供服务启动兜底和 scripts/make_samples.py 共用）。

样包由常规源图当前内容切出，source_sha256 与之一致；因此源图一旦变化（例如
不同 Pillow 版本的 PNG 编码差异），启动时必须重建，否则演示会因 SHA 不符被拒收。
"""
from __future__ import annotations

import hashlib
import io
import sqlite3
from pathlib import Path

from PIL import Image

SAMPLE_NAME = "regular-partial.mbtiles"

# 有代表性的空位组合（本站坐标 z/x/y，自北向南）：整级图 + 中段 + 四角
SAMPLE_TILES = [
    (0, 0, 0),
    (1, 1, 0),
    (2, 0, 0), (2, 1, 1), (2, 2, 2), (2, 3, 3),
    (3, 0, 0), (3, 2, 0), (3, 5, 0), (3, 7, 0),
    (3, 0, 3), (3, 3, 3), (3, 4, 3), (3, 7, 3),
    (3, 0, 5), (3, 2, 5), (3, 6, 5), (3, 7, 5),
    (3, 1, 7), (3, 3, 7), (3, 5, 7), (3, 7, 7),
]


def source_sha256(regular_png: Path) -> str:
    h = hashlib.sha256()
    with open(regular_png, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sample_matches(pkg: Path, regular_png: Path) -> bool:
    """样包存在且其 source_sha256 等于当前源图哈希。"""
    if not pkg.is_file():
        return False
    try:
        with sqlite3.connect(f"file:{pkg}?mode=ro", uri=True) as con:
            rows = con.execute(
                "SELECT value FROM metadata WHERE lower(name)='source_sha256'"
            ).fetchall()
    except sqlite3.DatabaseError:
        return False
    return bool(rows) and str(rows[0][0]).strip().lower() == source_sha256(regular_png)


def build_sample(regular_png: Path, dest_dir: Path, name: str = SAMPLE_NAME) -> Path:
    sha = source_sha256(regular_png)
    img = Image.open(regular_png).convert("RGB")
    dest_dir.mkdir(parents=True, exist_ok=True)
    db = dest_dir / name
    tmp = dest_dir / f".{name}.tmp"
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    con.executescript(
        "CREATE TABLE metadata (name TEXT, value TEXT);"
        "CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER,"
        " tile_row INTEGER, tile_data BLOB);"
    )
    con.executemany(
        "INSERT INTO metadata VALUES (?,?)",
        [("name", "常规底图局部瓦片样包"), ("format", "png"),
         ("type", "baselayer"), ("source_sha256", sha)],
    )
    rows = []
    for z, x, y in SAMPLE_TILES:
        side = 256 * (2 ** z)
        im = img if img.size == (side, side) else img.resize((side, side), Image.LANCZOS)
        tile = im.crop((x * 256, y * 256, (x + 1) * 256, (y + 1) * 256))
        buf = io.BytesIO()
        tile.save(buf, "PNG")
        row = side // 256 - 1 - y  # 本站自北向南 -> MBTiles 自南向北
        rows.append((z, x, row, buf.getvalue()))
    con.executemany("INSERT INTO tiles VALUES (?,?,?,?)", rows)
    con.commit()
    con.close()
    tmp.replace(db)
    return db


def ensure_sample(regular_png: Path, bundled: Path, writable: Path) -> Path:
    """返回可用于演示的样包路径：内置于仓库的样包若与源图不符，重建到可写目录。"""
    if sample_matches(bundled, regular_png):
        return bundled
    dest = writable / "samples"
    out = dest / SAMPLE_NAME
    if not sample_matches(out, regular_png):
        build_sample(regular_png, dest)
    return out
