"""
AI 閼奉亜濮╅崡鎷岃杽閺囧瓨鏌?閳?鐎规碍婀￠崚鍡樼€界紘銈堜喊閸愬懎顔愰敍灞炬纯閺傜増鍨氶崨?meta.impression閵?
娴ｆ粈璐熼崥搴″酱娴犺濮熸潻鎰攽閿涘奔绗夐梼璇差敚濞戝牊浼呮径鍕倞閵?
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
    timeout=10.0,
    max_retries=0,
)

IMPRESSION_PROMPT = """\
娴ｇ姵妲哥紘銈堜喊閸掑棙鐎介崝鈺傚閵嗗倸鐔€娴滃簼浜掓稉瀣付鏉╂垼浜版径鈺勵唶瑜版洩绱濋崚妤€鍤崗鏈佃厬閹绘劕鍩岄惃鍕槨娑擃亙姹夐敍宀€鏁ゆ稉鈧崣銉よ厬閺傚洦顩ч幏顑跨稑娴滃棜袙閸掓壆娈戦弬棰佷繆閹垬鈧?

閺嶇厧绱＄憰浣圭湴閿涘牅寮楅弽濂镐紥鐎瑰牞绱氶敍?
- 濮ｅ繗顢戞稉鈧稉顏冩眽閿涙艾鎮曠€涙⒍娑撯偓閸欍儴鐦介崡鎷岃杽
- 閸欘亜鍟撻懕濠傘亯鐠佹澘缍嶆稉顓炵杽闂勫懎鍤悳鎵畱娴?
- 閸楁媽钖勯崺杞扮艾閼卞﹤銇夐崘鍛啇閹恒劍鏌囬敍灞肩瑝鐟曚胶绱柅?
- 婵″倹鐏夐弻鎰嚋娴滅儤鐥呴張澶嬫煀娣団剝浼呴敍灞肩瑝鐟曚礁鍟搕a
- 娑撳秷顩﹂崘娆戝閻楁稑鏌曢懛顏勭箒

缁€杞扮伐鏉堟挸鍤敍?
閸欏爼鍙閺堚偓鏉╂垵婀悳銆S閿涘瞼绮＄敮姝岀箾鐠?
鐢箑鐢崗鏀熼幎钘夊煂娴滃摖SR瀵板牆绱戣箛?
閽傛瑧澧皘閺堚偓鏉╂垵浼愭担婊冪发韫囨瑧绮＄敮绋垮閻?
"""


async def update_impressions(group_id: str):
    """
    娴犲骸甯囩紓鈺佹倵閻ㄥ嫮鍏㈤懕濠佺瑐娑撳鏋冩稉顓熷絹閸欐牗鍨氶崨妯哄祪鐠炩€宠嫙娣囨繂鐡ㄩ妴?
    婢惰精瑙﹂棃娆撶帛閿涘奔绗夎ぐ鍗炴惙娑撶粯绁︾粙瀣ㄢ偓?
    """
    ctx = compress(group_id, max_items=30)
    if not ctx or len(ctx) < 50:
        return  # 娑撳﹣绗呴弬鍥с亰閻叏绱濇稉宥呭瀻閺?

    try:
        def _request_sync():
            return _client.chat.completions.create(
                model=os.getenv("MODEL", "deepseek-chat"),
                messages=[
                    {"role": "system", "content": IMPRESSION_PROMPT},
                    {"role": "user", "content": f"鑱婂ぉ璁板綍锛歕n{ctx}"},
                ],
                temperature=0.5,
                max_tokens=300,
            )

        response = await asyncio.wait_for(asyncio.to_thread(_request_sync), timeout=8)
    except asyncio.TimeoutError:
        print(f"[impression] AI timeout for group={group_id}")
        return
    except Exception as e:
        print(f"[impression] AI error: {e}")
        return
    try:
        text = response.choices[0].message.content or ""
    except Exception as e:
        print(f"[impression] AI parse error: {e}")
        return

    # 鐟欙絾鐎介敍姘槨鐞?"閸氬秴鐡閸楁媽钖?
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

        # 閸栧綊鍘ら悽銊﹀煕閿涘牊瀵滈崥宥呯摟閹存牕鍩嗛崥宥忕礆
        for uid, profile in users.items():
            pname = profile.get("name", "")
            aliases = profile.get("aliases", [])
            if name_part == pname or name_part in aliases:
                old_imp = profile.get("meta", {}).get("impression", "")
                # 閺傜増妫崥鍫濊嫙閿涘苯褰囬張鈧弬鎵畱閸︺劌澧犻敍灞锯偓璇插彙娑撳秷绉存潻?120 鐎?
                merged = f"{old_imp} {impression}" if old_imp else impression
                if len(merged) > 120:
                    merged = merged[:120]

                await update_user(uid, {"meta": {"impression": merged}})
                updated += 1

                # 婵″倹鐏夐崡鎷岃杽鐡掑啿顧勯梹鍖＄礄閳?5 鐎涙绱氶敍灞肩稊娑撴椽鏆遍張鐔活唶韫囧棗鐡ㄩ崒?
                if len(impression) >= 15:
                    from services.memory import add as add_memory
                    add_memory(impression, related_users=[uid], group_id=group_id)
                break

    if updated:
        print(f"[impression] updated {updated} members")


async def impression_loop():
    """按群聊进行印象画像更新（15 分钟执行一次）"""
    await asyncio.sleep(30)  # 閸氼垰濮╅崥搴ｇ搼 30s
    while True:
        await asyncio.sleep(15 * 60)  # 濮?15 閸掑棝鎸?
        groups = list(group_state.keys())
        for group_id in groups:
            try:
                await update_impressions(group_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[impression] loop error ({group_id}): {e}")

