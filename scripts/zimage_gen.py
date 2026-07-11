#!/usr/bin/env python3
"""
Z-Image generation tool - Automatic memory switching management
Usage: python3 zimage_gen.py "your prompt" [output path]
"""
import sys, os, time, subprocess, shlex
from pathlib import Path

def log(m):
    print(f"[zimage] {m}")

def run(cmd, timeout=300):
    try:
        r = subprocess.run(shlex.split(cmd), capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return False, "", "[TIMEOUT]"
    except Exception as e:
        return False, "", str(e)

# Parse args
prompt = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "a cute cat"
output_path = sys.argv[2] if len(sys.argv) > 2 else "/tmp/zimage_output.png"

if not prompt:
    print("Usage: python3 zimage_gen.py 'your prompt'")
    sys.exit(1)

log(f"Prompt: {prompt}")
log(f"Output: {output_path}")

# Step 1: Stop ollama
log("Stopping ollama to free memory...")
run("sudo systemctl stop ollama 2>/dev/null; sleep 2")
log("ollama stopped")

# Step 2: Generate via temp script (avoid shell quoting issues)
gen_script = f'''#!/usr/bin/env python3
from diffusers import StableDiffusion3Pipeline
import torch, time as _t, sys

pipe = StableDiffusion3Pipeline.from_pretrained(
    "Tongyi-MAI/Z-Image-Turbo",
    torch_dtype=torch.float16
)
pipe.to("cpu")
_start = _t.time()
img = pipe(
    {repr(prompt)},
    num_inference_steps=4,
    guidance_scale=0.0
).images[0]
img.save("{output_path}")
_elapsed = _t.time() - _start
print(f"ok time={{_elapsed:.1f}}s")
'''

tmp_script = "/tmp/_zimage_gen.py"
with open(tmp_script, "w") as f:
    f.write(gen_script)

log("Loading Z-Image-Turbo and generating...")
t0 = time.time()
ok, out, err = run(f"python3 {tmp_script}", timeout=300)
t = time.time() - t0
os.remove(tmp_script)

if ok and os.path.exists(output_path):
    size = os.path.getsize(output_path)
    log(f"✅ Generation succeeded! ({t:.1f}s, {size/1024:.0f}KB)")
    log(f"   File: {output_path}")
else:
    log(f"❌ Generation failed ({t:.1f}s)")
    if out: log(f"  stdout: {out[-200:]}")
    if err: log(f"  stderr: {err[-200:]}")

# Step 3: Restart ollama
log("Restarting ollama...")
ok, out, err = run("sudo systemctl start ollama 2>/dev/null; sleep 3")
log("✅ ollama restored" if ok else "⚠️ ollama startup failed")
