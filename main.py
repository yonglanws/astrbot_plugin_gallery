"""AstrBot 画廊插件主入口。

移植自 lunabot 的画廊服务。监听所有消息，指令兼容 / 前缀但也允许不带 /。
持久化数据存放于 data/plugin_data/astrbot_plugin_gallery/。
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import zipfile
from datetime import datetime, timedelta

import astrbot.api.message_components as Comp
from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.star.star_tools import StarTools
from astrbot.core.utils.io import download_image_by_url

from .gallery_manager import (
    Gallery,
    GalleryManager,
    GalleryMode,
    GalleryPic,
    GalleryPicRepeatedException,
    ReplyException,
    get_exc_desc,
)
from .history import HistoryManager
from . import image_utils
from .image_utils import ImageProcessor, process_image_for_gallery


@register(
    "astrbot_plugin_gallery",
    "mzkbot",
    "群内图片画廊：上传、查看、查重、管理表情/梗图",
    "1.0.0",
    "",
)
class GalleryPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # 持久化目录：data/plugin_data/astrbot_plugin_gallery/
        self.data_dir = os.path.normpath(StarTools.get_data_dir())
        os.makedirs(self.data_dir, exist_ok=True)
        # 临时下载目录
        self.tmp_dir = os.path.join(self.data_dir, "tmp")
        os.makedirs(self.tmp_dir, exist_ok=True)

        self._read_config()
        self.img_proc = ImageProcessor(
            self.size_limit_mb, self.hash1_threshold, self.hash2_threshold
        )
        self.gallery_manager = GalleryManager(self.data_dir, self.img_proc)
        self.history_manager = HistoryManager(self.data_dir, self.gallery_manager)
        self._sync_task: asyncio.Task | None = None

    def _read_config(self) -> None:
        """从 AstrBotConfig 读取配置项。"""
        self.size_limit_mb = float(self.config.get("size_limit_mb", 1.0))
        self.pick_limit = int(self.config.get("pick_limit", 5))
        self.hash1_threshold = int(self.config.get("hash1_difference_threshold", 5))
        self.hash2_threshold = int(self.config.get("hash2_difference_threshold", 1000))
        self.revert_expired_hours = int(
            self.config.get("user_recent_revert_expired_hours", 24)
        )
        self.enable_slash_prefix = bool(self.config.get("enable_slash_prefix", True))
        sync = self.config.get("sync", {}) or {}
        self.sync_enable = bool(sync.get("enable", False))
        self.sync_times = sync.get("sync_times", [[3, 30, 0]]) or []
        self.sync_verbose = bool(sync.get("verbose", True))
        self.sync_remote_dir = sync.get("remote_dir", "AstrBotGallery") or "AstrBotGallery"
        self.share_link = sync.get("share_link", "") or ""

    async def initialize(self):
        """插件初始化：加载数据、启动同步任务。"""
        self.gallery_manager.ensure_loaded()
        self.history_manager.ensure_loaded()
        if self.sync_enable and self.sync_times:
            self._sync_task = asyncio.create_task(self._sync_loop())
            logger.info("画廊百度网盘同步任务已启动")

    async def terminate(self):
        if self._sync_task and not self._sync_task.done():
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass
        logger.info("画廊插件已停止")

    # ==================== 指令路由 ====================

    # 画廊不存在错误文案（画廊名不允许含引号，此匹配不会误伤其他错误）
    _GALL_NOT_FOUND_RE = re.compile(r'画廊".+"不存在')

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """监听所有消息，分发画廊指令。兼容 / 前缀，也允许不带 /。

        说明：AstrBot 的 message_str 通常已剥离平台唤醒前缀，因此这里再处理
        一次开头可选的 /，保证 /看 与 看 都能触发。
        enable_slash_prefix=True(默认): /看 与 看 都生效；
        enable_slash_prefix=False: 仅接受不带 / 的裸指令。
        """
        text = (event.message_str or "").strip()
        if not text:
            return

        # 规范化前缀：去掉可选的 /
        has_slash = text.startswith("/")
        body = text[1:].strip() if has_slash else text
        if not body:
            return

        # 关闭斜杠兼容时，拒绝带 / 的指令（只走裸指令）
        if not self.enable_slash_prefix and has_slash:
            return

        # 前缀匹配指令（兼容原版 lunabot：看miku / 看表情 中间可以没有空格）
        parsed = self._parse_cmd(body)
        if not parsed:
            return
        cmd, args = parsed

        # 匹配到画廊指令，处理后停止事件传播（避免继续触发 LLM 等）
        try:
            async for result in self._dispatch(event, cmd, args):
                yield result
        except ReplyException as e:
            if self._GALL_NOT_FOUND_RE.fullmatch(str(e)):
                # 画廊不存在：视为误触发，不回复也不拦截，放行给其他插件/LLM
                return
            yield event.plain_result(str(e))
        except GalleryPicRepeatedException as e:
            yield event.plain_result(str(e))
        except AssertionError as e:
            yield event.plain_result(str(e))
        except Exception as e:
            logger.error(f"画廊指令处理出错 [{cmd}]: {get_exc_desc(e)}")
            yield event.plain_result(f"处理出错: {e}")
        event.stop_event()

    # 长指令必须排在短指令前面，避免「看所有」被拆成「看」+「所有」
    _COMMANDS = (
        "看所有", "看全部",
        "取消上传", "撤销上传", "回退上传",
        "上传记录",
        "下载图包", "下载看", "下载画廊",
        "上传", "添加",
        "看",
        "gall",
    )

    def _parse_cmd(self, body: str) -> tuple[str, str] | None:
        """从消息正文解析画廊指令。

        与原版 lunabot CmdHandler 一致：按最长前缀匹配，中文指令后可以不空格。
        ASCII 指令（gall）要求词边界，避免 gallery 误触发 gall。
        """
        for cmd in self._COMMANDS:
            if not body.startswith(cmd):
                continue
            rest = body[len(cmd):]
            if rest and cmd.isascii() and rest[0].isalnum():
                continue
            return cmd, rest.strip()
        return None

    def _is_gallery_cmd(self, cmd: str, args: str) -> bool:
        """判断是否为画廊指令（用于决定是否拦截事件传播）。"""
        return self._parse_cmd(f"{cmd} {args}".strip()) is not None

    async def _dispatch(self, event: AstrMessageEvent, cmd: str, args: str):
        """根据指令名分发到对应处理器。"""
        # 看图（普通用户）：内部直发合并消息链，不经过分段回复管线
        if cmd == "看":
            await self._cmd_pick(event, args)
        elif cmd == "看所有" or cmd == "看全部":
            async for r in self._cmd_list(event, args):
                yield r
        elif cmd == "上传" or cmd == "添加":
            async for r in self._cmd_add(event, args):
                yield r
        elif cmd == "取消上传" or cmd == "撤销上传" or cmd == "回退上传":
            async for r in self._cmd_cancel(event, args):
                yield r
        elif cmd == "上传记录":
            async for r in self._cmd_record(event, args):
                yield r
        elif cmd == "下载图包" or cmd == "下载看" or cmd == "下载画廊":
            yield event.plain_result(self.share_link or "未配置图包分享链接")
        elif cmd == "gall":
            # /gall <子指令> [参数...]
            if not args:
                yield event.plain_result(self._gall_help())
                return
            sub_parts = args.split(None, 1)
            sub = sub_parts[0]
            sub_args = sub_parts[1].strip() if len(sub_parts) > 1 else ""
            async for r in self._dispatch_gall(event, sub, sub_args):
                yield r
        else:
            # 非画廊指令，放行给其他插件/LLM
            return

    async def _dispatch_gall(self, event: AstrMessageEvent, sub: str, args: str):
        """分发 /gall 子指令（管理员指令在此校验权限）。"""
        is_admin = event.is_admin()

        # 管理员专属指令
        if sub == "open":
            self._require_admin(is_admin)
            name = args.strip()
            async with self.gallery_manager._lock:
                self.gallery_manager.open_gall(name)
                await self.gallery_manager._save()
            yield event.plain_result(f'画廊"{name}"创建成功')
        elif sub == "close":
            self._require_admin(is_admin)
            name = args.strip()
            async with self.gallery_manager._lock:
                self.gallery_manager.close_gall(name)
                await self.gallery_manager._save()
            yield event.plain_result(f'画廊"{name}"删除成功')
        elif sub == "mode":
            async for r in self._gall_mode(event, args, is_admin):
                yield r
        elif sub == "cover":
            self._require_admin(is_admin)
            async for r in self._gall_cover(event, args):
                yield r
        elif sub == "alias":
            # /gall alias add/del 画廊名 别名
            if not args:
                yield event.plain_result("使用方式: /gall alias add 画廊名称 别名\n/gall alias del 画廊名称 别名")
                return
            ap = args.split(None, 2)
            if len(ap) < 2:
                yield event.plain_result("使用方式: /gall alias add 画廊名称 别名\n/gall alias del 画廊名称 别名")
                return
            op = ap[0]
            if op not in ("add", "del", "remove"):
                yield event.plain_result("使用方式: /gall alias add 画廊名称 别名\n/gall alias del 画廊名称 别名")
                return
            self._require_admin(is_admin)
            if len(ap) < 3:
                yield event.plain_result(f"使用方式: /gall alias {op} 画廊名称 别名")
                return
            gall_name, alias = ap[1], ap[2]
            async with self.gallery_manager._lock:
                if op == "add":
                    self.gallery_manager.add_gall_alias(gall_name, alias)
                    yield event.plain_result(f'画廊"{gall_name}"添加别名"{alias}"成功')
                else:
                    self.gallery_manager.del_gall_alias(gall_name, alias)
                    yield event.plain_result(f'画廊"{gall_name}"删除别名"{alias}"成功')
                await self.gallery_manager._save()
        elif sub == "del" or sub == "remove":
            self._require_admin(is_admin)
            async for r in self._gall_del(event, args):
                yield r
        elif sub == "reload" or sub == "update":
            self._require_admin(is_admin)
            name = args.strip()
            new_pids, del_pids = await self.gallery_manager.async_reload_gall(name)
            yield event.plain_result(
                f'画廊"{name}"重新加载完成\n新增图片: {len(new_pids)}张\n失效图片: {len(del_pids)}张'
            )
        elif sub == "check":
            self._require_admin(is_admin)
            async for r in self._gall_check(event, args):
                yield r
        elif sub == "log":
            self._require_admin(is_admin)
            async for r in self._gall_log(event, args):
                yield r
        elif sub == "replace":
            self._require_admin(is_admin)
            async for r in self._gall_replace(event, args):
                yield r
        elif sub == "download":
            self._require_admin(is_admin)
            async for r in self._gall_download(event, args):
                yield r
        elif sub == "cancel" or sub == "revert":
            async for r in self._cmd_cancel(event, args):
                yield r
        elif sub == "add" or sub == "upload":
            async for r in self._cmd_add(event, args):
                yield r
        elif sub == "list":
            async for r in self._cmd_list(event, args):
                yield r
        elif sub == "pick":
            async for r in self._cmd_pick(event, args):
                yield r
        elif sub == "record":
            async for r in self._cmd_record(event, args):
                yield r
        else:
            yield event.plain_result(self._gall_help())

    @staticmethod
    def _require_admin(is_admin: bool) -> None:
        if not is_admin:
            raise ReplyException("该指令仅限管理员使用")

    def _gall_help(self) -> str:
        return (
            "画廊指令：\n"
            "看图: /看 画廊名 | /看 画廊名 x2 | /看 画廊名 -1 | /看 123 456\n"
            "画廊列表: /看所有 | /看所有 画廊名\n"
            "上传: (回复图片) /上传 画廊名 [force]\n"
            "撤销: /取消上传 [记录ID(管理员)]\n"
            "记录: /上传记录 记录ID\n"
            "图包链接: /下载图包\n"
            "管理员(/gall 子指令):\n"
            "  open/close/mode/cover/alias add|del\n"
            "  del/reload/check/log/replace/download"
        )

    # ==================== 指令实现 ====================

    async def _cmd_pick(self, event: AstrMessageEvent, args: str):
        """看图：随机或按 pid 查看画廊图片。"""
        if not args:
            raise ReplyException(
                "使用方式:\n/看 画廊名称\n/看 画廊名称 x2\n/看 画廊名称 -1\n/看 123 456..."
            )
        import random

        pics: list[GalleryPic] | None = None
        names: list[str] | None = None
        try:
            pids = [int(x) for x in args.split()]
            pics = [
                self.gallery_manager.find_pic(pid, raise_if_nofound=True) for pid in pids
            ]
            names = [p.gall_name for p in pics]
        except ValueError:
            pics = None
            num = 1
            a = args.replace("*", "x").replace("×", "x")
            if "-" in a:
                a, nindex_str = a.rsplit("-", 1)
                try:
                    nindex = int(nindex_str)
                except ValueError:
                    raise ReplyException("使用方式:\n/看 画廊名称\n/看 画廊名称 x2\n/看 画廊名称 -1")
                g = self.gallery_manager.find_gall(a.strip(), raise_if_nofound=True)
                if len(g.pics) < nindex:
                    raise ReplyException(f'画廊"{a.strip()}"仅有{len(g.pics)}张图片')
                pics = [g.pics[-nindex]]
            elif "x" in a:
                a, num_str = a.rsplit("x", 1)
                try:
                    num = int(num_str)
                except ValueError:
                    raise ReplyException("使用方式:\n/看 画廊名称\n/看 画廊名称 x2")
                if not (1 <= num <= self.pick_limit):
                    raise ReplyException(f"一次查看图片数量必须在1到{self.pick_limit}之间")
            names = [a.strip()]

        # 模式校验
        for name in names:
            g = self.gallery_manager.find_gall(name, raise_if_nofound=True)
            if not event.is_admin() and g.mode == GalleryMode.Off:
                raise ReplyException(f'画廊"{name}"已关闭')

        if pics is None:
            g = self.gallery_manager.find_gall(names[0], raise_if_nofound=True)
            if not g.pics:
                raise ReplyException(f'画廊"{names[0]}"没有图片')
            pics = [random.choice(g.pics) for _ in range(num)]

        if len(pics) > self.pick_limit:
            raise ReplyException(f"一次最多查看{self.pick_limit}张图片")

        # 合并消息：多图放在同一条消息链里直发。不能用 yield——
        # AstrBot 开启分段回复时会把 yield 的链按组件拆成多条消息
        chain = []
        for p in pics:
            chain.append(Comp.Image.fromFileSystem(p.path))
        await event.send(event.chain_result(chain))

    async def _cmd_list(self, event: AstrMessageEvent, args: str):
        """查看所有画廊列表，或指定画廊的图片网格。"""
        if not args:
            galls = self.gallery_manager.get_all_galls()
            if not galls:
                yield event.plain_result("当前没有任何画廊")
                return

            items = []
            for name, g in galls.items():
                cover = self.gallery_manager.find_pic(g.cover_pid or 0)
                if not cover and g.pics:
                    cover = g.pics[0]
                # 计算画廊总大小
                total_size = 0
                if os.path.isdir(g.pics_dir):
                    for fp in os.listdir(g.pics_dir):
                        fp_full = os.path.join(g.pics_dir, fp)
                        if os.path.isfile(fp_full):
                            total_size += os.path.getsize(fp_full)
                total_mb = total_size / (1024 * 1024)
                if total_mb == 0:
                    size_text = ""
                elif total_mb < 1:
                    size_text = "(<1M)"
                elif total_mb < 1024:
                    size_text = f"({total_mb:.0f}M)"
                else:
                    size_text = f"({total_mb/1024:.0f}G)"

                thumb_path = None
                if cover:
                    await asyncio.to_thread(cover.ensure_thumb)
                    if cover.thumb_path and os.path.exists(cover.thumb_path):
                        thumb_path = cover.thumb_path
                items.append(
                    {
                        "name": name,
                        "thumb_path": thumb_path,
                        "mode": g.mode.value if g.mode != GalleryMode.Edit else "",
                        "count": len(g.pics),
                        "size_text": size_text,
                    }
                )
            img_path = await image_utils.render_gallery_list(items, self.tmp_dir)
            yield event.image_result(img_path)
            return

        # 指定画廊的图片网格
        g = self.gallery_manager.find_gall(args, raise_if_nofound=True)
        if not event.is_admin() and g.mode == GalleryMode.Off:
            raise ReplyException(f'画廊"{args}"已关闭')
        if not g.pics:
            raise ReplyException(f'画廊"{args}"没有图片')

        items = []
        for pic in g.pics:
            await asyncio.to_thread(pic.ensure_thumb)
            items.append((pic.thumb_path, pic.pid))
        img_path = await image_utils.render_pic_grid(items, self.tmp_dir)
        yield event.image_result(img_path)

    async def _cmd_add(self, event: AstrMessageEvent, args: str):
        """上传图片到画廊。force 关闭查重。"""
        check_duplicated = True
        if "force" in args:
            check_duplicated = False
            args = args.replace("force", "").strip()
        name = args
        g = self.gallery_manager.find_gall(name, raise_if_nofound=True)

        if not event.is_admin() and g.mode != GalleryMode.Edit:
            raise ReplyException(f'画廊"{name}"不允许上传图片')

        # 提取消息中的图片
        image_urls = await self._extract_image_urls(event)
        if not image_urls:
            raise ReplyException("请回复或附带至少一张图片后再上传")

        start_time = datetime.now()
        ok_list: list[int] = []
        err_msg = ""
        repeats: list[tuple[str, int]] = []  # (待上传图临时路径, 相似 pid)

        # 并发下载/取本地路径
        async def fetch_local(url: str) -> str:
            try:
                return await self._localize_image(url)
            except Exception as e:
                logger.warning(f"获取图片失败: {get_exc_desc(e)}")
                return ""

        paths = await asyncio.gather(*[fetch_local(u) for u in image_urls])

        for i, path in enumerate(paths, 1):
            if not path or not os.path.exists(path):
                err_msg += f"第{i}张图片下载失败\n"
                continue
            try:
                await asyncio.to_thread(
                    process_image_for_gallery, path, 1, self.size_limit_mb
                )
                pid = await self.gallery_manager.async_add_pic(
                    name,
                    path,
                    check_duplicated=check_duplicated,
                    h1=self.hash1_threshold,
                    h2=self.hash2_threshold,
                )
                ok_list.append(pid)
                await self._append_add_log(event.get_sender_id(), pid, name)
            except GalleryPicRepeatedException as e:
                repeats.append((path, e.pid))
            except Exception as e:
                logger.error(f"上传第{i}张图片到画廊\"{name}\"失败: {get_exc_desc(e)}")
                err_msg += f"第{i}张图片上传失败: {e}\n"

        cost = (datetime.now() - start_time).total_seconds()
        logger.info(f'上传{len(ok_list)}/{len(image_urls)}张图片到画廊"{name}"完成, 耗时{cost:.2f}秒')

        hid = await self.history_manager.add_history(event.get_sender_id(), ok_list) if ok_list else None

        # 渲染重复对比图
        repeat_img_path = None
        if repeats:
            pairs = []
            for new_path, old_pid in repeats:
                old_path = None
                old_pic = self.gallery_manager.find_pic(old_pid, raise_if_nofound=False)
                if old_pic and os.path.exists(old_pic.path):
                    old_path = old_pic.path
                pairs.append(
                    {"new_path": new_path, "old_path": old_path, "old_pid": old_pid}
                )
            repeat_img_path = await image_utils.render_repeat(
                pairs,
                hint='查重错误可使用"/上传 画廊名 force"强制上传图片',
                save_dir=self.tmp_dir,
            )

        msg = f'成功上传{len(ok_list)}/{len(image_urls)}张图片到"{name}"'
        if ok_list:
            msg += f"（表情编号: {' '.join(str(p) for p in ok_list)}）"
        msg += "\n"
        msg += err_msg
        if repeats:
            msg += f"{len(repeats)}张图片与已有图片重复"
        msg += '\n主要收录表情/梗图，请勿上传可能有争议的图片（使用"/取消上传"回退）'

        chain = [Comp.Plain(msg)]
        if repeat_img_path:
            chain.append(Comp.Image.fromFileSystem(repeat_img_path))
        yield event.chain_result(chain)

        # 清理下载的临时文件（图片已拷贝到画廊目录或已渲染对比图）
        for path in paths:
            try:
                if path and os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass

    async def _cmd_cancel(self, event: AstrMessageEvent, args: str):
        """撤销上传：无参数撤销自己最近一次；管理员可指定记录 id。"""
        hid = None
        if args:
            try:
                hid = int(args)
            except ValueError:
                raise ReplyException(
                    f"使用方式:\n撤销你的最近一次上传: /取消上传\n"
                    f"撤销指定上传记录(仅管理员): /取消上传 记录ID"
                )

        if hid:
            if not event.is_admin():
                raise ReplyException("仅管理员可撤销指定上传记录，非管理员可留空参数撤销自己的最近一次上传")
            h, ok_list, err_list = await self.history_manager.revert_by_id(hid)
            msg = f"撤销{h['uid']}的上传记录#{h['id']}\n"
        else:
            h, ok_list, err_list = await self.history_manager.revert_last_by_user(
                event.get_sender_id(), self.revert_expired_hours
            )
            msg = f"撤销你的最近一次上传记录#{h['id']}\n"

        if ok_list:
            msg += f"{len(ok_list)}张图片删除成功:\n" + " ".join(str(p) for p in ok_list) + "\n"
        if err_list:
            msg += f"{len(err_list)}张图片删除失败:\n" + " ".join(str(p) for p in err_list) + "\n"
        yield event.plain_result(msg.strip())

    async def _cmd_record(self, event: AstrMessageEvent, args: str):
        """查看指定 id 的上传记录。"""
        try:
            hid = int(args)
        except (ValueError, TypeError):
            raise ReplyException(f"使用方式: /上传记录 记录ID")
        h = self.history_manager.get_history(hid)
        user_id = h["uid"]
        time_str = datetime.fromtimestamp(h["ts"]).strftime("%Y-%m-%d %H:%M:%S")
        pids = h["pids"]
        reverted = h["reverted"]

        pics: list[GalleryPic] = []
        not_found: list[int] = []
        for pid in pids:
            pic = self.gallery_manager.find_pic(pid, raise_if_nofound=False)
            if pic:
                pics.append(pic)
            else:
                not_found.append(pid)

        msg = f"{user_id}的上传记录#{h['id']}\n{time_str}\n"
        if reverted:
            msg += "该上传已撤销\n"
        msg += f"上传的图片数量:{len(pids)}\n"
        if not_found:
            msg += "未找到的图片id: " + " ".join(str(p) for p in not_found) + "\n"

        chain = [Comp.Plain(msg)]
        if pics:
            items = []
            for pic in pics:
                await asyncio.to_thread(pic.ensure_thumb)
                items.append((pic.thumb_path, pic.pid))
            img_path = await image_utils.render_pic_grid(items, self.tmp_dir)
            chain.append(Comp.Image.fromFileSystem(img_path))
        yield event.chain_result(chain)

    async def _gall_mode(self, event: AstrMessageEvent, args: str, is_admin: bool):
        """查看或设置画廊模式。"""
        parts = args.split()
        if len(parts) == 1:
            mode = self.gallery_manager.find_gall(parts[0], raise_if_nofound=True).mode.value
            yield event.plain_result(f'画廊"{parts[0]}"当前模式: {mode}')
            return
        if len(parts) != 2:
            raise ReplyException("使用方式: /gall mode 画廊名称 模式(edit/view/off)")
        self._require_admin(is_admin)
        name, mode = parts
        async with self.gallery_manager._lock:
            try:
                old, new = self.gallery_manager.change_gall_mode(name, GalleryMode(mode))
            except ValueError:
                raise ReplyException("模式必须是 edit/view/off 之一")
            await self.gallery_manager._save()
        yield event.plain_result(f'画廊"{name}"模式修改成功: {old.value} -> {new.value}')

    async def _gall_cover(self, event: AstrMessageEvent, args: str):
        """设置画廊封面。"""
        parts = args.split()
        if len(parts) != 2:
            raise ReplyException("使用方式: /gall cover 画廊名称 图片ID")
        name, pid_str = parts
        try:
            pid = int(pid_str)
        except ValueError:
            raise ReplyException("图片ID必须是整数")
        async with self.gallery_manager._lock:
            self.gallery_manager.set_cover_pic(name, pid)
            await self.gallery_manager._save()
        yield event.plain_result(f'画廊"{name}"封面图片设置为pid={pid}成功')

    async def _gall_del(self, event: AstrMessageEvent, args: str):
        """批量删除图片。"""
        l, r = None, None
        try:
            if "-" in args:
                l_str, r_str = args.split("-", 1)
                l, r = int(l_str), int(r_str)
                pids = list(range(l, r + 1))
            else:
                pids = [int(s) for s in args.split()]
            assert pids
        except Exception:
            raise ReplyException(
                "使用方式:\n/gall del 123 456 -1 -2 ...\n/gall del 123-456 (最多连续20张)"
            )

        if l is not None:
            if r - l >= 20:
                raise ReplyException("一次最多删除20张连续图片")
        # 禁止跨画廊删除
        gall_names = set()
        for pid in pids:
            pic = self.gallery_manager.find_pic(pid, raise_if_nofound=False)
            if pic:
                gall_names.add(pic.gall_name)
        if len(gall_names) > 1:
            raise ReplyException("禁止跨画廊删除图片")

        ok_list, err_list = [], []
        async with self.gallery_manager._lock:
            for pid in pids:
                try:
                    deleted = self.gallery_manager.del_pic(pid)
                    ok_list.append(deleted)
                except Exception as e:
                    logger.warning(f"删除画廊图片pid={pid}失败: {get_exc_desc(e)}")
                    err_list.append(pid)
            await self.gallery_manager._save()

        msg = ""
        if ok_list:
            msg += f"{len(ok_list)}张图片删除成功:\n" + " ".join(str(p) for p in ok_list) + "\n"
        if err_list:
            msg += f"{len(err_list)}张图片删除失败:\n" + " ".join(str(p) for p in err_list) + "\n"
        yield event.plain_result(msg.strip())

    async def _gall_check(self, event: AstrMessageEvent, args: str):
        """画廊查重，支持 all 和 rehash。"""
        rehash = False
        if "rehash" in args:
            rehash = True
            args = args.replace("rehash", "").strip()
        check_all = args == "all"
        if check_all:
            args = ""

        async def check_one(name: str):
            if rehash:
                msg = f'正在为画廊"{name}"重新计算hash并检查重复图片...'
            else:
                msg = f'正在为画廊"{name}"检查重复图片...'
            await event.send(event.plain_result(msg))
            res = await self.gallery_manager.async_check_gallery(name, rehash=rehash)
            if not res:
                await event.send(event.plain_result(f'画廊"{name}"检查完成，未发现重复图片'))
                return
            groups = []
            for first_pid, repeat_pids in res.items():
                row = []
                for pid in [first_pid] + repeat_pids:
                    pic_path = None
                    pic = self.gallery_manager.find_pic(pid, raise_if_nofound=False)
                    if pic and os.path.exists(pic.path):
                        pic_path = pic.path
                    row.append((pic_path, pid))
                groups.append(row)
            img_path = await image_utils.render_check(groups, self.tmp_dir)
            if rehash:
                msg = f'画廊"{name}"重新计算hash完成，发现重复图片组共{len(res)}组:'
            else:
                msg = f'画廊"{name}"检查完成，发现重复图片组共{len(res)}组:'
            await event.send(event.chain_result([Comp.Plain(msg), Comp.Image.fromFileSystem(img_path)]))
            recommend = "推荐移除的重复图片pid:\n"
            for first_pid, repeat_pids in res.items():
                recommend += " ".join(str(p) for p in repeat_pids) + "\n"
            await event.send(event.plain_result(recommend.strip()))

        if check_all:
            for name in self.gallery_manager.get_all_galls().keys():
                await check_one(name)
        else:
            await check_one(args)
        # check 不再 yield（已通过 event.send 发送）

    async def _gall_log(self, event: AstrMessageEvent, args: str):
        """查询某个 pid 的上传日志。"""
        try:
            pid = int(args)
        except ValueError:
            raise ReplyException("使用方式: /gall log pid")
        log_path = os.path.join(self.data_dir, "add.log")
        if os.path.exists(log_path):
            with open(log_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        else:
            lines = []
        for line in lines:
            if f"pid={pid}" in line:
                yield event.plain_result(line.strip())
                return
        raise ReplyException(f"pid={pid}的上传记录不存在")

    async def _gall_replace(self, event: AstrMessageEvent, args: str):
        """替换指定 pid 图片。修复原版 args.remove('force') bug。"""
        # 修复：字符串没有 .remove() 方法，使用 replace
        check_duplicated = True
        if "force" in args:
            check_duplicated = False
            args = args.replace("force", "").strip()
        try:
            pid = int(args)
        except ValueError:
            raise ReplyException("使用方式: /gall replace pid")

        image_urls = await self._extract_image_urls(event)
        if not image_urls:
            raise ReplyException("请附加要替换的图片")
        local = await self._localize_image(image_urls[0])
        if not local or not os.path.exists(local):
            raise ReplyException("获取图片失败")
        try:
            await asyncio.to_thread(process_image_for_gallery, local, 1, self.size_limit_mb)
            pid = await self.gallery_manager.async_replace_pic(
                pid,
                local,
                check_duplicated=check_duplicated,
                h1=self.hash1_threshold,
                h2=self.hash2_threshold,
            )
        except GalleryPicRepeatedException as e:
            raise ReplyException(f"替换失败: 画廊中已存在相似图片(pid={e.pid})")
        finally:
            # 清理下载的临时文件（图片已拷贝到画廊目录）
            try:
                if local and os.path.exists(local):
                    os.remove(local)
            except OSError:
                pass
        yield event.plain_result(f"成功替换图片pid={pid}")

    async def _gall_download(self, event: AstrMessageEvent, args: str):
        """打包下载指定画廊图片并上传群文件（仅群聊）。"""
        name = args
        g = self.gallery_manager.find_gall(name, raise_if_nofound=True)
        if not g.pics:
            raise ReplyException(f'画廊"{name}"没有图片')

        group_id = event.get_group_id()
        if not group_id:
            raise ReplyException("该指令仅在群聊中可用")

        # 打包 zip 到临时目录
        zip_path = os.path.join(self.tmp_dir, f"{name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip")
        await asyncio.to_thread(self._zip_gallery, g, zip_path)
        filesize = os.path.getsize(zip_path) / (1024 * 1024)
        await event.send(
            event.plain_result(
                f'正在发送画廊"{name}"所有{len(g.pics)}张图片的压缩包({filesize:.2f}M)...'
            )
        )
        try:
            # 通过平台适配器上传群文件
            await self._upload_group_file(event, group_id, zip_path, os.path.basename(zip_path))
        finally:
            try:
                os.remove(zip_path)
            except OSError:
                pass

    def _zip_gallery(self, g: Gallery, zip_path: str) -> None:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
            for pic in g.pics:
                if os.path.exists(pic.path):
                    arcname = os.path.basename(pic.path)
                    zipf.write(pic.path, arcname)

    async def _upload_group_file(
        self, event: AstrMessageEvent, group_id: str, file_path: str, file_name: str
    ) -> None:
        """通过平台适配器上传群文件（aiocqhttp）。"""
        try:
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                AiocqhttpMessageEvent,
            )
            if not isinstance(event, AiocqhttpMessageEvent):
                raise ReplyException("当前平台不支持上传群文件")
            client = event.bot
            await client.api.call_action(
                "upload_group_file",
                group_id=int(group_id),
                file=file_path,
                name=file_name,
            )
        except ReplyException:
            raise
        except Exception as e:
            logger.error(f"上传群文件失败: {get_exc_desc(e)}")
            raise ReplyException(f"上传群文件失败: {e}")

    # ==================== 辅助方法 ====================

    async def _localize_image(self, src: str) -> str:
        """把图片来源（网络 URL / file:/// / 本地路径 / base64）统一转为本地文件路径。

        aiocqhttp 下 Image.file 常是本地路径或 file:/// 路径，不是 URL——
        直接交给 download_image_by_url 会用 aiohttp 请求本地路径而报 InvalidUrlClientError。
        返回的临时文件扩展名按真实图片格式纠正，避免 gif 被存成 .jpg。
        """
        if not src:
            return ""
        if src.startswith("file:///"):
            return await asyncio.to_thread(
                self._copy_with_correct_ext, os.path.abspath(src[8:])
            )
        if src.startswith(("http://", "https://")):
            local = await download_image_by_url(src)
            return await asyncio.to_thread(self._fix_ext_by_format, local)
        if src.startswith("base64://"):
            import base64 as _b64
            data = _b64.b64decode(src[9:])
            dst = os.path.join(self.tmp_dir, f"imgseg_{datetime.now().strftime('%Y%m%d%H%M%S%f')}")
            with open(dst + ".bin", "wb") as f:
                f.write(data)
            return await asyncio.to_thread(self._fix_ext_by_format, dst + ".bin")
        # 本地路径（绝对或相对）。aiocqhttp 常把 GIF 落到 media_image_xxx.jpg/png
        if os.path.exists(src):
            return await asyncio.to_thread(
                self._copy_with_correct_ext, os.path.abspath(src)
            )
        # 兜底：尝试当 URL 下载（例如没带 http 前缀的链接）
        local = await download_image_by_url(src)
        return await asyncio.to_thread(self._fix_ext_by_format, local)

    def _copy_with_correct_ext(self, path: str) -> str:
        """复制到 tmp 后再按真实格式改扩展名，避免改写协议端缓存文件。"""
        if not path or not os.path.exists(path):
            return path
        dst = os.path.join(
            self.tmp_dir,
            f"img_{datetime.now().strftime('%Y%m%d%H%M%S%f')}{os.path.splitext(path)[1]}",
        )
        shutil.copy2(path, dst)
        return self._fix_ext_by_format(dst)

    @staticmethod
    def _fix_ext_by_format(path: str) -> str:
        """按 PIL 识别的真实格式重命名文件扩展名（gif/jpg/png），避免 .jpg 误标 gif。"""
        if not path or not os.path.exists(path):
            return path
        try:
            from PIL import Image
            with Image.open(path) as im:
                fmt = (im.format or "").upper()
        except Exception:
            return path
        ext_map = {"GIF": ".gif", "JPEG": ".jpg", "PNG": ".png"}
        correct = ext_map.get(fmt)
        if not correct:
            return path
        cur = os.path.splitext(path)[1].lower()
        if cur == correct:
            return path
        new_path = os.path.splitext(path)[0] + correct
        try:
            os.replace(path, new_path)
            return new_path
        except OSError:
            return path

    async def _extract_image_urls(self, event: AstrMessageEvent) -> list[str]:
        """从消息中提取图片 URL。

        支持：直接图片、引用消息(Reply.chain)、合并转发消息——
        包括内联 Node/Nodes 节点与 Forward（通过 get_forward_msg API 拉取内容）。
        """
        urls: list[str] = []
        seen_forward_ids: set[str] = set()
        if not event.message_obj or not event.message_obj.message:
            return urls

        def image_url_of(comp) -> str | None:
            return getattr(comp, "url", None) or getattr(comp, "file", None)

        async def walk(comps: list, depth: int):
            if depth > 4:
                return
            for comp in comps:
                try:
                    if isinstance(comp, Comp.Image):
                        url = image_url_of(comp)
                        if url:
                            urls.append(url)
                    elif isinstance(comp, Comp.Reply):
                        chain = getattr(comp, "chain", None)
                        if chain:
                            await walk(chain, depth + 1)
                    elif isinstance(comp, Comp.Node):
                        content = getattr(comp, "content", None)
                        if content:
                            await walk(content, depth + 1)
                    elif isinstance(comp, Comp.Nodes):
                        nodes = getattr(comp, "nodes", None) or []
                        await walk(nodes, depth + 1)
                    elif isinstance(comp, Comp.Forward):
                        fid = getattr(comp, "id", None) or getattr(
                            comp, "message_id", None
                        )
                        if not fid or str(fid) in seen_forward_ids:
                            continue
                        seen_forward_ids.add(str(fid))
                        segments = await self._fetch_forward_segments(event, fid)
                        urls.extend(self._urls_from_raw_segments(segments))
                except Exception as e:
                    logger.warning(f"提取图片组件失败: {get_exc_desc(e)}")

        await walk(event.message_obj.message, 0)
        return urls

    async def _fetch_forward_segments(self, event: AstrMessageEvent, forward_id) -> list:
        """通过 aiocqhttp 的 get_forward_msg API 拉取合并转发消息内容。

        返回原始 segment 字典列表（已展平所有节点）；非 aiocqhttp 平台或
        API 失败时返回空列表。
        """
        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
            AiocqhttpMessageEvent,
        )

        if not isinstance(event, AiocqhttpMessageEvent):
            logger.debug("当前平台不支持拉取合并转发消息内容")
            return []
        client = event.bot
        res = None
        # 不同协议端参数名不同：NapCat 用 message_id，部分实现用 id
        for action_kwargs in (
            {"message_id": forward_id},
            {"id": forward_id},
        ):
            try:
                res = await client.api.call_action("get_forward_msg", **action_kwargs)
                if res:
                    break
            except Exception as e:
                logger.debug(f"get_forward_msg({action_kwargs}) 失败: {get_exc_desc(e)}")
        if not res:
            return []

        # 兼容多种返回结构：
        # - {"messages": [ {content|message: [seg...]}, ... ]}
        # - [ {content|message: [...]}, ... ]
        # - [ {"type":"node","data":{"content":[...]}}, ... ]
        if isinstance(res, dict):
            res = res.get("messages") or res.get("nodes") or []
        flat: list[dict] = []

        def collect(nodes):
            for node in nodes or []:
                if not isinstance(node, dict):
                    continue
                segs = (
                    node.get("content")
                    or node.get("message")
                    or ((node.get("data") or {}).get("content"))
                    or ((node.get("data") or {}).get("message"))
                )
                if isinstance(segs, list):
                    flat.extend(s for s in segs if isinstance(s, dict))

        collect(res)
        return flat

    @staticmethod
    def _urls_from_raw_segments(segments: list) -> list[str]:
        """从 OneBot 原始 segment 列表中提取图片 URL（含嵌套 node）。"""
        out: list[str] = []
        for seg in segments or []:
            if not isinstance(seg, dict):
                continue
            stype = seg.get("type")
            data = seg.get("data") or {}
            if stype == "image":
                url = data.get("url") or data.get("file")
                if url:
                    out.append(url)
            elif stype == "node":
                inner = data.get("content") or data.get("message") or []
                out.extend(GalleryPlugin._urls_from_raw_segments(inner))
        return out

    async def _append_add_log(self, user_id: str, pid: int, gallery_name: str) -> None:
        """追加上传日志。"""
        log_path = os.path.join(self.data_dir, "add.log")

        def write_log():
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(
                    f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | @{user_id} "
                    f'upload pid={pid} to "{gallery_name}"\n'
                )

        await asyncio.to_thread(write_log)

    # ==================== 百度网盘同步 ====================

    async def _sync_loop(self) -> None:
        """定时同步画廊到百度网盘。

        使用 bypy 命令行工具，按配置的 sync_times 定时执行。
        """
        import subprocess

        while True:
            try:
                # 计算下一次同步时间
                now = datetime.now()
                next_time = self._calc_next_sync(now)
                wait_seconds = (next_time - now).total_seconds()
                if wait_seconds > 0:
                    await asyncio.sleep(wait_seconds)
                await self._do_sync()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"画廊同步循环出错: {get_exc_desc(e)}")
                await asyncio.sleep(60)

    def _calc_next_sync(self, now: datetime) -> datetime:
        """计算下一次同步时间点。"""
        candidates = []
        for t in self.sync_times:
            try:
                h, m, s = int(t[0]), int(t[1]), int(t[2])
                target = now.replace(hour=h, minute=m, second=s, microsecond=0)
                if target <= now:
                    # 已过今日该时间点，顺延到明天
                    target = target + timedelta(days=1)
                candidates.append(target)
            except Exception:
                continue
        if not candidates:
            # 默认每天 3:30
            target = now.replace(hour=3, minute=30, second=0, microsecond=0)
            if target <= now:
                target = target + timedelta(days=1)
            return target
        return min(candidates)

    async def _do_sync(self) -> None:
        """执行一次同步：把每个画廊图片拷到临时目录，调用 bypy syncup。"""
        import subprocess

        local_dir = os.path.join(self.tmp_dir, "sync")
        for name, g in self.gallery_manager.get_all_galls().items():
            try:
                logger.info(f'开始同步画廊"{name}"到百度网盘({self.sync_remote_dir})')
                gall_local = os.path.join(local_dir, name)
                os.makedirs(gall_local, exist_ok=True)
                for p in g.pics:
                    if os.path.exists(p.path):
                        _, ext = os.path.splitext(os.path.basename(p.path))
                        dst = os.path.join(gall_local, f"{p.pid}{ext}")
                        await asyncio.to_thread(shutil.copy2, p.path, dst)

                command = [
                    "bypy",
                    "syncup",
                    gall_local,
                    os.path.join(self.sync_remote_dir, name),
                    "True",
                    "-v",
                ]
                process = await asyncio.to_thread(
                    subprocess.Popen,
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                )
                while True:
                    output = await asyncio.to_thread(process.stdout.readline)
                    if output == "" and process.poll() is not None:
                        break
                    if output and self.sync_verbose:
                        logger.info(f"[bypy] {output.strip()}")
                if process.returncode != 0:
                    raise Exception(f"bypy执行失败: code={process.returncode}")
                logger.info(f'画廊"{name}"同步完成')
            except Exception as e:
                logger.error(f'同步画廊"{name}"失败: {get_exc_desc(e)}')
            finally:
                # 清理当前画廊的临时目录
                try:
                    if os.path.isdir(gall_local):
                        shutil.rmtree(gall_local, ignore_errors=True)
                except Exception:
                    pass
