#!/usr/bin/env python3
"""EITElite v0.7 primitive tests"""
import os, sys, time, tempfile, shutil
# sys.path already correct for tical_code.core
passed = failed = 0
def check(name, cond):
    global passed, failed
    if cond: print(f"  ✓ {name}"); passed += 1
    else: print(f"  ✗ {name}"); failed += 1

def run_tests():
    global passed, failed
    passed = failed = 0
    print("="*50 + "\nweb_sense\n" + "="*50)
    from tical_code.core.web_sense import web_fetch
    r1 = web_fetch("https://example.com"); check("HTTPS OK", r1.get("status")=="ok" and len(r1.get("content",""))>0)
    r2 = web_fetch("http://127.0.0.1"); check("SSRF blocked", r2.get("status") in ("forbidden","error"))
    r3 = web_fetch("not-a-url"); check("Invalid URL", r3.get("status") in ("error","forbidden"))
    r4 = web_fetch("https://news.sina.com.cn"); check("Chinese page", r4.get("status")=="ok" and len(r4.get("content",""))>100)
    r5 = web_fetch("https://www.baidu.com"); check("robots blocked", r5.get("status")=="blocked_by_robots")

    print("\n" + "="*50 + "\nmemory_sense\n" + "="*50)
    from tical_code.core.memory_sense import memory_search, memory_index
    tmpdir = tempfile.mkdtemp(prefix="tical_t_")
    with open(os.path.join(tmpdir,"ai.md"),"w") as f: f.write("Artificial Intelligence is a branch of computer science.\nMachine learning is a core AI technology.\nDeep learning uses neural networks.\n")
    with open(os.path.join(tmpdir,"phy.md"),"w") as f: f.write("Quantum mechanics is a foundation of physics.\nRelativity was proposed by Einstein.\n")
    c1 = memory_index(tmpdir); check("index dir", c1>=2)
    r = memory_search("Artificial Intelligence"); check("search AI", len(r)>0 and "Artificial" in r[0]["snippet"])
    r2 = memory_search("quantum mechanics"); check("search physics", len(r2)>0)
    r3 = memory_search("xyznonexist999"); check("no results", len(r3)==0)
    with open(os.path.join(tmpdir,"ai.md"),"w") as f: f.write("AI branches.\nLarge language models are the latest advances.\n")
    time.sleep(0.1); c2 = memory_index(tmpdir); check("incremental index", c2>=1)

    print("\n" + "="*50 + "\nstate_sense (SKIPPED - module removed)\n" + "="*50)
    print("  ⚠ state_sense module no longer exists in tical_code.core")

    print("\n" + "="*50 + "\nworker_loop integration (SKIPPED)\n" + "="*50)
    print("  ⚠ worker_loop module no longer available")

    shutil.rmtree(tmpdir, ignore_errors=True)
    print(f"\nv0.7 tests: {passed} passed, {failed} failed")
    return passed, failed

if __name__ == "__main__":
    p, f = run_tests()
    sys.exit(0 if f == 0 else 1)
