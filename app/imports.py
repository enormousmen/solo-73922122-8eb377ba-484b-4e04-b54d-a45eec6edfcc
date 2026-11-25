"""MBTiles 验包与导入：常规源图在别处切好的瓦片包。

约定
- 单包 ≤ MAX_BYTES（8 MiB）；metadata 的 source_sha256 必须等于常规源图当前 SHA-256；
- 只收 z0–z3 的 256×256 PNG；MBTiles 行号自南向北，本站自北向南，换算 y = (2^z - 1) - tile_row；
- 整包先完整解码校验：坐标越界 / 重复 / 无法完整解码 => 整包拒收，不写任何瓦片；
- 验包通过后只补空位：目标坐标已有文件则跳过，绝不覆盖（以先发布的文件为准）；
- 验包与导入期间照常切片、取图；常规任务被重置则此包作废；
- 写盘中途失败：已发布的完整瓦片保留，提示可重传；重传只补剩余空位。
"""
from __future__ import annotations

import hashlib
import io
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from PIL import Image

from .tilefs import link_or_copy

MAX_BYTES = 8 * 1024 * 1024
TILE_SIZE = 256
ZOOMS = (0, 1, 2, 3)
SHA_KEY = "source_sha256"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class Reject(Exception):
    """整包拒收：消息直接返回给值班员。"""


@dataclass
class Tile:
    z: int
    x: int
    y: int
    data: bytes

    @property
    def key(self) -> str:
        return f"{self.z}/{self.x}/{self.y}"


@dataclass
class Report:
    filename: str
    checksum: str
    message: str = "验包中"
    accepted: bool = False
    received: int = 0
    imported: int = 0
    skipped: int = 0
    rejected_reason: str | None = None
    imported_tiles: list[str] = field(default_factory=list)
    duration_ms: int = 0

    @property
    def state(self) -> str:
        if self.accepted:
            return "accepted"
        if self.rejected_reason and self.rejected_reason.startswith("导入中断"):
            return "partial"
        if self.rejected_reason and (
            "重置" in self.rejected_reason and "作废" in self.rejected_reason
        ):
            return "void"
        if self.rejected_reason:
            return "rejected"
        return "verifying"

    def snapshot(self) -> dict:
        return {
            "filename": self.filename,
            "checksum": self.checksum,
            "state": self.state,
            "accepted": self.accepted,
            "message": self.message,
            "received": self.received,
            "imported": self.imported,
            "skipped": self.skipped,
            "rejected_reason": self.rejected_reason,
            "imported_tiles": list(self.imported_tiles),
            "duration_ms": self.duration_ms,
            "progress": {
                "received": self.received,
                "imported": self.imported,
                "skipped": self.skipped,
            },
        }


def _connect_ro(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _decode_all(path: Path, expected_sha: str, verify_delay: float = 0.0,
                on_tile: Callable[[int], None] | None = None) -> list[Tile]:
    """只读打开包并完整解码全部瓦片；不写任何公开输出。任何问题抛 Reject。"""
    try:
        con = _connect_ro(path)
    except sqlite3.DatabaseError as e:
        raise Reject(f"不是有效的 MBTiles（{e}）") from None
    try:
        try:
            meta = con.execute("SELECT name, value FROM metadata").fetchall()
        except sqlite3.DatabaseError as e:
            raise Reject(f"包结构无效：缺少 metadata（{e}）") from None
        m = {str(r["name"]).strip().lower(): r["value"] for r in meta}
        if str(m.get("format", "")).strip().lower() != "png":
            raise Reject("metadata.format 必须为 png")
        sha = str(m.get(SHA_KEY) or "").strip().lower()
        if sha != expected_sha.lower():
            raise Reject("metadata.source_sha256 与常规源图文件的 SHA-256 不一致")
        try:
            rows = con.execute(
                "SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles"
            ).fetchall()
        except sqlite3.DatabaseError as e:
            raise Reject(f"包结构无效：缺少 tiles（{e}）") from None
        if not rows:
            raise Reject("包内没有任何瓦片")
        tiles: list[Tile] = []
        seen: set[tuple[int, int, int]] = set()
        for r in rows:
            z, col, row = r["zoom_level"], r["tile_column"], r["tile_row"]
            if not all(isinstance(n, int) for n in (z, col, row)):
                raise Reject("坐标不是整数")
            if z not in ZOOMS:
                raise Reject(f"只接受 z0–z3，发现 z{z}")
            n = 2 ** z
            if not (0 <= col < n) or not (0 <= row < n):
                raise Reject(f"坐标越界：z{z} column={col} row={row}")
            y = n - 1 - row  # MBTiles 自南向北 -> 本站自北向南
            k = (z, col, y)
            if k in seen:
                raise Reject(f"坐标重复（换算后）：{z}/{col}/{y}")
            seen.add(k)
            data = r["tile_data"]
            if not isinstance(data, (bytes, bytearray)) or not data:
                raise Reject(f"瓦片数据缺失：z{z} column={col} row={row}")
            if bytes(data[:8]) != PNG_MAGIC:
                raise Reject(f"瓦片不是 PNG：z{z} column={col} row={row}")
            try:
                with Image.open(io.BytesIO(data)) as probe:
                    if probe.format != "PNG":
                        raise Reject(f"瓦片不是 PNG：z{z} column={col} row={row}")
                    probe.verify()
                with Image.open(io.BytesIO(data)) as im:
                    im.load()
                    if im.size != (TILE_SIZE, TILE_SIZE):
                        raise Reject(
                            f"瓦片须为 {TILE_SIZE}×{TILE_SIZE}，实际 {im.size[0]}×{im.size[1]}："
                            f"z{z} column={col} row={row}"
                        )
            except Reject:
                raise
            except Exception as e:
                raise Reject(f"瓦片无法完整解码：z{z} column={col} row={row}（{e}）") from None
            tiles.append(Tile(z, col, y, bytes(data)))
            if on_tile:
                on_tile(len(tiles))
            if verify_delay:
                time.sleep(verify_delay)
        return tiles
    except Reject:
        raise
    except sqlite3.DatabaseError as e:
        raise Reject(f"不是有效的 MBTiles（{e}）") from None
    finally:
        con.close()


class ImportManager:
    """一次只处理一个包；验包与导入在工作线程中执行，不阻塞事件循环。"""

    def __init__(
        self,
        data_dir: Path,
        regular_src: Path,
        regular_out: Path,
        epoch: Callable[[], int],
        epoch_lock: threading.Lock,
        verify_delay: float = 0.0,
    ):
        self.data_dir = data_dir
        self.src = regular_src
        self.out = regular_out
        self.epoch = epoch
        self.epoch_lock = epoch_lock
        self.verify_delay = verify_delay
        self.reports: dict[str, Report] = {}
        self.last_id: str | None = None
        self.busy = False
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.on_tile: Callable[[int, int, int], None] | None = None
        self.on_done: Callable[[], None] | None = None

    # ---------- 资源清理 ----------

    def cleanup_orphans(self) -> None:
        """服务启动时（尚无任何导入线程）清理上传暂存目录与残留临时文件。"""
        staging = self.data_dir / "uploads"
        staging.mkdir(parents=True, exist_ok=True)
        for p in staging.iterdir():
            try:
                p.unlink()
            except OSError:
                pass
        for tmp in self.out.rglob("*.tmp"):
            try:
                tmp.unlink()
            except OSError:
                pass

    # ---------- 查询 ----------

    @property
    def latest(self) -> Report | None:
        return self.reports.get(self.last_id) if self.last_id else None

    def snapshot(self) -> dict:
        return {
            "busy": self.busy,
            "max_bytes": MAX_BYTES,
            "latest": self.latest.snapshot() if self.latest else None,
        }

    def shutdown(self) -> None:
        """等待当前导入线程收尾（最多 5 秒）；超时则不阻塞进程退出（线程为守护线程）。"""
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=5)

    # ---------- 受理 ----------

    def regular_sha(self) -> str:
        h = hashlib.sha256()
        with open(self.src, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    def submit(self, filename: str, body: bytes, *, verify_delay: float | None = None,
               fail_after: int = 0) -> Report:
        with self._lock:
            if self.busy:
                raise RuntimeError("已有导入在进行，请稍后再试")
            self.busy = True
        report = Report(
            filename=filename or "(未命名)",
            checksum=hashlib.sha256(body).hexdigest(),
        )
        report_id = uuid.uuid4().hex
        self.reports[report_id] = report
        self.last_id = report_id
        epoch0 = self.epoch()  # 受理时记录重置计数
        delay = self.verify_delay if verify_delay is None else verify_delay
        self._thread = threading.Thread(
            target=self._process,
            args=(report, body, report_id, epoch0, delay, fail_after),
            daemon=True,
            name="mbtiles-import",
        )
        self._thread.start()
        return report

    def _process(self, report: Report, body: bytes, report_id: str, epoch0: int,
                 verify_delay: float, fail_after: int) -> None:
        t0 = time.monotonic()
        staging = self.data_dir / "uploads"
        path = staging / f"{report_id}.mbtiles"
        try:
            if not body:
                raise Reject("空文件")
            if len(body) > MAX_BYTES:
                raise Reject(f"超过单包上限 {MAX_BYTES // (1024 * 1024)} MiB")
            staging.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
            expected = self.regular_sha()
            tiles = _decode_all(
                path, expected, verify_delay,
                on_tile=lambda n: setattr(report, "received", n),
            )  # 完整解码，不落公开目录
            self._check_epoch(epoch0)
            report.received = len(tiles)
            self._publish(tiles, report, epoch0, fail_after)
            report.accepted = True
            report.message = (
                f"验包通过：{len(tiles)} 张；补空位 {report.imported} 张，"
                f"已存在跳过 {report.skipped} 张"
            )
        except Reject as e:
            report.rejected_reason = str(e)
            report.message = f"拒收：{e}"
        except Exception as e:  # 写盘失败：已发布完整瓦片保留，请重传
            report.rejected_reason = f"导入中断：{e!r}"
            report.message = (
                f"导入中断：已发布的 {report.imported} 张完整瓦片保留，"
                f"可重传此包（重传只补剩余空位）"
            )
        finally:
            report.duration_ms = int((time.monotonic() - t0) * 1000)
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            with self._lock:
                self.busy = False
            if self.on_done:
                try:
                    self.on_done()
                except Exception:
                    pass

    def _check_epoch(self, epoch0: int) -> None:
        if self.epoch() != epoch0:
            raise Reject("常规任务被重置，此包作废")

    def _publish(self, tiles: list[Tile], report: Report, epoch0: int,
                 fail_after: int) -> None:
        for t in tiles:
            final = self.out / str(t.z) / str(t.x) / f"{t.y}.png"
            with self.epoch_lock:
                self._check_epoch(epoch0)
                if final.is_file():
                    report.skipped += 1
                    continue
                tmp = final.parent / f".{t.y}.import-{uuid.uuid4().hex}.tmp"
                tmp.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_bytes(t.data)
                if link_or_copy(tmp, final):
                    report.imported += 1
                    report.imported_tiles.append(t.key)
                    if self.on_tile:
                        try:
                            self.on_tile(t.z, t.x, t.y)
                        except Exception:
                            pass
                else:
                    report.skipped += 1
            if fail_after and report.imported >= fail_after:
                raise OSError(f"模拟写盘失败（已成功发布 {report.imported} 张）")
