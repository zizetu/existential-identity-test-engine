#!/bin/bash
# one-click deploy script (run on target node)

set -e

# 1. deploy.py
PROJECT_DIR="${PROJECT_DIR:-/home/YOUR_USER/project}"
if [ -f deploy.py ]; then
  cp deploy.py "$PROJECT_DIR/scripts/deploy.py" 2>/dev/null || cp deploy.py /root/project/scripts/deploy.py 2>/dev/null
fi

# 2. git config
cd "$PROJECT_DIR" 2>/dev/null || cd /root/project 2>/dev/null
git config user.name "Your Name" 2>/dev/null
git config user.email "your.email@example.com" 2>/dev/null
echo "git: done"

# 3. version file
mkdir -p worker
echo "v0.0.1" > worker/version.txt
echo "version: done"

# 4. verify
echo "=== verification ==="
echo "deploy.py: $(wc -l scripts/deploy.py 2>/dev/null || echo 'MISSING')"
echo "git user: $(git config user.name 2>/dev/null || echo 'MISSING')"
echo "version: $(cat worker/version.txt 2>/dev/null || echo 'MISSING')"
echo "done"
