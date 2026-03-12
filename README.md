# Options Ratio Screener (IBKR)

Screens real-time options chains from Interactive Brokers and ranks the best
**ratio bull-spread** combinations based on bid/ask prices.

## What is a ratio bull-spread?

You **buy** 1 call at strike K and **sell** N calls (N = 1, 2, 3 or 4) at a
higher strike K+Δ.
The screener evaluates every (K, K+Δ, N) combination and ranks them by how
efficiently the short premium funds the long leg.

## Prerequisites

1. **Interactive Brokers account** with market-data subscriptions for options.
2. **TWS or IB Gateway** running with API access enabled:
   - TWS: *Edit → Global Configuration → API → Settings → Enable ActiveX and Socket Clients*
   - Port defaults: TWS paper `7497`, TWS live `7496`, Gateway paper `4002`, Gateway live `4001`
3. **Python 3.10+**

## Installation

```bash
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and adjust the connection settings:

```bash
cp .env.example .env
```

## Usage

```bash
python screener.py SYMBOL YYYYMMDD [options]
```

### Examples

```bash
# Screen SPY calls expiring 2024-03-15 (default: top 20 results)
python screener.py SPY 20240315

# Screen AAPL with up to 1:4 ratio, gap ≤ 3 strikes, short must cover ≥ 50%
python screener.py AAPL 20240621 --ratio 4 --gap 3 --min-funding 50

# Restrict strikes to $450–$510
python screener.py SPY 20240315 --strike-low 450 --strike-high 510

# Use live TWS port
python screener.py QQQ 20240315 --port 7496

# Verbose output
python screener.py SPY 20240315 -v
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--host` | `127.0.0.1` | TWS/Gateway host |
| `--port` | `7497` | TWS/Gateway port |
| `--client-id` | `1` | API client ID |
| `--ratio` | `4` | Maximum short:long ratio (1–4) |
| `--gap` | `4` | Maximum strike gap steps between legs (1–4) |
| `--min-funding` | `0` | Min % of long ask covered by short premium |
| `--top` | `20` | Number of top results to display |
| `--strike-low` | — | Lower strike bound |
| `--strike-high` | — | Upper strike bound |
| `--no-otm-filter` | — | Disable automatic OTM-only filter |
| `-v` / `--verbose` | — | Enable debug logging |

## Output columns

| Column | Meaning |
|--------|---------|
| Long K | Strike of the long (bought) call |
| Short K | Strike of the short (sold) calls |
| Ratio | 1:N (e.g. 1:2 means sell 2, buy 1) |
| Gap | Strike steps between legs |
| Long ask | Ask price of the long call |
| Short bid | Bid price per short call |
| Net $/share | Net debit (positive) or credit (negative with − sign) |
| Funded % | `short_bid × N / long_ask × 100` |
| Lower BE | Breakeven on the upside (long_strike + net_debit) |
| Upper BE | Upper breakeven for ratio > 1:1 (where losses re-emerge) |
| Max profit $ | Maximum P&L per lot at short strike expiry |
| Score | Higher is better: `−(net_debit / spread_width)` |

## Scoring

```
score = −(net_debit / (short_strike − long_strike))
```

- **score > 0**: entered for a net credit (ideal)
- **score = 0**: zero-cost spread
- **score < 0**: still pays a net debit, but the ratio is favourable

Spreads are sorted descending by score; ties broken by max_profit_$.

## Risk Warning

Ratio spreads with N > 1 have **unlimited upside risk** beyond the short strike.
Always understand your risk before trading.
