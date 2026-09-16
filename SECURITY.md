# Security Policy

## Scope & Project Nature

Sightline is an independent, open-source personal project designed for home surveillance and private NAS hosting. While security hardening (Google SSO, CSRF origin verification, rate limiting, and local network isolation) has been implemented in good faith, this software has **not** undergone commercial security audits or formal penetration testing.

Users deploy and run Sightline at their own risk under the terms of the [GNU Affero General Public License](LICENSE).

---

## Supported Versions

Only the latest release receives maintenance and security updates on a best-effort basis:

| Version | Supported |
| :--- | :--- |
| `v1.0.x` | :white_check_mark: Supported (Best effort) |
| `< 1.0`  | :x: No longer supported |

---

## Reporting a Vulnerability

If you discover a potential security vulnerability, please report it responsibly:

1. **Preferred Method**: Use GitHub's **[Private Vulnerability Reporting](https://github.com/micatlkw/sightline/security/advisories/new)** feature (accessible under the repository's **Security** tab &rarr; **Advisories** &rarr; **Report a vulnerability**).
2. **Do Not Open Public Issues for Vulnerabilities**: Please avoid opening public GitHub issues for unpatched vulnerabilities, exploit demonstrations, or sensitive security weaknesses.
3. **Response Expectations**: As an independent personal project maintained in spare time, triage and fixes are provided on a best-effort, non-commercial basis.

---

## Recommended Deployment Best Practices

To keep your home installation safe:

- **Never expose port `8000` directly to the Internet**: Port `8000` is intended strictly for internal Docker container communication or trusted local host automation.
- **Use Cloudflare Tunnel or VPN**: For remote access, rely on Cloudflare Tunnel (`cloudflared`) or WireGuard/Tailscale rather than opening port-forwarding rules on your home router.
- **Enable Google SSO**: When exposing the Web Dashboard through a domain, always configure `allowed_google_emails` in `settings.yaml`.
- **Run Non-Root**: Ensure `PUID` and `PGID` in `.env` match an unprivileged host user.
