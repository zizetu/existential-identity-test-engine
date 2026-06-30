# tical-code Full Edition Guide

## Overview

tical-code Full includes all core features plus a comprehensive plugin system for specialized capabilities.

## System Requirements

- **RAM**: 1GB minimum (2GB recommended)
- **CPU**: 2 cores
- **Storage**: 500MB
- **Python**: 3.9+

## Idle Memory Usage

~200MB (varies by loaded plugins)

## Included Features

### Core Components
All Lite features plus:
- Enhanced verification levels
- Plugin management
- Extended CLI

### Plugin System

#### Browser Plugin
Automated browser control via Playwright or Selenium.

```python
plugin = BrowserPlugin()
await plugin.open_url({'url': 'https://example.com'})
```

#### Web Search Plugin
Search and extract web content.

```python
plugin = WebSearchPlugin()
results = await plugin.search({'query': 'AI agents', 'num_results': 10})
```

#### Trading Plugin
Execute trades via Interactive Brokers Web API (REST + OAuth2.0).

The Trading Plugin uses IB's Web API instead of the legacy TWS API (ib_insync), making it suitable for web applications and server deployments without requiring a local Java TWS Gateway process.

**Two Authentication Modes:**

1. **Gateway Mode** (personal use) — Uses Client Portal Gateway on localhost
2. **OAuth Mode** (multi-user / third-party) — Uses OAuth2.0 private_key_jwt (RFC 7521/7523)

**Gateway Mode (Personal):**

```python
plugin = TradingPlugin()

# Connect via Client Portal Gateway (must be running and logged in)
result = await plugin.connect({
    'mode': 'gateway',
    'base_url': 'https://localhost:5000/v1/api',  # optional, default shown
})

# Place an order
result = await plugin.place_order({
    'symbol': 'AAPL',
    'action': 'BUY',
    'quantity': 100,
    'order_type': 'LIMIT',
    'limit_price': 165.00,
})
```

**OAuth Mode (Multi-user):**

```python
plugin = TradingPlugin()

# Connect via OAuth2.0
result = await plugin.connect({
    'mode': 'oauth',
    'client_id': 'YOUR_CLIENT_ID',
    'client_key_id': 'YOUR_KEY_ID',
    'private_key_path': '/path/to/private-key.pem',
    'credential': 'ibkr_username',  # optional
    'account_id': 'DU1234567',      # optional
})

# Get account info
result = await plugin.get_account({})

# Get positions
result = await plugin.get_positions({})

# Get market data
result = await plugin.get_market_data({'symbol': 'AAPL'})

# Search for a contract (obtain conid)
result = await plugin.search_contract({'symbol': 'MSFT'})

# Get order status
result = await plugin.get_order_status({'order_id': '12345'})

# Stream market data (WebSocket)
result = await plugin.get_market_data_stream({'symbol': 'AAPL'})

# Cancel an order
result = await plugin.cancel_order({'order_id': '12345'})

# Disconnect
result = await plugin.disconnect()
```

**Web API Authentication Configuration:**

| Mode | Base URL | Auth Method | Requirements |
|------|----------|-------------|--------------|
| Gateway | `https://localhost:5000/v1/api` | Browser login + 2FA | CP Gateway running locally |
| OAuth | `https://api.ibkr.com/v1/api` | OAuth2.0 private_key_jwt | RSA key pair, client_id from IBKR |

For Gateway mode:
1. Download and run the Client Portal Gateway from IBKR
2. Log in via browser at `https://localhost:5000`
3. Complete 2FA
4. The plugin connects to the authenticated Gateway session

For OAuth mode:
1. Register with IBKR to obtain `client_id` and `client_key_id`
2. Generate an RSA key pair; upload the public key to IBKR
3. The plugin generates a JWT `client_assertion` (RFC 7523) and obtains an `access_token`
4. A brokerage session is initialized via `/iserver/auth/ssodh/init`
5. All subsequent requests use the Bearer token

**IB Web API Session Architecture:**

IBKR Web API uses a two-tier session model:
1. **Read-only session** — Access to `/portfolio`, `/trsrv` endpoints
2. **Brokerage session** — Access to `/iserver` endpoints (trading, market data)

The Gateway automatically initializes the brokerage session. For OAuth mode, the plugin calls `/iserver/auth/ssodh/init` after obtaining the access token.

#### X/Twitter Plugin
Post and search tweets.

```python
plugin = XUrlPlugin()
await plugin.post({'text': 'Hello from tical-code!'})
```

#### Vision Plugin
Image analysis with AI models.

```python
plugin = VisionPlugin()
result = await plugin.analyze({
    'image_path': 'photo.jpg',
    'prompt': 'What is in this image?'
})
```

#### Messenger Plugin
Multi-platform messaging.

```python
plugin = MessengerPlugin()
await plugin.send_telegram({
    'chat_id': '123456',
    'message': 'Alert from tical-code!'
})
```

## Plugin Management

```bash
# List available plugins
tical plugin list

# Enable a plugin
tical plugin enable browser

# Disable a plugin
tical plugin disable browser
```

## Installation

```bash
pip install tical-code[full]
# or
pip install tical-code  # Full is default
```

## Trading Safety

**WARNING**: Trading involves real money. The Trading plugin includes:
- DUAL verification for all orders (parameter validation + balance check)
- Position tracking in SkeletonMemory
- Order verification against account balance
- Brokerage session validation before trading operations
- API response verification (read-back from IB Web API)

Always review orders before execution in live trading.

### Trading Plugin API Endpoints

The Trading Plugin uses the following IB Web API endpoints:

| Operation | Method | Endpoint |
|-----------|--------|----------|
| Auth Status | GET | `/iserver/auth/status` |
| Get Accounts | GET | `/iserver/accounts` |
| Account Info | GET | `/portfolio/accounts` |
| Account Ledger | GET | `/portfolio/{accountId}/ledger` |
| Get Positions | GET | `/portfolio/{accountId}/positions` |
| Place Order | POST | `/iserver/account/{accountId}/orders` |
| Cancel Order | DELETE | `/iserver/account/{accountId}/order/{orderId}` |
| Get Orders | GET | `/iserver/account/orders` |
| Market Snapshot | GET | `/iserver/marketdata/snapshot` |
| Contract Search | POST | `/iserver/secdef/search` |
| Brokerage Session Init | POST | `/iserver/auth/ssodh/init` |
| Session Keepalive | GET | `/tickle` |
| OAuth Token | POST | `/oauth2/token` |
| Market Data Stream | WebSocket | `/ws` |

### Dependencies

```bash
pip install httpx websockets   # Required for live trading
pip install cryptography        # Required for OAuth mode (RSA key handling)
```

## Browser Automation

### Playwright (Recommended)

```bash
pip install playwright
playwright install chromium
```

### Selenium (Alternative)

```bash
pip install selenium
# Also install Chrome/Firefox driver
```

## Performance Tips

1. Load only needed plugins: Not all plugins increase memory
2. Use headless mode: `BrowserPlugin` defaults to headless
3. Periodic cleanup: `tical memory skeletonize`
4. Plugin-specific memory limits: Each plugin has its own memory store
