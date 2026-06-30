# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.1.x   | ✅ Active development |
| < 0.1   | ❌ Not supported |

## Reporting a Vulnerability

If you discover a security vulnerability, please **do NOT open a public GitHub issue**.

Instead, report it privately by:

1. **Email**: Open a GitHub issue with the `security` label but mark it as confidential
2. **GitHub Security Advisory**: Use the "Report a vulnerability" button on the 
   repository's Security tab (if enabled)

We will acknowledge receipt within **48 hours** and provide an initial assessment 
within **5 business days**.

## What to Include

- Description of the vulnerability
- Steps to reproduce (proof of concept is ideal)
- Affected versions
- Potential impact
- Suggested fix (if available)

## Our Commitment

- We will respond promptly to all reports
- We will keep you informed of progress
- We will credit you (with your permission) when the fix is released
- We will not take legal action against good-faith security research

## Scope

This security policy covers:

- The tical-code / eite-agent core codebase
- Official deployments and configurations
- Authentication and authorization mechanisms
- Sandbox execution and permission systems

**Out of scope:**

- Third-party dependencies (report those to the respective maintainers)
- User configurations and custom deployments
- General LLM API security (follow your provider's security practices)

## AI-Specific Security Notes

This project executes LLM-generated code in a sandboxed environment. While 
multiple layers of protection are in place (RestrictedPython, permission 
checking, SSRF protection), no sandbox is perfect. We recommend:

- Running in an isolated environment (container or VM)
- Never granting `admin` or `bypassPermissions` mode in production
- Auditing tool execution logs regularly
- Keeping the project and all dependencies updated

## Commercial Support

Commercial license holders receive priority security response. 
See [COMMERCIAL-LICENSE.md](./COMMERCIAL-LICENSE.md) for details.
