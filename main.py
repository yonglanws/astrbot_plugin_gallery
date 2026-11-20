"""AstrBot 画廊插件主入口。

移植自 lunabot 的画廊服务。指令通过 @filter.command 以 AstrBot 原生方式
注册，由 @机器人 / 唤醒前缀 / 私聊触发；配置开启 listen_all_messages 后
额外监听所有消息，未唤醒的裸指令（含无空格写法）也能触发。
持久化数据存放于 data/plugin_data/astrbot_plugin_gallery/。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import shutil
import uuid
from datetime import datetime

import astrbot.api.message_components as Comp
from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.star.star_tools import StarTools
from astrbot.core.utils.io import download_image_by_url

from .gallery_manager import (
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

# "以文件方式发送"的图片按扩展名识别：文件名/路径/链接带这些扩展名才尝试提取
IMAGE_FILE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

# 单张网络图片下载超时（秒）与重试次数：避免一条慢链接（aiohttp 默认
# total=300s）把整批上传和回执一起拖住
IMAGE_DOWNLOAD_TIMEOUT = 20
IMAGE_DOWNLOAD_RETRIES = 1


class SilentPassException(Exception):
    """静默放行：不回复任何内容，也不停止事件传播（消息继续交给 LLM/其他插件）。"""


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
        self.listen_all_messages = bool(
            self.config.get("listen_all_messages", False)
        )
        review = self.config.get("ai_review", {}) or {}
        self.ai_review_enable = bool(review.get("enable", False))
        self.ai_review_provider_id = str(review.get("provider_id", "") or "")
        self.ai_review_notice_threshold = int(
            review.get("batch_notice_threshold", 10)
        )
        self.ai_review_prompt = str(
            review.get("prompt")
            or (
                "你是群表情画廊的图片审核员，判断这张图片能否收录到表情/梗图库。"
                "不允许：色情低俗、血腥暴力、政治敏感、赌博毒品等违法违规内容、"
                "广告引流（二维码/联系方式）。"
                "允许：普通表情包、梗图、动漫/游戏/生活截图等日常内容。"
                '只输出 JSON，不要输出其他任何内容：通过时输出 {"pass": true}，'
                '不通过时输出 {"pass": false, "reason": "简短原因"}'
            )
        )

    async def initialize(self):
        """插件初始化：加载数据。"""
        self.gallery_manager.ensure_loaded()
        self.history_manager.ensure_loaded()

    async def terminate(self):
        logger.info("画廊插件已停止")

    # ==================== 指令路由 ====================

    # 画廊不存在错误文案（画廊名不允许含引号，此匹配不会误伤其他错误）
    _GALL_NOT_FOUND_RE = re.compile(r'画廊".+"不存在')

    # ---- AstrBot 原生指令注册（主要使用方式）----
    # 通过 @机器人 / 唤醒前缀(如 /) / 私聊触发，由 AstrBot 唤醒与指令系统接管。
    # 原生指令要求指令名后有空格（AstrBot CommandFilter 限制），
    # "看miku"这类无空格裸指令由下方 on_message 在 listen_all_messages
    # 开启时兜底。所有指令的实际处理统一走 _handle_cmd。
    # 注意：原生指令 handler 必须定义在 on_message 之前——AstrBot 按
    # 注册顺序执行 handler，先处理并 stop_event 可避免兼容监听器重复响应。

    @filter.command("看")
    async def cmd_pick_native(self, event: AstrMessageEvent):
        """看画廊图片：看 画廊名 [x数量|-序号] 或 看 图片pid..."""
        async for r in self._handle_cmd(event):
            yield r

    @filter.command("看所有", alias={"看全部"})
    async def cmd_list_native(self, event: AstrMessageEvent):
        """查看所有画廊列表，或指定画廊的图片网格"""
        async for r in self._handle_cmd(event):
            yield r

    @filter.command("上传", alias={"添加"})
    async def cmd_add_native(self, event: AstrMessageEvent):
        """上传图片到画廊：上传 画廊名 [force]（回复/附带图片，支持文件形式）"""
        async for r in self._handle_cmd(event):
            yield r

    @filter.command("取消上传", alias={"撤销上传", "回退上传"})
    async def cmd_cancel_native(self, event: AstrMessageEvent):
        """撤销最近一次上传；管理员可加记录ID撤销指定记录"""
        async for r in self._handle_cmd(event):
            yield r

    @filter.command("上传记录")
    async def cmd_record_native(self, event: AstrMessageEvent):
        """查看指定上传记录：上传记录 记录ID"""
        async for r in self._handle_cmd(event):
            yield r

    @filter.command("创建画廊")
    async def cmd_open_native(self, event: AstrMessageEvent):
        """创建画廊：创建画廊 画廊名（默认允许上传）"""
        async for r in self._handle_cmd(event):
            yield r

    @filter.command("添加别名")
    async def cmd_alias_add_native(self, event: AstrMessageEvent):
        """添加画廊别名：添加别名 画廊名 别名"""
        async for r in self._handle_cmd(event):
            yield r

    @filter.command("gall")
    async def cmd_gall_native(self, event: AstrMessageEvent):
        """画廊管理：open/close/alias/mode/cover/del/replace/reload/check/log"""
        async for r in self._handle_cmd(event):
            yield r

    # ---- 全消息监听（兼容模式，仅 listen_all_messages 开启时生效）----

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """监听所有消息的兼容模式。

        仅当配置 listen_all_messages=True（允许监听所有消息）时启用：
        未唤醒的群消息里的裸指令（如直接发 看表情包、看miku 无空格写法）
        也会触发画廊指令。默认关闭时此监听器不做任何事，指令统一走
        上面的原生注册。唤醒消息会先被原生指令处理并停止传播，不会
        在这里重复响应。
        """
        if not self.listen_all_messages:
            return
        async for r in self._handle_cmd(event):
            yield r

    async def _handle_cmd(self, event: AstrMessageEvent):
        """解析消息并分发画廊指令（原生指令与兼容监听器共用）。

        AstrBot 的 message_str 已剥离唤醒前缀，这里再处理开头可选的 /，
        保证 /看 与 看 都能触发。
        enable_slash_prefix 只影响 listen_all_messages 全消息监听模式：
        开启时 /看 与 看 都生效；关闭时仅接受不带 / 的裸指令。
        原生指令路径由 AstrBot 的 wake_prefix 决定，本开关管不到（见下方注释）。
        """
        text = (event.message_str or "").strip()
        if not text:
            return

        # 规范化前缀：去掉可选的 /
        # 注意：原生指令路径上 AstrBot 在唤醒阶段已剥掉唤醒前缀（如 /），
        # 插件看到的 has_slash 恒为 False，所以 enable_slash_prefix 实际只
        # 对 listen_all_messages 的全消息监听模式生效；原生指令是否带 /
        # 由 AstrBot 的 wake_prefix 配置决定，此处无法改变。
        has_slash = text.startswith("/")
        body = text[1:].strip() if has_slash else text
        if not body:
            return

        # 关闭斜杠兼容时，拒绝带 / 的裸消息指令（只走裸指令）
        if not self.enable_slash_prefix and has_slash:
            return

        # 前缀匹配指令（兼容原版 lunabot：看miku / 看表情 中间可以没有空格）
        parsed = self._parse_cmd(body)
        if not parsed:
            return
        cmd, args = parsed
        # 排查用：消息到达插件并匹配到画廊指令时必有此日志。
        # 若发了指令但日志中无此行，说明消息未到达插件（唤醒/连接/被其他插件拦截）。
        logger.info(
            f"画廊指令触发: [{cmd}] {args} (platform={event.get_platform_name()}, "
            f"session={event.unified_msg_origin})"
        )

        # 匹配到画廊指令，处理后停止事件传播（避免继续触发 LLM 等）
        try:
            async for result in self._dispatch(event, cmd, args):
                yield result
        except ReplyException as e:
            if cmd in ("看", "看所有", "看全部") and self._GALL_NOT_FOUND_RE.fullmatch(
                str(e)
            ):
                # 查询类指令遇画廊不存在：多为普通聊天误触发，静默放行不拦截
                return
            # 写操作（上传/gall 等）必须提示错误，否则表现为无响应难以排查
            yield event.plain_result(str(e))
        except GalleryPicRepeatedException as e:
            # 查重命中是预期交互，直接回执，不记 error 日志
            yield event.plain_result(str(e))
        except AssertionError as e:
            # 断言文案对用户有意义（如画廊名无效），保留回执并补日志
            logger.warning(f"画廊指令断言失败 [{cmd}]: {e}")
            yield event.plain_result(str(e))
        except SilentPassException:
            # 「看」参数缺失/无法解析：群聊里极易误触发，静默放行不回复不拦截
            return
        except Exception as e:
            # 异常原文可能带服务器绝对路径等信息，只进日志，群里回固定文案
            logger.error(
                f"画廊指令处理出错 [{cmd}]: {get_exc_desc(e)}", exc_info=True
            )
            yield event.plain_result("处理出错，已记录日志")
        event.stop_event()

    # 长指令必须排在短指令前面，避免「看所有」被拆成「看」+「所有」
    _COMMANDS = (
        "看所有", "看全部",
        "取消上传", "撤销上传", "回退上传",
        "上传记录",
        "创建画廊",
        # "添加别名" 必须排在 "添加" 前面，否则会被前缀匹配成上传指令
        "添加别名",
        "上传", "添加",
        "看",
        "gall",
    )

    # 写操作类中文指令：指令名后必须有空白或到行尾才算命中。
    # 否则「创建画廊了」会真建一个叫「了」的画廊并落盘、
    # 「添加失败了」会被当成上传指令抢答普通群聊。查询类（看/看所有/看全部）
    # 保持无空格前缀匹配，README 明确支持「看miku」这种写法。
    _WRITE_COMMANDS = {
        "取消上传", "撤销上传", "回退上传",
        "上传记录",
        "创建画廊",
        "添加别名",
    }

    # 「上传奶龙」「添加奶龙」这类无空格写法很常见，所以「上传/添加」不强制空格；
    # 但普通聊天里「上传失败了」同样以「上传」开头，于是折中：无空格时，
    # 后面第一个 token 必须正好是已有画廊名/别名才当指令，否则放行给 LLM。
    _NAME_AWARE_COMMANDS = {"上传", "添加"}

    def _parse_cmd(self, body: str) -> tuple[str, str] | None:
        """从消息正文解析画廊指令。

        与原版 lunabot CmdHandler 一致：按最长前缀匹配，中文指令后可以不空格。
        写操作类指令例外：指令名后必须是空白或行尾（见 _WRITE_COMMANDS）；
        「上传/添加」额外允许无空格，但后面必须是已有画廊名/别名
        （见 _NAME_AWARE_COMMANDS）。
        ASCII 指令（gall）要求词边界，避免 gallery 误触发 gall。
        """
        for cmd in self._COMMANDS:
            if not body.startswith(cmd):
                continue
            rest = body[len(cmd):]
            if cmd in self._NAME_AWARE_COMMANDS:
                if rest and not rest[0].isspace():
                    self.gallery_manager.ensure_loaded()
                    token = rest.split(None, 1)[0]
                    if self.gallery_manager.find_gall(token) is None:
                        continue
            elif cmd in self._WRITE_COMMANDS:
                if rest and not rest[0].isspace():
                    continue
            elif rest and cmd.isascii() and rest[0].isalnum():
                continue
            return cmd, rest.strip()
        return None

    async def _dispatch(self, event: AstrMessageEvent, cmd: str, args: str):
        """根据指令名分发到对应处理器。"""
        # 看图（普通用户）：内部直发合并消息链，不经过分段回复管线
        if cmd == "看":
            await self._cmd_pick(event, args, silent_usage=True)
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
        elif cmd == "创建画廊":
            async for r in self._gall_open(event, args):
                yield r
        elif cmd == "添加别名":
            parts = args.split(None, 1)
            if len(parts) < 2:
                yield event.plain_result("使用方式: /添加别名 画廊名 别名")
                return
            async for r in self._gall_alias_add(event, parts[0], parts[1].strip()):
                yield r
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
        """分发 /gall 子指令（管理员指令在此校验权限，管理员即 AstrBot
        配置 admins_id 中的用户，可用 event.is_admin() 识别）。"""
        is_admin = event.is_admin()

        # 所有用户可用：创建画廊（默认 edit 模式，允许上传）
        if sub == "open":
            async for r in self._gall_open(event, args):
                yield r
        # 管理员专属指令
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
            # 添加别名所有用户可用；删除别名仅管理员
            if not args:
                yield event.plain_result(
                    "使用方式: /gall alias add 画廊名称 别名\n"
                    "/gall alias del 画廊名称 别名\n"
                    "也可以用: /添加别名 画廊名 别名"
                )
                return
            ap = args.split(None, 2)
            if len(ap) < 2:
                yield event.plain_result(
                    "使用方式: /gall alias add 画廊名称 别名\n"
                    "/gall alias del 画廊名称 别名\n"
                    "也可以用: /添加别名 画廊名 别名"
                )
                return
            op = ap[0]
            if op not in ("add", "del", "remove"):
                yield event.plain_result(
                    "使用方式: /gall alias add 画廊名称 别名\n"
                    "/gall alias del 画廊名称 别名\n"
                    "也可以用: /添加别名 画廊名 别名"
                )
                return
            if op != "add":
                self._require_admin(is_admin)
            if len(ap) < 3:
                yield event.plain_result(f"使用方式: /gall alias {op} 画廊名称 别名")
                return
            gall_name, alias = ap[1], ap[2]
            if op == "add":
                async for r in self._gall_alias_add(event, gall_name, alias):
                    yield r
            else:
                async with self.gallery_manager._lock:
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
            raise ReplyException(
                "该指令仅限管理员使用（管理员在 AstrBot 配置的 admins_id 中设置）"
            )

    def _gall_help(self) -> str:
        return (
            "画廊指令：\n"
            "看图: /看 画廊名 | /看 画廊名 x2 | /看 画廊名 -1 | /看 123 456\n"
            "画廊列表: /看所有 | /看所有 画廊名\n"
            "上传: (回复图片) /上传 画廊名 [force]\n"
            "  (支持附带/回复/转发里的图片，图片也可用文件形式发送)\n"
            "创建画廊: /创建画廊 画廊名 (或 /gall open 画廊名)\n"
            "添加别名: /添加别名 画廊名 别名 (或 /gall alias add 画廊名 别名)\n"
            "撤销: /取消上传 [记录ID(管理员)]\n"
            "记录: /上传记录 记录ID\n"
            "管理员(/gall 子指令，管理员在 AstrBot 配置 admins_id 中设置):\n"
            "  close 删除画廊 | del 删图 | replace 换图 | reload/check/log\n"
            "  mode 模式 | cover 封面 | alias del 删别名"
        )

    # ==================== 指令实现 ====================

    @staticmethod
    def _pick_usage(silent: bool, msg: str) -> None:
        """「看」参数缺失/无法解析：silent 时静默放行，否则沿用用法提示。

        /gall pick 仍保留用法提示（显式调用，不会是误触发）。
        """
        if silent:
            raise SilentPassException()
        raise ReplyException(msg)

    async def _cmd_pick(
        self, event: AstrMessageEvent, args: str, silent_usage: bool = False
    ):
        """看图：随机或按 pid 查看画廊图片。

        silent_usage=True（「看」指令）时，参数缺失或无法解析一律静默放行：
        群聊里以「看」开头的普通消息很常见，回复用法提示会造成大量误触发。
        """
        if not args:
            self._pick_usage(
                silent_usage,
                "使用方式:\n/看 画廊名称\n/看 画廊名称 x2\n/看 画廊名称 -1\n/看 123 456...",
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
                    self._pick_usage(
                        silent_usage,
                        "使用方式:\n/看 画廊名称\n/看 画廊名称 x2\n/看 画廊名称 -1",
                    )
                if not a.strip():
                    # 「看 - 1」这类缺少画廊名的写法：同样静默放行
                    self._pick_usage(
                        silent_usage, "使用方式:\n/看 画廊名称\n/看 画廊名称 -1"
                    )
                g = self.gallery_manager.find_gall(a.strip(), raise_if_nofound=True)
                if len(g.pics) < nindex:
                    raise ReplyException(f'画廊"{a.strip()}"仅有{len(g.pics)}张图片')
                pics = [g.pics[-nindex]]
            elif "x" in a:
                a, num_str = a.rsplit("x", 1)
                try:
                    num = int(num_str)
                except ValueError:
                    self._pick_usage(
                        silent_usage, "使用方式:\n/看 画廊名称\n/看 画廊名称 x2"
                    )
                if not a.strip():
                    # 「看x2」这类缺少画廊名的写法：同样静默放行
                    self._pick_usage(
                        silent_usage, "使用方式:\n/看 画廊名称\n/看 画廊名称 x2"
                    )
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
        # 按空白分词识别 force：名字含 force 的画廊不应被误伤
        tokens = args.split()
        if "force" in tokens:
            check_duplicated = False
            tokens = [t for t in tokens if t != "force"]
        name = " ".join(tokens)
        g = self.gallery_manager.find_gall(name, raise_if_nofound=True)

        if not event.is_admin() and g.mode != GalleryMode.Edit:
            raise ReplyException(f'画廊"{name}"不允许上传图片')

        # 提取消息中的图片。stats 统计 url/file 都取不到、且无法转为本地
        # 文件的图片组件数，这类图计入回执分母，避免「成功上传1/2」静默丢图
        stats: dict[str, int] = {}
        image_urls = await self._extract_image_urls(event, stats)
        unrecognized = stats.get("unrecognized", 0)
        if not image_urls:
            if unrecognized:
                raise ReplyException(
                    f"{unrecognized} 张图片无法识别，请重新以图片形式发送后再上传"
                )
            raise ReplyException("请回复或附带至少一张图片后再上传")

        total = len(image_urls) + unrecognized
        start_time = datetime.now()
        ok_list: list[int] = []
        err_msg = ""
        repeats: list[tuple[str, int]] = []  # (待上传图临时路径, 相似 pid)
        paths: list[str] = []

        try:
            # 并发下载/取本地路径。超时与重试在 _localize_image/_download_image
            # 内部处理，单张慢链接不会拖住整批上传和回执。
            async def fetch_local(url: str) -> str:
                try:
                    return await self._localize_image(url)
                except Exception as e:
                    logger.warning(f"获取图片失败: {get_exc_desc(e)}")
                    return ""

            paths = await asyncio.gather(*[fetch_local(u) for u in image_urls])

            # AI 审核（配置启用时）。图片较多先提示等待，再并发送审；
            # 审核服务异常或输出无法解析时降级放行，不阻塞上传。
            review_results: list[tuple[bool, str] | None] = [None] * len(paths)
            if self.ai_review_enable:
                if len(paths) >= self.ai_review_notice_threshold:
                    yield event.plain_result(
                        f"一次上传的图片较多（{len(paths)}张），"
                        "正在进行 AI 审核，请耐心等待..."
                    )
                to_review = [i for i, p in enumerate(paths) if p]
                if to_review:
                    sem = asyncio.Semaphore(3)

                    async def _review_one(i: int):
                        async with sem:
                            review_results[i] = await self._ai_review_image(paths[i])

                    await asyncio.gather(*[_review_one(i) for i in to_review])

            for i, path in enumerate(paths, 1):
                if not path or not os.path.exists(path):
                    err_msg += f"第{i}张图片下载失败\n"
                    continue
                review = review_results[i - 1]
                if review is not None and not review[0]:
                    err_msg += f"第{i}张图片未通过AI审核: {review[1] or '内容不合规'}\n"
                    continue
                try:
                    await asyncio.to_thread(
                        process_image_for_gallery, path, 1, self.size_limit_mb
                    )
                    # 处理可能转换了格式（静态图转 gif），对齐扩展名避免 QQ 按后缀误判
                    path = await asyncio.to_thread(self._fix_ext_by_format, path)
                    paths[i - 1] = path  # 同步回列表，保证清理阶段能删除
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

            if unrecognized:
                err_msg = f"{unrecognized} 张图片无法识别已跳过\n" + err_msg

            cost = (datetime.now() - start_time).total_seconds()
            logger.info(f'上传{len(ok_list)}/{total}张图片到画廊"{name}"完成, 耗时{cost:.2f}秒')

            hid = await self.history_manager.add_history(event.get_sender_id(), ok_list) if ok_list else None

            # 渲染重复对比图；渲染失败只降级为纯文本回执，不影响上传结果
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
                try:
                    repeat_img_path = await image_utils.render_repeat(
                        pairs,
                        hint='查重错误可使用"/上传 画廊名 force"强制上传图片',
                        save_dir=self.tmp_dir,
                    )
                except Exception as e:
                    logger.warning(f"渲染重复对比图失败，降级为纯文本回执: {get_exc_desc(e)}")

            msg = f'成功上传{len(ok_list)}/{total}张图片到"{name}"'
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
        finally:
            # 成功/异常/超时都清理本次下载的临时文件
            # （图片已拷贝到画廊目录或已渲染对比图）
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

    async def _gall_open(self, event: AstrMessageEvent, args: str):
        """创建画廊（所有用户可用，默认 edit 模式，允许上传）。"""
        name = args.strip()
        async with self.gallery_manager._lock:
            self.gallery_manager.open_gall(name)
            await self.gallery_manager._save()
        yield event.plain_result(f'画廊"{name}"创建成功')

    async def _gall_alias_add(self, event: AstrMessageEvent, gall_name: str, alias: str):
        """添加画廊别名（所有用户可用）。"""
        async with self.gallery_manager._lock:
            self.gallery_manager.add_gall_alias(gall_name, alias)
            # 先落库再回执：_save 失败时不向用户报告成功
            await self.gallery_manager._save()
            yield event.plain_result(f'画廊"{gall_name}"添加别名"{alias}"成功')

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
        args = args.strip()
        # 只有单个「数字-数字」token 才按区间解析（中间不能有空格），
        # 避免把 /gall del 1 -2 里的负 pid 当成区间 1..2；
        # 其余按空白分词逐个 int（支持负数）
        m = re.fullmatch(r"(\d+)-(\d+)", args)
        usage = "使用方式:\n/gall del 123 456 -1 -2 ...\n/gall del 123-456 (最多连续20张)"
        try:
            if m:
                l, r = int(m.group(1)), int(m.group(2))
                # 先按起止校验张数再展开 range，避免超大区间分配 GB 级列表
                if r >= l and r - l + 1 > 20:
                    raise ReplyException("一次最多删除20张连续图片")
                pids = list(range(l, r + 1)) if r >= l else []
            else:
                pids = [int(s) for s in args.split()]
        except ValueError:
            raise ReplyException(usage)
        if not pids:
            raise ReplyException(usage)
        # 禁止跨画廊删除
        gall_names = set()
        for pid in pids:
            pic = self.gallery_manager.find_pic(pid, raise_if_nofound=False)
            if pic:
                gall_names.add(pic.gall_name)
        if len(gall_names) > 1:
            raise ReplyException("禁止跨画廊删除图片")

        ok_list, err_list = await self.gallery_manager.async_del_pics(pids)

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

        def read_lines() -> list[str]:
            if not os.path.exists(log_path):
                return []
            with open(log_path, "r", encoding="utf-8") as f:
                return f.readlines()

        # 日志可能很大，读取放到线程里，避免阻塞事件循环
        lines = await asyncio.to_thread(read_lines)
        # 精确匹配 pid=1，避免子串命中 pid=10/100 的记录
        pattern = re.compile(r"\bpid=(\d+)\b")
        for line in lines:
            found = pattern.search(line)
            if found and int(found.group(1)) == pid:
                yield event.plain_result(line.strip())
                return
        raise ReplyException(f"pid={pid}的上传记录不存在")

    async def _gall_replace(self, event: AstrMessageEvent, args: str):
        """替换指定 pid 图片。force 关闭查重。"""
        # 按空白分词识别 force：名字/参数里含 force 时不应误判为强制，
        # 原版字符串 replace('force', '') 是按子串删除
        check_duplicated = True
        tokens = args.split()
        if "force" in tokens:
            check_duplicated = False
            tokens = [t for t in tokens if t != "force"]
        try:
            pid = int(" ".join(tokens))
        except ValueError:
            raise ReplyException("使用方式: /gall replace pid [force]")

        image_urls = await self._extract_image_urls(event)
        if not image_urls:
            raise ReplyException("请附加要替换的图片")
        local = await self._localize_image(image_urls[0])
        if not local or not os.path.exists(local):
            raise ReplyException("获取图片失败")
        try:
            await asyncio.to_thread(
                process_image_for_gallery, local, 1, self.size_limit_mb
            )
            local = await asyncio.to_thread(self._fix_ext_by_format, local)
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

    # ==================== 辅助方法 ====================

    @staticmethod
    def _is_fetchable_src(src: str | None) -> bool:
        """判断图片来源是否可获取：网络 URL / file:// / base64 / 实际存在的本地路径。

        NapCat 的 Image.file 可能是内部 hash（如 "ABC123.image"），既非路径
        也非 URL，直接传给下载器只会报 InvalidUrl——视为无效来源。
        """
        if not src:
            return False
        if src.startswith(("http://", "https://", "file:///", "base64://")):
            return True
        return os.path.exists(src)

    async def _download_image(self, src: str) -> str:
        """下载网络图片：单张超时 + 一次重试。

        aiohttp 默认 total=300s，一条慢链接会让整批上传和回执一起卡住，
        这里用 asyncio.wait_for 给单张下载封顶。
        """
        last_exc: Exception | None = None
        for attempt in range(IMAGE_DOWNLOAD_RETRIES + 1):
            try:
                return await asyncio.wait_for(
                    download_image_by_url(src), timeout=IMAGE_DOWNLOAD_TIMEOUT
                )
            except Exception as e:
                last_exc = e
                logger.warning(
                    f"下载图片失败(第{attempt + 1}/{IMAGE_DOWNLOAD_RETRIES + 1}次): "
                    f"{get_exc_desc(e)}"
                )
        raise last_exc

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
            local = await self._download_image(src)
            return await asyncio.to_thread(self._fix_ext_by_format, local)
        if src.startswith("base64://"):
            import base64 as _b64
            data = _b64.b64decode(src[9:])
            # uuid 后缀避免并发本地化时微秒时间戳撞名（Windows 时钟精度不足）
            dst = os.path.join(
                self.tmp_dir,
                f"imgseg_{datetime.now().strftime('%Y%m%d%H%M%S%f')}_{uuid.uuid4().hex[:8]}",
            )
            with open(dst + ".bin", "wb") as f:
                f.write(data)
            return await asyncio.to_thread(self._fix_ext_by_format, dst + ".bin")
        # 本地路径（绝对或相对）。aiocqhttp 常把 GIF 落到 media_image_xxx.jpg/png
        if os.path.exists(src):
            return await asyncio.to_thread(
                self._copy_with_correct_ext, os.path.abspath(src)
            )
        # 兜底：尝试当 URL 下载（例如没带 http 前缀的链接）
        local = await self._download_image(src)
        return await asyncio.to_thread(self._fix_ext_by_format, local)

    def _copy_with_correct_ext(self, path: str) -> str:
        """复制到 tmp 后再按真实格式改扩展名，避免改写协议端缓存文件。"""
        if not path or not os.path.exists(path):
            return path
        # uuid 后缀避免并发本地化时微秒时间戳撞名（Windows 时钟精度不足）
        dst = os.path.join(
            self.tmp_dir,
            f"img_{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
            f"_{uuid.uuid4().hex[:8]}{os.path.splitext(path)[1]}",
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

    # ==================== AI 审核 ====================

    def _get_review_provider(self):
        """取审核用的 LLM 提供者：配置了 provider_id 用指定的，否则用当前默认。"""
        try:
            if self.ai_review_provider_id:
                return self.context.get_provider_by_id(self.ai_review_provider_id)
            return self.context.get_using_provider()
        except Exception as e:
            logger.warning(f"获取 LLM 提供者失败: {get_exc_desc(e)}")
            return None

    async def _ai_review_image(self, path: str) -> tuple[bool, str]:
        """用 LLM 审核单张图片，返回 (是否通过, 原因)。

        图片以本地路径传给 text_chat（AstrBot 会自动转 base64）。
        审核服务不可用、请求失败或输出无法解析时放行（降级不阻塞上传），
        仅记录日志。
        """
        provider = self._get_review_provider()
        if provider is None:
            logger.warning("AI审核已启用但没有可用的 LLM 提供者，图片直接放行")
            return True, ""
        try:
            resp = await asyncio.wait_for(
                provider.text_chat(
                    prompt=self.ai_review_prompt,
                    session_id=None,
                    image_urls=[path],
                    contexts=[],
                ),
                timeout=90,
            )
        except Exception as e:
            logger.warning(f"AI审核请求失败（放行）: {get_exc_desc(e)}")
            return True, ""
        text = (getattr(resp, "completion_text", "") or "").strip()
        passed, reason = self._parse_review_result(text)
        if not passed:
            logger.info(f"AI审核不通过({os.path.basename(path)}): {reason}")
        return passed, reason

    @staticmethod
    def _review_pass_to_bool(value) -> bool | None:
        """把 AI 输出里的 pass 字段转成布尔；无法识别返回 None。"""
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            v = value.strip().lower()
            if v == "true":
                return True
            if v == "false":
                return False
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        return None

    @staticmethod
    def _parse_review_result(text: str) -> tuple[bool, str]:
        """解析模型审核输出。

        pass 字段严格解析：bool 直接使用；"true"/"false"（大小写不敏感）
        与数字 1/0 转布尔。字段缺失或无法识别时按「解析失败」放行
        （保持 fail-open），并记录原始输出——避免 bool("false") 把拒绝判成通过。
        """
        m = re.search(r"\{[^{}]*\}", text, re.S)
        if m:
            try:
                data = json.loads(m.group(0))
            except Exception:
                data = None
            if isinstance(data, dict) and "pass" in data:
                passed = GalleryPlugin._review_pass_to_bool(data.get("pass"))
                if passed is not None:
                    return passed, str(data.get("reason") or "")
                logger.warning(f"AI审核输出 pass 字段无法识别，按放行处理: {text[:200]}")
                return True, ""
        lowered = text.lower()
        if (
            '"pass": false' in lowered
            or "'pass': false" in lowered
            or "不通过" in text
        ):
            return False, text[:100]
        logger.warning(f"AI审核输出无法解析，按放行处理: {text[:200]}")
        return True, ""

    async def _extract_image_urls(
        self, event: AstrMessageEvent, stats: dict | None = None
    ) -> list[str]:
        """从消息中提取图片 URL。

        支持：直接图片、引用消息(Reply.chain)、合并转发消息——
        包括内联 Node/Nodes 节点与 Forward（通过 get_forward_msg API 拉取内容）。

        stats 不为 None 时写入 "unrecognized"：url/file 都取不到、且无法
        转为本地文件的 Image 组件数（供上传回执如实展示跳过张数）。
        """
        urls: list[str] = []
        unrecognized = 0
        seen_forward_ids: set[str] = set()
        seen_reply_ids: set[str] = set()

        async def image_url_of(comp) -> str | None:
            url = getattr(comp, "url", None)
            if self._is_fetchable_src(url):
                return url
            file = getattr(comp, "file", None)
            if self._is_fetchable_src(file):
                return file
            # url/file 都不可用（部分协议端只给图片 ID）：让组件自己落成本地文件，
            # 避免整张图被静默丢弃、回执却显示上传成功
            convert = getattr(comp, "convert_to_file_path", None)
            if callable(convert):
                try:
                    path = convert()
                    if inspect.isawaitable(path):
                        path = await path
                    if self._is_fetchable_src(path):
                        return path
                except Exception as e:
                    logger.warning(f"图片组件转本地文件失败: {get_exc_desc(e)}")
            return None

        async def walk(comps: list, depth: int):
            nonlocal unrecognized
            if depth > 4:
                return
            for comp in comps:
                try:
                    if isinstance(comp, Comp.Image):
                        url = await image_url_of(comp)
                        if url:
                            urls.append(url)
                        else:
                            unrecognized += 1
                    elif isinstance(comp, Comp.File):
                        # QQ"以文件方式发送"的图片以 File 组件到达
                        src = await self._src_of_file_comp(event, comp)
                        if src:
                            urls.append(src)
                    elif isinstance(comp, Comp.Reply):
                        rid = getattr(comp, "id", None)
                        chain = getattr(comp, "chain", None)
                        before = len(urls)
                        before_unrecognized = unrecognized
                        if chain:
                            await walk(chain, depth + 1)
                        if len(urls) == before and rid and str(rid) not in seen_reply_ids:
                            # chain 缺失、或其中图片字段不全（NapCat 部分版本
                            # 不给 url、file 只是内部 hash）时，回退 get_msg
                            # 拉取被引用消息的原始 segment
                            seen_reply_ids.add(str(rid))
                            segments = await self._fetch_reply_segments(event, rid)
                            urls.extend(
                                await self._urls_from_raw_segments(event, segments)
                            )
                            if len(urls) > before:
                                # get_msg 兜底拿到了原图：chain 里识别失败的组件
                                # 已从中恢复，不再计为「无法识别」
                                unrecognized = before_unrecognized
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
                        urls.extend(
                            await self._urls_from_raw_segments(event, segments)
                        )
                except Exception as e:
                    logger.warning(f"提取图片组件失败: {get_exc_desc(e)}")

        if event.message_obj and event.message_obj.message:
            await walk(event.message_obj.message, 0)
        if stats is not None:
            stats["unrecognized"] = stats.get("unrecognized", 0) + unrecognized
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

    async def _fetch_reply_segments(self, event: AstrMessageEvent, reply_id) -> list:
        """通过 aiocqhttp 的 get_msg API 拉取被引用消息内容（原始 segment 字典列表）。

        用于 Reply 组件未携带 chain 的兜底场景；非 aiocqhttp 平台或
        API 失败时返回空列表。
        """
        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
            AiocqhttpMessageEvent,
        )

        if not isinstance(event, AiocqhttpMessageEvent):
            logger.debug("当前平台不支持拉取引用消息内容")
            return []
        client = getattr(event, "bot", None)
        if client is None:
            return []
        res = None
        candidates = [{"message_id": reply_id}]
        if str(reply_id).isdigit():
            candidates.append({"message_id": int(reply_id)})
        for action_kwargs in candidates:
            try:
                res = await client.api.call_action("get_msg", **action_kwargs)
                if res:
                    break
            except Exception as e:
                logger.debug(f"get_msg({action_kwargs}) 失败: {get_exc_desc(e)}")
        if not isinstance(res, dict):
            return []
        segs = res.get("message") or res.get("content") or []
        if isinstance(segs, list):
            return [s for s in segs if isinstance(s, dict)]
        return []

    @staticmethod
    def _is_image_name(name: str | None) -> bool:
        return bool(name) and os.path.splitext(name)[1].lower() in IMAGE_FILE_EXTS

    async def _src_of_file_comp(self, event: AstrMessageEvent, comp) -> str:
        """把 File 组件解析为可获取的图片源（http URL / file:/// / 本地路径）。

        QQ"以文件方式发送"的图片以 File 组件（而非 Image 组件）到达。
        新版 AstrBot 的 File.file 是同步 property，在异步上下文里只会
        告警并返回空串，取本地路径要用 await File.get_file()；
        旧版 File 的 file 字段可能是 OneBot 内部 file_id（不可直接获取），
        此时回退到协议端 API 换取下载链接。
        """
        if not (
            self._is_image_name(getattr(comp, "name", None))
            or self._is_image_name(getattr(comp, "url", None))
            or self._is_image_name(getattr(comp, "file_", None))
            or self._is_image_name(getattr(comp, "file", None))
        ):
            return ""
        for field in ("url", "file_"):
            src = getattr(comp, field, None)
            if self._is_fetchable_src(src):
                return src
        legacy = getattr(comp, "file", None)
        if self._is_fetchable_src(legacy):
            return legacy
        get_file = getattr(comp, "get_file", None)
        if get_file is not None:
            try:
                src = await get_file(allow_return_url=True)
            except TypeError:
                src = await get_file()
            except Exception as e:
                logger.warning(f"获取文件消息内容失败: {get_exc_desc(e)}")
                return ""
            if self._is_fetchable_src(src):
                return src
        return await self._fetch_file_url(event, getattr(comp, "id", None))

    async def _fetch_file_url(self, event: AstrMessageEvent, fid) -> str:
        """通过 aiocqhttp 的 get_group_file_url / get_private_file_url
        把 OneBot file_id 换成文件下载链接；失败返回空串。"""
        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
            AiocqhttpMessageEvent,
        )

        if not isinstance(event, AiocqhttpMessageEvent) or not fid:
            return ""
        client = getattr(event, "bot", None)
        if client is None:
            return ""
        group_id = event.get_group_id()
        try:
            if group_id:
                res = await client.api.call_action(
                    "get_group_file_url", file_id=fid, group_id=int(group_id)
                )
            else:
                res = await client.api.call_action(
                    "get_private_file_url", file_id=fid
                )
        except Exception as e:
            logger.debug(f"获取文件下载链接(file_id={fid})失败: {get_exc_desc(e)}")
            return ""
        url = res.get("url") if isinstance(res, dict) else None
        return url if self._is_fetchable_src(url) else ""

    async def _urls_from_raw_segments(self, event: AstrMessageEvent, segments: list) -> list[str]:
        """从 OneBot 原始 segment 列表中提取图片 URL（含嵌套 node）。"""
        out: list[str] = []
        for seg in segments or []:
            if not isinstance(seg, dict):
                continue
            stype = seg.get("type")
            data = seg.get("data") or {}
            if stype == "image":
                url = data.get("url") or data.get("file")
                if url and self._is_fetchable_src(url):
                    out.append(url)
            elif stype == "file":
                # 以文件形式随转发/引用消息传来的图片
                name = (
                    data.get("file_name")
                    or data.get("name")
                    or data.get("url")
                    or data.get("file")
                    or ""
                )
                if not self._is_image_name(name):
                    continue
                url = data.get("url")
                if url and self._is_fetchable_src(url):
                    out.append(url)
                else:
                    src = await self._fetch_file_url(
                        event, data.get("file_id") or data.get("id")
                    )
                    if src:
                        out.append(src)
            elif stype == "node":
                inner = data.get("content") or data.get("message") or []
                out.extend(await self._urls_from_raw_segments(event, inner))
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
