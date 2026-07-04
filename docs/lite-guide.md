# EITElite Lite Edition Guide

## Overview

EITElite Lite is the minimal edition designed for resource-constrained environments like micro VPS instances (Oracle Micro, etc.).

## System Requirements

- **RAM**: 256MB minimum (512MB recommended)
- **CPU**: 1 core
- **Storage**: 100MB
- **Python**: 3.8+

## Idle Memory Usage

~50MB (verified on 1C1G Oracle Micro instance)

## Included Features

### Core Components
- Force-Verify System
- Bootstrap Anchor
- Memory Skeletonization
- SSH Worker Management

### CLI Commands
- `tical setup` - Initialize EITElite
- `tical config` - Manage configuration
- `tical worker` - Manage workers
- `tical anchor` - Manage anchors
- `tical memory` - Manage memory
- `tical detect` - System detection
- `tical status` - Show status

## What's NOT Included

The following are Full edition only:
- Browser automation (Playwright/Selenium)
- Web search and extraction
- Trading APIs (Interactive Brokers, Futu)
- X/Twitter posting
- Vision/image analysis
- Telegram/WeChat integration

## Installation

```bash
pip install EITElite-lite
```

Or via extras:
```bash
pip install EITElite[lite]
```

## Configuration

Lite config is stored at `~/.tical/config.json`:

```json
{
    "edition": "lite",
    "verify_level": "schema",
    "log_level": "INFO",
    "log_dir": "~/.tical/logs",
    "data_dir": "~/.tical/data",
    "ssh_timeout": 30,
    "execution_timeout": 300
}
```

## Typical Use Cases

1. **Micro VPS Management**: Manage agents on Oracle Micro, low-end VPS
2. **SSH-based Automation**: Remote command execution with verification
3. **Minimal Footprint**: When resources are limited
4. **Core Workflow Only**: Basic deploy-verify-monitor cycle

## Performance Tips

1. Use `--edition lite` explicitly for micro instances
2. Disable debug logging in production: `tical config set log_level WARNING`
3. Periodic memory cleanup: `tical memory cleanup`
4. Rotate error logs: Already automatic via rotation

## Upgrading to Full

If you need plugins later:

```bash
pip install EITElite[full]
tical setup --edition full
```
