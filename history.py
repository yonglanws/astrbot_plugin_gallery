"""上传历史记录与撤销。

修复原版 bug：原版用 len(history)+1 作为记录 id，若日后清理历史会复用 id。
这里改用单独维护的 next_id 自增计数器，保证 id 全局唯一不回退。
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from astrbot.api import logger

from .gallery_manager import (
    ReplyException,
    get_exc_desc,
    quarantine_corrupt_file,
)

if TYPE_CHECKING:
    from .gallery_manager import GalleryManager


HISTORY_DB_FILE = "add_history.json"


def _assert_and_reply(cond, msg: str) -> None:
    if not cond:
        raise ReplyException(msg)


class HistoryManager:
    """上传历史记录管理。数据持久化到 plugin_data 目录的 add_history.json。"""

    def __init__(self, data_dir: str, gallery_manager: "GalleryManager"):
        self._data_dir = data_dir
        self._db_path = os.path.join(data_dir, HISTORY_DB_FILE)
        self._gallery_manager = gallery_manager
        self._lock = asyncio.Lock()
        self._next_id = 1
        self._history: list[dict] = []
        self._loaded = False

    def _load(self) -> None:
        self._history = []
        self._next_id = 1

        if not os.path.exists(self._db_path):
            return

        try:
            with open(self._db_path, "r", encoding="utf-8") as f:
                db = json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            # 解析失败不能静默变空历史：先把坏文件改名留档，避免下次 _save 覆盖唯一副本
            backup = quarantine_corrupt_file(self._db_path, "上传历史")
            logger.error(
                f"上传历史文件损坏，已备份为 {backup or '(备份失败)'}，以空历史启动: {e}"
            )
            return
        except Exception as e:
            logger.error(
                f"读取上传历史失败，以空历史启动（保留原文件）: {get_exc_desc(e)}"
            )
            return

        # 结构解析整体兜底：任何意外都不能让 initialize 失败
        try:
            if not isinstance(db, dict):
                raise ValueError(f"历史文件根节点应为对象，实际为 {type(db).__name__}")

            raw_next = db.get("next_id", 1)
            if (
                isinstance(raw_next, int)
                and not isinstance(raw_next, bool)
                and raw_next > 0
            ):
                self._next_id = raw_next
            else:
                logger.warning(f"上传历史 next_id={raw_next!r} 非法，重置为 1")

            raw_history = db.get("history", [])
            if not isinstance(raw_history, list):
                raise ValueError("history 字段应为数组")

            max_id = 0
            for idx, item in enumerate(raw_history):
                rec = self._normalize_record(item)
                if rec is None:
                    logger.warning(f"跳过非法上传历史记录 #{idx}: {item!r}")
                    continue
                self._history.append(rec)
                if rec["id"] > max_id:
                    max_id = rec["id"]

            # 兜底：若历史中有 id 不低于 next_id，则对齐，避免复用 id
            if max_id >= self._next_id:
                if self._next_id > 1:
                    logger.warning(
                        f"上传历史 next_id={self._next_id} 不大于最大记录 id={max_id}，已修正"
                    )
                self._next_id = max_id + 1
        except Exception as e:
            logger.error(
                f"解析上传历史失败，保留已加载的 {len(self._history)} 条记录: "
                f"{get_exc_desc(e)}"
            )

    @staticmethod
    def _normalize_record(item: object) -> dict | None:
        """校验并规范化单条历史记录；缺 id 等非法记录返回 None。"""
        if not isinstance(item, dict):
            return None

        hid = item.get("id")
        if not isinstance(hid, int) or isinstance(hid, bool) or hid <= 0:
            return None

        pids = item.get("pids", [])
        if not isinstance(pids, list) or not all(
            isinstance(p, int) and not isinstance(p, bool) for p in pids
        ):
            logger.warning(f"上传历史#{hid} 的 pids 非法，已跳过该记录")
            return None

        uid = item.get("uid", "")
        if not isinstance(uid, str):
            logger.warning(f"上传历史#{hid} 的 uid 非法，已转为字符串")
            uid = str(uid)

        ts = item.get("ts", 0)
        if not isinstance(ts, (int, float)) or isinstance(ts, bool):
            logger.warning(f"上传历史#{hid} 的 ts 非法，已置 0（视为已过期）")
            ts = 0

        reverted = item.get("reverted", False)
        if not isinstance(reverted, bool):
            logger.warning(f"上传历史#{hid} 的 reverted 非法，已置 False")
            reverted = False

        return {
            "id": hid,
            "uid": uid,
            "pids": pids,
            "ts": float(ts),
            "reverted": reverted,
        }

    def _save_sync(self) -> None:
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        tmp = self._db_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(
                {"next_id": self._next_id, "history": self._history},
                f,
                ensure_ascii=False,
                indent=2,
            )
        os.replace(tmp, self._db_path)

    async def _save(self) -> None:
        await asyncio.to_thread(self._save_sync)

    def ensure_loaded(self) -> None:
        if not self._loaded:
            self._load()
            self._loaded = True

    # ---------- 记录操作 ----------

    async def add_history(self, user_id: str, pids: list[int]) -> int:
        """添加上传记录，返回记录 id。"""
        async with self._lock:
            self.ensure_loaded()
            hid = self._next_id
            self._next_id += 1
            self._history.append(
                {
                    "id": hid,
                    "uid": user_id,
                    "pids": pids,
                    "ts": datetime.now().timestamp(),
                    "reverted": False,
                }
            )
            await self._save()
            return hid

    def get_history(self, hid: int) -> dict:
        """根据记录 id 获取上传记录。"""
        self.ensure_loaded()
        for h in self._history:
            if h["id"] == hid:
                return h
        raise ReplyException(f"上传#{hid}不存在")

    async def revert_by_id(self, hid: int) -> tuple[dict, list[int], list[int]]:
        """根据记录 id 撤销上传（管理员），返回(记录, 成功 pids, 失败 pids)。"""
        async with self._lock:
            self.ensure_loaded()
            h = None
            for item in self._history:
                if item["id"] == hid:
                    h = item
                    break
            _assert_and_reply(h is not None, f"上传#{hid}不存在")
            _assert_and_reply(not h["reverted"], f"上传#{hid}已被撤销")

            ok_list, err_list = [], []
            # 持有画廊锁执行删除，保证与 add_pic/replace 等互斥
            async with self._gallery_manager._lock:
                for pid in h["pids"]:
                    try:
                        self._gallery_manager.del_pic(pid)
                        ok_list.append(pid)
                    except Exception as e:
                        logger.warning(f"撤销上传记录#{hid}时删除图片pid={pid}失败: {get_exc_desc(e)}")
                        err_list.append(pid)
                await self._gallery_manager._save()

            h["reverted"] = True
            await self._save()
            return h, ok_list, err_list

    async def revert_last_by_user(
        self, user_id: str, expired_hours: int
    ) -> tuple[dict, list[int], list[int]]:
        """撤销某个用户最近一次未撤销的上传，返回(记录, 成功 pids, 失败 pids)。"""
        async with self._lock:
            self.ensure_loaded()
            user_histories = [
                h for h in reversed(self._history)
                if h["uid"] == user_id and not h["reverted"]
            ]
            _assert_and_reply(user_histories, "你没有可撤销的上传记录")
            h = user_histories[0]
            _assert_and_reply(
                (datetime.now() - datetime.fromtimestamp(h["ts"]))
                < timedelta(hours=expired_hours),
                f"最近一次上传记录已超过{expired_hours}小时，无法撤销",
            )

            ok_list, err_list = [], []
            async with self._gallery_manager._lock:
                for pid in h["pids"]:
                    try:
                        self._gallery_manager.del_pic(pid)
                        ok_list.append(pid)
                    except Exception as e:
                        logger.warning(
                            f"撤销上传记录#{h['id']}时删除图片pid={pid}失败: {get_exc_desc(e)}"
                        )
                        err_list.append(pid)
                await self._gallery_manager._save()

            h["reverted"] = True
            await self._save()
            return h, ok_list, err_list
