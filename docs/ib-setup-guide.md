# IB Web API Trading Plugin — Setup Guide

> For individual users, connecting Interactive Brokers for trading from scratch.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Download and Install Client Portal Gateway](#2-download-and-install-client-portal-gateway)
3. [Start and Authenticate Gateway](#3-start-and-authenticate-gateway)
4. [Paper Trading Account Configuration](#4-paper-trading-account-configuration)
5. [tical-code Trading Plugin Configuration](#5-tical-code-trading-plugin-configuration)
6. [Connection Testing Steps](#6-connection-testing-steps)
7. [Troubleshooting](#7-troubleshooting)

---

## 1. Prerequisites

### Required

- **IBKR Account**: An active Interactive Brokers account with a minimum deposit ($500)
- **Java Runtime**: JRE 8u192 or higher
  - Check: Run `java -version` in terminal
  - If not installed: Download from [Adoptium](https://adoptium.net/)
- **Python 3.8+**: Required for tical-code
- **Browser**: Chrome / Firefox / Safari (for Gateway authentication login)

### Optional

- Paper Trading account (simulated account strongly recommended for testing)
- Market data subscription (enable via Client Portal > Settings > Market Data Subscriptions)

### Important Distinctions

| Term | Description | Port |
|------|-------------|------|
| **Client Portal Gateway** | Java gateway for Web API — this is what tical-code uses | 5000 |
| **IB Gateway** | Lightweight version of TWS API (socket protocol) — **not** what we need | 4001/4002 |
| **TWS** | Trader Workstation full desktop client | 7496/7497 |

> ⚠️ tical-code Trading Plugin uses the **Web API (REST)**, which requires **Client Portal Gateway**, not IB Gateway (the socket protocol one).

---

## 2. Download and Install Client Portal Gateway

### 2.1 Download

Official download page: **https://www.interactivebrokers.com/en/index.php?f=16457**

Or search "Client Portal API" on the IBKR Campus documentation site > Quickstart > API Gateway Download.

Select your platform:
- **Windows**: Download `.zip` file
- **macOS**: Download `.zip` file
- **Linux**: Download `.zip` file

### 2.2 Installation

Client Portal Gateway requires no installation — just extract:

```bash
# Extract to your tools directory
unzip clientportal.gw.zip -d ~/ibkr/
cd ~/ibkr/clientportal.gw

# View directory structure
ls bin/   # run.bat (Windows) / run.sh (Unix)
ls root/  # conf.yaml configuration file
```

### 2.3 Verify Java

```bash
java -version
# Should output something like: java version "1.8.0_351" or higher
```

---

## 3. Start and Authenticate Gateway

### 3.1 Start Gateway

```bash
# Enter Gateway directory
cd ~/ibkr/clientportal.gw

# Windows
bin\run.bat root\conf.yaml

# macOS / Linux
bin/run.sh root/conf.yaml
```

Keep this terminal window open! Closing it will stop the Gateway.

### 3.2 Browser Authentication

1. Open a browser and visit **https://localhost:5000**
2. The browser will show an "insecure connection" warning — this is normal (self-signed certificate), click "Continue"
3. Enter your IBKR username and password to log in
4. Complete 2FA verification (IB Key / SMS / Security Card)
5. Seeing **"Client login succeeds"** page = authentication successful

### 3.3 Verify Authentication Status

In another terminal window:

```bash
# Ignore SSL warning, check authentication status
curl -sk https://localhost:5000/v1/api/iserver/auth/status | python -m json.tool
```

Should show:
```json
{
  "authenticated": true,
  "connected": true,
  ...
}
```

If `authenticated` is `false`, you need to re-login through the browser.

### 3.4 About Session Validity

- Re-authentication is required after midnight each day (at least once)
- Gateway automatically maintains heartbeat, but does not auto-re-login
- tical-code's tickle mechanism sends heartbeat every 60 seconds to maintain connection

### 3.5 Change Gateway Port (Optional)

If port 5000 is occupied (macOS AirPlay Receiver often uses this port):

```bash
# Edit configuration file
vim ~/ibkr/clientportal.gw/root/conf.yaml

# Change listenPort to 5001
# listenPort: 5001

# Restart Gateway
```

If you change the port, update the `base_url` in tical-code's configuration accordingly.

---

## 4. Paper Trading Account Configuration

### 4.1 Get Paper Trading Account

1. Log in to IBKR Client Portal: https://www.interactivebrokers.com
2. Go to **Settings** > **Account Configuration** > **Paper Trading Account**
3. You can find:
   - **Paper Trading Username** (e.g., `DU1234567`)
   - **Paper Trading Account Number** (e.g., `DU1234567`)

### 4.2 Log into Gateway with Paper Account

- Start Gateway, then visit https://localhost:5000 in your browser
- Log in with your **Paper Trading Username**, not your Live account username
- Paper account password is the same as your Live account password

### 4.3 Confirm Paper Mode

```bash
curl -sk https://localhost:5000/v1/api/iserver/accounts | python -m json.tool
```

The returned account list should start with `DU` (Paper account), not `U` (Live account).

> 💡 **Best Practice**: Run through the entire workflow with Paper Trading first, then switch to Live account once everything is verified.

---

## 5. tical-code Trading Plugin Configuration

### 5.1 Install tical-code (Full Edition)

```bash
cd tical-code-v0.3
pip install -e ".[full]"
```

This will automatically install `httpx` and `websockets` dependencies.

### 5.2 Verify Installation

```bash
python -c "from tical_code.plugins.trading import TradingPlugin; print('OK')"
# Should output: OK

python -c "import httpx; import websockets; print('Dependencies OK')"
# Should output: Dependencies OK
```

### 5.3 Configuration File

The configuration file is located at `config/trading-config.json`. Default values generally do not need modification.

**Gateway Mode (default, personal use):**
```json
{
  "gateway": {
    "mode": "gateway",
    "base_url": "https://localhost:5000/v1/api",
    "timeout": 30,
    "verify_ssl": false,
    "tickle_interval": 60
  }
}
```

**Paper Trading Mode:**
```json
{
  "gateway_paper": {
    "mode": "gateway",
    "base_url": "https://localhost:5000/v1/api",
    "paper_trading": true
  }
}
```

### 5.4 Environment Variables (Optional)

You can override configuration via environment variables:

```bash
# Gateway URL (if port changed or remote deployment)
export IB_GATEWAY_URL="https://localhost:5000/v1/api"

# Paper Trading flag
export IB_PAPER_TRADING="true"
```

---

## 6. Connection Testing Steps

### 6.1 Quick Start Script (Recommended)

```bash
# Paper Trading mode
python scripts/start_trading.py --paper

# Live mode (make sure you're logged into Gateway with Live account)
python scripts/start_trading.py

# Demo mode (no Gateway needed, uses simulated data)
python scripts/start_trading.py --demo
```

### 6.2 Manual Testing

```python
import asyncio
from tical_code.plugins.trading import TradingPlugin

async def test():
    plugin = TradingPlugin()
    
    # 1. Connect
    result = await plugin.connect({"mode": "gateway"})
    print(f"Connected: {result.success}")
    print(f"Data: {result.data}")
    
    if not result.success:
        print(f"Error: {result.error}")
        return
    
    # 2. Query account
    account = await plugin.get_account({})
    print(f"Account: {account.data}")
    
    # 3. Query positions
    positions = await plugin.get_positions({})
    print(f"Positions: {positions.data}")
    
    # 4. Search contract
    contract = await plugin.search_contract({"symbol": "AAPL"})
    print(f"AAPL Contract: {contract.data}")
    
    # 5. Disconnect
    await plugin.disconnect()
    print("Disconnected")

asyncio.run(test())
```

### 6.3 Using the Example Script

```bash
# Run the complete trading example (Paper Trading)
python examples/trading_demo.py
```

### 6.4 Run Tests

```bash
cd tical-code-v0.3
pytest tests/test_trading.py -v
```

---

## 7. Troubleshooting

### Q: `Cannot connect to Client Portal Gateway`

**Cause**: Gateway not running or wrong port.

**Solution**:
1. Verify Gateway is running: `bin/run.sh root/conf.yaml`
2. Check port: inspect `listenPort` in `root/conf.yaml`
3. Test connectivity: `curl -sk https://localhost:5000/v1/api/tickle`
4. If port is occupied, modify `listenPort` and restart Gateway

### Q: `Gateway session not authenticated`

**Cause**: Haven't logged in via browser, or session has expired.

**Solution**:
1. Open https://localhost:5000 in your browser
2. Log in with IBKR username and password
3. Complete 2FA verification
4. See "Client login succeeds" then retry
5. Make sure you're not simultaneously logged in elsewhere (TWS, Client Portal web)

### Q: Browser shows "Insecure connection" / SSL certificate error

**Cause**: Client Portal Gateway uses a self-signed certificate — this is normal.

**Solution**:
- In browser, click "Advanced" > "Continue to localhost"
- tical-code's httpx client has `verify=False` set, unaffected
- Connection is local-only; Gateway-to-IBKR connection is encrypted, security is not compromised

### Q: macOS port 5000 occupied

**Cause**: macOS Monterey+ AirPlay Receiver uses port 5000.

**Solution** (choose one):
1. System Settings > General > AirDrop & Handoff > Turn off AirPlay Receiver
2. Change Gateway port: edit `root/conf.yaml`, change `listenPort` to `5001`

### Q: `authenticated` is still `false` after authentication

**Cause**: Possibly logged into the same account elsewhere simultaneously.

**Solution**:
1. Exit all TWS, Client Portal web sessions
2. Use the "Logout" button instead of closing windows
3. Wait 1-2 minutes, then re-login to Gateway
4. If you logged into Gateway with a Live account, Paper account may need a different username

### Q: Order error `Brokerage session not active`

**Cause**: Brokerage session needs to be activated after authentication before trading.

**Solution**:
1. Check authentication status: `/iserver/auth/status`, confirm `authenticated: true`
2. If `competing` is `true`, another session is conflicting — exit other clients
3. Try reauthentication: `GET /iserver/reauthenticate`

### Q: Market data empty / preflight

**Cause**: IBKR's first market data request is "pre-flight" and returns no actual data.

**Solution**:
1. Wait 1 second after the first request before requesting again
2. tical-code automatically marks `preflight: true`, just re-request
3. Confirm you have subscribed to market data for the relevant instrument

### Q: `httpx` or `websockets` import fails

**Solution**:
```bash
pip install -e ".[full]"
# Or install individually
pip install httpx websockets
```

### Q: Need to re-login via browser every day

**Cause**: IBKR security policy requires re-authentication after midnight each day.

**Solution**:
- This is normal behavior — IBKR does not allow automated login
- Consider writing a reminder script to prompt login before market open each day
- Once logged in, tical-code's tickle maintains the session until midnight

### Q: How to switch between Paper Trading and Live

- **Paper Trading**: Log into Gateway with Paper account username (starts with `DU`)
- **Live**: Log into Gateway with Live account username (starts with `U`)
- Both cannot be logged into the same Gateway instance simultaneously
- To switch, first log out of the current session in the browser, then log in with the other username

---

## Reference Links

- [IBKR Client Portal API Documentation](https://www.interactivebrokers.com/campus/ibkr-api-page/cpapi-v1/)
- [Client Portal Gateway Download](https://www.interactivebrokers.com/en/index.php?f=16457)
- [IBKR Campus Video Tutorials](https://ibkrcampus.eu/trading-lessons/launching-and-authenticating-the-gateway/)
- [Web API Endpoint Reference](https://www.interactivebrokers.com/campus/ibkr-api-page/webapi-ref/)
- [tical-code Trading Plugin Source](../tical_code/plugins/trading/__init__.py)
