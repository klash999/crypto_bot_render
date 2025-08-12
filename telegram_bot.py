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
import os

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
    if data is None or data.empty:
        return None, None
    
    close = data['close']
    high = data['high']
    low = data['low']
    
    hammer = talib.CDLHAMMER(data['open'], high, low, close)
    inverted_hammer = talib.CDLINVERTEDHAMMER(data['open'], high, low, close)
    bullish_engulfing = talib.CDLENGULFING(data['open'], high, low, close)
    piercing_line = talib.CDLPIERCING(data['open'], high, low, close)
    
    if hammer.iloc[-1] != 0 or inverted_hammer.iloc[-1] != 0 or bullish_engulfing.iloc[-1] > 0 or piercing_line.iloc[-1] != 0:
        return 'BUY', 'نموذج صعودي قوي'

    hanging_man = talib.CDLHANGINGMAN(data['open'], high, low, close)
    shooting_star = talib.CDLSHOOTINGSTAR(data['open'], high, low, close)
    bearish_engulfing = talib.CDLENGULFING(data['open'], high, low, close)
    dark_cloud_cover = talib.CDLDARKCLOUDCOVER(data['open'], high, low, close)

    if hanging_man.iloc[-1] != 0 or shooting_star.iloc[-1] != 0 or bearish_engulfing.iloc[-1] < 0 or dark_cloud_cover.iloc[-1] != 0:
        return 'SELL', 'نموذج هبوطي قوي'
        
    return None, None

def generate_trade_info(signal, latest_data, timeframe, lang):
    translations = {
        'ar': {
            'duration_map': {'1m': 'دقائق قليلة', '5m': 'بضع ساعات', '15m': 'عدة ساعات', '1h': 'يوم أو أكثر', '4h': 'عدة أيام'},
            'undefined_duration': 'غير محدد'
        },
        'en': {
            'duration_map': {'1m': 'A few minutes', '5m': 'A few hours', '15m': 'Several hours', '1h': 'A day or more', '4h': 'Several days'},
            'undefined_duration': 'Undefined'
        }
    }
    
    lang_translations = translations[lang]
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

    duration = lang_translations['duration_map'].get(timeframe, lang_translations['undefined_duration'])

    return {
        'entry_price': entry_price,
        'stop_loss': stop_loss,
        'target1': target1,
        'target2': target2,
        'target3': target3,
        'duration': duration
    }

def generate_trading_signal(analyzed_data, timeframe, lang):
    latest_data = analyzed_data.iloc[-1]
    latest_rsi = latest_data['rsi']
    latest_macd_hist = latest_data['macd_hist']
    latest_stoch_k = latest_data['stoch_k']
    
    signal = 'HOLD'
    pattern_signal, pattern_name = analyze_patterns(analyzed_data)

    if latest_rsi < 35 or latest_macd_hist > 0 or latest_stoch_k < 20:
        signal = 'BUY'
    elif latest_rsi > 65 or latest_macd_hist < 0 or latest_stoch_k > 80:
        signal = 'SELL'
    
    if pattern_signal is not None:
        signal = pattern_signal

    if signal != 'HOLD':
        trade_info = generate_trade_info(signal, latest_data, timeframe, lang)
        if trade_info:
            trade_info['pattern'] = pattern_name if pattern_name else None
            return signal, trade_info
    
    return 'HOLD', None

# --- Database & Subscription Management ---
DATABASE_NAME = 'crypto_bot.db'
TOKEN = os.getenv('TOKEN')
ADMIN_USER_ID = int(os.getenv('ADMIN_USER_ID'))
YOUR_WALLET_ADDRESS = os.getenv('YOUR_WALLET_ADDRESS')

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

def get_user_language(user_id):
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT language FROM users WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else 'ar'

def update_user_language(user_id, lang):
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute('UPDATE users SET language = ? WHERE user_id = ?', (lang, user_id))
    conn.commit()
    conn.close()

# --- Localization ---
SUBSCRIPTION_PACKAGES = {
    'daily': {'ar': 'الاشتراك اليومي', 'en': 'Daily Subscription', 'price': '4 USDT'},
    'weekly': {'ar': 'الاشتراك الأسبوعي', 'en': 'Weekly Subscription', 'price': '15 USDT'},
    'monthly': {'ar': 'الاشتراك الشهري', 'en': 'Monthly Subscription', 'price': '50 USDT'}
}

MESSAGES = {
    'ar': {
        'start_welcome': "مرحباً بك! أنا بوت تداول العملات الرقمية الخاص بك.",
        'start_features': (
            "يقدم البوت الميزات التالية:\n"
            "- **إشارات تداول** فورية للفرص الواعدة في السوق بالكامل.\n"
            "- **تنبيهات بالاكتتابات** الجديدة على منصة Binance.\n"
            "- **أهم الأخبار** المباشرة في سوق العملات الرقمية.\n"
            "- **تحليل فني** دقيق للعملات عند الطلب.\n"
            "- **نظام اشتراكات** سهل ومرن للوصول الكامل للميزات."
        ),
        'choose_lang': "يرجى اختيار لغتك المفضلة:",
        'main_menu': "أهلاً بك في القائمة الرئيسية. اختر ما تريد فعله:",
        'back_to_menu': "العودة للقائمة الرئيسية",
        'signal': "إشارة تداول",
        'analyze_symbol': "تحليل عملة",
        'subscribe': "اشتراك",
        'subscribe_info': (
            "اختر باقة الاشتراك المناسبة لك:\n\n"
            "**{daily_name}**: {daily_price}\n"
            "**{weekly_name}**: {weekly_price}\n"
            "**{monthly_name}**: {monthly_price}\n\n"
            "للاشتراك، قم بتحويل المبلغ إلى عنوان المحفظة التالي:\n\n`{wallet_address}`\n\n"
            "بعد التحويل، أرسل إثبات الدفع (صورة) إلى الآدمن عبر الرسائل الخاصة.\n"
            "رابط الآدمن: [الآدمن](tg://user?id={admin_id})"
        ),
        'unsubscribed_msg': "عذراً، هذه الميزة متاحة للمشتركين فقط. يرجى الاشتراك للوصول الكامل.",
        'analyzing': "جاري تحليل السوق...",
        'platform_error': "عذرًا، حدث خطأ أثناء الاتصال بالمنصة. يرجى المحاولة لاحقًا.",
        'signal_found': (
            "التحليل مكتمل. إشارة التداول الحالية لـ **{symbol}** هي: **{signal}**\n\n"
            "**تفاصيل الصفقة:**\n- **فاصل زمني:** 1 ساعة\n- **سعر الدخول:** {entry_price:.2f}\n"
            "- **وقف الخسارة:** {stop_loss:.2f}\n- **الهدف 1:** {target1:.2f}\n"
            "- **الهدف 2:** {target2:.2f}\n- **الهدف 3:** {target3:.2f}\n"
            "- **مدة الصفقة المتوقعة:** {duration}"
        ),
        'signal_found_pattern': "\n- **النموذج:** {pattern}",
        'no_signal': "عذرًا، لم يتم العثور على أي فرصة تداول واعدة في السوق حالياً.",
        'waiting_for_symbol': "من فضلك أرسل لي رمز العملة التي تريد تحليلها (مثال: ETHUSDT).",
        'invalid_symbol': "عذراً، هذا ليس رمز عملة صالح. يرجى إرسال رمز مثل `ETHUSDT`.",
        'analysis_complete_hold': "التحليل مكتمل. إشارة التداول الحالية لـ **{symbol}** هي: **HOLD**",
        'analysis_error': "عذرًا، لا يمكن تحليل عملة **{symbol}**. تأكد من أن الرمز صحيح.",
        'admin_only': "عذراً، هذا الأمر مخصص للآدمن فقط.",
        'activate_success': "تم تفعيل اشتراك المستخدم {user_id} بنجاح.",
        'deactivate_success': "تم إلغاء اشتراك المستخدم {user_id} بنجاح.",
        'status_msg': "حالة المستخدم {user_id}: {status}",
        'invalid_command': "الرجاء استخدام الأمر بالشكل الصحيح: {command}",
        'not_found': "المستخدم غير موجود.",
        'new_listing_alert': "🆕 **تنبيه اكتتاب جديد!** 🆕\n\nتم إدراج عملات جديدة في منصة التداول. إليك الرموز:\n\n",
        'news_alert': "📰 **خبر عاجل!** 📰\n\n**{title}**\n\n[اقرأ المزيد]({link})",
        'proactive_alert': (
            "🚨 **تنبيه إشارة تداول جديد!** 🚨\n\n"
            "**العملة:** {symbol}\n"
            "**الإشارة:** {signal}\n\n"
            "**تفاصيل الصفقة:**\n"
            "- **فاصل زمني:** {timeframe}\n"
            "- **سعر الدخول:** {entry_price:.2f}\n"
            "- **وقف الخسارة:** {stop_loss:.2f}\n"
            "- **الهدف 1:** {target1:.2f}\n"
            "- **الهدف 2:** {target2:.2f}\n"
            "- **الهدف 3:** {target3:.2f}\n"
            "- **مدة الصفقة المتوقعة:** {duration}\n"
        ),
        'proactive_alert_pattern': "- **النموذج:** {pattern}\n",
        'real_time_analysis': "\nتحليل السوق في الوقت الفعلي."
    },
    'en': {
        'start_welcome': "Hello! I am your cryptocurrency trading bot.",
        'start_features': (
            "The bot offers the following features:\n"
            "- **Trading signals** for promising opportunities in the market.\n"
            "- **New listing alerts** on the Binance platform.\n"
            "- **Breaking news** in the crypto market.\n"
            "- **Technical analysis** for any coin upon request.\n"
            "- **Flexible subscription system** for full access to all features."
        ),
        'choose_lang': "Please choose your preferred language:",
        'main_menu': "Welcome to the main menu. Please choose what you want to do:",
        'back_to_menu': "Back to Main Menu",
        'signal': "Trading Signal",
        'analyze_symbol': "Analyze Symbol",
        'subscribe': "Subscribe",
        'subscribe_info': (
            "Choose your subscription package:\n\n"
            "**{daily_name}**: {daily_price}\n"
            "**{weekly_name}**: {weekly_price}\n"
            "**{monthly_name}**: {monthly_price}\n\n"
            "To subscribe, transfer the amount to the following wallet address:\n\n`{wallet_address}`\n\n"
            "After the transfer, send a proof of payment (screenshot) to the admin via private message.\n"
            "Admin Link: [Admin](tg://user?id={admin_id})"
        ),
        'unsubscribed_msg': "Sorry, this feature is for subscribers only. Please subscribe for full access.",
        'analyzing': "Analyzing the market...",
        'platform_error': "Sorry, an error occurred while connecting to the platform. Please try again later.",
        'signal_found': (
            "Analysis complete. The current trading signal for **{symbol}** is: **{signal}**\n\n"
            "**Trade Details:**\n- **Timeframe:** 1 hour\n- **Entry Price:** {entry_price:.2f}\n"
            "- **Stop Loss:** {stop_loss:.2f}\n- **Target 1:** {target1:.2f}\n"
            "- **Target 2:** {target2:.2f}\n- **Target 3:** {target3:.2f}\n"
            "- **Expected Duration:** {duration}"
        ),
        'signal_found_pattern': "\n- **Pattern:** {pattern}",
        'no_signal': "Sorry, no promising trading opportunities were found in the market at the moment.",
        'waiting_for_symbol': "Please send me the coin symbol you want to analyze (e.g., ETHUSDT).",
        'invalid_symbol': "Sorry, this is not a valid coin symbol. Please send a symbol like `ETHUSDT`.",
        'analysis_complete_hold': "Analysis complete. The current trading signal for **{symbol}** is: **HOLD**",
        'analysis_error': "Sorry, cannot analyze the symbol **{symbol}**. Make sure the symbol is correct.",
        'admin_only': "Sorry, this command is for the admin only.",
        'activate_success': "Subscription for user {user_id} activated successfully.",
        'deactivate_success': "Subscription for user {user_id} deactivated successfully.",
        'status_msg': "Status for user {user_id}: {status}",
        'invalid_command': "Please use the command correctly: {command}",
        'not_found': "User not found.",
        'new_listing_alert': "🆕 **New Listing Alert!** 🆕\n\nNew coins have been listed on the trading platform. Here are the symbols:\n\n",
        'news_alert': "📰 **Breaking News!** 📰\n\n**{title}**\n\n[Read More]({link})",
        'proactive_alert': (
            "🚨 **New Trading Signal!** 🚨\n\n"
            "**Coin:** {symbol}\n"
            "**Signal:** {signal}\n\n"
            "**Trade Details:**\n"
            "- **Timeframe:** {timeframe}\n"
            "- **Entry Price:** {entry_price:.2f}\n"
            "- **Stop Loss:** {stop_loss:.2f}\n"
            "- **Target 1:** {target1:.2f}\n"
            "- **Target 2:** {target2:.2f}\n"
            "- **Target 3:** {target3:.2f}\n"
            "- **Expected Duration:** {duration}\n"
        ),
        'proactive_alert_pattern': "- **Pattern:** {pattern}\n",
        'real_time_analysis': "\nReal-time market analysis."
    }
}

def get_messages(lang):
    return MESSAGES.get(lang, MESSAGES['ar'])

def get_main_keyboard(lang):
    translations = get_messages(lang)
    keyboard = [
        [InlineKeyboardButton(translations['signal'], callback_data='signal')],
        [InlineKeyboardButton(translations['analyze_symbol'], callback_data='analyze_symbol')],
        [InlineKeyboardButton(translations['subscribe'], callback_data='subscribe')]
    ]
    return InlineKeyboardMarkup(keyboard)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    add_user_if_not_exists(user_id)
    
    keyboard = [
        [InlineKeyboardButton("العربية", callback_data='set_lang_ar')],
        [InlineKeyboardButton("English", callback_data='set_lang_en')]
    ]
    
    await update.message.reply_text("Please choose your language: / من فضلك اختر لغتك:", reply_markup=InlineKeyboardMarkup(keyboard))

async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    query = update.callback_query
    await query.answer()
    
    if user_id in user_state:
        del user_state[user_id]
        
    lang = get_user_language(user_id)
    translations = get_messages(lang)
    features_message = translations['start_features']
    
    await query.edit_message_text(features_message, reply_markup=get_main_keyboard(lang))

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    callback_data = query.data
    
    if callback_data.startswith('set_lang_'):
        lang = callback_data.split('_')[-1]
        update_user_language(user_id, lang)
        translations = get_messages(lang)
        await query.edit_message_text(translations['start_features'], reply_markup=get_main_keyboard(lang))
        return

    lang = get_user_language(user_id)
    translations = get_messages(lang)

    if callback_data == 'subscribe':
        message = translations['subscribe_info'].format(
            daily_name=SUBSCRIPTION_PACKAGES['daily'][lang],
            daily_price=SUBSCRIPTION_PACKAGES['daily']['price'],
            weekly_name=SUBSCRIPTION_PACKAGES['weekly'][lang],
            weekly_price=SUBSCRIPTION_PACKAGES['weekly']['price'],
            monthly_name=SUBSCRIPTION_PACKAGES['monthly'][lang],
            monthly_price=SUBSCRIPTION_PACKAGES['monthly']['price'],
            wallet_address=YOUR_WALLET_ADDRESS,
            admin_id=ADMIN_USER_ID
        )
        keyboard = [[InlineKeyboardButton(translations['back_to_menu'], callback_data='back_to_menu')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(message, parse_mode='Markdown', disable_web_page_preview=True, reply_markup=reply_markup)

    elif callback_data == 'signal':
        if not is_user_subscribed(user_id):
            keyboard = [[InlineKeyboardButton(translations['back_to_menu'], callback_data='back_to_menu')]]
            await query.edit_message_text(translations['unsubscribed_msg'], reply_markup=InlineKeyboardMarkup(keyboard))
            return

        await query.edit_message_text(translations['analyzing'])
        
        exchange = ccxt.binance()
        try:
            tickers = exchange.fetch_tickers()
        except Exception as e:
            await query.edit_message_text(translations['platform_error'])
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
                    signal, trade_info = generate_trading_signal(data, '1h', lang)
                    if signal != 'HOLD' and trade_info:
                        selected_symbol = symbol
                        trade_details = trade_info
                        break
            except Exception as e:
                print(f"Error checking {symbol}: {e}")
                continue
        
        if selected_symbol:
            message = translations['signal_found'].format(
                symbol=selected_symbol,
                signal=trade_details['signal'],
                entry_price=trade_details['entry_price'],
                stop_loss=trade_details['stop_loss'],
                target1=trade_details['target1'],
                target2=trade_details['target2'],
                target3=trade_details['target3'],
                duration=trade_details['duration']
            )
            if trade_details.get('pattern'):
                message += translations['signal_found_pattern'].format(pattern=trade_details['pattern'])

            keyboard = [[InlineKeyboardButton(translations['back_to_menu'], callback_data='back_to_menu')]]
            await query.edit_message_text(message, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            keyboard = [[InlineKeyboardButton(translations['back_to_menu'], callback_data='back_to_menu')]]
            await query.edit_message_text(translations['no_signal'], reply_markup=InlineKeyboardMarkup(keyboard))
            
    elif callback_data == 'analyze_symbol':
        if not is_user_subscribed(user_id):
            keyboard = [[InlineKeyboardButton(translations['back_to_menu'], callback_data='back_to_menu')]]
            await query.edit_message_text(translations['unsubscribed_msg'], reply_markup=InlineKeyboardMarkup(keyboard))
            return
        
        user_state[user_id] = 'waiting_for_symbol'
        
        keyboard = [[InlineKeyboardButton(translations['back_to_menu'], callback_data='back_to_menu')]]
        await query.edit_message_text(translations['waiting_for_symbol'], reply_markup=InlineKeyboardMarkup(keyboard))
    
    elif callback_data == 'back_to_menu':
        await back_to_menu(update, context)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_message = update.message.text
    lang = get_user_language(user_id)
    translations = get_messages(lang)
    
    if user_id in user_state and user_state[user_id] == 'waiting_for_symbol':
        del user_state[user_id]
        
        if len(user_message) > 4 and user_message.isalnum():
            await analyze_symbol_on_demand(update, context, user_message, lang)
        else:
            keyboard = [[InlineKeyboardButton(translations['back_to_menu'], callback_data='back_to_menu')]]
            await update.message.reply_text(translations['invalid_symbol'], reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text(translations['main_menu'], reply_markup=get_main_keyboard(lang))

async def analyze_symbol_on_demand(update: Update, context: ContextTypes.DEFAULT_TYPE, symbol: str, lang):
    translations = get_messages(lang)
    await update.message.reply_text(f"**{translations['analyzing']}** {symbol.upper()}...", parse_mode='Markdown')
    data = fetch_and_analyze_data(symbol=symbol.upper(), timeframe='1h')
    
    if data is not None:
        signal, trade_info = generate_trading_signal(data, '1h', lang)
        if signal != 'HOLD' and trade_info:
            message = translations['signal_found'].format(
                symbol=symbol.upper(),
                signal=signal,
                entry_price=trade_info['entry_price'],
                stop_loss=trade_info['stop_loss'],
                target1=trade_info['target1'],
                target2=trade_info['target2'],
                target3=trade_info['target3'],
                duration=trade_info['duration']
            )
            if trade_info.get('pattern'):
                message += translations['signal_found_pattern'].format(pattern=trade_info['pattern'])
        else:
            message = translations['analysis_complete_hold'].format(symbol=symbol.upper())
        
        keyboard = [[InlineKeyboardButton(translations['back_to_menu'], callback_data='back_to_menu')]]
        await update.message.reply_text(message, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        keyboard = [[InlineKeyboardButton(translations['back_to_menu'], callback_data='back_to_menu')]]
        await update.message.reply_text(translations['analysis_error'].format(symbol=symbol.upper()), reply_markup=InlineKeyboardMarkup(keyboard))

# --- Admin Commands ---
async def activate_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    lang = get_user_language(user_id)
    translations = get_messages(lang)
    if user_id != ADMIN_USER_ID:
        await update.message.reply_text(translations['admin_only'])
        return
    
    try:
        user_to_activate = int(context.args[0])
        update_subscription_status(user_to_activate, 1)
        await update.message.reply_text(translations['activate_success'].format(user_id=user_to_activate))
    except (IndexError, ValueError):
        await update.message.reply_text(translations['invalid_command'].format(command="/admin_activate [user_id]"))

async def deactivate_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    lang = get_user_language(user_id)
    translations = get_messages(lang)
    if user_id != ADMIN_USER_ID:
        await update.message.reply_text(translations['admin_only'])
        return
    
    try:
        user_to_deactivate = int(context.args[0])
        update_subscription_status(user_to_deactivate, 0)
        await update.message.reply_text(translations['deactivate_success'].format(user_id=user_to_deactivate))
    except (IndexError, ValueError):
        await update.message.reply_text(translations['invalid_command'].format(command="/admin_deactivate [user_id]"))

async def check_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    lang = get_user_language(user_id)
    translations = get_messages(lang)
    if user_id != ADMIN_USER_ID:
        await update.message.reply_text(translations['admin_only'])
        return
    
    try:
        user_to_check = int(context.args[0])
        status = get_user_status(user_to_check)
        await update.message.reply_text(translations['status_msg'].format(user_id=user_to_check, status=status))
    except (IndexError, ValueError):
        await update.message.reply_text(translations['invalid_command'].format(command="/admin_status [user_id]"))

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

async def send_alert(context: ContextTypes.DEFAULT_TYPE, user_id: int, symbol: str, timeframe: str, signal: str, trade_info: dict, lang: str):
    translations = get_messages(lang)
    message = translations['proactive_alert'].format(
        symbol=symbol,
        signal=signal,
        timeframe=timeframe,
        entry_price=trade_info['entry_price'],
        stop_loss=trade_info['stop_loss'],
        target1=trade_info['target1'],
        target2=trade_info['target2'],
        target3=trade_info['target3'],
        duration=trade_info['duration']
    )
    if trade_info.get('pattern'):
        message += translations['proactive_alert_pattern'].format(pattern=trade_info['pattern'])
    message += translations['real_time_analysis']

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
                    lang = 'ar'  # Default language for proactive alerts
                    signal, trade_info = generate_trading_signal(analyzed_data, timeframe, lang)
                    
                    if signal != 'HOLD':
                        last_signal_id = get_sent_signals(symbol, timeframe)
                        if not last_signal_id:
                            for user_id in get_subscribed_users():
                                user_lang = get_user_language(user_id)
                                await send_alert(context, user_id, symbol, timeframe, signal, trade_info, user_lang)
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
            subscribed_users = get_subscribed_users()
            for user_id in subscribed_users:
                lang = get_user_language(user_id)
                translations = get_messages(lang)
                message = translations['new_listing_alert']
                for symbol in new_listings:
                    message += f"- **{symbol}**\n"
                
                try:
                    await context.bot.send_message(chat_id=user_id, text=message, parse_mode='Markdown')
                    print(f"New listing alert sent to user {user_id}")
                except Exception as e:
                    print(f"Failed to send new listing alert to user {user_id}: {e}")
            
            for symbol in new_listings:
                cursor.execute('INSERT INTO sent_listings (symbol) VALUES (?)', (symbol,))
            
            conn.commit()
            conn.close()
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
                subscribed_users = get_subscribed_users()
                for user_id in subscribed_users:
                    lang = get_user_language(user_id)
                    translations = get_messages(lang)
                    message = translations['news_alert'].format(title=title, link=link)
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

    # --- Getting variables from Render's environment ---
    try:
        token = os.getenv('TOKEN')
        admin_user_id = int(os.getenv('ADMIN_USER_ID'))
        your_wallet_address = os.getenv('YOUR_WALLET_ADDRESS')
        if not token or not admin_user_id or not your_wallet_address:
            raise ValueError("Environment variables not set correctly.")
    except (ValueError, TypeError) as e:
        print(f"Error reading environment variables: {e}")
        return

    # Using the variables
    global TOKEN, ADMIN_USER_ID, YOUR_WALLET_ADDRESS
    TOKEN = token
    ADMIN_USER_ID = admin_user_id
    YOUR_WALLET_ADDRESS = your_wallet_address

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
