# nemo-relay-spraay

A [NeMo Relay](https://github.com/NVIDIA/NeMo-Relay) plugin and managed tool for
multi-recipient stablecoin payouts through the [Spraay](https://gateway.spraay.app) gateway.

**The plugin builds an unsigned batch transaction; the app signs and broadcasts it.** It holds no
key, depends on no signing library, and never moves batch funds.

## What you get

| Piece | Role |
|---|---|
| `build_batch_transaction()` | Managed tool (`spraay_build_batch`) run through `tools.execute()`. Up to 200 recipients in one transaction, on Base or peaq. |
| Guardrail | Refuses malformed batches before any network call: chain, recipient count, 0x addresses, positive raw amounts. |
| Sanitizers | Keep recipient addresses and calldata out of emitted events and exporters. Your callback still gets real values. |
| Metadata and subscriber | Put human and raw amounts, protocol fee, and access-fee receipt on events, and record each built batch in `plugin.built_batches`. |

Relay plugins register middleware, not tools, so the tool is a function your app calls and the
plugin guards and observes it.

## Costs

Be aware of these before enabling this in an agent:

- **Access fee:** $0.02 per `batch/execute` call over x402, paid by your hook. It is capped by `max_access_fee_raw`.
- **Protocol fee:** 0.3% of the batch total, included in the transaction the app signs.
- **Gas:** paid by the app when it broadcasts. `/free/estimate-batch` gives a rough figure at no cost.

## Quick start

```bash
uv sync
uv run python examples/demo.py   # live gateway, free calls only, no wallet needed
uv run pytest
```

```python
from nemo_relay import plugin
from nemo_relay_spraay import PLUGIN_KIND, SpraayGateway, SpraayPaymentPlugin, build_batch_transaction, component

implementation = SpraayPaymentPlugin()
plugin.register(PLUGIN_KIND, implementation)

async with await plugin.initialize(plugin.PluginConfig(components=[component()])):
    async with SpraayGateway(payment_hook=my_hook) as gateway:
        outcome = await build_batch_transaction(
            gateway,
            chain="base",
            token="USDC",
            sender="0x000000000000000000000000000000000000dEaD",
            payments=[
                {"to": "0x1111111111111111111111111111111111111111", "amount": "1.50"},
                {"to": "0x2222222222222222222222222222222222222222", "amount": "2.25"},
            ],
        )

# Nothing has been signed. For ERC-20 tokens, the app first approves
# outcome.result["approval_required"], then signs and broadcasts outcome.result["transaction"].
```

Set `SPRAAY_GATEWAY_URL` to override the default gateway, `https://gateway.spraay.app`.

## Paying the access fee

A payment hook receives a `PaymentChallenge` and returns the headers for one retry. Without a hook,
paid calls raise `PaymentRequiredError` carrying the challenge. A rejected payment is not retried.
`examples/x402_payment_hook.py` shows a hook built on the x402 SDK; that code and its key belong to
your application.

## Safety checks

- Amounts are decimal strings, converted to raw base units exactly once. Floats and amounts with too
  many decimal places are refused rather than rounded.
- Free validation and estimate calls run before any paid call. `estimate.supported` decides whether a
  chain can be used.
- The response is checked before you get it: decimals, chain id, recipient count, totals, approval
  amount, and that the calldata encodes every requested recipient and amount.

## Configuration

| Field | Default | Meaning |
|---|---|---|
| `chains` | `["base", "peaq"]` | Enabled chains, a subset of the defaults. |
| `max_recipients` | `200` | Recipient limit, from 1 to 200. |
| `redact_recipients` | `true` | Remove addresses and calldata from emitted events. |
| `priority` | `10` | Middleware priority. |

## Live proof

`docs/live-execute-proof.json` records one real paid `batch/execute` response, and the test suite
replays those bytes. See `scripts/live_execute_proof.py`. The returned transaction was never signed
or broadcast.

## License

Apache-2.0
