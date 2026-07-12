# V3 pre-trade model release gate

This gate is a final safety check for **new BUY entries only**. It does not run on
SELL-to-close, stop, take-profit, liquidation, reconciliation, or other
risk-reducing paths.

It is off by default. When enabled, every automated-slate, orchestrator, and
manual-entry path must prove all of the following immediately before
`placeOrder`:

1. The local custom-Python server is ready and reports backend
   `custom-python-mlx`.
2. Its artifact manifest, realized model path, runtime receipt, runtime
   contract, and readiness-smoke hashes exactly equal a canonical promotion
   receipt. The configured model-name string is not trusted.
3. The receipt has lineage stage `v3` and identifies the exact parent BF16
   artifact manifest and model-tree hashes.
4. The quantization receipt and three read-only noninferiority receipts
   (`general_reasoning`, `trading`, and `portfolio`) still exist at their signed
   paths and have their signed SHA-256 hashes. Every pillar must be a frozen
   `PASS` bound to the same serving artifact.
5. The canonical receipt has status `PROMOTED`, a seven-day-or-shorter validity
   window, and a valid OpenSSH signature
   from identity `alfred-model-promotion` in namespace
   `alfred-model-promotion-v1`.
6. Its sequence, digest, activation nonce, and expiry equal the head of a
   root-owned append-only activation ledger. A newer activation or revocation
   makes every older signed receipt non-replayable.
7. For a model-generated decision, a verified request receipt still equals the
   active promoted runtime. Manual entries require a short-lived HMAC proof
   bound to the exact approved order. Missing provenance always blocks.
8. Every entry repeats the complete proof under the broker mutation lock, with
   the fresh runtime check immediately adjacent to `placeOrder`. A server swap,
   revocation, expiry, or file change after approval therefore blocks entry.

## Receipt shape

The file must be ASCII canonical JSON: sorted keys, compact separators, and one
trailing newline. Unknown or missing fields fail closed. Paths must be absolute,
canonical, owner/root-owned regular files with no symlink traversal and no
group/world write permission. The three noninferiority receipt files must have
no write bits at all.

```json
{"activation_nonce":"<64 lowercase hex>","expires_at":"2026-07-18T00:00:00Z","lineage":{"parent_bf16":{"artifact_id":"PARENT_BF16_ID","artifact_manifest_sha256":"<64 lowercase hex>","model_tree_sha256":"<64 lowercase hex>"},"stage":"v3"},"noninferiority":{"general_reasoning":{"candidate_artifact_id":"V3_SERVING_ID","candidate_artifact_manifest_sha256":"<64 lowercase hex>","decision":"PASS","frozen":true,"receipt_path":"/absolute/general.json","receipt_sha256":"<64 lowercase hex>"},"portfolio":{"candidate_artifact_id":"V3_SERVING_ID","candidate_artifact_manifest_sha256":"<64 lowercase hex>","decision":"PASS","frozen":true,"receipt_path":"/absolute/portfolio.json","receipt_sha256":"<64 lowercase hex>"},"trading":{"candidate_artifact_id":"V3_SERVING_ID","candidate_artifact_manifest_sha256":"<64 lowercase hex>","decision":"PASS","frozen":true,"receipt_path":"/absolute/trading.json","receipt_sha256":"<64 lowercase hex>"}},"not_before":"2026-07-11T00:00:00Z","promoted_at":"2026-07-11T00:00:00Z","promotion_id":"m3-v3-example","release_sequence":1,"schema":"alfred-model-promotion.v1","serving_artifact":{"artifact_id":"V3_SERVING_ID","artifact_manifest_path":"/absolute/artifact.M3.v3.json","artifact_manifest_sha256":"<64 lowercase hex>","backend":"custom-python-mlx","binding_kind":"pipeline-artifact","model_realpath":"/absolute/models/m3-v3-quantized","quantization_receipt_path":"/absolute/quantization.json","quantization_receipt_sha256":"<64 lowercase hex>","readiness_smoke_sha256":"<64 lowercase hex>","runtime_contract_sha256":"<64 lowercase hex>","runtime_receipt_sha256":"<64 lowercase hex>","runtime_schema":"pipeline-m3-runtime.v1","startup_nonce":"STARTUP_NONCE"},"status":"PROMOTED"}
```

The runtime receipt is startup-specific. A custom-server restart intentionally
invalidates the old promotion receipt; capture the new `/health` identity and
repeat the controlled signing ceremony before re-enabling entries.

## Future activation

Keep the private signing key and manual-provenance key outside the repository.
The activation ledger must be a root-owned, non-writable directory containing
contiguous canonical records named
`<20-digit-sequence>-<activate|revoke>-<record-sha256>.json`. Never rewrite or
delete a ledger record; append a revocation to disable a release.

An activation record is canonical JSON with this exact shape; its filename
sequence/action must agree with the body, its filename digest must hash the
complete record, and `previous_record_sha256` must hash the preceding record
(`00…00` for sequence 1). A revocation uses the same shape with the next
sequence and `"action":"REVOKE"`. The root-controlled release pipeline—not the
trading service account—owns this append operation.

```json
{"action":"ACTIVATE","activation_nonce":"<signed receipt nonce>","expires_at":"2026-07-18T00:00:00Z","previous_record_sha256":"<previous record hash>","promotion_id":"m3-v3-example","promotion_receipt_sha256":"<signed receipt hash>","recorded_at":"2026-07-11T00:01:00Z","schema":"alfred-model-activation.v1","sequence":1}
```

```sh
ssh-keygen -t ed25519 -N '' -f ~/.config/alfred/model-promotion-signing
printf 'alfred-model-promotion %s\n' "$(cat ~/.config/alfred/model-promotion-signing.pub)" \
  > /etc/alfred/model-release/trusted_allowed_signers
openssl rand -hex 32 > /tmp/manual-provenance.key
install -o "$TRADING_SERVICE_USER" -g "$TRADING_SERVICE_GROUP" -m 600 \
  /tmp/manual-provenance.key /etc/alfred/model-release/manual-provenance.key
rm /tmp/manual-provenance.key
chmod 444 /etc/alfred/model-release/trusted_allowed_signers
chmod 444 /absolute/general.json /absolute/trading.json /absolute/portfolio.json
ssh-keygen -Y sign -f ~/.config/alfred/model-promotion-signing \
  -n alfred-model-promotion-v1 /var/lib/alfred/model-releases/v3/promotion.json
chmod 444 /var/lib/alfred/model-releases/v3/promotion.json \
  /var/lib/alfred/model-releases/v3/promotion.json.sig
```

Then set `trading.model_release_gate.enabled: true` and run the read-only
preflight (it does not import or connect to IBKR):

```sh
PYTHONPATH=. python ops/verify_v3_model_release.py --config config.yaml
```

Only after that returns compact promotion evidence should the entry service be
restarted and armed. Confirm a `model_release_gate_passed` event on the next
entry. If any receipt, signature, runtime identity, or path changes, new entries
fail closed while protective exits keep running.
