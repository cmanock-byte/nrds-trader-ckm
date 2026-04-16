import streamlit as st
import pandas as pd
import pandas_ta as ta
import plotly.graph_objects as go
import os
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timedelta, timezone
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed
from streamlit_autorefresh import st_autorefresh

# ==========================================
# 1. SETUP & KEYS (REPLACE WITH YOUR ACTUAL KEYS!)
# ==========================================
# Alpaca Keys
ALPACA_API_KEY = "PK76I5OBEQ7J4MWZBH4O35QIXQ"
ALPACA_SECRET_KEY = "BCzKQgatG6eGznUaUEzBcLsCY3LzNJaq1skikmwsHubz"

# Free Alerting Setup
SENDER_EMAIL = "trading.app.cg@gmail.com"  # The Gmail sending the alert
SENDER_PASSWORD = "fdbi bmuv tahz Ipqt" # The App Password from Google
CELL_PHONE_EMAIL = "5595485468@tmomail.net" # Your phone number @ your carrier's gateway

alpaca_client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)

# ==========================================
# 2. FREE NOTIFICATION FUNCTION
# ==========================================
def fire_alerts(title, message_body):
    # 1. Send SMS via Carrier Email Gateway
    try:
        msg = MIMEText(message_body)
        msg['Subject'] = title
        msg['From'] = SENDER_EMAIL
        msg['To'] = CELL_PHONE_EMAIL
        
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.send_message(msg)
    except Exception as e:
        st.error(f"Email/SMS Alert Failed: {e}")

# ==========================================
# 3. PAGE CONFIG & AUTOREFRESH
# ==========================================
st.set_page_config(page_title="NRDS Trader", layout="wide")
st_autorefresh(interval=30000, key="live_clock") 

# ==========================================
# 4. SIDEBAR SAFEGUARDS
# ==========================================
st.sidebar.markdown("### 🛡️ 8-Layer Safeguards")
st.sidebar.markdown("**Next Earnings:** May 6, 2026")
earnings_guard = st.sidebar.checkbox("Earnings Blackout Active (May 1 - May 8)")
circuit_breaker = st.sidebar.checkbox("Circuit Breaker (3 Losses / $50 Down)")
trend_guard = st.sidebar.checkbox("Trend Guard Active")

if earnings_guard or circuit_breaker:
    st.error("🚨 TRADING HALTED: A critical safeguard is currently active. Do not execute trades.")
    st.stop()

# ==========================================
# 5. DATA FETCHING FUNCTION
# ==========================================
@st.cache_data(ttl=15)
def get_nrds_data():
    now = datetime.now(timezone.utc)
    start_date = now - timedelta(days=3)
    
    request_params = StockBarsRequest(
        symbol_or_symbols="NRDS",
        timeframe=TimeFrame.Minute,
        start=start_date,
        feed=DataFeed.IEX 
    )
    
    bars = alpaca_client.get_stock_bars(request_params).df
    if bars.empty: return pd.DataFrame()
        
    bars = bars.reset_index(level=0, drop=True)
    
    if 'vwap' in bars.columns:
        bars = bars.drop(columns=['vwap'])
    
    bars.ta.bbands(length=20, std=2.0, append=True)
    bars.ta.rsi(length=10, append=True)
    bars.ta.vwap(append=True)
    
    col_map = {}
    for col in bars.columns:
        if col.startswith('BBL_'): col_map[col] = 'lower_bb'
        elif col.startswith('BBU_'): col_map[col] = 'upper_bb'
        elif col.startswith('RSI_'): col_map[col] = 'rsi'
        elif col.startswith('VWAP_'): col_map[col] = 'vwap'
        
    bars = bars.rename(columns=col_map)
    bars = bars.loc[:, ~bars.columns.duplicated(keep='last')]
    return bars

# ==========================================
# 6. MAIN DASHBOARD UI
# ==========================================
st.title("📈 NRDS Mean Reversion Strategy (Paper Trading Signals)")

if 'last_alert' not in st.session_state:
    st.session_state.last_alert = None

try:
    df = get_nrds_data()
    
    if not df.empty:
        current_price = df['close'].iloc[-1]
        lower_band = df['lower_bb'].iloc[-1]
        upper_band = df['upper_bb'].iloc[-1]
        current_rsi = df['rsi'].iloc[-1]
        current_vwap = float(df['vwap'].iloc[-1])
        
        st.subheader(f"Current NRDS Price: ${current_price:.2f}")
        
        col1, col2 = st.columns(2)
        
        with col1:
            st.markdown("### 🚦 Current Signal")
            
            # Buy Signal Logic
            if current_price < lower_band and current_rsi < 30 and not trend_guard:
                st.success("🟢 BUY SIGNAL - Mean Reversion Setup Detected!")
                if st.session_state.last_alert != "BUY":
                    fire_alerts(
                        title="NRDS BUY SIGNAL", 
                        message_body=f"Price ${current_price:.2f} is below lower band. RSI is {current_rsi:.1f}"
                    )
                    st.session_state.last_alert = "BUY"
                        
            # Sell Signal Logic
            elif current_price > upper_band and current_rsi > 70 and not trend_guard:
                st.error("🔴 SELL SIGNAL - Mean Reversion Setup Detected!")
                if st.session_state.last_alert != "SELL":
                    fire_alerts(
                        title="NRDS SELL SIGNAL", 
                        message_body=f"Price ${current_price:.2f} is above upper band. RSI is {current_rsi:.1f}"
                    )
                    st.session_state.last_alert = "SELL"
                        
            else:
                st.info("⚪ NEUTRAL - Waiting for a setup.")

        with col2:
            st.markdown("### 📊 Live Indicators")
            st.write(f"**RSI (10):** {current_rsi:.1f}")
            st.write(f"**VWAP:** ${current_vwap:.2f}")
            st.write(f"**Lower Band:** ${lower_band:.2f}")
            st.write(f"**Upper Band:** ${upper_band:.2f}")

        # --- THE CHART ---
        st.markdown("### 📈 5-Minute Chart")
        fig = go.Figure()
        
        fig.add_trace(go.Candlestick(
            x=df.index, open=df['open'], high=df['high'], 
            low=df['low'], close=df['close'], name='Price'
        ))
        fig.add_trace(go.Scatter(
            x=df.index, y=df['vwap'], 
            line=dict(color='orange', width=2), name='VWAP'
        ))
        fig.add_trace(go.Scatter(
            x=df.index, y=df['upper_bb'], 
            line=dict(color='rgba(200, 200, 200, 0.5)', width=1, dash='dash'), name='Upper BB'
        ))
        fig.add_trace(go.Scatter(
            x=df.index, y=df['lower_bb'], 
            line=dict(color='rgba(200, 200, 200, 0.5)', width=1, dash='dash'), name='Lower BB',
            fill='tonexty', fillcolor='rgba(150, 150, 150, 0.1)'
        ))
        
        fig.update_layout(height=500, margin=dict(l=0, r=0, t=30, b=0), xaxis_rangeslider_visible=False)
        st.plotly_chart(fig, use_container_width=True)

    else:
        st.warning("No data found for the requested period. The market might be closed.")

except Exception as e:
    st.error(f"An error occurred: {e}")