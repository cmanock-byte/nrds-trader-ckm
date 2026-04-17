import streamlit as st
import pandas as pd
import pandas_ta as ta
import plotly.graph_objects as go
from datetime import datetime, timedelta, date
import pytz
import smtplib
from email.message import EmailMessage
import math

# Alpaca Imports
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus

# Auto-refresh
from streamlit_autorefresh import st_autorefresh

# ============================================================
# 1. CONFIG & SECRETS
# ============================================================
st.set_page_config(page_title="NRDS $300 Challenge", layout="wide", page_icon="📈")
st_autorefresh(interval=30000, key="data_refresh")

API_KEY = st.secrets["ALPACA_API_KEY"]
SECRET_KEY = st.secrets["ALPACA_SECRET_KEY"]
SENDER_EMAIL = st.secrets.get("SENDER_EMAIL", "")
SENDER_PASSWORD = st.secrets.get("SENDER_PASSWORD", "")
RECEIVER_SMS = st.secrets.get("RECEIVER_SMS", "")

SYMBOL = "NRDS"
SEED_CAPITAL = 300.00  # Your $300 challenge starting point

# ============================================================
# 2. CLIENT INIT
# ============================================================
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)

# ============================================================
# 3. EMAIL-TO-SMS ALERTING (Free)
# ============================================================
def send_sms_alert(subject, body):
    if not all([SENDER_EMAIL, SENDER_PASSWORD, RECEIVER_SMS]):
        return
    try:
        msg = EmailMessage()
        msg.set_content(body)
        msg['Subject'] = subject
        msg['From'] = SENDER_EMAIL
        msg['To'] = RECEIVER_SMS
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.send_message(msg)
        server.quit()
    except Exception as e:
        st.sidebar.error(f"SMS Alert Failed: {e}")

# ============================================================
# 4. DATA PIPELINE (IEX Feed)
# ============================================================
@st.cache_data(ttl=15)
def get_data():
    ny_tz = pytz.timezone('US/Eastern')
    end_dt = datetime.now(ny_tz)
    start_dt = end_dt - timedelta(days=3)
    req = StockBarsRequest(
        symbol_or_symbols=SYMBOL,
        timeframe=TimeFrame.Minute,
        start=start_dt,
        end=end_dt,
        feed="iex"
    )
    bars = data_client.get_stock_bars(req)
    if not bars.data or SYMBOL not in bars.data:
        return pd.DataFrame()
    df = bars.df.loc[SYMBOL].reset_index()
    df.set_index('timestamp', inplace=True)
    df.index = df.index.tz_convert(ny_tz)

    # Drop Alpaca's built-in vwap to avoid duplicates
    if 'vwap' in df.columns:
        df = df.drop(columns=['vwap'])

    # Calculate indicators
    df.ta.bbands(length=20, std=2, append=True)
    df.ta.rsi(length=10, append=True)
    df.ta.vwap(append=True)
    return df

# ============================================================
# 5. TRADE HISTORY FROM ALPACA (Persistent across restarts!)
# ============================================================
@st.cache_data(ttl=60)
def get_trade_history():
    """Pull all filled NRDS orders from Alpaca. This data persists
    on Alpaca's servers forever - no local storage needed."""
    try:
        request = GetOrdersRequest(
            status=QueryOrderStatus.CLOSED,
            symbols=[SYMBOL],
            limit=500
        )
        orders = trading_client.get_orders(filter=request)
        if not orders:
            return pd.DataFrame()

        rows = []
        for o in orders:
            if o.filled_qty and float(o.filled_qty) > 0:
                rows.append({
                    'time': o.filled_at,
                    'side': o.side.value,
                    'qty': float(o.filled_qty),
                    'avg_price': float(o.filled_avg_price),
                    'total': float(o.filled_qty) * float(o.filled_avg_price)
                })
        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)
        df = df.sort_values('time').reset_index(drop=True)
        return df
    except Exception:
        return pd.DataFrame()

def calculate_challenge_equity(trade_df):
    """Walk through trade history to reconstruct the $300 challenge
    balance over time, showing compounding growth."""
    if trade_df.empty:
        return pd.DataFrame()

    balance = SEED_CAPITAL
    shares = 0
    equity_curve = [{'time': trade_df['time'].iloc[0] - timedelta(hours=1),
                     'equity': SEED_CAPITAL, 'event': 'Start'}]

    for _, row in trade_df.iterrows():
        if row['side'] == 'buy':
            cost = row['qty'] * row['avg_price']
            # Only count up to what our challenge balance can afford
            if cost <= balance * 1.01:  # Small tolerance for market fills
                balance -= cost
                shares += row['qty']
                equity_curve.append({
                    'time': row['time'],
                    'equity': balance + (shares * row['avg_price']),
                    'event': f"BUY {int(row['qty'])} @ ${row['avg_price']:.2f}"
                })
        elif row['side'] == 'sell':
            if shares > 0:
                proceeds = row['qty'] * row['avg_price']
                shares -= row['qty']
                balance += proceeds
                equity_curve.append({
                    'time': row['time'],
                    'equity': balance + (shares * row['avg_price']) if shares > 0 else balance,
                    'event': f"SELL {int(row['qty'])} @ ${row['avg_price']:.2f}"
                })

    return pd.DataFrame(equity_curve)

# ============================================================
# 6. SAFEGUARDS
# ============================================================
def check_safeguards():
    ny_time = datetime.now(pytz.timezone('US/Eastern'))

    # Layer 1: Earnings Blackout
    if ny_time.date() == date(2026, 5, 6):
        return False, "⚠️ Earnings Blackout Active (May 6, 2026). Trading paused."

    # Layer 2: Market Hours (9:30 AM - 4:00 PM ET)
    market_open = ny_time.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = ny_time.replace(hour=16, minute=0, second=0, microsecond=0)
    if not (market_open <= ny_time <= market_close) or ny_time.weekday() > 4:
        return False, "⏸️ Outside market hours."

    return True, "✅ All safeguards passed. Engine armed."

# ============================================================
# 7. MAIN DASHBOARD
# ============================================================
def main():
    st.title(f"📈 NRDS $300 Challenge - Max-Buy Compounder")

    df = get_data()
    if df.empty:
        st.warning("Awaiting market data from Alpaca IEX feed...")
        return

    # --- Latest Tick ---
    latest = df.iloc[-1]
    current_price = latest['close']
    rsi_col = [c for c in df.columns if c.startswith('RSI')][0]
    bbl_col = [c for c in df.columns if c.startswith('BBL')][0]
    bbu_col = [c for c in df.columns if c.startswith('BBU')][0]
    vwap_col = [c for c in df.columns if c.startswith('VWAP')][0]

    rsi_val = latest[rsi_col]
    lower_bb = latest[bbl_col]
    upper_bb = latest[bbu_col]
    vwap_val = latest[vwap_col]

    # --- Portfolio State (from Alpaca - persists forever) ---
    account = trading_client.get_account()
    cash_available = float(account.cash)

    try:
        position = trading_client.get_open_position(SYMBOL)
        held_qty = int(float(position.qty))
        unrealized_pl = float(position.unrealized_pl)
        avg_entry = float(position.avg_entry_price)
    except Exception:
        position = None
        held_qty = 0
        unrealized_pl = 0.0
        avg_entry = 0.0

    # --- SIDEBAR ---
    st.sidebar.header("💰 $300 Challenge")
    # Calculate virtual challenge balance from trade history
    trade_df = get_trade_history()
    equity_df = calculate_challenge_equity(trade_df)
    if not equity_df.empty:
        current_equity = equity_df['equity'].iloc[-1]
        gain = current_equity - SEED_CAPITAL
        gain_pct = (gain / SEED_CAPITAL) * 100
        st.sidebar.metric("Challenge Equity", f"${current_equity:.2f}",
                          delta=f"${gain:+.2f} ({gain_pct:+.1f}%)")
    else:
        current_equity = SEED_CAPITAL
        st.sidebar.metric("Challenge Equity", f"${SEED_CAPITAL:.2f}", delta="Waiting for first trade")

    st.sidebar.markdown("---")
    st.sidebar.header("💼 Alpaca Paper Account")
    st.sidebar.write(f"**Cash:** ${cash_available:.2f}")
    st.sidebar.write(f"**{SYMBOL} Held:** {held_qty} shares")
    if held_qty > 0:
        pl_color = "green" if unrealized_pl > 0 else "red"
        st.sidebar.markdown(
            f"**Entry:** ${avg_entry:.2f} | **P/L:** "
            f"<span style='color:{pl_color}'>${unrealized_pl:.2f}</span>",
            unsafe_allow_html=True)

    st.sidebar.markdown("---")
    is_safe, sys_msg = check_safeguards()
    st.sidebar.info(sys_msg)

    # --- SIGNAL LOGIC ---
    signal = "HOLD"
    if current_price < lower_bb and rsi_val < 30 and current_price < vwap_val:
        signal = "BUY"
    elif current_price > upper_bb or rsi_val > 70:
        signal = "SELL"

    # --- Price & Signal Display ---
    col_price, col_signal, col_indicators = st.columns([1, 1, 2])
    with col_price:
        st.metric("Latest Price", f"${current_price:.2f}")
    with col_signal:
        if signal == "BUY":
            st.success(f"🟢 **BUY SIGNAL**")
        elif signal == "SELL":
            st.error(f"🔴 **SELL SIGNAL**")
        else:
            st.info(f"⚪ **HOLD** - Waiting")
    with col_indicators:
        st.write(f"**RSI:** {rsi_val:.1f} | **VWAP:** ${vwap_val:.2f} | "
                 f"**BB Low:** ${lower_bb:.2f} | **BB High:** ${upper_bb:.2f}")

    # --- EXECUTION ENGINE ---
    if "last_trade_time" not in st.session_state:
        st.session_state.last_trade_time = None

    if is_safe:
        # BUY: Max shares the $300 challenge can afford
        if signal == "BUY" and held_qty == 0:
            # Use the challenge equity (not the full $100k paper account)
            buy_budget = min(current_equity, cash_available)
            qty_to_buy = math.floor(buy_budget / current_price)

            if qty_to_buy > 0 and st.session_state.last_trade_time != latest.name:
                req = MarketOrderRequest(
                    symbol=SYMBOL,
                    qty=qty_to_buy,
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY
                )
                trading_client.submit_order(req)
                alert_msg = (f"BUY EXECUTED: {qty_to_buy} shares of {SYMBOL} "
                             f"@ ~${current_price:.2f}. "
                             f"Cost: ~${(qty_to_buy * current_price):.2f}")
                st.success(alert_msg)
                send_sms_alert(f"{SYMBOL} BUY", alert_msg)
                st.session_state.last_trade_time = latest.name

        # SELL: Liquidate entire position
        elif signal == "SELL" and held_qty > 0:
            if st.session_state.last_trade_time != latest.name:
                req = MarketOrderRequest(
                    symbol=SYMBOL,
                    qty=held_qty,
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY
                )
                trading_client.submit_order(req)
                alert_msg = (f"SELL EXECUTED: {held_qty} shares of {SYMBOL} "
                             f"@ ~${current_price:.2f}. P/L: ${unrealized_pl:.2f}")
                st.success(alert_msg)
                send_sms_alert(f"{SYMBOL} SELL", alert_msg)
                st.session_state.last_trade_time = latest.name

    # --- CHARTS ---
    # Tab 1: Live Price Chart | Tab 2: $300 Challenge Growth
    tab1, tab2, tab3 = st.tabs(["📊 Live Chart", "📈 $300 Challenge", "📋 Trade Log"])

    with tab1:
        fig = go.Figure()
        fig.add_trace(go.Candlestick(
            x=df.index, open=df['open'], high=df['high'],
            low=df['low'], close=df['close'], name='Price'))
        fig.add_trace(go.Scatter(
            x=df.index, y=df[bbu_col],
            line=dict(color='gray', width=1, dash='dot'), name='Upper BB'))
        fig.add_trace(go.Scatter(
            x=df.index, y=df[bbl_col],
            line=dict(color='gray', width=1, dash='dot'), name='Lower BB'))
        fig.add_trace(go.Scatter(
            x=df.index, y=df[vwap_col],
            line=dict(color='#ff9900', width=1.5), name='VWAP'))
        fig.update_layout(
            title=f"{SYMBOL} Live (1-Min, IEX Feed)",
            xaxis_title="Time (EST)", yaxis_title="Price ($)",
            template="plotly_dark", height=500,
            margin=dict(l=0, r=0, t=40, b=0))
        st.plotly_chart(fig, use_container_width=True)

    with tab2:
        if not equity_df.empty and len(equity_df) > 1:
            fig2 = go.Figure()
            fig2.add_trace(go.Scatter(
                x=equity_df['time'], y=equity_df['equity'],
                mode='lines+markers',
                line=dict(color='#00cc96', width=2),
                text=equity_df['event'],
                hovertemplate='%{text}<br>Equity: $%{y:.2f}<extra></extra>',
                name='Challenge Equity'))
            fig2.add_hline(y=SEED_CAPITAL,
                           line_dash="dash", line_color="yellow",
                           annotation_text="$300 Start")
            fig2.update_layout(
                title="$300 Challenge - Growth Over Time",
                xaxis_title="Date", yaxis_title="Equity ($)",
                template="plotly_dark", height=400,
                margin=dict(l=0, r=0, t=40, b=0))
            st.plotly_chart(fig2, use_container_width=True)
        else:
            st.info("Your $300 Challenge growth chart will appear here after your first completed trade!")

    with tab3:
        if not trade_df.empty:
            display_df = trade_df.copy()
            display_df['time'] = pd.to_datetime(display_df['time']).dt.strftime('%m/%d %H:%M')
            display_df['side'] = display_df['side'].str.upper()
            display_df['avg_price'] = display_df['avg_price'].apply(lambda x: f"${x:.2f}")
            display_df['total'] = display_df['total'].apply(lambda x: f"${x:.2f}")
            display_df.columns = ['Time', 'Side', 'Shares', 'Fill Price', 'Total']
            st.dataframe(display_df, use_container_width=True, hide_index=True)
        else:
            st.info("No trades yet. Your full trade log will appear here after the bot executes its first order!")

if __name__ == "__main__":
    main()