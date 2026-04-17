import streamlit as st
import pandas as pd
import pandas_ta as ta
import plotly.graph_objects as go
import datetime
import pytz
from streamlit_autorefresh import st_autorefresh
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

# --- 1. PAGE SETUP & AUTOREFRESH ---
st.set_page_config(page_title="NRDS Mean Reversion Bot", layout="wide")
st.title("NRDS $300 Challenge Dashboard 📈")

# Auto-refresh every 30 seconds
count = st_autorefresh(interval=30000, limit=None, key="data_refresh")

# --- 2. API KEYS & CLIENTS ---
API_KEY = st.secrets["ALPACA_API_KEY"]
SECRET_KEY = st.secrets["ALPACA_SECRET_KEY"]

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

# --- 3. FETCH DATA (Alpaca IEX 1-Min Data) ---
EST = pytz.timezone('America/New_York')
end_time = datetime.datetime.now(EST)
start_time = end_time - datetime.timedelta(days=3) 

request_params = StockBarsRequest(
    symbol_or_symbols="NRDS",
    timeframe=TimeFrame.Minute,
    start=start_time,
    end=end_time,
    feed="iex"
)

bars = data_client.get_stock_bars(request_params)
df = bars.df.reset_index()
df.set_index('timestamp', inplace=True)
df.index = df.index.tz_convert('America/New_York')

# --- 4. CALCULATE INDICATORS ---
bbands = ta.bbands(df['close'], length=20, std=2)
df = pd.concat([df, bbands], axis=1)
df['RSI_10'] = ta.rsi(df['close'], length=10)
df['VWAP'] = ta.vwap(df['high'], df['low'], df['close'], df['volume'])

# Safely extract dynamic Bollinger Band column names to avoid KeyErrors
lower_bb_col = [col for col in df.columns if col.startswith('BBL')][0]
upper_bb_col = [col for col in df.columns if col.startswith('BBU')][0]

latest = df.iloc[-1]
current_price = latest['close']
rsi_val = latest['RSI_10']
lower_bb = latest[lower_bb_col]
upper_bb = latest[upper_bb_col]

# --- 5. $300 CHALLENGE PERSISTENT LEDGER ---
# Fetch closed orders to reconstruct the challenge ledger
orders_req = GetOrdersRequest(
    status=QueryOrderStatus.CLOSED,
    symbols=["NRDS"],
    limit=500
)
closed_orders = trading_client.get_orders(filter=orders_req)

trade_data = []
for o in closed_orders:
    if o.filled_qty and float(o.filled_qty) > 0:
        trade_data.append({
            "Time": o.filled_at.astimezone(EST).strftime("%Y-%m-%d %H:%M:%S"),
            "Side": o.side.name,
            "Qty": float(o.filled_qty),
            "Avg Price": float(o.filled_avg_price),
            "Status": o.status.name
        })
        
ledger_df = pd.DataFrame(trade_data)
if not ledger_df.empty:
    ledger_df = ledger_df.sort_values("Time").reset_index(drop=True)

# Calculate Equity Curve compounding from $300
current_challenge_equity = 300.00
equity_curve = [{"Time": start_time.strftime("%Y-%m-%d %H:%M:%S"), "Equity": 300.00}]

if not ledger_df.empty:
    holdings = 0
    avg_cost = 0
    realized_pnl = 0
    
    for idx, row in ledger_df.iterrows():
        qty = row["Qty"]
        price = row["Avg Price"]
        if row["Side"] == "BUY":
            total_cost = (holdings * avg_cost) + (qty * price)
            holdings += qty
            avg_cost = total_cost / holdings
        elif row["Side"] == "SELL":
            trade_pnl = (price - avg_cost) * qty
            realized_pnl += trade_pnl
            holdings -= qty
            if holdings == 0:
                avg_cost = 0
        
        equity_curve.append({
            "Time": row["Time"],
            "Equity": 300.00 + realized_pnl
        })
        
    current_challenge_equity = 300.00 + realized_pnl

equity_df = pd.DataFrame(equity_curve)

# Current Position Check
try:
    position = trading_client.get_open_position('NRDS')
    current_qty = float(position.qty)
    unrealized_pl = float(position.unrealized_pl)
except Exception:
    current_qty = 0
    unrealized_pl = 0.0

# Max-Buy Compounding Logic
qty_to_buy = int(current_challenge_equity // current_price) if current_price > 0 else 0

# --- 6. EARNINGS BLACKOUT & SIGNAL LOGIC ---
BLACKOUT_START = EST.localize(datetime.datetime(2026, 5, 4, 0, 0, 0))
BLACKOUT_END = EST.localize(datetime.datetime(2026, 5, 13, 23, 59, 59))
is_blackout_active = BLACKOUT_START <= end_time <= BLACKOUT_END

signal = "HOLD"
reason = "Awaiting technical triggers."

if is_blackout_active:
    st.error("⚠️ **EARNINGS BLACKOUT ACTIVE (May 4 - May 13)**")
    if current_qty > 0:
        signal = "SELL_LIQUIDATE"
        reason = "🚨 Blackout triggered. Liquidating open position to protect capital."
    else:
        signal = "STANDBY"
        reason = "Bot paused for earnings. Manual trading enabled."
else:
    if rsi_val < 30 and current_price < lower_bb:
        signal = "BUY"
        reason = f"RSI ({rsi_val:.2f}) < 30 AND Price (${current_price:.2f}) < Lower BB."
    elif current_qty > 0 and (rsi_val > 70 and current_price > upper_bb):
        signal = "SELL"
        reason = f"RSI ({rsi_val:.2f}) > 70 AND Price (${current_price:.2f}) > Upper BB."

# --- 7. ORDER EXECUTION ---
if signal == "BUY" and qty_to_buy > 0:
    try:
        buy_order = MarketOrderRequest(
            symbol="NRDS",
            qty=qty_to_buy,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY
        )
        trading_client.submit_order(order_data=buy_order)
        st.success(f"Executed BUY for {qty_to_buy} shares at ~${current_price:.2f}")
    except Exception as e:
        st.error(f"Buy failed: {e}")

elif signal in ["SELL", "SELL_LIQUIDATE"] and current_qty > 0:
    try:
        sell_order = MarketOrderRequest(
            symbol="NRDS",
            qty=current_qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY
        )
        trading_client.submit_order(order_data=sell_order)
        st.success(f"Executed SELL for {current_qty} shares. Reason: {reason}")
    except Exception as e:
        st.error(f"Sell failed: {e}")

# --- 8. DASHBOARD UI ---
st.subheader("🏆 $300 Challenge Metrics")
colA, colB, colC = st.columns(3)
colA.metric("Starting Capital", "$300.00")
colB.metric("Challenge Equity", f"${current_challenge_equity:.2f}", f"${current_challenge_equity - 300.00:.2f} PnL")
colC.metric("Open Position PnL", f"${unrealized_pl:.2f}", f"{current_qty} Shares")

st.markdown("---")
st.subheader("Live Market Signals")
col1, col2, col3, col4 = st.columns(4)
col1.metric("Current Price", f"${current_price:.2f}")
col2.metric("Target Buy Qty (Max Buy)", f"{qty_to_buy} Shares")
col3.metric("RSI (10)", f"{rsi_val:.2f}")
col4.metric("Current Signal", signal)
st.write(f"**Bot Status:** {reason}")

# --- 9. TABS & PLOTLY CHARTS ---
tab1, tab2, tab3 = st.tabs(["Live Chart (1 Min)", "Challenge Equity Curve", "Trade Log"])

with tab1:
    fig = go.Figure()
    fig.add_trace(go.Candlestick(x=df.index,
                    open=df['open'], high=df['high'],
                    low=df['low'], close=df['close'],
                    name='Price'))
    fig.add_trace(go.Scatter(x=df.index, y=df['VWAP'], line=dict(color='orange', width=2), name='VWAP'))
    fig.add_trace(go.Scatter(x=df.index, y=df[upper_bb_col], line=dict(color='gray', width=1, dash='dash'), name='Upper BB'))
    fig.add_trace(go.Scatter(x=df.index, y=df[lower_bb_col], line=dict(color='gray', width=1, dash='dash'), name='Lower BB', fill='tonexty', fillcolor='rgba(128,128,128,0.1)'))
    fig.update_layout(title="NRDS Live Chart - 1 Min", xaxis_title="Time", yaxis_title="Price ($)", template="plotly_dark", xaxis_rangeslider_visible=False, height=600)
    st.plotly_chart(fig, use_container_width=True)

with tab2:
    fig_eq = go.Figure()
    fig_eq.add_trace(go.Scatter(x=equity_df["Time"], y=equity_df["Equity"], mode='lines+markers', name='Equity', line=dict(color='#00FF00', width=3)))
    fig_eq.update_layout(title="Compounding Growth from $300 Seed", xaxis_title="Time", yaxis_title="Account Equity ($)", template="plotly_dark", height=500)
    st.plotly_chart(fig_eq, use_container_width=True)

with tab3:
    if not ledger_df.empty:
        st.dataframe(ledger_df, use_container_width=True)
    else:
        st.info("No closed trades yet in the ledger.")
