from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters, JobQueue
import asyncio
import sqlite3
import datetime
import os
import requests
import feedparser
from tradingview_ta import TA_Handler, Interval, Exchange
import ccxt
import pandas as pd
import talib
import time
import telegram

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
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            is_subscribed INTEGER DEFAULT 0,
            subscription_expiry_date TEXT,
            language TEXT,
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
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS bot_status (
            last_signal_scan TEXT,
            last_news_scan TEXT
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
    current_time_iso = datetime.datetime.now().isoformat()
    cursor.execute('SELECT user_id, language, subscribed_symbols, subscribed_timeframes FROM users WHERE is_subscribed = 1 AND subscription_expiry_date > ?', (current_time_iso,))
    results = cursor.fetchall()
    conn.close()
    return results

def is_user_subscribed(user_id):
    if user_id == ADMIN_USER_ID:
        return True

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
    return result[0] if result and result[0] else None

def set_user_language(user_id, lang_code):
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute('UPDATE users SET language = ? WHERE user_id = ?', (lang_code, user_id))
    conn.commit()
    conn.close()

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

def get_bot_status():
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT last_signal_scan, last_news_scan FROM bot_status ORDER BY last_signal_scan DESC LIMIT 1')
    result = cursor.fetchone()
    conn.close()
    return result

def update_bot_status(scan_type):
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    if scan_type == 'signals':
        cursor.execute('INSERT INTO bot_status (last_signal_scan) VALUES (?)', (datetime.datetime.now().isoformat(),))
    elif scan_type == 'news':
        cursor.execute('INSERT INTO bot_status (last_news_scan) VALUES (?)', (datetime.datetime.now().isoformat(),))
    conn.commit()
    conn.close()

# --- Localization & UI ---
MESSAGES = {
    'ar': {
        'welcome_language_select': "مرحباً! يرجى اختيار اللغة:",
        'welcome': "مرحباً! أنا بوت تداول تلقائي. 🤖\n\n**مميزات البوت:**\n\n🔹 **تنبيهات تلقائية:** إشارات شراء وبيع للعملات الرقمية.\n🔹 **أخبار عاجلة:** أحدث أخبار السوق من مصادر موثوقة.\n🔹 **تحليل فوري:** يمكنك تحليل أي عملة تريدها عبر أمر `/analyze`.",
        'subscription_info': "\n\n**للاشتراك:**\n\n1. أرسل قيمة الاشتراك إلى محفظة Binance التالية:\n   `{binance_wallet_address}`\n\n2. **الباقات المتاحة:**\n   - **يومي:** {price_day}\n   - **أسبوعي:** {price_week}\n   - **شهري:** {price_month}\n\n3. أرسل صورة الإيصال ومعرف المستخدم الخاص بك (يمكنك الحصول عليه عبر الأمر /myid) للمدير ليتم تفعيل اشتراكك.",
        'myid': "معرف المستخدم (User ID) الخاص بك هو:\n\n`{user_id}`\n\nقم بنسخه وإرساله للمدير لتفعيل اشتراكك.",
        'status_info': "📊 **حالة البوت:**\n\n- آخر فحص للإشارات: {last_signal_scan}\n- آخر فحص للأخبار: {last_news_scan}",
        'status_not_found': "📊 **حالة البوت:**\n\n- لا توجد بيانات حالة حالياً. يرجى الانتظار حتى يتم أول فحص.",
        'info_not_found': "❌ لم يتم العثور على معلومات للعملة `{symbol}`. يرجى التأكد من الرمز والمحاولة مرة أخرى.",
        'info_details': "📈 **معلومات العملة:**\n\n**العملة:** `{symbol}`\n**السعر الحالي:** `{price}`\n**التغير اليومي (%):** `{change}`\n**أعلى سعر (24 ساعة):** `{high}`\n**أقل سعر (24 ساعة):** `{low}`\n**حجم التداول (24 ساعة):** `{volume}`",
        'main_menu_unsubscribed': "عذراً، يجب أن تكون مشتركاً للوصول إلى هذه القائمة. للتفعيل، اتبع الخطوات في الرسالة الترحيبية /start.",
        'main_menu_subscribed': "أهلاً بك في القائمة الرئيسية. اختر الإعدادات التي تريدها.\n\nيمكنك أيضاً استخدام أمر `/analyze` لتحليل أي عملة تريدها.",
        'subscription_button': "للاشتراك في البوت اضغط هنا",
        'signal_found': "🚨 **تنبيه إشارة تداول جديدة!** 🚨\n\n**العملة:** {symbol}\n**الفاصل الزمني:** {timeframe}\n**الإشارة:** `{signal}`\n\n**تحليل فني (تقديري):**\n- **سعر الدخول:** {entry_price}\n- **هدف أول (TP1):** {tp1}\n- **هدف ثاني (TP2):** {tp2}\n- **وقف الخسارة (SL):** {sl}",
        'news_alert': "📰 **أخبار عاجلة!** 📰\n\n**{title}**\n\n[اقرأ المزيد هنا]({link})",
        'admin_only': "عذراً، هذا الأمر للآدمن فقط.",
        'activate_success': "✅ تم تفعيل اشتراك المستخدم {user_id} لمدة {duration}.",
        'activate_usage': "الرجاء استخدام الأمر بالشكل الصحيح: /admin_activate [user_id] [day|week|month]",
        'menu_symbols': "إعدادات العملات",
        'menu_timeframes': "إعدادات الفواصل الزمنية",
        'back_to_menu': "العودة للقائمة الرئيسية",
        'select_symbols': "اختر العملات التي تريد متابعتها:",
        'select_timeframes': "اختر الفواصل الزمنية التي تريد متابعتها:",
        'analyze_usage': "الرجاء استخدام الأمر بالشكل الصحيح: /analyze [الرمز] [الفاصل الزمني]\nمثال: `/analyze BTCUSDT 4h`",
        'analyze_error': "حدث خطأ أثناء تحليل العملة. يرجى التحقق من الرمز أو الفاصل الزمني والمحاولة مرة أخرى.",
        'analyze_analyzing': "جاري تحليل العملة {symbol} على الفاصل الزمني {timeframe}...",
    },
    'en': {
        'welcome_language_select': "Hello! Please select your language:",
        'welcome': "Hello! I am an automatic trading bot. 🤖\n\n**Bot Features:**\n\n🔹 **Automatic Alerts:** Buy and sell signals for cryptocurrencies.\n🔹 **Breaking News:** Latest market news from trusted sources.\n🔹 **Instant Analysis:** You can analyze any currency you want with the `/analyze` command.",
        'subscription_info': "\n\n**To subscribe:**\n\n1. Send the subscription value to the following Binance wallet:\n   `{binance_wallet_address}`\n\n2. **Available Packages:**\n   - **Daily:** {price_day}\n   - **Weekly:** {price_week}\n   - **Monthly:** {price_month}\n\n3. Send the receipt and your User ID (you can get it with the /myid command) to the admin to activate your subscription.",
        'myid': "Your User ID is:\n\n`{user_id}`\n\nCopy and send it to the admin to activate your subscription.",
        'status_info': "📊 **Bot Status:**\n\n- Last Signal Scan: {last_signal_scan}\n- Last News Scan: {last_news_scan}",
        'status_not_found': "📊 **Bot Status:**\n\n- No status data found currently. Please wait for the first scan.",
        'info_not_found': "❌ No information was found for the symbol `{symbol}`. Please check the symbol and try again.",
        'info_details': "📈 **Symbol Information:**\n\n**Symbol:** `{symbol}`\n**Current Price:** `{price}`\n**Daily Change (%):** `{change}`\n**24h High:** `{high}`\n**24h Low:** `{low}`\n**24h Volume:** `{volume}`",
        'main_menu_unsubscribed': "Sorry, you must be a subscriber to access this menu. To activate, follow the steps in the welcome message /start.",
        'main_menu_subscribed': "Welcome to the main menu. Choose the settings you want.\n\nYou can also use the `/analyze` command to analyze any currency you want.",
        'subscription_button': "To subscribe to the bot, click here",
        'signal_found': "🚨 **New Trading Signal Alert!** 🚨\n\n**Symbol:** {symbol}\n**Timeframe:** {timeframe}\n**Signal:** `{signal}`\n\n**Technical Analysis (Approximate):**\n- **Entry Price:** {entry_price}\n- **Take Profit 1 (TP1):** {tp1}\n- **Take Profit 2 (TP2):** {tp2}\n- **Stop Loss (SL):** {sl}",
        'news_alert': "📰 **Breaking News!** 📰\n\n**{title}**\n\n[Read more here]({link})",
        'admin_only': "Sorry, this command is for the admin only.",
        'activate_success': "✅ Subscription for user {user_id} has been activated for {duration}.",
        'activate_usage': "Please use the command correctly: /admin_activate [user_id] [day|week|month]",
        'menu_symbols': "Symbols Settings",
        'menu_timeframes': "Timeframes Settings",
        'back_to_menu': "Back to Main Menu",
        'select_symbols': "Select the symbols you want to follow:",
        'select_timeframes': "Select the timeframes you want to follow:",
        'analyze_usage': "Please use the command correctly: /analyze [Symbol] [Timeframe]\nExample: `/analyze BTCUSDT 4h`",
        'analyze_error': "An error occurred while analyzing the symbol. Please check the symbol or timeframe and try again.",
        'analyze_analyzing': "Analyzing symbol {symbol} on timeframe {timeframe}...",
    }
}

def get_messages(lang):
    return MESSAGES.get(lang, MESSAGES['ar'])

async def analyze_and_send_signal(context: ContextTypes.DEFAULT_TYPE, user_id: int, symbol: str, timeframe_str: str, lang: str):
    translations = get_messages(lang)
    
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
            signal = None
            if recommendation in ['STRONG_BUY', 'BUY']:
                signal = "BUY"
            elif recommendation in ['STRONG_SELL', 'SELL']:
                signal = "SELL"
            
            if signal:
                exchange = ccxt.binance()
                ticker = exchange.fetch_ticker(symbol)
                current_price = ticker['last']
                
                ohlcv = exchange.fetch_ohlcv(symbol, timeframe_str, limit=14)
                df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                atr = talib.ATR(df['high'], df['low'], df['close'], timeperiod=14).iloc[-1]

                if signal == "BUY":
                    entry_price = current_price
                    sl = current_price - (atr * 1.5)
                    tp1 = current_price + (atr * 1.0)
                    tp2 = current_price + (atr * 2.0)
                else:
                    entry_price = current_price
                    sl = current_price + (atr * 1.5)
                    tp1 = current_price - (atr * 1.0)
                    tp2 = current_price - (atr * 2.0)
                
                message = translations['signal_found'].format(
                    symbol=symbol,
                    timeframe=timeframe_str,
                    signal=signal,
                    entry_price=round(entry_price, 4),
                    tp1=round(tp1, 4),
                    tp2=round(tp2, 4),
                    sl=round(sl, 4)
                )
                if CHANNEL_ID:
                    await context.bot.send_message(chat_id=CHANNEL_ID, text=message, parse_mode='Markdown')
                else:
                    await context.bot.send_message(chat_id=user_id, text=message, parse_mode='Markdown')
                print(f"Alert sent to user {user_id} for {symbol} on {timeframe_str} - {signal}")
    except Exception as e:
        print(f"Error fetching signal for {symbol} on {timeframe_str}: {e}")
        await context.bot.send_message(chat_id=user_id, text=translations['analyze_error'], parse_mode='Markdown')

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    add_user_if_not_exists(user_id)
    user_lang = get_user_language(user_id)
    
    if not user_lang:
        keyboard = [
            [InlineKeyboardButton("العربية", callback_data='set_lang_ar')],
            [InlineKeyboardButton("English", callback_data='set_lang_en')],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(MESSAGES['ar']['welcome_language_select'], reply_markup=reply_markup)
    else:
        translations = get_messages(user_lang)
        welcome_message = translations['welcome']
        
        if not is_user_subscribed(user_id):
            welcome_message += translations['subscription_info'].format(
                binance_wallet_address=BINANCE_WALLET_ADDRESS,
                price_day=SUBSCRIPTION_PRICES['day'],
                price_week=SUBSCRIPTION_PRICES['week'],
                price_month=SUBSCRIPTION_PRICES['month']
            )
            keyboard = [[InlineKeyboardButton(translations['subscription_button'], callback_data='show_subscription_info')]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(welcome_message, parse_mode='Markdown', reply_markup=reply_markup)
        else:
            await update.message.reply_text(welcome_message, parse_mode='Markdown')
            await menu_command(update, context)

async def myid_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    lang = get_user_language(user_id)
    translations = get_messages(lang)
    await update.message.reply_text(translations['myid'].format(user_id=user_id), parse_mode='Markdown')

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    lang = get_user_language(user_id)
    translations = get_messages(lang)

    if not is_user_subscribed(user_id):
        await update.message.reply_text(translations['main_menu_unsubscribed'])
        return

    status_data = get_bot_status()
    if status_data:
        last_signal = status_data[0] if status_data[0] else 'N/A'
        last_news = status_data[1] if status_data[1] else 'N/A'
        message = translations['status_info'].format(last_signal_scan=last_signal, last_news_scan=last_news)
        await update.message.reply_text(message, parse_mode='Markdown')
    else:
        await update.message.reply_text(translations['status_not_found'], parse_mode='Markdown')
    
async def info_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    lang = get_user_language(user_id)
    translations = get_messages(lang)

    if not is_user_subscribed(user_id):
        await update.message.reply_text(translations['main_menu_unsubscribed'])
        return

    try:
        symbol = context.args[0].upper()
        exchange = ccxt.binance()
        ticker = exchange.fetch_ticker(symbol)

        price = round(ticker['last'], 4)
        change_percent = round(ticker['change_24h'], 2)
        high = round(ticker['high_24h'], 4)
        low = round(ticker['low_24h'], 4)
        volume = round(ticker['quoteVolume'], 2)
        
        message = translations['info_details'].format(
            symbol=symbol,
            price=price,
            change=change_percent,
            high=high,
            low=low,
            volume=volume
        )
        await update.message.reply_text(message, parse_mode='Markdown')
    except (IndexError, ccxt.ExchangeError):
        await update.message.reply_text(translations['info_not_found'].format(symbol=context.args[0].upper()))
    except Exception as e:
        await update.message.reply_text(translations['analyze_error'])

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

async def analyze_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    lang = get_user_language(user_id)
    translations = get_messages(lang)

    if not is_user_subscribed(user_id):
        await update.message.reply_text(translations['main_menu_unsubscribed'])
        return
    
    try:
        symbol = context.args[0].upper()
        timeframe_str = context.args[1]
        
        if timeframe_str not in TIMEFRAMES_ENUM:
            raise ValueError
        
        await update.message.reply_text(translations['analyze_analyzing'].format(symbol=symbol, timeframe=timeframe_str))
        await analyze_and_send_signal(context, user_id, symbol, timeframe_str, lang)
    except (IndexError, ValueError):
        await update.message.reply_text(translations['analyze_usage'])

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # This is the corrected line to get the user ID regardless of whether the update is a message or a callback
    user_id = update.effective_user.id if hasattr(update, 'effective_user') else update.callback_query.from_user.id

    lang = get_user_language(user_id)
    translations = get_messages(lang)
    
    if not is_user_subscribed(user_id):
        keyboard = [[InlineKeyboardButton(translations['subscription_button'], callback_data='show_subscription_info')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(translations['main_menu_unsubscribed'], reply_markup=reply_markup)
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
    
    if query.data.startswith('set_lang_'):
        lang_code = query.data.split('_')[2]
        set_user_language(user_id, lang_code)
        
        translations = get_messages(lang_code)
        welcome_message = translations['welcome']
        
        if not is_user_subscribed(user_id):
            welcome_message += translations['subscription_info'].format(
                binance_wallet_address=BINANCE_WALLET_ADDRESS,
                price_day=SUBSCRIPTION_PRICES['day'],
                price_week=SUBSCRIPTION_PRICES['week'],
                price_month=SUBSCRIPTION_PRICES['month']
            )
            keyboard = [[InlineKeyboardButton(translations['subscription_button'], callback_data='show_subscription_info')]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            try:
                await query.edit_message_text(welcome_message, parse_mode='Markdown', reply_markup=reply_markup)
            except telegram.error.BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise e
        else:
            try:
                await query.edit_message_text(welcome_message, parse_mode='Markdown')
                await menu_command(update, context) # Changed to pass update, not query
            except telegram.error.BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise e
        return

    lang = get_user_language(user_id)
    translations = get_messages(lang)

    if query.data == 'show_subscription_info':
        welcome_message = translations['welcome']
        welcome_message += translations['subscription_info'].format(
            binance_wallet_address=BINANCE_WALLET_ADDRESS,
            price_day=SUBSCRIPTION_PRICES['day'],
            price_week=SUBSCRIPTION_PRICES['week'],
            price_month=SUBSCRIPTION_PRICES['month']
        )
        try:
            await query.edit_message_text(welcome_message, parse_mode='Markdown')
        except telegram.error.BadRequest as e:
            if "Message is not modified" not in str(e):
                raise e
        return

    if not is_user_subscribed(user_id):
        keyboard = [[InlineKeyboardButton(translations['subscription_button'], callback_data='show_subscription_info')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.reply_text(translations['main_menu_unsubscribed'], reply_markup=reply_markup)
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
        await menu_command(update, context) # Changed to pass update, not query

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
    try:
        await query.edit_message_text(translations['select_symbols'], reply_markup=reply_markup)
    except telegram.error.BadRequest as e:
        if "Message is not modified" not in str(e):
            raise e

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
    try:
        await query.edit_message_text(translations['select_timeframes'], reply_markup=reply_markup)
    except telegram.error.BadRequest as e:
        if "Message is not modified" not in str(e):
            raise e

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
    
    try:
        exchange = ccxt.binance()
        ticker = exchange.fetch_ticker(symbol)
        current_price = ticker['last']
        
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=14)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        atr = talib.ATR(df['high'], df['low'], df['close'], timeperiod=14).iloc[-1]

        if signal == "BUY":
            entry_price = current_price
            sl = current_price - (atr * 1.5)
            tp1 = current_price + (atr * 1.0)
            tp2 = current_price + (atr * 2.0)
        else:
            entry_price = current_price
            sl = current_price + (atr * 1.5)
            tp1 = current_price - (atr * 1.0)
            tp2 = current_price - (atr * 2.0)

        message = translations['signal_found'].format(
            symbol=symbol,
            timeframe=timeframe,
            signal=signal,
            entry_price=round(entry_price, 4),
            tp1=round(tp1, 4),
            tp2=round(tp2, 4),
            sl=round(sl, 4)
        )
        if CHANNEL_ID:
            await context.bot.send_message(chat_id=CHANNEL_ID, text=message, parse_mode='Markdown')
        else:
            await context.bot.send_message(chat_id=user_id, text=message, parse_mode='Markdown')
        print(f"Alert sent to user {user_id} for {symbol} on {timeframe} - {signal}")
    except Exception as e:
        print(f"Failed to send alert with TP/SL to user {user_id}: {e}")
        simple_message = translations['signal_found_simple'].format(symbol=symbol, timeframe=timeframe, signal=signal)
        await context.bot.send_message(chat_id=user_id, text=simple_message, parse_mode='Markdown')
    
    
async def monitor_tradingview_signals(context: ContextTypes.DEFAULT_TYPE):
    print("Running autonomous market scan...")
    update_bot_status('signals')
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
                time.sleep(1) 
                
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
    update_bot_status('news')
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
    
    # تردد الفحص: 300 ثانية = 5 دقائق
    job_queue.run_repeating(monitor_tradingview_signals, interval=300, first=datetime.time(0, 0))
    # تردد الفحص: 600 ثانية = 10 دقائق
    job_queue.run_repeating(monitor_news, interval=600, first=datetime.time(0, 0))

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("myid", myid_command))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("info", info_command))
    app.add_handler(CommandHandler("admin_activate", admin_activate))
    app.add_handler(CommandHandler("analyze", analyze_command))
    app.add_handler(CallbackQueryHandler(callback_handler))
    
    print("Bot is running and monitoring signals automatically...")
    app.run_polling()

if __name__ == "__main__":
    main()
