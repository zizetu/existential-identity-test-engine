# tical-code Security Baseline Self-Checklist

> v0.5.6 Audit Lessons: All 3 P0 issues (shutdown reversed / f-string injection / SSL missing) were missed by "implementation mindset" testing
> Must run through this checklist before every commit. Don't assume "no errors = it's correct"

## Pre-Commit Mandatory Checks

### 1. Event Initial State
- [ ] asyncio.Event() defaults to clear — do NOT call set() during initialization
- [ ] Shutdown/stop events: MUST start as clear, only call set() in the corresponding shutdown() method
- [ ] Check method: search `_event = asyncio.Event()` followed by `.set()`

### 2. f-string Injection Risk
- [ ] Any f-string that concatenates external input into code/JSON/SQL must use json.dumps() for escaping
- [ ] Prohibit `f'execute(\"{user_input}\")'` patterns
- [ ] Correct pattern: `f'execute({json.dumps(user_input)})'`
- [ ] Check method: search f-string variable references that enter code execution paths

### 3. SSL Certificate Verification
- [ ] All HTTP requests must use verify=True
- [ ] Prohibit verify=False (including requests/httpx/urllib)
- [ ] Prefer certifi: `ssl.create_default_context(cafile=certifi.where())`
- [ ] Check method: search `verify=False`, `ssl._create_unverified_context`, `CERT_NONE`

### 4. Secrets Never in Logs
- [ ] API keys in to_dict()/__repr__/__str__ must be masked
- [ ] Masking format: key[:4] + '***' + key[-4:] (when length > 8)
- [ ] logger must never print full keys
- [ ] Check method: search `api_key` usage in to_dict/repr/str/log contexts

### 5. Shell Command Interception
- [ ] Intercept operators before shell_exec: `; && || |` backticks `$() &`
- [ ] Whitelist must check beyond the first token
- [ ] Check method: search `shell=True`, verify operator interception exists

## Testing Principles

- **Test before feature**: Write test cases synchronously with each module, not "when there's time"
- **Implementation mindset vs Testing mindset**: Implementation mindset verifies "does it run", testing mindset verifies "does it run correctly"
- **Security test cases**: Every security fix must have a corresponding test case to prevent regression

## Audit History

| Date | Version | Auditors | P0 Count | P1 Count | Key Findings |
|------|---------|----------|----------|----------|--------------|
| 2026-05-14 | v0.5.6 | DeepSeek+Grok+MiMo | 3 | 6 | Shutdown reversed / f-string injection / SSL missing |
