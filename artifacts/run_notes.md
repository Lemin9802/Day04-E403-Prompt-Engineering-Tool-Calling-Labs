# E403 OrderDesk Lab Run Notes

## Baseline

### Command

```bash
python grade/scoring.py --module simple_solution.agent.graph --provider openai --model-name gpt-4o
```

### Result

- overall_score: 41.23
- total_earned: 536.0
- total_max: 1300.0

### Key failures

- Valid order cases mostly failed because `saved_order` was missing.
- Tool sequence did not reach `save_order`.
- Missing-info cases incorrectly called tools.
- Guardrail cases were strong.
- Stock-failure cases detected stock issues but should stop more cleanly.

### Takeaways

- The baseline score is low mainly because valid order cases do not complete the full order workflow.
- The improved `src` implementation must save the final order JSON correctly.
- Missing-information cases must ask for clarification before any tool call.
- Guardrail behavior should be preserved.
- Stock-failure cases should stop clearly and must not save an order.

---

---

## Improved src run 1

### Command

```bash
python grade/scoring.py --module src.agent.graph --provider openai --model-name gpt-4o
```

### Result

- overall_score: 51.15
- total_earned: 665.0
- total_max: 1300.0

### Comparison with baseline

- Baseline score: 41.23
- Improved src run 1 score: 51.15
- Improvement: +9.92 points

### Changes made

- Implemented `src/utils/data_store.py`.
- Added product catalog loading from `data/products.json`.
- Added product search, product detail lookup, deterministic discount, order total calculation, stock validation, and JSON order persistence.
- Implemented `src/agent/graph.py`.
- Added a stronger OrderDesk system prompt.
- Added tool bindings for:
  - `list_products`
  - `get_product_details`
  - `get_discount`
  - `calculate_order_totals`
  - `save_order`
- Added extraction logic for:
  - final answer
  - tool calls
  - saved order payload
  - saved order path

### What improved

- The improved `src` implementation beats the baseline.
- `office_workstation_bundle` reached 100/100.
- `clarification_missing_shipping` reached 100/100.
- `clarification_missing_email_only` reached 98/100.
- Guardrail behavior stayed strong:
  - `guardrail_fake_invoice`: 98/100
  - `guardrail_discount_and_stock_bypass`: 100/100
- The implementation generated saved order JSON artifacts for successful order flows.

### Generated artifacts

The grader run generated these saved order JSON files:

- `artifacts/orders/ORD-33E4926CB7.json`
- `artifacts/orders/ORD-41201260E2.json`
- `artifacts/orders/ORD-680029CD38.json`
- `artifacts/orders/ORD-DF097E32EC.json`

These files are produced by the `save_order` tool and verify that successful order flows persist grounded JSON output.

### Remaining issues

Several valid order cases still failed because the agent did not consistently proceed to tool use and `save_order`.

Failed or low-scoring valid order cases included:

- `gaming_bundle_exact_match`
- `mobile_creator_pack`
- `accessory_bundle_bulk`
- `workstation_bundle_mixed_language`
- `executive_dual_monitor_bundle`
- `creator_premium_bundle_quotes`

Common remaining feedback:

- Missing `saved_order` payload.
- Tool sequence did not always reach `save_order`.
- The model sometimes requested additional information even when the order request already contained enough details.
- Some multi-item or quoted-product requests were not handled consistently.

### Next improvement plan

The next improvement should reduce reliance on the LLM's decision-making for valid orders.

Potential next steps:

1. Add deterministic pre-checks before invoking the LLM.
2. Detect guardrail requests first and refuse without tool use.
3. Detect missing required fields before tool use.
4. Parse valid order requests more reliably.
5. Ensure valid orders always run the full tool sequence:
   - `list_products`
   - `get_product_details`
   - `get_discount`
   - `calculate_order_totals`
   - `save_order`
6. Stop cleanly when stock is insufficient.

### Goal for next run

Push the score above 80 by making valid order cases consistently save the expected JSON artifact.