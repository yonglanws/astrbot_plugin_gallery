"""画廊核心数据模型与管理器。

移植自 lunabot 的画廊服务，适配 AstrBot 的持久化规范：
- 数据存储于 data/plugin_data/astrbot_plugin_gallery/ 下
- gallery.json 只存相对文件名，运行时用 data_dir 拼路径（可跨 Windows/Linux/Docker）
- 修复了原版 async_reload_gall 重复加载、gall_replace force 失效、
  时间格式 %-S 跨平台、历史 id 复用等问题
- 使用 asyncio.to_thread 将 CPU 密集的哈希/缩略图计算移出事件循环
"""

from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING

import numpy as np
from PIL import Image

from astrbot.api import logger

if TYPE_CHECKING:
    from .image_utils import ImageProcessor


# ==================== 常量 ====================

THUMBNAIL_SIZE = (64, 64)
THUMBNAIL_BG_COLOR = (230, 240, 255, 255)
PIC_EXTS = (".jpg", ".jpeg", ".png", ".gif")

# 画廊元数据文件名
GALLERY_DB_FILE = "gallery.json"
# 图片目录相对 data_dir 的位置
PICS_SUBDIR = "pics"


def _filename_only(value: str | None) -> str:
    """从绝对路径或文件名中取出纯文件名。"""
    if not value:
        return ""
    return os.path.basename(value.replace("\\", "/"))


class GalleryMode(Enum):
    """画廊访问模式。

    - Edit: 允许上传和删除（默认）
    - View: 只允许查看图片
    - Off: 对普通用户关闭
    """

    Edit = "edit"
    View = "view"
    Off = "off"


# ==================== 数据模型 ====================


@dataclass
class GalleryPic:
    """画廊中的一张图片。

    JSON 只持久化 file（纯文件名）；path / thumb_path 是运行时根据
    data_dir + pics/<画廊名>/ 拼出来的绝对路径，不写入磁盘。
    """

    gall_name: str
    pid: int
    file: str
    hash1: str = None  # 64 位感知哈希，十六进制字符串
    hash2: str = None  # 16x16 灰度像素 hex，用于 MAE 精筛
    _data_dir: str = field(default="", repr=False, compare=False)

    @property
    def path(self) -> str:
        return os.path.join(self._data_dir, PICS_SUBDIR, self.gall_name, self.file)

    @path.setter
    def path(self, value: str) -> None:
        self.file = _filename_only(value)

    @property
    def thumb_path(self) -> str | None:
        if not self.file:
            return None
        return os.path.join(
            self._data_dir, PICS_SUBDIR, self.gall_name, f"{self.file}_thumb.jpg"
        )

    @thumb_path.setter
    def thumb_path(self, value: str | None) -> None:
        # 缩略图路径由 file 推导，外部置空只表示尚未生成，不影响 JSON
        return

    @classmethod
    def load(cls, data: dict, gall_name: str, data_dir: str) -> "GalleryPic":
        raw = data.get("file") or data.get("path") or ""
        return cls(
            gall_name=data.get("gall_name") or gall_name,
            pid=data["pid"],
            file=_filename_only(raw),
            hash1=data.get("hash1"),
            hash2=data.get("hash2"),
            _data_dir=data_dir,
        )

    def to_dict(self) -> dict:
        return {
            "pid": self.pid,
            "file": self.file,
            "hash1": self.hash1,
            "hash2": self.hash2,
        }

    def calc_hash(self, src_path: str | None = None) -> None:
        """计算感知哈希 hash1 与像素哈希 hash2。

        透明通道会先合成到白色背景上，避免透明区域被当作黑色影响哈希。
        src_path 用于入库前对源文件算哈希（此时 self.path 还不存在）。
        """
        image = Image.open(src_path or self.path)
        # 透明图先合成到纯白背景上
        if image.mode in ("RGBA", "LA") or (
            image.mode == "P" and "transparency" in image.info
        ):
            image = image.convert("RGBA").resize((64, 64), Image.Resampling.BILINEAR)
            bg = Image.new("RGBA", image.size, (255, 255, 255, 255))
            bg.alpha_composite(image)
            image = bg
        image = image.convert("RGB")
        image = image.resize((16, 16), Image.Resampling.BILINEAR).convert("L")
        # hash2: 16x16 灰度像素，用于 MAE 精筛
        self.hash2 = image.tobytes().hex()
        # hash1: 8x8 感知哈希，64 bit
        image = image.resize((8, 8), Image.Resampling.BILINEAR)
        pixels = np.array(image).flatten()
        avg = pixels.mean()
        bits = 0
        for idx, p in enumerate(pixels):
            if p >= avg:
                bits |= 1 << (63 - idx)
        self.hash1 = f"{bits:016x}"

    def is_same(self, other: "GalleryPic", hash1_threshold: int, hash2_threshold: int) -> bool:
        """两级相似判定：hash1 汉明距离粗筛 + hash2 MAE 精筛。"""
        # hash1 快速排除明显不同的图
        if (int(self.hash1, 16) ^ int(other.hash1, 16)).bit_count() > hash1_threshold:
            return False
        # hash2 精确判定
        img1 = np.frombuffer(bytes.fromhex(self.hash2), dtype=np.uint8)
        img2 = np.frombuffer(bytes.fromhex(other.hash2), dtype=np.uint8)
        diff = int(np.sum(np.abs(img1.astype(np.int16) - img2.astype(np.int16))))
        return diff <= hash2_threshold

    def ensure_thumb(self) -> None:
        """生成缩略图（如不存在）。失败时仅记日志，不影响 file。"""
        try:
            if not os.path.exists(self.thumb_path):
                img = Image.open(self.path).convert("RGBA")
                img.thumbnail(THUMBNAIL_SIZE, Image.Resampling.LANCZOS)
                thumb = Image.new("RGBA", img.size, THUMBNAIL_BG_COLOR)
                thumb.alpha_composite(img)
                thumb.convert("RGB").save(
                    self.thumb_path, format="JPEG", optimize=True, quality=85
                )
        except Exception as e:
            logger.warning(f"生成画廊图片 {self.pid} 缩略图失败: {e}")


class GalleryPicRepeatedException(Exception):
    """上传图片与画廊中已有图片重复。"""

    def __init__(self, pid: int):
        super().__init__(f"画廊中已存在相似图片(pid={pid})")
        self.pid = pid


@dataclass
class Gallery:
    """一个画廊。pics_dir 运行时由 data_dir 推导，不写入 JSON。"""

    name: str
    aliases: list[str]
    mode: GalleryMode
    cover_pid: int | None = None
    pics: list[GalleryPic] = field(default_factory=list)
    _data_dir: str = field(default="", repr=False, compare=False)

    @property
    def pics_dir(self) -> str:
        return os.path.join(self._data_dir, PICS_SUBDIR, self.name)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "aliases": self.aliases,
            "mode": self.mode.value,
            "cover_pid": self.cover_pid,
            "pics": [p.to_dict() for p in self.pics],
        }


# ==================== 画廊管理器 ====================


class GalleryManager:
    """画廊单例管理器，负责持久化与所有画廊/图片操作。"""

    def __init__(self, data_dir: str, img_proc: "ImageProcessor"):
        self._data_dir = data_dir
        self._db_path = os.path.join(data_dir, GALLERY_DB_FILE)
        self._img_proc = img_proc
        self.pid_top = 0
        self.galleries: dict[str, Gallery] = {}
        self._lock = asyncio.Lock()  # 保护写操作的串行化
        self._loaded = False

    # ---------- 持久化 ----------

    def _load(self) -> None:
        import json

        if os.path.exists(self._db_path):
            try:
                with open(self._db_path, "r", encoding="utf-8") as f:
                    db = json.load(f)
            except Exception as e:
                logger.error(f"读取画廊数据库失败，将重建: {e}")
                db = {}
        else:
            db = {}
        self.pid_top = db.get("pid_top", 0)
        self.galleries = {}
        for name, g in db.get("galleries", {}).items():
            gall_name = g.get("name") or name
            self.galleries[name] = Gallery(
                name=gall_name,
                aliases=g.get("aliases", []),
                cover_pid=g.get("cover_pid"),
                mode=GalleryMode(g.get("mode", "edit")),
                pics=[
                    GalleryPic.load(p, gall_name, self._data_dir)
                    for p in g.get("pics", [])
                ],
                _data_dir=self._data_dir,
            )
        logger.info(f"成功加载 {len(self.galleries)} 个画廊, pid_top={self.pid_top}")

    def _save_sync(self) -> None:
        import json

        db = {
            "pid_top": self.pid_top,
            "galleries": {name: g.to_dict() for name, g in self.galleries.items()},
        }
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        tmp = self._db_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False, indent=2)
        # 原子写，防止中途崩溃损坏数据库
        os.replace(tmp, self._db_path)

    async def _save(self) -> None:
        await asyncio.to_thread(self._save_sync)

    def ensure_loaded(self) -> None:
        """首次使用时加载数据库（惰性加载）。"""
        if not self._loaded:
            self._load()
            self._loaded = True

    # ---------- 名称校验 ----------

    @staticmethod
    def _check_name(name: str) -> bool:
        """画廊名/别名合法性校验（避免文件名注入与歧义）。"""
        if not name or len(name) > 32:
            return False
        if any(c in name for c in r'\/:*?"<>| '):
            return False
        if name.isdigit():
            return False
        return True

    # ---------- 画廊查询 ----------

    def get_all_galls(self) -> dict[str, Gallery]:
        return self.galleries

    def find_gall(
        self, name_or_alias: str, raise_if_nofound: bool = False
    ) -> Gallery | None:
        """通过名称或别名查找画廊。"""
        for g in self.galleries.values():
            if g.name == name_or_alias or name_or_alias in g.aliases:
                return g
        if raise_if_nofound:
            if not name_or_alias:
                raise ReplyException("画廊名称不能为空")
            raise ReplyException(f'画廊"{name_or_alias}"不存在')
        return None

    # ---------- 画廊增删改 ----------

    def open_gall(self, name: str) -> None:
        assert self._check_name(name), f'画廊名称"{name}"无效'
        assert self.find_gall(name) is None, f'画廊"{name}"已存在'
        g = Gallery(
            name=name,
            aliases=[],
            mode=GalleryMode.Edit,
            pics=[],
            _data_dir=self._data_dir,
        )
        os.makedirs(g.pics_dir, exist_ok=True)
        self.galleries[name] = g

    def close_gall(self, name_or_alias: str) -> None:
        g = self.find_gall(name_or_alias, raise_if_nofound=True)
        self.galleries.pop(g.name)
        # 尝试删除图片目录
        try:
            if os.path.isdir(g.pics_dir):
                shutil.rmtree(g.pics_dir, ignore_errors=True)
        except Exception as e:
            logger.warning(f'删除画廊 {g.name} 目录失败: {e}')

    def add_gall_alias(self, name_or_alias: str, alias: str) -> None:
        assert self._check_name(alias), f'别名"{alias}"无效'
        g = self.find_gall(name_or_alias, raise_if_nofound=True)
        assert self.find_gall(alias) is None, f'别名"{alias}"已被占用'
        g.aliases.append(alias)

    def del_gall_alias(self, name_or_alias: str, alias: str) -> None:
        g = self.find_gall(name_or_alias, raise_if_nofound=True)
        assert alias in g.aliases, f'别名"{alias}"不存在'
        g.aliases.remove(alias)

    def change_gall_mode(
        self, name_or_alias: str, mode: GalleryMode
    ) -> tuple[GalleryMode, GalleryMode]:
        g = self.find_gall(name_or_alias, raise_if_nofound=True)
        old_mode = g.mode
        g.mode = mode
        return old_mode, g.mode

    def set_cover_pic(self, name_or_alias: str, pid: int) -> None:
        g = self.find_gall(name_or_alias, raise_if_nofound=True)
        p = self.find_pic(pid, raise_if_nofound=True)
        assert p.gall_name == g.name, f'图片pid={pid}不属于画廊"{g.name}"'
        g.cover_pid = pid

    # ---------- 图片查询 ----------

    def find_pic(self, pid: int, raise_if_nofound: bool = False) -> GalleryPic | None:
        """通过图片ID查找图片。pid 可为负数，表示全局倒数第 |pid| 张。"""
        if pid < 0:
            pids = []
            for g in self.galleries.values():
                for p in g.pics:
                    pids.append(p.pid)
            pids.sort()
            if pid < -len(pids):
                if raise_if_nofound:
                    raise ReplyException(f"画廊仅有{len(pids)}张图片")
                return None
            pid = pids[pid]
        for g in self.galleries.values():
            for p in g.pics:
                if p.pid == pid:
                    return p
        if raise_if_nofound:
            raise ReplyException(f"画廊图片pid={pid}不存在")
        return None

    # ---------- 图片增删改 ----------

    async def _check_duplicated(
        self, pic: GalleryPic, gallery: Gallery, h1: int, h2: int
    ) -> int | None:
        def check() -> int | None:
            for p in gallery.pics:
                if pic.is_same(p, h1, h2):
                    return p.pid
            return None

        return await asyncio.to_thread(check)

    async def async_add_pic(
        self,
        name_or_alias: str,
        img_path: str,
        check_duplicated: bool = True,
        h1: int = 5,
        h2: int = 1000,
    ) -> int:
        """向画廊添加一张图片，拷贝 img_path 到画廊目录，返回图片 ID。"""
        async with self._lock:
            g = self.find_gall(name_or_alias, raise_if_nofound=True)
            pic = GalleryPic(
                gall_name=g.name,
                pid=self.pid_top + 1,
                file=_filename_only(img_path),
                _data_dir=self._data_dir,
            )
            await asyncio.to_thread(pic.calc_hash, img_path)

            if check_duplicated:
                if sim_pid := await self._check_duplicated(pic, g, h1, h2):
                    raise GalleryPicRepeatedException(sim_pid)

            self.pid_top += 1
            _, ext = os.path.splitext(os.path.basename(img_path))
            # 使用跨平台安全的时间格式（修复原版 %-S 在 Windows 不可用）
            time_str = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            dst_path = os.path.join(g.pics_dir, f"{time_str}_{self.pid_top}{ext}")
            await asyncio.to_thread(shutil.copy2, img_path, dst_path)

            pic.path = dst_path
            g.pics.append(pic)
            await asyncio.to_thread(pic.ensure_thumb)
            await self._save()
            return self.pid_top

    async def async_replace_pic(
        self,
        pid: int,
        img_path: str,
        check_duplicated: bool = True,
        h1: int = 5,
        h2: int = 1000,
    ) -> int:
        """替换画廊中的一张图片，返回图片 ID。"""
        async with self._lock:
            p = self.find_pic(pid, raise_if_nofound=True)
            g = self.find_gall(p.gall_name, raise_if_nofound=True)

            new_pic = GalleryPic(
                gall_name=g.name,
                pid=p.pid,
                file=_filename_only(img_path),
                _data_dir=self._data_dir,
            )
            await asyncio.to_thread(new_pic.calc_hash, img_path)

            if check_duplicated:
                if sim_pid := await self._check_duplicated(new_pic, g, h1, h2):
                    # 与自身相同允许替换
                    if sim_pid != pid:
                        raise GalleryPicRepeatedException(sim_pid)

            # 删除旧文件
            try:
                if os.path.exists(p.path):
                    os.remove(p.path)
                if p.thumb_path and os.path.exists(p.thumb_path):
                    os.remove(p.thumb_path)
            except Exception as e:
                logger.warning(f"删除画廊图片 {pid} 文件失败: {get_exc_desc(e)}")

            _, ext = os.path.splitext(os.path.basename(img_path))
            time_str = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            dst_path = os.path.join(g.pics_dir, f"{time_str}_{p.pid}{ext}")
            await asyncio.to_thread(shutil.copy2, img_path, dst_path)

            p.path = dst_path
            p.hash1 = new_pic.hash1
            p.hash2 = new_pic.hash2
            p.thumb_path = None
            await asyncio.to_thread(p.ensure_thumb)
            await self._save()
            return p.pid

    def del_pic(self, pid: int) -> int:
        """删除画廊中的一张图片，返回被删除的图片 ID（同步，需持有 _lock 调用）。"""
        p = self.find_pic(pid, raise_if_nofound=True)
        g = self.find_gall(p.gall_name, raise_if_nofound=True)
        g.pics.remove(p)
        try:
            if os.path.exists(p.path):
                os.remove(p.path)
            if p.thumb_path and os.path.exists(p.thumb_path):
                os.remove(p.thumb_path)
        except Exception as e:
            logger.warning(f"删除画廊图片 {pid} 文件失败: {get_exc_desc(e)}")
        return p.pid

    async def async_del_pic(self, pid: int) -> int:
        """异步删除图片（加锁 + 落库）。"""
        async with self._lock:
            deleted = self.del_pic(pid)
            await self._save()
            return deleted

    async def async_reload_gall(
        self, name_or_alias: str
    ) -> tuple[list[int], list[int]]:
        """从画廊图片目录重新加载，返回(新增图片 pids, 失效图片 pids)。

        修复原版 bug：原代码 continue 只跳过内层循环，导致所有图片被重复加载。
        这里先用已加载路径集合判断是否已存在。
        """
        import glob

        async with self._lock:
            g = self.find_gall(name_or_alias, raise_if_nofound=True)
            new_pids: list[int] = []
            del_pids: list[int] = []

            # 已加载的图片绝对路径集合，用于跳过
            existing_paths = {
                os.path.abspath(p.path) for p in g.pics if os.path.exists(p.path)
            }

            for file in glob.glob(os.path.join(g.pics_dir, "*")):
                if "_thumb" in file:
                    continue
                if os.path.abspath(file) in existing_paths:
                    continue  # 已加载，跳过
                _, ext = os.path.splitext(os.path.basename(file))
                if ext.lower() not in PIC_EXTS:
                    continue
                self.pid_top += 1
                pic = GalleryPic(
                    gall_name=g.name,
                    pid=self.pid_top,
                    file=_filename_only(file),
                    _data_dir=self._data_dir,
                )
                await asyncio.to_thread(pic.calc_hash)
                g.pics.append(pic)
                new_pids.append(pic.pid)

            # 检查失效的图片（文件已不在磁盘上）
            for pic in g.pics[:]:
                if not os.path.exists(pic.path):
                    g.pics.remove(pic)
                    del_pids.append(pic.pid)

            await self._save()
            return new_pids, del_pids

    async def async_check_gallery(
        self, name_or_alias: str, rehash: bool
    ) -> dict[int, list[int]]:
        """重新检查画廊重复图片，返回 {首个图片 id: 重复图片 id 列表}。"""
        async with self._lock:
            g = self.find_gall(name_or_alias, raise_if_nofound=True)

            def check() -> dict[int, list[int]]:
                # ret[pid] = (首个图, 重复的后续图片列表)
                ret: dict[int, tuple[GalleryPic, list[GalleryPic]]] = {}
                for pic in g.pics[:]:
                    if rehash:
                        pic.calc_hash()
                    sim_pid = None
                    for k, (first_pic, _) in ret.items():
                        if pic.is_same(
                            first_pic,
                            self._img_proc.hash1_threshold,
                            self._img_proc.hash2_threshold,
                        ):
                            sim_pid = k
                            break
                    if sim_pid is not None:
                        ret[sim_pid][1].append(pic)
                    else:
                        ret[pic.pid] = (pic, [])
                return {k: [p.pid for p in v[1]] for k, v in ret.items() if v[1]}

            result = await asyncio.to_thread(check)
            if rehash:
                await self._save()
            return result


# ==================== 辅助异常与函数 ====================


class ReplyException(Exception):
    """用于将用户可见的错误信息冒泡到指令处理层。"""


def get_exc_desc(e: Exception) -> str:
    """获取异常的简短描述，用于日志。"""
    return f"{type(e).__name__}: {e}"
