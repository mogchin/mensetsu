from __future__ import annotations
import os
from dotenv import load_dotenv
from google import genai
from google.genai.types import GenerateContentConfig, ThinkingConfig

# .env を読み込んで環境変数を設定
load_dotenv()

# Gemini 2.5 Flash 用クライアント初期化
# .env に GENAI_API_KEY=your-gemini-api-key を設定してください
genai_client = genai.Client(api_key=os.getenv("GENAI_API_KEY"))

# 以下、Discord Bot 用ライブラリ
import discord
from discord import app_commands, AuditLogAction
from discord.ext import commands, tasks
import json
import re
import asyncio
import logging
from datetime import datetime, timedelta, timezone, time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Iterable
import uuid
from collections import defaultdict
import aiofiles
import contextlib


# どこかグローバルで一度だけ
SCHEDULE_CACHE_MIN = 60 * 5          # 5 分
_schedule_cache = {"ts": 0.0, "text": ""}


# ------------------------------------------------
# TOKEN設定
# ------------------------------------------------
BOT_TOKEN: Optional[str] = os.getenv("DISCORD_BOT_TOKEN", "YOUR_TOKEN_HERE")

# ------------------------------------------------
# グローバル変数定義
# ------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, 'monthly_counts_data.json')
DATA_FILE_PATH = os.path.join(BASE_DIR, 'interview_records.json')
BAN_DATA_FILE = os.path.join(BASE_DIR, 'ban_data.json')
LOG_CHANNEL_ID: int = 1306053871855996979
# ★ 追記: 自動キックした際のログ出力先チャンネル
AUTO_KICK_LOG_CHANNEL_ID: int = 1361465393587163166

MONTHLY_GOAL: int = 10
transient_memo_cache: Dict[str, str] = {}

# ------------------------------------------------
# 設定 (config)
# ------------------------------------------------
# 【コード②：面接／進捗管理用】
MAIN_GUILD_ID: int = 784723518402592799
EXEMPT_ROLE_ID  = 784723518402592803
MAIN_CATEGORY_ID: int = 1305735985539055667
DASHBOARD_CHANNEL_ID: int = 1305732338499457075
INTERVIEWER_ROLE_ID: int = 892528673620623411  # 面接担当者ロールID
PROFILE_FORM_CHANNEL_ID: int = 1305911809576013926
SPECIFIC_ROLE_ID: int = 784723518402592803
PASS_ROLE_ID: int = 1304670207238996052
OTHER_SERVER_PASS_ROLE_NAME: str = "合格者"  # サブサーバー用の合格ロール名
INTERVIEW_MEMO_CHANNEL_ID: int = 1029739734945235014  # 面接メモ用（合否ボタン付）
PASS_MEMO_CHANNEL_ID: int = 1305384589111595088
ADDITIONAL_MEMO_CHANNEL_ID: int = 872102367636643870  # 追加メモ用（ボタンなし）
INTERVIEWER_REMIND_CHANNEL_ID: int = 1306090666891411476
INTERVIEWER_STATS_CHANNEL_ID: int = 1306053871855996979  # 統計表示先（担当者別）
MONTHLY_STATS_CHANNEL_ID: int = 1313069444272099449  # 統計表示先（月ごと合否）
ADMIN_ROLE_ID = 991112832655560825  # 管理者ロールID（手動追加用）
SCHEDULE_MESSAGE_ID: int = 1377625660897624205        # 面接官の予定が書かれているメッセージ ID
MANAGER_USER_ID:    int = 360280438654238720          # 推薦結果を DM する相手
# 推薦DM通知で除外するユーザーID一覧
RECOMMEND_DM_EXCLUDE_IDS: set[int] = {
    294729244712632322,
    278138935023239168,
    426215439761276939,
}
# 「候補者」と見なすロール
CANDIDATE_ROLE_IDS: set[int] = {
    784723518402592803,     # SPECIFIC_ROLE_ID
    1289488152301539339,    # もう一つの候補者ロール
}

# JSTタイムゾーン
JST: timezone = timezone(timedelta(hours=9))

# ------------------------------------------------
# ログ設定
# ------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s:%(name)s: %(message)s',
    handlers=[
        logging.FileHandler("bot.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger: logging.Logger = logging.getLogger(__name__)
# ─────────────────────────────────────────────────────────
# 月次進捗バー表示 ― 既存メッセージを探して更新（先月分も保持）
# ─────────────────────────────────────────────────────────
# 既存関数を丸ごと置換
async def update_stats(
    bot: commands.Bot,
    target_months: Optional[Iterable[str]] | None = None,   # ←★追加
) -> None:
    """
    target_months を与えると **その月だけ** 更新。
    省略時は従来どおり全月更新。
    """
    now = datetime.now(JST)
    current_ym = f"{now.year}-{now.month:02d}"

    guild_main: Optional[discord.Guild] = bot.get_guild(MAIN_GUILD_ID)
    if guild_main is None:
        logger.warning("update_stats: MAIN_GUILD_ID のギルドが取得できませんでした。")
        return

    channel: Optional[discord.TextChannel] = bot.get_channel(INTERVIEWER_STATS_CHANNEL_ID)
    if channel is None:
        logger.warning("update_stats: 統計出力チャンネルが見つかりませんでした。")
        return

    # ---------- 1) 更新対象の年月セット ----------
    if target_months:
        ym_set = set(target_months)
    else:
        ym_set = {current_ym}
        for rec in data_manager.interview_records:
            try:
                dt = datetime.fromisoformat(rec.get("date"))
                ym_set.add(f"{dt.year}-{dt.month:02d}")
            except Exception:
                continue

    # ---------- 2) 月ごとに Embed を生成 ----------
    saved_current_msg_id: Optional[int] = None

    for year_month in sorted(ym_set):
        year_i, month_i = map(int, year_month.split("-"))

        # ---- 2-1. 回数集計 -------------------------
        exec_counts: defaultdict[int, int] = defaultdict(int)
        for rec in data_manager.interview_records:
            try:
                dt = datetime.fromisoformat(rec.get("date"))
            except Exception:
                continue
            if dt.year == year_i and dt.month == month_i:
                exec_counts[int(rec.get("interviewer_id"))] += 1

        # 面接担当ロール保持者は 0 回でも載せる
        interviewer_role: Optional[discord.Role] = guild_main.get_role(INTERVIEWER_ROLE_ID)
        if interviewer_role:
            for m in interviewer_role.members:
                exec_counts.setdefault(m.id, 0)

        # ---- 2-2. 並べ替え -------------------------
        def sort_key(item: tuple[int, int]) -> tuple[int, str]:
            uid, cnt = item
            member = guild_main.get_member(uid)
            name = member.display_name if member else f"ID:{uid}"
            return (-cnt, name)

        sorted_exec = sorted(exec_counts.items(), key=sort_key)

        # ---- 2-3. Embed 作成 -----------------------
        embed = discord.Embed(
            title       = f"面接担当者の面接回数 {year_month}",
            description = "各担当者の面接回数と進捗\n目標 **10 回**",
            color       = 0xf1c40f,
            timestamp   = datetime.utcnow()
        )
        for uid, cnt in sorted_exec or [(0, 0)]:
            member = guild_main.get_member(uid)
            name   = member.display_name if member else f"ID:{uid}"
            bar    = generate_custom_progress_bar(cnt, MONTHLY_GOAL)
            embed.add_field(name=name, value=bar, inline=False)

        # ---- 2-4. メッセージ更新／送信 -------------
        msg_id = data_manager.interviewer_stats_message_ids.get(year_month)
        target: Optional[discord.Message] = None
        if msg_id:
            try:
                target = await channel.fetch_message(msg_id)
            except discord.NotFound:
                target = None

        if target is None:
            async for hist in channel.history(limit=50):
                if hist.author.id == bot.user.id and hist.embeds:
                    if (hist.embeds[0].title or "").startswith(
                        f"面接担当者の面接回数 {year_month}"
                    ):
                        target = hist
                        break

        if target:
            await target.edit(embed=embed)
            saved_id = target.id
        else:
            sent = await channel.send(embed=embed)
            saved_id = sent.id

        data_manager.interviewer_stats_message_ids[year_month] = saved_id

        if year_month == current_ym:
            saved_current_msg_id = saved_id

        # ---- ★ポイント★: ここでちょっと待つ ----
        # 1.1 秒待てば 5 回 / 5 秒のレートを確実に回避
        await asyncio.sleep(1.1)

    if saved_current_msg_id:
        data_manager.interviewer_stats_message_ids["current"] = saved_current_msg_id

    # ---------- 3) 永続化 & 月内訳 ---------------
    await data_manager.save_data()
    logger.info("update_stats: Embed 更新完了")
    # 当月が更新対象に含まれる場合だけ月次内訳も書き換える
    if current_ym in ym_set:
        await update_monthly_stats(bot)


async def update_monthly_stats(bot: commands.Bot) -> None:
    """
    当月の『面接結果内訳』を

        合格者数:
        不合格者数:
        合格率:

    だけでまとめて、既存チャンネル 1313069444272099449 の
    既存 Embed を上書き更新する。
    """
    # ---------------- 対象年月 ----------------
    now        = datetime.now(JST)
    year_month = f"{now.year}-{now.month:02d}"

    # ---------------- 集計 -------------------
    pass_cnt = 0
    fail_cnt = 0   # 不合格/BAN/インターバル + 各遅延

    for rec in data_manager.interview_records:
        try:
            dt = datetime.fromisoformat(rec.get("date"))
        except Exception:
            continue
        if dt.year != now.year or dt.month != now.month:
            continue

        result = str(rec.get("result", "")).upper()
        if result == "PASS":
            pass_cnt += 1
        elif result.startswith(("FAIL", "BAN", "INTERVAL")):
            fail_cnt += 1

    total     = pass_cnt + fail_cnt
    pass_rate = (pass_cnt / total) * 100 if total else 0.0

    # ---------------- Embed ------------------
    embed = discord.Embed(
        title     = f"面接結果内訳 {year_month}",
        color     = 0x3498db,
        timestamp = datetime.utcnow()
    )
    embed.add_field(name="合格者数",   value=f"{pass_cnt} 人",   inline=False)
    embed.add_field(name="不合格者数", value=f"{fail_cnt} 人",   inline=False)
    embed.add_field(name="合格率",     value=f"{pass_rate:.1f}% ", inline=False)

    # ---------------- 送信 / 更新 -------------
    channel: Optional[discord.TextChannel] = bot.get_channel(MONTHLY_STATS_CHANNEL_ID)
    if channel is None:
        logger.warning("update_monthly_stats: 出力チャンネルが見つかりません。")
        return

    msg_id = data_manager.monthly_stats_message_ids.get(year_month)
    target_msg: Optional[discord.Message] = None

    if msg_id:
        try:
            target_msg = await channel.fetch_message(msg_id)
        except discord.NotFound:
            target_msg = None

    if target_msg is None:      # タイトル一致で履歴検索（後方互換）
        async for hist in channel.history(limit=50):
            if hist.author.id == bot.user.id and hist.embeds:
                t = hist.embeds[0].title or ""
                if t.startswith(f"面接結果内訳 {year_month}"):
                    target_msg = hist
                    break

    try:
        if target_msg:
            await target_msg.edit(embed=embed)
            saved_id = target_msg.id
        else:
            sent = await channel.send(embed=embed)
            saved_id = sent.id
    except Exception as e:
        logger.error(f"update_monthly_stats: メッセージ送信/編集に失敗: {e}")
        return

    data_manager.monthly_stats_message_ids[year_month] = saved_id
    await data_manager.save_data()
    logger.info("update_monthly_stats: 月次内訳を更新しました。")

# ------------------------------------------------
# ★ 追記: 自動キック時に特定チャンネルへログ出力する関数
# ------------------------------------------------
async def log_auto_kick(bot: commands.Bot, user: discord.abc.User, guild: discord.Guild, reason: str):
    """
    自動キックが発生した際に、指定のチャンネル(AUTO_KICK_LOG_CHANNEL_ID)へ
    ユーザー/サーバー/理由を含むログを送信するヘルパー関数
    """
    channel = bot.get_channel(AUTO_KICK_LOG_CHANNEL_ID)
    if channel and isinstance(channel, discord.TextChannel):
        await channel.send(
            f"【自動キック】\n"
            f"ユーザー: {user.mention} (ID: {user.id})\n"
            f"サーバー: {guild.name}\n"
            f"理由: {reason}"
        )
    else:
        logger.warning("自動キックのログ出力先チャンネルが見つかりませんでした。")


# ------------------------------------------------
# BANデータ管理用クラス
# ------------------------------------------------
class BanManager:
    def __init__(self, file_path: str = "ban_data.json"):
        self.file_path = file_path
        self.ban_records = {}  # {"ユーザーID": {"ban_origin": "main" or "sub", "ban_type": "BAN" or "INTERVAL", "ban_time": ISO文字列}}
        self.load_data()

    def load_data(self):
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    self.ban_records = json.load(f)
                logger.info("Banデータロード成功")
            except Exception as e:
                logger.error(f"Banデータロード失敗: {e}")
        else:
            logger.info("Banデータファイルなし。新規作成します。")

    def save_data(self):
        try:
            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump(self.ban_records, f, ensure_ascii=False, indent=4)
            logger.info("Banデータ保存成功")
        except Exception as e:
            logger.error(f"Banデータ保存失敗: {e}")

    def add_ban(self, user_id: int, ban_origin: str, ban_type: str):
        self.ban_records[str(user_id)] = {
            "ban_origin": ban_origin,
            "ban_type": ban_type,
            "ban_time": get_current_time_iso()
        }
        self.save_data()

    def remove_ban(self, user_id: int):
        if str(user_id) in self.ban_records:
            del self.ban_records[str(user_id)]
            self.save_data()

    def check_ban(self, user_id: int):
        record = self.ban_records.get(str(user_id))
        if record:
            if record["ban_type"] == "INTERVAL":
                ban_time = datetime.fromisoformat(record["ban_time"])
                if datetime.now(JST) - ban_time >= timedelta(days=90):
                    self.remove_ban(user_id)
                    return None
            return record
        return None

    def remove_expired(self):
        to_remove = []
        for user_id, record in self.ban_records.items():
            if record["ban_type"] == "INTERVAL":
                ban_time = datetime.fromisoformat(record["ban_time"])
                if datetime.now(JST) - ban_time >= timedelta(days=90):
                    to_remove.append(user_id)
        for user_id in to_remove:
            del self.ban_records[user_id]
        if to_remove:
            self.save_data()


ban_manager = BanManager(BAN_DATA_FILE)

# ------------------------------------------------
# ダッシュボード更新のデバウンス処理
# ------------------------------------------------
_dashboard_update_lock = asyncio.Lock()
_dashboard_update_task: Optional[asyncio.Task] = None


def request_dashboard_update(bot: discord.Client, delay: float = 2.0):
    async def update_after_delay():
        try:
            await asyncio.sleep(delay)
            await update_dashboard(bot)
        except asyncio.CancelledError:
            pass
        finally:
            global _dashboard_update_task
            _dashboard_update_task = None

    async def wrapper():
        global _dashboard_update_task
        async with _dashboard_update_lock:
            if _dashboard_update_task is not None:
                _dashboard_update_task.cancel()
            _dashboard_update_task = asyncio.create_task(update_after_delay())

    # 同期関数内でバックグラウンドタスクとして wrapper() をスケジュールする
    asyncio.create_task(wrapper())

# ------------------------------------------------
# 共通ユーティリティ関数
# ------------------------------------------------
def get_current_time_iso() -> str:
    return datetime.now(JST).isoformat()


def make_progress_key(guild_id: int, member_id: int) -> str:
    return f"{guild_id}-{member_id}"
# ------------------------------------------------
# ★ 面接官自動推薦ヘルパー
# ------------------------------------------------
async def _fetch_schedule_text(bot: discord.Client) -> str:
    """予定表メッセージを 1 つ見つけて内容を返す（5 分キャッシュ）"""
    now = asyncio.get_running_loop().time()
    if now - _schedule_cache["ts"] < SCHEDULE_CACHE_MIN:
        return _schedule_cache["text"]

    guild = bot.get_guild(MAIN_GUILD_ID)
    if guild is None:
        return ""

    schedule_text = ""
    for ch in guild.text_channels:
        try:
            msg = await ch.fetch_message(SCHEDULE_MESSAGE_ID)
            schedule_text = msg.content or (msg.embeds[0].description if msg.embeds else "")
            break
        except (discord.NotFound, discord.Forbidden):
            continue
    _schedule_cache["ts"] = now
    _schedule_cache["text"] = schedule_text
    return schedule_text


def _count_by_interviewer_this_month() -> dict[int, int]:
    ym = datetime.now(JST).strftime("%Y-%m")
    counts: defaultdict[int, int] = defaultdict(int)
    for rec in data_manager.interview_records:
        try:
            dt = datetime.fromisoformat(rec["date"])
        except Exception:
            continue
        if f"{dt.year}-{dt.month:02d}" == ym:
            counts[int(rec["interviewer_id"])] += 1
    return counts

# ------------------------------------------------
# Gemini で “空き＋低負荷” 面接官をリストアップ（最大 3 名）
#   + 候補者プロフィール全文（面接可能時間を含む）も渡す
# ------------------------------------------------
async def _recommend_interviewer_with_gemini(
    bot: discord.Client,
    schedule_text: str,
    profile_text: str | None = None,          # ←★追加
) -> list[int] | None:
    """
    Returns
    -------
    list[int] | None
        優先順に並んだ面接官 ID のリスト（最大 3 件）
    """

    guild = bot.get_guild(MAIN_GUILD_ID)
    if guild is None:
        logger.warning("[autoAssign] MAIN_GUILD_ID の Guild が見つからない")
        return None

    role = guild.get_role(INTERVIEWER_ROLE_ID)
    if role is None or not role.members:
        logger.warning("[autoAssign] 面接担当者ロールが見つからない / メンバーゼロ")
        return None

    # ── 今月の面接回数を集計 ───────────────────────
    counts = _count_by_interviewer_this_month()
    info_lines = [
        f"{m.display_name} (ID:{m.id}) … {counts.get(m.id,0)} 回"
        for m in sorted(role.members, key=lambda u: u.display_name)
    ]
    info_block = "\n".join(info_lines) or "（今月はまだ面接回数がありません）"

    # ── Gemini に渡すプロンプト ────────────────────
    prompt = f"""
あなたは Discord の面接管理ボットです。

# 候補者プロフィール全文
{profile_text or '(プロフィール取得失敗)'}                    

# 面接官の今月の面接回数
{info_block}

# 面接官の予定表
{schedule_text}

## 指示
- 「候補者の面接可能な時間帯」と「面接官の空き時間＆今月の面接回数」を総合して、
  最も適切と思われる面接官を **最大 3 名まで** 優先順で選んでください。
- 出力は改行区切りで  
      ID:123456789012345678  
      ID:234567890123456789  
      ID:345678901234567890  
  のようにしてください。（先頭ほど優先度が高い）
- 余計な説明や名前は付けないでください。
"""

    try:
        resp = genai_client.models.generate_content(
            model="gemini-2.5-flash-preview-05-20",
            contents=prompt,
            config=GenerateContentConfig(
                max_output_tokens=64,
                temperature=0,
                thinking_config=ThinkingConfig(thinking_budget=0),
            ),
        )
        answer = "".join(
            getattr(part, "text", "") for part in resp.candidates[0].content.parts
        ).strip()
        logger.debug(f"[autoAssign] Gemini replied: {answer!r}")

    except Exception as e:
        logger.error(f"[autoAssign] Gemini 呼び出し失敗: {e}")
        return None

    # 「ID:xxxxxxxxxxxxxxx」を最大 3 件パース
    ids = re.findall(r"ID\s*:\s*(\d{17,20})", answer)[:3]
    return [int(x) for x in ids if guild.get_member(int(x))] or None

# ------------------------------------------------
# 推薦結果を処理（3 名表示・メンション付き）
# ------------------------------------------------
# 既存の auto_assign_interviewer 関数をこのコードで置き換えてください

async def auto_assign_interviewer(
    bot: discord.Client,
    candidate_channel: discord.TextChannel,
    cp: dict[str, Any],
) -> None:

    if cp.get("interviewer_id"):
        return  # すでに担当者が設定済みのため処理を中断

    logger.info(f"[autoAssign] 候補者 {candidate_channel.name} の担当者自動割り当てを開始")

    # ① 予定表テキストを取得
    schedule_text = await _fetch_schedule_text(bot)
    if not schedule_text:
        logger.warning("[autoAssign] 予定表メッセージが見つかりません。")
        return
    logger.info(f"[autoAssign] schedule len={len(schedule_text)} chars")

    # ② 候補者プロフィール本文を取得
    profile_text = None
    if cp.get("profile_message_id"):
        try:
            pm = await candidate_channel.fetch_message(cp["profile_message_id"])
            profile_text = pm.content
        except Exception as e:
            logger.warning(f"[autoAssign] プロフィールメッセージの取得に失敗: {e}")

    # ③ Geminiによる推薦を実行
    recommended_ids = await _recommend_interviewer_with_gemini(
        bot, schedule_text, profile_text
    )
    logger.info(f"[autoAssign] Geminiからの推薦IDリスト: {recommended_ids}")
    if not recommended_ids:
        logger.warning("[autoAssign] Gemini から有効な推薦が得られませんでした。")
        return

    # ④ 最優先の担当者を決定し、データを保存
    primary_id = recommended_ids[0]
    cp["interviewer_id"] = primary_id
    await data_manager.save_data()
    logger.info(f"[autoAssign] interviewer_id を {primary_id} で保存しました。")

    # ダッシュボードを更新
    request_dashboard_update(bot)

    # Gemini 推薦結果に基づく DM 通知は行わない

    # ⑤ 管理者へ推薦リストをDMで通知 (既存の処理)
    admin = bot.get_user(MANAGER_USER_ID)
    if admin:
        filtered_dm_ids = [uid for uid in recommended_ids
                           if uid not in RECOMMEND_DM_EXCLUDE_IDS]
        if filtered_dm_ids:
            try:
                counts = _count_by_interviewer_this_month()
                lines = [
                    f"- <@{uid}> (今月 {counts.get(uid, 0)} 回)"
                    for uid in filtered_dm_ids
                ]
                await admin.send(
                    f"🔔 **{candidate_channel.mention}**\n"
                    f"⏩ 推奨面接官（優先順）\n"
                    + "\n".join(lines)
                    + "\n(候補者の希望時間・予定表・回数を総合評価 / Gemini 推薦)"
                )
                logger.info("[autoAssign] 管理者への推薦結果DM送信完了")
            except Exception as e:
                logger.error(f"[autoAssign] 管理者への推薦結果DM失敗: {e}")

    logger.info("[autoAssign] --- 担当者自動割り当て処理完了 ---")

# ------------------------------------------------
# 自動割り当てを行わず、管理者へ推薦 DM のみ送信
# ------------------------------------------------
async def send_recommendation_dm(
    bot: discord.Client,
    candidate_channel: discord.TextChannel,
    cp: dict[str, Any],
) -> None:

    # ① 予定表
    schedule_text = await _fetch_schedule_text(bot)
    if not schedule_text:
        logger.warning("[recommendDM] 予定表メッセージが見つかりません。")
        return

    # ② 候補者プロフィール本文
    profile_text = None
    if cp.get("profile_message_id"):
        try:
            pm = await candidate_channel.fetch_message(cp["profile_message_id"])
            profile_text = pm.content
        except Exception:
            pass

    # ③ Gemini 推薦
    recommended_ids = await _recommend_interviewer_with_gemini(
        bot, schedule_text, profile_text
    )
    logger.info(f"[recommendDM] recommended_ids={recommended_ids}")
    if not recommended_ids:
        logger.warning("[recommendDM] Gemini から有効な推薦が得られませんでした。")
        return

    # 除外対象をフィルタリング
    recommended_ids = [uid for uid in recommended_ids
                       if uid not in RECOMMEND_DM_EXCLUDE_IDS]
    if not recommended_ids:
        logger.info("[recommendDM] 推薦候補が除外対象のみのため DM を送信しません。")
        return

    # ④ 管理者 DM
    admin = bot.get_user(MANAGER_USER_ID)
    if admin:
        try:
            counts = _count_by_interviewer_this_month()
            lines = [
                f"- <@{uid}> (今月 {counts.get(uid,0)} 回)"
                for uid in recommended_ids
            ]
            await admin.send(
                f"🔔 **{candidate_channel.mention}**\n"
                "⏩ 推奨面接官（優先順）\n"
                + "\n".join(lines)
                + "\n(候補者の希望時間・予定表・回数を総合評価 / Gemini 推薦)"
            )
            logger.info("[recommendDM] 推薦結果 DM 送信完了")
        except Exception as e:
            logger.error(f"[recommendDM] 推薦結果 DM 失敗: {e}")

# ------------------------------------------------
# Gemini でプロフィール全文を評価するヘルパー（改善版）
# ------------------------------------------------
async def evaluate_profile_with_ai(
    text: str,
    *,
    debug: bool = False,
    inrate_cleared: bool = False,
    move_cleared: bool = False,
) -> tuple[bool, str]:
    """
    【改善版】
    プロフィールを総合的に評価し、不備があれば「すべて」リストアップして返す。
    不備が一つもなければ (True, "") を返す。

    Returns
    -------
    (is_complete, feedback_or_empty)
        - True, "" … すべて OK
        - False, "不備リスト（箇条書き）" … 修正が必要
    """

    # ────────── プロンプト（改善版） ──────────
    system_prompt = """
あなたは Discord 面接ボットの厳格なプロフィールチェッカーです。
ユーザーが提出したプロフィールが、以下のルールに従っているかを評価してください。

### ルール

#### 必須項目（全13項目）
以下の13項目はすべて記載されている必要があります。
1. 呼ばれたい名前
2. 性別
3. 年齢
4. 身長
5. お住まい（都道府県）
6. 恋愛会議の経験(〇・×)
7. 現在入っている恋愛会議の有無(〇・×)
8. イン率(週〇日以上)
9. 長所
10. 短所
11. アピールポイント
12. 今すぐ面接可能（〇/×）

#### 任意項目
- 「面接できる時間帯」は任意項目です。内容が曖昧でも、未記入でも不備として指摘しないでください。
- 「その他何かあれば」も任意項目です。未記入でも不備として指摘しないでください。

#### 条件
- **年齢**:
    - **具体的な数字**（例: 25歳）で記載されている必要があります。「10代」「20代後半」のような曖昧な表現は不備です。
    - 記載された年齢が **18歳から36歳** の範囲内である必要があります。
- **イン率**:
    - **実質的に「週3日以上」の参加が可能だと解釈できる内容**である必要があります。
    - 「週〇日」という厳密なフォーマットは問いません。意味内容で判断してください。
    - **OKの例**: 「週3日」、「週4日以上」、「5日」、「毎日」、「ほぼ毎日」、「平日なら毎日」
    - **NGの例**: 「週2日」、「週1〜2日」、「不定期」、「気分次第」
- **お住まい**:
    - 日本国内の都道府県が記載されている必要があります。
    - 海外在住の場合は「6か月以内に日本へ移住予定」の旨が明記されている必要があります。
- **日本語**: 全体的に、日本語での円滑なコミュニケーションが可能であると判断できる必要があります。

### 評価手順と出力フォーマット

1.  **致命的な不備のチェック（最優先）**:
    - **年齢が明確に範囲外（18歳未満 or 37歳以上）の場合**:
      他の不備があっても、年齢に関するお断りの定型文のみを出力し、評価を終了してください。
      出力: `募集要項に記載の通り、当サーバーでは18歳以上36歳以下の方を対象としております。…`
    - **日本語でのコミュニケーションが困難と判断される場合**:
      英文のお断り定型文のみを出力し、評価を終了してください。

2.  **その他の不備チェック**:
    - 上記の致命的な不備がなければ、プロフィール全体をチェックしてください。
    - **見つかった不備や確認事項を【すべて】箇条書きでリストアップ**してください。
    - **指摘文言のルール**:
        - **イン率が条件（実質的に週3日以上）を満たさないと判断した場合**: `募集要項に記載の通り、当サーバーでは週3回以上の会議通話へのご参加をお願いしております。
もし可能であれば修正お願いします。難しい場合はご退出お願いします。` という定型文で指摘してください。
        - **年齢が曖昧な場合** (例: 「20代」): `- 年齢は「20代」ではなく、具体的な数字で記載してください。` と指摘してください。
    - 箇条書きの前後に余計な挨拶や説明は不要です。

3.  **すべての条件をクリア**:
    - すべての必須項目が記載され、すべての条件を満たしている場合のみ、大文字で `OK` とだけ出力してください。
""".strip()

    # --- フラグによる特例 ---
    extra = []
    if inrate_cleared:
        extra.append("※ 特例: イン率（週3日以上）の条件は、すでに口頭で確認済みとして扱ってください。")
    if move_cleared:
        extra.append("※ 特例: 『海外在住→6か月以内に日本移住』の条件は、すでに口頭で確認済みとして扱ってください。")
    if extra:
        system_prompt += "\n\n### 追加指示\n" + "\n".join(extra)

    prompt = f"{system_prompt}\n\n# ユーザー入力:\n```\n{text}\n```"

    try:
        resp = genai_client.models.generate_content(
            model="gemini-2.5-flash-preview-05-20",
            contents=prompt,
            config=GenerateContentConfig(
                max_output_tokens=512,
                temperature=0,
                thinking_config=ThinkingConfig(thinking_budget=0),
            ),
        )
        answer = "".join(
            getattr(p, "text", "") for p in resp.candidates[0].content.parts
        ).strip()

        if debug:
            logger.info(f"[AI-RESP]\n{answer}")

    except Exception as e:
        logger.error(f"Gemini 呼び出し失敗: {e}")
        return False, "プロフィール評価中にエラーが発生しました。お手数ですが再投稿をお願いいたします。"

    # ---------- 結果判定 ----------
    if answer.upper() == "OK":
        return True, ""

    # 年齢条件や言語のような即時お断りケース
    if answer.startswith("募集要項に記載の通り") or "This server is for Japanese speakers only" in answer:
         return False, answer

    # 上記以外は不備リストとみなし、親切な前置きを追加する
    feedback = (
        "プロフィールのご提出ありがとうございます。\n"
        "大変お手数ですが、以下の点についてご確認・修正の上、再度ご投稿いただけますでしょうか。\n\n"
        f"```\n{answer}\n```"
    )
    return False, feedback
# ------------------------------------------------
# AI で「肯定的な返答か」を判定するヘルパー
# ------------------------------------------------
async def is_affirmative_ai(text: str, *, debug: bool = False) -> bool:
    """
    Gemini 2.5 Flash に問い掛け、メッセージが肯定的・同意的かどうかを判定して True/False を返す。

    - 日本語／英語混在どちらにも対応
    - 出力は **YES / NO** の 2 文字のみを要求して確実にパース
    """
    prompt = f"""
あなたは Discord 面接ボットのサブモジュールです。
以下のユーザー発言が「肯定的・同意的に受け取れるか」を判定し、 **YES** か **NO** のどちらか 1 単語だけを出力してください。
肯定的とは、提案・要請などに賛同する意味合い（例: はい / 大丈夫です / OK / もちろん など）を指します。

# ユーザー発言
{text}
""".strip()

    try:
        resp = genai_client.models.generate_content(
            model="gemini-2.5-flash-preview-05-20",
            contents=prompt,
            config=GenerateContentConfig(
                max_output_tokens=4,
                temperature=0,
                thinking_config=ThinkingConfig(thinking_budget=0),
            ),
        )
        answer = "".join(getattr(p, "text", "") for p in resp.candidates[0].content.parts).strip().upper()

        if debug:
            logger.info(f"[AFFIRMATIVE_AI] Q={text!r} → A={answer!r}")

        return answer.startswith("Y")        # "YES" → True, それ以外 → False
    except Exception as e:
        logger.error(f"is_affirmative_ai: Gemini 呼び出し失敗: {e}")
        # フォールバック：失敗時は従来の単語フィルタに回す
        return any(word in text.lower() for word in
                   ["はい", "大丈夫", "できます", "可能です", "問題ない", "問題ありません", "ok", "yes"])
# ------------------------------------------------
# Gemini で「YES / NO / UNSURE」を返す分類ヘルパー
# ------------------------------------------------
async def classify_yes_no_ai(text: str, *, debug: bool = False) -> str:
    """
    Parameters
    ----------
    text : str
        候補者の発言
    Returns
    -------
    "YES" | "NO" | "UNSURE"
        YES…肯定 / NO…否定 / UNSURE…曖昧
    """
    prompt = f"""
あなたは Discord 面接ボットの入力分類器です。
次のユーザー発言が提案・質問への **肯定** (YES) / **否定** (NO) / **曖昧** (UNSURE) のどれに当たるかを判定し、
**YES / NO / UNSURE** のいずれか 1 語だけ出力してください。
- 日本語・英語混在可
- 例: 「はい」「もちろん」「OK」「あります」 → YES
      「いいえ」「無理です」「行けません」   → NO
      それ以外や判断が難しければ UNSURE
# ユーザー発言
{text}
""".strip()

    try:
        resp = genai_client.models.generate_content(
            model="gemini-2.5-flash-preview-05-20",
            contents=prompt,
            config=GenerateContentConfig(
                max_output_tokens=4,
                temperature=0,
                thinking_config=ThinkingConfig(thinking_budget=0),
            ),
        )
        answer = "".join(
            getattr(p, "text", "") for p in resp.candidates[0].content.parts
        ).strip().upper()

        if debug:
            logger.info(f"[CLASSIFY_YN] Q={text!r} → A={answer!r}")

        if answer.startswith("Y"):
            return "YES"
        if answer.startswith("N"):
            return "NO"
        return "UNSURE"

    except Exception as e:
        logger.error(f"classify_yes_no_ai: Gemini 呼び出し失敗: {e}")
        # フォールバック：判断不能
        return "UNSURE"

# ------------------------------------------------
# 投稿が「プロフィール本文らしい」か判定するヘルパー
# ------------------------------------------------
def looks_like_profile(text: str) -> bool:
    """
    - 必須項目（+任意項目）の見出しを 5 個以上含む
    - または改行が 8 行以上
    いずれかを満たす場合 True
    """
    headers = [
        "呼ばれたい名前", "性別", "年齢", "身長", "お住まい",
        "恋愛会議の経験", "現在入っている恋愛会議", "イン率",
        "長所", "短所", "アピールポイント", "今すぐ面接可能",
        "面接できる時間帯", "その他"
    ]
    hit = sum(1 for h in headers if h in text)
    if hit >= 5:
        return True
    return text.count("\n") >= 8


def get_main_display_name(bot: discord.Client, user_id: int) -> str:
    """
    メインサーバーのニックネーム（存在しなければユーザー名）を返す。
    bot: discord.Client  /  user_id: int
    """
    main_guild = bot.get_guild(MAIN_GUILD_ID)
    if main_guild:
        member = main_guild.get_member(user_id)
        if member:
            return member.display_name
    user = bot.get_user(user_id)
    if user:
        return user.display_name or user.name
    return f"ID:{user_id}"


async def update_or_send_message(
        channel: discord.TextChannel,
        current_msg_id: Optional[int],
        content: str,
        embed: Optional[discord.Embed] = None
) -> int:
    try:
        if current_msg_id:
            msg = await channel.fetch_message(current_msg_id)
            if msg.author.id == channel.guild.me.id:
                await msg.edit(content=content, embed=embed)
                return msg.id
        new_msg = await channel.send(content=content, embed=embed)
        return new_msg.id
    except discord.NotFound:
        new_msg = await channel.send(content=content, embed=embed)
        return new_msg.id

# ─────────────────────────────────────────────────────────
# VC・面接テキストチャンネル削除用ヘルパー（シグネチャ修正）
# ─────────────────────────────────────────────────────────
async def delete_candidate_channels(
    bot: commands.Bot,
    guild: discord.Guild,
    candidate_id: int
) -> None:
    progress_key = make_progress_key(guild.id, candidate_id)

    # ---------- テキストチャンネル削除 ----------
    for ch in guild.text_channels:
        if data_manager.interview_channel_mapping.get(ch.id) == progress_key:
            try:
                await ch.delete()
                logger.info(f"候補者 {candidate_id} のテキストチャンネル {ch.id} を削除")
            except Exception as e:
                logger.error(f"テキストチャンネル {ch.id} 削除失敗: {e}")
            data_manager.interview_channel_mapping.pop(ch.id, None)
            break

    # ---------- VC 削除 ----------
    # 1) cp 由来の voice_channel_id
    cp = data_manager.candidate_progress.get(progress_key)
    vc_candidates: list[int] = []
    if cp and cp.get("voice_channel_id"):
        vc_candidates.append(cp["voice_channel_id"])

    # 2) インデックスから逆引き
    for ch_id, pk in data_manager.interview_channel_mapping.items():
        if pk == progress_key:
            vc_candidates.append(ch_id)

    # 重複除去
    for vc_id in set(vc_candidates):
        vc_obj = bot.get_channel(vc_id)
        if isinstance(vc_obj, discord.VoiceChannel):
            try:
                await vc_obj.delete()
                logger.info(f"候補者 {candidate_id} のボイスチャンネル {vc_id} を削除")
            except Exception as e:
                logger.error(f"ボイスチャンネル {vc_id} 削除失敗: {e}")
            data_manager.interview_channel_mapping.pop(vc_id, None)
            if cp:
                cp.pop("voice_channel_id", None)
            break   # 1 つ削除できれば十分

    # 変更を永続化
    await data_manager.save_data()


def update_candidate_status(cp: Dict[str, Any], status: str) -> None:
    cp['status'] = status
    cp['timestamp'] = get_current_time_iso()


async def ensure_channel_exists(
        bot: discord.Client, progress_key: str, cp: Dict[str, Any]
) -> Optional[discord.TextChannel]:
    channel = bot.get_channel(cp.get('channel_id'))
    if not channel:
        data_manager.candidate_progress.pop(progress_key, None)
        await data_manager.save_data()
        await update_dashboard(bot)
        return None
    return channel


def generate_custom_progress_bar(count: int, goal: int) -> str:
    if count <= goal:
        filled = "🟩" * count
        empty = "⬜" * (goal - count)
        bar = filled + empty
        return f"[{bar}] {count}/{goal}回"
    else:
        extra = count - goal
        if count < 20:
            colored_block = "🟨"
        elif count < 30:
            colored_block = "🟧"
        else:
            colored_block = "🟥"
        base_bar = colored_block * goal
        extra_bar = "★" * extra
        return f"[{base_bar}{extra_bar}] {count}/{goal}回 (+{extra}超過)"

def make_message_link(guild_id: int, channel_id: int, message_id: int) -> str:
    """<https://discord.com/channels/...> 形式のジャンプ URL を返す"""
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"

class utils:
    @staticmethod
    async def safe_fetch_member(guild: discord.Guild, user_id: int) -> Optional[discord.Member]:
        try:
            return await guild.fetch_member(user_id)
        except discord.NotFound:
            return None
        except discord.HTTPException:
            logger.warning(f"fetch_member failed in guild {guild.id}")
            return None


# ------------------------------------------------
# DataManager（永続化用）
# ------------------------------------------------
class DataManager:
    def __init__(self, file_path: str):
        self.file_path = file_path
        self.lock = asyncio.Lock()
        self.interview_records: List[Dict[str, Any]] = []
        self.interviewer_stats_message_ids: Dict[str, int] = {}
        self.monthly_stats_message_ids: Dict[str, int] = {}
        self.candidate_progress: Dict[str, Dict[str, Any]] = {}
        self.interview_channel_mapping: Dict[int, str] = {}
        self.dashboard_message_id: Optional[int] = None
        self.memo_history: Dict[str, List[Dict[str, Any]]] = {}
        self.left_before_interview_logs: Dict[str, str] = {}  # ★ 追加: {user_id: message_url}
        self.load_data()

    async def save_data(self) -> None:
        async with self.lock:
            data: Dict[str, Any] = {
                'interview_records': self.interview_records,
                'interviewer_stats_message_ids': self.interviewer_stats_message_ids,
                'monthly_stats_message_ids': self.monthly_stats_message_ids,
                'candidate_progress': self.candidate_progress,
                'interview_channel_mapping': {str(k): v for k, v in self.interview_channel_mapping.items()},
                'dashboard_message_id': self.dashboard_message_id,
                'memo_history': self.memo_history,
                'left_before_interview_logs': self.left_before_interview_logs,  # ★ 追加
            }
            try:
                async with aiofiles.open(self.file_path, 'w', encoding='utf-8') as f:
                    await f.write(json.dumps(data, ensure_ascii=False, indent=4))
                logger.info("データ保存に成功")
            except Exception as e:
                logger.error(f"データ保存に失敗: {e}")

    def load_data(self) -> None:
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if isinstance(data, list): # 旧形式からの移行
                        self.interview_records = data
                    elif isinstance(data, dict):
                        self.interview_records = data.get('interview_records', [])
                        self.interviewer_stats_message_ids = data.get('interviewer_stats_message_ids', {})
                        self.monthly_stats_message_ids = data.get('monthly_stats_message_ids', {})
                        self.candidate_progress = data.get('candidate_progress', {})
                        imap = data.get('interview_channel_mapping', {})
                        self.interview_channel_mapping = {int(k): v for k, v in imap.items()}
                        self.dashboard_message_id = data.get('dashboard_message_id')
                        self.memo_history = data.get('memo_history', {})
                        self.left_before_interview_logs = data.get('left_before_interview_logs', {}) # ★ 追加
                    # logger.info("データロードに成功") # ログが多すぎるのでコメントアウト
            except Exception as e:
                import traceback
                logger.error(f"データロードに失敗: {e}\n{traceback.format_exc()}")
                logger.info("データロードに成功")
            except Exception as e:
                import traceback
                logger.error(f"データロードに失敗: {e}\n{traceback.format_exc()}")
        else:
            logger.warning(f"データファイルなし。空の状態から開始します。({self.file_path} が見つかりません。カレントディレクトリ: {os.getcwd()})")

data_manager = DataManager(DATA_FILE_PATH)


# ------------------------------------------------
# 通知／補助関数
# ------------------------------------------------
async def send_interviewer_notification(bot: discord.Client, remind_channel: discord.TextChannel,
                                        candidate_channel: discord.TextChannel) -> None:
    await remind_channel.send(
        f"日程調整お願いします。 <@&{INTERVIEWER_ROLE_ID}> {candidate_channel.mention}"
    )
# ------------------------------------------------
# 通知ヘルパー（面接官 DM 通知）
# ------------------------------------------------
async def notify_interviewer_of_candidate_message(
        bot: discord.Client,
        cp: Dict[str, Any],
        message: discord.Message,
        silence_seconds: int = 300        # 連続通知抑止 (秒)
) -> None:
    """
    候補者がメンション／返信なしで投稿した場合に
    担当面接官へ DM で知らせるヘルパー。
    """
    interviewer_id: Optional[int] = cp.get("interviewer_id")
    if not interviewer_id:          # 担当未設定なら終了
        return

    # --- 連続通知の抑止 -------------------------------------------------
    last_iso: Optional[str] = cp.get("last_dm_notify")
    if last_iso:
        try:
            last_dt = datetime.fromisoformat(last_iso)
            if (datetime.now(JST) - last_dt).total_seconds() < silence_seconds:
                return
        except Exception:
            pass  # 壊れていても通知は出す

    interviewer: Optional[discord.User] = bot.get_user(interviewer_id)
    if interviewer is None:
        return

    # --- DM 送信 -------------------------------------------------------
    try:
        await interviewer.send(
            f"👋 候補者 **{message.author.display_name}** から新しいメッセージです:\n{message.jump_url}"
        )
        cp["last_dm_notify"] = get_current_time_iso()   # タイムスタンプ更新
        await data_manager.save_data()
    except Exception as e:
        logger.error(f"DM 通知失敗: {e}")


async def notify_interviewer_assignment(
    interviewer: discord.abc.User,
    candidate_member: discord.Member,
    candidate_channel: discord.TextChannel,
    cp: Dict[str, Any]
) -> None:
    """面接担当者に割り当て通知を送信"""
    if cp.get("notified_assignment"):
        return
    try:
        await interviewer.send(
            "📄 **面接担当のお知らせ** 📄\n\n"
            f"候補者「**{candidate_member.display_name}**」さんの面接担当に割り当てられました。\n\n"
            "以下の専用チャンネルで候補者と直接やり取りし、面接日程の調整をお願いします。\n"
            f"▶ **専用チャンネル**: {candidate_channel.mention}\n\n"
            "よろしくお願いいたします。"
        )
        cp["notified_assignment"] = True
        await data_manager.save_data()
        logger.info(f"担当者 {interviewer.id} へ割り当てDMを送信しました")
    except discord.Forbidden:
        logger.warning(
            f"担当者 {interviewer.id} への割り当てDMがブロックされており、送信できませんでした。"
        )
    except Exception as e:
        logger.error(f"担当者への割り当てDM送信中にエラーが発生しました: {e}")


async def notify_interviewer_schedule(
    interviewer: discord.abc.User,
    candidate_member: discord.Member,
    candidate_channel: discord.TextChannel,
    schedule_time: datetime,
) -> None:
    """面接担当者に面接日時を通知"""
    try:
        await interviewer.send(
            "⏰ **面接日時が設定されました** ⏰\n\n"
            f"候補者: **{candidate_member.display_name}**\n"
            f"日時: {schedule_time.strftime('%m/%d %H:%M')}\n"
            f"チャンネル: {candidate_channel.mention}"
        )
        logger.info(f"担当者 {interviewer.id} へ面接日時DMを送信しました")
    except discord.Forbidden:
        logger.warning(
            f"担当者 {interviewer.id} への面接日時DMがブロックされており、送信できませんでした。"
        )
    except Exception as e:
        logger.error(f"担当者への面接日時DM送信中にエラーが発生しました: {e}")


def get_interviewer_role(guild: discord.Guild) -> Optional[discord.Role]:
    if guild.id == MAIN_GUILD_ID:
        role: Optional[discord.Role] = guild.get_role(INTERVIEWER_ROLE_ID)
        if role is None:
            logger.warning(f"ID {INTERVIEWER_ROLE_ID} の面接担当者ロールが見つかりません")
        return role
    else:
        role = discord.utils.get(guild.roles, name="面接手伝い")
        if role is None:
            logger.warning(f"名前 '面接手伝い' のロールが見つかりません")
        return role


async def update_dashboard(bot: discord.Client) -> None:
    await bot.wait_until_ready()
    dashboard_channel: Optional[discord.TextChannel] = bot.get_channel(DASHBOARD_CHANNEL_ID)
    if dashboard_channel is None:
        logger.error(f"ダッシュボードチャンネル (ID: {DASHBOARD_CHANNEL_ID}) が見つかりません")
        return
    now = datetime.now(JST)
    status_mapping = {
        "プロフィール未記入": "プロフィール未記入",
        "記入済み": "記入済み",
        "担当者待ち": "担当者待ち",
        "日程調整済み": "日程調整済み",
        "面接済み": "面接済み",
        "不合格": None,
    }
    embed_config = {
        "プロフィール未記入": {"title": "⚠️ プロフィール未記入", "color": 0x808080},
        "記入済み": {"title": "要連絡！", "color": 0x00FF00},
        "担当者待ち": {"title": "日程調整してね！", "color": 0xFF0000},
        "日程調整済み": {"title": "📅 日程調整済み", "color": 0x0000FF},
        "面接済み": {"title": "✅ 面接済み", "color": 0x808080},
    }
    dashboard_sections = {key: [] for key in embed_config.keys()}

    for progress_key, cp in data_manager.candidate_progress.items():
        orig_status = cp.get("status", "")
        display_key = status_mapping.get(orig_status)
        if display_key is None:
            continue
        channel_obj: Optional[discord.TextChannel] = bot.get_channel(cp.get("channel_id"))
        if channel_obj is None:
            logger.warning(f"候補者 {progress_key} のチャンネル (ID: {cp.get('channel_id')}) が見つかりません")
            continue
        channel_link: str = channel_obj.mention
        if orig_status != "日程調整済み" and cp.get("voice_channel_id"):
            vc_obj: Optional[discord.VoiceChannel] = bot.get_channel(cp.get("voice_channel_id"))
            if vc_obj:
                channel_link += f" (VC: {vc_obj.mention})"
        candidate_id = cp.get("candidate_id")
        candidate: Optional[discord.User] = bot.get_user(candidate_id)
        if not candidate:
            logger.warning(f"候補者 {candidate_id} が見つかりません")
            continue

        entry = ""
        if orig_status in ["担当者待ち", "日程調整済み", "面接済み"]:
            interviewer = bot.get_user(cp.get("interviewer_id")) if cp.get("interviewer_id") else None
            interviewer_name = interviewer.display_name if interviewer else "未設定"
            interview_time_str = ""
            if cp.get("interview_time"):
                try:
                    it = datetime.fromisoformat(cp.get("interview_time"))
                    interview_time_str = it.strftime('%m/%d %H:%M')
                except Exception as e:
                    logger.error(f"面接時間解析失敗: {e}")
            entry = f"**{interviewer_name}** | {interview_time_str} | {candidate.display_name} {channel_link}"
        elif orig_status == "記入済み":
            prefix = ""
            if cp.get("profile_filled_time"):
                try:
                    filled_time = datetime.fromisoformat(cp.get("profile_filled_time"))
                    hours_passed = (now - filled_time).total_seconds() / 3600
                    if hours_passed >= 16:
                        prefix = ":bangbang: "
                    elif hours_passed >= 8:
                        prefix = ":exclamation: "
                except Exception as e:
                    logger.error(f"記入済み時間解析失敗: {e}")
            entry = f"{prefix}**{candidate.display_name}** {channel_link}"
        else:
            entry = f"{candidate.display_name} {channel_link}"
        dashboard_sections[display_key].append(entry)

    dashboard_sections["記入済み"].sort()
    for key in ["担当者待ち", "面接済み"]:
        dashboard_sections[key].sort(key=lambda entry: entry.split(" | ")[0])
    if dashboard_sections["日程調整済み"]:
        def sort_key(entry: str):
            parts = entry.split(" | ")
            interviewer_name = parts[0].replace("**", "").strip() if len(parts) > 0 else ""
            interview_time_str = parts[1].strip() if len(parts) > 1 else ""
            try:
                dt = datetime.strptime(interview_time_str, "%m/%d %H:%M")
            except Exception:
                dt = datetime.min
            return (interviewer_name, dt)

        dashboard_sections["日程調整済み"].sort(key=sort_key)
    dashboard_sections["プロフィール未記入"].sort()

    embeds = []
    for key, config in embed_config.items():
        entries = dashboard_sections.get(key, [])
        description = "\n".join(entries) if entries else "なし"
        embed = discord.Embed(
            title=config["title"],
            description=description,
            color=config["color"],
            timestamp=datetime.now(JST)
        )
        embed.set_footer(text="最終更新: " + now.strftime("%Y-%m-%d %H:%M:%S"))
        embeds.append(embed)

    if data_manager.dashboard_message_id:
        try:
            msg = await dashboard_channel.fetch_message(data_manager.dashboard_message_id)
            await msg.edit(content="", embeds=embeds)
        except discord.NotFound:
            msg = await dashboard_channel.send(embeds=embeds)
            data_manager.dashboard_message_id = msg.id
    else:
        msg = await dashboard_channel.send(embeds=embeds)
        data_manager.dashboard_message_id = msg.id
    await data_manager.save_data()
    logger.info("ダッシュボード更新完了")

# ------------------------------------------------
# CandidateContext および補助関数
# ------------------------------------------------
@dataclass
class CandidateContext:
    candidate_id: int
    progress: Dict[str, Any]
    target_guild: discord.Guild
    target_member: discord.Member
    main_guild: discord.Guild
    interviewer: discord.Member
    progress_key: str

async def get_candidate_context(
    interaction: discord.Interaction,
    progress_key_override: Optional[str] = None,
    candidate_id: Optional[int] = None
) -> Optional[CandidateContext]:
    """
    ────────────────────────────────────────────────────────────────
    ・面接関連ボタンで呼ばれ、候補者／担当者／進捗をまとめて取得するヘルパー
    ・interviewer_id が未設定なら「ボタンを押した面接官」を自動で担当者に登録
    ・interview_channel_mapping に欠損があっても、candidate_progress を走査して
      自己修復（チャンネル ID → progress_key の再登録）を行う
    ────────────────────────────────────────────────────────────────
    """

    async def send_error(msg: str):
        if not interaction.response.is_done():
            await interaction.response.send_message(msg, ephemeral=True)
        else:
            await interaction.followup.send(msg, ephemeral=True)

    bot: discord.Client = interaction.client

    # ─────────────────────────────────────────────
    # 1) progress_key を取得  (int → str → 自己修復)
    # ─────────────────────────────────────────────
    progress_key = (
        progress_key_override
        or data_manager.interview_channel_mapping.get(interaction.channel.id)          # int キー
        or data_manager.interview_channel_mapping.get(str(interaction.channel.id))     # 旧 str キー
    )

    # ----- 欠損時: candidate_progress をスキャンして自己修復 -----
    if progress_key is None:
        for pk, rec in data_manager.candidate_progress.items():
            if rec.get("channel_id") == interaction.channel.id or \
               rec.get("voice_channel_id") == interaction.channel.id:
                progress_key = pk
                # int キーでマッピングを復元
                data_manager.interview_channel_mapping[interaction.channel.id] = pk
                await data_manager.save_data()
                break

    if progress_key is None:
        await send_error("進捗情報が見つかりません。")
        return None

    # ─────────────────────────────────────────────
    # 2) 進捗データと候補者 ID
    # ─────────────────────────────────────────────
    cp = data_manager.candidate_progress.get(progress_key)
    if not cp:
        await send_error("進捗情報が見つかりません。")
        return None

    cid: int = cp.get("candidate_id", candidate_id)

    # ─────────────────────────────────────────────
    # 3) メインサーバーと面接担当ロール
    # ─────────────────────────────────────────────
    main_guild: Optional[discord.Guild] = bot.get_guild(MAIN_GUILD_ID)
    if not main_guild:
        await send_error("メインサーバーが見つかりません。")
        return None

    interviewer_role: Optional[discord.Role] = main_guild.get_role(INTERVIEWER_ROLE_ID)
    if interviewer_role is None:
        await send_error("面接担当者ロールが見つかりません。")
        return None

    # ボタンを押した人の “メインサーバー側 Member” を取得
    main_member = main_guild.get_member(interaction.user.id) \
        or await utils.safe_fetch_member(main_guild, interaction.user.id)

    # ─────────────────────────────────────────────
    # 4) interviewer_id を確定
    #    ・未設定なら、押した人が面接担当ロールを持っていれば自動登録
    # ─────────────────────────────────────────────
    interviewer_id: Optional[int] = cp.get("interviewer_id")
    if interviewer_id is None:
        if main_member and interviewer_role in main_member.roles:
            cp["interviewer_id"] = main_member.id
            interviewer_id = main_member.id
            await data_manager.save_data()
            request_dashboard_update(bot)
        else:
            await send_error("担当者が設定されていません。")
            return None

    # 担当者 Member オブジェクト取得（メインサーバー）
    interviewer = main_guild.get_member(interviewer_id) \
        or await utils.safe_fetch_member(main_guild, interviewer_id)
    if interviewer is None or interviewer_role not in interviewer.roles:
        await send_error("操作権限がありません。")
        return None

    # ─────────────────────────────────────────────
    # 4-B) ボタンを押した人の権限確認
    #      ・担当者本人、または面接担当ロールを持つメンバーのみ許可
    # ─────────────────────────────────────────────
    if not (
        main_member
        and (
            main_member.id == interviewer_id
            or interviewer_role in main_member.roles
        )
    ):
        await send_error("操作権限がありません。")
        return None

    # ─────────────────────────────────────────────
    # 5) 対象候補者 (guild / member) を探す
    # ─────────────────────────────────────────────
    guild_ids: list[int] = []
    source_gid: Optional[int] = cp.get("source_guild_id")
    if source_gid:
        guild_ids.append(source_gid)
    if interaction.guild and interaction.guild.id not in guild_ids:
        guild_ids.append(interaction.guild.id)
    guild_ids.extend(g.id for g in bot.guilds if g.id not in guild_ids)

    target_guild: Optional[discord.Guild] = None
    target_member: Optional[discord.Member] = None
    for gid in guild_ids:
        g = bot.get_guild(gid)
        if g is None:
            continue
        member = g.get_member(cid) or await utils.safe_fetch_member(g, cid)
        if member:
            target_guild, target_member = g, member
            if source_gid != gid:
                cp["source_guild_id"] = gid
                await data_manager.save_data()
            break

    if target_member is None:
        await send_error("対象メンバーが見つかりません。")
        return None

    # ─────────────────────────────────────────────
    # 6) まとめて返却
    # ─────────────────────────────────────────────
    return CandidateContext(
        candidate_id=cid,
        progress=cp,
        target_guild=target_guild,
        target_member=target_member,
        main_guild=main_guild,
        interviewer=interviewer,
        progress_key=progress_key,
    )

# ------------------------------------------------
# ヘルパー関数（共通処理）
# ------------------------------------------------
async def update_memo_result_simple(target_member: discord.Member, result_str: str) -> None:
    candidate_key = str(target_member.id)
    if candidate_key in data_manager.memo_history and data_manager.memo_history[candidate_key]:
        data_manager.memo_history[candidate_key][-1]["result"] = result_str
        await data_manager.save_data()


async def process_immediate_action(
        interaction: discord.Interaction,
        context: CandidateContext,
        action_type: str
) -> None:
    """
    fail / ban / interval いずれも:
        ① 面接記録・進捗削除・統計更新
        ② ボタンを押したサーバー (target_guild) だけでキック
    ban / interval のときは BanManager へも記録を残す
    """
    candidate_id, cp, target_guild, target_member, main_guild, interviewer, progress_key = (
        context.candidate_id, context.progress, context.target_guild,
        context.target_member, context.main_guild, context.interviewer, context.progress_key
    )

    # ----- 理由 -----
    reasons = {
        "fail":     "面接不合格",
        "ban":      "即時BANにより入室不可",
        "interval": "即時インターバルにより入室不可"
    }
    reason = reasons.get(action_type)
    if not reason:
        return  # 想定外

    # ----- BAN / INTERVAL は BanManager にも記録 -----
    if action_type in ("ban", "interval"):
        ban_origin = "main" if target_guild.id == MAIN_GUILD_ID else "sub"
        ban_manager.add_ban(
            candidate_id,
            ban_origin,
            "BAN" if action_type == "ban" else "INTERVAL"
        )

    try:
        # --- 1. チャンネル削除 --------------------------------------------
        try:
            await delete_candidate_channels(interaction.client, target_guild, candidate_id)
        except Exception as e:
            logger.error(f"候補者チャンネル削除失敗: {e}")

        # --- 2. 面接記録追加 & 進捗削除 -----------------------------------
        data_manager.interview_records.append({
            "date":          get_current_time_iso(),
            "interviewer_id": cp["interviewer_id"],
            "interviewee_id": candidate_id,
            "result":         action_type.upper()
        })
        update_candidate_status(cp, action_type.upper())
        data_manager.candidate_progress.pop(progress_key, None)
        await data_manager.save_data()

        # --- 3. ダッシュボード & 統計更新 ---------------------------------
        request_dashboard_update(interaction.client)
        asyncio.create_task(update_stats(interaction.client))
        await update_memo_result_simple(target_member, action_type.upper())

        # ③ **押されたサーバーだけでキック** -------------------------------
        kicked = False
        try:
            member = target_guild.get_member(candidate_id) or await target_guild.fetch_member(candidate_id)
            if member:
                await target_guild.kick(member, reason=reason)
                kicked = True
                logger.info(f"Guild {target_guild.id}: {action_type.upper()} で {candidate_id} をキック")
                await log_auto_kick(interaction.client, member, target_guild, reason)
        except discord.Forbidden:
            logger.warning(f"[権限不足] Guild {target_guild.id}: {candidate_id} をキックできません")
        except discord.HTTPException as e:
            logger.error(f"Guild {target_guild.id}: キック失敗 ({e})")

        # ④ 応答 -----------------------------------------------------------
        msg = (
            f"{target_member.mention} を **{action_type.upper()}** にしました。\n"
            f"{'✅ キック完了' if kicked else '⚠️ キックに失敗 / 既にいません'}"
        )
        await interaction.followup.send(msg, ephemeral=True)



    except Exception as e:
        logger.error(f"process_immediate_action 全体失敗: {e}", exc_info=True)


async def register_delayed_action(
        interaction: discord.Interaction,
        context: CandidateContext,
        action_type: str
) -> None:
    candidate_id, cp, target_guild, target_member, *_ = (
        context.candidate_id, context.progress, context.target_guild, context.target_member
    )

    # --- BanManager への登録 ----------------------------------------------
    if action_type in ("ban", "interval"):
        ban_origin = "main" if target_guild.id == MAIN_GUILD_ID else "sub"
        ban_manager.add_ban(
            candidate_id,
            ban_origin,
            "BAN" if action_type == "ban" else "INTERVAL"
        )

    # --- 遅延アクションを保存 ---------------------------------------------
    tomorrow_9 = datetime.combine(
        datetime.now(JST).date() + timedelta(days=1),
        time(9, tzinfo=JST)
    )
    action = {
        "id": str(uuid.uuid4()),
        "action_type": action_type,
        "candidate_id": candidate_id,
        "progress_key": context.progress_key,  # 実行時に進捗を削除するためキーを渡す
        "scheduled_time": tomorrow_9.isoformat(),
        "apply_all": target_guild.id == MAIN_GUILD_ID,
        **({} if target_guild.id == MAIN_GUILD_ID else {"guild_id": target_guild.id})
    }
    await delayed_action_manager.add(action)

    # --- 面接記録・進捗 ----------------------------------------------------
    update_candidate_status(cp, f"DELAY_{action_type.upper()}")
    data_manager.interview_records.append({
        "date":          get_current_time_iso(),
        "interviewer_id": cp["interviewer_id"],
        "interviewee_id": candidate_id,
        "result":        f"{action_type.upper()} (遅延)"
    })
    await data_manager.save_data()

    # --- UI 更新 ----------------------------------------------------------
    asyncio.create_task(update_stats(interaction.client))

    await interaction.followup.send(
        f"{target_member.mention} の **{action_type.upper()}** を "
        f"{tomorrow_9.strftime('%Y-%m-%d %H:%M')} に予約しました。",
        ephemeral=True
    )

# ▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼ 修正箇所 ▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼
async def process_pass_action(interaction: discord.Interaction,
                              context: CandidateContext) -> None:
    """
    合格処理（チャンネル削除版）:
      1. PASS ロール付与 + 候補者ロール剥奪
      2. チャンネル／VC を削除
      3. 面接記録・進捗更新 (status=案内待ち)
      4. 合格メモ送信
      5. ダッシュボード・統計更新
    """
    global transient_memo_cache
    candidate_id, cp, target_guild, target_member, main_guild, interviewer, progress_key = (
        context.candidate_id, context.progress, context.target_guild,
        context.target_member, context.main_guild, context.interviewer,
        context.progress_key
    )

    # ---------- ① PASS ロール付与 + 候補者ロール剥奪 ----------
    pass_role = (
        target_guild.get_role(PASS_ROLE_ID)
        if target_guild.id == MAIN_GUILD_ID
        else discord.utils.get(target_guild.roles, name=OTHER_SERVER_PASS_ROLE_NAME)
    )
    if pass_role is None:
        await interaction.followup.send("合格ロールが見つかりませんでした。", ephemeral=True)
        return

    remove_roles = [r for r in target_member.roles if r.id in CANDIDATE_ROLE_IDS]
    try:
        await target_member.add_roles(pass_role, reason="面接合格")
        if remove_roles:
            await target_member.remove_roles(*remove_roles, reason="面接合格")
    except Exception as e:
        logger.error(f"ロール操作失敗: {e}")
        await interaction.followup.send("ロール付与/剥奪に失敗しました。", ephemeral=True)
        return

    # ---------- ② テキストチャンネル & VC を削除 ---------------
    await delete_candidate_channels(interaction.client, target_guild, candidate_id)

    # ---------- ③ 面接記録・進捗 ------------------------------
    update_candidate_status(cp, "案内待ち")
    # チャンネルが存在しなくなるので参照をクリア
    cp.pop("channel_id", None)
    cp.pop("voice_channel_id", None)

    data_manager.interview_records.append({
        "date":          get_current_time_iso(),
        "interviewer_id": cp["interviewer_id"],
        "interviewee_id": candidate_id,
        "result":        "PASS"
    })
    await data_manager.save_data()

    # ---------- ④ 合格メモ送信 -------------------------------
    pass_channel = main_guild.get_channel(PASS_MEMO_CHANNEL_ID)
    if pass_channel:
        memo_text = transient_memo_cache.pop(progress_key, "")
        # 10 連続 @ 以降はカット
        m = re.search(r'@{10,}', memo_text)
        if m:
            memo_text = memo_text[:m.start()].rstrip()
        embed = discord.Embed(
            description=f"{target_member.mention}\n{memo_text}" if memo_text else target_member.mention,
            color=0x00FF00,
            timestamp=datetime.now(JST)
        )
        await pass_channel.send(embed=embed)

    # ---------- ⑤ ダッシュボード・統計更新 ---------------
    request_dashboard_update(interaction.client)
    asyncio.create_task(update_stats(interaction.client))
    
    # 候補者への合格通知DMを削除
    # try:
    #     await target_member.send(
    #         "🎉 合格おめでとうございます！\n"
    #         "このあと案内担当より手続きがありますのでお待ちください。"
    #     )
    # except Exception:
    #     pass

    await interaction.followup.send("合格処理が完了しました（チャンネル/VC は削除済み）。", ephemeral=True)
# ▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲ 修正箇所 ▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲▲



# ------------------------------------------------
# InterviewResultView（各ボタン付きView）
# ------------------------------------------------
class InterviewResultView(discord.ui.View):
    def __init__(self, progress_key: str) -> None:
        super().__init__(timeout=None)
        self.progress_key = progress_key

        pass_button = discord.ui.Button(label='合格', style=discord.ButtonStyle.success,
                                        custom_id=f'pass_button_{progress_key}')
        pass_button.callback = self.pass_button_callback
        self.add_item(pass_button)

        immediate_fail_button = discord.ui.Button(label='不合格', style=discord.ButtonStyle.red,
                                                  custom_id=f'fail_button_{progress_key}')
        immediate_fail_button.callback = self.immediate_fail_callback
        self.add_item(immediate_fail_button)

        immediate_ban_button = discord.ui.Button(label='BAN', style=discord.ButtonStyle.danger,
                                                 custom_id=f'ban_button_{progress_key}')
        immediate_ban_button.callback = self.immediate_ban_callback
        self.add_item(immediate_ban_button)

        immediate_interval_button = discord.ui.Button(label='インターバル', style=discord.ButtonStyle.secondary,
                                                      custom_id=f'interval_button_{progress_key}')
        immediate_interval_button.callback = self.immediate_interval_callback
        self.add_item(immediate_interval_button)

        delayed_fail_button = discord.ui.Button(label='[遅延]不合格', style=discord.ButtonStyle.red,
                                                custom_id=f'delayed_fail_button_{progress_key}')
        delayed_fail_button.callback = self.delayed_fail_callback
        self.add_item(delayed_fail_button)

        delayed_ban_button = discord.ui.Button(label='[遅延]BAN', style=discord.ButtonStyle.danger,
                                               custom_id=f'delayed_ban_button_{progress_key}')
        delayed_ban_button.callback = self.delayed_ban_callback
        self.add_item(delayed_ban_button)

        delayed_interval_button = discord.ui.Button(label='[遅延]インターバル', style=discord.ButtonStyle.secondary,
                                                    custom_id=f'delayed_interval_button_{progress_key}')
        delayed_interval_button.callback = self.delayed_interval_callback
        self.add_item(delayed_interval_button)

    async def pass_button_callback(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        context = await get_candidate_context(interaction, progress_key_override=self.progress_key)
        if context:
            await process_pass_action(interaction, context)
        try:
            await interaction.message.delete()
        except Exception as e:
            logger.error(f"ボタン付きメッセージ削除失敗: {e}")

    async def immediate_fail_callback(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        context = await get_candidate_context(interaction, progress_key_override=self.progress_key)
        if context:
            await process_immediate_action(interaction, context, "fail")
        try:
            await interaction.message.delete()
        except Exception as e:
            logger.error(f"ボタン付きメッセージ削除失敗: {e}")

    async def immediate_ban_callback(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        context = await get_candidate_context(interaction, progress_key_override=self.progress_key)
        if context:
            await process_immediate_action(interaction, context, "ban")
        try:
            await interaction.message.delete()
        except Exception as e:
            logger.error(f"ボタン付きメッセージ削除失敗: {e}")

    async def immediate_interval_callback(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        context = await get_candidate_context(interaction, progress_key_override=self.progress_key)
        if context:
            await process_immediate_action(interaction, context, "interval")
        try:
            await interaction.message.delete()
        except Exception as e:
            logger.error(f"ボタン付きメッセージ削除失敗: {e}")

    async def delayed_fail_callback(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        context = await get_candidate_context(interaction, progress_key_override=self.progress_key)
        if context:
            await register_delayed_action(interaction, context, "fail")
        try:
            await interaction.message.delete()
        except Exception as e:
            logger.error(f"ボタン付きメッセージ削除失敗: {e}")

    async def delayed_ban_callback(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        context = await get_candidate_context(interaction, progress_key_override=self.progress_key)
        if context:
            await register_delayed_action(interaction, context, "ban")
        try:
            await interaction.message.delete()
        except Exception as e:
            logger.error(f"ボタン付きメッセージ削除失敗: {e}")

    async def delayed_interval_callback(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        context = await get_candidate_context(interaction, progress_key_override=self.progress_key)
        if context:
            await register_delayed_action(interaction, context, "interval")
        try:
            await interaction.message.delete()
        except Exception as e:
            logger.error(f"ボタン付きメッセージ削除失敗: {e}")
# ------------------------------------------------
# VCControlView
# ------------------------------------------------
class VCControlView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label='[管理用]VC作成',
        style=discord.ButtonStyle.gray,
        custom_id='create_vc'
    )
    async def create_vc(
            self,
            interaction: discord.Interaction,
            button: discord.ui.Button
    ) -> None:
        """候補者ごとの面接 VC を作成（担当ロールも入室可能に）"""

        # --- 0) 事前準備 --------------------------------------
        await interaction.response.defer(ephemeral=True)
        context = await get_candidate_context(interaction)
        if not context:
            return

        candidate_id, cp, target_guild, target_member, \
            main_guild, interviewer, progress_key = (
            context.candidate_id, context.progress, context.target_guild,
            context.target_member, context.main_guild, context.interviewer,
            context.progress_key
        )

        guild: discord.Guild = target_guild
        channel: discord.TextChannel = interaction.channel  # VC ボタンを押した面接テキスト ch
        interviewer_role_obj = guild.get_role(INTERVIEWER_ROLE_ID)

        # --- 1) VC 用パーミッション ---------------------------
        voice_overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),

            # 候補者
            target_member: discord.PermissionOverwrite(
                view_channel=True,
                connect=True,
                speak=True,
                use_voice_activation=True
            ),

            # 面接官（押した人）
            interviewer: discord.PermissionOverwrite(
                view_channel=True,
                connect=True,
                speak=True,
                use_voice_activation=True
            ),

            # 面接担当ロール ― ★ 追加 ★
            interviewer_role_obj: discord.PermissionOverwrite(
                view_channel=True,
                connect=True,
                speak=True,
                use_voice_activation=True
            ) if interviewer_role_obj else None,

            # Bot 自身
            guild.me: discord.PermissionOverwrite(
                view_channel=True,
                connect=True,
                speak=True,
                use_voice_activation=True
            ),
        }
        # dict 内で None が入るとエラーになるので除去
        voice_overwrites = {k: v for k, v in voice_overwrites.items() if k is not None}

        # --- 2) VC 作成 ---------------------------------------
        try:
            if guild.id == MAIN_GUILD_ID and channel.category:
                vc = await guild.create_voice_channel(
                    channel.name,
                    overwrites=voice_overwrites,
                    category=channel.category
                )
            else:
                vc = await guild.create_voice_channel(
                    channel.name,
                    overwrites=voice_overwrites
                )
            logger.info(f"VC {vc.id} 作成")
        except Exception as e:
            await interaction.followup.send(f"VC作成失敗: {e}", ephemeral=True)
            return

        # --- 3) 進捗 & マッピング更新 --------------------------
        cp['voice_channel_id'] = vc.id
        prev_id = cp.get('interviewer_id')
        cp['previous_interviewer_id'] = prev_id
        cp['interviewer_id'] = interaction.user.id
        changed = prev_id != interaction.user.id
        if changed:
            cp['notified_assignment'] = False
        data_manager.interview_channel_mapping[vc.id] = progress_key
        await data_manager.save_data()

        # --- 3.5) 割り当て通知 -------------------------------
        if changed:
            await notify_interviewer_assignment(
                interaction.user,
                target_member,
                channel,
                cp,
            )

        # --- 4) UI 反映 ---------------------------------------
        update_candidate_status(cp, "担当者待ち")
        request_dashboard_update(interaction.client)
        await interaction.followup.send(f"VC {vc.mention} を作成しました。", ephemeral=True)

    @discord.ui.button(label='[管理用]VC削除', style=discord.ButtonStyle.gray, custom_id='delete_vc')
    async def delete_vc(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """
        VC削除ボタンのコールバック（堅牢性向上版）
        VCを複数の方法で探し、データも確実にクリーンアップする
        """
        # 先に応答を保留し、タイムアウトを防ぎます
        await interaction.response.defer(ephemeral=True)
        
        context = await get_candidate_context(interaction)
        if not context:
            await interaction.followup.send("候補者の情報が見つかりませんでした。", ephemeral=True)
            return

        candidate_id, cp, target_guild, target_member, main_guild, interviewer, progress_key = (
            context.candidate_id, context.progress, context.target_guild,
            context.target_member, context.main_guild, context.interviewer,
            context.progress_key
        )

        vc_to_delete: Optional[discord.VoiceChannel] = None
        vc_id_to_delete: Optional[int] = None

        # --- 探索フェーズ ---
        # 方法1: candidate_progress から探す (最優先)
        vc_id_from_cp = cp.get('voice_channel_id')
        if vc_id_from_cp:
            channel = interaction.client.get_channel(vc_id_from_cp)
            if isinstance(channel, discord.VoiceChannel):
                vc_to_delete = channel
                vc_id_to_delete = vc_id_from_cp

        # 方法2: マッピングから探す (フォールバック)
        if vc_to_delete is None:
            for channel_id, pk in data_manager.interview_channel_mapping.items():
                if pk == progress_key:
                    channel = interaction.client.get_channel(channel_id)
                    if isinstance(channel, discord.VoiceChannel):
                        vc_to_delete = channel
                        vc_id_to_delete = channel_id
                        break # 1つ見つかれば十分

        # 方法3: チャンネル名から探す (最終手段)
        if vc_to_delete is None:
            # VCはテキストチャンネルと同じ名前で作成される想定
            if isinstance(interaction.channel, discord.TextChannel):
                vc_candidate = discord.utils.get(target_guild.voice_channels, name=interaction.channel.name)
                if vc_candidate:
                    vc_to_delete = vc_candidate
                    vc_id_to_delete = vc_candidate.id


        # --- 削除フェーズ ---
        if vc_to_delete is None:
            await interaction.followup.send("VCは存在しません。", ephemeral=True)
            return

        try:
            await vc_to_delete.delete(reason=f"管理用ボタンにより {interaction.user} が削除")
            logger.info(f"VC {vc_id_to_delete} を削除しました。 (ProgressKey: {progress_key})")
        except discord.NotFound:
            logger.warning(f"VC {vc_id_to_delete} は削除しようとしたときには既に存在しませんでした。")
            # 既にないのでエラーにはせず、データクリーンアップに進む
        except Exception as e:
            logger.error(f"VC {vc_id_to_delete} の削除に失敗: {e}")
            await interaction.followup.send("VCの削除に失敗しました。", ephemeral=True)
            return

        # --- データクリーンアップフェーズ ---
        # candidate_progress から削除
        if cp.get('voice_channel_id') == vc_id_to_delete:
            cp.pop('voice_channel_id', None)

        # interview_channel_mapping から削除 (int と str 両方のキーを試す)
        if vc_id_to_delete:
            data_manager.interview_channel_mapping.pop(vc_id_to_delete, None)
            data_manager.interview_channel_mapping.pop(str(vc_id_to_delete), None)

        await data_manager.save_data()
        request_dashboard_update(interaction.client)
        await interaction.followup.send("VCを削除しました。", ephemeral=True)


    @discord.ui.button(label='[管理用]日時設定/変更',
                       style=discord.ButtonStyle.gray,
                       custom_id='schedule_interview')
    async def schedule_interview(self,
                                 interaction: discord.Interaction,
                                 button: discord.ui.Button) -> None:
        """日時モーダルを開く際に、押した面接官を担当者として再登録する"""

        context = await get_candidate_context(interaction)
        if not context:
            return

        candidate_id, cp, target_guild, target_member, main_guild, interviewer, progress_key = (
            context.candidate_id, context.progress, context.target_guild,
            context.target_member, context.main_guild, context.interviewer,
            context.progress_key
        )

        # ── **担当者を上書き** ───────────────────────────────
        prev_id = cp.get("interviewer_id")
        cp["previous_interviewer_id"] = prev_id
        cp["interviewer_id"] = interaction.user.id
        changed = prev_id != interaction.user.id
        if changed:
            cp["notified_assignment"] = False
        await data_manager.save_data()
        request_dashboard_update(interaction.client)

        # ── 担当者へDM通知 ─────────────────────────────
        if changed:
            await notify_interviewer_assignment(
                interaction.user,
                target_member,
                interaction.channel,
                cp,
            )

        # ── モーダル表示 ─────────────────────────────────
        modal = ScheduleModal(progress_key, interaction.user.id)
        await interaction.response.send_modal(modal)
        logger.info("日時設定モーダル表示（担当者更新済み）")

    @discord.ui.button(label='[管理用]開始',
                       style=discord.ButtonStyle.gray,
                       custom_id='submit_memo')
    async def submit_memo(self,
                          interaction: discord.Interaction,
                          button: discord.ui.Button) -> None:
        """面接メモ入力モーダルを開き、押した面接官を担当者にする"""

        context = await get_candidate_context(interaction)
        if not context:
            return

        candidate_id, cp, target_guild, target_member, main_guild, interviewer, progress_key = (
            context.candidate_id, context.progress, context.target_guild,
            context.target_member, context.main_guild, context.interviewer,
            context.progress_key
        )

        # ── **担当者を上書き** ───────────────────────────────
        prev_id = cp.get("interviewer_id")
        cp["previous_interviewer_id"] = prev_id
        cp["interviewer_id"] = interaction.user.id
        changed = prev_id != interaction.user.id
        if changed:
            cp["notified_assignment"] = False
        await data_manager.save_data()
        request_dashboard_update(interaction.client)

        # ── 担当者へDM通知 ------------------------------
        if changed:
            await notify_interviewer_assignment(
                interaction.user,
                target_member,
                interaction.channel,
                cp,
            )

        # ── モーダル表示 ─────────────────────────────────
        modal = MemoModal(progress_key,
                          interaction.user.id,
                          cp.get('source_guild_id', MAIN_GUILD_ID))
        await interaction.response.send_modal(modal)
        logger.info("面接メモ入力モーダル表示（担当者更新済み）")


# ------------------------------------------------
# MemoModal
# ------------------------------------------------
class MemoModal(discord.ui.Modal, title="面接メモの入力"):
    memo: discord.ui.TextInput = discord.ui.TextInput(
        label="面接メモ", style=discord.TextStyle.paragraph, required=True
    )

    def __init__(self, progress_key: str, interviewer_id: int, source_guild_id: int) -> None:
        super().__init__()
        self.progress_key = progress_key
        self.interviewer_id = interviewer_id
        self.source_guild_id = source_guild_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """
        面接メモ送信:
          1) 押した人を担当者に確定（メインサーバーのニックネームで保存）
          2) 追加メモ ch とボタン ch に Embed を送信
          3) 進捗・履歴を保存しダッシュボードを更新
        """
        global transient_memo_cache
        await interaction.response.defer(ephemeral=True)
        bot: discord.Client = interaction.client

        # ---------- コンテキスト ------------------------------
        context = await get_candidate_context(
            interaction, progress_key_override=self.progress_key
        )
        if context is None:
            return

        candidate_id, cp, target_guild, target_member, main_guild, _, _ = (
            context.candidate_id, context.progress, context.target_guild,
            context.target_member, context.main_guild, context.interviewer,
            context.progress_key
        )

        # ---------- ① 担当者を送信者に更新 ---------------------
        cp["interviewer_id"] = interaction.user.id
        update_candidate_status(cp, "面接済み")
        await data_manager.save_data()
        request_dashboard_update(bot)

        # ---------- ② Embed 生成 ------------------------------
        interviewer_name = get_main_display_name(bot, interaction.user.id)
        embed = discord.Embed(description=self.memo.value)
        embed.set_author(name=str(target_member),
                         icon_url=target_member.display_avatar.url)
        embed.set_footer(
            text=f"面接担当者: {interviewer_name}\n"
                 f"候補者ユーザーID: {candidate_id}"
        )

        channel_link: Optional[str] = None
        channel_id = cp.get("channel_id")
        if channel_id:
            ch_obj: Optional[discord.TextChannel] = bot.get_channel(channel_id)
            channel_link = ch_obj.mention if ch_obj else f"<#{channel_id}>"

        # ---------- ③ チャンネル取得 --------------------------
        additional_channel: Optional[discord.TextChannel] = main_guild.get_channel(
            ADDITIONAL_MEMO_CHANNEL_ID)
        button_channel: Optional[discord.TextChannel] = main_guild.get_channel(
            INTERVIEW_MEMO_CHANNEL_ID)

        if additional_channel is None and button_channel is None:
            await interaction.followup.send(
                "面接メモの送信先チャンネルが見つかりません。", ephemeral=True
            )
            return

        # ---------- ④ 過去 3 件リンク抽出 ---------------------
        prev_links: list[str] = []
        history = data_manager.memo_history.get(str(candidate_id), [])
        for rec in reversed(history):
            if rec["channel_id"] == ADDITIONAL_MEMO_CHANNEL_ID:
                prev_links.append(
                    make_message_link(rec["guild_id"], rec["channel_id"], rec["message_id"])
                )
                if len(prev_links) == 3:
                    break

        # ---------- ⑤ 追加メモ ch へ送信（必ず実行）------------
        memo_msg: Optional[discord.Message] = None
        if additional_channel:
            memo_msg = await additional_channel.send(embed=embed)

        # ---------- ⑥ ボタン ch へ送信（リンク付き）------------
        if button_channel:
            btn_embed = embed.copy()
            if prev_links:
                link_lines = [f"[メモ {i + 1}]({url})" for i, url in enumerate(prev_links)]
                btn_embed.add_field(
                    name="📎 過去メモ (最新 ≤ 3 件)",
                    value="\n".join(link_lines),
                    inline=False
                )
            if channel_link:
                btn_embed.add_field(
                    name="📌 面接チャンネル",
                    value=channel_link,
                    inline=False,
                )

            # ★ 追加: 過去の退出ログがあれば取得・追記
            past_leave_log_url = data_manager.left_before_interview_logs.pop(str(candidate_id), None)
            if past_leave_log_url:
                btn_embed.add_field(
                    name="⚠️ 過去の退出履歴",
                    value=f"[退出時のログ]({past_leave_log_url})",
                    inline=False
                )
                # popしたので保存
                await data_manager.save_data()

            await button_channel.send(
                embed=btn_embed,
                view=InterviewResultView(self.progress_key)
            )

        # ---------- ⑦ 応答 -----------------------------------
        await interaction.followup.send("面接メモ送信完了", ephemeral=True)

        # ---------- ⑧ 履歴保存 & 一時キャッシュ ---------------
        transient_memo_cache[self.progress_key] = self.memo.value
        if memo_msg:
            data_manager.memo_history.setdefault(str(candidate_id), []).append(
                {
                    "guild_id": main_guild.id,
                    "channel_id": memo_msg.channel.id,
                    "message_id": memo_msg.id,
                    "timestamp": get_current_time_iso(),
                    "interviewer_id": interaction.user.id,
                    "result": "未評価",
                    "memo_text": self.memo.value,
                }
            )
            await data_manager.save_data()

        logger.info("MemoModal.on_submit 完了")


# ------------------------------------------------
# ScheduleModal
# ------------------------------------------------
class ScheduleModal(discord.ui.Modal, title="面接日時の入力"):
    interview_time: discord.ui.TextInput = discord.ui.TextInput(label="面接開始時間（HHMM）", placeholder="例: 2130",
                                                                required=True)
    interview_date: discord.ui.TextInput = discord.ui.TextInput(label="面接日付（MMDD、空白なら当日）",
                                                                placeholder="例: 0131", required=False)

    def __init__(self, progress_key: str, interviewer_id: int) -> None:
        super().__init__()
        self.progress_key = progress_key
        self.interviewer_id = interviewer_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        time_str: str = self.interview_time.value.strip()
        date_str: str = self.interview_date.value.strip()
        current_year: int = datetime.now(JST).year
        if not date_str:
            date_str = datetime.now(JST).strftime("%m%d")
        datetime_str: str = f"{current_year} {date_str} {time_str}"
        try:
            dt: datetime = datetime.strptime(datetime_str, "%Y %m%d %H%M")
            dt = dt.replace(tzinfo=JST)
        except Exception:
            await interaction.response.send_message("入力形式が正しくありません。", ephemeral=True)
            return
        if dt < datetime.now(JST):
            await interaction.response.send_message("未来の日時を入力してください。", ephemeral=True)
            return
        cp: Optional[Dict[str, Any]] = data_manager.candidate_progress.get(self.progress_key)
        if cp:
            cp['interview_time'] = dt.isoformat()
            cp['previous_interviewer_id'] = cp.get('interviewer_id')
            cp['interviewer_id'] = self.interviewer_id
            cp['scheduled_time'] = get_current_time_iso()
            cp['notified_candidate'] = False
            cp['notified_interviewer'] = False
            update_candidate_status(cp, "日程調整済み")
            await data_manager.save_data()
            request_dashboard_update(interaction.client)
            interviewer_user = interaction.client.get_user(self.interviewer_id)
            candidate_user = interaction.client.get_user(cp.get('candidate_id'))
            channel_obj = interaction.client.get_channel(cp.get('channel_id'))
            if (
                interviewer_user
                and candidate_user
                and isinstance(channel_obj, discord.TextChannel)
            ):
                await notify_interviewer_schedule(
                    interviewer_user,
                    candidate_user,
                    channel_obj,
                    dt,
                )
        await interaction.response.send_message("面接日時設定完了", ephemeral=True)

# ------------------------------------------------
# 月次カウント機能（Cog）
# ------------------------------------------------
class MonthlyCountCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.monthly_counts_data: Dict[str, Dict[int, Dict[str, Any]]] = {}
        self.monthly_messages: Dict[str, Optional[discord.Message]] = {}
        self.current_year_month: Optional[str] = None
        self.load_counts_data()

        # ★ 追加: 月替わり監視ループ起動
        self._month_watch.start()

    # ★ 追加: 5 分おきに月替わりをチェック
    @tasks.loop(minutes=5)
    async def _month_watch(self) -> None:
        await self.check_monthly_reset()

    @_month_watch.before_loop
    async def _before_month_watch(self) -> None:
        await self.bot.wait_until_ready()


    def load_counts_data(self):
        if os.path.isfile(DATA_FILE):
            try:
                with open(DATA_FILE, "r", encoding="utf-8") as f:
                    raw_data = json.load(f)
                for ym, data in raw_data.items():
                    self.monthly_counts_data[ym] = {}
                    for exec_id_str, info in data.items():
                        try:
                            exec_id_int = int(exec_id_str)
                            self.monthly_counts_data[ym][exec_id_int] = {
                                "name": info.get("name", "不明な担当者"),
                                "assigned": set(info.get("assigned", []))
                            }
                        except ValueError:
                            logger.warning(f"データロード中に無効な担当者IDをスキップ: {exec_id_str} in {ym}")
                logger.info("月次カウントデータのロードに成功しました。")
            except json.JSONDecodeError as e:
                logger.error(f"月次カウントデータファイル({DATA_FILE})のJSON解析エラー: {e}")
            except Exception as e:
                logger.error(f"月次カウントデータのロード中に予期せぬエラー: {e}", exc_info=True)
        else:
            logger.info("月次カウント用の保存済みデータファイルが見つかりません。新規に開始します。")

        now = datetime.now(JST)
        ym = f"{now.year}-{now.month:02d}"
        if self.current_year_month is None:
            self.current_year_month = ym
            if ym not in self.monthly_counts_data:
                 self.monthly_counts_data[ym] = {}

    def save_counts_data(self):
        to_save = {}
        for ym, data in self.monthly_counts_data.items():
            to_save[ym] = {}
            for exec_id, info in data.items():
                to_save[ym][str(exec_id)] = {
                    "name": info.get("name", "不明な担当者"),
                    "assigned": list(info.get("assigned", set()))
                }
        try:
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(to_save, f, ensure_ascii=False, indent=4)
        except Exception as e:
            logger.error(f"月次カウントデータの保存中にエラー: {e}", exc_info=True)

    async def check_monthly_reset(self):
        now = datetime.now(JST)
        ym = f"{now.year}-{now.month:02d}"
        if self.current_year_month != ym:
            logger.info(f"月が替わりました: {self.current_year_month} -> {ym}")
            self.current_year_month = ym
            if ym not in self.monthly_counts_data:
                self.monthly_counts_data[ym] = {}
            self.monthly_messages[ym] = None

            channel = self.bot.get_channel(LOG_CHANNEL_ID)
            if channel and isinstance(channel, discord.TextChannel):
                try:
                    await channel.send(f"✨ **{ym}** の案内カウントを開始します。")
                except discord.Forbidden:
                    logger.error(f"チャンネル {LOG_CHANNEL_ID} への月替わり通知送信権限がありません。")
                except Exception as e:
                    logger.error(f"チャンネル {LOG_CHANNEL_ID} への月替わり通知送信中にエラー: {e}")
            else:
                logger.warning(
                    f"月替わり通知用のチャンネル {LOG_CHANNEL_ID} が見つからないか、テキストチャンネルではありません。")

            await self.update_log_message()
            self.save_counts_data()

            # ★ 追加: 新しい月の面接回数 Embed を生成
            await update_stats(self.bot)

    async def update_log_message(self):
        # 実装略
        pass
class GuideCountCog(commands.Cog):
    """案内回数（月次）をカウントし、進捗バーをダッシュボードに表示"""

    GUIDE_ROLE_ID = 892542047918116874     # 案内担当ロール
    CHANNEL_ID    = 1313073156256436244    # ダッシュボード出力チャンネル
    DATA_FILE     = os.path.join(BASE_DIR, "guide_counts_data.json")

    # ─────────────────────────────────────────────
    #  月替わり監視タスク（5 分おき）
    # ─────────────────────────────────────────────
    @tasks.loop(minutes=5)
    async def _month_watch(self):
        now = datetime.now(JST)
        ym  = f"{now.year}-{now.month:02d}"
        if ym != self.current_ym:
            self.current_ym = ym
            self.monthly_counts.setdefault(ym, {})
            await self._update_log_message()

    @_month_watch.before_loop
    async def _before_month_watch(self):
        await self.bot.wait_until_ready()

    # ─────────────────────────────────────────────
    #  初期化
    # ─────────────────────────────────────────────
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.monthly_counts:   dict[str, dict[int, dict[str, Any]]] = {}  # ym -> {uid: {name,count}}
        self.monthly_messages: dict[str, int] = {}                         # ym -> message_id
        self.current_ym: str | None = None

        self._load_data()                                 # counts / messages を復元
        self.bot.loop.create_task(self._send_initial())   # 起動直後にダッシュボード生成
        self._month_watch.start()                         # 監視ループ開始

    # ─────────────────────────────────────────────
    #  データロード・保存
    # ─────────────────────────────────────────────
    def _load_data(self) -> None:
        if os.path.isfile(self.DATA_FILE):
            try:
                with open(self.DATA_FILE, "r", encoding="utf-8") as fp:
                    raw = json.load(fp)

                counts_raw = raw.get("counts", {})
                self.monthly_counts = {
                    ym: {int(uid): info for uid, info in users.items()}
                    for ym, users in counts_raw.items()
                }
                self.monthly_messages = {k: int(v) for k, v in raw.get("messages", {}).items()}
                logger.info("GuideCountCog: データロード成功")
            except Exception as e:
                logger.error(f"GuideCountCog: データロード失敗: {e}")

        now = datetime.now(JST)
        ym  = f"{now.year}-{now.month:02d}"
        if self.current_ym is None:
            self.current_ym = ym
        self.monthly_counts.setdefault(self.current_ym, {})

    def _save_data(self) -> None:
        out = {
            "counts":   self.monthly_counts,
            "messages": self.monthly_messages,
        }
        try:
            with open(self.DATA_FILE, "w", encoding="utf-8") as fp:
                json.dump(out, fp, ensure_ascii=False, indent=4)
            logger.info("GuideCountCog: データ保存成功")
        except Exception as e:
            logger.error(f"GuideCountCog: データ保存失敗: {e}")

    # 旧コード互換（on_member_update / adjust_guide_count から呼ばれる）
    def save_counts_data(self) -> None:
        self._save_data()

    # ─────────────────────────────────────────────
    #  起動後 1 回だけ呼ぶ
    # ─────────────────────────────────────────────
    async def _send_initial(self) -> None:
        await self.bot.wait_until_ready()
        await self._update_log_message()

    # ─────────────────────────────────────────────
    #  ダッシュボード更新（Embed 送信 / 更新 / 重複掃除）
    # ─────────────────────────────────────────────

    async def _update_log_message(self) -> None:
        ym   = self.current_ym
        data = self.monthly_counts.setdefault(ym, {})

        # 0 回のメンバーも入れる
        guild = self.bot.get_guild(MAIN_GUILD_ID)
        if guild:
            role = guild.get_role(self.GUIDE_ROLE_ID)
            if role:
                for m in role.members:
                    data.setdefault(m.id, {"name": m.display_name, "count": 0})

        # ---- Embed 作成 ----
        items = sorted(data.items(), key=lambda kv: (-kv[1]["count"], kv[1]["name"]))
        embed = discord.Embed(
            title       = f"案内回数 {ym}",
            description = f"進捗バー（目標 **{MONTHLY_GOAL} 回**）",
            color       = 0x1abc9c,
            timestamp   = datetime.now(JST),
        )
        for uid, info in items:
            bar = generate_custom_progress_bar(info["count"], MONTHLY_GOAL)
            embed.add_field(name=info["name"], value=bar, inline=False)

        channel = self.bot.get_channel(self.CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            return

        # ---- 既存メッセージ探索 ----
        msg_id  = self.monthly_messages.get(ym)
        target: Optional[discord.Message] = None

        if msg_id:
            try:
                target = await channel.fetch_message(msg_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                logger.warning(f"GuideCountCog: メッセージ取得に失敗: {e}")
                target = None

        if target is None:
            try:
                async for hist in channel.history(limit=None):
                    if hist.author.id == self.bot.user.id and hist.embeds:
                        if (hist.embeds[0].title or "").startswith(f"案内回数 {ym}"):
                            target = hist
                            break
            except (discord.Forbidden, discord.HTTPException) as e:
                logger.warning(f"GuideCountCog: 履歴取得に失敗: {e}")
                target = None

        # ---- 送信 or 更新 ----
        if target:
            await target.edit(embed=embed)
            new_msg = target
        else:
            new_msg = await channel.send(embed=embed)

        # ---- 重複掃除（同タイトル & Bot 投稿） ----
        async for hist in channel.history(limit=None):
            if hist.id == new_msg.id:
                continue
            if hist.author.id == self.bot.user.id and hist.embeds:
                if (hist.embeds[0].title or "").startswith(f"案内回数 {ym}"):
                    try:
                        await hist.delete()
                    except discord.HTTPException:
                        pass

        # ---- ID 保存 ----
        self.monthly_messages[ym] = new_msg.id
        self._save_data()
        logger.info(f"GuideCountCog: ダッシュボード更新完了 ({ym})  msg={new_msg.id}")

    # ─────────────────────────────────────────────
    #  SPECIFIC_ROLE_ID 付与 ⇒ 案内カウント +1
    # ─────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.roles == after.roles:
            return

        specific = after.guild.get_role(SPECIFIC_ROLE_ID)
        guide    = after.guild.get_role(self.GUIDE_ROLE_ID)
        if not (specific and guide):
            return

        # SPECIFIC_ROLE_ID が “新規” に付与されたか？
        if specific in after.roles and specific not in before.roles:
            # 付与者（AuditLog）を取得
            assigner: Optional[discord.Member] = None
            async for entry in after.guild.audit_logs(limit=10, action=AuditLogAction.member_role_update):
                if entry.target.id != after.id:
                    continue
                b_ids = [r.id for r in getattr(entry.before, "roles", [])]
                a_ids = [r.id for r in getattr(entry.after,  "roles", [])]
                if specific.id in a_ids and specific.id not in b_ids:
                    if isinstance(entry.user, discord.Member):
                        assigner = entry.user
                    else:
                        assigner = after.guild.get_member(entry.user.id)
                    break

            if not assigner or guide not in assigner.roles:
                logger.debug("GuideCountCog: assigner not found or lacks guide role")
                return

            ym   = self.current_ym
            buck = self.monthly_counts.setdefault(ym, {})
            info = buck.setdefault(assigner.id, {"name": assigner.display_name, "count": 0})
            info["count"] += 1

            self._save_data()
            await self._update_log_message()

    # ─────────────────────────────────────────────
    #  /adjust_guide_count  （手動調整コマンド）
    # ─────────────────────────────────────────────
    @app_commands.command(
        name        = "adjust_guide_count",
        description = "案内回数を手動で調整します（add/sub/set）",
    )
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(
        guide = "対象の案内担当者（メンバー）",
        count = "調整値 (0 以上の整数)",
        mode  = "操作モード: add=加算 / sub=減算 / set=上書き",
        month = "対象月 (YYYY-MM、省略で当月)",
    )
    async def adjust_guide_count(
        self,
        interaction: discord.Interaction,
        guide: discord.Member,
        count: app_commands.Range[int, 0],
        mode: str = "add",
        month: str | None = None,
    ):
        await interaction.response.defer(ephemeral=True)

        mode = mode.lower()
        if mode not in ("add", "sub", "set"):
            await interaction.followup.send("mode は **add / sub / set** のいずれかで指定してください。", ephemeral=True)
            return

        # 対象年月決定
        if month:
            try:
                dt_target = datetime.strptime(month, "%Y-%m").replace(tzinfo=JST, day=1, hour=0, minute=0, second=0)
            except ValueError:
                await interaction.followup.send("month は **YYYY-MM** 形式で指定してください。", ephemeral=True)
                return
        else:
            now = datetime.now(JST)
            dt_target = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        ym_key = f"{dt_target.year}-{dt_target.month:02d}"

        bucket = self.monthly_counts.setdefault(ym_key, {})
        entry  = bucket.setdefault(guide.id, {"name": guide.display_name, "count": 0})
        current = entry["count"]

        if mode == "add":
            new_count = current + count
        elif mode == "sub":
            new_count = max(current - count, 0)
        else:        # set
            new_count = count

        entry["count"] = new_count
        entry["name"]  = guide.display_name

        self._save_data()
        await self._update_log_message()

        await interaction.followup.send(
            f"{ym_key} の {guide.mention} の案内回数を **{mode} {count}** して **{new_count} 回** にしました。",
            ephemeral=True,
        )


# ------------------------------------------------
# EventCog（参加・退出・チャンネル削除などのイベントハンドラ）
#   ✅ 参加制御ポリシーを追加
#      1. 先にメインに入った人 → サブからキック
#      2. サブ → 先に入った人は別サブへ入れない
#      3. メインで EXEMPT_ROLE_ID を持つ人はサブ許可
# ------------------------------------------------
class EventCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ---------- 内部ヘルパ ----------

    async def _should_kick_sub_join(
        self,
        member: discord.Member,
        sub_guild: discord.Guild,
    ) -> tuple[bool, str]:
        """
        サブサーバー参加可否を判定
        Returns
        -------
        (kick?, reason)
        """
        # 1️⃣ すでにメインサーバーに在籍しているか？
        main_guild = self.bot.get_guild(MAIN_GUILD_ID)
        if main_guild:
            main_member = main_guild.get_member(member.id)
            if main_member:
                # ― メイン在籍者は基本 NG ―
                exempt_role = main_guild.get_role(EXEMPT_ROLE_ID)
                if exempt_role and exempt_role in main_member.roles:
                    # ✨ 例外ロール保持者 → 許可
                    return False, ""
                return True, "メインサーバー在籍者はサブサーバーに参加できません"

        # 2️⃣ 他のサブサーバーに在籍していないか？
        for g in self.bot.guilds:
            if g.id in (MAIN_GUILD_ID, sub_guild.id):
                continue
            if g.get_member(member.id):
                return True, "既に別のサブサーバーに在籍しているため参加できません"

        # ✅ どちらにも該当しなければ参加可
        return False, ""

    # ---------- 参加イベント ----------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """
        ・BAN / INTERVAL の即時キック
        ・メイン/サブ参加ポリシーの強制
        ・新規候補者チャンネル作成
        """
        # ------------- 0) BAN / INTERVAL チェック (サーバー問わず最優先で実行) -------------
        ban_record = ban_manager.check_ban(member.id)
        if ban_record:
            try:
                reason = f"{ban_record.get('ban_type', '不明な種別').upper()}により入室不可"
                await member.guild.kick(member, reason=reason)
                await log_auto_kick(self.bot, member, member.guild, reason)
                logger.info(f"自動キック: {member.id} @ {member.guild.id} ({reason})")
            except Exception as e:
                logger.error(f"BAN/インターバルキック失敗: {e}")
            return  # ★重要: キックしたら以降の処理（チャンネル作成など）は行わない

        # ------------- 1) 参加制御ポリシー (サブサーバーのみ) --------------------
        if member.guild.id != MAIN_GUILD_ID:
            kick, reason = await self._should_kick_sub_join(member, member.guild)
            if kick:
                try:
                    await member.guild.kick(member, reason=reason)
                    await log_auto_kick(self.bot, member, member.guild, reason)
                    logger.info(f"自動キック: {member.id} @ {member.guild.id}  ({reason})")
                except Exception as e:
                    logger.error(f"ポリシーキック失敗: {e}")
                return  # キックしたら処理終了

        # ------------- 2) ここから先は既存処理 -----------------
        guild: discord.Guild = member.guild
        channel_name: str = f"面接部屋-{member.display_name}"
        is_main_guild: bool = (guild.id == MAIN_GUILD_ID)
        interviewer_role: Optional[discord.Role] = get_interviewer_role(guild)

        # --- 面接テキストチャンネル作成 ---
        text_overwrites: Dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        # 面接官ロールが取得できた場合のみ、そのロールに権限を付与
        if interviewer_role:
            text_overwrites[interviewer_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

        try:
            category: Optional[discord.CategoryChannel] = (
                guild.get_channel(MAIN_CATEGORY_ID) if is_main_guild else None
            )
            interview_channel: discord.TextChannel = await guild.create_text_channel(
                channel_name, overwrites=text_overwrites, category=category
            )
        except Exception as e:
            logger.error(f"面接チャンネル作成失敗: {e}")
            return

        progress_key = make_progress_key(guild.id, member.id)
        data_manager.interview_channel_mapping[interview_channel.id] = progress_key

        data_manager.candidate_progress[progress_key] = {
            'candidate_id': member.id,
            'status': "プロフィール未記入",
            'channel_id': interview_channel.id,
            'source_guild_id': guild.id,
            'timestamp': get_current_time_iso(),
            'interview_time': None,
            'interviewer_id': None,
            'previous_interviewer_id': None,
            'join_time': get_current_time_iso(),
            'profile_filled_time': None,
            'scheduled_time': None,
            'notified_candidate': False,
            'notified_interviewer': False,
            'notified_assignment': False,
            'notify_time': None,
            'failed': False,
            'profile_message_id': None,
            'pending_inrate_confirmation': False,
            'pending_move_confirmation': False, # ★追加: 整合性のため初期化
        }
        await data_manager.save_data()
        request_dashboard_update(self.bot)

        await interview_channel.send(content='\u200b', view=VCControlView())
        await interview_channel.send(f"{member.mention} さん、当会議に面接にお越しいただき、ありがとうございます✨")

        # --- プロフィールフォーム転送（既存ロジック） ---
        source_channel: Optional[discord.TextChannel] = self.bot.get_channel(PROFILE_FORM_CHANNEL_ID)
        if isinstance(source_channel, discord.TextChannel):
            try:
                async for msg in source_channel.history(limit=2, oldest_first=True):
                    await interview_channel.send(msg.content)
            except Exception as e:
                logger.error(f"プロフィールフォーム送信失敗: {e}")
        else:
            logger.warning("プロフィールフォームチャンネルが見つかりません。")

    # ---------- チャンネル削除 / 退出イベントなど（既存実装は変更なし） ----------
    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        data_manager.interview_channel_mapping.pop(channel.id, None)
        for progress_key, cp in list(data_manager.candidate_progress.items()):
            if cp.get('channel_id') == channel.id:
                data_manager.candidate_progress.pop(progress_key, None)
                await data_manager.save_data()
                request_dashboard_update(self.bot)
                logger.info(f"候補者 {progress_key} の進捗削除 (チャンネル削除)")

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        """
        メンバーがサーバーから退出した際のイベントハンドラ
        - 自己退出時にログを残す (メインサーバーのみ)
        - 担当者がいる候補者の場合にDMで通知する
        - 関連するチャンネルやデータをクリーンアップする
        """
        progress_key = make_progress_key(member.guild.id, member.id)
        cp = data_manager.candidate_progress.get(progress_key)

        if not cp:
            return

        # --- 自己退出ログ記録 (メインサーバーのみ) ---
        if member.guild.id == MAIN_GUILD_ID:
            # 完了済みのステータスは無視
            status = cp.get("status", "").upper()
            if not status.startswith(("PASS", "FAIL", "BAN", "INTERVAL", "DELAY_", "案内待ち")):
                # 監査ログをチェックして、キックでないことを確認
                is_kick = False
                try:
                    latest_kick_log = await member.guild.audit_logs(action=discord.AuditLogAction.kick, limit=1).flatten()
                    if latest_kick_log:
                        entry = latest_kick_log[0]
                        now_utc = datetime.now(timezone.utc)
                        if entry.target and entry.target.id == member.id and (now_utc - entry.created_at).total_seconds() < 5:
                            is_kick = True
                            logger.info(f"ユーザー {member.id} はキックされたため、退出ログの記録をスキップします。")
                except discord.Forbidden:
                    logger.warning("監査ログの読み取り権限がありません。キックと自己退出の判別ができません。")
                except Exception as e:
                    logger.error(f"監査ログの取得中にエラーが発生しました: {e}")

                if not is_kick:
                    log_channel = self.bot.get_channel(ADDITIONAL_MEMO_CHANNEL_ID)
                    if log_channel and isinstance(log_channel, discord.TextChannel):
                        logger.info(f"面接前に候補者 {member.id} が退出しました。ログを記録します。")
                        profile_text = "プロフィール未提出または取得失敗"
                        profile_message_id = cp.get("profile_message_id")
                        channel_id = cp.get("channel_id")
                        if profile_message_id and channel_id:
                            try:
                                candidate_channel = self.bot.get_channel(channel_id)
                                if isinstance(candidate_channel, discord.TextChannel):
                                    profile_message = await candidate_channel.fetch_message(profile_message_id)
                                    profile_text = profile_message.content
                            except discord.NotFound:
                                profile_text = "プロフィールのメッセージが見つかりませんでした。"
                            except Exception as e:
                                logger.error(f"プロフィールメッセージの取得に失敗: {e}")

                        embed = discord.Embed(
                            title="候補者退出（面接前）",
                            description=f"候補者が面接プロセス中にサーバーから退出しました。",
                            color=0xffa500, # Orange
                            timestamp=datetime.now(JST)
                        )
                        embed.add_field(name="候補者", value=f"{member.display_name} (`{member.name}`)", inline=False)
                        embed.add_field(name="ユーザーID", value=str(member.id), inline=False)
                        embed.add_field(name="退出時のステータス", value=cp.get("status", "不明"), inline=False)
                        if len(profile_text) > 1024:
                            profile_text = profile_text[:1021] + "..."
                        embed.add_field(name="プロフィール", value=f"```\n{profile_text}\n```", inline=False)
                        embed.set_footer(text="自動記録")
                        try:
                            sent_message = await log_channel.send(embed=embed)
                            data_manager.left_before_interview_logs[str(member.id)] = sent_message.jump_url
                            await data_manager.save_data()
                        except Exception as e:
                            logger.error(f"退出ログの送信または保存に失敗: {e}")

        logger.info(f"候補者 '{member.display_name}' ({member.id}) がサーバーから退出しました。クリーンアップ処理を開始します。")

        # --- 面接が完了していたかどうかの判定ロジックを整理 ---
        completed_statuses = {
            'PASS_NOTIFIED', 'DELAY_PASS', 'FAIL_NOTIFIED', 'DELAY_FAIL',
            'BAN_NOTIFIED', 'DELAY_BAN', 'INTERVAL_NOTIFIED', 'DELAY_INTERVAL',
            '面接済み'  # このステータスも完了とみなす
        }

        # interview_records内のIDは文字列の可能性があるため str() で比較
        has_interview_record = any(
            str(r.get("candidate_id")) == str(member.id)
            for r in data_manager.interview_records
        )
        
        # 「完了ステータス」または「面接記録あり」のいずれかで完了とみなす
        is_interview_completed = (cp.get("status") in completed_statuses) or has_interview_record
        
        # --- 担当者への通知 ---
        interviewer_id = cp.get("interviewer_id")
        
        # ★ 修正点2: 通知条件を「担当者がいて、かつ面接未完了」に簡素化 ★
        if interviewer_id and not is_interview_completed:
            try:
                interviewer = await self.bot.fetch_user(interviewer_id)
                if interviewer:
                    await interviewer.send(
                        f"【担当候補者 サーバー退出のお知らせ】\n\n"
                        f"担当されていた候補者「{member.display_name}」さんがサーバーから退出しました。\n"
                    )
                    logger.info(f"候補者 {member.id} の退出を担当者 {interviewer_id} にDMで通知しました。")
            except discord.NotFound:
                logger.warning(f"担当者ID {interviewer_id} が見つからず、退出DMを送信できませんでした。")
            except discord.Forbidden:
                logger.warning(f"担当者 {interviewer_id} へのDMがブロックされており、退出DMを送信できませんでした。")
            except Exception as e:
                logger.error(f"担当者への退出通知DM送信中に予期せぬエラーが発生しました: {e}", exc_info=True)
        else:
            # (改善) 通知しなかった場合に理由をログ出力し、デバッグしやすくする
            if interviewer_id:
                reason = "面接完了済みのため"
            else:
                reason = "担当者が未設定のため"
            logger.info(f"候補者 {member.id} の退出通知は送信しませんでした。理由: {reason}")

        # --- 候補者データのクリーンアップ処理 ---
        try:
            await delete_candidate_channels(self.bot, member.guild, member.id)
        except Exception as e:
            logger.error(f"退出した候補者 {member.id} のチャンネル削除中にエラーが発生しました: {e}", exc_info=True)

        if progress_key in data_manager.candidate_progress:
            del data_manager.candidate_progress[progress_key]
            await data_manager.save_data()
            logger.info(f"退出した候補者 {member.id} の進捗データを削除しました。")
            request_dashboard_update(self.bot)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        """特定ロール付与時に候補者データをクリーンアップ"""
        if before.roles == after.roles:
            return

        role_added = any(r.id == EXEMPT_ROLE_ID for r in after.roles) and all(
            r.id != EXEMPT_ROLE_ID for r in before.roles
        )
        if not role_added:
            return

        progress_key = make_progress_key(after.guild.id, after.id)
        if progress_key not in data_manager.candidate_progress:
            return

        try:
            await delete_candidate_channels(self.bot, after.guild, after.id)
        except Exception as e:
            logger.error(f"ロール付与によるチャンネル削除失敗: {e}")

        data_manager.candidate_progress.pop(progress_key, None)
        await data_manager.save_data()
        request_dashboard_update(self.bot)
        logger.info(
            f"ロール {EXEMPT_ROLE_ID} 付与により候補者 {after.id} の進捗を削除しました。"
        )
# ------------------------------------------------
# TaskCog（定期タスク）
# ------------------------------------------------
class TaskCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.check_candidate_status.start()
        self.schedule_notifications.start()

    @tasks.loop(minutes=5)
    async def check_candidate_status(self) -> None:
        now: datetime = datetime.now(JST)
        for progress_key, cp in list(data_manager.candidate_progress.items()):
            # 候補者が既にサーバーから退出していないか確認
            candidate_id = cp.get("candidate_id")
            guild_id = cp.get("source_guild_id", MAIN_GUILD_ID)
            guild = self.bot.get_guild(guild_id)  # type: ignore
            if not guild:
                continue
            if guild.get_member(candidate_id) is None:
                try:
                    await delete_candidate_channels(self.bot, guild, candidate_id)
                except Exception as e:
                    logger.error(f"退出候補者 {candidate_id} のチャンネル削除失敗: {e}")
                data_manager.candidate_progress.pop(progress_key, None)
                await data_manager.save_data()
                request_dashboard_update(self.bot)
                logger.info(f"候補者 {candidate_id} が既に退出していたため進捗を削除")
                continue
            # ★ 変更点: AI評価が一度でも行われていれば、自動リマインド・キックをスキップ
            if cp.get("profile_evaluated", False):
                continue

            if cp.get("status") != "プロフィール未記入":
                continue

            join_time_str: Optional[str] = cp.get('join_time')
            if not join_time_str:
                continue
            try:
                join_time: datetime = datetime.fromisoformat(join_time_str)
            except Exception as e:
                logger.error(f"join_time チェック失敗: {e}")
                continue

            channel = await ensure_channel_exists(self.bot, progress_key, cp) # type: ignore
            if channel is None:
                continue

            candidate: Optional[discord.User] = self.bot.get_user(candidate_id) # type: ignore
            if candidate is None:
                continue

            # 6 時間後リマインド
            if now - join_time >= timedelta(hours=6) and not cp.get("profile_notification_6h_sent", False):
                await channel.send(f"{candidate.mention} プロフィール記入をお願いします。")
                cp["profile_notification_6h_sent"] = True
                await data_manager.save_data()

            # 24 時間後警告
            if now - join_time >= timedelta(hours=24) and not cp.get("profile_warning_24h_sent", False):
                await channel.send(
                    f"{candidate.mention} 本日中にプロフィール記入がされない場合はキックとなります。"
                )
                cp["profile_warning_24h_sent"] = True
                await data_manager.save_data()

            # 36 時間後キック
            if now - join_time >= timedelta(hours=36):
                guild_id = cp.get("source_guild_id", MAIN_GUILD_ID)
                guild = self.bot.get_guild(guild_id) # type: ignore
                if guild:
                    member_obj = guild.get_member(candidate_id)
                    if member_obj:
                        try:
                            await guild.kick(member_obj, reason="プロフィール未記入による自動キック")
                            logger.info(f"候補者 {candidate_id} を自動キックしました。")
                            await log_auto_kick(self.bot, member_obj, guild, "プロフィール未記入による自動キック") # type: ignore
                            data_manager.candidate_progress.pop(progress_key, None)
                            await data_manager.save_data()
                        except Exception as e:
                            logger.error(f"自動キック失敗: {e}")
    # schedule_notifications は変更なし
    @tasks.loop(minutes=1)
    async def schedule_notifications(self) -> None:
        now: datetime = datetime.now(JST)
        for progress_key, cp in list(data_manager.candidate_progress.items()):
            candidate_id = cp.get('candidate_id')
            member: Optional[discord.User] = self.bot.get_user(candidate_id) # type: ignore
            if not member:
                continue
            channel = await ensure_channel_exists(self.bot, progress_key, cp) # type: ignore
            if channel is None:
                continue

            if cp['status'] in ["日程調整済み", "面接済み"]:
                interview_time: Optional[str] = cp.get('interview_time')
                interviewer_id: Optional[int] = cp.get('interviewer_id')
                scheduled_time: Optional[str] = cp.get('scheduled_time')
                if not interview_time or not interviewer_id or not scheduled_time:
                    continue
                try:
                    it: datetime = datetime.fromisoformat(interview_time)
                    st: datetime = datetime.fromisoformat(scheduled_time)
                except Exception as e:
                    logger.error(f"日時解析失敗: {e}")
                    continue
                if it <= now:
                    continue
                if (it - now) <= timedelta(hours=1) and not cp.get('notified_candidate') and (it - st) >= timedelta(hours=1):
                    await channel.send(f"{member.mention} 面接開始1時間前です。")
                    cp['notified_candidate'] = True
                    await data_manager.save_data()
                if (it - now) <= timedelta(minutes=10) and not cp.get('notified_interviewer'):
                    interviewer: Optional[discord.User] = self.bot.get_user(interviewer_id) # type: ignore
                    if interviewer:
                        try:
                            await interviewer.send(f"面接開始10分前です。候補者: {member.mention}")
                            cp['notified_interviewer'] = True
                            await data_manager.save_data()
                        except Exception as e:
                            logger.error(f"10分前リマインド失敗: {e}")

                if now >= it + timedelta(minutes=1): # 面接予定時刻を過ぎたらリマインドフラグをリセット
                    cp['notified_candidate'] = False
                    cp['notified_interviewer'] = False
                    await data_manager.save_data()
# ------------------------------------------------
# DelayedActionManager（遅延アクション管理）
# ------------------------------------------------
class DelayedActionManager:

    """
    ・JSON ファイルへの “追記ではなく全量書き込み” を常に行い、クラッシュ時の
      破損を防止するためにテンポラリ → アトミック rename 方式を採用
    ・ asyncio.Lock で同時アクセスを直列化
    ・ BASE_DIR 配下に保存して絶対パス問題を解消
    """
    _TMP_SUFFIX = ".tmp"

    def __init__(self, loop: asyncio.AbstractEventLoop, file_name: str = "delayed_actions.json") -> None:
        self._loop = loop
        self._lock = asyncio.Lock()
        self.file_path = os.path.join(BASE_DIR, file_name)          # ★ ← BASE_DIR を使用
        self.actions: list[dict[str, Any]] = []                     # = [{id, action_type, …}]
        self._load()

    # ---------- Public API ----------

    async def add(self, action: dict) -> None:
        """
        action 例:
            {
              "id": "<uuid>",
              "action_type": "ban" | "fail" | "interval",
              "candidate_id": 123,
              "scheduled_time": "<ISO8601>",
              "apply_all": True/False,
              "guild_id": 999 (optional)
            }
        """
        async with self._lock:
            self.actions.append(action)
            await self._save()

    async def remove(self, action_id: str) -> None:
        async with self._lock:
            self.actions = [a for a in self.actions if a.get("id") != action_id]
            await self._save()

    async def pop_due(self) -> list[dict]:
        """現在時刻までに到達したアクションを返して同時にキューから除去"""
        now = datetime.now(JST)
        async with self._lock:
            due, remain = [], []
            for a in self.actions:
                try:
                    if datetime.fromisoformat(a["scheduled_time"]) <= now:
                        due.append(a)
                    else:
                        remain.append(a)
                except Exception as e:
                    logger.error(f"[DelayedAction] 日時解析失敗: {e}")
                    remain.append(a)                 # 壊れていても落とさない
            self.actions = remain
            if due:
                await self._save()
            return due

    # ---------- Private ----------

    def _load(self) -> None:
        if not os.path.isfile(self.file_path):
            return
        try:
            with open(self.file_path, "r", encoding="utf-8") as fp:
                self.actions = json.load(fp) or []
            logger.info(f"[DelayedAction] ロード完了 ({len(self.actions)} 件)")
        except Exception as e:
            logger.error(f"[DelayedAction] ロード失敗: {e}")
            self.actions = []

    async def _save(self) -> None:
        """テンポラリへ書き込んでからアトミック rename"""
        tmp = self.file_path + self._TMP_SUFFIX
        try:
            async with aiofiles.open(tmp, "w", encoding="utf-8") as fp:   # type: ignore
                await fp.write(json.dumps(self.actions, ensure_ascii=False, indent=4))
            os.replace(tmp, self.file_path)
        except Exception as e:
            logger.error(f"[DelayedAction] 保存失敗: {e}")
            # tmp が残っていたら掃除
            with contextlib.suppress(FileNotFoundError):
                os.remove(tmp)

async def execute_delayed_action(action: dict, bot: commands.Bot):
    candidate_id = action["candidate_id"]
    action_type = action["action_type"]
    progress_key = action.get("progress_key")

    if action_type == "ban":
        reason = "BAN (遅延キック)"
    elif action_type == "fail":
        reason = "面接不合格 (遅延キック)"
    elif action_type == "interval":
        reason = "インターバル (遅延キック)"
    else:
        logger.error(f"不明なアクション種別: {action_type}")
        return

    apply_all = action.get("apply_all", False)
    guilds_to_process = bot.guilds if apply_all else [bot.get_guild(action.get("guild_id"))]

    for guild in guilds_to_process:
        if not guild:
            continue
        try:
            # 1. チャンネル削除
            await delete_candidate_channels(bot, guild, candidate_id)

            # 2. キック
            member = guild.get_member(candidate_id) or await guild.fetch_member(candidate_id)
            if member:
                await guild.kick(member, reason=reason)
                logger.info(f"Guild {guild.id} で候補者 {candidate_id} に対して {action_type} 遅延処理実行")
                await log_auto_kick(bot, member, guild, reason)
        except Exception as e:
            logger.error(f"Guild {guild.id} での遅延処理失敗（候補者: {candidate_id}）: {e}")

    # 3. 進捗データ削除
    if progress_key and progress_key in data_manager.candidate_progress:
        del data_manager.candidate_progress[progress_key]
        await data_manager.save_data()
        request_dashboard_update(bot)
        logger.info(f"遅延処理完了に伴い、候補者 {candidate_id} の進捗データを削除しました。")

# ------------------------------------------------
# DelayedActionCog（遅延アクションの実行）
# ------------------------------------------------
class DelayedActionCog(commands.Cog):
    """30 秒ごとにキューを監視し、BOT 起動直後にも 1 回だけ即実行"""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._loop = bot.loop
        self._manager = delayed_action_manager      # グローバルをそのまま再利用
        self._watcher.start()                       # tasks.loop デコレータで定義

    async def cog_unload(self) -> None:
        self._watcher.cancel()

    # ---------- 起動直後ワンショット ----------
    async def _initial_run(self) -> None:
        await self.bot.wait_until_ready()
        await self._execute_due_actions()

    # ---------- 周期タスク ----------
    @tasks.loop(seconds=30)
    async def _watcher(self) -> None:
        await self._execute_due_actions()

    async def _execute_due_actions(self) -> None:

        due = await self._manager.pop_due()
        if not due:
            return
        for act in due:
            try:
                await execute_delayed_action(act, self.bot)
            except Exception:                        # 1 件失敗しても他を止めない
                logger.exception("[DelayedAction] 実行中に例外")

    # ---------- ループ開始前 ----------
    @_watcher.before_loop
    async def _before(self):
        # 起動直後の取りこぼし防止
        self.bot.loop.create_task(self._initial_run())
        await self.bot.wait_until_ready()

# ------------------------------------------------
# MessageCog  ―  投稿・編集イベント (★要望箇所★)
# ------------------------------------------------
# 修正点: 重複していた空のクラス定義を削除
class MessageCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ===== 共通処理: プロフィール評価 ===============================
    async def _process_profile(
        self,
        message: discord.Message,
        cp: Dict[str, Any],
        progress_key: str,
        *,
        inrate_cleared: bool = False,
        move_cleared: bool = False,
    ):
        """
        プロフィール本文を評価し、結果を反映する共通ヘルパー
        - message: 評価対象のメッセージオブジェクト
        - cp: 候補者の進捗データ
        - progress_key: 進捗データのキー
        - inrate_cleared: (特例) イン率が口頭で確認済みか
        - move_cleared: (特例) 移住意思が口頭で確認済みか
        """
        # 評価が行われたことを記録し、自動キック対象から外す
        cp["profile_evaluated"] = True
        # 評価対象のメッセージIDを保存
        cp["profile_message_id"] = message.id

        # ★変更点1: 評価前のステータスを保持
        previous_status = cp.get("status")

        ok, fb = await evaluate_profile_with_ai(
            message.content,
            debug=True,
            inrate_cleared=inrate_cleared,
            move_cleared=move_cleared,
        )

        # ----------- OK -----------
        if ok:
            update_candidate_status(cp, "記入済み")
            cp["profile_filled_time"] = get_current_time_iso()
            # 確認フローはすべて完了
            cp["pending_inrate_confirmation"] = False
            cp["pending_move_confirmation"] = False

            # ★変更点2: ステータスが「未記入」または「要修正」から「記入済み」に変わった初回のみ通知
            if previous_status in ("プロフィール未記入", "要修正"):
                await message.reply("プロフィールありがとうございます。面接官が確認次第ご連絡します。")

                # 面接官通知と自動推薦も、この初回完了時にのみ実行するのが自然
                if 7 <= datetime.now(JST).hour < 23:
                    ch = self.bot.get_channel(INTERVIEWER_REMIND_CHANNEL_ID)
                    if isinstance(ch, discord.TextChannel):
                        await send_interviewer_notification(self.bot, ch, message.channel)

                try:
                    await send_recommendation_dm(self.bot, message.channel, cp)
                except Exception:
                    logger.exception("send_recommendation_dm で例外発生")

        # ----------- NG / 要確認 -----------
        else:
            await message.reply(fb)

            # AIのフィードバック内容に応じて、確認待ちステータスを更新
            if "週3日以上" in fb:
                cp["pending_inrate_confirmation"] = True
                update_candidate_status(cp, "プロフィール未記入") # 確認が終わるまで未記入扱い
            elif "半年以内に日本へ移住予定はございますか" in fb:
                cp["pending_move_confirmation"] = True
                update_candidate_status(cp, "プロフィール未記入") # 確認が終わるまで未記入扱い
            else:
                # 上記以外の不備の場合は、確認フローには入らない
                cp["pending_inrate_confirmation"] = False
                cp["pending_move_confirmation"] = False
                update_candidate_status(cp, "要修正")

        # 変更を保存し、ダッシュボードを更新
        await data_manager.save_data()
        request_dashboard_update(self.bot)

        # ===== on_message: 新規投稿の処理 (★このメソッドを置き換えてください★) ==============================
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return

        await self.bot.process_commands(message)

        progress_key = data_manager.interview_channel_mapping.get(message.channel.id)
        if not progress_key:
            return

        cp = data_manager.candidate_progress.get(progress_key)
        if not cp:
            return

        # --- A) メッセージ送信者が候補者本人の場合 ---
        if cp.get("candidate_id") == message.author.id:
            # 既存の候補者からのメッセージ（プロフィール投稿、質問など）を処理するロジック
            # ──────────────────────────────────────────────
            # A-1. イン率確認フェーズへの返答
            # ──────────────────────────────────────────────
            if cp.get("pending_inrate_confirmation"):
                if looks_like_profile(message.content):
                    cp["pending_inrate_confirmation"] = False
                    await self._process_profile(message, cp, progress_key)
                    request_dashboard_update(self.bot)
                    return

                yn_inrate = await classify_yes_no_ai(message.content, debug=True)
                if yn_inrate == "YES":
                    cp["pending_inrate_confirmation"] = False
                    if cp.get("profile_message_id"):
                        try:
                            orig_profile_msg = await message.channel.fetch_message(cp["profile_message_id"])
                            await self._process_profile(orig_profile_msg, cp, progress_key, inrate_cleared=True)
                        except discord.NotFound:
                            await message.reply("元のプロフィールが見つかりませんでした。お手数ですが、再度プロフィール全体を投稿してください。")
                            update_candidate_status(cp, "プロフィール未記入")
                            await data_manager.save_data()
                    else:
                        await message.reply("イン率について確認いたしました。お手数ですが、再度プロフィール全体をご投稿ください。")
                        update_candidate_status(cp, "プロフィール未記入")
                        await data_manager.save_data()
                elif yn_inrate == "NO":
                    await message.reply("承知いたしました。今回はお見送りとさせていただきます。")
                else:
                    await message.reply("恐れ入ります、イン率については **はい** / **いいえ** でお答えいただけますか？")
                request_dashboard_update(self.bot)
                return

            # ──────────────────────────────────────────────
            # A-2. 移住予定確認フェーズへの返答
            # ──────────────────────────────────────────────
            if cp.get("pending_move_confirmation"):
                if looks_like_profile(message.content):
                    cp["pending_move_confirmation"] = False
                    await self._process_profile(message, cp, progress_key)
                    request_dashboard_update(self.bot)
                    return

                yn_move = await classify_yes_no_ai(message.content, debug=True)
                if yn_move == "YES":
                    cp["pending_move_confirmation"] = False
                    if cp.get("profile_message_id"):
                        try:
                            orig_profile_msg = await message.channel.fetch_message(cp["profile_message_id"])
                            await self._process_profile(orig_profile_msg, cp, progress_key, move_cleared=True)
                        except discord.NotFound:
                            await message.reply("元のプロフィールが見つかりませんでした。お手数ですが、再度プロフィール全体を投稿してください。")
                            update_candidate_status(cp, "プロフィール未記入")
                            await data_manager.save_data()
                    else:
                        await message.reply("移住のご意思は確認いたしました。お手数ですが、再度プロフィール全体をご投稿ください。")
                        update_candidate_status(cp, "プロフィール未記入")
                        await data_manager.save_data()
                elif yn_move == "NO":
                    await message.reply("申し訳ありませんが、今回はお見送りとさせていただきます。")
                else:
                    await message.reply("恐れ入ります、移住予定については **はい** / **いいえ** でお答えいただけますか？")
                request_dashboard_update(self.bot)
                return

            # ──────────────────────────────────────────────
            # A-3. 通常のプロフィール投稿 or 自由な発言
            # ──────────────────────────────────────────────
            current_status = cp.get("status")
            if current_status in ("プロフィール未記入", "要修正") and looks_like_profile(message.content):
                await self._process_profile(message, cp, progress_key)
            elif cp.get("interviewer_id") and cp.get("profile_evaluated") and current_status != "面接済み":
                if not message.mentions and not message.reference:
                    await notify_interviewer_of_candidate_message(self.bot, cp, message)
            return  # 候補者のメッセージ処理はここで終了


        # --- B) メッセージ送信者が候補者以外（面接官を想定）の場合 ---
        main_guild = self.bot.get_guild(MAIN_GUILD_ID)
        if not main_guild:
            return

        # 送信者が面接官ロールを持っているかチェック
        interviewer_member = main_guild.get_member(message.author.id)
        interviewer_role = main_guild.get_role(INTERVIEWER_ROLE_ID)
        if not (interviewer_member and interviewer_role and interviewer_role in interviewer_member.roles):
            return  # 面接官でなければ何もしない

        # 条件: ステータスが「記入済み」(要連絡！) AND 担当者が未設定
        #       → メッセージを送信した面接官を担当者に設定する
        if cp.get("status") == "記入済み" and not cp.get("interviewer_id"):
            logger.info(f"チャット投稿により、候補者 {cp.get('candidate_id')} の担当者を {interviewer_member.id} に設定します。")

            # 担当者IDを保存
            cp["interviewer_id"] = interviewer_member.id

            # ステータスを「記入済み」(要連絡！) → 「担当者待ち」(日程調整してね！) に更新
            update_candidate_status(cp, "担当者待ち")

            # 担当者への割り当て通知を確実に行うためフラグをリセット
            cp["notified_assignment"] = False

            # 変更を永続化
            await data_manager.save_data()

            # ダッシュボードを更新
            request_dashboard_update(self.bot)

            # 担当者にDMで通知
            target_guild = self.bot.get_guild(cp.get("source_guild_id")) or main_guild
            candidate_member = await utils.safe_fetch_member(target_guild, cp.get("candidate_id"))

            if candidate_member and isinstance(message.channel, discord.TextChannel):
                await notify_interviewer_assignment(
                    interviewer_member,
                    candidate_member,
                    message.channel,
                    cp,
                )
                # チャンネルへの通知メッセージを削除
            return


    # ===== on_message_edit: 編集の処理 ===========================
    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        # Bot自身の編集や、内容が変わっていない場合は無視
        if after.author.bot or before.content == after.content:
            return

        progress_key = data_manager.interview_channel_mapping.get(after.channel.id)
        if not progress_key:
            return

        cp = data_manager.candidate_progress.get(progress_key)
        if not cp or cp.get("candidate_id") != after.author.id:
            return

        # 編集されたメッセージが、保存済みのプロフィールIDと一致する場合、
        # または、ステータスが「要修正」で、内容がプロフィールらしい場合に再評価
        is_profile_message = (cp.get("profile_message_id") == after.id)
        is_retry_submission = (cp.get("status") == "要修正" and looks_like_profile(after.content))

        if is_profile_message or is_retry_submission:
            logger.info(f"プロフィール編集を検知 (MsgID: {after.id})。再評価を実行します。")
            # 編集による再評価のため、特例フラグは立てずに共通処理を呼び出す
            await self._process_profile(after, cp, progress_key)

# ------------------------------------------------
# AdminCog（管理者用コマンド）
# ------------------------------------------------

def is_admin_or_has_role(role_id: int):
    """管理者、または指定されたロールIDを持つユーザーのみコマンド実行を許可するカスタムチェック。"""
    async def predicate(interaction: discord.Interaction) -> bool:
        # 管理者権限を持っているかチェック
        if interaction.permissions.administrator:
            return True
        # Member オブジェクトであり、かつ指定ロールを持っているかチェック
        if isinstance(interaction.user, discord.Member):
            return any(r.id == role_id for r in interaction.user.roles)
        return False
    return app_commands.check(predicate)

class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="update_stats", description="統計を手動更新して出力します")
    @app_commands.checks.has_permissions(administrator=True)
    async def update_stats_command(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await update_stats(self.bot)
        await interaction.followup.send("統計情報を更新しました。", ephemeral=True)

    @app_commands.command(name="remove_ban", description="対象ユーザーのBAN／インターバルを手動で解除します（メインサーバー専用）")
    @app_commands.checks.has_permissions(administrator=True)
    async def remove_ban_command(self, interaction: discord.Interaction, target: discord.Member):
        if interaction.guild.id != MAIN_GUILD_ID:
            await interaction.response.send_message("このコマンドはメインサーバーでのみ使用可能です。", ephemeral=True)
            return
        ban_record = ban_manager.check_ban(target.id)
        if not ban_record:
            await interaction.response.send_message(f"{target.mention} は現在BAN／インターバル状態ではありません。", ephemeral=True)
            return
        ban_manager.remove_ban(target.id)
        await interaction.response.send_message(f"{target.mention} のBAN／インターバルを手動で解除しました。", ephemeral=True)

    @app_commands.command(name="ban_list", description="現在のBAN／インターバル対象の一覧を表示します（メインサーバー専用）")
    @app_commands.checks.has_permissions(administrator=True)
    async def ban_list_command(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if interaction.guild.id != MAIN_GUILD_ID:
            await interaction.followup.send("このコマンドはメインサーバーでのみ使用可能です。", ephemeral=True)
            return
        if not ban_manager.ban_records:
            await interaction.followup.send("現在、BAN／インターバル状態のユーザーは存在しません。", ephemeral=True)
            return
        lines = []
        for user_id_str, record in ban_manager.ban_records.items():
            user_id = int(user_id_str)
            member = interaction.guild.get_member(user_id)
            name = member.display_name if member else f"ID:{user_id}"
            ban_type = record.get("ban_type", "Unknown")
            ban_origin = record.get("ban_origin", "Unknown")
            ban_time = record.get("ban_time", "Unknown")
            lines.append(f"{name} - {ban_type} (origin: {ban_origin}) - {ban_time})")
        
        # メッセージを2000文字のチャンクに分割して送信
        message_chunks = []
        current_chunk = "現在のBAN／インターバル対象一覧:\n"
        for line in lines:
            if len(current_chunk) + len(line) + 1 > 2000:
                message_chunks.append(current_chunk)
                current_chunk = ""
            current_chunk += line + "\n"
        
        if current_chunk:
            message_chunks.append(current_chunk)

        for chunk in message_chunks:
            await interaction.followup.send(chunk, ephemeral=True)

    @app_commands.command(name="add_ban", description="指定したIDのユーザーをBAN／インターバルリストに手動で追加します。")
    @is_admin_or_has_role(ADMIN_ROLE_ID)
    @app_commands.describe(
        user_id="対象ユーザーのID",
        ban_type="種別を選択してください",
    )
    @app_commands.choices(ban_type=[
        app_commands.Choice(name="BAN (無期限)", value="BAN"),
        app_commands.Choice(name="インターバル (90日)", value="INTERVAL"),
    ])
    async def add_ban_command(self, interaction: discord.Interaction, user_id: str, ban_type: str):
        try:
            target_id = int(user_id)
        except ValueError:
            await interaction.response.send_message("ユーザーIDは数字で入力してください。", ephemeral=True)
            return

        if ban_manager.check_ban(target_id):
            target_user = await self.bot.fetch_user(target_id)
            mention = target_user.mention if target_user else f"ユーザーID `{target_id}`"
            await interaction.response.send_message(f"{mention} は既にリストに登録されています。", ephemeral=True)
            return

        ban_manager.add_ban(
            user_id=target_id,
            ban_origin="manual",
            ban_type=ban_type.upper()
        )

        target_user = await self.bot.fetch_user(target_id)
        mention = target_user.mention if target_user else f"ユーザーID `{target_id}`"
        await interaction.response.send_message(f"{mention} を {ban_type.upper()} リストに手動で追加しました。", ephemeral=True)
        logger.info(f"/add_ban: {target_id} を {ban_type.upper()} に追加 by {interaction.user.id}")

    # ------------------------------------------------
    # /add_manual_count ─ 手動で面接回数を調整
    # ------------------------------------------------
    @app_commands.command(
        name="add_manual_count",
        description="面接官の面接回数を手動調整します（add / sub / set）"
    )
    @is_admin_or_has_role(ADMIN_ROLE_ID)
    @app_commands.describe(
        interviewer="対象の面接官メンバー",
        count="調整値 (0 以上の整数)",
        mode="操作モード: add=加算 / sub=減算 / set=上書き",
        month="対象月 (YYYY-MM、省略で当月)"
    )
    async def add_manual_count_command(
            self,
            interaction: discord.Interaction,
            interviewer: discord.Member,
            count: app_commands.Range[int, 0],
            mode: str = "add",
            month: str | None = None,
    ):
        """
        ── mode ────────────────────────────────
           add … 既存値に +count
           sub … manual_set を最大 count 件削除
           set … manual_set を全削除して +count 件追加
        ── month ───────────────────────────────
           ・YYYY-MM 形式（例 2025-05）
           ・省略時は現在 JST での当月
        """
        await interaction.response.defer(ephemeral=True)
        mode = mode.lower()
        if mode not in ("add", "sub", "set"):
            await interaction.followup.send(
                "mode は **add / sub / set** のいずれかで指定してください。",
                ephemeral=True
            )
            return

        # ---------- 対象年月を決定 ----------
        if month:
            try:
                dt_target = datetime.strptime(month, "%Y-%m").replace(
                    tzinfo=JST, day=1, hour=0, minute=0, second=0, microsecond=0
                )
            except ValueError:
                await interaction.followup.send(
                    "month は **YYYY-MM** 形式で指定してください。", ephemeral=True
                )
                return
        else:
            now = datetime.now(JST)
            dt_target = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        ym_key = f"{dt_target.year}-{dt_target.month:02d}"
        iid = interviewer.id

        logger.info(
            f"/add_manual_count: mode={mode} ym={ym_key} interviewer={iid} "
            f"count={count} by {interaction.user.id}"
        )

        # ---------- 既存 manual_set 抽出 ----------
        def same_month(rec_dt: datetime) -> bool:
            return rec_dt.year == dt_target.year and rec_dt.month == dt_target.month

        existing_manual = [
            r for r in data_manager.interview_records
            if (
                    r.get("interviewer_id") == iid
                    and r.get("result") == "manual_set"
                    and same_month(datetime.fromisoformat(r.get("date")))
            )
        ]

        # ---------- mode 別処理 ----------
        if mode == "set":
            data_manager.interview_records = [
                r for r in data_manager.interview_records
                if not (
                        r.get("interviewer_id") == iid
                        and r.get("result") == "manual_set"
                        and same_month(datetime.fromisoformat(r.get("date")))
                )
            ]
            delta = count

        elif mode == "add":
            delta = count

        else:  # sub
            remove_n = min(count, len(existing_manual))
            keep_n = len(existing_manual) - remove_n
            removed = 0
            new_records = []
            for r in data_manager.interview_records:
                if (
                        removed < remove_n
                        and r.get("interviewer_id") == iid
                        and r.get("result") == "manual_set"
                        and same_month(datetime.fromisoformat(r.get("date")))
                ):
                    removed += 1
                    continue
                new_records.append(r)

            data_manager.interview_records = new_records
            await data_manager.save_data()
            await update_stats(self.bot, target_months=[ym_key])  # ★ 変更点
            if ym_key == datetime.now(JST).strftime("%Y-%m"):
                await update_monthly_stats(self.bot)
            await interaction.followup.send(
                f"{ym_key} の manual_set を **{remove_n} 件削除** しました。\n"
                f"現在の manual_set 件数: **{keep_n}**",
                ephemeral=True
            )
            return  # sub 処理終了

        # add / set → delta 件追加
        for _ in range(delta):
            data_manager.interview_records.append(
                {
                    "date": dt_target.isoformat(),
                    "interviewer_id": iid,
                    "interviewee_id": f"manual_set_{uuid.uuid4()}",
                    "result": "manual_set",
                }
            )

        await data_manager.save_data()
        await update_stats(self.bot, target_months=[ym_key])  # ★ 変更点
        if ym_key == datetime.now(JST).strftime("%Y-%m"):
            await update_monthly_stats(self.bot)

        op_word = {"add": "加算", "set": "上書き"}[mode]
        await interaction.followup.send(
            f"{ym_key} の {interviewer.mention} の面接回数を **{op_word} +{delta} 回** しました。",
            ephemeral=True
        )

    # ------------------------------------------------
    # エラーハンドラ
    # ------------------------------------------------
    @add_manual_count_command.error
    async def add_manual_count_error(
            self,
            interaction: discord.Interaction,
            error: app_commands.AppCommandError
    ):
        if isinstance(error, app_commands.MissingRole):
            msg = "このコマンドを実行する権限がありません。"
        else:
            msg = "コマンド実行中にエラーが発生しました。"
            logger.error(f"/add_manual_count エラー: {error}", exc_info=True)

        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)

    # ------------------------------------------------
    # /show_candidate_memos  ─ 追加メモを自分だけに一覧表示
    # ------------------------------------------------
    @app_commands.command(
        name="show_candidate_memos",
        description="候補者（ユーザーID / @メンション / 表示名）の追加メモを最新20件まで表示"
    )
    @app_commands.describe(candidate="ユーザーID・@メンション・表示名のいずれか")
    @app_commands.checks.has_any_role(INTERVIEWER_ROLE_ID, ADMIN_ROLE_ID)
    async def show_candidate_memos(self, interaction: discord.Interaction, candidate: str):
        await interaction.response.defer(ephemeral=True)
        bot = interaction.client

        # ---------- ① 候補者 ID 解決 ----------------------------------
        def resolve_candidate_id(raw: str) -> Optional[int]:
            raw = raw.strip()
            if raw.startswith("<@") and raw.endswith(">"):
                raw = raw.strip("<@!>")
            if raw.isdigit():
                return int(raw)
            mg = bot.get_guild(MAIN_GUILD_ID)
            if mg:
                m = discord.utils.find(
                    lambda u: u.display_name == raw or u.name == raw,
                    mg.members
                )
                return m.id if m else None
            return None

        cand_id = resolve_candidate_id(candidate)
        if cand_id is None:
            await interaction.followup.send("候補者を特定できませんでした。", ephemeral=True)
            return

        # ---------- ② 872… チャンネルの履歴抽出 -----------------------
        records = [
            r for r in data_manager.memo_history.get(str(cand_id), [])
            if r["channel_id"] == ADDITIONAL_MEMO_CHANNEL_ID
        ]
        if not records:
            await interaction.followup.send("追加メモは見つかりませんでした。", ephemeral=True)
            return

        records.sort(key=lambda r: r["timestamp"], reverse=True)
        records = records[:20]

        # ========== interviewer 名を補完する関数 =======================
        async def resolve_interviewer_name(rec: dict[str, Any]) -> str:
            iid = rec.get("interviewer_id")
            if iid:
                return get_main_display_name(bot, iid)

            # interviewer_id がない場合はメッセージ解析
            try:
                g = bot.get_guild(rec["guild_id"])
                ch = g.get_channel(rec["channel_id"]) if g else None
                if ch:
                    msg = await ch.fetch_message(rec["message_id"])
                    if msg.embeds:
                        footer = msg.embeds[0].footer.text or ""
                        m = re.search(r"面接担当者:\s*(.+?)\n", footer)
                        if m:
                            return m.group(1).strip()
            except Exception:
                pass
            return "不明"

        # ---------- ③ Embed 作成 --------------------------------------
        embed = discord.Embed(
            title=f"候補者 {cand_id} の追加メモ ({len(records)}件)",
            color=0x734bd1
        )

        for idx, rec in enumerate(records, start=1):
            interviewer_name = await resolve_interviewer_name(rec)
            url = make_message_link(rec["guild_id"], rec["channel_id"], rec["message_id"])
            ts = rec["timestamp"][:19].replace("T", " ")
            embed.add_field(
                name=f"メモ {idx}　📅{ts}",
                value=f"担当: **{interviewer_name}**\n{url}",
                inline=False
            )

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ---------- エラーハンドラ ----------------------------------------
    @show_candidate_memos.error
    async def show_candidate_memos_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingAnyRole):
            msg = "このコマンドを実行する権限がありません。"
        else:
            logger.error(f"/show_candidate_memos エラー: {error}", exc_info=True)
            msg = "コマンド実行中にエラーが発生しました。"
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)

    # ------------------------------------------------
    # /list_passed_candidates
    #   メインサーバー限定：
    #   面接担当ロール(892…) → 候補者ロール保持者を列挙
    #   担当者は candidate_progress が優先、
    #   無ければ interview_records の最新 interviewer_id、
    #   それでも無ければ「担当者不明」グループへ
    # ------------------------------------------------
    @app_commands.command(
        name="list_passed_candidates",
        description="面接担当者ごとに候補者ロール保持ユーザーを表示（メインサーバー）"
    )
    @app_commands.checks.has_any_role(INTERVIEWER_ROLE_ID, ADMIN_ROLE_ID)
    async def list_passed_candidates(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        bot = interaction.client
        guild = bot.get_guild(MAIN_GUILD_ID)
        if guild is None:
            await interaction.followup.send("メインサーバーが見つかりません。", ephemeral=True)
            return

        interviewer_role = guild.get_role(INTERVIEWER_ROLE_ID)
        if interviewer_role is None:
            await interaction.followup.send("面接担当者ロールが見つかりません。", ephemeral=True)
            return

        candidate_role_ids = {784723518402592803, 1289488152301539339}

        # ---------- ① interviewer_id → [display_name,…] ------------
        from collections import defaultdict
        mapping: defaultdict[int | str, list[str]] = defaultdict(list)  # str "unknown" 用

        # 1) メインサーバーで候補者ロール保持メンバーを取得
        candidate_members = [
            m for m in guild.members
            if any(r.id in candidate_role_ids for r in m.roles)
        ]

        # 2) interview_records を最新→古い順にインデックス
        latest_interviewer_by_user: dict[int, int | None] = {}
        for rec in reversed(data_manager.interview_records):
            uid = rec.get("interviewee_id")
            if uid is None or uid in latest_interviewer_by_user:
                continue
            latest_interviewer_by_user[uid] = rec.get("interviewer_id")

        # 3) 各候補者について担当者を決定
        for member in candidate_members:
            iid: int | None = None

            # 3-A candidate_progress に残っていれば優先
            prog = next(
                (cp for cp in data_manager.candidate_progress.values()
                 if cp.get("candidate_id") == member.id
                 and cp.get("source_guild_id") == MAIN_GUILD_ID),
                None
            )
            if prog:
                iid = prog.get("interviewer_id")

            # 3-B それでも None なら interview_records から
            if iid is None:
                iid = latest_interviewer_by_user.get(member.id)

            # 3-C 担当者ロールを持っていない場合 → unknown
            if iid is None or guild.get_member(iid) not in interviewer_role.members:
                mapping["unknown"].append(member.display_name)
            else:
                mapping[iid].append(member.display_name)

        # ---------- ② 出力生成 (テキスト階層) -----------------------
        if not mapping:
            await interaction.followup.send("該当する候補者が見つかりませんでした。", ephemeral=True)
            return

        lines: list[str] = []
        # 担当者あり
        for interviewer in sorted(interviewer_role.members, key=lambda m: m.display_name):
            cand_list = mapping.get(interviewer.id, [])
            if not cand_list:
                continue
            lines.append(interviewer.display_name)
            for n in sorted(cand_list):
                lines.append(f"ー{n}")

        await interaction.followup.send("\n".join(lines), ephemeral=True)

    # ---------- エラーハンドラ ------------------------------------
    @list_passed_candidates.error
    async def list_passed_candidates_error(
            self,
            interaction: discord.Interaction,
            error: app_commands.AppCommandError
    ):
        if isinstance(error, app_commands.MissingAnyRole):
            text = "このコマンドを実行する権限がありません。"
        else:
            logger.error("/list_passed_candidates エラー", exc_info=True)
            text = "コマンド実行中にエラーが発生しました。"
        if interaction.response.is_done():            await interaction.followup.send(text, ephemeral=True)
        else:            await interaction.response.send_message(text, ephemeral=True)

    @app_commands.command(name="check_permissions", description="指定ユーザーのチャンネル権限を詳細に確認します")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(target="確認対象のメンバー", channel="確認対象のテキストチャンネル")
    async def check_permissions_command(self, interaction: discord.Interaction, target: discord.Member, channel: discord.TextChannel):
        """指定されたユーザーの、指定されたチャンネルにおける権限の詳細を調査して表示する"""
        await interaction.response.defer(ephemeral=True)

        # 1. ユーザーが持つロールの一覧
        user_roles = target.roles
        role_names = [role.name for role in user_roles]

        # 2. チャンネルの権限上書き設定（オーバーライト）を取得
        overwrites = channel.overwrites

        # 3. 最終的に計算された権限
        final_perms = channel.permissions_for(target)

        # --- Embedを作成して結果を表示 ---
        embed = discord.Embed(
            title="権限詳細チェック",
            description=f"**ユーザー:** {target.mention}**チャンネル:** {channel.mention}",
            color=discord.Color.orange()
        )

        embed.add_field(name="保有ロール一覧", value="`".join(role_names) or "なし", inline=False)

        # --- オーバーライトの詳細 --- 
        overwrite_details = []
        # @everyone
        everyone_overwrite = overwrites.get(interaction.guild.default_role)
        if everyone_overwrite:
            allow, deny = everyone_overwrite.pair()
            overwrite_details.append(f"**@everyone**:")
            if [p for p, v in allow if v]:
                overwrite_details.append(f"  許可: `{', '.join([p for p, v in allow if v])}`")
            if [p for p, v in deny if v]:
                overwrite_details.append(f"  拒否: `{', '.join([p for p, v in deny if v])}`")

        # ユーザーの各ロール
        for role in sorted(user_roles, key=lambda r: r.position, reverse=True):
            role_overwrite = overwrites.get(role)
            if role_overwrite:
                allow, deny = role_overwrite.pair()
                overwrite_details.append(f"**{role.name} (ロール)**:")
                if [p for p, v in allow if v]:
                    overwrite_details.append(f"  許可: `{', '.join([p for p, v in allow if v])}`")
                if [p for p, v in deny if v]:
                    overwrite_details.append(f"  拒否: `{', '.join([p for p, v in deny if v])}`")
        
        # ユーザー固有
        user_overwrite = overwrites.get(target)
        if user_overwrite:
            allow, deny = user_overwrite.pair()
            overwrite_details.append(f"**{target.display_name} (ユーザー固有)**:")
            if [p for p, v in allow if v]:
                overwrite_details.append(f"  許可: `{', '.join([p for p, v in allow if v])}`")
            if [p for p, v in deny if v]:
                overwrite_details.append(f"  拒否: `{', '.join([p for p, v in deny if v])}`")
        if overwrite_details:
            embed.add_field(name="チャンネル権限オーバーライド", value="\n".join(overwrite_details), inline=False)
        else:
            embed.add_field(name="チャンネル権限オーバーライド", value="なし", inline=False)

        # --- 最終権限 --- 
        final_perms_list = [
            f"チャンネルの表示: {'✅' if final_perms.view_channel else '❌'}",
            f"メッセージを送信: {'✅' if final_perms.send_messages else '❌'}",
            f"スレッドの作成: {'✅' if final_perms.create_public_threads else '❌'}",
            f"埋め込みリンク: {'✅' if final_perms.embed_links else '❌'}",
            f"ファイルの添付: {'✅' if final_perms.attach_files else '❌'}"
        ]
        embed.add_field(name="最終的な計算権限", value="\n".join(final_perms_list), inline=False)

        await interaction.followup.send(embed=embed, ephemeral=True)

    @check_permissions_command.error
    async def check_permissions_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, (app_commands.MissingPermissions, app_commands.MissingAnyRole, app_commands.MissingRole)):
            text = "このコマンドは管理者権限が必要です。"
        else:
            logger.error("/check_permissions エラー", exc_info=True)
            text = "内部エラーが発生しました。ログを確認してください。"
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)


# ------------------------------------------------
# Bot本体（Cog登録など）
# ------------------------------------------------
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True

class MyBot(commands.Bot):
    async def setup_hook(self) -> None:
        global delayed_action_manager
        delayed_action_manager = DelayedActionManager(asyncio.get_running_loop())
        await self.add_cog(EventCog(self))
        await self.add_cog(TaskCog(self))
        await self.add_cog(AdminCog(self))
        await self.add_cog(MessageCog(self))
        await self.add_cog(DelayedActionCog(self))
        await self.add_cog(MonthlyCountCog(self))
        await self.add_cog(GuideCountCog(self))
        self.add_view(VCControlView())

    async def on_ready(self) -> None:
        await self.tree.sync()
        logger.info(f'Logged in as {self.user.name}')
        for progress_key, cp in data_manager.candidate_progress.items():
            self.add_view(InterviewResultView(progress_key))

bot = MyBot(command_prefix="!$", intents=intents)
bot.run(BOT_TOKEN)
