"""FastAPI 入口：切片值班台。

接口：
  GET  /api/status                     两个任务的进度快照 + 最近一次导入包状态
  POST /api/jobs/{kind}/start          启动切片（kind=regular|emergency；emergency 支持 ?simulate_fail=true 自测失败）
  POST /api/jobs/{kind}/reset          清空已发布瓦片（任务空闲时才允许；重置会使在途导入包作废）
  POST /api/import/mbtiles             上传 MBTiles 包（≤8 MiB，验包后只补空位）
  GET  /api/import/{id}                查询某次导入的状态
  GET  /tiles/{kind}/{z}/{x}/{y}.png   取已发布瓦片
  GET  /sources/{kind}.png             源图预览
  GET  /samples/regular-partial.mbtiles  下载内置样包
  GET  /                               演示页面
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .imports import MAX_BYTES
from .samplepack import SAMPLE_NAME, ensure_sample
from .scheduler import KINDS, Scheduler
from .sources import ensure_sources

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data")).resolve()
TILE_DELAY = float(os.environ.get("TILE_DELAY", "0.15"))
IMPORT_DELAY = float(os.environ.get("IMPORT_VERIFY_DELAY", "0"))
STATIC_DIR = Path(__file__).resolve().parent / "static"
SAMPLES_DIR = Path(__file__).resolve().parent.parent / "samples"

sched = Scheduler(DATA_DIR, TILE_DELAY, import_delay=IMPORT_DELAY)


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_sources(DATA_DIR)
    sched.start()
    yield
    await sched.shutdown()


app = FastAPI(title="应急切片插队服务", lifespan=lifespan)


def _check(kind: str) -> None:
    if kind not in KINDS:
        raise HTTPException(404, "unknown job kind")


@app.get("/api/status")
async def status():
    return sched.snapshot()


@app.post("/api/jobs/{kind}/start")
async def start(kind: str, simulate_fail: bool = False):
    _check(kind)
    job = sched.request_start(kind, simulate_fail=simulate_fail)
    return {"ok": True, "job": job.snapshot()}


@app.post("/api/import/mbtiles")
async def import_mbtiles(file: UploadFile = File(...), import_fail_after: int = 0):
    """受理 MBTiles 包：先限长读取，再后台验包；验包期间照常切片/取图。"""
    body = bytearray()
    while True:
        chunk = await file.read(1 << 20)
        if not chunk:
            break
        body.extend(chunk)
        if len(body) > MAX_BYTES:  # 边读边判断，避免大包占内存
            raise HTTPException(413, f"包超过上限 {MAX_BYTES // (1024 * 1024)} MiB")
    try:
        sched.imports.submit(
            file.filename or "(未命名)", bytes(body),
            fail_after=max(0, import_fail_after),  # 自测钩子：写盘 N 张后模拟失败
        )
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"ok": True, "import": sched.imports.snapshot(), "import_id": sched.imports.last_id}


@app.get("/api/import/{import_id}")
async def import_status(import_id: str):
    rep = sched.imports.reports.get(import_id)
    if rep is None:
        raise HTTPException(404, "unknown import id")
    return {"import": rep.snapshot()}


@app.get("/samples/regular-partial.mbtiles")
def sample_package():
    p = SAMPLES_DIR / SAMPLE_NAME
    # 仓库里的样包若与当前源图哈希不符（例如不同 Pillow 版本的 PNG 编码差异），
    # 就按当前源图重建一份放到 DATA_DIR/samples，保证演示包始终能通过验包。
    try:
        p = ensure_sample(DATA_DIR / "sources" / "regular.png", p, DATA_DIR)
    except OSError:
        pass  # 只读环境下退回仓库内样包；不匹配时验包会给出明确拒收原因
    if not p.is_file():
        raise HTTPException(404, "sample missing")
    return FileResponse(p, media_type="application/x-sqlite3",
                        filename=SAMPLE_NAME)


@app.post("/api/jobs/{kind}/reset")
async def reset(kind: str):
    _check(kind)
    try:
        job = sched.reset(kind)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"ok": True, "job": job.snapshot()}


@app.get("/tiles/{kind}/{z}/{x}/{y}.png")
def tile(kind: str, z: int, x: int, y: int):
    _check(kind)
    if z < 0 or z > 10 or x < 0 or y < 0 or x >= 2 ** z or y >= 2 ** z:
        raise HTTPException(404, "tile out of range")
    p = DATA_DIR / "tiles" / kind / str(z) / str(x) / f"{y}.png"
    if not p.is_file():
        raise HTTPException(404, "tile not published")
    return FileResponse(p, media_type="image/png")


@app.get("/sources/{kind}.png")
def source(kind: str):
    _check(kind)
    p = DATA_DIR / "sources" / f"{kind}.png"
    if not p.is_file():
        raise HTTPException(404, "source missing")
    return FileResponse(p, media_type="image/png")


# 静态页面挂在最后，避免覆盖上面的 API 路由
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
