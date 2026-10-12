import asyncio
import json
import os
import pandas as pd
import pandas_ta as ta
import requests
from flask import Flask, render_template, request, jsonify
from metaapi_cloud_sdk import MetaApi
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)

CONFIG_FILE = "config.json"
EXAMPLE_CONFIG = "config.json.example"
signal_logs = []


def run_async(coro):
    """Utility runner for executing asynchronous MetaAPI calls in synchronous Flask routes."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def load_config():
    """Loads application configuration from config.json or fallback example."""
    target_file = CONFIG_FILE if os.path.exists(CONFIG_FILE) else EXAMPLE_CONFIG
    try:
        with open(target_file, "r") as f:
            return json.load(f)
    except Exception as e:
        print(f"Config load error: {e}")
        return {
            "META_API_TOKEN": "",
            "ACCOUNT_ID": "",
            "TELEGRAM_BOT_TOKEN": "",
            "TELEGRAM_CHAT_ID": "",
            "TELEGRAM_ENABLED": True,
            "CONFIDENCE_THRESHOLD": 70,
            "SYMBOL": "XAUUSD",
            "TIMEFRAME": "15m",
            "AUTO_RUN_INTERVAL_MINUTES": 15,
            "AUTO_TRADING_ENABLED": False,
            "DEFAULT_LOT_SIZE": 0.01,
            "DEFAULT_STOP_LOSS_PIPS": 3.0,
            "DEFAULT_TAKE_PROFIT_PIPS": 6.0,
            "MAX_OPEN_TRADES": 1
        }


def save_config(data):
    """Saves active configuration settings to config.json."""
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=4)


def send_telegram_alert(message, config):
    """Dispatches markdown-formatted alert messages to Telegram."""
    if not config.get("TELEGRAM_ENABLED") or not config.get("TELEGRAM_BOT_TOKEN"):
        return False
    url = f"https://api.telegram.org/bot{config['TELEGRAM_BOT_TOKEN']}/sendMessage"
    payload = {
        "chat_id": config["TELEGRAM_CHAT_ID"],
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        res = requests.post(url, json=payload, timeout=8)
        return res.status_code == 200
    except Exception as e:
        print(f"Telegram dispatch failed: {e}")
        return False


# --- MetaAPI Connection & Trading Engine ---

async def get_metaapi_connection(config):
    """Initializes and returns synchronized MetaAPI RPC connection."""
    api = MetaApi(token=config["META_API_TOKEN"])
    account = await api.metatrader_account_api.get_account(config["ACCOUNT_ID"])
    await account.deploy()
    await account.wait_connected()
    connection = account.get_rpc_connection()
    await connection.connect()
    await connection.wait_synchronized()
    return connection


async def fetch_historical_candles(config):
    """Fetches historical OHLC candle data for indicator calculations."""
    connection = await get_metaapi_connection(config)
    candles = await connection.get_historical_candles(
        config["SYMBOL"], config["TIMEFRAME"], limit=100
    )
    df = pd.DataFrame(candles)
    df['close'] = df['close'].astype(float)
    return df


async def fetch_open_positions_async():
    """Fetches currently open MetaTrader account positions."""
    config = load_config()
    connection = await get_metaapi_connection(config)
    positions = await connection.get_positions()
    return positions


async def execute_market_trade_async(symbol, action, volume, stop_loss=None, take_profit=None):
    """Executes a Market Buy or Sell order via MetaAPI RPC."""
    config = load_config()
    connection = await get_metaapi_connection(config)
    
    sl = float(stop_loss) if stop_loss else None
    tp = float(take_profit) if take_profit else None
    vol = float(volume)

    if action.upper() == "BUY":
        result = await connection.create_market_buy_order(symbol, vol, sl, tp)
    elif action.upper() == "SELL":
        result = await connection.create_market_sell_order(symbol, vol, sl, tp)
    else:
        raise ValueError("Invalid trade action specified.")
    
    return result


async def close_position_async(position_id):
    """Closes an individual open position by ID."""
    config = load_config()
    connection = await get_metaapi_connection(config)
    result = await connection.close_position(position_id)
    return result


# --- Strategy & Indicator Calculations ---

def analyze_market_data(df):
    """Calculates EMA, RSI, and MACD indicators to determine signal direction & score."""
    df['ema_20'] = ta.ema(df['close'], length=20)
    df['ema_50'] = ta.ema(df['close'], length=50)
    df['rsi'] = ta.rsi(df['close'], length=14)
    macd = ta.macd(df['close'])
    df['macd'] = macd['MACD_12_26_9']
    df['macd_signal'] = macd['MACDs_12_26_9']

    latest = df.iloc[-1]
    prev = df.iloc[-2]

    buy_score = 0
    sell_score = 0

    # 1. EMA Crossover (30%)
    if latest['ema_20'] > latest['ema_50']:
        buy_score += 30
    else:
        sell_score += 30

    # 2. RSI Momentum (30%)
    if 50 < latest['rsi'] < 70:
        buy_score += 30
    elif 30 < latest['rsi'] <= 50:
        sell_score += 30
    elif latest['rsi'] <= 30:
        buy_score += 25
    elif latest['rsi'] >= 70:
        sell_score += 25

    # 3. MACD Crossover (40%)
    if prev['macd'] < prev['macd_signal'] and latest['macd'] > latest['macd_signal']:
        buy_score += 40
    elif prev['macd'] > prev['macd_signal'] and latest['macd'] < latest['macd_signal']:
        sell_score += 40
    elif latest['macd'] > latest['macd_signal']:
        buy_score += 20
    else:
        sell_score += 20

    direction = "BUY" if buy_score >= sell_score else "SELL"
    confidence = buy_score if direction == "BUY" else sell_score

    return {
        "direction": direction,
        "confidence": confidence,
        "price": round(float(latest['close']), 2),
        "rsi": round(float(latest['rsi']), 2),
        "ema_trend": "Bullish" if latest['ema_20'] > latest['ema_50'] else "Bearish",
        "macd_status": "Bullish" if latest['macd'] > latest['macd_signal'] else "Bearish"
    }


def execute_full_analysis_cycle():
    """Runs data retrieval, signal generation, auto-trading execution, and Telegram alerting."""
    config = load_config()
    if not config.get("META_API_TOKEN") or not config.get("ACCOUNT_ID"):
        return {"status": "error", "message": "MetaAPI broker credentials missing in configuration."}

    try:
        df = run_async(fetch_historical_candles(config))
        result = analyze_market_data(df)

        threshold = int(config.get("CONFIDENCE_THRESHOLD", 70))
        sent_telegram = False
        auto_traded = False

        # Signal Alert Dispatch
        if result["confidence"] >= threshold:
            msg = (
                f"🔥 *GOLD (XAU/USD) SIGNAL* 🔥\n\n"
                f"📈 *Direction:* {result['direction']}\n"
                f"🎯 *Confidence:* {result['confidence']}%\n"
                f"💰 *Market Price:* ${result['price']}\n"
                f"⏱️ *Timeframe:* {config['TIMEFRAME']}\n"
            )
            sent_telegram = send_telegram_alert(msg, config)

            # Execution Engine (Auto-Trader)
            if config.get("AUTO_TRADING_ENABLED", False):
                open_positions = run_async(fetch_open_positions_async())
                active_xau_trades = [p for p in open_positions if p.get('symbol') == config["SYMBOL"]]

                max_trades = int(config.get("MAX_OPEN_TRADES", 1))
                if len(active_xau_trades) < max_trades:
                    lot = float(config.get("DEFAULT_LOT_SIZE", 0.01))
                    sl_pips = float(config.get("DEFAULT_STOP_LOSS_PIPS", 3.0))
                    tp_pips = float(config.get("DEFAULT_TAKE_PROFIT_PIPS", 6.0))
                    
                    price = result["price"]
                    sl = (price - sl_pips) if result["direction"] == "BUY" else (price + sl_pips)
                    tp = (price + tp_pips) if result["direction"] == "BUY" else (price - tp_pips)

                    run_async(execute_market_trade_async(config["SYMBOL"], result["direction"], lot, sl, tp))
                    auto_traded = True

                    trade_msg = (
                        f"⚡ *AUTOMATED TRADE EXECUTED* ⚡\n\n"
                        f"Action: *{result['direction']}*\n"
                        f"Symbol: *{config['SYMBOL']}*\n"
                        f"Lot Size: *{lot}*\n"
                        f"Entry Price: *${price}*\n"
                        f"SL: *${sl:.2f}* | TP: *${tp:.2f}*\n"
                    )
                    send_telegram_alert(trade_msg, config)

        log_entry = {
            "time": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
            "direction": result["direction"],
            "confidence": result["confidence"],
            "price": result["price"],
            "sent_telegram": sent_telegram,
            "auto_traded": auto_traded,
            "rsi": result["rsi"],
            "ema_trend": result["ema_trend"],
            "macd_status": result["macd_status"]
        }
        signal_logs.insert(0, log_entry)
        if len(signal_logs) > 50:
            signal_logs.pop()

        return {"status": "success", "data": result, "sent_telegram": sent_telegram, "auto_traded": auto_traded}

    except Exception as e:
        print(f"Execution Error: {e}")
        return {"status": "error", "message": str(e)}


# --- Web Server Endpoints ---

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/positions")
def positions_page():
    return render_template("positions.html")


@app.route("/settings")
def settings_page():
    config = load_config()
    return render_template("settings.html", config=config)


@app.route("/api/settings", methods=["POST"])
def update_settings():
    new_config = request.json
    save_config(new_config)
    reschedule_background_job()
    return jsonify({"status": "success", "message": "Settings updated and scheduler restarted!"})


@app.route("/api/analyze", methods=["GET"])
def trigger_analysis():
    res = execute_full_analysis_cycle()
    if res["status"] == "error":
        return jsonify(res), 400
    return jsonify(res)


@app.route("/api/logs", methods=["GET"])
def get_logs():
    return jsonify(signal_logs)


@app.route("/api/positions", methods=["GET"])
def get_positions():
    try:
        positions = run_async(fetch_open_positions_async())
        return jsonify({"status": "success", "positions": positions})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/trade/open", methods=["POST"])
def manual_trade():
    try:
        data = request.json
        symbol = data.get("symbol", "XAUUSD")
        action = data.get("action")
        volume = data.get("volume", 0.01)
        stop_loss = data.get("stopLoss")
        take_profit = data.get("takeProfit")

        trade_res = run_async(execute_market_trade_async(symbol, action, volume, stop_loss, take_profit))
        
        config = load_config()
        send_telegram_alert(
            f"🖐️ *MANUAL TRADE OPENED*\n\nAction: *{action}*\nSymbol: *{symbol}*\nVolume: *{volume}*", 
            config
        )
        return jsonify({"status": "success", "result": trade_res})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/trade/close", methods=["POST"])
def close_trade():
    try:
        data = request.json
        position_id = data.get("positionId")
        res = run_async(close_position_async(position_id))
        return jsonify({"status": "success", "result": res})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/trade/close-all", methods=["POST"])
def close_all_trades():
    try:
        positions = run_async(fetch_open_positions_async())
        closed_count = 0
        for pos in positions:
            run_async(close_position_async(pos["id"]))
            closed_count += 1
        return jsonify({"status": "success", "closed_count": closed_count})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# --- Scheduler Initialization ---

scheduler = BackgroundScheduler()

def reschedule_background_job():
    config = load_config()
    interval = int(config.get("AUTO_RUN_INTERVAL_MINUTES", 15))
    scheduler.remove_all_jobs()
    scheduler.add_job(func=execute_full_analysis_cycle, trigger="interval", minutes=interval, id="xauusd_trading_job")

scheduler.start()
reschedule_background_job()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
