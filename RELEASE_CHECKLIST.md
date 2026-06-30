# Release Checklist

## Pre-Release Verification

### Code Quality
- [ ] All tests pass: `python3 -m pytest tests/ -v`
- [ ] No bare `except:`: `grep -rn 'except:' | grep -v 'except Exception' | grep -v 'except ('`
- [ ] No CJK characters in code: `grep -cP '[\x{4e00}-\x{9fff}]' *.py`
- [ ] All files pass py_compile: `find . -name '*.py' -exec python3 -m py_compile {} \;`
- [ ] Linting clean: `flake8 --max-line-length=100`

### Security & Privacy
- [ ] No hardcoded API keys, tokens, or secrets in source files
- [ ] No hardcoded personal paths (user home directories, VPS IPs)
- [ ] No hardcoded test credentials or personal domains
- [ ] `.env` files and `config/env/` listed in `.gitignore`
- [ ] No sensitive info in commit messages or git history
- [ ] `.git-credentials` and `.netrc` not present

### Legal & Licensing
- [ ] `LICENSE` file present (AGPLv3)
- [ ] `README.md` header shows copyright, license, and commercial restriction
- [ ] All source files have copyright + license header
- [ ] `COMMERCIAL-LICENSE.md` present (if dual-licensing)
- [ ] `NOTICE.md` lists all third-party dependencies
- [ ] `CONTRIBUTING.md` states contribution license terms
- [ ] `SECURITY.md` has vulnerability reporting instructions

### Version & Tags
- [ ] `VERSION` file updated to new version (semver)
- [ ] `pyproject.toml` version matches `VERSION` file
- [ ] `CHANGELOG.md` or git log updated with release notes
- [ ] Git tag created: `git tag v<version> && git push --tags`

### Documentation
- [ ] `README.md` Quickstart section works on fresh install
- [ ] `DISCLAIMER.md` present
- [ ] CLI help output matches documented commands
- [ ] Example config files are up to date

## Release Steps

1. Run pre-release checks above
2. Commit all changes to `main` branch
3. Update version in `VERSION` and `pyproject.toml`
4. Push to GitHub: `git push origin main --tags`
5. Verify GitHub Actions / CI passes (if configured)
6. Create GitHub Release with release notes
7. Announce on relevant channels (if public)

## Version Numbering

Follow [Semantic Versioning 2.0.0](https://semver.org/):

- **MAJOR**: Breaking API/architecture changes
- **MINOR**: New features, backward-compatible
- **PATCH**: Bug fixes, backward-compatible

Current version: 0.8.3
