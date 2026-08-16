# Security policy

## Supported posture

VNEDGE's supported production posture is public-data measurement plus
paper/shadow operation. Live trading is not deployed and Delta live is blocked
until a native private order/fill stream exists. No security report should be
interpreted as evidence that a strategy is profitable or capital-approved.

## Reporting a vulnerability

Please use the repository's private GitHub Security Advisory reporting flow.
Do not open a public issue containing credentials, account identifiers, private
trade history, dashboard tokens, infrastructure addresses, or exploit details.

Include the affected commit, component, reproduction steps using redacted or
synthetic data, impact, and any suggested mitigation. Maintainers should
acknowledge reports before discussing a disclosure timeline.

## Secret exposure response

If any API key, dashboard token, signing secret, or private certificate may
have been exposed:

1. revoke or rotate it at the provider immediately;
2. activate the kill switch and keep all execution reduce-only;
3. stop live-capable processes and verify venue orders/positions independently;
4. preserve logs for investigation without committing them;
5. inspect the complete Git history and deployment artifacts before resuming;
6. resume only after reconciliation and the fleet-policy verifier are clean.

Never rely on deleting a secret from the latest commit: Git history, CI logs,
container layers, forks, and caches may retain it.

## Deployment requirements

- Trade-only API keys; withdrawals disabled and IP allowlisting enabled.
- Secrets supplied through environment or a managed secret store, never Git.
- Authenticated dashboard behind TLS and an operator allowlist.
- Journal-before-submit, private fill truth, reconciliation, and kill switch
  remain mandatory for any future live deployment.
- Run `python -m vnedge.runtime.fleet_policy` after every deployment.
