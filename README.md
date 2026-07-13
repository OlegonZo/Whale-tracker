# Solana Whale Transaction Enricher

A Python utility that enriches Solana monitoring alerts with transaction context using standard Solana JSON-RPC.

## What it does

- Fetches and parses a confirmed Solana transaction
- Extracts token-account addresses, owners, mints, and pre/post balances
- Calculates exact balance deltas with `Decimal`
- Identifies likely senders and recipients
- Detects known Jupiter, Orca Whirlpool, and Meteora DLMM program IDs
- Heuristically labels transfers, swaps, and possible liquidity actions
- Produces structured dictionaries or Telegram-ready Russian alerts
- Adds direct Solscan links for the transaction and related accounts

## Requirements

- Python 3.9+
- `requests`
- A Solana RPC endpoint; public mainnet RPC is used by default

```powershell
$env:SOLANA_RPC_URL="https://your-rpc-endpoint"
pip install requests
```

## Example

```python
from spc_tx_enricher import enrich_spc_transaction, enrich_and_format

signature = "SOLANA_TRANSACTION_SIGNATURE"

data = enrich_spc_transaction(
    signature=signature,
    watched_address="OPTIONAL_WALLET_OR_TOKEN_ACCOUNT",
    token_mint="OPTIONAL_TOKEN_MINT",
)

print(data)
print(enrich_and_format(signature))
```

## Notes and limitations

Action classification is heuristic. Program presence and token-balance patterns are useful signals, but they are not a full instruction-level decoder. Production monitoring should use a reliable private RPC, retries, rate-limit handling, tests, and protocol-specific parsers.

No private RPC keys, wallet credentials, or monitored addresses are included in this public repository.
