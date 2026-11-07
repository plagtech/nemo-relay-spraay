# nemo-relay-spraay

A [NeMo Relay](https://github.com/NVIDIA/NeMo-Relay) gRPC worker plugin for multi-recipient
stablecoin payouts through the [Spraay](https://gateway.spraay.app) gateway.

**The plugin builds an unsigned batch transaction; the app signs and broadcasts it.** The worker
holds no key, depends on no signing library, and never moves batch funds.

It runs as a separate process, built on `nemo-relay-plugin`, so it isn't tied to the host's language.

## How it works

A host runs the managed tool `spraay_build_batch` and the worker handles it:

| Order | Surface | What it does |
|---|---|---|
| 1 | Conditional-execution guardrail | Refuses malformed batches before any network call: chain, recipient count, addresses, amounts. |
| 2 | Request intercept | Converts human amounts to raw base units, exactly once. |
| 3 | Sanitizers | Keep recipient addresses, the sender, and calldata out of emitted events. |
| 4 | Execution intercept | Runs the free checks, calls the gateway, verifies the response, and returns it. |
| 5 | Metadata injector and subscriber | Put human and raw amounts, protocol fee, and access-fee receipt on events, and emit a `spraay.batch.built` mark. |

Other tools pass through untouched.

## Costs

- **Access fee:** $0.02 per `batch/execute` call over x402, paid by your callback and capped by `max_access_fee_raw`.
- **Protocol fee:** 0.3% of the batch total, included in the transaction the app signs.
- **Gas:** paid by the app when it broadcasts.

## Install

Requires Relay 0.11 (`pip install "nemo-relay[cli]"`).

```bash
git clone https://github.com/plagtech/nemo-relay-spraay
nemo-relay plugins add ./nemo-relay-spraay/relay-plugin.toml
nemo-relay plugins enable plagtech.spraay_batch
```

Relay refuses unsigned plugins by default, and this one isn't signed yet. The manifest digest still
pins `worker.py`, so if you accept that level of assurance, opt in by adding this to `plugins.toml`:

```toml
[plugins.policy.defaults]
attestation = "integrity_only"
```

That setting is a host-wide default, so it applies to your other dynamic plugins too.

## Use

The host runs the tool through a managed wrapper. In Python:

```python
import nemo_relay
from nemo_relay import plugin, tools


def pay_access_fee(args):
    # Only called when the gateway asks for x402 payment. Your app owns the key.
    challenge = args["spraay_payment_challenge"]
    return nemo_relay.ToolExecutionResult({"headers": sign_x402_payment(challenge)}, None)


async with await plugin.initialize(plugin.PluginConfig(components=[])):
    outcome = await tools.execute(
        "spraay_build_batch",
        {
            "chain": "base",
            "token": "USDC",
            "sender": "0x000000000000000000000000000000000000dEaD",
            "payments": [
                {"to": "0x1111111111111111111111111111111111111111", "amount": "1.50"},
                {"to": "0x2222222222222222222222222222222222222222", "amount": "2.25"},
            ],
        },
        pay_access_fee,
    )

# Nothing has been signed. For ERC-20 tokens, the app first approves
# outcome.result["approval_required"], then signs and broadcasts outcome.result["transaction"].
```

Amounts are decimal strings in human units. Pass `decimals` for any token that isn't one of the
gateway's popular symbols.

### The payment callback

The tool's callback is the payment hook. A Python function can't cross a process boundary, but the
worker can call back into the host, so this works from any language and the key never leaves your app.

- The callback receives `{"spraay_payment_challenge": {"url", "accepts", "body"}}`, holding only options within the fee cap.
- Return `{"headers": {...}}` to pay. The worker retries once and never retries a rejected payment.
- Return anything else to decline. The tool then returns `{"status": "payment_required", "challenge": ...}`, with nothing spent.

## Where it runs

Tested over real gRPC with the Python SDK host on Relay 0.11.1, on Windows.

- **Not yet tested:** Node.js and Rust SDK hosts, and the CLI gateway.
- **Requires managed execution:** building a batch relies on an execution intercept, which needs a host
  that runs the tool through a managed wrapper (`tools.execute`, `toolCallExecute`, `tool_call_execute`).
  Hosts that only report tool activity through hooks, as coding-agent harnesses generally do, own the
  tool call themselves, so expect the guardrail and events to be the most that can apply there.
- **Relay 1.0:** the unreleased 1.0 line renames the protocol to `nvidia.nemo.relay.worker.v1`. This
  manifest declares `grpc-v1` because that is what published Relay accepts. It will need updating when 1.0 ships.

## Safety checks

- Floats and amounts with too many decimal places are refused rather than rounded.
- Free validation and estimate calls run before any paid call. `estimate.supported` decides whether a chain can be used.
- The response is checked before you get it: decimals, chain id, recipient count, totals, approval
  amount, and that the calldata encodes every requested recipient and amount.

## Configuration

Set under `[plugins.dynamic.config]` in `plugins.toml`.

| Field | Default | Meaning |
|---|---|---|
| `chains` | `["base", "peaq"]` | Enabled chains, a subset of the defaults. |
| `max_recipients` | `200` | Recipient limit, from 1 to 200. |
| `redact_recipients` | `true` | Remove addresses and calldata from emitted events. |
| `priority` | `10` | Middleware priority. |
| `max_access_fee_raw` | `20000` | Largest access fee, in raw units, offered to the callback. |
| `gateway_url` | `https://gateway.spraay.app` | Gateway base URL. `SPRAAY_GATEWAY_URL` is the fallback. |

## Development

```bash
uv sync
uv run pytest
```

Tests run offline and replay `docs/live-execute-proof.json`, a recorded response from one real paid
`batch/execute` call. That transaction was never signed or broadcast.

All logic stays in `nemo_relay_spraay/worker.py` because the manifest digest covers only that file.
If you change it, update `integrity.sha256` in `relay-plugin.toml`; a test fails until you do.

## License

Apache-2.0
