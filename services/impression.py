"""
AI 自动印象更新 — 定期分析群聊内容，更新成员 meta.impression。
作为后台任务运行，不阻塞消息处理。
"""
import asyncio
import os
from openai import OpenAI
from dotenv import load_dotenv

from services.data_store import get_users_sync, update_user
from services.context_compressor import compress
from plugins.summon import group_state

load_dotenv()

_client = OpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url=os.getenv("DEEPSEEK_BASE_URL"),
)

IMPRESSION_PROMPT = """\
你是群聊分析助手。基于以下最近聊天记录，列出其中提到的每个人，用一句中文概括你了解到的新信息。

格式要求（严格遵守）：
- 每行一个人：名字|一句话印象
- 只写聊天记录中实际出现的人
- 印象基于聊天内容推断，不要编造
- 如果某个人没有新信息，不要写ta
- 不要写牛牛喵自己

示例输出：
叶酱|最近在玩FPS，经常连跪
带带兔|抽到了SSR很开心
蒙牛|最近工作很忙经常加班
"""


async def update_impressions(group_id: str):
    """
    从压缩后的群聊上下文中提取成员印象并保存。
    失败静默，不影响主流程。
    """
    ctx = compress(group_id, max_items=30)
    if not ctx or len(ctx) < 50:
        return  # 上下文太短，不分析

    try:
        response = _client.chat.completions.create(
            model=os.getenv("MODEL", "deepseek-chat"),
            messages=[
                {"role": "system", "content": IMPRESSION_PROMPT},
                {"role": "user", "content": f"聊天记录：\n{ctx}"},
            ],
            temperature=0.5,
            max_tokens=300,
        )
        text = response.choices[0].message.content or ""
    except Exception as e:
        print(f"[impression] AI error: {e}")
        return

    # 解析：每行 "名字|印象"
    users = get_users_sync()
    updated = 0

    for line in text.strip().split("\n"):
        line = line.strip()
        if "|" not in line:
            continue

        name_part, impression = line.split("|", 1)
        name_part = name_part.strip()
        impression = impression.strip()

        if not impression:
            continue

        # 匹配用户（按名字或别名）
        for uid, profile in users.items():
            pname = profile.get("name", "")
            aliases = profile.get("aliases", [])
            if name_part == pname or name_part in aliases:
                old_imp = profile.get("meta", {}).get("impression", "")
                # 新旧合并，取最新的在前，总共不超过 120 字
                merged = f"{impression}；{old_imp}" if old_imp else impression
                if len(merged) > 120:
                    merged = merged[:120]

                await update_user(uid, {"meta": {"impression": merged}})
                updated += 1

                # 如果印象足够长（≥15 字），作为长期记忆存储
                if len(impression) >= 15:
                    from services.memory import add as add_memory
                    add_memory(impression, related_users=[uid], group_id=group_id)
                break

    if updated:
        print(f"[impression] updated {updated} members")


async def impression_loop():
    """后台定期更新印象（每 15 分钟）"""
    await asyncio.sleep(30)  # 启动后等 30s
    while True:
        await asyncio.sleep(15 * 60)  # 每 15 分钟
        groups = list(group_state.keys())
        for group_id in groups:
            try:
                await update_impressions(group_id)
            except Exception as e:
                print(f"[impression] loop error ({group_id}): {e}")
