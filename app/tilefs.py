"""瓦片发布的小工具：写入临时文件后以"不覆盖"方式原子发布。

发布语义：
- 目标已存在 => 返回 False（先发布的文件为准，调用方不得覆盖）；
- 目标不存在 => 原子地让目标出现（优先硬链接，失败时退化为独占创建拷贝）；
- 目标路径上只可能出现完整文件，读者不会读到半成品。
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path


def link_or_copy(tmp: Path, final: Path) -> bool:
    """把已写完整的 tmp 发布到 final；final 已存在时返回 False，且不覆盖。"""
    try:
        os.link(tmp, final)  # 原子；目标已存在时抛 FileExistsError
        return True
    except FileExistsError:
        return False
    except OSError:
        # 个别网络/容器卷不支持硬链接：退化为独占创建（xb）后拷贝。
        # 任何失败都会删掉半成品，避免在目标路径留下不完整瓦片。
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
