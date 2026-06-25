"""
涩图插件 — 检测关键词，从 data/setu/ 随机发图，支持撤回。
"""
import os
import random
import glob

from nonebot import on_message, get_bot
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageSegment

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETU_DIR = os.path.join(BASE_DIR, "data", "setu")

setu = on_message(priority=5, block=False)

# 最近发送的图片消息 ID（用于撤回）
_recent_images: list[dict] = []  # [{group_id, message_id}]

SETU_COMMANDS: tuple[str, ...] = (
    "牛牛喵快逃",
    "来点色图",
    "三连冲",
    "五连冲",
)


def _list_setu() -> list[str]:
    """列出 setu 文件夹中所有图片"""
    if not os.path.exists(SETU_DIR):
        return []
    files = []
    for ext in ("*.gif", "*.jpg", "*.jpeg", "*.png", "*.webp", "*.bmp"):
        files.extend(glob.glob(os.path.join(SETU_DIR, ext)))
    return files


def _random_setu(count: int) -> list[str]:
    """随机选 count 张不重复的图，不足则全部返回"""
    all_files = _list_setu()
    if not all_files:
        return []
    if count >= len(all_files):
        return random.sample(all_files, len(all_files))
    return random.sample(all_files, count)


def _file_to_segment(filepath: str) -> MessageSegment:
    uri = "file:///" + filepath.replace("\\", "/")
    return MessageSegment.image(file=uri)


async def _recall_all(group_id: int):
    """撤回该群最近由 bot 发送的所有图片"""
    global _recent_images
    bot = get_bot()
    recalled = 0

    for item in list(_recent_images):
        if item["group_id"] == group_id:
            try:
                await bot.delete_msg(message_id=item["message_id"])
                recalled += 1
            except Exception as e:
                print(f"[setu] recall error for msg {item['message_id']}: {e}")

    # 清理已撤回的记录
    _recent_images = [r for r in _recent_images if r["group_id"] != group_id]
    return recalled


@setu.handle()
async def _(event: GroupMessageEvent):
    global _recent_images
    group_id = event.group_id
    text = event.get_plaintext().strip()
    files = _list_setu()

    # ── 牛牛喵快逃：撤回所有图片 ──
    if text == "牛牛喵快逃":
        if not _recent_images:
            await setu.finish("没有要撤回的图片喵~")
        n = await _recall_all(group_id)
        await setu.finish(f"🏃‍♀️ 撤回了 {n} 张图！溜了溜了")
        return

    # ── 没有 setu 文件 ──
    if not files:
        return  # 静默，让 ai_chat 正常处理

    count = 0

    if text == "来点色图":
        count = 1
    elif text == "三连冲":
        count = 3
    elif text == "五连冲":
        count = 5
    else:
        return  # 不匹配，让 ai_chat 处理

    # 选图
    picked = _random_setu(count)

    if not picked:
        await setu.finish("图库空了喵…")
        return

    # 发送图片
    bot = get_bot()
    sent_count = 0
    for fp in picked:
        try:
            result = await bot.send_group_msg(
                group_id=group_id,
                message=_file_to_segment(fp),
            )
            # send_group_msg 返回 {"message_id": 12345}，取实际 id
            msg_id = result.get("message_id", 0) if isinstance(result, dict) else result
            _recent_images.append({
                "group_id": group_id,
                "message_id": int(msg_id),
            })
            sent_count += 1
        except Exception as e:
            print(f"[setu] send error: {e}")

    # 限制撤回列表最多保留 50 条
    if len(_recent_images) > 50:
        _recent_images = _recent_images[-50:]

    if sent_count > 0 and count > 1:
        await bot.send_group_msg(
            group_id=group_id,
            message=f"已发送 {sent_count} 张 {random.choice(['涩图', '好图', '美图', '图图'])}~",
        )
