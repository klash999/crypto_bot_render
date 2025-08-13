from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters, JobQueue
import asyncio
import ccxt
import pandas as pd
import talib
import sqlite3
import datetime
import os
import requests
from tradingview_ta import TA_Handler, Interval, Exchange

# --- Bot Configuration ---
TOKEN = os.getenv('TOKEN')
ADMIN_USER_ID = int(os.getenv('ADMIN_USER_ID'))

# --- Database & Subscription Management ---
DATABASE_NAME = 'crypto_bot.db'

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
            symbol TEXT,
            timeframe TEXT,
            signal TEXT,
            timestamp DATETIME,
            PRIMARY KEY (symbol, timeframe)
        )
    ''')
    conn.commit()
    conn.close()

def get_subscribed_users():
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT user_id, language FROM users WHERE is_subscribed = 1')
    results = cursor.fetchall()
    conn.close()
    return results

def is_user_subscribed(user_id):
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT is_subscribed FROM users WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()
    conn.close()
    return result and result[0] == 1

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
    cursor.execute('INSERT OR REPLACE INTO sent_signals (symbol, timeframe, signal, timestamp) VALUES (?, ?, ?, ?)', (symbol, timeframe, signal, datetime.datetime.now()))
    conn.commit()
    conn.close()

# --- Localization & UI ---
MESSAGES = {
    'ar': {
        'welcome': "مرحباً! أنا بوت تداول تلقائي. 🤖",
        'main_menu': "أهلاً بك في القائمة الرئيسية.",
        'analyzing': "جاري تحليل السوق...",
        'signal_found': "🚨 **تنبيه إشارة تداول جديدة!** 🚨\n\n**العملة:** {symbol}\n**الفاصل الزمني:** {timeframe}\n**الإشارة:** `{signal}`",
        'admin_only': "عذراً، هذا الأمر للآدمن فقط.",
        'activate_success': "✅ تم تفعيل اشتراك المستخدم {user_id}.",
    }
}

def get_messages(lang):
    return MESSAGES.get(lang, MESSAGES['ar'])

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    add_user_if_not_exists(user_id)
    lang = get_user_language(user_id)
    translations = get_messages(lang)
    
    await update.message.reply_text(translations['welcome'])
    
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
        await update.message.reply_text(f"الرجاء استخدام الأمر بالشكل الصحيح: /admin_activate [user_id]")

# --- Proactive Alerting System ---
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "ADAUSDT", "XRPUSDT"]
TIMEFRAMES = {
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
        await context.bot.send_message(chat_id=user_id, text=message, parse_mode='Markdown')
        print(f"Alert sent to user {user_id} for {symbol} on {timeframe} - {signal}")
    except Exception as e:
        print(f"Failed to send alert to user {user_id}: {e}")

async def monitor_tradingview_signals(context: ContextTypes.DEFAULT_TYPE):
    print("Running autonomous market scan...")
    subscribed_users = get_subscribed_users()
    
    for symbol in SYMBOLS:
        for timeframe_str, timeframe_enum in TIMEFRAMES.items():
            try:
                handler = TA_Handler(
                    symbol=symbol,
                    screener="crypto",
                    exchange="BINANCE",
                    interval=timeframe_enum,
                )
                analysis = handler.get_analysis()
                
                if analysis and analysis.summary:
                    recommendation = analysis.summary['RECOMMENDATION']
                    
                    if recommendation in ['STRONG_BUY', 'BUY', 'STRONG_SELL', 'SELL']:
                        signal = "BUY" if "BUY" in recommendation else "SELL"
                        
                        last_signal = get_last_sent_signal(symbol, timeframe_str)
                        
                        if not last_signal or last_signal[0] != signal:
                            for user_id, lang in subscribed_users:
                                await send_alert(context, user_id, symbol, timeframe_str, signal, lang)
                            save_sent_signal(symbol, timeframe_str, signal)
            except Exception as e:
                print(f"Error fetching signal for {symbol} on {timeframe_str}: {e}")
                
def main():
    setup_database()
    
    if not TOKEN or not ADMIN_USER_ID:
        print("Please set the TOKEN and ADMIN_USER_ID environment variables.")
        return
        
    app = Application.builder().token(TOKEN).build()
    job_queue = app.job_queue
    
    # Run the signal monitor every 5 minutes
    job_queue.run_repeating(monitor_tradingview_signals, interval=300, first=datetime.time(0, 0))

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("admin_activate", activate_subscription))
    
    print("Bot is running and monitoring signals automatically...")
    app.run_polling()

if __name__ == "__main__":
    main()
