"""图片处理与渲染工具。

- 处理入库图片：大小限制、静态 gif 转换（保留 QQ 表情格式）
- 使用 draw.py（原版 lunabot 的 Pillow Canvas 布局工具包移植）渲染：
  画廊列表 / 图片网格 / 上传查重对比 / 查重结果，布局写法与原版画廊代码一致
"""

from __future__ import annotations

import asyncio
import math
import os
import time

from PIL import Image, ImageSequence

from astrbot.api import logger

from .draw import (
    BLACK,
    THUMBNAIL_BG_COLOR,
    DEFAULT_BOLD_FONT,
    DEFAULT_FONT,
    Canvas,
    FillBg,
    Grid,
    HSplit,
    ImageBox,
    Spacer,
    TextStyle,
    VSplit,
    TextBox,
)

# 与 gallery_manager 中保持一致的缩略图尺寸
THUMBNAIL_SIZE = (64, 64)
# 原版：画廊列表封面显示尺寸（缩略图的两倍）与查重对比图展示尺寸
CARD_COVER_SIZE = (THUMBNAIL_SIZE[0] * 2, THUMBNAIL_SIZE[1] * 2)
REPEAT_IMAGE_SHOW_SIZE = (128, 128)


# ==================== 配置容器 ====================


class ImageProcessor:
    """持有查重阈值与大小限制，供管理器与指令层共享。"""

    def __init__(self, size_limit_mb: float, hash1_threshold: int, hash2_threshold: int):
        self.size_limit_mb = size_limit_mb
        self.hash1_threshold = hash1_threshold
        self.hash2_threshold = hash2_threshold

    def update(
        self,
        size_limit_mb: float | None = None,
        hash1_threshold: int | None = None,
        hash2_threshold: int | None = None,
    ) -> None:
        if size_limit_mb is not None:
            self.size_limit_mb = size_limit_mb
        if hash1_threshold is not None:
            self.hash1_threshold = hash1_threshold
        if hash2_threshold is not None:
            self.hash2_threshold = hash2_threshold


# ==================== 图片处理流水线 ====================


def _is_animated(img: Image.Image) -> bool:
    """判断是否为动图（GIF/APNG）。"""
    try:
        if getattr(img, "is_animated", False):
            return True
        # 多帧 GIF
        if getattr(img, "n_frames", 1) > 1:
            return True
    except Exception:
        pass
    return False


def _get_image_pixels(img: Image.Image) -> int:
    return img.width * img.height


def _limit_image_by_pixels(img: Image.Image, target_pixels: int) -> Image.Image:
    """按像素总量等比缩小，使宽*高 <= target_pixels。"""
    pixels = img.width * img.height
    if pixels <= target_pixels:
        return img
    ratio = (target_pixels / pixels) ** 0.5
    new_w = max(1, int(img.width * ratio))
    new_h = max(1, int(img.height * ratio))
    return img.resize((new_w, new_h), Image.Resampling.LANCZOS)


def _save_transparent_static_gif(img: Image.Image, path: str) -> None:
    """保存为保留透明通道的静态 GIF（单帧）。

    用于 QQ 表情：非动画的表情包以 gif 格式存储可保留透明度。
    """
    img = img.convert("RGBA")
    img.save(path, format="GIF", save_all=False, disposal=2)


def _save_transparent_gif(img: Image.Image, duration: int, path: str) -> None:
    """保存动画 GIF，保留透明度与帧时长。"""
    frames = []
    durations = []
    for frame in ImageSequence.Iterator(img):
        frames.append(frame.convert("RGBA"))
        durations.append(frame.info.get("duration", duration) or duration)
    if not frames:
        img.save(path, format="GIF", save_all=True, disposal=2)
        return
    if len(frames) == 1:
        durations = [duration]
    frames[0].save(
        path,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        disposal=2,
        loop=0,
    )


def _get_gif_duration(img: Image.Image) -> int:
    return getattr(img, "info", {}).get("duration", 100) or 100


def process_image_for_gallery(path: str, sub_type: int, size_limit_mb: float) -> None:
    """入库前对图片进行格式修正与大小限制。

    Args:
        path: 图片本地路径（会被原地覆盖）
        sub_type: 是否为表情包（>0 表示是表情）
        size_limit_mb: 大小上限(MB)，超过则等比缩小
    """
    img = Image.open(path)
    # 表情包静态图转静态 gif 以保留透明度（QQ 表情格式）
    need_to_gif = bool(sub_type) and not _is_animated(img)

    scaled = False
    filesize_mb = os.path.getsize(path) / (1024 * 1024)
    if filesize_mb > size_limit_mb:
        pixels = _get_image_pixels(img)
        img = _limit_image_by_pixels(img, int(pixels * size_limit_mb / filesize_mb))
        scaled = True

    if need_to_gif:
        _save_transparent_static_gif(img, path)
    elif scaled:
        if _is_animated(img):
            _save_transparent_gif(img, _get_gif_duration(img), path)
        else:
            img.save(path)
        new_size_mb = os.path.getsize(path) / (1024 * 1024)
        logger.info(f"缩放过大的图片 {filesize_mb:.2f}M -> {new_size_mb:.2f}M")


# ==================== 渲染辅助 ====================


def _save_render(img: Image.Image, save_dir: str) -> str:
    """将渲染结果保存为临时 PNG 并返回路径；顺带清理超过 1 小时的旧渲染文件。"""
    os.makedirs(save_dir, exist_ok=True)
    try:
        now = time.time()
        for f in os.listdir(save_dir):
            if f.startswith("render_") and f.endswith(".png"):
                fp = os.path.join(save_dir, f)
                try:
                    if os.path.getmtime(fp) < now - 3600:
                        os.remove(fp)
                except OSError:
                    pass
    except OSError:
        pass
    path = os.path.join(save_dir, f"render_{int(time.time() * 1000)}_{id(img)}.png")
    img.save(path, format="PNG")
    return path


def _grid_row_count(n: int) -> int:
    """与原版一致：row_count = int(sqrt(元素数))，至少为 1。"""
    return max(1, int(math.sqrt(max(1, n))))


# ==================== 渲染器（原版 Pillow Canvas 风格） ====================


async def render_gallery_list(items: list[dict], save_dir: str) -> str:
    """渲染所有画廊的列表卡片网格。

    items: [{'name', 'thumb_path', 'mode', 'count', 'size_text'}, ...]
    thumb_path 为封面缩略图路径（可为 None）。
    """
    with Canvas(bg=FillBg(THUMBNAIL_BG_COLOR)).set_padding(8) as canvas:
        with Grid(row_count=_grid_row_count(len(items)), hsep=8, vsep=8) \
                .set_item_align('t').set_content_align('t'):
            for it in items:
                with VSplit().set_padding(0).set_sep(4) \
                        .set_content_align('c').set_item_align('c'):
                    if it["thumb_path"] and os.path.exists(it["thumb_path"]):
                        ImageBox(image=it["thumb_path"], size=CARD_COVER_SIZE,
                                 image_size_mode='fit').set_content_align('c')
                    else:
                        Spacer(w=CARD_COVER_SIZE[0], h=CARD_COVER_SIZE[1])
                    TextBox(it["name"], TextStyle(DEFAULT_BOLD_FONT, 24, BLACK))
                    TextBox(f"{it['count']}张 {it['size_text']}",
                            TextStyle(DEFAULT_FONT, 20, BLACK))

    img = await canvas.get_img()
    return _save_render(img, save_dir)


async def render_pic_grid(pics: list[tuple], save_dir: str) -> str:
    """渲染单个画廊的图片缩略图网格（也用于上传记录视图）。

    pics: [(thumb_path 或 None, pid), ...]
    """
    with Canvas(bg=FillBg(THUMBNAIL_BG_COLOR)).set_padding(8) as canvas:
        with Grid(row_count=_grid_row_count(len(pics)), hsep=4, vsep=4):
            for thumb_path, pid in pics:
                with VSplit().set_padding(0).set_sep(2) \
                        .set_content_align('c').set_item_align('c'):
                    if thumb_path and os.path.exists(thumb_path):
                        ImageBox(thumb_path, size=THUMBNAIL_SIZE,
                                 image_size_mode='fit').set_content_align('c')
                    else:
                        Spacer(w=THUMBNAIL_SIZE[0], h=THUMBNAIL_SIZE[1])
                    TextBox(str(pid), TextStyle(DEFAULT_FONT, 12, BLACK))

    img = await canvas.get_img()
    return _save_render(img, save_dir)


async def render_repeat(pairs: list[dict], hint: str, save_dir: str) -> str:
    """渲染上传查重对比图：待上传图片 vs 已存在的相似图片。

    pairs: [{'new_path', 'old_path', 'old_pid'}, ...]（路径均可为 None）
    """
    with Canvas(bg=FillBg(THUMBNAIL_BG_COLOR)).set_padding(8) as canvas:
        with VSplit().set_padding(8).set_sep(16).set_item_align('lt').set_content_align('lt'):
            if hint:
                TextBox(hint, TextStyle(DEFAULT_FONT, 16, BLACK))
            with Grid(row_count=_grid_row_count(len(pairs)), hsep=8, vsep=8) \
                    .set_item_align('t').set_content_align('t'):
                for pair in pairs:
                    with HSplit().set_padding(0).set_sep(4):
                        with VSplit().set_padding(0).set_sep(4) \
                                .set_content_align('c').set_item_align('c'):
                            if pair["new_path"] and os.path.exists(pair["new_path"]):
                                ImageBox(image=pair["new_path"], size=REPEAT_IMAGE_SHOW_SIZE,
                                         image_size_mode='fit').set_content_align('c')
                            else:
                                Spacer(w=REPEAT_IMAGE_SHOW_SIZE[0], h=REPEAT_IMAGE_SHOW_SIZE[1])
                            TextBox("待上传图片", TextStyle(DEFAULT_FONT, 16, BLACK))
                        with VSplit().set_padding(0).set_sep(4) \
                                .set_content_align('c').set_item_align('c'):
                            if pair["old_path"] and os.path.exists(pair["old_path"]):
                                ImageBox(image=pair["old_path"], size=REPEAT_IMAGE_SHOW_SIZE,
                                         image_size_mode='fit').set_content_align('c')
                            else:
                                Spacer(w=REPEAT_IMAGE_SHOW_SIZE[0], h=REPEAT_IMAGE_SHOW_SIZE[1])
                            TextBox(f"pid: {pair['old_pid']}", TextStyle(DEFAULT_FONT, 16, BLACK))

    img = await canvas.get_img()
    return _save_render(img, save_dir)


async def render_check(groups: list[list[tuple]], save_dir: str) -> str:
    """渲染查重结果：每组重复图片并排展示。

    groups: [[(pic_path 或 None, pid), ...], ...]
    """
    with Canvas(bg=FillBg(THUMBNAIL_BG_COLOR)).set_padding(8) as canvas:
        with VSplit().set_padding(16).set_sep(8).set_item_align('lt').set_content_align('lt'):
            for group in groups:
                with HSplit().set_padding(0).set_sep(4) \
                        .set_item_align('lt').set_content_align('lt'):
                    for pic_path, pid in group:
                        with VSplit().set_padding(0).set_sep(4) \
                                .set_content_align('c').set_item_align('c'):
                            if pic_path and os.path.exists(pic_path):
                                ImageBox(image=pic_path, size=REPEAT_IMAGE_SHOW_SIZE,
                                         image_size_mode='fit').set_content_align('c')
                            else:
                                Spacer(w=REPEAT_IMAGE_SHOW_SIZE[0],
                                       h=REPEAT_IMAGE_SHOW_SIZE[1])
                            TextBox(f"pid: {pid}", TextStyle(DEFAULT_FONT, 16, BLACK))

    img = await canvas.get_img()
    return _save_render(img, save_dir)
