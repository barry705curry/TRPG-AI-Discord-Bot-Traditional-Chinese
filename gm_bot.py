import discord
from google import genai
import json
import asyncio
import os
import re
import aiofiles
import uuid
import random
from datetime import datetime
from dotenv import load_dotenv
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# ==========================================
# 1. Token / Key
# ==========================================
load_dotenv()

DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')

# 規則模組化：啟動時一次性載入各分冊，依 AI 呼叫類型組裝 system_instruction，
# 避免每次呼叫都把整本規則塞進 system_prompt（降低雜訊、提升遵規精準度）。
RULES_DIR = 'config/rules'

def _load_rule(name):
    with open(f'{RULES_DIR}/{name}.md', 'r', encoding='utf-8') as f:
        return f.read()

RULES = {name: _load_rule(name) for name in ('common', 'creation', 'gameplay', 'dice')}

def build_system_instruction(call_type, current_status=None):
    """依呼叫類型與當前狀態組裝對應的規則分冊。
    - dice      : 裁判擲骰，只需第三部分。
    - memory    : 記憶萃取，格式已 inline 在 prompt，不需規則（回傳 None）。
    - storyteller: 說書人，依 current_status 給創建或遊玩規則。
    """
    if call_type == 'dice':
        return RULES['dice']
    if call_type == 'memory':
        return None
    if current_status == '副本進行中':
        return "\n\n".join((RULES['common'], RULES['gameplay'], RULES['dice']))
    return "\n\n".join((RULES['common'], RULES['creation']))

ai_client = genai.Client(api_key=GEMINI_API_KEY)
TARGET_MODEL = 'gemini-2.5-flash'

# ==========================================
# 2. 檔案路徑與核心工具
# ==========================================
DATA_FILE = 'data/characters.json'
ENCYCLOPEDIA_FILE = 'data/encyclopedia.json'
GAME_WORLD_FILE = 'data/Game_World.json'
DUNGEON_HISTORY_FILE = 'data/Dungeon_History.json'
MONSTER_FILE = 'data/Monster.json'

BK = "`" * 3
gm_lock = asyncio.Lock()

def safe_get(obj, key, default=None):
    if isinstance(obj, dict): return obj.get(key, default)
    return default

def deep_merge(old, new):
    if not isinstance(new, dict): return old 
    if not isinstance(old, dict): old = {}   
    
    for key, val in new.items():
        if key == "adventure_log": continue 
        if key in old and isinstance(old[key], dict) and isinstance(val, dict):
            deep_merge(old[key], val)
        elif key in old and isinstance(old[key], list) and isinstance(val, list):
            for item in val:
                if item not in old[key]: old[key].append(item)
        else:
            old[key] = val
    return old

# ==========================================
# 🎲 核心升級：Python 獨立裁判系統 (取代 AI 擲骰)
# ==========================================
def execute_dice_rolls(dice_data):
    messages = []
    results_for_ai = []
    
    for act in dice_data.get("actions", []):
        player = act.get("player", "未知")
        action_name = act.get("action", "行動")
        difficulty = act.get("difficulty")
        if difficulty is None:
            raise ValueError("骰子請求缺少難度")
        modifier = act.get("modifier", 0)
        adv = act.get("advantage", False)
        disadv = act.get("disadvantage", False)

        rolls = [random.randint(1, 100)]
        if adv or disadv:
            rolls.append(random.randint(1, 100))

        # 依照規則書特製的優劣勢判斷邏輯
        if disadv:
            # 劣勢：若有大失敗(>=96)取大失敗，否則取較小值。不會大成功。
            if any(r >= 96 for r in rolls):
                raw_roll = max(r for r in rolls if r >= 96)
                final_val = raw_roll 
            else:
                raw_roll = min(rolls)
                final_val = raw_roll + modifier
                if final_val <= 5:
                  final_val = 6
        elif adv:
            # 優勢：若有大成功取大成功，若無取較大值,不會大失敗
            if any(r <= 5 for r in rolls):
                raw_roll = min(r for r in rolls if r <= 5)
                final_val = raw_roll
            else:
                raw_roll = max(rolls)
                final_val = raw_roll + modifier
                if final_val >= 96:
                  final_val = 95
        else:
            raw_roll = rolls[0]
            final_val = raw_roll + modifier


        # 絕對鐵則：1~5大成功、96~100大失敗
        is_crit_fail = (raw_roll >= 96) and not adv
        is_crit_success = (raw_roll <= 5) and not disadv  # 劣勢無法大成功
        is_success = (final_val >= difficulty)

        if is_crit_fail:
            res_text = "💀 **大失敗**"
        elif is_crit_success:
            res_text = "✨ **大成功**"
        elif is_success:
            res_text = "🟢 **成功**"
        else:
            res_text = "🔴 **失敗**"

        if len(rolls) > 1:
            roll_str = (
                f"原始骰值: [{', '.join(map(str, rolls))}] "
                f"+ 調整值: {modifier:+} "
                f"→ 最終結果: **{final_val}**"
            )
        else:
            roll_str = (
                f"原始骰值: {rolls[0]} "
                f"+ 調整值: {modifier:+} "
                f"→ 最終結果: **{final_val}**"
            )
        
        # 組裝發給玩家看的文字
        messages.append(f"👤 **{player}** 執行 **{action_name}**\n🎲 擲骰: {roll_str} (難度: {difficulty}) ➔ {res_text}")
        
        # 組裝給 AI 看的強制結果
        results_for_ai.append(f"玩家 [{player}] 執行 [{action_name}]：骰值 {final_val}，難度 {difficulty} ➔ 結果為【{res_text}】")
        
    return "\n\n".join(messages), "\n".join(results_for_ai)

# ==========================================
# 讀取與存檔功能 
# ==========================================
async def load_characters():
    if os.path.exists(DATA_FILE):
        async with aiofiles.open(DATA_FILE, 'r', encoding='utf-8') as f:
            content = await f.read()
            if content.strip(): 
                try:
                    data = json.loads(content)
                    return data if isinstance(data, dict) else {}
                except: pass
    return {}

async def save_characters(data):
    old = await load_characters()
    merged = deep_merge(old, data)
    async with aiofiles.open(DATA_FILE, 'w', encoding='utf-8') as f:
        await f.write(json.dumps(merged, ensure_ascii=False, indent=2))

async def load_encyclopedia():
    if os.path.exists(ENCYCLOPEDIA_FILE):
        async with aiofiles.open(ENCYCLOPEDIA_FILE, 'r', encoding='utf-8') as f:
            content = await f.read()
            if content.strip():
                try:
                    data = json.loads(content)
                    return data if isinstance(data, dict) else {}
                except: pass
    return {}

async def save_encyclopedia(data):
    old = await load_encyclopedia()
    merged = deep_merge(old, data)
    async with aiofiles.open(ENCYCLOPEDIA_FILE, 'w', encoding='utf-8') as f:
        await f.write(json.dumps(merged, ensure_ascii=False, indent=2))

async def load_game_world():
    data = {}
    if os.path.exists(GAME_WORLD_FILE):
        async with aiofiles.open(GAME_WORLD_FILE, 'r', encoding='utf-8') as f:
            content = await f.read()
            if content.strip(): 
                try:
                    loaded = json.loads(content)
                    if isinstance(loaded, dict): data = loaded
                except: pass

    if "current_status" not in data: data["current_status"] = "尚未創建副本"
    if "player_ready" not in data: data["player_ready"] = False
    
    if "public" not in data or not isinstance(data["public"], dict):
        data["public"] = {
            "current_dungeon_name": "未定", "theme": "未定", "genre": "未定", "mechanic": "未定",
            "victory_condition": "未定", "intro": "未定", "starting_location": "未定",
            "current_location": {}, "adventure_log": []
        }
    if "adventure_log" not in data["public"] or not isinstance(data["public"]["adventure_log"], list):
        data["public"]["adventure_log"] = []

    # current_location：追蹤「每個角色」目前所在位置的字典 {角色名: 地點}，
    # 與永久不變的 starting_location 區隔，並支援隊伍分頭探索（對接戰爭迷霧鐵律）。
    # 僅確保欄位存在；舊版單一字串格式會在 on_message 依當前角色名展開為字典。
    if "current_location" not in data["public"]:
        data["public"]["current_location"] = {}

    if "secret" not in data or not isinstance(data["secret"], dict):
        data["secret"] = {}

    return data

async def save_game_world(data, overwrite=False):
    old = await load_game_world()
    if overwrite:
        final = data if isinstance(data, dict) else await load_game_world()
    else:
        safe_log = old.get("public", {}).get("adventure_log", [])
        final = deep_merge(old, data)
        if "public" not in final: final["public"] = {}
        final["public"]["adventure_log"] = safe_log

        if old.get("current_status") == "副本進行中" and "secret" in old:
            final["secret"] = old["secret"]

    async with aiofiles.open(GAME_WORLD_FILE, 'w', encoding='utf-8') as f:
        await f.write(json.dumps(final, ensure_ascii=False, indent=2))

async def load_monster():
    if os.path.exists(MONSTER_FILE):
        async with aiofiles.open(MONSTER_FILE, 'r', encoding='utf-8') as f:
            content = await f.read()
            if content.strip(): 
                try:
                    data = json.loads(content)
                    return data if isinstance(data, dict) else {}
                except: pass
    return {}

async def save_monster(data):
    if not isinstance(data, dict): data = {}
    async with aiofiles.open(MONSTER_FILE, 'w', encoding='utf-8') as f:
        await f.write(json.dumps(data, ensure_ascii=False, indent=2))

async def load_dungeon_history():
    if os.path.exists(DUNGEON_HISTORY_FILE):
        async with aiofiles.open(DUNGEON_HISTORY_FILE, 'r', encoding='utf-8') as f:
            content = await f.read()
            if content.strip(): 
                try:
                    data = json.loads(content)
                    if isinstance(data, list): return data
                    if isinstance(data, dict): return [data] if data else []
                except: pass
    return [] 

async def save_dungeon_history(data):
    history = await load_dungeon_history()
    if data and isinstance(data, dict): history.append(data)
    async with aiofiles.open(DUNGEON_HISTORY_FILE, 'w', encoding='utf-8') as f:
        await f.write(json.dumps(history, ensure_ascii=False, indent=2))

# ==========================================
# 🌟 高穩 API 呼叫函式
# ==========================================
@retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=2, min=4, max=20), retry=retry_if_exception_type(Exception), reraise=True)
async def safe_generate_content(prompt_text, system_instruction, temperature=0.7):
    config = genai.types.GenerateContentConfig(
        system_instruction=system_instruction,
        temperature=temperature
    )
    response = await ai_client.aio.models.generate_content(
        model=TARGET_MODEL,
        contents=prompt_text,
        config=config
    )
    return response.text

# ==========================================
# 3. Discord 機器人主邏輯
# ==========================================
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    print(f"主神系統 (V9.0 雙模組裁判架構版) 已上線！目前登入身分：{client.user}")
    print(f"目前使用模型：{TARGET_MODEL}")

@client.event
async def on_message(message):
    if message.author == client.user: return
    if client.user not in message.mentions: return

    if "查看角色" in message.content:
        chars = await load_characters()
        await message.channel.send(f"📊 **[系統] 目前角色狀態：**\n```json\n{json.dumps(chars, ensure_ascii=False, indent=2)}\n```")
        return
    if "查看世界" in message.content or "查看狀態" in message.content:
        world = await load_game_world()
        await message.channel.send(f"🌍 **[系統] 目前公開世界情報與長期記憶：**\n```json\n{json.dumps(world.get('public', {}), ensure_ascii=False, indent=2)}\n```")
        return

    async with gm_lock:
        async with message.channel.typing():
            try:
                # 系統提示訊息（裁判運算中、擲骰廣播、存檔/記憶提示等）的開頭符號，
                # 這些不是劇情，納入上下文只會干擾 AI，需略過。
                SYSTEM_PREFIXES = ("⚖️", "🎲", "💾", "🧠", "📊", "🌍", "⚙️", "🛑", "⚠️")
                recent_msgs = []
                previous_scene = ""  # 上一則 GM 敘事，獨立餵給 AI 作場景銜接，不混入玩家發言
                async for msg in message.channel.history(limit=50):
                    if msg.id == message.id:
                        recent_msgs.append(msg)
                        continue
                    is_bot = (msg.author == client.user)
                    # 跳過機器人的系統提示訊息（裁判運算中、擲骰廣播、存檔/記憶提示等）。
                    if is_bot and msg.content.strip().startswith(SYSTEM_PREFIXES):
                        continue
                    if is_bot:
                        # 抓到「上一則 GM 敘事」即停止：另存到 previous_scene 作場景銜接，
                        # 不放進玩家發言串，避免 AI 把自己的敘事誤認成玩家輸入而混淆。
                        previous_scene = msg.content.replace(f"<@{client.user.id}>", "").strip()
                        break
                    recent_msgs.append(msg)
                recent_msgs.reverse()

                # 上一幕敘事可能很長，裁到尾段（通常是收尾＋選項）以控制 token 成本。
                if len(previous_scene) > 1500:
                    previous_scene = "...(前略)...\n" + previous_scene[-1500:]

                compiled_text = ""
                for msg in recent_msgs:
                    clean_content = msg.content.replace(f"<@{client.user.id}>", "").strip()
                    if clean_content: compiled_text += f"{msg.author.display_name}: {clean_content}\n"
                
                if not compiled_text.strip(): return
                if len(compiled_text) > 12000:
                    compiled_text = "...(系統提示：過往對話已省略)...\n" + compiled_text[-12000:]

                # 攔截指令區塊
                if "開始跑團" in message.content:
                    current_world = await load_game_world()
                    current_world["current_status"] = "等待玩家資料"
                    current_world["player_ready"] = False 
                    await save_game_world(current_world, overwrite=True)
                    await message.channel.send("⚙️ **[系統公告] 主神空間已開啟。**\n請各位玩家自由討論並提供設定。準備完畢請輸入 `@主神 準備完畢` 以生成副本。")
                    return

                if "準備完畢" in message.content:
                    current_world = await load_game_world()
                    if current_world["current_status"] == "等待玩家資料":
                        current_world["current_status"] = "副本生成中"
                        current_world["player_ready"] = True
                        await save_game_world(current_world, overwrite=False)
                        compiled_text += "\n\n[系統事件：玩家已確認角色鎖定。後續訊息不得修改已提交資料。若提出新想法，請視為副本開始後的行動。]"

                if "跑團結束" in message.content:
                    current_world = await load_game_world()
                    current_public = current_world["public"]
                    dungeon_name = current_public.get("current_dungeon_name")
                    if dungeon_name and dungeon_name != "未定":
                        history_entry = {
                            "id": str(uuid.uuid4()), "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "dungeon_name": current_public.get("current_dungeon_name", "未定"),
                            "theme": current_public.get("theme", "未定"), "genre": current_public.get("genre", "未定"),
                            "mechanic": current_public.get("mechanic", "無"), "victory_condition": current_public.get("victory_condition", "未定"),
                            "status": "已通關/結束"
                        }
                        await save_dungeon_history(history_entry)

                    reset_data = {
                        "current_status": "尚未創建副本", "player_ready": False,
                        "public": {
                            "current_dungeon_name": "未定", "theme": "未定", "genre": "未定",
                            "mechanic": "未定", "victory_condition": "未定", "intro": "未定",
                            "starting_location": "未定", "current_location": {}, "adventure_log": []
                        },
                        "secret": {}
                    }
                    await save_game_world(reset_data, overwrite=True)
                    async with aiofiles.open(MONSTER_FILE, 'w', encoding='utf-8') as f:
                        await f.write(json.dumps({}, ensure_ascii=False, indent=2))
                    await message.channel.send("🛑 **[系統公告] 輪迴通道已關閉，本次跑團正式結束。**")
                    return

                # 讀取世界與後台資料
                current_chars = await load_characters()
                char_status_str = json.dumps(current_chars, ensure_ascii=False, indent=2)
                current_encyclopedia = await load_encyclopedia()
                encyclopedia_str = json.dumps(current_encyclopedia, ensure_ascii=False, indent=2)

                current_world = await load_game_world()
                current_status = current_world["current_status"]
                
                # 黑名單與狀態整理
                current_history = await load_dungeon_history()
                flat_history = []
                for entry in current_history:
                    if isinstance(entry, dict):
                        dungeons_list = entry.get("dungeons")
                        if isinstance(dungeons_list, list): flat_history.extend(dungeons_list)
                        else: flat_history.append(entry)

                used_themes, used_genres, used_mechanics = set(), set(), set()
                for entry in flat_history:
                    if not isinstance(entry, dict): continue
                    if entry.get("theme") and entry.get("theme") != "未定": used_themes.add(entry.get("theme"))
                    if entry.get("genre") and entry.get("genre") != "未定": used_genres.add(entry.get("genre"))
                    if entry.get("mechanic") and entry.get("mechanic") not in ["無", "未定"]: used_mechanics.add(entry.get("mechanic"))
                
                blacklist_str = f"已使用主題: {', '.join(used_themes) or '無'} | 已使用類型: {', '.join(used_genres) or '無'} | 已使用機制: {', '.join(used_mechanics) or '無'}"

                pub_data = current_world["public"]
                sec_data = current_world["secret"]

                # 正規化 current_location 為 {角色: 地點} 字典，相容舊版單一字串格式：
                # 舊字串代表「全隊在同一處」，依當前角色名展開為個別位置。
                start_loc = pub_data.get("starting_location", "未定")
                loc_map = pub_data.get("current_location")
                if not isinstance(loc_map, dict):
                    legacy = loc_map if isinstance(loc_map, str) and loc_map not in ("", "未定") else start_loc
                    loc_map = {name: legacy for name in current_chars}
                    pub_data["current_location"] = loc_map

                # 逐角色列出位置；沒有專屬紀錄的角色，視為仍在起始地點。
                loc_lines = ""
                for name in current_chars:
                    loc_lines += f"　- {name}：{loc_map.get(name) or start_loc}\n"
                for name, loc in loc_map.items():  # 涵蓋角色卡以外（如 NPC/離隊）的位置紀錄
                    if name not in current_chars:
                        loc_lines += f"　- {name}：{loc}\n"
                if not loc_lines:
                    loc_lines = f"　- （全體）：{start_loc}\n"

                world_section = f"""
===========
PUBLIC_WORLD [系統資訊：當前世界觀與「長期記憶日誌 (adventure_log)」]
===========
📍【各角色目前所在位置 / current_location】
{loc_lines}（⚠️ 位置鐵則：每位角色描述「你現在位在……」時，必須各自對應上方該角色的位置。
　starting_location 只是副本最初的出生點，角色早已可能離開並深入其他區域，絕對禁止無故把角色寫回起始點！
　戰爭迷霧：分頭行動的角色彼此看不到對方所在，嚴禁讓 A 知道只有 B 在場才看得到的事物。
　請對照下方 adventure_log 確認各角色的移動軌跡。）

{BK}json
{json.dumps(pub_data, ensure_ascii=False, indent=2)}
{BK}
"""
                if current_status in ["尚未創建副本", "等待玩家資料", "副本生成中", "副本進行中"]:
                    world_section += f"""
===========
SECRET_WORLD [系統隱藏資訊]
(⚠️ 系統最高級別警告：此為 GM 專屬劇本真相。絕對禁止直接向玩家暴雷！僅供「上帝視角」底層邏輯參考。)
===========
{BK}json
{json.dumps(sec_data, ensure_ascii=False, indent=2)}
{BK}
===========
MONSTER_DATABASE [怪物圖鑑]
===========
{BK}json
{json.dumps(await load_monster(), ensure_ascii=False, indent=2)}
{BK}
"""

                # ==========================================
                # 🎲 階段一：判斷器 (Dice Check & Generation)
                # ==========================================
                dungeon_creation_prompt = ""
                dice_result_for_ai = "無須擲骰，請直接順暢地描述劇情發展。"

                if current_status == "副本進行中":
                    # 提示玩家系統正在判斷
                    status_msg = await message.channel.send("⚖️ **[系統] 裁判引擎運算中...**")
                    
                    dice_check_prompt = f"""
                    【系統任務：行動解析】
                    請作為冷酷的系統裁判，判斷玩家剛才的行動是否包含「風險、戰鬥、解密、潛行、搜索、交涉」等需要判定成功率的動作？
                    若需要擲骰，請嚴格輸出以下格式的 JSON (不要輸出任何其他對話)：
                    {BK}json dice_request
                    {{
                      "need_roll": true,
                      "actions": [
                        {{
                          "player": "玩家名稱",
                          "action": "搜索房間",
                          "difficulty": 60,
                          "modifier": 0,
                          "advantage": false,
                          "disadvantage": false
                        }}
                      ]
                    }}
                    {BK}
                    若完全是閒聊或普通移動，無須判定，請輸出 {{"need_roll": false}}。
                    
                    [近期對話]
                    {compiled_text}
                    """
                    dice_req_text = await safe_generate_content(dice_check_prompt, build_system_instruction('dice'), temperature=0.1)
                    
                    # 刪除「運算中」的提示
                    try: await status_msg.delete() 
                    except: pass
                    
                    pattern_dice = rf"{BK}json[\s\n]*dice_request[\s\n]*(.*?)\n{BK}"
                    match_dice = re.search(pattern_dice, dice_req_text, re.DOTALL | re.IGNORECASE)
                    
                    if match_dice:
                        try:
                            dice_data = json.loads(match_dice.group(1).strip())
                            if dice_data.get("need_roll"):
                                # 呼叫 Python 裁判引擎擲骰
                                broadcast_msg, ai_report = execute_dice_rolls(dice_data)
                                
                                # 立即向 Discord 發布擲骰結果
                                await message.channel.send(f"🎲 **[命運之輪轉動]**\n{broadcast_msg}")
                                
                                # 將絕對不可篡改的結果交給 AI
                                dice_result_for_ai = f"【裁判系統擲骰結果】(絕對鐵則：你必須完全依照此結果敘事，禁止擅自修改成功/失敗或大成功/大失敗的結論！)\n{ai_report}"
                        except Exception as e:
                            print(f"擲骰解析失敗: {e}")

                    # 組合第二階段敘事 Prompt
                    dungeon_creation_prompt = f"""
                    【GM 任務：推進劇情】
                    {dice_result_for_ai}
                    請根據玩家行動與上方【裁判系統擲骰結果】推進劇情，給出沉浸感的敘事回覆。
                    📍【位置追蹤｜每回合必做】：本回合敘事結束後，請務必在回覆最後輸出以下精簡標籤，
                    　以 {{角色名: 地點}} 逐一填入每位角色「這一幕結束時」的所在位置（移動了就更新，沒移動就照填目前位置）。
                    　隊伍在一起時就把各角色填成同一地點；分頭行動時各自填各自的位置。
                    　這「不是」重新生成副本，僅更新位置，副本核心設定與 secret 一律不得改動：
                    {BK}json game_world
                    {{ "public": {{ "current_location": {{ "角色名": "該角色此刻明確的所在地點（例：神社正殿 銅鏡神龕前）" }} }} }}
                    {BK}
                    ⚠️ 【系統指令】：若本次劇情發生了「戰鬥、線索、重要抉擇」，請務必在回覆最後加上以下 JSON 標籤來喚醒記憶子系統：
                    {BK}json memory_flag
                    {{ "trigger": true }}
                    {BK}
                    """
                    
                elif current_status == "等待玩家資料":
                    dungeon_creation_prompt = "⚠️ 系統目前為「等待玩家資料」階段。請協助玩家創角，絕對禁止生成副本！"
                elif current_status == "副本生成中":
                    dungeon_creation_prompt = f"""
                    【GM 任務：創建新副本】
                    第一段：輸出副本資訊卡 (包含副本名稱、難度、主題Theme、類型Genre、機制Mechanic、勝利條件、背景介紹)
                    第二段：沉浸式開場敘事。包含具體環境細節、五感異常描寫及「第一個選擇」。
                    務必更新 JSON：1. current_status 轉為「副本進行中」 2. 更新標籤至 public 3. 更新【隱藏真相】到 secret。
                    ⚠️ 絕對禁止生成與以下標籤相似的副本：{blacklist_str}
                    """

                # 上一幕 GM 敘事：獨立區塊，明確標示「這是你自己的上一幕、非玩家發言」，
                # 讓 AI 能銜接「那扇門/那個選項/剛剛那句話」等指涉，又不會誤把它當成要回應的玩家輸入。
                if previous_scene:
                    previous_scene_section = f"""
                ===========
                【上一幕場景（你自己上次的敘事，僅供銜接：請延續此場景與位置，勿重複照抄、勿當成玩家發言）】
                ===========
                {previous_scene}
"""
                else:
                    previous_scene_section = ""

                prompt = f"""
                【GM後台資料】
                {world_section}
                {dungeon_creation_prompt}

                CHARACTERS [玩家狀態]
                {BK}json
                {char_status_str}
                {BK}

                ENCYCLOPEDIA [字典]
                {BK}json
                {encyclopedia_str}
                {BK}
                {previous_scene_section}
                ===========
                玩家本回合發言
                ===========
                {compiled_text}
                """
                # ==========================================
                # 📜 階段二：說書人 (GM Storyteller)
                # ==========================================
                try:
                    reply_text = await safe_generate_content(prompt, build_system_instruction('storyteller', current_status), temperature=0.7)

                    trigger_memory = False
                    pattern_mem = rf"{BK}json\s+memory_flag\s*(.*?){BK}"
                    match_mem = re.search(pattern_mem, reply_text, re.DOTALL | re.IGNORECASE)
                    if match_mem:
                        try:
                            mem_data = json.loads(match_mem.group(1).strip())
                            if mem_data.get("trigger"): trigger_memory = True
                            reply_text = re.sub(pattern_mem, "", reply_text, flags=re.DOTALL | re.IGNORECASE).strip()
                        except: pass

                    SAVE_TARGETS = {
                        "encyclopedia": save_encyclopedia,
                        "game_world": save_game_world, 
                        "monster": save_monster
                    }
                    if current_status in ["尚未創建副本", "等待玩家資料", "副本生成中"]:
                        SAVE_TARGETS["characters"] = save_characters

                    updated_files = []
                    for tag, save_func in SAVE_TARGETS.items():
                        pattern = rf"{BK}json\s+{tag}\s*(.*?){BK}"
                        match = re.search(pattern, reply_text, re.DOTALL | re.IGNORECASE)

                        if match:
                            try:
                                new_data = json.loads(match.group(1).strip())
                                await save_func(new_data)
                                updated_files.append(tag)
                                reply_text = re.sub(pattern, "", reply_text, flags=re.DOTALL | re.IGNORECASE).strip()
                            except json.JSONDecodeError:
                                await message.channel.send(f"⚠️ **[系統警告] {tag} 格式解析失敗。**")

                    if updated_files:
                        await message.channel.send(f"💾 **[系統提示] 後台資料 ({', '.join(updated_files)}) 已同步！**")

                    if len(reply_text) > 2000:
                        for i in range(0, len(reply_text), 2000):
                            await message.channel.send(reply_text[i:i + 2000])
                    elif reply_text:
                        await message.channel.send(reply_text)

                except Exception as e:
                    if current_world["current_status"] == "副本生成中":
                        current_world["current_status"] = "等待玩家資料"
                        current_world["player_ready"] = False
                        await save_game_world(current_world, overwrite=True)
                        await message.channel.send(f"⚠️ **[系統錯誤] 副本生成失敗，已退回「等待玩家資料」。請稍後重試。**\n開發者除錯訊息：{e}")
                    else:
                        await message.channel.send(f"主神系統發生異常：{e}")
                
                # ==========================================
                # 🧠 階段三：記憶萃取器 (Memory Extractor)
                # ==========================================
                if current_world["current_status"] == "副本進行中" and reply_text and trigger_memory:
                    try:
                        memory_prompt = f"""
                        【系統任務：記憶萃取】
                        請判斷剛才的互動發生了什麼「推動劇情的重要事件」？
                        請嚴格輸出以下 JSON (包含 event, type, importance)：
                        {BK}json game_world
                        {{
                          "public": {{
                            "adventure_log": [
                              {{ "event": "玩家推開地下室的門發現符號。", "type": "clue", "importance": 3 }}
                            ]
                          }}
                        }}
                        {BK}
                        [對話紀錄]
                        玩家：{compiled_text}
                        GM：{reply_text}
                        """
                        mem_response_text = await safe_generate_content(memory_prompt, build_system_instruction('memory'), temperature=0.2)
                        
                        pattern = rf"{BK}json[\s\n]*game_world[\s\n]*(.*?)\n{BK}"
                        match = re.search(pattern, mem_response_text, re.DOTALL | re.IGNORECASE)

                        if match:
                            new_data = json.loads(match.group(1).strip())
                            if "public" in new_data and "adventure_log" in new_data["public"]:
                                logs = new_data["public"]["adventure_log"]
                                if isinstance(logs, list) and logs:
                                    latest_world = await load_game_world()
                                    log_events = []
                                    for log in logs:
                                        if isinstance(log, dict) and log.get('event'):
                                            log["event_id"] = f"evt_{uuid.uuid4().hex[:8]}"
                                            latest_world["public"]["adventure_log"].append(log)
                                            log_events.append(log['event'])
                                            
                                    if log_events:
                                        await save_game_world(latest_world, overwrite=True)
                                        await message.channel.send(f"🧠 **[系統記憶萃取]** 已登錄關鍵事件：\n- " + "\n- ".join(log_events))
                    except Exception as e:
                        print(f"記憶萃取子系統錯誤: {e}")

            except Exception as e:
                await message.channel.send(f"主神系統外層發生異常：{e}")

client.run(DISCORD_TOKEN)