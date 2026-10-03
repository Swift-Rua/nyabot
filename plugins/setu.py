"""
涩图插件 — 检测关键词，从 data/setu/ 随机发图，支持撤回。
"""
import os
import random
import glob
import asyncio

from nonebot import on_message, get_bot
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageSegment

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETU_DIR = os.path.join(BASE_DIR, "data", "setu")

setu = on_message(priority=5, block=False)

# 最近发送的图片消息 ID（用于撤回）
_recent_images: list[dict] = []  # [{group_id, message_id}]
_recall_lock = asyncio.Lock()
_image_batch_lock = asyncio.Lock()
_RECALL_TIMEOUT_SECONDS = 8
_NOTICE_TIMEOUT_SECONDS = 15
_RECALL_CONCURRENCY = 3
_IMAGE_SEND_TIMEOUT_SECONDS = 15

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

    async with _recall_lock:
        targets = [item for item in _recent_images if item["group_id"] == group_id]
        # 先摘出本次任务，避免并发的重复命令同时撤回同一批消息。
        _recent_images = [item for item in _recent_images if item["group_id"] != group_id]
        semaphore = asyncio.Semaphore(_RECALL_CONCURRENCY)

        async def recall_one(item: dict) -> bool:
            async with semaphore:
                try:
                    await asyncio.wait_for(
                        bot.delete_msg(message_id=item["message_id"]),
                        timeout=_RECALL_TIMEOUT_SECONDS,
                    )
                    return True
                except Exception as e:
                    print(f"[setu] recall error for msg {item['message_id']}: {e}")
                    return False

        results = await asyncio.gather(*(recall_one(item) for item in targets))
        recalled = sum(results)
        failed = len(results) - recalled
        return recalled, failed


async def _send_notice(bot, group_id: int, message: str) -> None:
    """发送进度或结果，避免通知 API 无限等待。"""
    try:
        await asyncio.wait_for(
            bot.send_group_msg(group_id=group_id, message=message),
            # NapCat's QQ sendMsg request currently times out after about 12s.
            # Let its own error return before cancelling the OneBot API call.
            timeout=_NOTICE_TIMEOUT_SECONDS,
        )
    except Exception as e:
        print(f"[setu] notice error: {e}")


@setu.handle()
async def _(event: GroupMessageEvent):
    global _recent_images
    group_id = event.group_id
    text = event.get_plaintext().strip()

    # ── 牛牛喵快逃：撤回所有图片 ──
    if text == "牛牛喵快逃":
        bot = get_bot()
        waiting_for_batch = _image_batch_lock.locked()
        if waiting_for_batch:
            await _send_notice(bot, group_id, "收到，当前这批图发完后马上撤回…")

        async with _image_batch_lock:
            pending_count = sum(item["group_id"] == group_id for item in _recent_images)
            if not pending_count:
                await _send_notice(bot, group_id, "没有要撤回的图片喵~")
                await setu.finish()

            if not waiting_for_batch:
                await _send_notice(bot, group_id, f"收到，正在撤回 {pending_count} 张图…")
            recalled, failed = await _recall_all(group_id)
            if failed:
                result = f"🏃‍♀️ 已撤回 {recalled} 张，{failed} 张撤回失败。"
            else:
                result = f"🏃‍♀️ 撤回了 {recalled} 张图！溜了溜了"
            await _send_notice(bot, group_id, result)
        await setu.finish()
        return

    count = {
        "来点色图": 1,
        "三连冲": 3,
        "五连冲": 5,
    }.get(text, 0)
    if not count:
        return  # 不匹配，让 ai_chat 处理

    print(f"[setu] command received: group={group_id}, command={text!r}")

    files = _list_setu()
    if not files:
        await setu.finish("图库里还没有图片喵，先把图片放进 data/setu/ 吧。")
        return

    # 选图
    picked = _random_setu(count)

    if not picked:
        await setu.finish("图库空了喵…")
        return

    # 发送图片
    bot = get_bot()
    if count > 1:
        if _image_batch_lock.locked():
            notice = f"收到，前一批还在发送；这批 {len(picked)} 张已排队…"
        else:
            notice = f"收到，开始发送 {len(picked)} 张图…"
        await _send_notice(bot, group_id, notice)

    async with _image_batch_lock:
        sent_count = 0
        for fp in picked:
            try:
                result = await asyncio.wait_for(
                    bot.send_group_msg(
                        group_id=group_id,
                        message=_file_to_segment(fp),
                    ),
                    timeout=_IMAGE_SEND_TIMEOUT_SECONDS,
                )
                # send_group_msg 返回 {"message_id": 12345}，取实际 id
                msg_id = result.get("message_id", 0) if isinstance(result, dict) else result
                _recent_images.append({
                    "group_id": group_id,
                    "message_id": int(msg_id),
                })
                sent_count += 1
            except Exception as e:
                print(f"[setu] send error for {os.path.basename(fp)}: {type(e).__name__}: {e}")

        # 限制撤回列表最多保留 50 条
        if len(_recent_images) > 50:
            _recent_images = _recent_images[-50:]

        if sent_count > 0 and count > 1:
            await _send_notice(
                bot,
                group_id,
                f"已发送 {sent_count} 张 {random.choice(['涩图', '好图', '美图', '图图'])}~",
            )
        elif sent_count == 0:
            await _send_notice(bot, group_id, "图片没能发出去，可能是 QQ 发图接口超时了，稍后再试喵。")
