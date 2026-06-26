"""Group event summary task helpers."""

import asyncio
import json
import os
import time
from datetime import datetime

from openai import OpenAI
from dotenv import load_dotenv

from services.context_compressor import compress, get_group_ids
from plugins.summon import group_state


load_dotenv()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVENTS_FILE = os.path.join(BASE_DIR, "data", "group_events.json")

_client = OpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url=os.getenv("DEEPSEEK_BASE_URL"),
    timeout=10.0,
    max_retries=0,
)

SUMMARY_PROMPT = """You are summarizing recent group history.
Output 3-6 concise bullet points, mentioning key topics and emotional tone."""


def load() -> dict:
    if not os.path.exists(EVENTS_FILE):
        return {"events": []}
    try:
        with open(EVENTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"events": []}


def save(data: dict):
    os.makedirs(os.path.dirname(EVENTS_FILE), exist_ok=True)
    with open(EVENTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_recent(days: int = 3, group_id: str | None = None) -> list[dict]:
    """Return latest event records."""
    data = load()
    events = data.get("events", [])
    if group_id:
        events = [e for e in events if e.get("group_id") == group_id]
    return events[-days:]


def build_context(days: int = 3, group_id: str | None = None) -> str:
    """Build plain text context from recent records."""
    recent = get_recent(days, group_id=group_id)
    if not recent:
        return ""

    lines = ["Recent group events:"]
    for ev in recent:
        date = ev.get("date", "?")
        lines.append(f"  {date}:")
        for item in ev.get("items", []):
            lines.append(f"    - {item}")
    return "\n".join(lines)


async def generate_summary(group_id: str):
    """Generate and persist one-day summary for a group."""
    ctx = compress(group_id, max_items=60)
    if not ctx or len(ctx) < 100:
        return

    try:
        def _request_sync():
            return _client.chat.completions.create(
                model=os.getenv("MODEL", "deepseek-chat"),
                messages=[
                    {"role": "system", "content": SUMMARY_PROMPT},
                    {"role": "user", "content": f"Recent group context:\n{ctx}"},
                ],
                temperature=0.5,
                max_tokens=300,
            )

        response = await asyncio.wait_for(asyncio.to_thread(_request_sync), timeout=8)
    except asyncio.TimeoutError:
        print("[events] generate_summary timeout")
        return
    try:
        text = response.choices[0].message.content or ""
    except Exception as e:
        print(f"[events] AI error: {e}")
        return

    items = []
    for line in text.strip().split("\n"):
        line = line.strip()
        if line.startswith("- "):
            items.append(line[2:])
        elif line:
            items.append(line)

    if not items:
        return

    today = time.strftime("%Y-%m-%d")
    data = load()
    events = data.get("events", [])
    events = [
        e for e in events
        if not (e.get("date") == today and e.get("group_id") == group_id)
    ]
    events.append(
        {
            "date": today,
            "group_id": group_id,
            "items": items,
            "created": time.strftime("%Y-%m-%d %H:%M"),
        }
    )
    data["events"] = events[-30:]
    save(data)
    print(f"[events] saved {len(items)} items for {today} (group {group_id})")


async def event_loop():
    """Run daily summaries at 22:00-23:00 for active groups."""
    await asyncio.sleep(60)
    print("[events] event_loop started")
    done_today = {}

    while True:
        await asyncio.sleep(30 * 60)
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        hour = now.hour

        if 22 <= hour <= 23:
            active_groups = set(group_state.keys()) | set(get_group_ids())
            for group_id in list(active_groups):
                if done_today.get(group_id) == today:
                    continue
                try:
                    await generate_summary(group_id)
                    done_today[group_id] = today
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    print(f"[events] summary error for {group_id}: {e}")
