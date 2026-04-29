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
# 1. PAGE SETUP & AUTOREFRESH
# ================================================================
st.set_page_config(page_title="NRDS Trading Bot", layout="wide")
st.title("NRDS Trading Bot 📈")

count = st_autorefresh(interval=30000, limit=None, key="data_refresh")

# ================================================================
# 2. API KEYS, MODE & CLIENTS
# ================================================================
API_KEY = st.secrets["ALPACA_API_KEY"]
SECRET_KEY = st.secrets["ALPACA_SECRET_KEY"]
PAPER_MODE = st.secrets.get("PAPER_MODE", "true").lower() == "true"
SEED_CAPITAL = float(st.secrets.get("SEED_CAPITAL", "300"))

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=PAPER_MODE)
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

if PAPER_MODE:
    st.success("📝 **PAPER TRADING MODE** - Simulated trades, no real money at risk.")
else:
    st.error("🔴 **LIVE TRADING MODE** - Real money. Real consequences.")

# ================================================================
# 3. TICKER CONFIGURATION
#
# Same tuning as before. The split-capital logic is handled
# globally - every ticker uses the Patient/Active slot system.
#
# PATIENT SLOT: Only sells on profit target. Holds for days.
# ACTIVE  SLOT: Sells on profit target OR technical overbought.
# ================================================================
EST = pytz.timezone('America/New_York')

TICKERS = {
    "NRDS": {
        "bb_std": 1.5,
        "rsi_buy": 35,
        "rsi_sell": 65,
        "profit_target": 0.08,
        "blackout_start": EST.localize(datetime.datetime(2026, 5, 4, 0, 0, 0)),
        "blackout_end": EST.localize(datetime.datetime(2026, 5, 13, 23, 59, 59)),
    },
    "OPFI": {
        "bb_std": 1.5,
        "rsi_buy": 35,
        "rsi_sell": 65,
        "profit_target": 0.08,
        "blackout_start": EST.localize(datetime.datetime(2026, 5, 4, 0, 0, 0)),
        "blackout_end": EST.localize(datetime.datetime(2026, 5, 13, 23, 59, 59)),
    },
    "PTON": {
        "bb_std": 1.2,
        "rsi_buy": 30,
        "rsi_sell": 60,
        "profit_target": 0.06,
        "blackout_start": None,
        "blackout_end": None,
    },
    "OPEN": {
        "bb_std": 1.8,
        "rsi_buy": 30,
        "rsi_sell": 60,
        "profit_target": 0.10,
        "blackout_start": None,
        "blackout_end": None,
    },
    "PENN": {
        "bb_std": 1.5,
        "rsi_buy": 35,
        "rsi_sell": 65,
        "profit_target": 0.12,
        "blackout_start": None,
        "blackout_end": None,
    },
    "PUBM": {
        "bb_std": 1.5,
        "rsi_buy": 35,
        "rsi_sell": 65,
        "profit_target": 0.08,
        "blackout_start": None,
        "blackout_end": None,
    },
}

# ================================================================
# 4. FETCH 1-MINUTE DATA FOR ALL TICKERS
# ================================================================
end_time = datetime.datetime.now(EST)
start_time = end_time - datetime.timedelta(days=3)

ticker_data = {}

for symbol, config in TICKERS.items():
    try:
        request_params = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Minute,
            start=start_time,
            end=end_time,
            feed="iex"
        )
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
            "df": df,
            "current_price": latest['close'],
            "rsi_val": latest['RSI_6'],
            "lower_bb": latest[lower_bb_col],
            "upper_bb": latest[upper_bb_col],
            "lower_bb_col": lower_bb_col,
            "upper_bb_col": upper_bb_col,
        }
    except Exception as e:
        st.warning(f"⚠️ Could not fetch data for {symbol}: {e}")

# ================================================================
# 5. RECONSTRUCT SLOT POSITIONS FROM ORDER HISTORY
#
# Alpaca sees one position per symbol, but WE split it into two
# virtual slots: Patient (PAT_) and Active (ACT_).
#
# Every buy/sell order we submit gets a client_order_id starting
# with "PAT_" or "ACT_". On each refresh we walk the full order
# history and reconstruct how many shares each slot holds and at
# what average entry price.
#
# Orders placed BEFORE this update (no PAT_/ACT_ prefix) are
# treated as legacy Active-slot orders so existing positions
# transition cleanly.
# ================================================================
all_symbols = list(TICKERS.keys())
orders_req = GetOrdersRequest(
    status=QueryOrderStatus.CLOSED,
    symbols=all_symbols,
    limit=500
)
closed_orders = trading_client.get_orders(filter=orders_req)

# Sort oldest-first so we can walk the ledger chronologically
closed_orders_sorted = sorted(
    [o for o in closed_orders if o.filled_qty and float(o.filled_qty) > 0],
    key=lambda o: o.filled_at
)

# Build trade ledger AND reconstruct slot state in one pass
trade_data = []
slots = {}  # { symbol: { "patient": {"qty": 0, "entry": 0}, "active": {"qty": 0, "entry": 0} } }
realized_pnl = 0.0
equity_curve = [{"Time": start_time.strftime("%Y-%m-%d %H:%M:%S"), "Equity": SEED_CAPITAL}]

for o in closed_orders_sorted:
    symbol = o.symbol
    qty = float(o.filled_qty)
    price = float(o.filled_avg_price)
    side = o.side.name  # "BUY" or "SELL"
    order_id = o.client_order_id or ""

    # Determine which slot this order belongs to
    if order_id.startswith("PAT_"):
        slot_name = "patient"
    elif order_id.startswith("ACT_"):
        slot_name = "active"
    else:
        # Legacy order (before split-capital update) -> treat as active
        slot_name = "active"

    # Initialize slot tracking for this symbol if needed
    if symbol not in slots:
        slots[symbol] = {
            "patient": {"qty": 0, "entry": 0.0},
            "active": {"qty": 0, "entry": 0.0},
        }

    slot = slots[symbol][slot_name]

    if side == "BUY":
        # Weighted average entry price
        total_cost = (slot["qty"] * slot["entry"]) + (qty * price)
        slot["qty"] += qty
        if slot["qty"] > 0:
            slot["entry"] = total_cost / slot["qty"]
    elif side == "SELL":
        # Calculate realized P&L for this slot's sell
        trade_pnl = (price - slot["entry"]) * qty
        realized_pnl += trade_pnl
        slot["qty"] -= qty
        if slot["qty"] <= 0:
            slot["qty"] = 0
            slot["entry"] = 0.0
        equity_curve.append({
            "Time": o.filled_at.astimezone(EST).strftime("%Y-%m-%d %H:%M:%S"),
            "Equity": SEED_CAPITAL + realized_pnl
        })

    # Build ledger row with slot label
    slot_label = "Patient" if slot_name == "patient" else "Active"
    trade_data.append({
        "Time": o.filled_at.astimezone(EST).strftime("%Y-%m-%d %H:%M:%S"),
        "Symbol": symbol,
        "Slot": slot_label,
        "Side": side,
        "Qty": qty,
        "Avg Price": price,
        "Status": o.status.name
    })

ledger_df = pd.DataFrame(trade_data)
if not ledger_df.empty:
    ledger_df = ledger_df.sort_values("Time").reset_index(drop=True)

current_challenge_equity = SEED_CAPITAL + realized_pnl
equity_df = pd.DataFrame(equity_curve)

# ================================================================
# 6. DETERMINE CURRENT SLOT STATE
#
# Which ticker are we in? What does each slot hold?
# ================================================================
current_ticker = None  # The ONE ticker both slots are bound to
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
            break  # Only one ticker allowed at a time

total_qty = patient_qty + active_qty

# Also check Alpaca for any open position not yet in closed orders
# (e.g., a buy that just filled this cycle)
if current_ticker is None:
    for symbol in TICKERS:
        try:
            position = trading_client.get_open_position(symbol)
            pos_qty = float(position.qty)
            if pos_qty > 0:
                current_ticker = symbol
                # Can't determine slot split for in-flight orders;
                # treat entire position as active (will reconcile next cycle)
                active_qty = pos_qty
                active_entry = float(position.avg_entry_price)
                total_qty = pos_qty
                break
        except Exception:
            continue

unrealized_pl = 0.0
if current_ticker and current_ticker in ticker_data:
    price_now = ticker_data[current_ticker]["current_price"]
    unrealized_pl = (price_now - patient_entry) * patient_qty + (price_now - active_entry) * active_qty

# ================================================================
# 7. SIGNAL LOGIC - SPLIT CAPITAL
#
# BUYING:
#   - If BOTH slots are empty -> scan all tickers for BUY signal
#   - If Patient is holding but Active is empty -> Active can
#     re-enter the SAME ticker on a new BUY signal
#   - Buy signal = RSI < threshold OR Price < Lower BB (same as before)
#   - Each slot gets 50% of challenge equity
#
# SELLING:
#   - PATIENT: Only sells when price >= patient_entry + profit_target
#     (no technical exits - infinite patience)
#   - ACTIVE: Sells on profit_target OR RSI > threshold OR Price > Upper BB
#     (same fast-scalp logic as the old bot)
#   - Earnings blackout liquidates BOTH slots
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

    # Check earnings blackout
    is_blackout = False
    if config["blackout_start"] and config["blackout_end"]:
        is_blackout = config["blackout_start"] <= end_time <= config["blackout_end"]

    signal = "HOLD"
    reason = "Awaiting technical triggers."

    if is_blackout:
        if current_ticker == symbol and total_qty > 0:
            signal = "SELL_LIQUIDATE"
            reason = f"🚨 Earnings blackout. Liquidating all {symbol} slots."
        else:
            signal = "STANDBY"
            reason = f"Earnings blackout active for {symbol}."
    else:
        # --- BUY LOGIC ---
        buy_signal_fired = rsi < config["rsi_buy"] or price < lower_bb

        if buy_signal_fired:
            reasons_list = []
            if rsi < config["rsi_buy"]:
                reasons_list.append(f"RSI ({rsi:.2f}) < {config['rsi_buy']}")
            if price < lower_bb:
                reasons_list.append(f"Price (${price:.2f}) < Lower BB (${lower_bb:.2f})")
            buy_reason_text = " | ".join(reasons_list)

            # Case 1: Both slots empty, no position anywhere -> full entry
            if current_ticker is None:
                signal = "BUY"
                reason = f"BUY Signal (both slots): {buy_reason_text}"

            # Case 2: We're in THIS ticker, active slot is empty -> active re-entry
            elif current_ticker == symbol and active_qty == 0:
                signal = "BUY_ACTIVE"
                reason = f"BUY Signal (active slot re-entry): {buy_reason_text}"

        # --- SELL LOGIC (only for the ticker we're holding) ---
        if current_ticker == symbol:
            # PATIENT SLOT: profit target ONLY
            if patient_qty > 0:
                pat_pnl = price - patient_entry
                if pat_pnl >= config["profit_target"]:
                    patient_sell = True
                    patient_sell_reason = f"💰 PATIENT profit target: +${pat_pnl:.2f}/share (target: +${config['profit_target']:.2f})"

            # ACTIVE SLOT: profit target OR technical overbought
            if active_qty > 0:
                act_pnl = price - active_entry
                if act_pnl >= config["profit_target"]:
                    active_sell = True
                    active_sell_reason = f"💰 ACTIVE profit target: +${act_pnl:.2f}/share (target: +${config['profit_target']:.2f})"
                elif rsi > config["rsi_sell"] or price > upper_bb:
                    active_sell = True
                    sell_reasons = []
                    if rsi > config["rsi_sell"]:
                        sell_reasons.append(f"RSI ({rsi:.2f}) > {config['rsi_sell']}")
                    if price > upper_bb:
                        sell_reasons.append(f"Price (${price:.2f}) > Upper BB (${upper_bb:.2f})")
                    active_sell_reason = "ACTIVE SELL: " + " | ".join(sell_reasons)

            # Update display signal for this ticker
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
                # We're holding but no sell signal -> show holding status
                holding_parts = []
                if patient_qty > 0:
                    pat_tgt = patient_entry + config["profit_target"]
                    holding_parts.append(f"Patient: {int(patient_qty)} shares @ ${patient_entry:.2f} (target ${pat_tgt:.2f})")
                if active_qty > 0:
                    act_tgt = active_entry + config["profit_target"]
                    holding_parts.append(f"Active: {int(active_qty)} shares @ ${active_entry:.2f} (target ${act_tgt:.2f})")
                reason = "Holding - " + " | ".join(holding_parts)

    signals[symbol] = {"signal": signal, "reason": reason}

    # Track first BUY candidate (priority = order in TICKERS dict)
    if signal in ["BUY", "BUY_ACTIVE"] and buy_candidate is None:
        buy_candidate = (symbol, signal)

# ================================================================
# 8. ORDER EXECUTION - SPLIT CAPITAL
#
# GUARD: Before submitting ANY order, check Alpaca for open/pending
# orders on that symbol. If one exists, skip to avoid duplicates.
# ================================================================

# Check for any open/pending orders to prevent duplicate submissions
try:
    open_orders_req = GetOrdersRequest(
        status=QueryOrderStatus.OPEN,
        symbols=all_symbols
    )
    open_orders = trading_client.get_orders(filter=open_orders_req)
    pending_symbols = {o.symbol for o in open_orders}
except Exception:
    pending_symbols = set()

# --- SELL EXECUTION ---
# Earnings blackout: liquidate everything
for symbol, sig_data in signals.items():
    if sig_data["signal"] == "SELL_LIQUIDATE" and current_ticker == symbol and total_qty > 0:
        if symbol in pending_symbols:
            st.warning(f"⏳ Sell already pending for {symbol}. Waiting for fill...")
            continue
        try:
            sell_order = MarketOrderRequest(
                symbol=symbol,
                qty=total_qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
                client_order_id=f"ACT_{uuid.uuid4().hex[:8]}"
            )
            trading_client.submit_order(order_data=sell_order)
            st.success(f"🚨 Liquidated ALL {int(total_qty)} shares of **{symbol}** (blackout).")
        except Exception as e:
            st.error(f"Blackout liquidation failed for {symbol}: {e}")

# Patient slot sell
if patient_sell and current_ticker and patient_qty > 0:
    if current_ticker in pending_symbols:
        st.warning(f"⏳ Order already pending for {current_ticker}. Waiting for fill...")
    else:
        try:
            sell_order = MarketOrderRequest(
                symbol=current_ticker,
                qty=patient_qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
                client_order_id=f"PAT_{uuid.uuid4().hex[:8]}"
            )
            trading_client.submit_order(order_data=sell_order)
            st.success(f"✅ PATIENT SELL: {int(patient_qty)} shares of **{current_ticker}**. {patient_sell_reason}")
        except Exception as e:
            st.error(f"Patient sell failed: {e}")

# Active slot sell
if active_sell and current_ticker and active_qty > 0:
    if current_ticker in pending_symbols:
        st.warning(f"⏳ Order already pending for {current_ticker}. Waiting for fill...")
    else:
        try:
            sell_order = MarketOrderRequest(
                symbol=current_ticker,
                qty=active_qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
                client_order_id=f"ACT_{uuid.uuid4().hex[:8]}"
            )
            trading_client.submit_order(order_data=sell_order)
            st.success(f"✅ ACTIVE SELL: {int(active_qty)} shares of **{current_ticker}**. {active_sell_reason}")
        except Exception as e:
            st.error(f"Active sell failed: {e}")

# --- BUY EXECUTION ---
if buy_candidate:
    symbol, buy_type = buy_candidate
    price = ticker_data[symbol]["current_price"]

    if symbol in pending_symbols:
        st.warning(f"⏳ Order already pending for {symbol}. Waiting for fill...")
    elif buy_type == "BUY":
        # Both slots empty -> split capital 50/50
        half_capital = current_challenge_equity / 2.0
        patient_buy_qty = int(half_capital // price) if price > 0 else 0
        active_buy_qty = int(half_capital // price) if price > 0 else 0

        if patient_buy_qty > 0:
            try:
                order = MarketOrderRequest(
                    symbol=symbol,
                    qty=patient_buy_qty,
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                    client_order_id=f"PAT_{uuid.uuid4().hex[:8]}"
                )
                trading_client.submit_order(order_data=order)
                st.success(f"✅ PATIENT BUY: {patient_buy_qty} shares of **{symbol}** at ~${price:.2f}")
            except Exception as e:
                st.error(f"Patient buy failed: {e}")

        if active_buy_qty > 0:
            try:
                order = MarketOrderRequest(
                    symbol=symbol,
                    qty=active_buy_qty,
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                    client_order_id=f"ACT_{uuid.uuid4().hex[:8]}"
                )
                trading_client.submit_order(order_data=order)
                st.success(f"✅ ACTIVE BUY: {active_buy_qty} shares of **{symbol}** at ~${price:.2f}")
            except Exception as e:
                st.error(f"Active buy failed: {e}")

    elif buy_type == "BUY_ACTIVE":
        # Patient is holding, active re-entering same ticker
        half_capital = current_challenge_equity / 2.0
        active_buy_qty = int(half_capital // price) if price > 0 else 0

        if active_buy_qty > 0:
            try:
                order = MarketOrderRequest(
                    symbol=symbol,
                    qty=active_buy_qty,
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                    client_order_id=f"ACT_{uuid.uuid4().hex[:8]}"
                )
                trading_client.submit_order(order_data=order)
                st.success(f"✅ ACTIVE RE-ENTRY: {active_buy_qty} shares of **{symbol}** at ~${price:.2f}")
            except Exception as e:
                st.error(f"Active re-entry buy failed: {e}")

# ================================================================
# 9. DASHBOARD UI - PORTFOLIO OVERVIEW
# ================================================================
st.subheader("🏆 Portfolio Overview")
colA, colB, colC, colD = st.columns(4)
colA.metric("Starting Capital", f"${SEED_CAPITAL:.2f}")
colB.metric("Challenge Equity", f"${current_challenge_equity:.2f}", f"${current_challenge_equity - SEED_CAPITAL:.2f} PnL")

if current_ticker:
    colC.metric("Holding", f"{current_ticker}", f"{int(total_qty)} Shares")
    colD.metric("Open PnL", f"${unrealized_pl:.2f}")
else:
    colC.metric("Holding", "None", "Scanning all tickers...")
    colD.metric("Open PnL", "$0.00")

# Show slot details when holding a position
if current_ticker and current_ticker in TICKERS:
    config = TICKERS[current_ticker]
    slot_info_parts = []

    if patient_qty > 0:
        pat_target = patient_entry + config["profit_target"]
        slot_info_parts.append(
            f"🐢 **Patient**: {int(patient_qty)} shares @ ${patient_entry:.2f} | "
            f"🎯 Target: ${pat_target:.2f} (+${config['profit_target']:.2f}/share) | "
            f"Sells ONLY on profit target"
        )
    else:
        slot_info_parts.append("🐢 **Patient**: Empty")

    if active_qty > 0:
        act_target = active_entry + config["profit_target"]
        slot_info_parts.append(
            f"⚡ **Active**: {int(active_qty)} shares @ ${active_entry:.2f} | "
            f"🎯 Target: ${act_target:.2f} (+${config['profit_target']:.2f}/share) | "
            f"Sells on profit target or overbought"
        )
    else:
        slot_info_parts.append("⚡ **Active**: Empty (will re-enter on next BUY signal)")

    for part in slot_info_parts:
        st.info(part)

st.markdown("---")

# ================================================================
# 10. SIGNAL SCANNER - All tickers at a glance
# ================================================================
st.subheader("📡 Signal Scanner")
signal_cols = st.columns(len(TICKERS))

for i, (symbol, sig_data) in enumerate(signals.items()):
    sig = sig_data["signal"]
    with signal_cols[i]:
        if symbol in ticker_data:
            price = ticker_data[symbol]["current_price"]
            rsi = ticker_data[symbol]["rsi_val"]
            if sig in ["BUY", "BUY_ACTIVE"]:
                st.success(f"**{symbol}**\n\n${price:.2f}\n\nRSI: {rsi:.1f}\n\n🟢 **{sig}**")
            elif sig in ["SELL_PATIENT", "SELL_ACTIVE", "SELL_BOTH", "SELL_LIQUIDATE"]:
                st.error(f"**{symbol}**\n\n${price:.2f}\n\nRSI: {rsi:.1f}\n\n🔴 **{sig}**")
            elif sig == "STANDBY":
                st.warning(f"**{symbol}**\n\n${price:.2f}\n\nRSI: {rsi:.1f}\n\n⚠️ **BLACKOUT**")
            else:
                st.info(f"**{symbol}**\n\n${price:.2f}\n\nRSI: {rsi:.1f}\n\n⏳ **{sig}**")
        else:
            st.error(f"**{symbol}**\n\n❌ DATA ERROR")

st.markdown("---")

# ================================================================
# 11. PER-TICKER TABS + EQUITY CURVE + TRADE LOG
# ================================================================
tab_names = list(TICKERS.keys()) + ["📈 Equity Curve", "📋 Trade Log"]
tabs = st.tabs(tab_names)

for i, symbol in enumerate(TICKERS.keys()):
    with tabs[i]:
        if symbol not in ticker_data:
            st.error(f"No data available for {symbol}.")
            continue

        td = ticker_data[symbol]
        config = TICKERS[symbol]
        sig_data = signals[symbol]
        df = td["df"]

        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Price", f"${td['current_price']:.2f}")
        col2.metric("RSI (6)", f"{td['rsi_val']:.2f}")
        col3.metric("Signal", sig_data["signal"])
        half_equity = current_challenge_equity / 2.0
        qty_possible = int(half_equity // td["current_price"]) if td["current_price"] > 0 else 0
        col4.metric("Half-Buy Qty", f"{qty_possible}")

        st.write(f"**Status:** {sig_data['reason']}")
        st.write(f"**Tuning:** BB(20, {config['bb_std']}) | RSI Buy < {config['rsi_buy']} | RSI Sell > {config['rsi_sell']} | Profit Target: ${config['profit_target']:.2f}")

        fig = go.Figure()
        fig.add_trace(go.Candlestick(
            x=df.index, open=df['open'], high=df['high'],
            low=df['low'], close=df['close'], name='Price'))
        fig.add_trace(go.Scatter(
            x=df.index, y=df['VWAP'],
            line=dict(color='orange', width=2), name='VWAP'))
        fig.add_trace(go.Scatter(
            x=df.index, y=df[td["upper_bb_col"]],
            line=dict(color='gray', width=1, dash='dash'), name='Upper BB'))
        fig.add_trace(go.Scatter(
            x=df.index, y=df[td["lower_bb_col"]],
            line=dict(color='gray', width=1, dash='dash'), name='Lower BB',
            fill='tonexty', fillcolor='rgba(128,128,128,0.1)'))
        fig.update_layout(
            title=f"{symbol} Live Chart - 1 Min",
            xaxis_title="Time", yaxis_title="Price ($)",
            template="plotly_dark",
            xaxis_rangeslider_visible=False, height=500)
        st.plotly_chart(fig, use_container_width=True)

with tabs[-2]:
    fig_eq = go.Figure()
    fig_eq.add_trace(go.Scatter(
        x=equity_df["Time"], y=equity_df["Equity"],
        mode='lines+markers', name='Equity',
        line=dict(color='#00FF00', width=3)))
    fig_eq.update_layout(
        title=f"Compounding Growth from ${SEED_CAPITAL:.0f} Seed (All Tickers)",
        xaxis_title="Time", yaxis_title="Account Equity ($)",
        template="plotly_dark", height=500)
    st.plotly_chart(fig_eq, use_container_width=True)

with tabs[-1]:
    if not ledger_df.empty:
        st.dataframe(ledger_df, use_container_width=True)
    else:
        st.info("No closed trades yet in the ledger.")
