# Contributing to EITE-agent

Thank you for considering contributing! We welcome contributions from the community.

## License Agreement

By submitting a pull request, you agree that:

1. Your contributions are **original work** (you own the copyright)
2. Your contributions are licensed under the **GNU Affero General Public License v3.0** 
   (the same license as the project)
3. You grant the project maintainer the right to also offer your contributions under 
   a **commercial license** (dual-licensing model)
4. You will not submit contributions that violate third-party rights

If you cannot accept these terms, please do not submit a pull request. Contact us 
if you need to contribute under different licensing arrangements.

## Getting Started

1. **Fork** the repository
2. **Clone** your fork: `git clone https://github.com/YOUR_USERNAME/eite-agent.git`
3. **Create a branch**: `git checkout -b feature/my-feature`
4. **Make your changes** following the coding guidelines below
5. **Test** your changes: `python3 -m pytest tests/`
6. **Submit a PR** against the `main` branch

## Coding Guidelines

- **English only** — all code, comments, docstrings, commit messages must be in English.
  Zero CJK characters in any `.py` file.
- **No bare `except:`** — always specify the exception type (`except Exception:`).
- **Environment variables** — all paths, credentials, and configuration must use 
  environment variables. Never hardcode personal paths or API keys.
- **Type hints** — use Python type annotations for all function signatures.
- **Compile check** — run `python3 -m py_compile <file>` before committing.
- **Tests** — new features must include tests. Run the full suite before submitting.

## Commit Messages

Use conventional commits format:

- `feat:` — new feature
- `fix:` — bug fix
- `docs:` — documentation
- `refactor:` — code restructuring
- `test:` — adding/updating tests
- `chore:` — maintenance tasks

Example: `feat: add provider auto-discovery from env vars`

## Code of Conduct

- Be respectful and constructive
- Focus on the code, not the person
- Assume good faith
- Help others learn

## Reporting Issues

- Use GitHub Issues for bug reports and feature requests
- Include steps to reproduce for bugs
- Include your environment (OS, Python version, pip freeze output)
- Check existing issues before filing a duplicate
