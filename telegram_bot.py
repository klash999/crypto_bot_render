from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters, JobQueue
import asyncio
import sqlite3
import datetime
import os
import requests
import feedparser
from tradingview_ta import TA_Handler, Interval, Exchange

# --- Bot Configuration ---
TOKEN = os.getenv('TOKEN')
ADMIN_USER_ID = int(os.getenv('ADMIN_USER_ID'))
CHANNEL_ID = os.getenv('CHANNEL_ID')
NEWS_RSS_URL = 'https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml'

# === قم بتعديل هذه المعلومات ===
BINANCE_WALLET_ADDRESS = "YOUR_BINANCE_WALLET_ADDRESS_HERE"
SUBSCRIPTION_PRICES = {
    'day': '4 USDT',
    'week': '15 USDT',
    'month': '45 USDT'
}
# =================================

# --- Database & Subscription Management ---
DATABASE_NAME = 'crypto_bot.db'

def setup_database():
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    # تعديل جدول users لإضافة تاريخ انتهاء الاشتراك
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            is_subscribed INTEGER DEFAULT 0,
            subscription_expiry_date TEXT,
            language TEXT DEFAULT 'ar',
            subscribed_symbols TEXT,
            subscribed_timeframes TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sent_signals (
            symbol TEXT,
            timeframe TEXT,
            signal TEXT,
            timestamp TEXT,
            PRIMARY KEY (symbol, timeframe)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sent_news (
            link TEXT PRIMARY KEY
        )
    ''')
    conn.commit()
    conn.close()

def get_user_settings(user_id):
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT subscribed_symbols, subscribed_timeframes FROM users WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()
    conn.close()
    if result and result[0] and result[1]:
        return result[0].split(','), result[1].split(',')
    return [], []

def update_user_settings(user_id, symbols, timeframes):
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    symbols_str = ','.join(symbols)
    timeframes_str = ','.join(timeframes)
    cursor.execute('UPDATE users SET subscribed_symbols = ?, subscribed_timeframes = ? WHERE user_id = ?', (symbols_str, timeframes_str, user_id))
    conn.commit()
    conn.close()

def get_subscribed_users():
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    # التحقق من أن تاريخ الاشتراك لم ينتهِ
    current_time_iso = datetime.datetime.now().isoformat()
    cursor.execute('SELECT user_id, language, subscribed_symbols, subscribed_timeframes FROM users WHERE is_subscribed = 1 AND subscription_expiry_date > ?', (current_time_iso,))
    results = cursor.fetchall()
    conn.close()
    return results

def is_user_subscribed(user_id):
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT subscription_expiry_date FROM users WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()
    conn.close()
    if result and result[0]:
        expiry_date = datetime.datetime.fromisoformat(result[0])
        return expiry_date > datetime.datetime.now()
    return False

def add_user_if_not_exists(user_id):
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute('INSERT OR IGNORE INTO users (user_id) VALUES (?)', (user_id,))
    conn.commit()
    conn.close()

def update_subscription_status(user_id, status, duration=None):
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    expiry_date = None
    if status == 1 and duration:
        if duration == 'day':
            expiry_date = datetime.datetime.now() + datetime.timedelta(days=1)
        elif duration == 'week':
            expiry_date = datetime.datetime.now() + datetime.timedelta(weeks=1)
        elif duration == 'month':
            expiry_date = datetime.datetime.now() + datetime.timedelta(days=30)
    
    if expiry_date:
        cursor.execute('UPDATE users SET is_subscribed = ?, subscription_expiry_date = ? WHERE user_id = ?', (status, expiry_date.isoformat(), user_id))
    else:
        cursor.execute('UPDATE users SET is_subscribed = ? WHERE user_id = ?', (status, user_id))
    conn.commit()
    conn.close()

def get_user_language(user_id):
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT language FROM users WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else 'ar'

def get_last_sent_signal(symbol, timeframe):
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT signal, timestamp FROM sent_signals WHERE symbol = ? AND timeframe = ?', (symbol, timeframe))
    result = cursor.fetchone()
    conn.close()
    return result

def save_sent_signal(symbol, timeframe, signal):
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute('INSERT OR REPLACE INTO sent_signals (symbol, timeframe, signal, timestamp) VALUES (?, ?, ?, ?)', (symbol, timeframe, signal, datetime.datetime.now().isoformat()))
    conn.commit()
    conn.close()
    
def is_news_sent(link):
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT link FROM sent_news WHERE link = ?', (link,))
    result = cursor.fetchone()
    conn.close()
    return result is not None

def save_news_sent(link):
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute('INSERT OR IGNORE INTO sent_news (link) VALUES (?)', (link,))
    conn.commit()
    conn.close()

# --- Localization & UI ---
MESSAGES = {
    'ar': {
        'welcome': "مرحباً! أنا بوت تداول تلقائي. 🤖\n\n**مميزات البوت:**\n\n🔹 **تنبيهات تلقائية:** إشارات شراء وبيع للعملات الرقمية.\n🔹 **أخبار عاجلة:** أحدث أخبار السوق من مصادر موثوقة.\n🔹 **إعدادات مخصصة:** اختر العملات والفواصل الزمنية التي تهمك.\n\n**للاشتراك:**\n\n1. أرسل قيمة الاشتراك إلى محفظة Binance التالية:\n   `{binance_wallet_address}`\n\n2. **الباقات المتاحة:**\n   - **يومي:** {price_day}\n   - **أسبوعي:** {price_week}\n   - **شهري:** {price_month}\n\n3. أرسل صورة الإيصال ومعرف المستخدم الخاص بك (يمكنك الحصول عليه عبر الأمر /myid) للمدير ليتم تفعيل اشتراكك.",
        'myid': "معرف المستخدم (User ID) الخاص بك هو:\n\n`{user_id}`\n\nقم بنسخه وإرساله للمدير لتفعيل اشتراكك.",
        'main_menu_unsubscribed': "عذراً، يجب أن تكون مشتركاً للوصول إلى هذه القائمة. للتفعيل، اتبع الخطوات في الرسالة الترحيبية /start.",
        'main_menu_subscribed': "أهلاً بك في القائمة الرئيسية. اختر الإعدادات التي تريدها.",
        'signal_found': "🚨 **تنبيه إشارة تداول جديدة!** 🚨\n\n**العملة:** {symbol}\n**الفاصل الزمني:** {timeframe}\n**الإشارة:** `{signal}`",
        'news_alert': "📰 **أخبار عاجلة!** 📰\n\n**{title}**\n\n[اقرأ المزيد هنا]({link})",
        'admin_only': "عذراً، هذا الأمر للآدمن فقط.",
        'activate_success': "✅ تم تفعيل اشتراك المستخدم {user_id} لمدة {duration}.",
        'activate_usage': "الرجاء استخدام الأمر بالشكل الصحيح: /admin_activate [user_id] [day|week|month]",
        'menu_symbols': "إعدادات العملات",
        'menu_timeframes': "إعدادات الفواصل الزمنية",
        'back_to_menu': "العودة للقائمة الرئيسية",
        'select_symbols': "اختر العملات التي تريد متابعتها:",
        'select_timeframes': "اختر الفواصل الزمنية التي تريد متابعتها:",
    }
}

def get_messages(lang):
    return MESSAGES.get(lang, MESSAGES['ar'])

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    add_user_if_not_exists(user_id)
    lang = get_user_language(user_id)
    translations = get_messages(lang)
    
    await update.message.reply_text(translations['welcome'].format(
        binance_wallet_address=BINANCE_WALLET_ADDRESS,
        price_day=SUBSCRIPTION_PRICES['day'],
        price_week=SUBSCRIPTION_PRICES['week'],
        price_month=SUBSCRIPTION_PRICES['month']
    ), parse_mode='Markdown')

async def myid_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    lang = get_user_language(user_id)
    translations = get_messages(lang)
    await update.message.reply_text(translations['myid'].format(user_id=user_id), parse_mode='Markdown')
    
async def admin_activate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    lang = get_user_language(user_id)
    translations = get_messages(lang)
    
    if user_id != ADMIN_USER_ID:
        await update.message.reply_text(translations['admin_only'])
        return
    
    try:
        user_to_activate = int(context.args[0])
        duration = context.args[1]
        if duration not in ['day', 'week', 'month']:
            await update.message.reply_text(translations['activate_usage'])
            return
            
        update_subscription_status(user_to_activate, 1, duration)
        await update.message.reply_text(translations['activate_success'].format(user_id=user_to_activate, duration=duration))
    except (IndexError, ValueError):
        await update.message.reply_text(translations['activate_usage'])

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    lang = get_user_language(user_id)
    translations = get_messages(lang)

    if not is_user_subscribed(user_id):
        await update.message.reply_text(translations['main_menu_unsubscribed'])
        return

    keyboard = [
        [InlineKeyboardButton(translations['menu_symbols'], callback_data='symbols')],
        [InlineKeyboardButton(translations['menu_timeframes'], callback_data='timeframes')],
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(translations['main_menu_subscribed'], reply_markup=reply_markup)

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = get_user_language(user_id)
    translations = get_messages(lang)

    if not is_user_subscribed(user_id):
        await query.message.reply_text(translations['main_menu_unsubscribed'])
        return

    if query.data == 'symbols':
        await show_symbols_menu(query, translations)
    elif query.data == 'timeframes':
        await show_timeframes_menu(query, translations)
    elif query.data.startswith('toggle_symbol_'):
        await toggle_symbol(query, translations)
    elif query.data.startswith('toggle_timeframe_'):
        await toggle_timeframe(query, translations)
    elif query.data == 'back_to_menu':
        await menu_command(query, context)

async def show_symbols_menu(query, translations):
    user_id = query.from_user.id
    subscribed_symbols, _ = get_user_settings(user_id)
    all_symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "ADAUSDT", "XRPUSDT"]
    
    keyboard = []
    for symbol in all_symbols:
        emoji = "✅" if symbol in subscribed_symbols else "◻️"
        keyboard.append([InlineKeyboardButton(f"{emoji} {symbol}", callback_data=f'toggle_symbol_{symbol}')])
    
    keyboard.append([InlineKeyboardButton(translations['back_to_menu'], callback_data='back_to_menu')])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(translations['select_symbols'], reply_markup=reply_markup)

async def show_timeframes_menu(query, translations):
    user_id = query.from_user.id
    _, subscribed_timeframes = get_user_settings(user_id)
    all_timeframes = ["1h", "4h"]
    
    keyboard = []
    for timeframe in all_timeframes:
        emoji = "✅" if timeframe in subscribed_timeframes else "◻️"
        keyboard.append([InlineKeyboardButton(f"{emoji} {timeframe}", callback_data=f'toggle_timeframe_{timeframe}')])
    
    keyboard.append([InlineKeyboardButton(translations['back_to_menu'], callback_data='back_to_menu')])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(translations['select_timeframes'], reply_markup=reply_markup)

async def toggle_symbol(query, translations):
    user_id = query.from_user.id
    symbol = query.data.split('_')[2]
    subscribed_symbols, subscribed_timeframes = get_user_settings(user_id)
    
    if symbol in subscribed_symbols:
        subscribed_symbols.remove(symbol)
    else:
        subscribed_symbols.append(symbol)
    
    update_user_settings(user_id, subscribed_symbols, subscribed_timeframes)
    await show_symbols_menu(query, translations)

async def toggle_timeframe(query, translations):
    user_id = query.from_user.id
    timeframe = query.data.split('_')[2]
    subscribed_symbols, subscribed_timeframes = get_user_settings(user_id)
    
    if timeframe in subscribed_timeframes:
        subscribed_timeframes.remove(timeframe)
    else:
        subscribed_timeframes.append(timeframe)
    
    update_user_settings(user_id, subscribed_symbols, subscribed_timeframes)
    await show_timeframes_menu(query, translations)

# --- Proactive Alerting System ---
TIMEFRAMES_ENUM = {
    "1h": Interval.INTERVAL_1_HOUR,
    "4h": Interval.INTERVAL_4_HOURS,
}

async def send_alert(context: ContextTypes.DEFAULT_TYPE, user_id: int, symbol: str, timeframe: str, signal: str, lang: str):
    translations = get_messages(lang)
    message = translations['signal_found'].format(
        symbol=symbol,
        timeframe=timeframe,
        signal=signal
    )
    try:
        if CHANNEL_ID:
            await context.bot.send_message(chat_id=CHANNEL_ID, text=message, parse_mode='Markdown')
        else:
            await context.bot.send_message(chat_id=user_id, text=message, parse_mode='Markdown')
        print(f"Alert sent to user {user_id} for {symbol} on {timeframe} - {signal}")
    except Exception as e:
        print(f"Failed to send alert to user {user_id}: {e}")

async def monitor_tradingview_signals(context: ContextTypes.DEFAULT_TYPE):
    print("Running autonomous market scan...")
    subscribed_users = get_subscribed_users()
    
    all_symbols_to_monitor = set()
    all_timeframes_to_monitor = set()
    
    for _, _, symbols_str, timeframes_str in subscribed_users:
        if symbols_str:
            for s in symbols_str.split(','):
                all_symbols_to_monitor.add(s)
        if timeframes_str:
            for t in timeframes_str.split(','):
                all_timeframes_to_monitor.add(t)

    for symbol in all_symbols_to_monitor:
        for timeframe_str in all_timeframes_to_monitor:
            try:
                handler = TA_Handler(
                    symbol=symbol,
                    screener="crypto",
                    exchange="BINANCE",
                    interval=TIMEFRAMES_ENUM[timeframe_str],
                )
                analysis = handler.get_analysis()
                
                if analysis and analysis.summary:
                    recommendation = analysis.summary['RECOMMENDATION']
                    
                    if recommendation in ['STRONG_BUY', 'BUY', 'STRONG_SELL', 'SELL']:
                        signal = "BUY" if "BUY" in recommendation else "SELL"
                        
                        last_signal = get_last_sent_signal(symbol, timeframe_str)
                        
                        if not last_signal or last_signal[0] != signal:
                            for user_id, lang, user_symbols, user_timeframes in subscribed_users:
                                if user_symbols and user_timeframes and symbol in user_symbols.split(',') and timeframe_str in user_timeframes.split(','):
                                    await send_alert(context, user_id, symbol, timeframe_str, signal, lang)
                            save_sent_signal(symbol, timeframe_str, signal)
            except Exception as e:
                print(f"Error fetching signal for {symbol} on {timeframe_str}: {e}")

async def monitor_news(context: ContextTypes.DEFAULT_TYPE):
    print("Running news monitor...")
    try:
        feed = feedparser.parse(NEWS_RSS_URL)
        if feed.entries:
            latest_news = feed.entries[0]
            if not is_news_sent(latest_news.link):
                subscribed_users = get_subscribed_users()
                for user_id, lang, _, _ in subscribed_users:
                    translations = get_messages(lang)
                    message = translations['news_alert'].format(
                        title=latest_news.title,
                        link=latest_news.link
                    )
                    await context.bot.send_message(chat_id=user_id, text=message, parse_mode='Markdown')
                save_news_sent(latest_news.link)
    except Exception as e:
        print(f"Error fetching news: {e}")
        
def main():
    setup_database()
    
    if not TOKEN or not ADMIN_USER_ID:
        print("Please set the TOKEN and ADMIN_USER_ID environment variables.")
        return
        
    app = Application.builder().token(TOKEN).build()
    job_queue = app.job_queue
    
    job_queue.run_repeating(monitor_tradingview_signals, interval=300, first=datetime.time(0, 0))
    job_queue.run_repeating(monitor_news, interval=900, first=datetime.time(0, 0))

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("myid", myid_command))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("admin_activate", admin_activate))
    app.add_handler(CallbackQueryHandler(callback_handler))
    
    print("Bot is running and monitoring signals automatically...")
    app.run_polling()

if __name__ == "__main__":
    main()
