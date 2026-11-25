"""命令行入口：uvicorn app.main:app 的包装，固定单进程。"""
from __future__ import annotations

import os


def main() -> None:
    import uvicorn

    for var in ("WEB_CONCURRENCY", "UVICORN_WORKERS"):
        val = os.environ.get(var, "").strip()
        if val not in ("", "1"):
            raise SystemExit(f"调度状态在内存中，只允许单进程运行（{var}={val} 被拒绝）")
    uvicorn.run(
        "app.main:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
    )


if __name__ == "__main__":
    main()
