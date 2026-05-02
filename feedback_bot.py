import os
from dotenv import load_dotenv
load_dotenv()
import sqlite3
import time
import threading
import asyncio
import logging
import json
from datetime import datetime, time as dt_time, timezone, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeAllGroupChats, BotCommandScopeChat
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler, ConversationHandler
from telegram.helpers import escape_markdown

# ==================== 配置 ====================
TOKEN = os.getenv("FEEDBACK_BOT_TOKEN")
ADMIN_GROUP_ID = int(os.getenv("FEEDBACK_ADMIN_GROUP", "0"))
SUPER_ADMIN_ID = int(os.getenv("FEEDBACK_SUPER_ADMIN", "0"))
NON_WORKING_START = 2
NON_WORKING_END = 10
MAX_CONCURRENT_USERS = 50
# =============================================

DB_PATH = "feedback.db"

# 对话状态
SETTING_WELCOME = 1
PREVIEW_WELCOME = 2

# 日志配置
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# 并发控制
db_lock = threading.Lock()
user_semaphore = asyncio.Semaphore(MAX_CONCURRENT_USERS)

# ------------------ 数据库初始化和迁移 ------------------
def migrate_database(conn):
    """数据库迁移：添加新字段"""
    cursor = conn.cursor()

    # 检查并添加 welcome_message 表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS welcome_message (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content_type TEXT NOT NULL,
            content TEXT,
            file_id TEXT,
            caption TEXT,
            created_by INTEGER,
            created_at INTEGER,
            updated_at INTEGER
        )
    ''')

    # 检查 messages 表是否有新字段
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='messages'")
    if cursor.fetchone():
        cursor.execute("PRAGMA table_info(messages)")
        columns = [col[1] for col in cursor.fetchall()]

        if 'content_type' not in columns:
            logger.info("迁移数据库：添加 content_type 列")
            cursor.execute("ALTER TABLE messages ADD COLUMN content_type TEXT DEFAULT 'text'")

        if 'file_id' not in columns:
            logger.info("迁移数据库：添加 file_id 列")
            cursor.execute("ALTER TABLE messages ADD COLUMN file_id TEXT")

    conn.commit()

def init_db():
    with sqlite3.connect(DB_PATH, check_same_thread=False) as conn:
        c = conn.cursor()
        # 启用WAL模式以提高并发性能
        c.execute('PRAGMA journal_mode=WAL')
        c.execute('PRAGMA synchronous=NORMAL')
        c.execute('PRAGMA busy_timeout=5000')

        # 创建基础表
        c.execute('''
            CREATE TABLE IF NOT EXISTS reply_mapping (
                msg_id INTEGER PRIMARY KEY,
                user_id INTEGER,
                user_msg_id INTEGER,
                timestamp INTEGER
            )
        ''')

        c.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                direction TEXT,
                content TEXT,
                content_type TEXT DEFAULT 'text',
                file_id TEXT,
                replied INTEGER DEFAULT 0,
                timestamp INTEGER
            )
        ''')

        c.execute('''
            CREATE TABLE IF NOT EXISTS bot_config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')

        # 创建索引提高查询速度
        c.execute('CREATE INDEX IF NOT EXISTS idx_reply_mapping_msg_id ON reply_mapping(msg_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_messages_user_id ON messages(user_id, timestamp)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_messages_replied ON messages(replied)')

        c.execute('INSERT OR IGNORE INTO bot_config (key, value) VALUES (?, ?)', 
                 ('admin_group_id', str(ADMIN_GROUP_ID)))
        conn.commit()

    # 执行数据库迁移
    with sqlite3.connect(DB_PATH, check_same_thread=False) as conn:
        migrate_database(conn)

def get_admin_group_id():
    with db_lock:
        with sqlite3.connect(DB_PATH, check_same_thread=False) as conn:
            c = conn.cursor()
            c.execute('SELECT value FROM bot_config WHERE key = ?', ('admin_group_id',))
            row = c.fetchone()
    return int(row[0]) if row else ADMIN_GROUP_ID

def set_admin_group_id(group_id):
    with db_lock:
        with sqlite3.connect(DB_PATH, check_same_thread=False) as conn:
            c = conn.cursor()
            c.execute('INSERT OR REPLACE INTO bot_config (key, value) VALUES (?, ?)', 
                     ('admin_group_id', str(group_id)))
            conn.commit()

def save_message(user_id, direction, content, content_type='text', file_id=None):
    with db_lock:
        with sqlite3.connect(DB_PATH, check_same_thread=False) as conn:
            c = conn.cursor()
            c.execute('''
                INSERT INTO messages (user_id, direction, content, content_type, file_id, timestamp, replied)
                VALUES (?, ?, ?, ?, ?, ?, 0)
            ''', (user_id, direction, content, content_type, file_id, int(time.time())))
            msg_id = c.lastrowid
            conn.commit()
    return msg_id

def save_reply_mapping(admin_msg_id, user_id, user_msg_id):
    with db_lock:
        with sqlite3.connect(DB_PATH, check_same_thread=False) as conn:
            c = conn.cursor()
            c.execute('INSERT OR REPLACE INTO reply_mapping (msg_id, user_id, user_msg_id, timestamp) VALUES (?, ?, ?, ?)',
                      (admin_msg_id, user_id, user_msg_id, int(time.time())))
            conn.commit()

def get_user_by_reply_msg(admin_msg_id):
    with db_lock:
        with sqlite3.connect(DB_PATH, check_same_thread=False) as conn:
            c = conn.cursor()
            c.execute('SELECT user_id, user_msg_id FROM reply_mapping WHERE msg_id = ?', (admin_msg_id,))
            row = c.fetchone()
    return row if row else (None, None)

def mark_message_replied(msg_id):
    with db_lock:
        with sqlite3.connect(DB_PATH, check_same_thread=False) as conn:
            c = conn.cursor()
            c.execute('UPDATE messages SET replied = 1 WHERE id = ?', (msg_id,))
            conn.commit()

def get_last_message_status(user_id):
    with db_lock:
        with sqlite3.connect(DB_PATH, check_same_thread=False) as conn:
            c = conn.cursor()
            c.execute('''
                SELECT id, content, replied FROM messages
                WHERE user_id = ? AND direction = 'user_to_admin'
                ORDER BY timestamp DESC LIMIT 1
            ''', (user_id,))
            row = c.fetchone()
    return {'id': row[0], 'content': row[1], 'replied': row[2]} if row else None

# ------------------ 欢迎消息管理 ------------------
def save_welcome_message(content_type, content=None, file_id=None, caption=None, created_by=None):
    """保存欢迎消息"""
    with db_lock:
        with sqlite3.connect(DB_PATH, check_same_thread=False) as conn:
            c = conn.cursor()
            # 删除旧消息
            c.execute('DELETE FROM welcome_message')
            # 插入新消息
            c.execute('''
                INSERT INTO welcome_message (content_type, content, file_id, caption, created_by, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (content_type, content, file_id, caption, created_by, int(time.time()), int(time.time())))
            conn.commit()

def get_welcome_message():
    """获取欢迎消息"""
    with db_lock:
        with sqlite3.connect(DB_PATH, check_same_thread=False) as conn:
            c = conn.cursor()
            c.execute('SELECT content_type, content, file_id, caption FROM welcome_message ORDER BY id DESC LIMIT 1')
            row = c.fetchone()
            if row:
                return {
                    'content_type': row[0],
                    'content': row[1],
                    'file_id': row[2],
                    'caption': row[3]
                }
            return None

def delete_welcome_message():
    """删除欢迎消息"""
    with db_lock:
        with sqlite3.connect(DB_PATH, check_same_thread=False) as conn:
            c = conn.cursor()
            c.execute('DELETE FROM welcome_message')
            conn.commit()

# ------------------ 工作时间判断 ------------------
def is_working_time():
    beijing_tz = timezone(timedelta(hours=8))
    now = datetime.now(beijing_tz)
    hour = now.time().hour
    return not (NON_WORKING_START <= hour < NON_WORKING_END)

async def send_typing(chat_id, context):
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

# ------------------ 并发消息处理包装器 ------------------
async def process_message_with_semaphore(user_id, callback, *args, **kwargs):
    async with user_semaphore:
        try:
            current_concurrent = MAX_CONCURRENT_USERS - user_semaphore._value
            logger.info(f"开始处理用户 {user_id} 的消息，当前并发数: {current_concurrent}")
            return await callback(*args, **kwargs)
        except Exception as e:
            logger.error(f"处理用户 {user_id} 消息时出错: {e}")
            raise
        finally:
            current_concurrent = MAX_CONCURRENT_USERS - user_semaphore._value
            logger.info(f"完成处理用户 {user_id} 的消息，当前并发数: {current_concurrent}")

# ------------------ 发送欢迎消息给用户 ------------------
async def send_welcome_to_user(chat_id, context):
    """向用户发送欢迎消息"""
    welcome = get_welcome_message()

    if not welcome:
        # 没有自定义欢迎消息，发送默认消息
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "🤖 反馈机器人已启动\n\n"
                "你可以直接发送消息给我，管理员会收到并回复你。\n\n"
                "✨ 支持同时处理多人消息，无需等待！\n\n"
                f"📌 非工作时间（{NON_WORKING_START}:00 - {NON_WORKING_END}:00）消息会延迟处理。"
            )
        )
        return

    try:
        content_type = welcome['content_type']

        if content_type == 'text':
            # 文字欢迎消息
            await context.bot.send_message(
                chat_id=chat_id,
                text=welcome['content']
            )
        elif content_type == 'photo':
            # 图片欢迎消息
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=welcome['file_id'],
                caption=welcome['caption'] or ""
            )
        elif content_type == 'video':
            # 视频欢迎消息
            await context.bot.send_video(
                chat_id=chat_id,
                video=welcome['file_id'],
                caption=welcome['caption'] or ""
            )
        elif content_type == 'animation':
            # GIF动图欢迎消息
            await context.bot.send_animation(
                chat_id=chat_id,
                animation=welcome['file_id'],
                caption=welcome['caption'] or ""
            )
        elif content_type == 'document':
            # 文件欢迎消息
            await context.bot.send_document(
                chat_id=chat_id,
                document=welcome['file_id'],
                caption=welcome['caption'] or ""
            )
        elif content_type == 'voice':
            # 语音欢迎消息
            await context.bot.send_voice(
                chat_id=chat_id,
                voice=welcome['file_id'],
                caption=welcome['caption'] or ""
            )
        elif content_type == 'audio':
            # 音频欢迎消息
            await context.bot.send_audio(
                chat_id=chat_id,
                audio=welcome['file_id'],
                caption=welcome['caption'] or ""
            )
        else:
            # 未知类型，发送默认消息
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "🤖 反馈机器人已启动\n\n"
                    "你可以直接发送消息给我，管理员会收到并回复你。\n\n"
                    f"📌 非工作时间（{NON_WORKING_START}:00 - {NON_WORKING_END}:00）消息会延迟处理。"
                )
            )
    except Exception as e:
        logger.error(f"发送欢迎消息失败: {e}")
        # 发送失败时显示默认消息
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "🤖 反馈机器人已启动\n\n"
                "你可以直接发送消息给我，管理员会收到并回复你。\n\n"
                f"📌 非工作时间（{NON_WORKING_START}:00 - {NON_WORKING_END}:00）消息会延迟处理。"
            )
        )

# ------------------ 预览欢迎消息 ------------------
async def preview_welcome(chat_id, context):
    """预览当前设置的欢迎消息"""
    welcome = get_welcome_message()

    if not welcome:
        await context.bot.send_message(
            chat_id=chat_id,
            text="📝 **当前没有设置欢迎消息**\n\n用户启动时会看到默认消息。",
            parse_mode="Markdown"
        )
        return

    try:
        content_type = welcome['content_type']

        # 先发送提示
        await context.bot.send_message(
            chat_id=chat_id,
            text="📝 **当前欢迎消息预览：**",
            parse_mode="Markdown"
        )

        if content_type == 'text':
            await context.bot.send_message(
                chat_id=chat_id,
                text=welcome['content']
            )
        elif content_type == 'photo':
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=welcome['file_id'],
                caption=welcome['caption'] or ""
            )
        elif content_type == 'video':
            await context.bot.send_video(
                chat_id=chat_id,
                video=welcome['file_id'],
                caption=welcome['caption'] or ""
            )
        elif content_type == 'animation':
            await context.bot.send_animation(
                chat_id=chat_id,
                animation=welcome['file_id'],
                caption=welcome['caption'] or ""
            )
        elif content_type == 'document':
            await context.bot.send_document(
                chat_id=chat_id,
                document=welcome['file_id'],
                caption=welcome['caption'] or ""
            )
        elif content_type == 'voice':
            await context.bot.send_voice(
                chat_id=chat_id,
                voice=welcome['file_id']
            )
            if welcome['caption']:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=welcome['caption']
                )
        elif content_type == 'audio':
            await context.bot.send_audio(
                chat_id=chat_id,
                audio=welcome['file_id'],
                caption=welcome['caption'] or ""
            )
    except Exception as e:
        logger.error(f"预览欢迎消息失败: {e}")
        await context.bot.send_message(
            chat_id=chat_id,
            text="❌ 预览失败，欢迎消息可能已失效。"
        )

# ------------------ 命令自动设置 ------------------
async def setup_bot_commands(app):
    """自动设置机器人命令菜单"""
    try:
        # 私聊命令
        private_commands = [
            BotCommand("start", "🤖 启动机器人，查看欢迎消息"),
            BotCommand("id", "🆔 获取你的用户ID"),
            BotCommand("status", "📊 查询消息回复状态"),
        ]

        # 普通群组命令
        group_commands = [
            BotCommand("pending", "📬 查看待回复消息数"),
            BotCommand("concurrent", "🔄 查看并发处理状态"),
            BotCommand("id", "🆔 获取群组ID"),
            BotCommand("help", "❓ 查看帮助信息"),
        ]

        # 1. 设置私聊命令
        await app.bot.set_my_commands(
            commands=private_commands,
            scope=BotCommandScopeAllPrivateChats()
        )
        logger.info("✅ 私聊命令菜单已设置")
        print("✅ 私聊命令菜单已设置")

        # 2. 设置所有群组命令（不包含管理功能）
        await app.bot.set_my_commands(
            commands=group_commands,
            scope=BotCommandScopeAllGroupChats()
        )
        logger.info("✅ 群组基础命令菜单已设置")
        print("✅ 群组基础命令菜单已设置")

        # 3. 为管理员群组设置特殊命令
        admin_group_id = get_admin_group_id()
        if admin_group_id:
            admin_commands = group_commands + [
                BotCommand("setwelcome", "📝 设置自定义欢迎消息"),
                BotCommand("preview", "👁️ 预览当前欢迎消息"),
                BotCommand("delwelcome", "🗑️ 删除欢迎消息"),
                BotCommand("setgroup", "⚙️ 设置管理员群组"),
            ]
            await app.bot.set_my_commands(
                commands=admin_commands,
                scope=BotCommandScopeChat(chat_id=admin_group_id)
            )
            logger.info(f"✅ 管理员群组命令菜单已设置 (ID: {admin_group_id})")
            print(f"✅ 管理员群组命令菜单已设置 (ID: {admin_group_id})")

        logger.info("🎉 所有命令菜单设置完成！")
        print("🎉 所有命令菜单设置完成！")
        print("现在用户在输入 / 时会自动显示可用命令。")

    except Exception as e:
        logger.error(f"设置命令菜单失败: {e}")
        print(f"❌ 设置命令菜单失败: {e}")
        print("请手动运行 setup_commands.py 设置命令菜单")

# ------------------ 用户命令 ------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """启动命令 - 直接发送欢迎消息"""
    await send_welcome_to_user(update.effective_chat.id, context)

async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    chat_id = chat.id
    chat_type = chat.type

    if chat_type == "private":
        user = update.effective_user
        user_id = user.id
        first_name = user.first_name or ""
        last_name = user.last_name or ""
        full_name = f"{first_name} {last_name}".strip()
        username = f"@{user.username}" if user.username else "未设置"

        await update.message.reply_text(
            f"📌 **你的用户信息**\n\n"
            f"👤 昵称: {full_name}\n"
            f"🆔 用户名: {username}\n"
            f"🔢 用户ID: `{user_id}`\n\n"
            f"💡 这个ID是你的唯一标识。",
            parse_mode="Markdown"
        )
    elif chat_type in ["group", "supergroup"]:
        group_name = chat.title or "未命名群组"
        await update.message.reply_text(
            f"📌 **群组信息**\n\n"
            f"📛 群组名称: {group_name}\n"
            f"🆔 群组ID: `{chat_id}`\n\n"
            f"💡 复制这个ID（包括负号）用于配置。",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(f"当前聊天ID: `{chat_id}`\n聊天类型: {chat_type}", parse_mode="Markdown")

async def my_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    last = get_last_message_status(user_id)
    if not last:
        await update.message.reply_text("你尚未发送过消息。")
        return
    if last['replied']:
        await update.message.reply_text(f"✅ 你最后一条消息已收到回复。\n内容: {last['content'][:100]}")
    else:
        await update.message.reply_text(f"⏳ 你最后一条消息还未被回复，请耐心等待。\n内容: {last['content'][:100]}")

# ------------------ 超级管理员命令 ------------------
async def set_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != SUPER_ADMIN_ID:
        await update.message.reply_text("❌ 只有超级管理员可以使用此命令。")
        return
    args = context.args
    if not args:
        await update.message.reply_text("用法: /setgroup <群组ID>\n\n例如: /setgroup -1001234567890")
        return
    try:
        new_group_id = int(args[0])
        set_admin_group_id(new_group_id)
        await update.message.reply_text(f"✅ 管理员群组已修改为: {new_group_id}\n\n请确保机器人已加入该群组。")
        logger.info(f"管理员群组已修改为: {new_group_id}")
    except ValueError:
        await update.message.reply_text("❌ 群组ID必须是数字（负数）。")

# ------------------ 欢迎消息设置（对话流程） ------------------
async def set_welcome_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """开始设置欢迎消息 - 只在管理员群组中可用"""
    chat_id = update.effective_chat.id

    # 检查是否在管理员群组中
    if str(chat_id) != str(get_admin_group_id()):
        await update.message.reply_text("❌ 此命令只能在管理员群组中使用。")
        return ConversationHandler.END

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ 取消设置", callback_data="cancel_welcome")]
    ])

    await update.message.reply_text(
        "📝 **设置自定义欢迎消息**\n\n"
        "请直接发送你想要设置的欢迎消息内容。\n\n"
        "支持以下类型：\n"
        "• 📝 文字消息\n"
        "• 🖼️ 图片（可带说明）\n"
        "• 🎬 视频（可带说明）\n"
        "• 🎞️ GIF动图\n"
        "• 📄 文件\n"
        "• 🎤 语音消息\n"
        "• 🎵 音频消息\n\n"
        "点击下方按钮取消设置。",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

    return SETTING_WELCOME

async def set_welcome_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """接收欢迎消息内容并预览"""
    message = update.message
    user_id = update.effective_user.id

    # 存储消息数据到 context.user_data
    context.user_data['welcome_temp'] = {}

    try:
        if message.text:
            context.user_data['welcome_temp'] = {
                'type': 'text',
                'content': message.text
            }
        elif message.photo:
            context.user_data['welcome_temp'] = {
                'type': 'photo',
                'file_id': message.photo[-1].file_id,
                'caption': message.caption or ""
            }
        elif message.video:
            context.user_data['welcome_temp'] = {
                'type': 'video',
                'file_id': message.video.file_id,
                'caption': message.caption or ""
            }
        elif message.animation:
            context.user_data['welcome_temp'] = {
                'type': 'animation',
                'file_id': message.animation.file_id,
                'caption': message.caption or ""
            }
        elif message.document:
            context.user_data['welcome_temp'] = {
                'type': 'document',
                'file_id': message.document.file_id,
                'caption': message.caption or "",
                'file_name': message.document.file_name or "文件"
            }
        elif message.voice:
            context.user_data['welcome_temp'] = {
                'type': 'voice',
                'file_id': message.voice.file_id,
                'caption': message.caption or ""
            }
        elif message.audio:
            context.user_data['welcome_temp'] = {
                'type': 'audio',
                'file_id': message.audio.file_id,
                'caption': message.caption or ""
            }
        else:
            await message.reply_text("❌ 不支持的消息类型，请重新设置。")
            return SETTING_WELCOME

        # 显示预览
        welcome_data = context.user_data['welcome_temp']
        content_type = welcome_data['type']

        # 发送预览提示
        await message.reply_text("📝 **欢迎消息预览：**", parse_mode="Markdown")

        # 发送预览内容
        if content_type == 'text':
            await message.reply_text(welcome_data['content'])
        elif content_type == 'photo':
            await message.reply_photo(
                photo=welcome_data['file_id'],
                caption=welcome_data.get('caption', '')
            )
        elif content_type == 'video':
            await message.reply_video(
                video=welcome_data['file_id'],
                caption=welcome_data.get('caption', '')
            )
        elif content_type == 'animation':
            await message.reply_animation(
                animation=welcome_data['file_id'],
                caption=welcome_data.get('caption', '')
            )
        elif content_type == 'document':
            await message.reply_document(
                document=welcome_data['file_id'],
                caption=welcome_data.get('caption', '')
            )
        elif content_type == 'voice':
            await message.reply_voice(
                voice=welcome_data['file_id']
            )
            if welcome_data.get('caption'):
                await message.reply_text(welcome_data['caption'])
        elif content_type == 'audio':
            await message.reply_audio(
                audio=welcome_data['file_id'],
                caption=welcome_data.get('caption', '')
            )

        # 发送确认按钮
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ 确认设置", callback_data="confirm_welcome"),
                InlineKeyboardButton("🔄 重新设置", callback_data="reset_welcome"),
            ],
            [InlineKeyboardButton("❌ 取消", callback_data="cancel_welcome")]
        ])

        await message.reply_text(
            "👆 这是欢迎消息的预览效果\n\n"
            "用户点击 /start 时就会看到这个消息。\n"
            "请选择操作：",
            reply_markup=keyboard
        )

        return PREVIEW_WELCOME

    except Exception as e:
        logger.error(f"预览欢迎消息失败: {e}")
        await message.reply_text("❌ 预览失败，请重试。")
        return SETTING_WELCOME

async def confirm_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """确认设置欢迎消息"""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    welcome_data = context.user_data.get('welcome_temp')

    if not welcome_data:
        await query.edit_message_text("❌ 设置已过期，请使用 /setwelcome 重新设置。")
        return ConversationHandler.END

    try:
        content_type = welcome_data['type']

        if content_type == 'text':
            save_welcome_message('text', content=welcome_data['content'], created_by=user_id)
        else:
            save_welcome_message(
                content_type,
                file_id=welcome_data['file_id'],
                caption=welcome_data.get('caption', ''),
                created_by=user_id
            )

        await query.edit_message_text(
            "✅ **欢迎消息设置成功！**\n\n"
            "用户现在点击 /start 就会看到这条消息。",
            parse_mode="Markdown"
        )

        logger.info(f"管理员 {user_id} 设置了新的欢迎消息，类型: {content_type}")

        # 清除临时数据
        context.user_data.pop('welcome_temp', None)

        return ConversationHandler.END

    except Exception as e:
        logger.error(f"保存欢迎消息失败: {e}")
        await query.edit_message_text("❌ 保存失败，请重试。")
        return ConversationHandler.END

async def reset_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """重新设置欢迎消息"""
    query = update.callback_query
    await query.answer()

    # 清除临时数据
    context.user_data.pop('welcome_temp', None)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ 取消设置", callback_data="cancel_welcome")]
    ])

    await query.edit_message_text(
        "📝 请重新发送欢迎消息内容。",
        reply_markup=keyboard
    )

    return SETTING_WELCOME

async def cancel_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """取消设置欢迎消息"""
    query = update.callback_query
    await query.answer()

    # 清除临时数据
    context.user_data.pop('welcome_temp', None)

    await query.edit_message_text("❌ 已取消设置欢迎消息。")
    return ConversationHandler.END

# ------------------ 预览和删除欢迎消息 ------------------
async def preview_welcome_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """预览当前欢迎消息（管理员命令）"""
    chat_id = update.effective_chat.id

    if str(chat_id) != str(get_admin_group_id()):
        await update.message.reply_text("❌ 此命令只能在管理员群组中使用。")
        return

    welcome = get_welcome_message()

    if not welcome:
        await update.message.reply_text(
            "📝 **当前没有设置自定义欢迎消息**\n\n"
            "用户启动时会看到默认消息。\n\n"
            "使用 /setwelcome 设置欢迎消息。",
            parse_mode="Markdown"
        )
        return

    await update.message.reply_text("📝 **当前欢迎消息预览：**", parse_mode="Markdown")

    try:
        content_type = welcome['content_type']

        if content_type == 'text':
            await update.message.reply_text(welcome['content'])
        elif content_type == 'photo':
            await update.message.reply_photo(
                photo=welcome['file_id'],
                caption=welcome.get('caption', '')
            )
        elif content_type == 'video':
            await update.message.reply_video(
                video=welcome['file_id'],
                caption=welcome.get('caption', '')
            )
        elif content_type == 'animation':
            await update.message.reply_animation(
                animation=welcome['file_id'],
                caption=welcome.get('caption', '')
            )
        elif content_type == 'document':
            await update.message.reply_document(
                document=welcome['file_id'],
                caption=welcome.get('caption', '')
            )
        elif content_type == 'voice':
            await update.message.reply_voice(voice=welcome['file_id'])
            if welcome.get('caption'):
                await update.message.reply_text(welcome['caption'])
        elif content_type == 'audio':
            await update.message.reply_audio(
                audio=welcome['file_id'],
                caption=welcome.get('caption', '')
            )
    except Exception as e:
        logger.error(f"预览欢迎消息失败: {e}")
        await update.message.reply_text("❌ 预览失败，欢迎消息可能已失效。")

async def delete_welcome_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """删除欢迎消息"""
    chat_id = update.effective_chat.id

    if str(chat_id) != str(get_admin_group_id()):
        await update.message.reply_text("❌ 此命令只能在管理员群组中使用。")
        return

    welcome = get_welcome_message()
    if not welcome:
        await update.message.reply_text("❌ 当前没有设置欢迎消息。")
        return

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ 确认删除", callback_data="confirm_delete_welcome"),
            InlineKeyboardButton("❌ 取消", callback_data="cancel_delete_welcome")
        ]
    ])

    await update.message.reply_text(
        "⚠️ **确定要删除当前欢迎消息吗？**\n\n"
        "删除后，用户启动时将看到默认消息。",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

async def confirm_delete_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """确认删除欢迎消息"""
    query = update.callback_query
    await query.answer()

    delete_welcome_message()
    await query.edit_message_text("✅ 欢迎消息已删除。用户启动时将看到默认消息。")
    logger.info("欢迎消息已删除")

async def cancel_delete_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """取消删除欢迎消息"""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("❌ 已取消删除。")

# ------------------ 状态查询回调 ------------------
async def status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data_parts = query.data.split("_")
    if len(data_parts) < 2:
        await query.answer("❌ 无效的查询", show_alert=True)
        return

    user_id = int(data_parts[1])
    if query.from_user.id != user_id:
        await query.answer("❌ 这不是你的消息", show_alert=True)
        return

    last = get_last_message_status(user_id)
    if not last:
        await query.edit_message_text("你尚未发送过消息。")
    elif last['replied']:
        await query.edit_message_text(f"✅ 你最后一条消息已收到回复。\n内容: {last['content'][:100]}")
    else:
        await query.edit_message_text(f"⏳ 你最后一条消息还未被回复，请耐心等待。\n内容: {last['content'][:100]}")

# ------------------ 消息转发（支持并发） ------------------
async def _forward_to_admin_impl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.message
    user_id = user.id

    name = escape_markdown(user.first_name or "", version=2)
    username = f"@{escape_markdown(user.username or '', version=2)}" if user.username else "无"

    forward_text_template = (
        f"📨 **新消息**\n"
        f"👤 昵称: {name}\n"
        f"🆔 用户名: {username}\n"
        f"🔢 ID: `{user_id}`\n"
        f"💬 内容: {{}}\n\n"
        f"👇 回复此消息即可回答用户。"
    )

    admin_group = get_admin_group_id()

    try:
        if message.text:
            content = escape_markdown(message.text, version=2)
            if len(content) > 500:
                content = content[:500] + "..."

            forward_text = forward_text_template.format(content)
            sent = await context.bot.send_message(
                chat_id=admin_group,
                text=forward_text,
                parse_mode="MarkdownV2"
            )
            msg_id = save_message(user_id, "user_to_admin", message.text[:1000], 'text')
            save_reply_mapping(sent.message_id, user_id, msg_id)

        elif message.photo:
            file_id = message.photo[-1].file_id
            caption = escape_markdown(message.caption or "无说明", version=2)
            if len(caption) > 200:
                caption = caption[:200] + "..."

            forward_text = forward_text_template.format(f"📷 图片\n说明: {caption}")
            sent = await context.bot.send_photo(
                chat_id=admin_group,
                photo=file_id,
                caption=forward_text,
                parse_mode="MarkdownV2"
            )
            msg_id = save_message(user_id, "user_to_admin", f"📷 图片: {message.caption or '无说明'}", 'photo', file_id)
            save_reply_mapping(sent.message_id, user_id, msg_id)

        elif message.video:
            file_id = message.video.file_id
            caption = escape_markdown(message.caption or "无说明", version=2)
            if len(caption) > 200:
                caption = caption[:200] + "..."

            forward_text = forward_text_template.format(f"🎬 视频\n说明: {caption}")
            sent = await context.bot.send_video(
                chat_id=admin_group,
                video=file_id,
                caption=forward_text,
                parse_mode="MarkdownV2"
            )
            msg_id = save_message(user_id, "user_to_admin", f"🎬 视频: {message.caption or '无说明'}", 'video', file_id)
            save_reply_mapping(sent.message_id, user_id, msg_id)

        elif message.document:
            file_id = message.document.file_id
            file_name = escape_markdown(message.document.file_name or "未命名文件", version=2)
            caption = escape_markdown(message.caption or "无说明", version=2)
            if len(caption) > 200:
                caption = caption[:200] + "..."

            forward_text = forward_text_template.format(f"📄 文件: {file_name}\n说明: {caption}")
            sent = await context.bot.send_document(
                chat_id=admin_group,
                document=file_id,
                caption=forward_text,
                parse_mode="MarkdownV2"
            )
            msg_id = save_message(user_id, "user_to_admin", f"📄 文件 {file_name}: {message.caption or '无说明'}", 'document', file_id)
            save_reply_mapping(sent.message_id, user_id, msg_id)

        elif message.voice:
            file_id = message.voice.file_id

            forward_text = forward_text_template.format("🎤 语音消息")
            sent_text = await context.bot.send_message(
                chat_id=admin_group,
                text=forward_text,
                parse_mode="MarkdownV2"
            )
            await context.bot.send_voice(
                chat_id=admin_group,
                voice=file_id
            )
            msg_id = save_message(user_id, "user_to_admin", "🎤 语音消息", 'voice', file_id)
            save_reply_mapping(sent_text.message_id, user_id, msg_id)

        elif message.sticker:
            file_id = message.sticker.file_id
            emoji = message.sticker.emoji or "未知"

            forward_text = forward_text_template.format(f"🏷️ 贴纸 ({emoji})")
            sent = await context.bot.send_sticker(
                chat_id=admin_group,
                sticker=file_id
            )
            sent_text = await context.bot.send_message(
                chat_id=admin_group,
                text=forward_text,
                parse_mode="MarkdownV2"
            )
            msg_id = save_message(user_id, "user_to_admin", f"🏷️ 贴纸 ({emoji})", 'sticker', file_id)
            save_reply_mapping(sent_text.message_id, user_id, msg_id)

        else:
            await update.message.reply_text("❌ 暂不支持此类型消息")
            return

        logger.info(f"消息已转发到管理员群组，用户ID: {user_id}, 消息ID: {msg_id}")

    except Exception as e:
        logger.error(f"转发失败: {e}")
        await update.message.reply_text(f"❌ 转发失败，请稍后重试。")
        return

    await send_typing(update.effective_chat.id, context)
    if is_working_time():
        await update.message.reply_text("✅ 消息已收到，管理员会尽快回复。")
    else:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 查看回复状态", callback_data=f"status_{user_id}")]
        ])
        await update.message.reply_text(
            f"⏰ 当前是非工作时间（{NON_WORKING_START}:00 - {NON_WORKING_END}:00），消息已记录，点击下方按钮查看状态。",
            reply_markup=keyboard
        )

async def forward_to_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await process_message_with_semaphore(user_id, _forward_to_admin_impl, update, context)

# ------------------ 管理员回复 ------------------
async def handle_admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(get_admin_group_id()):
        return

    message = update.message

    if not message.reply_to_message:
        return

    original_msg_id = message.reply_to_message.message_id
    user_id, user_msg_id = get_user_by_reply_msg(original_msg_id)
    if not user_id:
        return

    try:
        if message.text:
            reply_content = message.text
            await context.bot.send_message(
                chat_id=user_id,
                text=f"📬 **管理员回复**：\n{reply_content}",
                parse_mode="Markdown"
            )
            save_message(user_id, "admin_to_user", reply_content, 'text')

        elif message.photo:
            file_id = message.photo[-1].file_id
            caption = message.caption or ""
            await context.bot.send_photo(
                chat_id=user_id,
                photo=file_id,
                caption=f"📬 **管理员回复**：\n{caption}" if caption else "📬 **管理员回复**",
                parse_mode="Markdown"
            )
            save_message(user_id, "admin_to_user", f"📷 图片: {caption}" if caption else "📷 图片", 'photo', file_id)

        elif message.video:
            file_id = message.video.file_id
            caption = message.caption or ""
            await context.bot.send_video(
                chat_id=user_id,
                video=file_id,
                caption=f"📬 **管理员回复**：\n{caption}" if caption else "📬 **管理员回复**",
                parse_mode="Markdown"
            )
            save_message(user_id, "admin_to_user", f"🎬 视频: {caption}" if caption else "🎬 视频", 'video', file_id)

        elif message.document:
            file_id = message.document.file_id
            file_name = message.document.file_name or "文件"
            caption = message.caption or ""
            await context.bot.send_document(
                chat_id=user_id,
                document=file_id,
                caption=f"📬 **管理员回复**：{file_name}\n{caption}" if caption else f"📬 **管理员回复**：{file_name}",
                parse_mode="Markdown"
            )
            save_message(user_id, "admin_to_user", f"📄 文件 {file_name}: {caption}" if caption else f"📄 文件 {file_name}", 'document', file_id)

        elif message.voice:
            file_id = message.voice.file_id
            await context.bot.send_voice(
                chat_id=user_id,
                voice=file_id
            )
            await context.bot.send_message(
                chat_id=user_id,
                text="📬 **管理员回复了一条语音消息**",
                parse_mode="Markdown"
            )
            save_message(user_id, "admin_to_user", "🎤 语音消息", 'voice', file_id)

        elif message.sticker:
            file_id = message.sticker.file_id
            await context.bot.send_sticker(
                chat_id=user_id,
                sticker=file_id
            )
            await context.bot.send_message(
                chat_id=user_id,
                text="📬 **管理员回复了一个贴纸**",
                parse_mode="Markdown"
            )
            save_message(user_id, "admin_to_user", "🏷️ 贴纸", 'sticker', file_id)

        else:
            return

        mark_message_replied(user_msg_id)

        try:
            await message.reply_text("✅ 回复已发送")
        except:
            pass

        logger.info(f"管理员回复已发送，用户ID: {user_id}, 原消息ID: {user_msg_id}")

    except Exception as e:
        logger.error(f"回复失败: {e}")
        try:
            await message.reply_text(f"❌ 发送失败: {str(e)[:100]}")
        except:
            pass

# ------------------ 附加功能 ------------------
async def pending_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(get_admin_group_id()):
        return

    with db_lock:
        with sqlite3.connect(DB_PATH, check_same_thread=False) as conn:
            c = conn.cursor()
            c.execute('SELECT COUNT(*) FROM messages WHERE direction = \'user_to_admin\' AND replied = 0')
            count = c.fetchone()[0]

    await update.message.reply_text(f"📊 当前待回复消息数: {count}")

async def concurrent_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) != str(get_admin_group_id()):
        return

    current_concurrent = MAX_CONCURRENT_USERS - user_semaphore._value
    await update.message.reply_text(
        f"🔄 并发状态：\n"
        f"当前处理中: {current_concurrent}\n"
        f"最大并发: {MAX_CONCURRENT_USERS}\n"
        f"可用槽位: {user_semaphore._value}"
    )

# ------------------ 主函数 ------------------
def main():
    # 初始化数据库（包含迁移）
    init_db()

    # 创建应用，配置并发参数
    app = (
        Application.builder()
        .token(TOKEN)
        .concurrent_updates(True)
        .pool_timeout(20.0)
        .build()
    )

    # 设置欢迎消息的对话处理
    welcome_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("setwelcome", set_welcome_start)],
        states={
            SETTING_WELCOME: [
                MessageHandler(
                    filters.TEXT | filters.PHOTO | filters.VIDEO | 
                    filters.Document.ALL | filters.VOICE | filters.AUDIO |
                    filters.ANIMATION,
                    set_welcome_receive
                )
            ],
            PREVIEW_WELCOME: [
                CallbackQueryHandler(confirm_welcome, pattern="^confirm_welcome$"),
                CallbackQueryHandler(reset_welcome, pattern="^reset_welcome$"),
                CallbackQueryHandler(cancel_welcome, pattern="^cancel_welcome$"),
            ],
        },
        fallbacks=[CallbackQueryHandler(cancel_welcome, pattern="^cancel_welcome$")],
    )

    # 添加命令处理器
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("status", my_status))
    app.add_handler(CommandHandler("setgroup", set_group))
    app.add_handler(CommandHandler("pending", pending_stats))
    app.add_handler(CommandHandler("concurrent", concurrent_status))
    app.add_handler(CommandHandler("preview", preview_welcome_cmd))
    app.add_handler(CommandHandler("delwelcome", delete_welcome_cmd))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(welcome_conv_handler)

    # 删除欢迎消息的回调查询
    app.add_handler(CallbackQueryHandler(confirm_delete_welcome, pattern="^confirm_delete_welcome$"))
    app.add_handler(CallbackQueryHandler(cancel_delete_welcome, pattern="^cancel_delete_welcome$"))

    # 用户私聊消息（非命令）
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & ~filters.COMMAND, 
        forward_to_admin
    ))

    # 群组消息处理
    app.add_handler(MessageHandler(
        filters.ChatType.GROUPS & filters.REPLY & ~filters.COMMAND,
        handle_admin_reply
    ))

    # 回调查询
    app.add_handler(CallbackQueryHandler(status_callback, pattern="^status_"))

    # 添加生命周期钩子 - 启动时自动设置命令
    async def post_init(app):
        """应用初始化完成后执行"""
        logger.info("正在设置命令菜单...")
        await setup_bot_commands(app)

    # 注册后初始化钩子
    app.post_init = post_init

    # 启动信息
    admin_group = get_admin_group_id()
    logger.info("🤖 反馈机器人启动中...")

    print("\n" + "="*50)
    print("🤖 反馈机器人启动中...")
    print("="*50)
    print(f"📋 管理员群组ID: {admin_group}")
    print(f"👑 超级管理员ID: {SUPER_ADMIN_ID}")
    print(f"👥 最大并发用户数: {MAX_CONCURRENT_USERS}")
    print(f"⏰ 工作时间: {NON_WORKING_END}:00 - 次日{NON_WORKING_START}:00")
    print("="*50)
    print("✅ 机器人启动成功！")
    print("💡 命令菜单将在启动后自动设置")
    print("📱 现在可以向机器人发送消息了")
    print("⏹️  按 Ctrl+C 停止")
    print("="*50 + "\n")

    # 启动轮询
    app.run_polling(allowed_updates=Update.ALL_TYPES)

# 添加帮助命令
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """显示帮助信息"""
    chat_type = update.effective_chat.type

    if chat_type == "private":
        help_text = (
            "🤖 **反馈机器人使用帮助**\n\n"
            "📝 **基本使用：**\n"
            "• 直接发送消息即可，管理员会看到并回复\n"
            "• 支持文字、图片、视频、文件、语音等\n\n"
            "📋 **可用命令：**\n"
            "• /start - 查看欢迎消息\n"
            "• /id - 获取你的用户ID\n"
            "• /status - 查询消息回复状态\n\n"
            "⏰ **工作时间：**\n"
            f"• {NON_WORKING_END}:00 - 次日{NON_WORKING_START}:00 为工作时间\n"
            "• 非工作时间消息会延迟处理\n\n"
            "💡 **提示：**\n"
            "• 请耐心等待管理员回复\n"
            "• 可以发送多条消息"
        )
    else:
        help_text = (
            "🤖 **群组反馈机器人帮助**\n\n"
            "📋 **可用命令：**\n"
            "• /pending - 查看待回复消息数\n"
            "• /concurrent - 查看并发处理状态\n"
            "• /id - 获取当前群组ID\n\n"
            "💡 **管理员功能：**\n"
            "• 回复机器人转发的消息来回复用户\n"
            "• /setwelcome - 设置欢迎消息\n"
            "• /preview - 预览欢迎消息\n"
            "• /delwelcome - 删除欢迎消息"
        )

    await update.message.reply_text(help_text, parse_mode="Markdown")

if __name__ == "__main__":
    main()
