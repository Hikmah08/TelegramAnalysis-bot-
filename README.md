# ICT / SMC Trading Analysis Bot

A modular, analysis-only system designed to scan financial markets for ICT (Inner Circle Trader) / Smart Money Concepts (SMC) trading setups.

> **Strict Safety Notice:** This software is for **analysis and educational purposes only**. It does not connect to brokerage accounts, execute trades, handle API trading credentials, or manage funds.

---

## Analytical Hierarchy

The strategy operates across 5 distinct timeframes without allowing lower timeframes to override higher-timeframe market context:

1. **4H (4-Hour)**: Directional bias (Market Trend & Order Flow).
2. **1H (1-Hour)**: Area of Interest (AOI / Key Levels).
3. **15M (15-Minute)**: Setup formation (Liquidity sweep & structural shift).
4. **5M (5-Minute)**: Confirmation (FVG identification & market structure).
5. **1M (1-Minute)**: Entry refinement (Precision retracement check).

---

## Getting Started

### 1. Prerequisites
- Python 3.10+ (tested on Python 3.13)
- Windows PowerShell, macOS terminal, or Linux bash

### 2. Environment Setup

Create and activate the Python virtual environment:

```powershell
# Windows
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Linux / macOS
python3 -m venv .venv
source .venv/bin/activate
```

Install minimal dependencies:
```bash
pip install -r requirements.txt
```

---

## Running the Multi-Timeframe Scanner

Execute the top-down 5-timeframe confluence scanner (4H Bias -> 1H AOI -> 15M Setup -> 5M Confirmation -> 1M Refinement):

```bash
python run_scanner.py
```

---

## Running Historical Backtesting

Execute the historical backtest engine with realistic frictions (fees, slippage, and position sizing) over real historical candles:

```bash
python run_backtest.py
```

---

## Running the Web Dashboard & Paper Trading

Launch the local FastAPI web dashboard displaying live 4H bias, active multi-timeframe setups, and simulated paper trading equity:

```bash
python run_dashboard.py
```

Then open your browser to `http://127.0.0.1:8000`.

---

## Running Automated Tests

Run the complete test suite using `pytest`:

```bash
pytest -v
```

