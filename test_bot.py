"""
本地测试工具 — 模拟群聊对话，无需启动 QQ / NapCat / NoneBot。
直接调用 ask_ai() 测试牛牛喵的反应。
"""
import asyncio
import os
import sys

# 确保工作目录正确（从 nyabot/ 运行时 data/ 和 .env 才能找到）
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from services.data_store import get_users_sync
from services.deepseek_client import ask_ai
from services.mention_resolver import resolve_mentions
from services.context_compressor import record_message
from services.profile_updater import ProfileUpdater


async def main():
    users = get_users_sync()
    if not users:
        print("❌ data/members.json 没有成员数据")
        return

    # ── 成员列表 ──
    member_list = [(uid, p["name"]) for uid, p in users.items()]
    print("=" * 50)
    print("🐱 牛牛喵 本地测试")
    print("=" * 50)
    print("\n可用成员身份:")
    for i, (uid, name) in enumerate(member_list):
        tags = users[uid].get("tags", {})
        behavior = ", ".join(tags.get("behavior", []))
        extra = f"  [{behavior}]" if behavior else ""
        print(f"  [{i}] {name}{extra}")

    # ── 选择身份 ──
    try:
        choice = input("\n选择你的身份 (输入序号，默认 0): ").strip()
        idx = int(choice) if choice else 0
        user_id, user_name = member_list[idx]
    except (ValueError, IndexError):
        print("无效选择，使用默认")
        user_id, user_name = member_list[0]

    print(f"\n你正在以 [{user_name}] 的身份说话")
    print("输入消息查看牛牛喵的回复")
    print("命令: /list (查看成员)  /who (切换身份)  /quit (退出)\n")

    # ── 对话循环 ──
    while True:
        try:
            text = input("你: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n👋 退出测试")
            break

        if not text:
            continue

        # 命令
        if text == "/quit":
            print("👋 退出测试")
            break

        if text == "/list":
            for uid, p in users.items():
                print(f"  {p['name']} (ID:{uid})")
            continue

        if text.startswith("/who"):
            try:
                new_idx = int(text.split()[1])
                user_id, user_name = member_list[new_idx]
                print(f"✅ 切换到 [{user_name}]")
            except (ValueError, IndexError):
                print("❌ 用法: /who <序号>")
            continue

        # ── 记录到上下文历史 ──
        record_message("test_group", user_name, text)

        # ── 提及解析 ──
        mentioned_ids = resolve_mentions(text)
        if mentioned_ids:
            names = [users[uid]["name"] for uid in mentioned_ids if uid in users]
            if names:
                print(f"  🔍 识别到: {', '.join(names)}")

        # ── 调用 AI ──
        print("  ⏳ 思考中...", end="\r")
        try:
            reply = await ask_ai(
                message=text,
                user_id=user_id,
                group_id="test_group",
                mentioned_ids=mentioned_ids,
            )
            print(f"牛牛喵: {reply}\n")
        except Exception as e:
            print(f"❌ AI 调用失败: {e}\n")


if __name__ == "__main__":
    asyncio.run(main())
