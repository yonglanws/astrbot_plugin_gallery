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
import tempfile
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


def _is_valid_pid(value: object) -> bool:
    """图片 id 必须是正整数（bool 是 int 子类，需排除）。"""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


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
        # 解析成真实路径，并要求它正好是 data/pics/<name>（父目录必须是 pics/），
        # 防止旧数据里的 "." / ".." / 带分隔符的名字把读写引到插件数据目录甚至更外层。
        root = os.path.realpath(os.path.join(self._data_dir, PICS_SUBDIR))
        path = os.path.realpath(os.path.join(root, self.name))
        assert os.path.dirname(path) == root, f'画廊"{self.name}"的图片目录越界: {path}'
        return path

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

        self.galleries = {}
        self.pid_top = 0

        if not os.path.exists(self._db_path):
            logger.info("画廊数据库不存在，使用空库")
            return

        try:
            with open(self._db_path, "r", encoding="utf-8") as f:
                db = json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            # 解析失败不能静默变空库：先把坏文件改名留档，避免下一次 _save 覆盖唯一副本
            backup = quarantine_corrupt_file(self._db_path, "画廊数据库")
            logger.error(
                f"画廊数据库损坏，已备份为 {backup or '(备份失败)'}，以空库启动: {e}"
            )
            return
        except Exception as e:
            logger.error(
                f"读取画廊数据库失败，以空库启动（保留原文件）: {get_exc_desc(e)}"
            )
            return

        # 结构解析同样整体兜底：任何意外都不能让 initialize 失败
        try:
            if not isinstance(db, dict):
                raise ValueError(f"数据库根节点应为对象，实际为 {type(db).__name__}")

            raw_pid_top = db.get("pid_top", 0)
            if (
                isinstance(raw_pid_top, int)
                and not isinstance(raw_pid_top, bool)
                and raw_pid_top >= 0
            ):
                self.pid_top = raw_pid_top
            else:
                logger.warning(f"画廊数据库 pid_top={raw_pid_top!r} 非法，重置为 0")

            raw_galls = db.get("galleries", {})
            if not isinstance(raw_galls, dict):
                raise ValueError("galleries 字段应为对象")

            max_pid = 0
            for key, raw_g in raw_galls.items():
                gallery = self._load_gall_record(str(key), raw_g)
                if gallery is None:
                    continue
                if gallery.name in self.galleries:
                    logger.warning(f'画廊名称"{gallery.name}"重复，后者覆盖前者')
                self.galleries[gallery.name] = gallery
                for pic in gallery.pics:
                    if pic.pid > max_pid:
                        max_pid = pic.pid

            # pid_top 必须大于所有已加载图片的 pid，否则新图会和旧图撞 id
            if max_pid > self.pid_top:
                if self.pid_top:
                    logger.warning(
                        f"pid_top={self.pid_top} 小于已加载的最大图片 pid={max_pid}，已修正"
                    )
                self.pid_top = max_pid

            logger.info(f"成功加载 {len(self.galleries)} 个画廊, pid_top={self.pid_top}")
        except Exception as e:
            logger.error(
                f"解析画廊数据库失败，保留已加载的 {len(self.galleries)} 个画廊: "
                f"{get_exc_desc(e)}"
            )

    def _load_gall_record(self, key: str, raw: object) -> "Gallery | None":
        """解析单条画廊记录；结构或 mode 非法时跳过该记录并告警。"""
        if not isinstance(raw, dict):
            logger.warning(f'跳过非法的画廊记录 "{key}": 不是对象')
            return None

        name = raw.get("name") or key
        if not isinstance(name, str) or not name:
            logger.warning(f'跳过非法的画廊记录 "{key}": 名称为空')
            return None

        try:
            mode = GalleryMode(raw.get("mode", "edit"))
        except (TypeError, ValueError):
            logger.warning(f'跳过画廊 "{name}": mode={raw.get("mode")!r} 非法')
            return None

        aliases = raw.get("aliases", [])
        if not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases):
            logger.warning(f'画廊 "{name}" 的 aliases 非法，已忽略')
            aliases = []

        cover_pid = raw.get("cover_pid")
        if cover_pid is not None and not _is_valid_pid(cover_pid):
            logger.warning(f'画廊 "{name}" 的 cover_pid={cover_pid!r} 非法，已忽略')
            cover_pid = None

        raw_pics = raw.get("pics", [])
        if not isinstance(raw_pics, list):
            logger.warning(f'画廊 "{name}" 的 pics 字段非法，按空画廊处理')
            raw_pics = []

        pics: list[GalleryPic] = []
        for idx, raw_pic in enumerate(raw_pics):
            pic = self._load_pic_record(name, raw_pic)
            if pic is None:
                logger.warning(f'画廊 "{name}" 跳过非法图片记录 #{idx}: {raw_pic!r}')
                continue
            pics.append(pic)

        if not self._check_name(name):
            # 旧数据可能含 "." / ".." 等非法名称：允许加载查看，
            # 但 open/close 等会改动目录的操作会被拒绝。
            logger.warning(f'画廊 "{name}" 名称非法（旧数据），已加载但禁止新建/删除目录')

        return Gallery(
            name=name,
            aliases=aliases,
            mode=mode,
            cover_pid=cover_pid,
            pics=pics,
            _data_dir=self._data_dir,
        )

    def _load_pic_record(self, gall_name: str, raw: object) -> "GalleryPic | None":
        """解析单条图片记录；缺 pid / 文件名为空等非法记录返回 None。"""
        if not isinstance(raw, dict):
            return None

        pid = raw.get("pid")
        if not _is_valid_pid(pid):
            return None

        raw_file = raw.get("file")
        if not isinstance(raw_file, str) or not raw_file:
            raw_file = raw.get("path")
        if not isinstance(raw_file, str):
            return None
        file = _filename_only(raw_file)
        if not file:
            return None

        raw_gall = raw.get("gall_name") or gall_name
        if not isinstance(raw_gall, str):
            return None

        hash1 = raw.get("hash1")
        if hash1 is not None and not isinstance(hash1, str):
            logger.warning(f"画廊图片 pid={pid} 的 hash1 非法，已重置")
            hash1 = None
        hash2 = raw.get("hash2")
        if hash2 is not None and not isinstance(hash2, str):
            logger.warning(f"画廊图片 pid={pid} 的 hash2 非法，已重置")
            hash2 = None

        return GalleryPic(
            gall_name=raw_gall,
            pid=pid,
            file=file,
            hash1=hash1,
            hash2=hash2,
            _data_dir=self._data_dir,
        )

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
        """画廊名/别名合法性校验（避免文件名注入与目录穿越）。"""
        if not isinstance(name, str) or not name or len(name) > 32:
            return False
        # "." / ".." 以及首尾带点：Windows 会吞掉结尾的点，且 "."/".." 会指向父目录
        if name in (".", "..") or name.startswith(".") or name.endswith("."):
            return False
        if any(c in name for c in r'\/:*?"<>| '):
            return False
        # 控制字符（换行、NUL 等）会让文件名/日志不可控
        if any(ord(c) < 0x20 or ord(c) == 0x7F for c in name):
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

    def _safe_pics_dir(self, name: str) -> str | None:
        """返回 data/pics/<name> 的真实路径；名称非法或目录越界时返回 None。"""
        if not self._check_name(name):
            return None
        root = os.path.realpath(os.path.join(self._data_dir, PICS_SUBDIR))
        target = os.path.realpath(os.path.join(root, name))
        if os.path.dirname(target) != root or os.path.basename(target) != name:
            return None
        return target

    def close_gall(self, name_or_alias: str) -> None:
        g = self.find_gall(name_or_alias, raise_if_nofound=True)
        # 删除前再次独立校验（不依赖 pics_dir 属性）：realpath 必须正好是
        # data/pics/<name>，否则旧数据里的 "." / ".." 会删掉整个 pics 甚至数据目录。
        target = self._safe_pics_dir(g.name)
        if target is None:
            logger.warning(
                f'画廊"{g.name}"名称非法，跳过删除图片目录（仅移除画廊记录）'
            )
        else:
            try:
                if os.path.isdir(target):
                    shutil.rmtree(target)
            except Exception as e:
                logger.warning(f'删除画廊 {g.name} 目录失败: {e}')
        self.galleries.pop(g.name, None)

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

            # 旧图不能先删：先拷到画廊目录内的临时文件，确认成功后再原子替换目标名。
            # 任何一步失败都清理临时文件并保留旧图。
            old_path = p.path
            old_thumb = p.thumb_path
            _, ext = os.path.splitext(os.path.basename(img_path))
            time_str = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            dst_path = os.path.join(g.pics_dir, f"{time_str}_{p.pid}{ext}")

            fd, tmp_path = await asyncio.to_thread(
                tempfile.mkstemp,
                prefix=f".replace-{p.pid}-",
                suffix=".tmp",
                dir=g.pics_dir,
            )
            os.close(fd)
            try:
                await asyncio.to_thread(shutil.copy2, img_path, tmp_path)
                if not os.path.exists(tmp_path) or os.path.getsize(tmp_path) <= 0:
                    raise OSError("拷贝替换图片后临时文件不存在或为空")
                await asyncio.to_thread(os.replace, tmp_path, dst_path)
            except Exception:
                try:
                    if os.path.exists(tmp_path):
                        await asyncio.to_thread(os.remove, tmp_path)
                except Exception as e:
                    logger.warning(
                        f"清理替换临时文件 {tmp_path} 失败: {get_exc_desc(e)}"
                    )
                raise

            # 新图已落盘，才更新内存、重建缩略图并保存
            p.path = dst_path
            p.hash1 = new_pic.hash1
            p.hash2 = new_pic.hash2
            # 旧缩略图必须先删掉；新旧文件名相同时否则会留下旧缩略图
            try:
                if old_thumb and os.path.exists(old_thumb):
                    os.remove(old_thumb)
            except Exception as e:
                logger.warning(f"删除画廊图片 {pid} 旧缩略图失败: {get_exc_desc(e)}")
            await asyncio.to_thread(p.ensure_thumb)
            await self._save()
            # 落库成功后再删旧图：即使此处失败，JSON 也不会指向已删除的文件
            try:
                if (
                    os.path.abspath(old_path) != os.path.abspath(dst_path)
                    and os.path.exists(old_path)
                ):
                    os.remove(old_path)
            except Exception as e:
                logger.warning(f"删除画廊图片 {pid} 旧图失败: {get_exc_desc(e)}")
            return p.pid

    @staticmethod
    def _remove_pic_files(p: GalleryPic) -> None:
        """删除图片及其缩略图；失败只记 warning，不影响已落库的删除结果。"""
        try:
            if os.path.exists(p.path):
                os.remove(p.path)
            if p.thumb_path and os.path.exists(p.thumb_path):
                os.remove(p.thumb_path)
        except Exception as e:
            logger.warning(f"删除画廊图片 {p.pid} 文件失败: {get_exc_desc(e)}")

    def del_pic(self, pid: int) -> int:
        """删除画廊中的一张图片，返回被删除的图片 ID（同步，需持有 _lock 调用）。

        注意：该接口给 history.py 撤销上传复用，仍是先删文件后由调用方落库；
        需要「落库成功后再删文件」的语义请用 async_del_pics。
        """
        p = self.find_pic(pid, raise_if_nofound=True)
        g = self.find_gall(p.gall_name, raise_if_nofound=True)
        g.pics.remove(p)
        self._remove_pic_files(p)
        return p.pid

    async def async_del_pic(self, pid: int) -> int:
        """异步删除图片（加锁 + 落库，落库成功后才删文件）。"""
        async with self._lock:
            p = self.find_pic(pid, raise_if_nofound=True)
            g = self.find_gall(p.gall_name, raise_if_nofound=True)
            idx = g.pics.index(p)
            g.pics.remove(p)
            try:
                await self._save()
            except Exception:
                # 落库失败：恢复内存，避免内存与 JSON 不一致
                g.pics.insert(idx, p)
                raise
            self._remove_pic_files(p)
            return p.pid

    async def async_del_pics(self, pids: list[int]) -> tuple[list[int], list[int]]:
        """批量删除画廊图片，返回 (成功 pids, 失败 pids)。

        锁内只改内存并落库，_save 成功后才删磁盘文件：即使删文件失败，
        JSON 也不会指向已删除的文件。单张失败不影响其它张（与 main.py
        原先逐张 try/except 的行为一致），失败原因记 warning。
        """
        ok_list: list[int] = []
        err_list: list[int] = []
        async with self._lock:
            # (画廊, 原下标, 图片)，用于落库失败时按原顺序放回
            removed: list[tuple[Gallery, int, GalleryPic]] = []
            for pid in pids:
                try:
                    p = self.find_pic(pid, raise_if_nofound=True)
                    g = self.find_gall(p.gall_name, raise_if_nofound=True)
                    idx = g.pics.index(p)
                    g.pics.remove(p)
                    removed.append((g, idx, p))
                    ok_list.append(p.pid)
                except Exception as e:
                    logger.warning(f"删除画廊图片pid={pid}失败: {get_exc_desc(e)}")
                    err_list.append(pid)
            if not removed:
                return ok_list, err_list
            try:
                await self._save()
            except Exception:
                # 落库失败：恢复内存，避免「内存已删、JSON 仍有记录」
                for g, idx, p in reversed(removed):
                    g.pics.insert(idx, p)
                raise
            # 落库成功后再删文件；删文件失败只记 warning
            for _, _, p in removed:
                self._remove_pic_files(p)
        return ok_list, err_list

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
        """重新检查画廊重复图片，返回 {首个图片 id: 重复图片 id 列表}。

        锁内只取 pics 快照；O(n²) 去重与 rehash（都可能是数十秒级）放在锁外
        （仍在 to_thread 里）执行，避免阻塞上传/删除。写回哈希/落库时再取短锁。
        因为用的是快照，期间并发新增的图片可能漏检，但不会和写操作互相覆盖。
        """
        async with self._lock:
            g = self.find_gall(name_or_alias, raise_if_nofound=True)
            gall_name = g.name
            snapshot = [(p.pid, p.path, p.file, p.hash1, p.hash2) for p in g.pics]

        def check() -> tuple[
            dict[int, list[int]], dict[int, tuple[str, str, str, str]]
        ]:
            h1_threshold = self._img_proc.hash1_threshold
            h2_threshold = self._img_proc.hash2_threshold
            # pid -> (file, 快照时的 hash1, 新 hash1, 新 hash2)
            new_hashes: dict[int, tuple[str, str, str, str]] = {}
            entries: list[tuple[int, str, str, str]] = []  # (pid, file, hash1, hash2)

            for pid, path, file, hash1, hash2 in snapshot:
                if rehash:
                    tmp = GalleryPic(
                        gall_name=gall_name,
                        pid=pid,
                        file=file,
                        _data_dir=self._data_dir,
                    )
                    try:
                        tmp.calc_hash(path)
                    except Exception as e:
                        logger.warning(
                            f"重新计算画廊图片 {pid} hash 失败，跳过: {get_exc_desc(e)}"
                        )
                        continue
                    new_hashes[pid] = (file, hash1, tmp.hash1, tmp.hash2)
                    hash1, hash2 = tmp.hash1, tmp.hash2
                if not hash1 or not hash2:
                    continue
                # 旧数据可能带非法 hash 字符串，先探测再参与比对，避免整次检查中断
                try:
                    int(hash1, 16)
                    bytes.fromhex(hash2)
                except (TypeError, ValueError):
                    logger.warning(f"画廊图片 {pid} 的 hash 非法，跳过查重")
                    continue
                entries.append((pid, file, hash1, hash2))

            # ret[pid] = (首个图, 重复的后续图片列表)
            ret: dict[int, tuple[GalleryPic, list[GalleryPic]]] = {}
            for pid, file, h1, h2 in entries:
                pic = GalleryPic(
                    gall_name=gall_name,
                    pid=pid,
                    file=file,
                    hash1=h1,
                    hash2=h2,
                    _data_dir=self._data_dir,
                )
                sim_pid = None
                for first_pid, (first_pic, _) in ret.items():
                    if pic.is_same(first_pic, h1_threshold, h2_threshold):
                        sim_pid = first_pid
                        break
                if sim_pid is not None:
                    ret[sim_pid][1].append(pic)
                else:
                    ret[pid] = (pic, [])
            return (
                {k: [p.pid for p in v[1]] for k, v in ret.items() if v[1]},
                new_hashes,
            )

        result, new_hashes = await asyncio.to_thread(check)

        if rehash:
            # 短锁写回：只更新快照期间没被并发替换/改动 hash 的图片，
            # 避免覆盖 replace/add 刚写入的新 hash。
            async with self._lock:
                g = self.find_gall(gall_name, raise_if_nofound=False)
                if g is not None:
                    applied = 0
                    for pic in g.pics:
                        rec = new_hashes.get(pic.pid)
                        if (
                            rec is not None
                            and rec[0] == pic.file
                            and rec[1] == pic.hash1
                        ):
                            pic.hash1, pic.hash2 = rec[2], rec[3]
                            applied += 1
                    await self._save()
                    logger.info(
                        f'画廊"{gall_name}"rehash 写回 {applied}/{len(new_hashes)} 张图片的 hash'
                    )
        return result


# ==================== 辅助异常与函数 ====================


class ReplyException(Exception):
    """用于将用户可见的错误信息冒泡到指令处理层。"""


def get_exc_desc(e: Exception) -> str:
    """获取异常的简短描述，用于日志。"""
    return f"{type(e).__name__}: {e}"


def quarantine_corrupt_file(path: str, desc: str = "数据文件") -> str | None:
    """把损坏的 JSON 文件改名为 <原路径>.corrupt-<时间戳>，返回新路径。

    返回 None 表示改名失败（原文件保留，调用方仍以空库启动并记录错误）。
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    dest = f"{path}.corrupt-{stamp}"
    try:
        os.replace(path, dest)
        return dest
    except Exception as e:
        logger.error(f"备份损坏的{desc} {path} 失败: {get_exc_desc(e)}")
        return None
