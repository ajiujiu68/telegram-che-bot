#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram 群组「上车」自动提醒机器人 (多群组·群主/管理员版)
数据持久化：Neon Postgres (asyncpg)
"""

import asyncio
import html
import json
import logging
import os
import random
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional

import asyncpg

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, ContextTypes, MessageHandler, filters,
    CallbackQueryHandler, ChatMemberHandler
)

# ==================================================================
#                          配 置 区
# ==================================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
TRIGGER = "上车"
SUPER_ADMIN_IDS: list[int] = []

CARS = ["奥迪", "奔驰", "玛莎拉蒂", "帕拉梅拉", "保时捷", "法拉利", "宝马", "凯迪拉克"]
REPLY_TEMPLATE = "{name}导师，{car}🚗来咯，请注意提前做好准备接🏎"

ADMIN_CACHE_TTL = 20
MEMBER_CACHE_TTL = 30
MAX_NAME_LEN = 32

ADMIN_ROLES = ("creator", "administrator")
MEMBER_ROLES = ("creator", "administrator", "member", "restricted")

TABLE_GROUPS = "che_groups"
TABLE_DRIVERS = "che_drivers"
TABLE_STATE = "che_state"

# ==================================================================
#                          逻辑区
# ==================================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

user_states: dict = {}
admin_cache: dict = {}
member_cache: dict = {}

db_pool: Optional[asyncpg.Pool] = None
groups: dict = {}
drivers_list: dict = {}
state: dict = {}


# ------------------------ 数据库操作 ------------------------

async def init_db() -> None:
    global db_pool
    logger.info("正在连接数据库...")
    db_pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=1,
        max_size=5,
        command_timeout=30,
        max_inactive_connection_lifetime=240,
    )
    logger.info("✅ Neon 数据库连接池已创建")

    async with db_pool.acquire() as conn:
        await conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {TABLE_GROUPS} (
                id INTEGER PRIMARY KEY DEFAULT 1,
                data JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        await conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {TABLE_DRIVERS} (
                id INTEGER PRIMARY KEY DEFAULT 1,
                data JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        await conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {TABLE_STATE} (
                id INTEGER PRIMARY KEY DEFAULT 1,
                data JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)
    logger.info("✅ 数据库表已就绪")


async def close_db() -> None:
    global db_pool
    if db_pool:
        await db_pool.close()
        logger.info("🔌 数据库连接池已关闭")


async def db_load(table: str) -> dict:
    if not db_pool:
        return {}
    try:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(f"SELECT data FROM {table} WHERE id = 1")
            if row:
                raw = row["data"]
                if isinstance(raw, str):
                    return json.loads(raw)
                return dict(raw) if raw else {}
            return {}
    except Exception as e:
        logger.error("db_load(%s) 失败: %s", table, e)
        return {}


async def db_save(table: str, data: dict) -> None:
    if not db_pool:
        return
    try:
        async with db_pool.acquire() as conn:
            await conn.execute(f"""
                INSERT INTO {table} (id, data, updated_at)
                VALUES (1, $1::jsonb, NOW())
                ON CONFLICT (id) DO UPDATE
                SET data = EXCLUDED.data, updated_at = NOW()
            """, json.dumps(data, ensure_ascii=False))
    except Exception as e:
        logger.error("db_save(%s) 失败: %s", table, e)


async def load_all_data() -> None:
    global groups, drivers_list, state
    groups = await db_load(TABLE_GROUPS)
    drivers_list = await db_load(TABLE_DRIVERS)
    state = await db_load(TABLE_STATE)

    clean_drivers = {}
    for k, v in drivers_list.items():
        if isinstance(v, list):
            clean_drivers[str(k)] = v
    drivers_list = clean_drivers

    clean_state = {}
    for k, v in state.items():
        try:
            clean_state[str(k)] = int(v)
        except (TypeError, ValueError):
            continue
    state = clean_state

    logger.info("📂 已从数据库加载：%d 群组 / %d 接车人 / %d 状态",
                len(groups), total_drivers(), len(state))


async def save_groups() -> None:
    await db_save(TABLE_GROUPS, groups)


async def save_drivers() -> None:
    await db_save(TABLE_DRIVERS, drivers_list)


async def save_state() -> None:
    await db_save(TABLE_STATE, state)


# ------------------------ 小工具 ------------------------

def esc(text, quote: bool = False) -> str:
    return html.escape(str(text), quote=quote)


def build_mention_html(driver: dict) -> str:
    name = esc(driver.get("name", ""))
    user_id = driver.get("user_id")
    username = str(driver.get("username", "") or "").lstrip("@")

    if user_id:
        return f'<a href="tg://user?id={user_id}">{name}</a>'
    if username:
        if username.isdigit():
            return f'<a href="tg://user?id={username}">{name}</a>'
        return f'<a href="https://t.me/{username}">{name}</a>'
    return name


async def track_group(chat) -> bool:
    if chat is None or chat.type not in ("group", "supergroup"):
        return False
    key = str(chat.id)
    title = chat.title or key
    old = groups.get(key)
    if old and old.get("title") == title:
        return False
    groups[key] = {"title": title, "updated": int(time.time())}
    await save_groups()
    admin_cache.clear()
    return True


def list_known_groups() -> list:
    items = [(k, v.get("title", k)) for k, v in groups.items()]
    items.sort(key=lambda x: x[1])
    return items


def group_title(chat_id_str: str) -> str:
    info = groups.get(str(chat_id_str))
    if info:
        t = info.get("title", "")
        if t:
            return t
    return str(chat_id_str)


def total_drivers() -> int:
    return sum(len(v) for v in drivers_list.values())


def is_super_admin(user_id: int) -> bool:
    return user_id in SUPER_ADMIN_IDS


def role_label(status: Optional[str]) -> str:
    return {
        "super": "超级管理员",
        "creator": "群主",
        "administrator": "管理员",
        "member": "成员",
        "restricted": "受限成员",
    }.get(status or "", status or "未知")


def driver_exists(dl: list, new_driver: dict) -> bool:
    name = new_driver.get("name", "")
    username = (new_driver.get("username") or "").lstrip("@").lower()
    uid = new_driver.get("user_id")

    for d in dl:
        if name and d.get("name") == name:
            return True
        d_uid = d.get("user_id")
        if uid and d_uid == uid:
            return True
        d_uname = (d.get("username") or "").lstrip("@").lower()
        if username and d_uname == username:
            return True
    return False


async def safe_edit(query, text: str, **kwargs) -> None:
    try:
        await query.edit_message_text(text, **kwargs)
    except Exception as e:
        if "not modified" in str(e).lower():
            return
        try:
            await query.message.reply_text(text, **kwargs)
        except Exception as e2:
            pass


# ------------------------ 管理员权限 ------------------------

async def get_member_status(context: ContextTypes.DEFAULT_TYPE,
                            chat_id_int: int, user_id: int) -> Optional[str]:
    key = f"{chat_id_int}:{user_id}"
    now = time.time()
    cached = member_cache.get(key)
    if cached and now - cached[0] < MEMBER_CACHE_TTL:
        return cached[1]

    try:
        m = await context.bot.get_chat_member(chat_id=chat_id_int, user_id=user_id)
        status = getattr(m, "status", None)
    except Exception:
        status = None

    member_cache[key] = (now, status)
    return status


async def compute_admin_groups(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> list:
    if is_super_admin(user_id):
        return [(cid, title, "super") for cid, title in list_known_groups()]

    result = []
    bot_id = context.bot.id
    for chat_id_str, title in list_known_groups():
        try:
            chat_id_int = int(chat_id_str)
        except ValueError:
            continue
        user_status = await get_member_status(context, chat_id_int, user_id)
        if user_status not in ADMIN_ROLES:
            continue
        bot_status = await get_member_status(context, chat_id_int, bot_id)
        if bot_status != "administrator":
            continue
        result.append((chat_id_str, title, user_status))
    return result


async def get_admin_groups(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> list:
    now = time.time()
    cached = admin_cache.get(user_id)
    if cached and now - cached[0] < ADMIN_CACHE_TTL:
        return cached[1]
    result = await compute_admin_groups(context, user_id)
    admin_cache[user_id] = (now, result)
    return result


async def require_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, query=None) -> tuple:
    user_id = update.effective_user.id
    admin_groups = await get_admin_groups(context, user_id)
    if admin_groups:
        return True, admin_groups
    text = "❌ 您没有权限，请联系管理员操作"
    if query is not None:
        await safe_edit(query, text)
    else:
        try:
            await update.message.reply_text(text)
        except Exception:
            pass
    return False, []


# ------------------------ 主菜单 / 视图 ------------------------

def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ 添加接车人", callback_data="add_start")],
        [InlineKeyboardButton("🗑️ 删除接车人", callback_data="del_start")],
        [InlineKeyboardButton("📋 我的接车人", callback_data="my_list")],
        [InlineKeyboardButton("🌐 查看我所在的群组", callback_data="list_groups")],
    ])


def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏠 返回主菜单", callback_data="menu")],
    ])


async def send_main_menu(context: ContextTypes.DEFAULT_TYPE, user_id: int, target_msg=None, query=None) -> None:
    admin_groups = await get_admin_groups(context, user_id)
    if admin_groups:
        parts = []
        for _, title, role in admin_groups[:5]:
            parts.append(f"{esc(title)}（{role_label(role)}）")
        titles = "、".join(parts)
        if len(admin_groups) > 5:
            titles += f" 等 {len(admin_groups)} 个群"
        text = f"👋 你好，管理员！\n\n你可管理的群组：{titles}\n\n请选择操作："
        keyboard = main_menu_keyboard()
    else:
        text = "❌ 您没有权限，请联系管理员操作"
        keyboard = None

    if query is not None:
        await safe_edit(query, text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    else:
        await target_msg.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)


# ------------------------ 命令处理 ------------------------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type in ("group", "supergroup"):
        await track_group(update.effective_chat)
        await update.message.reply_text("⚠️ 为了不影响群内秩序，请私聊我进行管理操作哦！点击我的头像私聊即可。")
        return
    user_id = update.effective_user.id
    user_states.pop(user_id, None)
    await send_main_menu(context, user_id, target_msg=update.message)


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type in ("group", "supergroup"): return
    user_id = update.effective_user.id
    user_states.pop(user_id, None)
    await send_main_menu(context, user_id, target_msg=update.message)


async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type in ("group", "supergroup"):
        await track_group(update.effective_chat)
        await update.message.reply_text("⚠️ 请私聊我进行添加操作，群组内仅供触发上车。")
        return
    user_id = update.effective_user.id
    user_states.pop(user_id, None)
    ok, admin_groups = await require_admin(update, context)
    if not ok: return
    await prompt_choose_group(context, user_id, admin_groups, query=None, message=update.message)


async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type in ("group", "supergroup"):
        await track_group(update.effective_chat)
        await update.message.reply_text("⚠️ 请私聊我进行删除操作。")
        return
    user_id = update.effective_user.id
    user_states.pop(user_id, None)
    ok, admin_groups = await require_admin(update, context)
    if not ok: return
    await show_delete_list(context, user_id, admin_groups, query=None, message=update.message)


async def my_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type in ("group", "supergroup"):
        await update.message.reply_text("⚠️ 请私聊我查看。")
        return
    user_id = update.effective_user.id
    ok, admin_groups = await require_admin(update, context)
    if not ok: return
    await show_my_list(context, user_id, admin_groups, query=None, message=update.message)


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type in ("group", "supergroup"):
        await update.message.reply_text("⚠️ 请私聊我查看。")
        return
    user_id = update.effective_user.id
    ok, _ = await require_admin(update, context)
    if not ok: return
    await show_user_groups(context, user_id, query=None, message=update.message)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type in ("group", "supergroup"): return
    user_id = update.effective_user.id
    if user_states.pop(user_id, None) is not None:
        await update.message.reply_text("❌ 操作已取消。")
    else:
        await update.message.reply_text("当前没有进行中的操作。")


# ------------------------ 视图 ------------------------

async def prompt_choose_group(context: ContextTypes.DEFAULT_TYPE, user_id: int, admin_groups: list, query=None, message=None) -> None:
    if not admin_groups:
        text = ("⚠️ 你还不是任何群的管理员。\n\n成为管理员的步骤：\n1. 把机器人拉进你的群；\n2. 将机器人设为【群管理员】；\n3. 你本人是该群的群主或管理员；\n4. 在群里发一条消息（例如「上车」）；\n5. 回到私聊，点击「添加接车人」。")
        if query: await safe_edit(query, text)
        else: await message.reply_text(text)
        return

    if len(admin_groups) == 1:
        chat_id_str, title, role = admin_groups[0]
        user_states[user_id] = {"state": "ASK_NAME", "data": {"target_chat_id": chat_id_str}}
        text = f"🎯 目标群组：<b>{esc(title)}</b>（你的身份：{role_label(role)}）\n\n请输入接车人的 <b>姓名</b>（例如：张三）：\n\n（随时发送 /cancel 取消本次添加）"
        if query: await safe_edit(query, text, parse_mode=ParseMode.HTML)
        else: await message.reply_text(text, parse_mode=ParseMode.HTML)
        return

    keyboard = []
    for chat_id_str, title, role in admin_groups:
        label = f"{title}（{role_label(role)}）"
        if len(label) > 32: label = label[:30] + "…"
        keyboard.append([InlineKeyboardButton(f"🌐 {label}", callback_data=f"grp|{chat_id_str}")])
    keyboard.append([InlineKeyboardButton("🏠 返回主菜单", callback_data="menu")])
    text = "请选择要给哪个群组添加接车人："
    if query: await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard))
    else: await message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def show_delete_list(context: ContextTypes.DEFAULT_TYPE, user_id: int, admin_groups: list, query=None, message=None) -> None:
    admin_chat_ids = {cid for cid, _, _ in admin_groups}
    entries = []
    for chat_id_str, dl in drivers_list.items():
        if chat_id_str not in admin_chat_ids: continue
        for i, d in enumerate(dl): entries.append((chat_id_str, i, d))

    if not entries:
        text = "📭 你管理的群组里还没有任何接车人。\n可以点击「添加接车人」添加。"
        if query: await safe_edit(query, text, reply_markup=back_to_menu_keyboard())
        else: await message.reply_text(text, reply_markup=back_to_menu_keyboard())
        return

    entries.sort(key=lambda x: (group_title(x[0]), x[1]))
    keyboard = []
    for chat_id_str, idx, d in entries:
        title = group_title(chat_id_str)
        short = title if len(title) <= 14 else title[:12] + "…"
        name = d.get("name", "未命名")
        label = f"🗑️ [{short}] {name}"
        if len(label) > 60: label = label[:58] + "…"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"del|{chat_id_str}|{idx}")])
    keyboard.append([InlineKeyboardButton("🏠 返回主菜单", callback_data="menu")])
    text = "请选择要删除的接车人："
    if query: await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(keyboard))
    else: await message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def show_my_list(context: ContextTypes.DEFAULT_TYPE, user_id: int, admin_groups: list, query=None, message=None) -> None:
    admin_chat_ids = {cid for cid, _, _ in admin_groups}
    has_any = False
    lines = ["📋 <b>你的接车人名单</b>："]
    for chat_id_str in sorted(admin_chat_ids, key=lambda k: group_title(k)):
        dl = drivers_list.get(chat_id_str, [])
        if not dl: continue
        has_any = True
        title = esc(group_title(chat_id_str))
        lines.append(f"\n🌐 <b>{title}</b>（{len(dl)} 人）：")
        for i, d in enumerate(dl, 1):
            mention = build_mention_html(d)
            link = esc(d.get("link", ""), quote=True)
            lines.append(f'  {i}. {mention} - <a href="{link}">链接</a>')

    if not has_any:
        text = "📭 你管理的群组里还没有任何接车人。\n可以点击「添加接车人」添加。"
        if query: await safe_edit(query, text, reply_markup=back_to_menu_keyboard())
        else: await message.reply_text(text, reply_markup=back_to_menu_keyboard())
        return

    body = "\n".join(lines)
    if query:
        await safe_edit(query, body, parse_mode=ParseMode.HTML, disable_web_page_preview=True, reply_markup=back_to_menu_keyboard())
    else:
        await message.reply_text(body, parse_mode=ParseMode.HTML, disable_web_page_preview=True, reply_markup=back_to_menu_keyboard())


async def show_user_groups(context: ContextTypes.DEFAULT_TYPE, user_id: int, query=None, message=None) -> None:
    member_groups = []
    for chat_id_str, title in list_known_groups():
        try: chat_id_int = int(chat_id_str)
        except ValueError: continue
        status = await get_member_status(context, chat_id_int, user_id)
        if status in MEMBER_ROLES: member_groups.append((chat_id_str, title, status))

    if not member_groups:
        text = "🌐 没有找到你所在的群组。\n请先把机器人拉进群，并在群里发一条消息。"
        if query: await safe_edit(query, text, reply_markup=back_to_menu_keyboard())
        else: await message.reply_text(text, reply_markup=back_to_menu_keyboard())
        return

    lines = ["🌐 <b>你所在的群组</b>："]
    for chat_id_str, title, status in member_groups:
        count = len(drivers_list.get(chat_id_str, []))
        lines.append(f"• {esc(title)}（{role_label(status)}，{count} 位接车人）")
    body = "\n".join(lines)
    if query:
        await safe_edit(query, body, parse_mode=ParseMode.HTML, reply_markup=back_to_menu_keyboard())
    else:
        await message.reply_text(body, parse_mode=ParseMode.HTML, reply_markup=back_to_menu_keyboard())


# ------------------------ 按钮回调 ------------------------

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global drivers_list, state
    query = update.callback_query
    try: await query.answer()
    except Exception: pass

    user_id = update.effective_user.id
    chat_type = update.effective_chat.type
    data = query.data or ""

    if chat_type in ("group", "supergroup"):
        await track_group(update.effective_chat)
        await safe_edit(query, "⚠️ 请在私聊中操作。")
        return

    if data == "menu":
        user_states.pop(user_id, None)
        await send_main_menu(context, user_id, query=query)
        return

    ok, admin_groups = await require_admin(update, context, query=query)
    if not ok: return

    if data == "add_start":
        user_states.pop(user_id, None)
        await prompt_choose_group(context, user_id, admin_groups, query=query)
        return

    if data == "del_start":
        user_states.pop(user_id, None)
        await show_delete_list(context, user_id, admin_groups, query=query)
        return

    if data == "my_list":
        await show_my_list(context, user_id, admin_groups, query=query)
        return

    if data == "list_groups":
        await show_user_groups(context, user_id, query=query)
        return

    if data.startswith("grp|"):
        chat_id_str = data.split("|", 1)[1]
        if not any(cid == chat_id_str for cid, _, _ in admin_groups):
            await safe_edit(query, "❌ 你无权管理该群组。")
            return
        user_states[user_id] = {"state": "ASK_NAME", "data": {"target_chat_id": chat_id_str}}
        title = esc(group_title(chat_id_str))
        await safe_edit(query, f"🎯 目标群组：<b>{title}</b>\n\n请输入接车人的 <b>姓名</b>（例如：张三）：\n\n（随时发送 /cancel 取消本次添加）", parse_mode=ParseMode.HTML)
        return

    if data.startswith("del|"):
        parts = data.split("|")
        if len(parts) != 3:
            await safe_edit(query, "⚠️ 无效的删除请求。")
            return
        _, chat_id_str, idx_str = parts
        if not any(cid == chat_id_str for cid, _, _ in admin_groups):
            await safe_edit(query, "❌ 你无权管理该群组。")
            return
        try: idx = int(idx_str)
        except ValueError:
            await safe_edit(query, "⚠️ 无效的删除请求。")
            return
        dl = drivers_list.get(chat_id_str)
        if not dl or idx < 0 or idx >= len(dl):
            await safe_edit(query, "⚠️ 该接车人已不存在，请返回主菜单重新操作。", reply_markup=back_to_menu_keyboard())
            return
        removed = dl.pop(idx)
        await save_drivers()
        if dl: state[chat_id_str] = state.get(chat_id_str, 0) % len(dl)
        else: state.pop(chat_id_str, None)
        await save_state()
        title = esc(group_title(chat_id_str))
        remain = len(drivers_list.get(chat_id_str, []))
        await safe_edit(query, f"✅ 已成功移除接车人：{removed.get('name', '未命名')}。\n所属群组：<b>{title}</b>\n该群当前剩余 {remain} 位接车人。", parse_mode=ParseMode.HTML, reply_markup=back_to_menu_keyboard())
        return


# ------------------------ 群组变更事件 ------------------------

async def on_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cm = update.my_chat_member
    if cm is None or cm.chat is None: return
    chat = cm.chat
    if chat.type not in ("group", "supergroup"): return
    new_status = cm.new_chat_member.status if cm.new_chat_member else None

    if new_status in ("member", "administrator", "creator"):
        await track_group(chat)
        admin_cache.clear()
        logger.info("机器人已加入/身份变更群组: %s (%s) -> %s", chat.title, chat.id, new_status)
    elif new_status in ("left", "kicked"):
        key = str(chat.id)
        dirty = False
        if key in drivers_list:
            del drivers_list[key]; await save_drivers(); dirty = True
        if key in state:
            del state[key]; await save_state(); dirty = True
        if key in groups:
            del groups[key]; await save_groups(); dirty = True
        admin_cache.clear(); member_cache.clear()
        logger.info("机器人已离开群组: %s (%s)，已清理数据=%s", chat.title, chat.id, dirty)


# ------------------------ 消息总入口 ------------------------

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global state, drivers_list
    msg = update.effective_message
    if msg is None or not msg.text: return
    text = msg.text.strip()
    user_id = update.effective_user.id
    chat = update.effective_chat
    chat_type = chat.type

    if chat_type in ("group", "supergroup"): await track_group(chat)

    if chat_type == "private" and user_id in user_states:
        admin_groups = await get_admin_groups(context, user_id)
        if not admin_groups:
            user_states.pop(user_id, None)
            await msg.reply_text("❌ 您没有权限，请联系管理员操作")
            return
        sd = user_states[user_id]
        cur = sd["state"]

        if cur == "ASK_NAME":
            if not text:
                await msg.reply_text("姓名不能为空，请重新输入："); return
            if len(text) > MAX_NAME_LEN:
                await msg.reply_text(f"⚠️ 姓名过长（最多 {MAX_NAME_LEN} 个字符），请重新输入："); return
            sd["data"]["name"] = text
            sd["state"] = "ASK_USERNAME"
            await msg.reply_text("请输入需要 @ 的用户名（例如：Paul_0321，不含 @）。\n如果没有用户名，请发送 /skip：")
            return

        if cur == "ASK_USERNAME":
            if text.lower() in ("/skip", "skip"): sd["data"]["username"] = ""
            else: sd["data"]["username"] = text.lstrip("@").strip()
            sd["state"] = "ASK_LINK"
            await msg.reply_text("请输入要展示的 <b>链接</b>（例如：https://t.me/Paul_0321）：", parse_mode=ParseMode.HTML)
            return

        if cur == "ASK_LINK":
            link = text
            if not link or " " in link:
                await msg.reply_text("⚠️ 链接不能为空且不能包含空格，请重新输入："); return
            d = sd["data"]
            target = d.get("target_chat_id")
            if not target or target not in groups:
                await msg.reply_text("⚠️ 目标群组已失效（机器人可能已退出该群），请重新 /add。")
                user_states.pop(user_id, None); return
            if not any(cid == target for cid, _, _ in admin_groups):
                await msg.reply_text("❌ 你已无权管理该群组，本次添加已取消。")
                user_states.pop(user_id, None); return
            username = d.get("username", "") or ""
            user_id_val: Optional[int] = None
            if username.isdigit():
                user_id_val = int(username); username = ""
            new_driver = {"name": d.get("name", ""), "username": username, "user_id": user_id_val, "link": link, "owner_id": user_id}
            dl = drivers_list.setdefault(target, [])
            title = group_title(target)
            if driver_exists(dl, new_driver):
                await msg.reply_text(f"⚠️ 群「{title}」中已存在相同的接车人（同名 / 同用户名 / 同 ID），本次添加取消。", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ 再添加一个", callback_data="add_start")],[InlineKeyboardButton("🏠 返回主菜单", callback_data="menu")]]))
            else:
                dl.append(new_driver); await save_drivers()
                await msg.reply_text(f"✅ 成功添加接车人：{new_driver['name']}！\n目标群组：{title}\n该群当前共有 {len(dl)} 位接车人。", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ 再添加一个", callback_data="add_start")],[InlineKeyboardButton("🏠 返回主菜单", callback_data="menu")]]))
            user_states.pop(user_id, None); return

    if TRIGGER in text:
        if chat_type not in ("group", "supergroup"): return
        chat_id_str = str(chat.id)
        dl = drivers_list.get(chat_id_str, [])
        if not dl:
            await msg.reply_text("⚠️ 本群还没有配置接车人，请先私聊我使用 /add 添加。"); return
        lock: asyncio.Lock = context.application.bot_data["lock"]
        async with lock:
            idx = state.get(chat_id_str, 0) % len(dl)
            driver = dl[idx]
            car = random.choice(CARS)
            state[chat_id_str] = (idx + 1) % len(dl)
            await save_state()
        mention = build_mention_html(driver)
        body = esc(REPLY_TEMPLATE.format(name=driver.get("name", ""), car=car))
        link = esc(driver.get("link", ""), quote=True)
        reply_text = f'{mention}\n链接：<a href="{link}">{link}</a>\n{body}'
        try:
            await msg.reply_text(reply_text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except Exception as e:
            logger.exception("发送失败：%s", e)


# ==================================================================
#           健康检查 HTTP 服务（Render Web Service 必需）
# ==================================================================

class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK - telegram-che-bot (Neon) running")
    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()
    def log_message(self, format, *args):
        pass


def start_health_server() -> None:
    port_str = os.environ.get("PORT")
    if not port_str:
        logger.info("未检测到 PORT 环境变量，跳过健康检查服务（本地运行模式）")
        return
    try:
        port = int(port_str)
    except ValueError:
        logger.warning("PORT 环境变量无效：%s，跳过健康检查服务", port_str)
        return
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    logger.info("健康检查服务器已启动：http://0.0.0.0:%d/", port)


# ==================================================================
#                          启 动
# ==================================================================

async def post_init(app: Application) -> None:
    app.bot_data["lock"] = asyncio.Lock()
    await init_db()
    await load_all_data()
    logger.info("机器人已启动... 已记录 %d 个群组，共 %d 位接车人", len(groups), total_drivers())


async def post_shutdown(app: Application) -> None:
    await close_db()


def main():
    # 严格检查环境变量
    if not BOT_TOKEN or ":" not in BOT_TOKEN:
        raise SystemExit("❌ 未配置 BOT_TOKEN。请设置环境变量 BOT_TOKEN。")
    if not DATABASE_URL:
        raise SystemExit("❌ 未配置 DATABASE_URL。请设置环境变量 DATABASE_URL (Neon 连接字符串)。")

    start_health_server()

    try:
        app = (
            Application.builder()
            .token(BOT_TOKEN)
            .post_init(post_init)
            .post_shutdown(post_shutdown)
            .build()
        )

        app.add_handler(CommandHandler("start", start_command))
        app.add_handler(CommandHandler("menu", menu_command))
        app.add_handler(CommandHandler("add", add_command))
        app.add_handler(CommandHandler("remove", remove_command))
        app.add_handler(CommandHandler("my", my_command))
        app.add_handler(CommandHandler("list", list_command))
        app.add_handler(CommandHandler("cancel", cancel_command))

        app.add_handler(ChatMemberHandler(on_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
        app.add_handler(CallbackQueryHandler(button_callback))
        app.add_handler(MessageHandler(filters.TEXT, on_message))

        logger.info("🚀 机器人开始运行...")
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception as e:
        logger.exception(f"❌ 机器人运行失败，错误信息: {e}")
        time.sleep(10) # 强制等待 10 秒，防止 Render 频繁重启造成日志刷屏


if __name__ == '__main__':
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except AttributeError:
        pass
        
    try:
        main()
    except (KeyboardInterrupt, SystemExit):
        logger.info("🛑 机器人已停止")
    except Exception as e:
        logger.exception(f"❌ 发生致命错误: {e}")
        time.sleep(5)
