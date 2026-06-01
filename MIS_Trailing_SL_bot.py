# run_intraday_bot_professional_final.py

from kiteconnect import KiteConnect, KiteTicker
from dotenv import load_dotenv
import os
import logging
import time
from datetime import datetime, time as dtime, timedelta
import json
import threading
import pandas as pd
import pandas_ta as ta

def wait_for_market_open():
    """
    Waits for the market to open. If the script is run during market hours,
    it starts immediately. If run after hours, it schedules for the next day.
    """
    MARKET_OPEN = dtime(9, 15, 5)
    MARKET_CLOSE = dtime(15, 17, 0) # Based on the bot's AUTO_STOP_TIME

    now = datetime.now()
    current_time = now.time()

    if MARKET_OPEN <= current_time < MARKET_CLOSE:
        # If current time is within trading hours, start immediately.
        print("Market is already open. Starting trading logic immediately.")
        return
    
    # Determine the target start time
    if current_time < MARKET_OPEN:
        # If it's before market open today, wait for today's open.
        target_time = now.replace(hour=MARKET_OPEN.hour, minute=MARKET_OPEN.minute, second=MARKET_OPEN.second, microsecond=0)
        print(f"Waiting for market to open at {target_time.strftime('%H:%M:%S')}...")
    else: # current_time >= MARKET_CLOSE
        # If it's after market close, schedule for the next day.
        print("Market is closed for today. Scheduling for the next trading session.")
        tomorrow = now + timedelta(days=1)
        target_time = tomorrow.replace(hour=MARKET_OPEN.hour, minute=MARKET_OPEN.minute, second=MARKET_OPEN.second, microsecond=0)

    # Execute the waiting loop
    time_to_wait = (target_time - now).total_seconds()
    if time_to_wait > 0:
        print(f"Bot will start at {target_time.strftime('%Y-%m-%d %H:%M:%S')}.")
        while time_to_wait > 0:
            hrs, rem = divmod(time_to_wait, 3600)
            mins, secs = divmod(rem, 60)
            print(f"Time until start: {int(hrs):02d}:{int(mins):02d}:{int(secs):02d}", end='\r')
            time.sleep(1)
            time_to_wait -= 1
    
    print("\n--- Market open! Starting trading logic. ---")


class TradingBot:
    def __init__(self, portfolio_path='MIS_portfolio.json', settings_path='details_MIS.json'):
        # ... (initialization of keys, paths, etc. is the same) ...
        load_dotenv()
        self.api_key = os.getenv('KITE_API_KEY')
        self.access_token = os.getenv('KITE_ACCESS_TOKEN')
        if not self.api_key or not self.access_token:
            raise ValueError("API Key or Access Token not found in .env file.")
        self.portfolio_path = portfolio_path
        self.settings_path = settings_path
        self.portfolio = []
        self.portfolio_lock = threading.Lock()
        self.AUTO_STOP_TIME = dtime(15, 17)
        self.setup_logging()
        self.load_settings()
        self.kite = KiteConnect(api_key=self.api_key)
        self.kite.set_access_token(self.access_token)
        self.instrument_map = {}
        self.token_to_symbol = {}
        self.kws = None
        self.daily_pnl = 0.0

        # --- NEW: THIS IS THE BOT'S MEMORY TO PREVENT RE-ENTRY ---
        self.traded_symbols_today = set()

    def setup_logging(self):
        logging.basicConfig(filename='tradingbot.log', level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
        console = logging.StreamHandler()
        console.setLevel(logging.INFO)
        logging.getLogger('').addHandler(console)

    def load_settings(self):
        try:
            with open(self.settings_path, 'r') as f:
                settings = json.load(f)
                self.max_daily_loss = settings.get("max_daily_loss", 2000)
                self.risk_per_trade = settings.get("risk_per_trade", 500)
                logging.info(f"Settings loaded: Max Daily Loss={self.max_daily_loss}, Risk Per Trade={self.risk_per_trade}")
        except FileNotFoundError:
            logging.error(f"Settings file '{self.settings_path}' not found. Using defaults.")
            self.max_daily_loss = 2000
            self.risk_per_trade = 500

    def mark_trade_as_complete(self, stock, reason="SL"):
        """Central function to close a trade and add it to the 'traded today' list."""
        if not stock.get('active_trade'):
            return # Avoid duplicate processing

        try:
            # Place the sell order
            ltp = self.kite.ltp(f"NSE:{stock['symbol']}")[f"NSE:{stock['symbol']}"]['last_price']
            self.kite.place_order(variety="regular", tradingsymbol=stock['symbol'], exchange="NSE",
                                  transaction_type="SELL", quantity=stock['qty'],
                                  order_type="MARKET", product="MIS")
            
            # Update PNL and state
            realized_pnl = (ltp - stock['buy_price']) * stock['qty']
            self.daily_pnl += realized_pnl
            stock["active_trade"] = False
            
            # --- CRITICAL: ADD TO TRADED LIST ---
            self.traded_symbols_today.add(stock['symbol'])
            
            logging.info(f"Closed {stock['symbol']} due to {reason}. Realized PNL: {realized_pnl:.2f}. Total Daily PNL: {self.daily_pnl:.2f}")

        except Exception as e:
            logging.error(f"Failed to close position for {stock['symbol']} due to {reason}: {e}")

    def on_ticks(self, ws, ticks):
        with self.portfolio_lock:
            for tick in ticks:
                symbol = self.token_to_symbol.get(tick['instrument_token'])
                if not symbol: continue
                stock = next((s for s in self.portfolio if s['symbol'] == symbol), None)
                if not stock or not stock.get("active_trade"): continue
                
                ltp = tick['last_price']
                if ltp > stock.get("high", ltp): stock["high"] = ltp
                
                stop_loss_points = stock['atr'] * stock.get('atr_multiplier', 2)
                trailing_sl = round(stock["high"] - stop_loss_points, 2)
                
                logging.info(f"[{symbol}] LTP: {ltp}, High: {stock['high']:.2f}, SL: {trailing_sl:.2f} (ATR Based)")

                if ltp <= trailing_sl:
                    self.mark_trade_as_complete(stock, reason="StopLoss")

    def auto_square_off(self):
        logging.info(f"--- AUTO-SQUARE-OFF at {self.AUTO_STOP_TIME} ---")
        with self.portfolio_lock:
            for stock in self.portfolio:
                if stock.get("active_trade"):
                    self.mark_trade_as_complete(stock, reason="AutoSquareOff")

    def check_for_portfolio_updates(self):
        """The core function for dynamic management, now with re-entry protection."""
        target_portfolio = self.load_portfolio_from_json()
        
        with self.portfolio_lock:
            current_symbols = {s['symbol'] for s in self.portfolio}
            target_symbols = {s['symbol'] for s in target_portfolio if s.get('active')}

            # --- 1. Add new stocks (with re-entry check) ---
            new_symbols = target_symbols - current_symbols
            for symbol in new_symbols:
                # --- RE-ENTRY PROTECTION ---
                if symbol in self.traded_symbols_today:
                    logging.info(f"Ignoring '{symbol}' as it has already been traded today.")
                    continue
                
                new_stock_config = next(s for s in target_portfolio if s['symbol'] == symbol)
                logging.info(f"New stock '{symbol}' detected. Initiating trade.")
                self.portfolio.append(new_stock_config)
                self.fetch_instrument_tokens([symbol])
                threading.Thread(target=self.place_order_for_stock, args=(new_stock_config,)).start()

            # --- 2. Remove stocks ---
            removed_symbols = current_symbols - target_symbols
            for symbol in removed_symbols:
                stock_to_remove = next((s for s in self.portfolio if s['symbol'] == symbol), None)
                if stock_to_remove:
                    if stock_to_remove.get('active_trade'):
                        logging.info(f"Stock '{symbol}' set to inactive. Closing position.")
                        self.mark_trade_as_complete(stock_to_remove, reason="ConfigChange")
                    # No need for an else, if it's not active, it will just be ignored
    
    # ... (All other functions like place_order_for_stock, run, etc., are the same as the previous version) ...
    def load_portfolio_from_json(self):
        try:
            with open(self.portfolio_path, 'r') as f: return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            logging.error(f"Error reading portfolio file: {e}"); return []
    def fetch_instrument_tokens(self, symbols_to_fetch):
        try:
            instruments = self.kite.instruments("NSE")
            for symbol in symbols_to_fetch:
                if symbol not in self.instrument_map:
                    instrument = next((inst for inst in instruments if inst['tradingsymbol'] == symbol), None)
                    if instrument:
                        token = instrument['instrument_token']
                        self.instrument_map[symbol] = token
                        self.token_to_symbol[token] = symbol
                    else: logging.warning(f"Could not find instrument for {symbol}.")
        except Exception as e: logging.error(f"Error fetching instrument tokens: {e}")
    def calculate_atr(self, symbol, instrument_token):
        try:
            to_date = datetime.now().date()
            from_date = to_date - timedelta(days=30)
            hist_data = self.kite.historical_data(instrument_token, from_date, to_date, "day")
            if not hist_data: return None
            df = pd.DataFrame(hist_data)
            df['atr'] = df.ta.atr(length=14)
            return df['atr'].iloc[-1]
        except Exception as e:
            logging.error(f"Failed to calculate ATR for {symbol}: {e}"); return None
    def place_order_for_stock(self, stock):
        symbol = stock['symbol']; symbol_full = f"NSE:{symbol}"
        instrument_token = self.instrument_map.get(symbol)
        if not instrument_token: return False
        stock['atr'] = self.calculate_atr(symbol, instrument_token)
        if not stock['atr']:
            logging.error(f"Skipping {symbol} as ATR could not be calculated."); return False
        max_wait = 300; waited = 0
        logging.info(f"Attempting to place order for {symbol} with ATR={stock['atr']:.2f}")
        while waited < max_wait and datetime.now().time() <= self.AUTO_STOP_TIME:
            try:
                ltp_data = self.kite.ltp(symbol_full)
                quote = self.kite.quote(symbol_full)
                stock['prev_close'] = quote[symbol_full]['ohlc']['close']
                ltp = ltp_data[symbol_full]['last_price']
                band = stock.get("buy_range_pct", 0.1) / 100
                lower = round(stock['prev_close'] * (1 - band), 2)
                upper = round(stock['prev_close'] * (1 + band), 2)
                logging.info(f"Checking {symbol}: LTP={ltp}, Range=[{lower}, {upper}]")
                if lower <= ltp <= upper:
                    stop_loss_points = stock['atr'] * stock.get('atr_multiplier', 2)
                    risk_per_share = stop_loss_points
                    if risk_per_share <= 0: return False
                    quantity = int(self.risk_per_trade / risk_per_share)
                    if quantity < 1: return False
                    stock['qty'] = quantity
                    logging.info(f"Calculated position size for {symbol}: {quantity} shares.")
                    order_id = self.kite.place_order(variety='regular', tradingsymbol=symbol, exchange="NSE", transaction_type="BUY", quantity=quantity, order_type="MARKET", product="MIS")
                    with self.portfolio_lock:
                        stock.update({"active_trade": True, "buy_price": ltp, "high": ltp})
                    logging.info(f"BUY ORDER PLACED for {quantity} shares of {symbol} at {ltp}. Order ID: {order_id}")
                    return True
            except Exception as e:
                logging.error(f"Order logic failed for {symbol}: {e}"); return False
            time.sleep(2); waited += 2
        logging.warning(f"{symbol} was not purchased (timeout)."); return False
    def on_connect(self, ws, response):
        logging.info("WebSocket connected. Subscribing to all active tokens.")
        with self.portfolio_lock:
            tokens = [self.instrument_map[s['symbol']] for s in self.portfolio if s.get('active_trade') and s['symbol'] in self.instrument_map]
        if tokens:
            ws.subscribe(tokens); ws.set_mode(ws.MODE_FULL, tokens)
    def run(self):
        self.kws = KiteTicker(self.api_key, self.access_token)
        self.kws.on_ticks = self.on_ticks
        self.kws.on_connect = self.on_connect
        self.kws.connect(threaded=True)
        while not self.kws.is_connected(): time.sleep(1)
        try:
            while True:
                self.check_for_portfolio_updates()
                if self.daily_pnl <= -self.max_daily_loss:
                    logging.critical(f"CRITICAL: Daily loss limit of {-self.max_daily_loss} hit. SHUTTING DOWN.")
                    self.auto_square_off(); break
                if datetime.now().time() >= self.AUTO_STOP_TIME:
                    self.auto_square_off(); break
                if not any(s.get("active_trade") for s in self.portfolio):
                    logging.info("No active positions. Monitoring config file for new trades...")
                time.sleep(5)
            logging.info("--- Bot has finished its session. ---")
        except KeyboardInterrupt:
            logging.info("Bot manually stopped by user.")
        finally:
            if self.kws and self.kws.is_connected(): self.kws.close()

if __name__ == "__main__":
    wait_for_market_open()

    # Get the absolute path of the directory where the script is located
    script_dir = os.path.dirname(os.path.realpath(__file__))
    
    # Construct absolute paths for the config files by joining the script's directory with the filenames
    portfolio_file_path = os.path.join(script_dir, 'MIS_portfolio.json')
    settings_file_path = os.path.join(script_dir, 'details_MIS.json')
    
    # Pass these absolute paths when creating the bot instance
    bot = TradingBot(portfolio_path=portfolio_file_path, settings_path=settings_file_path)
    
    bot.run()


# How to reactivate the stock
#Re-activating the Stock: 
#If you subsequently edit the file again and set the stock back to "active": true, the bot will detect it as a "new" stock
#because it is present in the JSON file but no longer in the bot's active memory. 
#This will trigger the place_order_for_stock function all over again, starting a brand new 300-second buying attempt.