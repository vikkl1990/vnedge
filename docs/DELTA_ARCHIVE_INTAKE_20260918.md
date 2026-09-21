# Delta archive intake — 18 September 2026

## Outcome

Read all 17 user-supplied ZIP archives in `/Users/scorpion/Downloads` without
extracting or modifying them. Every archive contains one CSV, with matching
product symbol and nondecreasing timestamps. Reading each member through EOF
also exercises the ZIP member CRC check. Total: 113,131,407 rows
(BTC 68,140,136; ETH 44,991,271).

This is an intake audit, not a certification of complete venue history.
No archive was copied into the live lake, no coverage flag was upgraded, and
no production process was restarted.

## Inventory

| Month | BTC rows | ETH rows | Calendar coverage observed |
|---|---:|---:|---|
| January | 7,205,234 | 7,097,521 | Every calendar date represented |
| February | 11,182,384 | 9,662,061 | Every calendar date represented |
| March | 10,901,866 | Not supplied | BTC March 20 absent |
| April | 8,441,215 | 6,524,494 | Every calendar date represented |
| May | 6,753,417 | 4,946,456 | May 11 absent in both |
| June | 9,479,187 | 6,373,103 | June 24 absent in both |
| July | 6,422,268 | 4,798,776 | July 1–26 only, both symbols |
| August | 7,440,458 | 5,369,573 | Every calendar date represented |
| September | 314,107 | 219,287 | September 1 only, both symbols |

Having rows on every date does not establish intraday completeness. BTC March
was read from the explicitly supplied `futures-trades-monthly-BTCUSD-2026-03.csv (1).zip`.
No unlisted ETH March file was assumed or substituted.

## September evidence

CSV fields: `product_symbol,price,size,timestamp,buyer_role`.
There is no trade ID, sequence number, timezone field or completeness manifest.
The displayed timestamps below are literal archive values; timezone needs
independent confirmation before normalization and cross-source comparison.

BTC first: `2026-09-01 00:00:00.163651`.
BTC last: `2026-09-01 23:59:59.839039`.
ETH first: `2026-09-01 00:00:00.209164`.
ETH last: `2026-09-01 23:59:54.592690`.

Both September files have zero null fields, positive integer-valued sizes,
only maker/taker buyer roles, and zero exact duplicate rows. These checks were
not extended to every numeric field of the earlier-month files. No general
permission to deduplicate identical ID-less trades is implied.

September compressed-file SHA-256:

- BTC: `723c5354002238a424c2943e1ce73e272bc7bad049ffc09b0478f2ffe58bcb51`
- ETH: `3afccb3000a9f923c3959493df620cfe1a794f4d14f13e8d5ef97d4deca4af7d`

## Repair decision

The previously captured scanner windows have unverified intervals on September
12–17. Neither supplied September archive contains any rows on those dates.
Therefore these files cannot repair those intervals. A monthly filename is
not evidence of coverage through the download date.

Next required evidence: an updated Delta India trade export covering the
affected September dates. Before an owner-mediated repair, verify timestamp
zone, contract units and buyer-role mapping, compare against verified overlap,
retain archive fingerprints and source provenance, and establish the coverage
claim. Do not fill missing dates with OHLC, carry-forward prices or synthetic
prints. Earlier months can be staged separately for exploratory research only
after the same schema/unit and intraday-gap checks.
