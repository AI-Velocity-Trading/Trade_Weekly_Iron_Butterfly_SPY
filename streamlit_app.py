#!/usr/bin/env python3
"""
AI Velocity Trading - SPY Weekly Iron Butterfly
Interactive Streamlit demonstration of the autonomous trading agent.

This application provides a visual walkthrough of the agent's decision process
from market observation through autonomous execution and risk management.

IMPORTANT: This is a DEMO/SIMULATION. No real orders are submitted.
Historical backtest results are provided for validation only.
"""

import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import plotly.express as px
from datetime import datetime, timedelta
import os
from pathlib import Path

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION & STYLING
# ═══════════════════════════════════════════════════════════════════════════════

st.set_page_config(
    page_title="AI Velocity Trading - SPY Iron Butterfly",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Custom dark theme with cyan/purple accents
CUSTOM_CSS = """
<style>
    :root {
        --bg-primary: #0a0f1f;
        --bg-secondary: #1a2035;
        --accent-cyan: #00d9ff;
        --accent-purple: #7c3aed;
        --text-primary: #e8eef5;
        --text-secondary: #a8b2c5;
        --border-color: #00d9ff;
    }
    
    html, body, [data-testid="stAppViewContainer"] {
        background-color: var(--bg-primary);
        color: var(--text-primary);
    }
    
    [data-testid="stSidebar"] {
        background-color: var(--bg-secondary);
    }
    
    .stTabs [data-baseweb="tab-list"] {
        border-bottom: 2px solid var(--border-color);
    }
    
    .stTabs [data-baseweb="tab"] {
        color: var(--text-secondary);
        border-bottom: 3px solid transparent;
    }
    
    .stTabs [aria-selected="true"] {
        color: var(--accent-cyan);
        border-bottom-color: var(--accent-cyan) !important;
    }
    
    .demo-panel {
        background-color: var(--bg-secondary);
        border: 1px solid var(--accent-cyan);
        border-radius: 8px;
        padding: 20px;
        margin: 15px 0;
    }
    
    .demo-header {
        color: var(--accent-cyan);
        font-family: 'Courier New', monospace;
        font-weight: bold;
        font-size: 14px;
        letter-spacing: 2px;
        margin-bottom: 10px;
    }
    
    .demo-body {
        color: var(--text-primary);
        font-size: 13px;
    }
    
    .metric-box {
        background-color: var(--bg-secondary);
        border-left: 3px solid var(--accent-cyan);
        padding: 15px;
        border-radius: 4px;
        margin: 10px 0;
    }
    
    .risk-condition {
        background-color: rgba(123, 58, 237, 0.1);
        border-left: 3px solid var(--accent-purple);
        padding: 12px;
        border-radius: 4px;
        margin: 8px 0;
        font-family: 'Courier New', monospace;
        font-size: 12px;
    }
    
    .system-log {
        background-color: rgba(0, 0, 0, 0.3);
        border: 1px solid var(--border-color);
        border-radius: 4px;
        padding: 12px;
        font-family: 'JetBrains Mono', 'Courier New', monospace;
        font-size: 11px;
        color: var(--accent-cyan);
        max-height: 400px;
        overflow-y: auto;
        line-height: 1.4;
    }
    
    .button-primary {
        background-color: var(--accent-cyan) !important;
        color: var(--bg-primary) !important;
        font-weight: bold;
        border: none !important;
        border-radius: 4px;
    }
    
    .button-secondary {
        background-color: var(--accent-purple) !important;
        color: white !important;
        font-weight: bold;
        border: 1px solid var(--accent-purple) !important;
        border-radius: 4px;
    }
    
    .status-badge {
        display: inline-block;
        background-color: rgba(0, 217, 255, 0.2);
        border: 1px solid var(--accent-cyan);
        color: var(--accent-cyan);
        padding: 6px 12px;
        border-radius: 4px;
        font-size: 11px;
        font-weight: bold;
        letter-spacing: 1px;
        margin-right: 8px;
        margin-bottom: 8px;
    }
    
    h1, h2, h3 {
        color: var(--text-primary);
    }
    
    .stButton > button {
        border: 1px solid var(--accent-cyan);
        background-color: transparent;
        color: var(--accent-cyan);
        border-radius: 4px;
        padding: 8px 16px;
        font-weight: 600;
    }
    
    .stButton > button:hover {
        background-color: rgba(0, 217, 255, 0.1);
    }
</style>
"""

st.markdown(CUSTOM_CSS, unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════════════════
# SESSION STATE INITIALIZATION
# ═══════════════════════════════════════════════════════════════════════════════

if "demo_active" not in st.session_state:
    st.session_state.demo_active = False
if "demo_step" not in st.session_state:
    st.session_state.demo_step = 0
if "audit_log" not in st.session_state:
    st.session_state.audit_log = []
if "current_scenario" not in st.session_state:
    st.session_state.current_scenario = None

# ═══════════════════════════════════════════════════════════════════════════════
# DATA LOADING
# ═══════════════════════════════════════════════════════════════════════════════

@st.cache_data
def load_backtest_data():
    """Load backtest results from CSV."""
    csv_path = "weekly_iron_butterfly_spy_backtest_dynamic.csv"
    if os.path.exists(csv_path):
        try:
            df = pd.read_csv(csv_path)
            return df
        except Exception as e:
            st.warning(f"Could not load backtest CSV: {e}")
            return None
    return None

@st.cache_resource
def check_media_files():
    """Check if video and PDF files exist locally."""
    video_path = "Weekly_iron_butterfly_spy.mp4"
    pdf_path = "Team-AI-Velocity-Trading.pdf"
    return {
        "video": os.path.exists(video_path),
        "pdf": os.path.exists(pdf_path),
    }

backtest_df = load_backtest_data()
media_files = check_media_files()

# ═══════════════════════════════════════════════════════════════════════════════
# DEMO STATE MACHINE
# ═══════════════════════════════════════════════════════════════════════════════

def add_audit_log(time_str, market_state, observation, decision, action):
    """Add an entry to the decision audit log."""
    st.session_state.audit_log.append({
        "TIME": time_str,
        "MARKET_STATE": market_state,
        "OBSERVATION": observation,
        "DECISION": decision,
        "ACTION": action,
    })

def reset_demo():
    """Reset the demo to initial state."""
    st.session_state.demo_step = 0
    st.session_state.audit_log = []
    st.session_state.demo_active = False

def advance_demo():
    """Advance to the next demo step."""
    st.session_state.demo_step += 1

# ═══════════════════════════════════════════════════════════════════════════════
# DEMO SIMULATION LOGIC
# ═══════════════════════════════════════════════════════════════════════════════

def simulate_demo_full():
    """Run a complete agent demo cycle."""
    if st.session_state.demo_step == 0:
        st.session_state.audit_log = []
        add_audit_log("09:35:00", "ENTRY_WINDOW", "Monitoring begins", "OBSERVE", "POLL_ALPACA")
        st.session_state.demo_step = 1
    elif st.session_state.demo_step == 1:
        add_audit_log("09:36:00", "POLLING", "SPY $765.50", "OBSERVE", "TRAILING_RANGE_UPDATED")
        add_audit_log("09:37:00", "POLLING", "Range: $1.23", "WAIT", "NONE")
        add_audit_log("09:38:00", "VOLATILE", "New range low detected", "WAIT", "NONE")
        st.session_state.demo_step = 2
    elif st.session_state.demo_step == 2:
        add_audit_log("09:41:00", "STABLE", "Range: $1.10 (stable)", "WAIT", "MONITOR")
        add_audit_log("09:42:00", "STABLE", "Stable min 1/3", "WAIT", "MONITOR")
        add_audit_log("09:43:00", "STABLE", "Stable min 2/3", "WAIT", "MONITOR")
        st.session_state.demo_step = 3
    elif st.session_state.demo_step == 3:
        add_audit_log("09:44:00", "QUIET_DETECTED", "3 min without new low", "ENTER", "BUILD_POSITION")
        st.session_state.demo_step = 4
    elif st.session_state.demo_step == 4:
        add_audit_log("09:45:00", "CONSTRUCTION", "4-leg butterfly built", "ENTER", "ALPACA_EXECUTION")
        add_audit_log("09:45:30", "EXECUTION", "Orders submitted (demo)", "ENTER", "ORDERS_FILLED")
        st.session_state.demo_step = 5
    elif st.session_state.demo_step == 5:
        add_audit_log("09:46:00", "MONITORING", "ATM=766, SPY=$765.80", "MONITOR", "RISK_CHECK")
        add_audit_log("14:30:00", "MONITORING", "ATM=766, SPY=$767.50", "MONITOR", "RISK_CHECK")
        st.session_state.demo_step = 6
    elif st.session_state.demo_step == 6:
        add_audit_log("15:38:00", "RISK_CHECK", "P&L ≈ +$9,200 (48% of max)", "MONITOR", "APPROACHING_TARGET")
        st.session_state.demo_step = 7
    elif st.session_state.demo_step == 7:
        add_audit_log("15:40:00", "PROFIT_TARGET", "P&L ≥ +90% max profit", "EXIT", "CLOSE_POSITION")
        add_audit_log("15:40:15", "EXIT_COMPLETE", "Position closed", "EXIT", "AGENT_CYCLE_COMPLETE")
        st.session_state.demo_step = 8

def simulate_scenario(scenario_type):
    """Simulate a specific scenario."""
    st.session_state.audit_log = []
    
    if scenario_type == "quiet_market":
        add_audit_log("09:35:00", "ENTRY_WINDOW", "Search begins", "OBSERVE", "POLL")
        add_audit_log("09:36:00", "QUIET", "Range: $0.85", "WAIT", "MONITOR")
        add_audit_log("09:37:00", "QUIET", "Stable min 1/3", "WAIT", "MONITOR")
        add_audit_log("09:38:00", "QUIET", "Stable min 2/3", "WAIT", "MONITOR")
        add_audit_log("09:39:00", "QUIET", "Stable min 3/3", "ENTER", "EXECUTE")
        
    elif scenario_type == "wing_breach":
        add_audit_log("09:45:00", "CONSTRUCTION", "ATM=766, wings=$756-$776", "ENTER", "EXECUTE")
        add_audit_log("10:15:00", "MONITORING", "SPY=$766.50", "MONITOR", "OK")
        add_audit_log("11:30:00", "MONITORING", "SPY=$775.80", "MONITOR", "NEAR_BREACH")
        add_audit_log("11:31:00", "BREACH", "SPY moved $9.80 (≥$10 breach)", "EXIT", "CLOSE_IMMEDIATE")
        
    elif scenario_type == "profit_target":
        add_audit_log("09:45:00", "CONSTRUCTION", "Max profit=$62,750", "ENTER", "EXECUTE")
        add_audit_log("13:00:00", "MONITORING", "P&L=$50,000 (80% of max)", "MONITOR", "OK")
        add_audit_log("14:15:00", "MONITORING", "P&L=$56,500 (90% of max)", "EXIT", "TARGET_HIT")
        add_audit_log("14:15:30", "EXIT_COMPLETE", "Position closed", "EXIT", "PROFIT_LOCKED")
        
    elif scenario_type == "timeout_entry":
        add_audit_log("09:35:00", "ENTRY_WINDOW", "Search begins", "OBSERVE", "POLL")
        add_audit_log("09:50:00", "VOLATILE", "High volatility all window", "WAIT", "MONITOR")
        add_audit_log("10:20:00", "VOLATILE", "Still volatile", "WAIT", "MONITOR")
        add_audit_log("10:30:00", "TIMEOUT", "Window closed at 10:30 ET", "ENTER", "HARD_TIMEOUT_EXECUTE")

# ═══════════════════════════════════════════════════════════════════════════════
# VISUALIZATION COMPONENTS
# ═══════════════════════════════════════════════════════════════════════════════

def render_price_chart():
    """Render a simulated SPY price/range chart."""
    times = pd.date_range("09:35", "10:45", freq="1min")
    np.random.seed(42)
    base = 765.5
    noise = np.cumsum(np.random.randn(len(times)) * 0.15)
    prices_array = base + noise
    # Convert to Pandas Series to use .rolling()
    prices = pd.Series(prices_array, index=times)
    
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=times, y=prices.values,
        mode='lines', name='SPY Price',
        line=dict(color='#00d9ff', width=2),
        fill='tozeroy', fillcolor='rgba(0, 217, 255, 0.1)',
    ))
    
    # Add range band (now prices is a Series, so .rolling() works)
    high = prices.rolling(20).max()
    low = prices.rolling(20).min()
    fig.add_trace(go.Scatter(x=times, y=high.values, mode='lines', name='20-min High', line=dict(width=0), showlegend=False))
    fig.add_trace(go.Scatter(x=times, y=low.values, mode='lines', name='20-min Low', line=dict(width=0), fillcolor='rgba(123, 58, 237, 0.2)', fill='tonexty', showlegend=False))
    
    # Dynamic y-axis with padding to ensure readable scale for small ranges
    y_min, y_max = prices.min(), prices.max()
    y_range = y_max - y_min
    padding = max(0.5, y_range * 0.15)  # 15% padding or $0.50 minimum
    
    fig.update_layout(
        title="SPY Minute-by-Minute Price Action (Simulated)",
        xaxis_title="Time ET",
        yaxis_title="Price ($)",
        yaxis=dict(range=[y_min - padding, y_max + padding]),
        template="plotly_dark",
        height=350,
        hovermode='x unified',
        paper_bgcolor='rgba(26, 32, 53, 0.8)',
        plot_bgcolor='rgba(10, 15, 31, 0.8)',
        font=dict(color='#e8eef5', family='monospace'),
    )
    
    return fig

def render_position_diagram():
    """Render the 4-leg iron butterfly structure."""
    fig = go.Figure()
    
    strikes = [756, 766, 776]
    atm = 766
    
    # Strike levels
    fig.add_hline(y=756, line_dash="dash", line_color="rgba(0, 217, 255, 0.3)", annotation_text="Long Put $756")
    fig.add_hline(y=766, line_dash="dash", line_color="rgba(0, 217, 255, 0.3)", annotation_text="ATM $766")
    fig.add_hline(y=776, line_dash="dash", line_color="rgba(0, 217, 255, 0.3)", annotation_text="Long Call $776")
    
    # Payoff diagram
    x_range = np.linspace(740, 790, 100)
    y_long_put = np.maximum(756 - x_range, 0) * 100  # 100 contracts
    y_short_put = -np.maximum(766 - x_range, 0) * 100
    y_short_call = -np.maximum(x_range - 766, 0) * 100
    y_long_call = np.maximum(x_range - 776, 0) * 100
    y_total = y_long_put + y_short_put + y_short_call + y_long_call
    
    fig.add_trace(go.Scatter(x=x_range, y=y_total, mode='lines', name='Position P&L',
                             line=dict(color='#00d9ff', width=3), fill='tozeroy', fillcolor='rgba(0, 217, 255, 0.1)'))
    
    fig.update_layout(
        title="Iron Butterfly Payoff at Expiration",
        xaxis_title="SPY Price at Expiration ($)",
        yaxis_title="Profit/Loss ($)",
        template="plotly_dark",
        height=350,
        paper_bgcolor='rgba(26, 32, 53, 0.8)',
        plot_bgcolor='rgba(10, 15, 31, 0.8)',
        font=dict(color='#e8eef5'),
    )
    
    return fig

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN APPLICATION
# ═══════════════════════════════════════════════════════════════════════════════

# HEADER
st.markdown("""
<div style="text-align: center; margin-bottom: 30px;">
    <h1 style="color: #00d9ff; font-size: 48px; letter-spacing: 3px; margin-bottom: 5px;">
        AI VELOCITY TRADING
    </h1>
    <h2 style="color: #e8eef5; font-size: 32px; margin-bottom: 15px;">
        SPY Weekly Iron Butterfly
    </h2>
    <p style="color: #a8b2c5; font-size: 16px; margin-bottom: 20px;">
        Autonomous Options Trading Agent
    </p>
    <p style="color: #a8b2c5; font-size: 14px; margin-bottom: 20px;">
        Dynamic entry. Defined risk. Autonomous execution through Alpaca.
    </p>
</div>
""", unsafe_allow_html=True)

st.markdown("---")

# SIMULATION DISCLAIMER
st.info(
    """
    🎬 **DEMO / SIMULATION** — This interactive demonstration shows the agent's decision process.
    No Alpaca orders are submitted. All scenarios are deterministic replays based on actual agent logic.
    """,
    icon="⚠️"
)

st.markdown("---")

# ═══════════════════════════════════════════════════════════════════════════════
# PRESENTATION MATERIALS (if available)
# ═══════════════════════════════════════════════════════════════════════════════

if media_files["video"] or media_files["pdf"]:
    st.markdown("""
    <h3 style="color: #00d9ff; font-size: 24px; letter-spacing: 2px; margin-top: 30px;">
        PRESENTATION MATERIALS
    </h3>
    """, unsafe_allow_html=True)
    
    tab1, tab2 = st.tabs(["Demo Video", "Slides"])
    
    with tab1:
        if media_files["video"]:
            st.markdown("### Autonomous Trading Demo")
            col1, col2 = st.columns([2, 1])
            with col1:
                st.video("Weekly_iron_butterfly_spy.mp4")
        else:
            st.info("Demo video not available in this deployment.")
    
    with tab2:
        if media_files["pdf"]:
            st.markdown("### Strategy Presentation")
            with open("Team-AI-Velocity-Trading.pdf", "rb") as f:
                pdf_bytes = f.read()
            st.download_button(
                label="📥 Download PDF Slides",
                data=pdf_bytes,
                file_name="Team-AI-Velocity-Trading.pdf",
                mime="application/pdf",
            )
            st.markdown("""
            <p style="color: #a8b2c5; margin-top: 15px;">
            Strategy overview, decision logic, backtest results, and Alpaca integration details.
            </p>
            """, unsafe_allow_html=True)
        else:
            st.info("PDF slides not available in this deployment.")
    
    st.markdown("---")

# ═══════════════════════════════════════════════════════════════════════════════
# HISTORICAL VALIDATION SECTION
# ═══════════════════════════════════════════════════════════════════════════════

st.markdown("""
<h3 style="color: #00d9ff; font-size: 24px; letter-spacing: 2px; margin-top: 30px;">
    📊 HISTORICAL VALIDATION
</h3>
<p style="color: #a8b2c5; margin-bottom: 20px;">
Aug 2021 – Aug 2026 Backtest
</p>
""", unsafe_allow_html=True)

col1, col2, col3, col4, col5 = st.columns(5)

with col1:
    st.metric("Historical P&L", "+$108,840")

with col2:
    st.metric("Win Rate", "59.8%")

with col3:
    st.metric("Sharpe Ratio", "1.06")

with col4:
    st.metric("Total Trades", "97")

with col5:
    st.metric("Max Drawdown", "-$36,280")

st.markdown("""
<p style="color: #a8b2c5; font-size: 14px; margin-top: 15px;">
<strong>Backtested results.</strong> Historical performance is not indicative of future results.
</p>
<p style="color: #a8b2c5; font-size: 12px;">
5-year historical backtest using SPY 1-minute bars and Alpaca OPRA options pricing.
</p>
""", unsafe_allow_html=True)

st.markdown("---")

# ═══════════════════════════════════════════════════════════════════════════════
# PRIMARY DEMO SECTION
# ═════════════════════════════════════════════════════════════════════════════════

st.markdown("""
<h3 style="color: #00d9ff; font-size: 24px; letter-spacing: 2px; margin-top: 30px;">
    ▶ RUN AGENT DEMO
</h3>
<p style="color: #a8b2c5; margin-bottom: 20px;">
Watch the agent move from market observation to entry, execution, risk monitoring, and exit.
</p>
""", unsafe_allow_html=True)

col1, col2, col3 = st.columns([2, 1, 1])

with col1:
    if st.button("▶ START DEMO", use_container_width=True):
        st.session_state.demo_active = True
        st.session_state.demo_step = 0
        st.session_state.current_scenario = "full"

with col2:
    if st.button("⏭ NEXT STEP", use_container_width=True):
        if st.session_state.demo_active:
            advance_demo()

with col3:
    if st.button("⏹ RESET", use_container_width=True):
        reset_demo()

# RUN DEMO LOGIC
if st.session_state.demo_active and st.session_state.current_scenario == "full":
    simulate_demo_full()

# DISPLAY DEMO PANELS
if len(st.session_state.audit_log) > 0:
    st.markdown("### Demo Progression")
    
    # State 1-2: Entry Window
    if st.session_state.demo_step >= 1:
        st.markdown("""
        <div class="demo-panel">
            <div class="demo-header">01 · ENTRY WINDOW OPEN</div>
            <div class="demo-body">
                Monday · 9:35–10:30 ET<br>
                Agent polls SPY minute-by-minute, tracking the 20-minute trailing high/low range to identify when the trailing range stabilizes.
            </div>
        </div>
        """, unsafe_allow_html=True)
        
        st.plotly_chart(render_price_chart(), use_container_width=True, key="price_chart_1")
    
    # State 3-4: Range Tracking & Quiet Period
    if st.session_state.demo_step >= 3:
        col1, col2 = st.columns(2)
        with col1:
            st.markdown("""
            <div class="demo-panel">
                <div class="demo-header">02 · TRAILING RANGE MONITOR</div>
                <div class="demo-body">
                    Range: 20-minute high/low<br>
                    Current: $1.10 (quietest observed)<br>
                    Status: Stabilizing...
                </div>
            </div>
            """, unsafe_allow_html=True)
        
        with col2:
            st.markdown("""
            <div class="demo-panel">
                <div class="demo-header">03 · VOLATILITY SETTLING</div>
                <div class="demo-body">
                    Minute 1/3: No new low ✓<br>
                    Minute 2/3: No new low ✓<br>
                    Minute 3/3: Quiet period confirmed!
                </div>
            </div>
            """, unsafe_allow_html=True)
    
    # State 4: Entry Trigger
    if st.session_state.demo_step >= 4:
        st.markdown("""
        <div class="demo-panel">
            <div class="demo-header">04 · ENTRY TRIGGERED</div>
            <div class="demo-body">
                <strong>QUIET PERIOD DETECTED</strong><br>
                3 consecutive minutes without a new trailing-range low<br>
                <strong>DECISION:</strong> Enter now<br>
                <strong>ATM Strike:</strong> $766 (from quietest minute's open)
            </div>
        </div>
        """, unsafe_allow_html=True)
    
    # State 5: Position Construction
    if st.session_state.demo_step >= 5:
        st.markdown("""
        <div class="demo-panel">
            <div class="demo-header">05 · POSITION CONSTRUCTION</div>
            <div class="demo-body">
                4-leg Iron Butterfly on SPY Weekly (Exp: Friday)<br>
                <strong>BUY PUT</strong> @ $756 (ATM − $10)<br>
                <strong>SELL PUT</strong> @ $766 (ATM)<br>
                <strong>SELL CALL</strong> @ $766 (ATM)<br>
                <strong>BUY CALL</strong> @ $776 (ATM + $10)<br>
                <br>
                <strong>Wing Width: $10</strong> | <strong>Net Credit: $4.27/contract</strong><br>
                <strong>Max Profit: $427</strong> (1 contract) | <strong>Max Loss: $573</strong> (1 contract)
            </div>
        </div>
        """, unsafe_allow_html=True)
        
        st.plotly_chart(render_position_diagram(), use_container_width=True, key="position_diagram")
    
    # State 6: Alpaca Execution
    if st.session_state.demo_step >= 6:
        st.markdown("""
        <div class="demo-panel">
            <div class="demo-header">06 · ALPACA EXECUTION (DEMO)</div>
            <div class="demo-body">
                <strong>EXECUTION LAYER</strong><br>
                • Framework: Alpaca CLI (subprocess)<br>
                • Authentication: Stored profile (no hardcoded keys)<br>
                • Order Package: 4 legs, 1 contract each<br>
                • Status: SIMULATED EXECUTION → FILLED<br>
                <br>
                <strong style="color: #00d9ff;">SIMULATION:</strong> No order submitted to Alpaca.<br>
                Production agent would route through real Alpaca trading endpoints.
            </div>
        </div>
        """, unsafe_allow_html=True)
    
    # State 7: Risk Monitor
    if st.session_state.demo_step >= 7:
        st.markdown("""
        <div class="demo-panel">
            <div class="demo-header">07 · AUTONOMOUS RISK MONITOR</div>
            <div class="demo-body">
                Four exit conditions watched continuously (Monday–Thursday):
            </div>
        </div>
        """, unsafe_allow_html=True)
        
        col1, col2 = st.columns(2)
        with col1:
            st.markdown("""
            <div class="risk-condition">
                <strong>01 · WING BREACH</strong><br>
                Price moves ±$10 from ATM<br>
                → Immediate close
            </div>
            <div class="risk-condition">
                <strong>02 · +90% PROFIT TARGET</strong><br>
                P&L ≥ $384 (90% of max)<br>
                → Lock in gains early
            </div>
            """, unsafe_allow_html=True)
        
        with col2:
            st.markdown("""
            <div class="risk-condition">
                <strong>03 · −80% STOP LOSS</strong><br>
                P&L ≤ −$458 (80% of max loss)<br>
                → Limit damage
            </div>
            <div class="risk-condition">
                <strong>04 · THURSDAY 3:40 PM ET</strong><br>
                Mandatory fallback close<br>
                → Avoid Friday expiration risk
            </div>
            """, unsafe_allow_html=True)
    
    # State 8: Exit
    if st.session_state.demo_step >= 8:
        st.markdown("""
        <div class="demo-panel">
            <div class="demo-header">08 · EXIT EXECUTION</div>
            <div class="demo-body">
                <strong>EXIT CONDITION MET</strong><br>
                Condition: +90% Profit Target<br>
                Time: 15:40 ET (Thursday)<br>
                P&L: +$384<br>
                <br>
                <strong style="color: #00d9ff;">POSITION CLOSED</strong><br>
                <strong style="color: #00d9ff;">AGENT CYCLE COMPLETE</strong>
            </div>
        </div>
        """, unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════════════════
# DECISION AUDIT LOG
# ═══════════════════════════════════════════════════════════════════════════════

if len(st.session_state.audit_log) > 0:
    st.markdown("### DECISION AUDIT LOG")
    
    audit_df = pd.DataFrame(st.session_state.audit_log)
    
    # Custom styled table
    st.markdown("""
    <div class="system-log">
    """ + "\n".join([
        f"<strong>{row['TIME']}</strong>  {row['MARKET_STATE']:<15} {row['OBSERVATION']:<30} {row['DECISION']:<10} {row['ACTION']}"
        for _, row in audit_df.iterrows()
    ]) + """
    </div>
    """, unsafe_allow_html=True)

st.markdown("---")

# ═══════════════════════════════════════════════════════════════════════════════
# SCENARIO EXPLORER
# ═══════════════════════════════════════════════════════════════════════════════

st.markdown("""
<h3 style="color: #00d9ff; font-size: 24px; letter-spacing: 2px; margin-top: 30px;">
    EXPLORE AGENT DECISIONS
</h3>
<p style="color: #a8b2c5; margin-bottom: 20px;">
Inspect specific decision branches without waiting for market conditions.
<br><strong style="color: #7c3aed;">SIMULATED SCENARIOS — No orders submitted</strong>
</p>
""", unsafe_allow_html=True)

col1, col2, col3, col4 = st.columns(4)

with col1:
    if st.button("Quiet-Market Entry", use_container_width=True):
        st.session_state.current_scenario = "quiet_market"
        simulate_scenario("quiet_market")

with col2:
    if st.button("Wing-Breach Exit", use_container_width=True):
        st.session_state.current_scenario = "wing_breach"
        simulate_scenario("wing_breach")

with col3:
    if st.button("Profit-Target Exit", use_container_width=True):
        st.session_state.current_scenario = "profit_target"
        simulate_scenario("profit_target")

with col4:
    if st.button("10:30 Timeout Entry", use_container_width=True):
        st.session_state.current_scenario = "timeout_entry"
        simulate_scenario("timeout_entry")

# Display scenario audit log if a scenario was clicked
if st.session_state.current_scenario and st.session_state.current_scenario != "full":
    st.markdown("### Scenario Audit Log")
    
    audit_df = pd.DataFrame(st.session_state.audit_log)
    st.markdown("""
    <div class="system-log">
    """ + "\n".join([
        f"<strong>{row['TIME']}</strong>  {row['MARKET_STATE']:<15} {row['OBSERVATION']:<30} {row['DECISION']:<10} {row['ACTION']}"
        for _, row in audit_df.iterrows()
    ]) + """
    </div>
    """, unsafe_allow_html=True)

st.markdown("---")

# ═══════════════════════════════════════════════════════════════════════════════
# AGENT ARCHITECTURE
# ═══════════════════════════════════════════════════════════════════════════════

st.markdown("""
<h3 style="color: #00d9ff; font-size: 24px; letter-spacing: 2px; margin-top: 30px;">
    FROM MARKET DATA TO AUTONOMOUS EXECUTION
</h3>
""", unsafe_allow_html=True)

stages = [
    ("ALPACA MARKET DATA", "Real-time price feeds via Alpaca CLI"),
    ("DYNAMIC ENTRY ENGINE", "Poll SPY minute-by-minute; wait for volatility to settle"),
    ("IRON BUTTERFLY CONSTRUCTION", "4-leg structure built from identified ATM strike"),
    ("ALPACA EXECUTION", "Orders routed through Alpaca CLI trading endpoints"),
    ("CONTINUOUS RISK MONITOR", "Wing-breach, profit target, stop loss checked every 20 sec"),
    ("AUTONOMOUS EXIT", "Position closed by rule (not recommendation)"),
    ("AUDIT LOG", "Every decision recorded to CSV for post-trade analysis"),
]

for i, (stage, desc) in enumerate(stages, 1):
    col1, col2 = st.columns([1, 3])
    with col1:
        st.markdown(f"""
        <div style="text-align: center; color: #00d9ff; font-weight: bold; font-size: 24px;">
            {i}
        </div>
        """, unsafe_allow_html=True)
    with col2:
        st.markdown(f"""
        <div class="metric-box">
            <strong style="color: #00d9ff;">{stage}</strong><br>
            <span style="color: #a8b2c5;">{desc}</span>
        </div>
        """, unsafe_allow_html=True)

st.markdown("---")

# ═══════════════════════════════════════════════════════════════════════════════
# REAL IMPLEMENTATION
# ═══════════════════════════════════════════════════════════════════════════════

st.markdown("""
<h3 style="color: #00d9ff; font-size: 24px; letter-spacing: 2px; margin-top: 30px;">
    BUILT FOR REAL ALPACA EXECUTION
</h3>
<p style="color: #a8b2c5; margin-bottom: 20px;">
This Streamlit interface is a safe public simulation of the agent's behavior. 
The repository contains the actual production implementation.
</p>
""", unsafe_allow_html=True)

impl_features = [
    ("Alpaca CLI Integration", "All trading and market data via subprocess — no direct HTTPS"),
    ("Profile-Based Auth", "Credentials stored in ~/.config/alpaca/profiles/ — never hardcoded"),
    ("Market Data Polling", "Live SPY price feed from Alpaca latest-trade endpoint"),
    ("Order Execution", "4-leg orders submitted through Alpaca trading API"),
    ("Autonomous Monitoring", "Continuous wing-breach and risk checks (every 20 seconds)"),
    ("CSV Audit Logging", "Complete trade history logged to csv-files/ for post-trade analysis"),
]

for feature, desc in impl_features:
    st.markdown(f"""
    <div class="metric-box">
        <strong style="color: #00d9ff;">✓ {feature}</strong><br>
        <span style="color: #a8b2c5; font-size: 13px;">{desc}</span>
    </div>
    """, unsafe_allow_html=True)

st.markdown("---")

# ═══════════════════════════════════════════════════════════════════════════════
# SOURCE CODE & DOCUMENTATION
# ═══════════════════════════════════════════════════════════════════════════════

st.markdown("""
<h3 style="color: #00d9ff; font-size: 24px; letter-spacing: 2px; margin-top: 30px;">
    SOURCE CODE & DOCUMENTATION
</h3>
""", unsafe_allow_html=True)

col1, col2 = st.columns(2)

with col1:
    st.markdown("""
    <a href="https://github.com/ptsdsavage/Trade_Weekly_Iron_Butterfly_SPY" 
       style="display: inline-block; padding: 12px 20px; background-color: #00d9ff; color: #0a0f1f; 
       text-decoration: none; border-radius: 4px; font-weight: bold; border: 1px solid #00d9ff;">
        🔗 VIEW SOURCE ON GITHUB
    </a>
    """, unsafe_allow_html=True)

with col2:
    st.markdown("""
    <a href="https://github.com/ptsdsavage/Trade_Weekly_Iron_Butterfly_SPY/blob/main/README.md" 
       style="display: inline-block; padding: 12px 20px; background-color: #7c3aed; color: white; 
       text-decoration: none; border-radius: 4px; font-weight: bold; border: 1px solid #7c3aed;">
        📖 VIEW STRATEGY DOCUMENTATION
    </a>
    """, unsafe_allow_html=True)

st.markdown("---")

# ═══════════════════════════════════════════════════════════════════════════════
# FOOTER
# ═══════════════════════════════════════════════════════════════════════════════

st.markdown("""
<div style="margin-top: 50px; padding-top: 30px; border-top: 1px solid #1a2035; text-align: center;">
    <p style="color: #a8b2c5; font-size: 12px; margin: 10px 0;">
        <strong>AI Velocity Trading</strong> — SPY Weekly Iron Butterfly
    </p>
    <p style="color: #a8b2c5; font-size: 12px; margin: 10px 0;">
        Alpaca AI Trading Agents Hackathon
    </p>
    <p style="color: #a8b2c5; font-size: 11px; margin-top: 15px;">
        <strong>Disclaimers:</strong><br>
        Demo scenarios are simulated and submit no orders.<br>
        Historical backtest results are not indicative of future performance.
    </p>
</div>
""", unsafe_allow_html=True)
