import sqlite3
import time
from datetime import datetime, time as dt_time, timezone, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from telegram.helpers import escape_markdown

# ==================== 配置 ====================
TOKEN = "8781850872:AAFcGdfKXv8ktPbTiUNBHvzBBm0uO2R7EoE"
ADMIN_GROUP_ID = -3939997685        # 管理员群组ID（机器人必须加入）
SUPER_ADMIN_ID = 8107909168             # 超级管理员的用户ID
NON_WORKING_START = 2                   # 凌晨2点开始休息
NON_WORKING_END = 10                    # 上午10点结束休息
# =============================================

DB_PATH = "feedback.db"

# ------------------ 数据库初始化 ------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
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
    conn.commit()
    c.execute('INSERT OR IGNORE INTO bot_config (key, value) VALUES (?, ?)', ('admin_group_id', str(ADMIN_GROUP_ID)))
    conn.commit()
    conn.close()

def get_admin_group_id():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT value FROM bot_config WHERE key = ?', ('admin_group_id',))
    row = c.fetchone()
    conn.close()
    return int(row[0]) if row else ADMIN_GROUP_ID

def set_admin_group_id(group_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO bot_config (key, value) VALUES (?, ?)', ('admin_group_id', str(group_id)))
    conn.commit()
    conn.close()

def save_message(user_id, direction, content):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        INSERT INTO messages (user_id, direction, content, timestamp, replied)
        VALUES (?, ?, ?, ?, 0)
    ''', (user_id, direction, content, int(time.time())))
    msg_id = c.lastrowid
    conn.commit()
    conn.close()
    return msg_id

def save_reply_mapping(admin_msg_id, user_id, user_msg_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO reply_mapping (msg_id, user_id, user_msg_id, timestamp) VALUES (?, ?, ?, ?)',
              (admin_msg_id, user_id, user_msg_id, int(time.time())))
    conn.commit()
    conn.close()

def get_user_by_reply_msg(admin_msg_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT user_id, user_msg_id FROM reply_mapping WHERE msg_id = ?', (admin_msg_id,))
    row = c.fetchone()
    conn.close()
    return row if row else (None, None)

def mark_message_replied(msg_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('UPDATE messages SET replied = 1 WHERE id = ?', (msg_id,))
    conn.commit()
    conn.close()

def get_last_message_status(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        SELECT id, content, replied FROM messages
        WHERE user_id = ? AND direction = 'user_to_admin'
        ORDER BY timestamp DESC LIMIT 1
    ''', (user_id,))
    row = c.fetchone()
    conn.close()
    return {'id': row[0], 'content': row[1], 'replied': row[2]} if row else None

# ------------------ 工作时间判断 ------------------
def is_working_time():
    beijing_tz = timezone(timedelta(hours=8))
    now = datetime.now(beijing_tz)
    hour = now.time().hour
    return not (NON_WORKING_START <= hour < NON_WORKING_END)

async def send_typing(chat_id, context):
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

# ------------------ 用户命令 ------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 反馈机器人已启动\n\n"
        "你可以直接发送消息给我，管理员会收到并回复你。\n\n"
        f"📌 非工作时间（{NON_WORKING_START}:00 - {NON_WORKING_END}:00）消息会延迟处理。"
    )

async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """获取当前聊天ID（私聊返回用户ID，群组返回群组ID）"""
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
    except ValueError:
        await update.message.reply_text("❌ 群组ID必须是数字（负数）。")

# ------------------ 消息转发 ------------------
async def forward_to_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.message
    user_id = user.id

    name = escape_markdown(user.first_name or "", version=2)
    username = f"@{escape_markdown(user.username or '', version=2)}" if user.username else "无"

    if message.text:
        content = escape_markdown(message.text, version=2)
        msg_type = "text"
        file_id = None
    elif message.photo:
        msg_type = "photo"
        file_id = message.photo[-1].file_id
        content = "📷 图片"
    elif message.video:
        msg_type = "video"
        file_id = message.video.file_id
        content = "🎬 视频"
    elif message.document:
        msg_type = "document"
        file_id = message.document.file_id
        content = f"📄 文件: {escape_markdown(message.document.file_name or '', version=2)}"
    elif message.voice:
        msg_type = "voice"
        file_id = message.voice.file_id
        content = "🎤 语音"
    else:
        await update.message.reply_text("❌ 暂不支持此类型消息")
        return

    user_msg_id = save_message(user_id, "user_to_admin", content)
    admin_group = get_admin_group_id()
    forward_text = (
        f"📨 **新消息**\n"
        f"👤 昵称: {name}\n"
        f"🆔 用户名: {username}\n"
        f"🔢 ID: `{user_id}`\n"
        f"💬 内容: {content}\n\n"
        f"👇 回复此消息即可回答用户。"
    )

    try:
        sent = await context.bot.send_message(
            chat_id=admin_group,
            text=forward_text,
            parse_mode="MarkdownV2"
        )
        save_reply_mapping(sent.message_id, user_id, user_msg_id)

        if msg_type in ("photo", "video", "document", "voice"):
            if msg_type == "photo":
                await context.bot.send_photo(chat_id=admin_group, photo=file_id)
            elif msg_type == "video":
                await context.bot.send_video(chat_id=admin_group, video=file_id)
            elif msg_type == "document":
                await context.bot.send_document(chat_id=admin_group, document=file_id)
            elif msg_type == "voice":
                await context.bot.send_voice(chat_id=admin_group, voice=file_id)
    except Exception as e:
        await update.message.reply_text(f"❌ 转发失败: {e}")
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

# ------------------ 状态查询回调 ------------------
async def status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = int(query.data.split("_")[1])
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

# ------------------ 管理员回复 ------------------
async def handle_admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != get_admin_group_id():
        return
    message = update.message
    if not message.reply_to_message:
        await message.reply_text("❌ 请回复我之前发给你的用户消息。")
        return

    original_msg_id = message.reply_to_message.message_id
    user_id, user_msg_id = get_user_by_reply_msg(original_msg_id)
    if not user_id:
        await message.reply_text("❌ 无法找到对应的用户。")
        return

    reply_text = message.text
    if not reply_text:
        await message.reply_text("❌ 请使用文字回复。")
        return

    save_message(user_id, "admin_to_user", reply_text)
    mark_message_replied(user_msg_id)

    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=f"📬 **管理员回复**：\n{reply_text}",
            parse_mode="Markdown"
        )
        await message.reply_text("✅ 回复已发送。")
    except Exception as e:
        await message.reply_text(f"❌ 发送失败: {e}")

# ------------------ 主函数 ------------------
def main():
    init_db()
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("status", my_status))
    app.add_handler(CommandHandler("setgroup", set_group))

    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, forward_to_admin))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.COMMAND, handle_admin_reply))
    app.add_handler(CallbackQueryHandler(status_callback, pattern="^status_"))

    print("🤖 精简版双向机器人启动...")
    print(f"当前管理员群组ID: {get_admin_group_id()}")
    print(f"超级管理员ID: {SUPER_ADMIN_ID}")
    app.run_polling()

if __name__ == "__main__":
    main()
