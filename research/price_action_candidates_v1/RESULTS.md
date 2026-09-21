# Three research scanners — August 2026 exploratory replay

Built and tested all three independently. None supports activation from this
screen. This is already-seen August data, not an untouched promotion judgment.

Base model: 5.9bps fee per fill plus 3bps adverse slippage per side. Funding is
not included. These are frozen assumptions, not verified exchange fills/rates.

| Candidate | Symbol | Setups | Modeled trades | Mean net bps/trade | Profit factor |
| --- | --- | ---: | ---: | ---: | ---: |
| Sweep → reclaim → confirmation | BTC | 159 | 151 | -21.45 | 0.112 |
| Sweep → reclaim → confirmation | ETH | 131 | 122 | -20.69 | 0.193 |
| Session VWAP bounce | BTC | 17 | 0 | N/A | N/A |
| Session VWAP bounce | ETH | 14 | 0 | N/A | N/A |
| Consolidation breakout | BTC | 189 | 160 | -19.31 | 0.208 |
| Consolidation breakout | ETH | 216 | 188 | -22.23 | 0.187 |

Sweep gross-only mean: BTC -4.26bps (150 trades), ETH -3.95bps (122).
Breakout gross-only mean: BTC -1.08bps (159), ETH -4.54bps (182).
Thus costs worsen these rules; they are not the sole explanation for losses.
Slippage shifts entry/stop/target levels and holding times, so trade counts
can differ across scenarios. Do not subtract two means as paired attribution.

VWAP: all 17 BTC / 14 ETH base entries fail the 1-ATR maximum distance from
decision VWAP at modeled next-open fill. Gross-only permits just two BTC and
one ETH trade: far too few to establish anything. Zero admitted trades is
insufficient evidence, not positive performance. No threshold was loosened.

## Evidence and limitations

- 7,440,458 BTC and 5,369,573 ETH archive rows processed.
- 8,928 calendar 5m bars each; 8,914 BTC / 8,898 ETH have all five minutes
  observed. Empty/partial bars are not filled.
- 8,874 BTC / 8,710 ETH bars have every minute observed from UTC midnight.
  Exact trade notional/base produces session VWAP; no close-volume proxy.
- Archive timezone and lot conversions are provisional. Observed minutes do
  not certify complete transport or unique trade identity.
- Costs omit funding and lack book/latency/queue reconstruction. No account
  sizing, exchange lot rounding, gateway or full paper-order replay was run.
- Report drawdown is cumulative unweighted trade-bps drawdown, not account
  drawdown. No leverage/PnL claim follows from it.
- Fixed UTC-block and side results are descriptive. None was selected to
  rescue the candidates. No automatic session or regime filter was added.

`attempt_01/report.json` contains all 18 symbol/candidate/cost results, exclusion
counts, side/session breakdowns and source/code/contract hashes. Per-scenario
JSONL files contain modeled entries, exits, stops, targets, fees-in-net and
exit reasons. Parquet feature frames support replay inspection.

## Authority

Research detector IDs only; not runtime registry entries. Existing BTC/ETH HTF
scanners, roster, gateway, capital allowlist and live gates are unchanged.
No paper or live orders were submitted. Nothing deployed.

Reproduce in a new output directory (the runner refuses to overwrite one):

```sh
.venv/bin/python -m vnedge.research.price_action_candidates \
  --downloads /Users/scorpion/Downloads \
  --contract research/price_action_candidates_v1/CONTRACT.md \
  --output research/price_action_candidates_v1/attempt_02
```

Any changed rule set requires a new version and a new frozen contract. These
results reject the tested variants as activation evidence, not every possible
sweep, VWAP or breakout strategy.
