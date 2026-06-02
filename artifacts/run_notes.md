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