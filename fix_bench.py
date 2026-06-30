#!/usr/bin/env python3
"""
Fix bench audit issues in index.html
"""
import json, os, sys

HTML_PATH = os.environ.get("BENCH_HTML_PATH", "/home/YOUR_USER/benchmark/static/index.html")

with open(HTML_PATH, 'r') as f:
    content = f.read()

lines = content.split('\n')
print(f"Original: {len(lines)} lines")

# Fix 1: Duplicate style attribute (already fixed by sed, but verify)
old = 'style="display:none" class="timestamp" style="font-size:.6rem;color:#555;display:block;margin-top:2px;">'
new = 'style="display:none;font-size:.6rem;color:#555;display:block;margin-top:2px;" class="timestamp">'
if old in content:
    content = content.replace(old, new)
    print("Fix 4: Duplicate style attribute FIXED")
else:
    print("Fix 4: Duplicate style already fixed or not found")

lines = content.split('\n')

# Fix 2: DOM nesting - panel-health and panel-audit inside tab-mz
# Find key lines (1-indexed)
# tab-mz starts at line 159
# panel-health starts at line 165
# panel-audit starts at line 176
# .container closes at line 156
# scripts at lines 157-158
# </body> at line 184

# Find the tab-mz div
tab_mz_start = None
for i, line in enumerate(lines):
    if 'id="tab-mz"' in line:
        tab_mz_start = i
        break

if tab_mz_start is None:
    print("ERROR: Could not find tab-mz")
    sys.exit(1)

print(f"tab-mz found at line {tab_mz_start + 1}")

# Find panel-health and panel-audit inside the tab-mz block
health_start = None
audit_start = None
for i in range(tab_mz_start, len(lines)):
    if 'id="panel-health"' in lines[i] and health_start is None:
        health_start = i
    if 'id="panel-audit"' in lines[i] and audit_start is None:
        audit_start = i
    # Stop if we hit </body>
    if '</body>' in lines[i]:
        break

print(f"panel-health at line {health_start + 1}")
print(f"panel-audit at line {audit_start + 1}")

# Extract the parts
# Part 1: Everything up to and including the .history-section closing
# The .container closing </div> is right after history-section
# Find the </div> that closes .container (right before scripts)
container_close = None
for i in range(tab_mz_start - 1, 0, -1):
    stripped = lines[i].strip()
    if stripped == '</div>' and i < tab_mz_start:
        container_close = i
        break

print(f".container closing div at line {container_close + 1}")

# Extract panel-health block
health_block = []
depth = 0
for i in range(health_start, len(lines)):
    line = lines[i]
    depth += line.count('<div') - line.count('</div')
    health_block.append(line)
    if depth <= 0:
        break

# Extract panel-audit block
audit_block = []
depth = 0
for i in range(audit_start, len(lines)):
    line = lines[i]
    depth += line.count('<div') - line.count('</div')
    audit_block.append(line)
    if depth <= 0:
        break

# Build tab-mz without health/audit (just the model-zero content)
tab_mz_block = []
depth = 0
for i in range(tab_mz_start, len(lines)):
    line = lines[i]
    if i >= health_start:
        # We've reached the health panel - close tab-mz and stop
        tab_mz_block.append('  </div>')
        break
    tab_mz_block.append(line)
    depth += line.count('<div') - line.count('</div')

print(f"\ntab-mz block ({len(tab_mz_block)} lines):")
for l in tab_mz_block:
    print(f"  {l.rstrip()}")

print(f"\nhealth block ({len(health_block)} lines):")
for l in health_block:
    print(f"  {l.rstrip()}")

print(f"\naudit block ({len(audit_block)} lines):")
for l in audit_block:
    print(f"  {l.rstrip()}")

# Now rebuild the file:
# 1. Lines 0 to container_close-1 (everything before .container closing)
# 2. tab-mz block
# 3. health block  
# 4. audit block
# 5. </div> to close .container
# 6. <script src="app.js"></script>
# 7. </body></html>

new_lines = []
# Everything before .container closing
new_lines.extend(lines[0:container_close])
# Add a blank line
new_lines.append('')
# tab-mz
new_lines.extend(tab_mz_block)
new_lines.append('')
# panel-health
new_lines.extend(health_block)
new_lines.append('')
# panel-audit
new_lines.extend(audit_block)
new_lines.append('')
# Close .container
new_lines.append('</div>')
new_lines.append('')
# Scripts
new_lines.append('<script src="app.js"></script>')
new_lines.append('')
# Close body/html
new_lines.append('</body>')
new_lines.append('</html>')

result = '\n'.join(new_lines)

with open(HTML_PATH, 'w') as f:
    f.write(result)

print(f"\nFile rewritten: {len(lines)} -> {len(new_lines)} lines")

# Verify
with open(HTML_PATH, 'r') as f:
    verify = f.read()

checks = {
    "switchTab('buglog') present": "switchTab('buglog')" in verify,
    "No bare switchTab(buglog)": "switchTab(buglog)" not in verify,
    "No duplicate style": "style=\"display:none\" class=\"timestamp\" style=" not in verify,
    "panel-health outside tab-mz": verify.find('id="tab-mz"') < verify.find('id="panel-health"') and verify.find('id="panel-health"') < verify.find('</body>'),
    "panel-audit outside tab-mz": verify.find('id="tab-mz"') < verify.find('id="panel-audit"') and verify.find('id="panel-audit"') < verify.find('</body>'),
    "tab-mz before panel-health": verify.find('id="tab-mz"') < verify.find('id="panel-health"'),
    ".container closes after panels": verify.rfind('</div>') > verify.find('id="panel-audit"'),
    "app.js script present": '<script src="app.js"></script>' in verify,
    "HTML properly closed": verify.rstrip().endswith('</html>'),
}

print("\n=== Verification ===")
all_pass = True
for check, result in checks.items():
    status = "PASS" if result else "FAIL"
    if not result:
        all_pass = False
    print(f"  {status}: {check}")

if all_pass:
    print("\nAll checks passed!")
else:
    print("\nSome checks FAILED - review needed")
