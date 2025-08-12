from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters, JobQueue
import asyncio
import ccxt
import pandas as pd
import talib
import sqlite3
import datetime
import math
import feedparser

# --- Trading Logic and Analysis ---
def fetch_and_analyze_data(symbol, timeframe='1h', limit=200):
    try:
        exchange = ccxt.binance()
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df['close'] = pd.to_numeric(df['close'])
        df['high'] = pd.to_numeric(df['high'])
        df['low'] = pd.to_numeric(df['low'])
        
        df['rsi'] = talib.RSI(df['close'], timeperiod=14)
        macd_output = talib.MACD(df['close'], fastperiod=12, slowperiod=26, signalperiod=9)
        df['macd'], df['macd_signal'], df['macd_hist'] = macd_output
        df['atr'] = talib.ATR(df['high'], df['low'], df['close'], timeperiod=14)
        df['sma200'] = talib.SMA(df['close'], timeperiod=200)
        
        stoch_k, stoch_d = talib.STOCH(df['high'], df['low'], df['close'], 
                                       fastk_period=14, slowk_period=3, 
                                       slowd_period=3, slowk_matype=0, slowd_matype=0)
        df['stoch_k'] = stoch_k
        df['stoch_d'] = stoch_d
        
        df = df.dropna().reset_index(drop=True)
        return df
    except Exception as e:
        print(f"An error occurred during analysis of {symbol} on {timeframe}: {e}")
        return None

def analyze_patterns(data):
    """
    Analyzes candlestick patterns and returns a signal.
    """
    if data is None or data.empty:
        return None, None
    
    close = data['close']
    high = data['high']
    low = data['low']
    
    # Check for Bullish patterns
    hammer = talib.CDLHAMMER(data['open'], high, low, close)
    inverted_hammer = talib.CDLINVERTEDHAMMER(data['open'], high, low, close)
    bullish_engulfing = talib.CDLENGULFING(data['open'], high, low, close)
    piercing_line = talib.CDLPIERCING(data['open'], high, low, close)
    
    if hammer.iloc[-1] != 0 or inverted_hammer.iloc[-1] != 0 or bullish_engulfing.iloc[-1] > 0 or piercing_line.iloc[-1] != 0:
        return 'BUY', 'نموذج صعودي قوي'

    # Check for Bearish patterns
    hanging_man = talib.CDLHANGINGMAN(data['open'], high, low, close)
    shooting_star = talib.CDLSHOOTINGSTAR(data['open'], high, low, close)
    bearish_engulfing = talib.CDLENGULFING(data['open'], high, low, close)
    dark_cloud_cover = talib.CDLDARKCLOUDCOVER(data['open'], high, low, close)

    if hanging_man.iloc[-1] != 0 or shooting_star.iloc[-1] != 0 or bearish_engulfing.iloc[-1] < 0 or dark_cloud_cover.iloc[-1] != 0:
        return 'SELL', 'نموذج هبوطي قوي'
        
    return None, None

def generate_trade_info(signal, latest_data, timeframe):
    close_price = latest_data['close']
    atr_value = latest_data['atr']
    
    if signal == 'BUY':
        entry_price = close_price
        stop_loss = entry_price - (1.5 * atr_value)
        target1 = entry_price + (1 * atr_value)
        target2 = entry_price + (2 * atr_value)
        target3 = entry_price + (3 * atr_value)
    elif signal == 'SELL':
        entry_price = close_price
        stop_loss = entry_price + (1.5 * atr_value)
        target1 = entry_price - (1 * atr_value)
        target2 = entry_price - (2 * atr_value)
        target3 = entry_price - (3 * atr_value)
    else:
        return None

    duration_map = {
        '1m': 'دقائق قليلة',
        '5m': 'بضع ساعات',
        '15m': 'عدة ساعات',
        '1h': 'يوم أو أكثر',
        '4h': 'عدة أيام'
    }
    duration = duration_map.get(timeframe, 'غير محدد')

    return {
        'entry_price': entry_price,
        'stop_loss': stop_loss,
        'target1': target1,
        'target2': target2,
        'target3': target3,
        'duration': duration
    }

def generate_trading_signal(analyzed_data, timeframe):
    latest_data = analyzed_data.iloc[-1]
    latest_rsi = latest_data['rsi']
    latest_macd_hist = latest_data['macd_hist']
    latest_stoch_k = latest_data['stoch_k']
    
    signal = 'HOLD'
    pattern_signal, pattern_name = analyze_patterns(analyzed_data)

    # Simplified signal logic for more opportunities
    if latest_rsi < 35 or latest_macd_hist > 0 or latest_stoch_k < 20:
        signal = 'BUY'
    elif latest_rsi > 65 or latest_macd_hist < 0 or latest_stoch_k > 80:
        signal = 'SELL'
    
    # NEW: Prioritize pattern signals if they exist
    if pattern_signal is not None:
        signal = pattern_signal

    if signal != 'HOLD':
        trade_info = generate_trade_info(signal, latest_data, timeframe)
        if trade_info:
            if pattern_name:
                trade_info['pattern'] = pattern_name
            else:
                trade_info['pattern'] = None
            return signal, trade_info
    
    return 'HOLD', None

# --- Database & Subscription Management ---
DATABASE_NAME = 'crypto_bot.db'
TOKEN = "7502779556:AAGLINA2ZD0xmeuz0Csbl50IdBhPoeyPSYY"
ADMIN_USER_ID = 1793820239
YOUR_WALLET_ADDRESS = "TQCKc4Ri6tgGTKfXDMmwfsJtBA4srRWsGM"

user_state = {}

def setup_database():
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            is_subscribed INTEGER DEFAULT 0,
            language TEXT DEFAULT 'ar'
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sent_signals (
            signal_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            symbol TEXT,
            timeframe TEXT,
            signal TEXT,
            entry_price REAL,
            target1 REAL,
            target2 REAL,
            target3 REAL,
            stop_loss REAL,
            duration TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sent_listings (
            listing_id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT UNIQUE
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sent_news (
            news_id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT UNIQUE
        )
    ''')
    conn.commit()
    conn.close()

def get_subscribed_users():
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT user_id FROM users WHERE is_subscribed = 1')
    results = cursor.fetchall()
    conn.close()
    return [result[0] for result in results]

def is_user_subscribed(user_id):
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT is_subscribed FROM users WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()
    conn.close()
    if result and result[0] == 1:
        return True
    return False

def add_user_if_not_exists(user_id):
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute('INSERT OR IGNORE INTO users (user_id) VALUES (?)', (user_id,))
    conn.commit()
    conn.close()

def update_subscription_status(user_id, status):
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute('UPDATE users SET is_subscribed = ? WHERE user_id = ?', (status, user_id))
    conn.commit()
    conn.close()

def get_user_status(user_id):
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT is_subscribed FROM users WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()
    conn.close()
    if result:
        return "مشترك" if result[0] == 1 else "غير مشترك"
    return "المستخدم غير موجود."

# --- Telegram Bot Logic ---
SUBSCRIPTION_PACKAGES = {
    'daily': {'price': '4 USDT', 'name': 'الاشتراك اليومي'},
    'weekly': {'price': '15 USDT', 'name': 'الاشتراك الأسبوعي'},
    'monthly': {'price': '50 USDT', 'name': 'الاشتراك الشهري'}
}

def get_main_keyboard():
    keyboard = [
        [InlineKeyboardButton("إشارة تداول", callback_data='signal')],
        [InlineKeyboardButton("تحليل عملة", callback_data='analyze_symbol')],
        [InlineKeyboardButton("اشتراك", callback_data='subscribe')]
    ]
    return InlineKeyboardMarkup(keyboard)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    add_user_if_not_exists(user_id)
    
    welcome_message = "مرحباً بك! أنا بوت تداول العملات الرقمية الخاص بك."
    features_message = (
        "يقدم البوت الميزات التالية:\n"
        "- **إشارات تداول** فورية للفرص الواعدة في السوق بالكامل.\n"
        "- **تنبيهات بالاكتتابات** الجديدة على منصة Binance.\n"
        "- **أهم الأخبار** المباشرة في سوق العملات الرقمية.\n"
        "- **تحليل فني** دقيق للعملات عند الطلب.\n"
        "- **نظام اشتراكات** سهل ومرن للوصول الكامل للميزات."
    )

    await update.message.reply_text(welcome_message)
    await update.message.reply_text(features_message, reply_markup=get_main_keyboard())

async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    query = update.callback_query
    await query.answer()
    
    if user_id in user_state:
        del user_state[user_id]
        
    features_message = (
        "أهلاً بك في القائمة الرئيسية. اختر ما تريد فعله:\n"
        "- **إشارات تداول** فورية للفرص الواعدة في السوق بالكامل.\n"
        "- **تنبيهات بالاكتتابات** الجديدة على منصة Binance.\n"
        "- **أهم الأخبار** المباشرة في سوق العملات الرقمية.\n"
        "- **تحليل فني** دقيق للعملات عند الطلب.\n"
        "- **نظام اشتراكات** سهل ومرن للوصول الكامل للميزات."
    )
    
    await query.edit_message_text(features_message, reply_markup=get_main_keyboard())

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    callback_data = query.data

    if callback_data == 'subscribe':
        message = "اختر باقة الاشتراك المناسبة لك:\n\n"
        for key, package in SUBSCRIPTION_PACKAGES.items():
            message += f"**{package['name']}**: {package['price']}\n"
        message += f"\nللاشتراك، قم بتحويل المبلغ إلى عنوان المحفظة التالي:\n\n`{YOUR_WALLET_ADDRESS}`\n\n"
        message += f"بعد التحويل، أرسل إثبات الدفع (صورة) إلى الآدمن عبر الرسائل الخاصة.\n"
        message += f"رابط الآدمن: [الآدمن](tg://user?id={ADMIN_USER_ID})"

        keyboard = [[InlineKeyboardButton("العودة للقائمة الرئيسية", callback_data='back_to_menu')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(message, parse_mode='Markdown', disable_web_page_preview=True, reply_markup=reply_markup)

    elif callback_data == 'signal':
        if not is_user_subscribed(user_id):
            keyboard = [[InlineKeyboardButton("العودة للقائمة الرئيسية", callback_data='back_to_menu')]]
            await query.edit_message_text("عذراً، هذه الميزة متاحة للمشتركين فقط. يرجى الاشتراك للوصول الكامل.", reply_markup=InlineKeyboardMarkup(keyboard))
            return

        await query.edit_message_text("جاري تحليل السوق...")
        
        exchange = ccxt.binance()
        try:
            tickers = exchange.fetch_tickers()
        except Exception as e:
            await query.edit_message_text("عذرًا، حدث خطأ أثناء الاتصال بالمنصة. يرجى المحاولة لاحقًا.")
            return

        high_potential_symbols = []
        for symbol, ticker_data in tickers.items():
            try:
                if ticker_data['quote'] == 'USDT' and ticker_data['active'] and ticker_data['quoteVolume'] and ticker_data['percentage']:
                    if ticker_data['quoteVolume'] > 10000000 and abs(ticker_data['percentage']) > 5:
                        high_potential_symbols.append(symbol)
            except (KeyError, TypeError) as e:
                continue

        selected_symbol = None
        for symbol in high_potential_symbols:
            try:
                data = fetch_and_analyze_data(symbol=symbol, timeframe='1h')
                if data is not None and not data.empty:
                    signal, trade_info = generate_trading_signal(data, '1h')
                    if signal != 'HOLD' and trade_info:
                        selected_symbol = symbol
                        trade_details = trade_info
                        break
            except Exception as e:
                print(f"Error checking {symbol}: {e}")
                continue
        
        if selected_symbol:
            message = f"التحليل مكتمل. إشارة التداول الحالية لـ **{selected_symbol}** هي: **{signal}**"
            message += f"\n\n**تفاصيل الصفقة:**\n- **فاصل زمني:** 1 ساعة\n- **سعر الدخول:** {trade_details['entry_price']:.2f}\n- **وقف الخسارة:** {trade_details['stop_loss']:.2f}\n- **الهدف 1:** {trade_details['target1']:.2f}\n- **الهدف 2:** {trade_details['target2']:.2f}\n- **الهدف 3:** {trade_details['target3']:.2f}\n- **مدة الصفقة المتوقعة:** {trade_details['duration']}"
            if trade_details.get('pattern'):
                message += f"\n- **النموذج:** {trade_details['pattern']}"

            keyboard = [[InlineKeyboardButton("العودة للقائمة الرئيسية", callback_data='back_to_menu')]]
            await query.edit_message_text(message, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            keyboard = [[InlineKeyboardButton("العودة للقائمة الرئيسية", callback_data='back_to_menu')]]
            await query.edit_message_text("عذرًا، لم يتم العثور على أي فرصة تداول واعدة في السوق حالياً.", reply_markup=InlineKeyboardMarkup(keyboard))
            
    elif callback_data == 'analyze_symbol':
        if not is_user_subscribed(user_id):
            keyboard = [[InlineKeyboardButton("العودة للقائمة الرئيسية", callback_data='back_to_menu')]]
            await query.edit_message_text("عذراً، هذه الميزة متاحة للمشتركين فقط. يرجى الاشتراك للوصول الكامل.", reply_markup=InlineKeyboardMarkup(keyboard))
            return
        
        user_state[user_id] = 'waiting_for_symbol'
        
        keyboard = [[InlineKeyboardButton("العودة للقائمة الرئيسية", callback_data='back_to_menu')]]
        await query.edit_message_text("من فضلك أرسل لي رمز العملة التي تريد تحليلها (مثال: ETHUSDT).", reply_markup=InlineKeyboardMarkup(keyboard))
    
    elif callback_data == 'back_to_menu':
        await back_to_menu(update, context)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_message = update.message.text
    
    if user_id in user_state and user_state[user_id] == 'waiting_for_symbol':
        del user_state[user_id]
        
        if len(user_message) > 4 and user_message.isalnum():
            await analyze_symbol_on_demand(update, context, user_message)
        else:
            keyboard = [[InlineKeyboardButton("العودة للقائمة الرئيسية", callback_data='back_to_menu')]]
            await update.message.reply_text("عذراً، هذا ليس رمز عملة صالح. يرجى إرسال رمز مثل `ETHUSDT`.", reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text("عذراً، لم أفهم طلبك. يرجى استخدام القائمة الرئيسية.", reply_markup=get_main_keyboard())

async def analyze_symbol_on_demand(update: Update, context: ContextTypes.DEFAULT_TYPE, symbol: str):
    await update.message.reply_text(f"جاري تحليل عملة **{symbol.upper()}** على الفاصل الزمني 1 ساعة...", parse_mode='Markdown')
    data = fetch_and_analyze_data(symbol=symbol.upper(), timeframe='1h')
    
    if data is not None:
        signal, trade_info = generate_trading_signal(data, '1h')
        if signal != 'HOLD' and trade_info:
            message = f"التحليل مكتمل. إشارة التداول الحالية لـ **{symbol.upper()}** هي: **{signal}**"
            message += f"\n\n**تفاصيل الصفقة:**\n- **فاصل زمني:** 1 ساعة\n- **سعر الدخول:** {trade_info['entry_price']:.2f}\n- **وقف الخسارة:** {trade_info['stop_loss']:.2f}\n- **الهدف 1:** {trade_info['target1']:.2f}\n- **الهدف 2:** {trade_info['target2']:.2f}\n- **الهدف 3:** {trade_info['target3']:.2f}\n- **مدة الصفقة المتوقعة:** {trade_info['duration']}"
            if trade_info.get('pattern'):
                message += f"\n- **النموذج:** {trade_info['pattern']}"
        else:
            message = f"التحليل مكتمل. إشارة التداول الحالية لـ **{symbol.upper()}** هي: **HOLD**"
        
        keyboard = [[InlineKeyboardButton("العودة للقائمة الرئيسية", callback_data='back_to_menu')]]
        await update.message.reply_text(message, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        keyboard = [[InlineKeyboardButton("العودة للقائمة الرئيسية", callback_data='back_to_menu')]]
        await update.message.reply_text(f"عذرًا، لا يمكن تحليل عملة **{symbol.upper()}**. تأكد من أن الرمز صحيح.", reply_markup=InlineKeyboardMarkup(keyboard))

# --- Admin Commands ---
async def activate_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = update.effective_user.id
    if admin_id != ADMIN_USER_ID:
        await update.message.reply_text("عذراً، هذا الأمر مخصص للآدمن فقط.")
        return
    
    try:
        user_to_activate = int(context.args[0])
        update_subscription_status(user_to_activate, 1)
        await update.message.reply_text(f"تم تفعيل اشتراك المستخدم {user_to_activate} بنجاح.")
    except (IndexError, ValueError):
        await update.message.reply_text("الرجاء استخدام الأمر بالشكل الصحيح: /admin_activate [user_id]")

async def deactivate_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = update.effective_user.id
    if admin_id != ADMIN_USER_ID:
        await update.message.reply_text("عذراً، هذا الأمر مخصص للآدمن فقط.")
        return
    
    try:
        user_to_deactivate = int(context.args[0])
        update_subscription_status(user_to_deactivate, 0)
        await update.message.reply_text(f"تم إلغاء اشتراك المستخدم {user_to_deactivate} بنجاح.")
    except (IndexError, ValueError):
        await update.message.reply_text("الرجاء استخدام الأمر بالشكل الصحيح: /admin_deactivate [user_id]")

async def check_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = update.effective_user.id
    if admin_id != ADMIN_USER_ID:
        await update.message.reply_text("عذراً، هذا الأمر مخصص للآدمن فقط.")
        return
    
    try:
        user_to_check = int(context.args[0])
        status = get_user_status(user_to_check)
        await update.message.reply_text(f"حالة المستخدم {user_to_check}: {status}")
    except (IndexError, ValueError):
        await update.message.reply_text("الرجاء استخدام الأمر بالشكل الصحيح: /admin_status [user_id]")

# --- Proactive Alerting System ---
TIMEFRAMES = ['1h']

def get_sent_signals(symbol, timeframe):
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT signal_id FROM sent_signals WHERE symbol = ? AND timeframe = ? ORDER BY timestamp DESC LIMIT 1', (symbol, timeframe))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else None

def save_sent_signal(user_id, symbol, timeframe, signal, trade_info):
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO sent_signals (user_id, symbol, timeframe, signal, entry_price, target1, target2, target3, stop_loss, duration)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (user_id, symbol, timeframe, signal, trade_info['entry_price'], trade_info['target1'], trade_info['target2'], trade_info['target3'], trade_info['stop_loss'], trade_info['duration']))
    conn.commit()
    conn.close()

async def send_alert(context: ContextTypes.DEFAULT_TYPE, symbol: str, timeframe: str, signal: str, trade_info: dict):
    subscribed_users = get_subscribed_users()
    if not subscribed_users:
        print("No subscribed users to send alerts to.")
        return
    
    message = f"🚨 **تنبيه إشارة تداول جديد!** 🚨\n\n"
    message += f"**العملة:** {symbol}\n"
    message += f"**الإشارة:** {signal}\n\n"
    message += f"**تفاصيل الصفقة:**\n"
    message += f"- **فاصل زمني:** {timeframe}\n"
    message += f"- **سعر الدخول:** {trade_info['entry_price']:.2f}\n"
    message += f"- **وقف الخسارة:** {trade_info['stop_loss']:.2f}\n"
    message += f"- **الهدف 1:** {trade_info['target1']:.2f}\n"
    message += f"- **الهدف 2:** {trade_info['target2']:.2f}\n"
    message += f"- **الهدف 3:** {trade_info['target3']:.2f}\n"
    message += f"- **مدة الصفقة المتوقعة:** {trade_info['duration']}\n"
    if trade_info.get('pattern'):
        message += f"- **النموذج:** {trade_info['pattern']}\n"
    message += f"\nتحليل السوق في الوقت الفعلي."

    for user_id in subscribed_users:
        try:
            await context.bot.send_message(chat_id=user_id, text=message, parse_mode='Markdown')
            print(f"Alert sent to user {user_id} for {symbol} on {timeframe} - {signal}")
        except Exception as e:
            print(f"Failed to send alert to user {user_id}: {e}")

async def monitor_and_find_signals(context: ContextTypes.DEFAULT_TYPE):
    print("Running AI-driven market scan...")
    exchange = ccxt.binance()
    try:
        tickers = exchange.fetch_tickers()
        high_potential_symbols = []
        for symbol, ticker_data in tickers.items():
            try:
                if ticker_data['quote'] == 'USDT' and ticker_data['active'] and ticker_data['quoteVolume'] and ticker_data['percentage']:
                    if ticker_data['quoteVolume'] > 10000000 and abs(ticker_data['percentage']) > 5:
                        high_potential_symbols.append(symbol)
            except (KeyError, TypeError) as e:
                continue
        
        print(f"Found {len(high_potential_symbols)} high potential symbols from quick scan.")

        for symbol in high_potential_symbols:
            for timeframe in TIMEFRAMES:
                analyzed_data = fetch_and_analyze_data(symbol=symbol, timeframe=timeframe)
                
                if analyzed_data is not None and not analyzed_data.empty:
                    signal, trade_info = generate_trading_signal(analyzed_data, timeframe)
                    
                    if signal != 'HOLD':
                        last_signal_id = get_sent_signals(symbol, timeframe)
                        if not last_signal_id:
                            await send_alert(context, symbol, timeframe, signal, trade_info)
                            for user_id in get_subscribed_users():
                                save_sent_signal(user_id, symbol, timeframe, signal, trade_info)
        
    except Exception as e:
        print(f"Error during AI-driven scan: {e}")

async def check_new_listings(context: ContextTypes.DEFAULT_TYPE):
    print("Checking for new crypto listings...")
    exchange = ccxt.binance()
    try:
        markets = exchange.fetch_markets()
        current_symbols = {market['symbol'] for market in markets if market['active']}
        
        conn = sqlite3.connect(DATABASE_NAME)
        cursor = conn.cursor()
        cursor.execute('SELECT symbol FROM sent_listings')
        sent_symbols = {row[0] for row in cursor.fetchall()}
        
        new_listings = current_symbols - sent_symbols
        
        if new_listings:
            message = "🆕 **تنبيه اكتتاب جديد!** 🆕\n\nتم إدراج عملات جديدة في منصة التداول. إليك الرموز:\n\n"
            for symbol in new_listings:
                message += f"- **{symbol}**\n"
                cursor.execute('INSERT INTO sent_listings (symbol) VALUES (?)', (symbol,))
            
            conn.commit()
            conn.close()

            subscribed_users = get_subscribed_users()
            for user_id in subscribed_users:
                try:
                    await context.bot.send_message(chat_id=user_id, text=message, parse_mode='Markdown')
                    print(f"New listing alert sent to user {user_id}")
                except Exception as e:
                    print(f"Failed to send new listing alert to user {user_id}: {e}")
        else:
            conn.close()
            print("No new listings found.")

    except Exception as e:
        print(f"Error checking new listings: {e}")

async def check_crypto_news(context: ContextTypes.DEFAULT_TYPE):
    print("Checking for new crypto news...")
    feed_url = 'https://cryptoslate.com/feed/'
    try:
        feed = feedparser.parse(feed_url)
        conn = sqlite3.connect(DATABASE_NAME)
        cursor = conn.cursor()

        for entry in feed.entries[:5]:
            title = entry.title
            link = entry.link
            
            cursor.execute('SELECT title FROM sent_news WHERE title = ?', (title,))
            if cursor.fetchone() is None:
                message = f"📰 **خبر عاجل!** 📰\n\n**{title}**\n\n[اقرأ المزيد]({link})"
                
                subscribed_users = get_subscribed_users()
                for user_id in subscribed_users:
                    try:
                        await context.bot.send_message(chat_id=user_id, text=message, parse_mode='Markdown', disable_web_page_preview=True)
                        print(f"News alert sent to user {user_id}: {title}")
                    except Exception as e:
                        print(f"Failed to send news alert to user {user_id}: {e}")
                
                cursor.execute('INSERT INTO sent_news (title) VALUES (?)', (title,))
                conn.commit()
        
        conn.close()
    except Exception as e:
        print(f"Error checking for news: {e}")

def main():
    setup_database()
    app = Application.builder().token(TOKEN).build()
    job_queue = app.job_queue
    
    job_queue.run_repeating(monitor_and_find_signals, interval=300, first=datetime.time(0, 0))
    job_queue.run_repeating(check_new_listings, interval=3600, first=datetime.time(0, 0))
    job_queue.run_repeating(check_crypto_news, interval=1800, first=datetime.time(0, 0))

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CallbackQueryHandler(back_to_menu, pattern='^back_to_menu$'))
    app.add_handler(CallbackQueryHandler(handle_callback))
    
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    app.add_handler(CommandHandler("admin_activate", activate_subscription))
    app.add_handler(CommandHandler("admin_deactivate", deactivate_subscription))
    app.add_handler(CommandHandler("admin_status", check_status))
    
    print("Bot is running and monitoring symbols automatically...")
    app.run_polling()

if __name__ == "__main__":
    main()