# XAUUSD Automated Trading Terminal & Signal Engine

A Web Trading Terminal and Bot for **XAU/USD (Gold)** with **MetaAPI Cloud SDK** execution and **Telegram Notification Dispatches**.

---

## Technical Features

- **Automated Trading Engine:** Automatically opens market orders on MT4/MT5 when signal confidence reaches or exceeds target levels ($\ge 70\%$).
- **One-Click Execution Ticket:** Execute manual Buy/Sell orders with custom volume, Stop Loss, and Take Profit directly from the web interface.
- **Active Trade Manager:** Real-time view of open broker positions, live profit/loss updates, individual position closure, and panic "Close All Positions" control.
- **Telegram Dispatcher:** Sends formatted signal alerts and execution logs directly to your Telegram channel.

---

## Installation & Setup

### 1. Repository Setup

```bash
git clone [https://github.com/YOUR_USERNAME/xauusd-trading-terminal.git](https://github.com/YOUR_USERNAME/xauusd-trading-terminal.git)
cd xauusd-trading-terminal

python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
