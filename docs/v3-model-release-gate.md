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
5. The canonical receipt has status `PROMOTED` and a valid OpenSSH signature
   from identity `alfred-model-promotion` in namespace
   `alfred-model-promotion-v1`.
6. For a model-generated decision, its captured runtime identity still equals
   the active promoted runtime. A server swap after generation therefore
   blocks the entry.

## Receipt shape

The file must be ASCII canonical JSON: sorted keys, compact separators, and one
trailing newline. Unknown or missing fields fail closed. Paths must be absolute,
canonical, owner/root-owned regular files with no symlink traversal and no
group/world write permission. The three noninferiority receipt files must have
no write bits at all.

```json
{"lineage":{"parent_bf16":{"artifact_id":"PARENT_BF16_ID","artifact_manifest_sha256":"<64 lowercase hex>","model_tree_sha256":"<64 lowercase hex>"},"stage":"v3"},"noninferiority":{"general_reasoning":{"candidate_artifact_id":"V3_SERVING_ID","candidate_artifact_manifest_sha256":"<64 lowercase hex>","decision":"PASS","frozen":true,"receipt_path":"/absolute/general.json","receipt_sha256":"<64 lowercase hex>"},"portfolio":{"candidate_artifact_id":"V3_SERVING_ID","candidate_artifact_manifest_sha256":"<64 lowercase hex>","decision":"PASS","frozen":true,"receipt_path":"/absolute/portfolio.json","receipt_sha256":"<64 lowercase hex>"},"trading":{"candidate_artifact_id":"V3_SERVING_ID","candidate_artifact_manifest_sha256":"<64 lowercase hex>","decision":"PASS","frozen":true,"receipt_path":"/absolute/trading.json","receipt_sha256":"<64 lowercase hex>"}},"promoted_at":"2026-07-11T00:00:00Z","promotion_id":"m3-v3-example","schema":"alfred-model-promotion.v1","serving_artifact":{"artifact_id":"V3_SERVING_ID","artifact_manifest_path":"/absolute/artifact.M3.v3.json","artifact_manifest_sha256":"<64 lowercase hex>","backend":"custom-python-mlx","binding_kind":"pipeline-artifact","model_realpath":"/absolute/models/m3-v3-quantized","quantization_receipt_path":"/absolute/quantization.json","quantization_receipt_sha256":"<64 lowercase hex>","readiness_smoke_sha256":"<64 lowercase hex>","runtime_contract_sha256":"<64 lowercase hex>","runtime_receipt_sha256":"<64 lowercase hex>"},"status":"PROMOTED"}
```

The runtime receipt is startup-specific. A custom-server restart intentionally
invalidates the old promotion receipt; capture the new `/health` identity and
repeat the controlled signing ceremony before re-enabling entries.

## Future activation

Keep the private signing key outside the repository and readable only by its
owner. The repository needs only the public allowed-signers file.

```sh
ssh-keygen -t ed25519 -N '' -f ~/.config/alfred/model-promotion-signing
printf 'alfred-model-promotion %s\n' "$(cat ~/.config/alfred/model-promotion-signing.pub)" \
  > /Users/alfredpennyworth/model-releases/trusted_allowed_signers
chmod 444 /Users/alfredpennyworth/model-releases/trusted_allowed_signers
chmod 444 /absolute/general.json /absolute/trading.json /absolute/portfolio.json
ssh-keygen -Y sign -f ~/.config/alfred/model-promotion-signing \
  -n alfred-model-promotion-v1 /Users/alfredpennyworth/model-releases/v3/promotion.json
chmod 444 /Users/alfredpennyworth/model-releases/v3/promotion.json \
  /Users/alfredpennyworth/model-releases/v3/promotion.json.sig
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
