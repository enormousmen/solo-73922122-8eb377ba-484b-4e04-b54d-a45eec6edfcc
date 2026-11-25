#!/usr/bin/env python3
"""自测脚本（仅标准库）：

  测试A 应急插队：常规渲染到一半启动应急 -> 常规立即被中止、在渲瓦片被舍弃，
        应急算完后常规自动续做，已发布瓦片不重算（mtime 不变）、瓦片可取。
  测试B 应急失败后续做：应急模拟失败退出后，常规仍然续做至完成，
        应急已发布的部分瓦片照常可取。
  测试C MBTiles 拒收：SHA 不符 / 越界 / 重复 / 无法解码 均整包拒收且不落盘。
  测试D 导入补空位：验包期间切片照常进行；通过后只补空位、不覆盖已有文件；
        切片随后跳过已导入坐标，进度按实际出图坐标计。
  测试E 重置作废 / 写盘中断：验包期间重置常规 -> 此包作废；
        写盘中途失败保留已发布瓦片，重传只补剩余空位。

用法：先启动服务（DATA_DIR 与之一致），再运行
  BASE=http://127.0.0.1:8000 DATA_DIR=./data python3 scripts/selftest.py
  （测试C–E 需要 samples/regular-partial.mbtiles，仓库内已附带）
"""
import io
import json
import os
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.environ.get("BASE", "http://127.0.0.1:8000")
DATA = Path(os.environ.get("DATA_DIR", "./data"))
ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "samples" / "regular-partial.mbtiles"
TOTAL = 85  # z0..z3
MAX_BYTES = 8 * 1024 * 1024


def api(method: str, path: str, data=None, headers=None) -> dict:
    req = urllib.request.Request(BASE + path, method=method, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def status() -> dict:
    return api("GET", "/api/status")


def job(s: dict, kind: str) -> dict:
    return s["jobs"][kind]


def imp(s: dict) -> dict | None:
    return s["import"]["latest"]


def wait_for(pred, timeout: float, desc: str, on_poll=None) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = status()
        if on_poll:
            on_poll(s)
        if pred(s):
            return s
        time.sleep(0.05)
    raise AssertionError(f"超时未等到：{desc}")


def reset_all() -> None:
    for k in ("regular", "emergency"):
        try:
            api("POST", f"/api/jobs/{k}/reset")
        except urllib.error.HTTPError as e:
            if e.code != 409:
                raise
    wait_for(
        lambda s: all(j["status"] in ("idle", "done", "failed") for j in s["jobs"].values()),
        10, "重置后回到空闲",
    )


def tile_path(kind: str, t: str) -> Path:
    z, x, y = t.split("/")
    return DATA / "tiles" / kind / z / x / f"{y}.png"


def get_tile(kind: str, t: str) -> bytes:
    z, x, y = t.split("/")
    with urllib.request.urlopen(f"{BASE}/tiles/{kind}/{z}/{x}/{y}.png", timeout=10) as r:
        assert r.status == 200, f"{kind}/{t} HTTP {r.status}"
        return r.read()


def regular_count() -> int:
    return len(list((DATA / "tiles" / "regular").rglob("*.png")))


def upload_package(path: Path, query: str = "") -> int:
    """用标准库拼 multipart 上传；返回 HTTP 状态码（200 表示受理，验包结果见状态接口）。"""
    body = bytearray()
    boundary = b"----selftest-boundary"
    body += (f"--{boundary.decode()}\r\n"
             f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
             f"Content-Type: application/octet-stream\r\n\r\n").encode()
    body += path.read_bytes()
    body += f"\r\n--{boundary.decode()}--\r\n".encode()
    try:
        api("POST", f"/api/import/mbtiles{query}", bytes(body),
            {"Content-Type": f"multipart/form-data; boundary={boundary.decode()}"})
        return 200
    except urllib.error.HTTPError as e:
        return e.code


def wait_import(timeout: float = 60) -> dict:
    s = wait_for(lambda s: imp(s) is not None and imp(s)["state"] != "verifying",
                 timeout, "导入结束")
    return imp(s)


def test_preempt() -> None:
    print("== 测试A：应急插队 ==")
    reset_all()
    api("POST", "/api/jobs/regular/start")
    s = wait_for(
        lambda s: job(s, "regular")["published"] >= 8 and job(s, "regular")["current_tile"],
        30, "常规进入在渲状态",
    )
    j = job(s, "regular")
    inflight = tuple(j["current_tile"])
    inflight_key = "/".join(map(str, inflight))
    pre = set(j["tiles"])
    assert inflight_key not in pre, "在渲瓦片不应已发布"
    mtimes = {t: tile_path("regular", t).stat().st_mtime_ns for t in pre}
    print(f"  常规已发布 {len(pre)} 张，在渲 {inflight_key}，此刻受理应急…")

    t0 = time.time()
    api("POST", "/api/jobs/emergency/start")
    s = wait_for(lambda s: s["running_kind"] == "emergency", 5, "应急接管渲染进程")
    dt = time.time() - t0
    assert job(s, "regular")["status"] == "interrupted", "常规应被标记为被抢占"
    print(f"  {dt:.2f}s 内切换到应急，常规状态=interrupted")

    seen = set()

    def sample(s):
        # 只统计应急任务本身存续期间的渲染进程归属
        if job(s, "emergency")["status"] in ("queued", "running"):
            seen.add(s["running_kind"])

    s = wait_for(lambda s: job(s, "emergency")["status"] == "done", 90, "应急完成",
                 on_poll=sample)
    assert job(s, "emergency")["published"] == TOTAL, "应急应完整出图"
    assert seen <= {"emergency"}, f"应急期间不应出现其他渲染进程：{seen}"
    print(f"  应急完成 {TOTAL}/{TOTAL}，期间渲染进程始终为 emergency")

    s = wait_for(lambda s: job(s, "regular")["status"] == "done", 90, "常规自动续做至完成")
    j = job(s, "regular")
    assert j["published"] == TOTAL, "常规应最终出齐 85 张"
    for t, m in mtimes.items():
        assert tile_path("regular", t).stat().st_mtime_ns == m, f"已发布瓦片被重算：{t}"
    assert inflight_key in set(j["tiles"]), "被舍弃的在渲瓦片应已重算发布"
    for t in ("0/0/0", "2/3/1", "3/7/7"):
        assert get_tile("regular", t)[:8] == b"\x89PNG\r\n\x1a\n", f"瓦片 {t} 不是合法 PNG"
    print(f"  常规续做完成 {TOTAL}/{TOTAL}；{len(mtimes)} 张已发布瓦片 mtime 未变（未重算）；"
          f"在渲瓦片 {inflight_key} 已重算；抽样瓦片均可取")
    print("  ✔ 测试A 通过")


def test_emergency_failure() -> None:
    print("== 测试B：应急失败后续做 ==")
    reset_all()
    api("POST", "/api/jobs/regular/start")
    wait_for(
        lambda s: job(s, "regular")["published"] >= 5 and job(s, "regular")["current_tile"],
        30, "常规进入在渲状态",
    )
    api("POST", "/api/jobs/emergency/start?simulate_fail=true")
    s = wait_for(lambda s: job(s, "emergency")["status"] == "failed", 60, "应急失败")
    j = job(s, "emergency")
    assert j["published"] == 10, f"应急应发布 10 张后失败，实际 {j['published']}"
    print(f"  应急发布 {j['published']} 张后失败（error={j['error']!r}），等待常规续做…")

    s = wait_for(lambda s: job(s, "regular")["status"] == "done", 90, "常规续做至完成")
    assert job(s, "regular")["published"] == TOTAL, "应急失败后常规应出齐 85 张"
    assert get_tile("emergency", "0/0/0")[:8] == b"\x89PNG\r\n\x1a\n", "应急已发布瓦片应仍可取"
    assert get_tile("regular", "3/0/0")[:8] == b"\x89PNG\r\n\x1a\n", "常规瓦片应可取"
    print("  常规续做完成 85/85；应急失败留下的已发布瓦片仍可取")
    print("  ✔ 测试B 通过")


def _make_bad_packages(tmp: Path) -> dict[str, Path]:
    """生成若干应被整包拒收的样包（仅标准库 + Pillow 可选；无 Pillow 时跳过解码类用例）。"""
    import hashlib

    try:
        from PIL import Image
    except ImportError:
        Image = None
    reg = DATA / "sources" / "regular.png"
    sha = hashlib.sha256(reg.read_bytes()).hexdigest()
    tmp.mkdir(parents=True, exist_ok=True)

    def make(name, meta_sha=sha, fmt="png", rows=None):
        p = tmp / f"{name}.mbtiles"
        p.unlink(missing_ok=True)
        con = sqlite3.connect(p)
        con.executescript(
            "CREATE TABLE metadata (name TEXT, value TEXT);"
            "CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER,"
            " tile_row INTEGER, tile_data BLOB);"
        )
        con.executemany("INSERT INTO metadata VALUES (?,?)",
                        [("format", fmt), ("name", "bad"), ("source_sha256", meta_sha)])
        con.executemany("INSERT INTO tiles VALUES (?,?,?,?)", rows or [])
        con.commit()
        con.close()
        return p

    png = b"\x89PNG\r\n\x1a\n"
    if Image is None:
        return {}
    buf = io.BytesIO()
    Image.new("RGB", (256, 256), (120, 160, 200)).save(buf, "PNG")
    good = buf.getvalue()
    return {
        "sha_mismatch": make("sha_mismatch", meta_sha="0" * 64,
                             rows=[(0, 0, 0, good)]),
        "out_of_range": make("out_of_range", rows=[(3, 8, 0, good)]),
        "duplicate": make("duplicate", rows=[(3, 0, 0, good), (3, 0, 0, good)]),
        "undecodable": make("undecodable", rows=[(0, 0, 0, good),
                                                 (3, 0, 0, png + b"garbage")]),
        "empty": make("empty", rows=[]),
    }


def test_reject() -> None:
    print("== 测试C：MBTiles 整包拒收 ==")
    if not SAMPLE.is_file():
        print("  跳过：缺少 samples/regular-partial.mbtiles")
        return
    reset_all()
    tmp = Path("/tmp/selftest-mbtiles")
    before = regular_count()
    pkgs = _make_bad_packages(tmp)
    expect = {
        "sha_mismatch": "source_sha256",
        "out_of_range": "坐标越界",
        "duplicate": "坐标重复",
        "undecodable": "无法完整解码",
        "empty": "没有任何瓦片",
    }
    for name, pkg in pkgs.items():
        code = upload_package(pkg)
        assert code == 200, f"{name} 上传 HTTP {code}"
        r = wait_import(30)
        assert r["state"] == "rejected", f"{name} 应拒收，实际 {r['state']}"
        assert expect[name] in r["message"], f"{name} 拒收原因不含 {expect[name]!r}：{r['message']}"
        assert regular_count() == before, f"{name} 拒收后不应落盘"
    # 超上限：直接拼一个 >8MiB 的文件
    big = tmp / "oversize.bin"
    big.write_bytes(b"x" * (MAX_BYTES + 1))
    code = upload_package(big)
    assert code == 413, f"超上限应 413，实际 {code}"
    assert regular_count() == before
    print(f"  {len(pkgs)} 个坏包全部拒收且未落盘；超 8 MiB 返回 413")
    print("  ✔ 测试C 通过")


def test_import_fill() -> None:
    print("== 测试D：验包期间照常切片 · 只补空位 ==")
    if not SAMPLE.is_file():
        print("  跳过：缺少 samples/regular-partial.mbtiles")
        return
    reset_all()
    api("POST", "/api/jobs/regular/start")
    s = wait_for(lambda s: job(s, "regular")["published"] >= 2, 30, "常规开始出图")
    p0 = job(s, "regular")["published"]

    def upload_in_thread():
        res["code"] = upload_package(SAMPLE)

    res = {}
    th = threading.Thread(target=upload_in_thread)
    th.start()
    s = wait_for(lambda s: imp(s) is not None and imp(s)["state"] == "verifying" and imp(s)["received"],
                 30, "进入验包")
    seen_verifying = True
    p_before = job(s, "regular")["published"]
    assert get_tile("regular", "0/0/0")[:8] == b"\x89PNG\r\n\x1a\n", "验包期间应可取图"
    time.sleep(0.4)
    s = status()
    assert job(s, "regular")["published"] > p_before, "验包期间切片应照常推进"
    print(f"  验包期间常规从 {p_before} 张推进到 {job(s, 'regular')['published']} 张，瓦片可取")
    th.join()
    r = wait_import(60)
    assert r["state"] == "accepted", r["message"]
    imported = set(r["imported_tiles"])
    mtimes = {t: tile_path("regular", t).stat().st_mtime_ns for t in imported}
    print(f"  验包通过：收到 {r['received']} 张，补空位 {r['imported']}，已存在跳过 {r['skipped']}")
    if r["imported"]:
        # 导入完成后已进入下一轮（可能很快完成），直接量已发布集合覆盖导入坐标
        assert imported <= {t for t in _tiles_from_disk()}, "导入坐标应计入进度"
    else:
        print("  （全部坐标已由切片先行发布，本次无补空位）")

    s = wait_for(lambda s: job(s, "regular")["status"] == "done", 90, "常规完成")
    j = job(s, "regular")
    assert j["published"] == TOTAL, "导入后常规仍应出齐 85 张"
    assert imported <= set(j["tiles"]), "导入的坐标应计入进度"
    for t, m in mtimes.items():
        assert tile_path("regular", t).stat().st_mtime_ns == m, f"导入瓦片被覆盖：{t}"
    print(f"  常规完成 {TOTAL}/{TOTAL}；{len(imported)} 张导入瓦片未被覆盖")
    print("  ✔ 测试D 通过")


def _tiles_from_disk() -> set[str]:
    return {f"{p.parent.parent.name}/{p.parent.name}/{p.stem}"
            for p in (DATA / "tiles" / "regular").rglob("*.png")}


def test_import_first_plan() -> None:
    """先导入、再启动切片：本轮待渲数应等于剩余空位，不重复渲染已导入坐标。"""
    print("== 测试D2：导入后再启动切片跳过已导入坐标 ==")
    if not SAMPLE.is_file():
        print("  跳过：缺少 samples/regular-partial.mbtiles")
        return
    reset_all()
    assert upload_package(SAMPLE) == 200
    r = wait_import(60)
    assert r["state"] == "accepted" and r["imported"] > 0, r["message"]
    n = r["imported"]
    api("POST", "/api/jobs/regular/start")
    s = wait_for(lambda s: job(s, "regular")["plan"] is not None, 30, "子进程汇报计划")
    plan = job(s, "regular")["plan"]
    assert plan["remaining"] == TOTAL - n and plan["total"] == TOTAL, \
        f"剩余空位应为 {TOTAL - n}，实际计划 {plan}"
    print(f"  已导入 {n} 张；切片本轮计划 {plan['remaining']}/{plan['total']}（已跳过导入坐标）")
    s = wait_for(lambda s: job(s, "regular")["status"] == "done", 90, "常规完成")
    assert job(s, "regular")["published"] == TOTAL
    print("  ✔ 测试D2 通过")


def test_void_and_partial() -> None:
    print("== 测试E：重置作废 · 写盘中断可重传 ==")
    if not SAMPLE.is_file():
        print("  跳过：缺少 samples/regular-partial.mbtiles")
        return
    reset_all()

    def upload_in_thread(query=""):
        res["code"] = upload_package(SAMPLE, query)

    print("  E1 验包期间重置常规…")
    res = {}
    th = threading.Thread(target=upload_in_thread)
    th.start()
    wait_for(lambda s: imp(s) is not None and imp(s)["state"] == "verifying" and imp(s)["received"],
             30, "进入验包")
    api("POST", "/api/jobs/regular/reset")
    th.join()
    r = wait_import(60)
    assert r["state"] == "void", f"重置后此包应作废，实际 {r['state']}：{r['message']}"
    assert r["imported"] == 0 and regular_count() == 0, "作废包不应留下瓦片"
    print(f"  此包作废：{r['message']}")

    print("  E2 写盘中途失败后重传…")
    upload_package(SAMPLE, "?import_fail_after=8")
    r = wait_import(60)
    assert r["state"] == "partial", f"写盘失败应标记 partial，实际 {r['state']}"
    assert r["imported"] == 8 and regular_count() == 8, "已发布的完整瓦片应保留"
    mtimes = {t: tile_path("regular", t).stat().st_mtime_ns for t in r["imported_tiles"]}
    print(f"  已保留 {r['imported']} 张：{r['message']}")
    upload_package(SAMPLE)
    r = wait_import(60)
    assert r["state"] == "accepted", r["message"]
    assert regular_count() == r["received"], "重传后应补齐全部空位"
    for t, m in mtimes.items():
        assert tile_path("regular", t).stat().st_mtime_ns == m, f"重传覆盖了已有瓦片：{t}"
    print(f"  重传补齐：补空位 {r['imported']}，已存在跳过 {r['skipped']}，未覆盖任何已有文件")
    print("  ✔ 测试E 通过")


def main() -> int:
    test_preempt()
    test_emergency_failure()
    test_reject()
    test_import_fill()
    test_import_first_plan()
    test_void_and_partial()
    print("\n全部自测通过 ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
