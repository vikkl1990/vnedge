"""Read-only DNS and browser-trust checks. Never disable TLS verification."""
from __future__ import annotations

import argparse
import ipaddress
import json
import re
import socket
import ssl
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener


def validate_domain(domain: str) -> str:
    # No URL, port, wildcard, userinfo, local name or Caddy config expansion.
    domain = domain.lower()
    if (len(domain) > 253 or not re.fullmatch(r"[A-Za-z0-9.-]+", domain)
        or len(domain.split(".")) < 2 or domain.endswith((".local", ".localhost", ".internal"))
        or any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", p)
               for p in domain.split("."))):
        raise ValueError("public_dns_name_required")
    try:
        ipaddress.ip_address(domain)
    except ValueError:
        return domain.lower()
    raise ValueError("public_dns_name_required")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("unexpected_dashboard_redirect")


def preflight(domain: str, expected_ip: str, *, verify_https: bool = False) -> dict:
    domain = validate_domain(domain)
    expected = str(ipaddress.ip_address(expected_ip))
    addresses = sorted({str(ipaddress.ip_address(row[4][0]))
                        for row in socket.getaddrinfo(domain, 443, type=socket.SOCK_STREAM)})
    report = {"domain": domain, "resolved_addresses": addresses, "expected_ip": expected,
              "dns_matches": addresses == [expected], "https_verified": False,
              "can_trade": False, "changes_applied": False}
    # Mixed A/AAAA routing needs explicit resolution, not a lucky one-IP check.
    if verify_https and report["dns_matches"]:
        opener = build_opener(HTTPSHandler(context=ssl.create_default_context()), NoRedirect())
        with opener.open(Request(f"https://{domain}/healthz"), timeout=10) as response:
            report["https_verified"] = response.status == 200
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", required=True)
    parser.add_argument("--expected-ip", required=True)
    parser.add_argument("--verify-https", action="store_true")
    args = parser.parse_args()
    result = preflight(args.domain, args.expected_ip, verify_https=args.verify_https)
    print(json.dumps(result))
    return 0 if result["dns_matches"] and (not args.verify_https or result["https_verified"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
