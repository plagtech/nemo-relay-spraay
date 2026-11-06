# Gateway follow-ups

Gateway-side observations from this work. The gateway was read-only for this session, so nothing here was changed. Code targets the API as it behaved on 2026-11-06 (gateway 3.8.3).

## Needs a decision

1. **The `payments[]` body described in the session rulings doesn't match the live API.**
   - `POST /free/validate-batch` with `payments[]` returns `valid:false`, `Missing or empty "recipients" array`. It accepts `recipients: [{to, amount}]`.
   - `POST /free/estimate-batch` returns 404. It is GET-only, with query params `recipients`, `chain`, `amount`, `token`.
   - The plugin follows live behaviour. If a `payments[]` shape is deployed later, `SpraayGateway.validate` and `.estimate` are the only places to change.

## Worth fixing

2. **Estimate and execute use different units.** `/free/estimate-batch` reads `amount` as human units, while execute takes raw units. `amount=1000000` on USDC returns a $3,000 fee. The plugin sends human units here.
3. **Validate and execute use different recipient shapes.** Validate needs `[{to, amount}]`; execute's legacy object shape uses `[{address, amount}]` and its flat shape uses parallel arrays. Validate rejects both execute shapes.
4. **`sender` is optional in the execute source.** It is only used for gas estimation (`src/routes/batch-payments.ts:461`). The plugin requires it, as ruled.
5. **Execute accepts a zero amount.** The source only rejects negative amounts, so a zero-value recipient reaches calldata. The plugin's guardrail refuses it.
6. **`/api/v1/tokens` ignores `?chain=`.** `chain=peaq` returns the Base catalog, and it lists unichain and robinhood as `live` although execute rejects them. The plugin only uses it to look up decimals, and it checks those against the execute response.
7. **No free way to get peaq token decimals.** Because of item 6, a peaq symbol resolves through the Base catalog. It works for USDC (6 on both), and the response check catches any mismatch, but only after the access fee is paid.
8. **`validate-batch` allows chains execute doesn't.** Its list includes ethereum, solana, xrp and others, so `valid:true` doesn't mean a batch can be built. The plugin's real gate is `estimate.supported`.
9. **Solana atomicity claim.** BPA section 7 says all rails are atomic, but `spraay-solana-gateway` splits batches into several transactions (`MAX_INSTRUCTIONS_PER_TX_TOKEN: 7`). Out of scope for v1, but a reviewer could raise it.
10. **Stale Solana README.** It tells operators to configure a private key, while the code returns unsigned transactions and holds no key.
11. **`spraay.app` answers 200 HTML on every GET path.** A client pointed at it can mistake that for success. The plugin treats non-JSON as an error and says to check the URL.
12. **Unpaid POST to the Solana gateway returns 402 with an empty `{}` body**, so there is no challenge to read.

## Known and deferred

13. **Stale `transactions[]`** in the 402 `bazaar` example and the BPA spec. Execute returns `transaction` singular, confirmed live. Listed for completeness.
