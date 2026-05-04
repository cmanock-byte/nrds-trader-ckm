import streamlit as st
import pandas as pd
import pandas_ta as ta
import plotly.graph_objects as go
import datetime
import pytz
import uuid
from streamlit_autorefresh import st_autorefresh
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

# ================================================================
# SECTION 1 - PAGE SETUP & AUTOREFRESH
# ================================================================
st.set_page_config(page_title="NRDS Trading Bot", layout="wide")
st.title("NRDS Trading Bot")
count = st_autorefresh(interval=30000, limit=None, key="data_refresh")

# ================================================================
# SECTION 2 - API KEYS, MODE, CLIENTS & COOLDOWN
# ================================================================
API_KEY = st.secrets["ALPACA_API_KEY"]
SECRET_KEY = st.secrets["ALPACA_SECRET_KEY"]
PAPER_MODE = st.secrets.get("PAPER_MODE", "true").lower() == "true"
SEED_CAPITAL = float(st.secrets.get("SEED_CAPITAL", "300"))
RESET_AFTER = st.secrets.get("RESET_AFTER", None)

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=PAPER_MODE)
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

if PAPER_MODE:
    st.success("PAPER TRADING MODE - No real money at risk.")
else:
    st.error("LIVE TRADING MODE - Real money. Real consequences.")
if "paused" not in st.session_state:
    st.session_state["paused"] = False
PAUSED = st.toggle("Pause Bot", value=st.session_state["paused"])
st.session_state["paused"] = PAUSED
if PAUSED:
    st.error("BOT PAUSED - Flip the toggle to resume.")

# --- 60-Second Order Cooldown (Race Condition Fix) ---
# After ANY order, block all new orders for 60 seconds.
# Tracked in local session memory, immune to Alpaca API lag.
ORDER_COOLDOWN_SECONDS = 60

if "last_order_time" not in st.session_state:
    st.session_state["last_order_time"] = datetime.datetime.min

def can_submit_order():
    elapsed = (datetime.datetime.now() - st.session_state["last_order_time"]).total_seconds()
    return elapsed >= ORDER_COOLDOWN_SECONDS

def cooldown_remaining():
    elapsed = (datetime.datetime.now() - st.session_state["last_order_time"]).total_seconds()
    return max(0, ORDER_COOLDOWN_SECONDS - elapsed)

def mark_order_submitted():
    st.session_state["last_order_time"] = datetime.datetime.now()
# --- Market Hours Gate ---
def market_is_open():
    now_et = datetime.datetime.now(EST)
    if now_et.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    market_open_time = datetime.time(9, 30, 0)
    market_close_time = datetime.time(16, 0, 0)
    return market_open_time <= now_et.time() <= market_close_time

# --- Budget Safety Factor (prevents "insufficient buying power" errors) ---
# Uses 95% of available budget to absorb price movement between
# signal detection and order execution.
BUDGET_SAFETY_FACTOR = 0.95

# ================================================================
# SECTION 3 - TICKER CONFIGURATION
# ================================================================
EST = pytz.timezone('America/New_York')

TICKERS = {
    "NRDS": {"bb_std": 1.5, "rsi_buy": 35, "rsi_sell": 65, "profit_target": 0.08,
             "blackout_start": EST.localize(datetime.datetime(2026, 5, 4, 0, 0, 0)),
             "blackout_end": EST.localize(datetime.datetime(2026, 5, 13, 23, 59, 59))},
    "OPFI": {"bb_std": 1.5, "rsi_buy": 35, "rsi_sell": 65, "profit_target": 0.08,
             "blackout_start": EST.localize(datetime.datetime(2026, 5, 4, 0, 0, 0)),
             "blackout_end": EST.localize(datetime.datetime(2026, 5, 13, 23, 59, 59))},
    "PTON": {"bb_std": 1.2, "rsi_buy": 30, "rsi_sell": 60, "profit_target": 0.06,
             "blackout_start": None, "blackout_end": None},
    "OPEN": {"bb_std": 1.8, "rsi_buy": 30, "rsi_sell": 60, "profit_target": 0.10,
             "blackout_start": None, "blackout_end": None},
    "PENN": {"bb_std": 1.5, "rsi_buy": 35, "rsi_sell": 65, "profit_target": 0.12,
             "blackout_start": None, "blackout_end": None},
    "PUBM": {"bb_std": 1.5, "rsi_buy": 35, "rsi_sell": 65, "profit_target": 0.08,
             "blackout_start": None, "blackout_end": None},
}

# ================================================================
# SECTION 4 - FETCH 1-MINUTE DATA FOR ALL TICKERS
# ================================================================
end_time = datetime.datetime.now(EST)
start_time = end_time - datetime.timedelta(days=3)
ticker_data = {}

for symbol, config in TICKERS.items():
    try:
        request_params = StockBarsRequest(
            symbol_or_symbols=symbol, timeframe=TimeFrame.Minute,
            start=start_time, end=end_time, feed="iex")
        bars = data_client.get_stock_bars(request_params)
        df = bars.df.reset_index()
        df.set_index('timestamp', inplace=True)
        df.index = df.index.tz_convert('America/New_York')
        bbands = ta.bbands(df['close'], length=20, std=config["bb_std"])
        df = pd.concat([df, bbands], axis=1)
        df['RSI_6'] = ta.rsi(df['close'], length=6)
        df['VWAP'] = ta.vwap(df['high'], df['low'], df['close'], df['volume'])
        lower_bb_col = [col for col in df.columns if col.startswith('BBL')][0]
        upper_bb_col = [col for col in df.columns if col.startswith('BBU')][0]
        latest = df.iloc[-1]
        ticker_data[symbol] = {
            "df": df, "current_price": latest['close'],
            "rsi_val": latest['RSI_6'], "lower_bb": latest[lower_bb_col],
            "upper_bb": latest[upper_bb_col],
            "lower_bb_col": lower_bb_col, "upper_bb_col": upper_bb_col,
        }
    except Exception as e:
        st.warning(f"Could not fetch data for {symbol}: {e}")

# ================================================================
# SECTION 5 - RECONSTRUCT SLOT POSITIONS FROM ORDER HISTORY
#
# If RESET_AFTER is set in Secrets, all orders before that date
# are ignored. This lets you start fresh after a bug without
# resetting your Alpaca account.
# ================================================================
all_symbols = list(TICKERS.keys())
orders_req = GetOrdersRequest(
    status=QueryOrderStatus.CLOSED, symbols=all_symbols, limit=500)
closed_orders = trading_client.get_orders(filter=orders_req)

closed_orders_sorted = sorted(
    [o for o in closed_orders if o.filled_qty and float(o.filled_qty) > 0],
    key=lambda o: o.filled_at)

if RESET_AFTER:
    reset_dt = datetime.datetime.fromisoformat(RESET_AFTER).replace(tzinfo=pytz.UTC)
    closed_orders_sorted = [o for o in closed_orders_sorted if o.filled_at >= reset_dt]

trade_data = []
slots = {}
realized_pnl = 0.0
equity_curve = [{"Time": start_time.strftime("%Y-%m-%d %H:%M:%S"), "Equity": SEED_CAPITAL}]

for o in closed_orders_sorted:
    symbol = o.symbol
    qty = float(o.filled_qty)
    price = float(o.filled_avg_price)
    side = o.side.name
    order_id = o.client_order_id or ""
    if order_id.startswith("PAT_"):
        slot_name = "patient"
    elif order_id.startswith("ACT_"):
        slot_name = "active"
    else:
        slot_name = "active"
    if symbol not in slots:
        slots[symbol] = {"patient": {"qty": 0, "entry": 0.0}, "active": {"qty": 0, "entry": 0.0}}
    slot = slots[symbol][slot_name]
    if side == "BUY":
        total_cost = (slot["qty"] * slot["entry"]) + (qty * price)
        slot["qty"] += qty
        if slot["qty"] > 0:
            slot["entry"] = total_cost / slot["qty"]
    elif side == "SELL":
        trade_pnl = (price - slot["entry"]) * qty
        realized_pnl += trade_pnl
        slot["qty"] -= qty
        if slot["qty"] <= 0:
            slot["qty"] = 0
            slot["entry"] = 0.0
        equity_curve.append({
            "Time": o.filled_at.astimezone(EST).strftime("%Y-%m-%d %H:%M:%S"),
            "Equity": SEED_CAPITAL + realized_pnl})
    slot_label = "Patient" if slot_name == "patient" else "Active"
    trade_data.append({
        "Time": o.filled_at.astimezone(EST).strftime("%Y-%m-%d %H:%M:%S"),
        "Symbol": symbol, "Slot": slot_label, "Side": side,
        "Qty": qty, "Avg Price": price, "Status": o.status.name})

ledger_df = pd.DataFrame(trade_data)
if not ledger_df.empty:
    ledger_df = ledger_df.sort_values("Time").reset_index(drop=True)
current_challenge_equity = SEED_CAPITAL + realized_pnl
equity_df = pd.DataFrame(equity_curve)

# ================================================================
# SECTION 6 - DETERMINE CURRENT POSITION (TWO-LAYER CHECK)
#
# Layer 1: Reconstructed from order history above.
# Layer 2: Hard-check Alpaca's live positions API. If Alpaca
#          sees shares that Layer 1 missed, Layer 2 wins.
#          This is what lets Chuck resume with shares held.
# ================================================================
current_ticker = None
patient_qty = 0
patient_entry = 0.0
active_qty = 0
active_entry = 0.0

for symbol in TICKERS:
    if symbol in slots:
        p = slots[symbol]["patient"]
        a = slots[symbol]["active"]
        if p["qty"] > 0 or a["qty"] > 0:
            current_ticker = symbol
            patient_qty = p["qty"]
            patient_entry = p["entry"]
            active_qty = a["qty"]
            active_entry = a["entry"]
            break

total_qty = patient_qty + active_qty

try:
    all_positions = trading_client.get_all_positions()
    for pos in all_positions:
        if pos.symbol in TICKERS and float(pos.qty) > 0:
            alpaca_symbol = pos.symbol
            alpaca_qty = float(pos.qty)
            alpaca_entry = float(pos.avg_entry_price)
            if current_ticker is None:
                current_ticker = alpaca_symbol
                active_qty = alpaca_qty
                active_entry = alpaca_entry
                total_qty = alpaca_qty
            elif current_ticker == alpaca_symbol:
                patient_qty = min(patient_qty, alpaca_qty)
                active_qty = max(0, alpaca_qty - patient_qty)
                if active_qty > 0 and active_entry == 0:
                    active_entry = alpaca_entry
                total_qty = alpaca_qty
            break
except Exception:
    pass

unrealized_pl = 0.0
if current_ticker and current_ticker in ticker_data:
    price_now = ticker_data[current_ticker]["current_price"]
    unrealized_pl = (price_now - patient_entry) * patient_qty + (price_now - active_entry) * active_qty

patient_cost_basis = patient_qty * patient_entry
active_cost_basis = active_qty * active_entry
deployed_cost = patient_cost_basis + active_cost_basis
remaining_budget = max(0.0, current_challenge_equity - deployed_cost)
# ================================================================
# SECTION 7 - SIGNAL LOGIC (SPLIT CAPITAL)
# ================================================================
signals = {}
buy_candidate = None
patient_sell = False
active_sell = False
patient_sell_reason = ""
active_sell_reason = ""

for symbol, config in TICKERS.items():
    if symbol not in ticker_data:
        signals[symbol] = {"signal": "ERROR", "reason": "Data fetch failed."}
        continue
    td = ticker_data[symbol]
    price = td["current_price"]
    rsi = td["rsi_val"]
    lower_bb = td["lower_bb"]
    upper_bb = td["upper_bb"]
    is_blackout = False
    if config["blackout_start"] and config["blackout_end"]:
        is_blackout = config["blackout_start"] <= end_time <= config["blackout_end"]
    signal = "HOLD"
    reason = "Awaiting technical triggers."

    if is_blackout:
        if current_ticker == symbol and total_qty > 0:
            signal = "SELL_LIQUIDATE"
            reason = "Earnings blackout. Liquidating all slots."
        else:
            signal = "STANDBY"
            reason = f"Earnings blackout active for {symbol}."
    else:
        buy_signal_fired = rsi < config["rsi_buy"] or price < lower_bb
        if buy_signal_fired:
            reasons_list = []
            if rsi < config["rsi_buy"]:
                reasons_list.append(f"RSI ({rsi:.2f}) < {config['rsi_buy']}")
            if price < lower_bb:
                reasons_list.append(f"Price (${price:.2f}) < Lower BB (${lower_bb:.2f})")
            buy_reason_text = " | ".join(reasons_list)
            if current_ticker is None:
                signal = "BUY"
                reason = f"BUY Signal (both slots): {buy_reason_text}"
            elif current_ticker == symbol and active_qty == 0:
                signal = "BUY_ACTIVE"
                reason = f"BUY Signal (active slot re-entry): {buy_reason_text}"

        if current_ticker == symbol:
            if patient_qty > 0:
                pat_pnl = price - patient_entry
                if pat_pnl >= config["profit_target"]:
                    patient_sell = True
                    patient_sell_reason = f"PATIENT profit target: +${pat_pnl:.2f}/share"
            if active_qty > 0:
                act_pnl = price - active_entry
                if act_pnl >= config["profit_target"]:
                    active_sell = True
                    active_sell_reason = f"ACTIVE profit target: +${act_pnl:.2f}/share"
                elif rsi > config["rsi_sell"] or price > upper_bb:
                    active_sell = True
                    sell_reasons = []
                    if rsi > config["rsi_sell"]:
                        sell_reasons.append(f"RSI ({rsi:.2f}) > {config['rsi_sell']}")
                    if price > upper_bb:
                        sell_reasons.append(f"Price (${price:.2f}) > Upper BB (${upper_bb:.2f})")
                    active_sell_reason = "ACTIVE SELL: " + " | ".join(sell_reasons)

            if patient_sell and active_sell:
                signal = "SELL_BOTH"
                reason = f"{patient_sell_reason} | {active_sell_reason}"
            elif patient_sell:
                signal = "SELL_PATIENT"
                reason = patient_sell_reason
            elif active_sell:
                signal = "SELL_ACTIVE"
                reason = active_sell_reason
            elif patient_qty > 0 or active_qty > 0:
                holding_parts = []
                if patient_qty > 0:
                    pat_tgt = patient_entry + config["profit_target"]
                    holding_parts.append(f"Patient: {int(patient_qty)} @ ${patient_entry:.2f} (tgt ${pat_tgt:.2f})")
                if active_qty > 0:
                    act_tgt = active_entry + config["profit_target"]
                    holding_parts.append(f"Active: {int(active_qty)} @ ${active_entry:.2f} (tgt ${act_tgt:.2f})")
                reason = "Holding - " + " | ".join(holding_parts)

    signals[symbol] = {"signal": signal, "reason": reason}
    if signal in ["BUY", "BUY_ACTIVE"] and buy_candidate is None:
        buy_candidate = (symbol, signal)

# ================================================================
# SECTION 8 - ORDER EXECUTION (TRIPLE-GUARDED + MARKET HOURS)
# ================================================================

BUDGET_SAFETY_FACTOR = 0.95

def market_is_open():
    now_et = datetime.datetime.now(EST)
    if now_et.weekday() >= 5:
        return False
    market_open_time = datetime.time(9, 30, 0)
    market_close_time = datetime.time(16, 0, 0)
    return market_open_time <= now_et.time() <= market_close_time

# GUARD 2: Check for pending orders
try:
    open_orders_req = GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=all_symbols)
    open_orders = trading_client.get_orders(filter=open_orders_req)
    pending_symbols = {o.symbol for o in open_orders}
except Exception:
    pending_symbols = set()

# GUARD 3: Block buys if fully deployed
if buy_candidate and remaining_budget <= 0:
    st.error(f"CAPITAL LIMIT: ${deployed_cost:.2f} deployed of ${current_challenge_equity:.2f}. No budget for new buys.")
    buy_candidate = None

# SAFETY: Get actual Alpaca position before any sell
# Prevents "not allowed to short" by never selling more than actually held
actual_held_qty = 0
if current_ticker:
    try:
        actual_pos = trading_client.get_open_position(current_ticker)
        actual_held_qty = float(actual_pos.qty)
    except Exception:
        actual_held_qty = 0

# Show status
if PAUSED:
    st.error("BOT PAUSED - Flip the toggle to resume.")
elif not market_is_open():
    st.info("Market closed. Bot is monitoring only - no orders will be submitted.")
elif not can_submit_order():
    st.warning(f"Order cooldown active. Next order in {int(cooldown_remaining())} seconds...")

# --- SELL EXECUTION ---
if market_is_open() and not PAUSED:
    for symbol, sig_data in signals.items():
        if sig_data["signal"] == "SELL_LIQUIDATE" and current_ticker == symbol and actual_held_qty > 0:
            if not can_submit_order() or symbol in pending_symbols:
                continue
            try:
                sell_order = MarketOrderRequest(symbol=symbol, qty=actual_held_qty, side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY, client_order_id=f"ACT_{uuid.uuid4().hex[:8]}")
                trading_client.submit_order(order_data=sell_order)
                mark_order_submitted()
                st.success(f"Liquidated ALL {int(actual_held_qty)} shares of {symbol} (blackout).")
            except Exception as e:
                st.error(f"Blackout liquidation failed: {e}")

    if patient_sell and current_ticker and patient_qty > 0:
        safe_qty = min(patient_qty, actual_held_qty)
        if safe_qty > 0 and can_submit_order() and current_ticker not in pending_symbols:
            try:
                sell_order = MarketOrderRequest(symbol=current_ticker, qty=safe_qty, side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY, client_order_id=f"PAT_{uuid.uuid4().hex[:8]}")
                trading_client.submit_order(order_data=sell_order)
                mark_order_submitted()
                st.success(f"PATIENT SELL: {int(safe_qty)} shares of {current_ticker}. {patient_sell_reason}")
            except Exception as e:
                st.error(f"Patient sell failed: {e}")

    if active_sell and current_ticker and active_qty > 0:
        safe_qty = min(active_qty, actual_held_qty)
        if safe_qty > 0 and can_submit_order() and current_ticker not in pending_symbols:
            try:
                sell_order = MarketOrderRequest(symbol=current_ticker, qty=safe_qty, side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY, client_order_id=f"ACT_{uuid.uuid4().hex[:8]}")
                trading_client.submit_order(order_data=sell_order)
                mark_order_submitted()
                st.success(f"ACTIVE SELL: {int(safe_qty)} shares of {current_ticker}. {active_sell_reason}")
            except Exception as e:
                st.error(f"Active sell failed: {e}")

    # --- BUY EXECUTION ---
    if buy_candidate:
        symbol, buy_type = buy_candidate
        price = ticker_data[symbol]["current_price"]

        if not can_submit_order():
            pass
        elif symbol in pending_symbols:
            st.warning(f"Order pending for {symbol}. Waiting for fill...")
        elif buy_type == "BUY":
            half_budget = (remaining_budget * BUDGET_SAFETY_FACTOR) / 2.0
            patient_buy_qty = int(half_budget // price) if price > 0 else 0
            active_buy_qty = int(half_budget // price) if price > 0 else 0
            total_buy_cost = (patient_buy_qty + active_buy_qty) * price
            if total_buy_cost > current_challenge_equity:
                st.error(f"BLOCKED: Cost ${total_buy_cost:.2f} exceeds equity ${current_challenge_equity:.2f}.")
            elif patient_buy_qty > 0 and active_buy_qty > 0:
                try:
                    pat_order = MarketOrderRequest(symbol=symbol, qty=patient_buy_qty, side=OrderSide.BUY,
                        time_in_force=TimeInForce.DAY, client_order_id=f"PAT_{uuid.uuid4().hex[:8]}")
                    act_order = MarketOrderRequest(symbol=symbol, qty=active_buy_qty, side=OrderSide.BUY,
                        time_in_force=TimeInForce.DAY, client_order_id=f"ACT_{uuid.uuid4().hex[:8]}")
                    trading_client.submit_order(order_data=pat_order)
                    trading_client.submit_order(order_data=act_order)
                    mark_order_submitted()
                    st.success(f"SPLIT BUY {symbol} ~${price:.2f}: Patient {patient_buy_qty} + Active {active_buy_qty} shares (${total_buy_cost:.2f} of ${current_challenge_equity:.2f})")
                except Exception as e:
                    mark_order_submitted()
                    st.error(f"Buy failed: {e}")
            else:
                st.warning(f"Not enough equity for {symbol} at ${price:.2f}.")

        elif buy_type == "BUY_ACTIVE":
            active_buy_qty = int((remaining_budget * BUDGET_SAFETY_FACTOR) // price) if price > 0 else 0
            new_total_cost = deployed_cost + (active_buy_qty * price)
            if new_total_cost > current_challenge_equity:
                st.error(f"BLOCKED: Total cost ${new_total_cost:.2f} exceeds equity ${current_challenge_equity:.2f}.")
            elif active_buy_qty > 0:
                try:
                    order = MarketOrderRequest(symbol=symbol, qty=active_buy_qty, side=OrderSide.BUY,
                        time_in_force=TimeInForce.DAY, client_order_id=f"ACT_{uuid.uuid4().hex[:8]}")
                    trading_client.submit_order(order_data=order)
                    mark_order_submitted()
                    st.success(f"ACTIVE RE-ENTRY: {active_buy_qty} shares of {symbol} ~${price:.2f} (${active_buy_qty * price:.2f} of ${remaining_budget:.2f} available)")
                except Exception as e:
                    mark_order_submitted()
                    st.error(f"Active re-entry failed: {e}")
            else:
                st.warning(f"Not enough remaining equity (${remaining_budget:.2f}) for {symbol} at ${price:.2f}.")
# ================================================================
# SECTION 9 - PORTFOLIO OVERVIEW
# ================================================================
st.subheader("Portfolio Overview")
colA, colB, colC, colD = st.columns(4)
colA.metric("Starting Capital", f"${SEED_CAPITAL:.2f}")
colB.metric("Challenge Equity", f"${current_challenge_equity:.2f}", f"${current_challenge_equity - SEED_CAPITAL:.2f} PnL")
if current_ticker:
    colC.metric("Holding", f"{current_ticker}", f"{int(total_qty)} Shares")
    colD.metric("Open PnL", f"${unrealized_pl:.2f}")
else:
    colC.metric("Holding", "None", "Scanning...")
    colD.metric("Open PnL", "$0.00")

st.progress(min(1.0, deployed_cost / current_challenge_equity if current_challenge_equity > 0 else 0))
st.caption(f"Capital deployed: ${deployed_cost:.2f} / ${current_challenge_equity:.2f} ({deployed_cost / current_challenge_equity * 100:.1f}% used) | Remaining: ${remaining_budget:.2f}")

if current_ticker and current_ticker in TICKERS:
    config = TICKERS[current_ticker]
    if patient_qty > 0:
        pat_target = patient_entry + config["profit_target"]
        st.info(f"Patient: {int(patient_qty)} shares @ ${patient_entry:.2f} | Target: ${pat_target:.2f} | Cost: ${patient_cost_basis:.2f} | Sells ONLY on profit target")
    else:
        st.info("Patient: Empty")
    if active_qty > 0:
        act_target = active_entry + config["profit_target"]
        st.info(f"Active: {int(active_qty)} shares @ ${active_entry:.2f} | Target: ${act_target:.2f} | Cost: ${active_cost_basis:.2f} | Sells on profit or overbought")
    else:
        st.info("Active: Empty (re-enters on next BUY signal)")

st.markdown("---")

# ================================================================
# SECTION 10 - SIGNAL SCANNER
# ================================================================
st.subheader("Signal Scanner")
signal_cols = st.columns(len(TICKERS))
for i, (symbol, sig_data) in enumerate(signals.items()):
    sig = sig_data["signal"]
    with signal_cols[i]:
        if symbol in ticker_data:
            price = ticker_data[symbol]["current_price"]
            rsi = ticker_data[symbol]["rsi_val"]
            if sig in ["BUY", "BUY_ACTIVE"]:
                st.success(f"**{symbol}**\n\n${price:.2f}\n\nRSI: {rsi:.1f}\n\nBUY")
            elif sig in ["SELL_PATIENT", "SELL_ACTIVE", "SELL_BOTH", "SELL_LIQUIDATE"]:
                st.error(f"**{symbol}**\n\n${price:.2f}\n\nRSI: {rsi:.1f}\n\nSELL")
            elif sig == "STANDBY":
                st.warning(f"**{symbol}**\n\n${price:.2f}\n\nRSI: {rsi:.1f}\n\nBLACKOUT")
            else:
                st.info(f"**{symbol}**\n\n${price:.2f}\n\nRSI: {rsi:.1f}\n\n{sig}")
        else:
            st.error(f"**{symbol}**\n\nDATA ERROR")
st.markdown("---")

# ================================================================
# SECTION 11 - TICKER CHARTS + EQUITY CURVE + TRADE LOG
# ================================================================
tab_names = list(TICKERS.keys()) + ["Equity Curve", "Trade Log"]
tabs = st.tabs(tab_names)

for i, symbol in enumerate(TICKERS.keys()):
    with tabs[i]:
        if symbol not in ticker_data:
            st.error(f"No data for {symbol}.")
            continue
        td = ticker_data[symbol]
        config = TICKERS[symbol]
        sig_data = signals[symbol]
        df = td["df"]
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Price", f"${td['current_price']:.2f}")
        col2.metric("RSI (6)", f"{td['rsi_val']:.2f}")
        col3.metric("Signal", sig_data["signal"])
        max_buyable = int(remaining_budget // td["current_price"]) if td["current_price"] > 0 else 0
        col4.metric("Max Buyable", f"{max_buyable}")
        st.write(f"**Status:** {sig_data['reason']}")
        st.write(f"**Tuning:** BB(20, {config['bb_std']}) | RSI Buy < {config['rsi_buy']} | RSI Sell > {config['rsi_sell']} | Profit: ${config['profit_target']:.2f}")
        fig = go.Figure()
        fig.add_trace(go.Candlestick(x=df.index, open=df['open'], high=df['high'],
            low=df['low'], close=df['close'], name='Price'))
        fig.add_trace(go.Scatter(x=df.index, y=df['VWAP'],
            line=dict(color='orange', width=2), name='VWAP'))
        fig.add_trace(go.Scatter(x=df.index, y=df[td["upper_bb_col"]],
            line=dict(color='gray', width=1, dash='dash'), name='Upper BB'))
        fig.add_trace(go.Scatter(x=df.index, y=df[td["lower_bb_col"]],
            line=dict(color='gray', width=1, dash='dash'), name='Lower BB',
            fill='tonexty', fillcolor='rgba(128,128,128,0.1)'))
        fig.update_layout(title=f"{symbol} - 1 Min", xaxis_title="Time", yaxis_title="Price ($)",
            template="plotly_dark", xaxis_rangeslider_visible=False, height=500)
        st.plotly_chart(fig, use_container_width=True)

with tabs[-2]:
    fig_eq = go.Figure()
    fig_eq.add_trace(go.Scatter(x=equity_df["Time"], y=equity_df["Equity"],
        mode='lines+markers', name='Equity', line=dict(color='#00FF00', width=3)))
    fig_eq.update_layout(title=f"Growth from ${SEED_CAPITAL:.0f} Seed",
        xaxis_title="Time", yaxis_title="Equity ($)", template="plotly_dark", height=500)
    st.plotly_chart(fig_eq, use_container_width=True)

with tabs[-1]:
    if not ledger_df.empty:
        st.dataframe(ledger_df, use_container_width=True)
    else:
        st.info("No closed trades yet.")            
