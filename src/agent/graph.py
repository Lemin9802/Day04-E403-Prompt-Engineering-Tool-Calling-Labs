from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool

from core.llm import build_chat_model, normalize_content
from core.schemas import (
    AgentResult,
    CalculateTotalsInput,
    DiscountInput,
    ListProductsInput,
    OrderLineInput,
    ProductDetailInput,
    SaveOrderInput,
    ToolCallRecord,
)
from utils.data_store import OrderDataStore

ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_DATA_DIR = ROOT_DIR / "data"
DEFAULT_OUTPUT_DIR = ROOT_DIR / "artifacts" / "orders"


def build_system_prompt(today: str | None = None) -> str:
    current_day = today or "2026-06-01"

    return f"""
You are OrderDesk, a careful order assistant for an electronics retailer.
Today is {current_day}.

You must understand Vietnamese and mixed Vietnamese-English order requests.

Your job:
- help customers create electronics orders
- use tools in the correct order
- ask for missing required information before any tool call
- refuse unsafe or policy-breaking requests
- save the final order as grounded JSON
- answer concisely in Vietnamese

Required customer/order information before using any tool:
1. customer full name
2. phone number
3. email
4. shipping address
5. at least one requested product
6. quantity for every requested product

If any required information is missing:
- do not call any tool
- ask one concise Vietnamese clarification question
- list only the missing fields
- stop

Refuse immediately without calling tools if the user asks to:
- create a fake invoice
- manually force or override a discount
- bypass stock validation
- ignore the catalog
- ignore policy
- invent product, price, stock, discount, total, or file path

For a valid order with all required information, use this exact workflow:
1. list_products
2. get_product_details
3. inspect stock from get_product_details
4. if stock is insufficient for any requested quantity, stop and explain in Vietnamese; do not call get_discount, calculate_order_totals, or save_order
5. get_discount
6. calculate_order_totals
7. if calculate_order_totals returns status "error", stop and explain the error; do not save
8. save_order
9. final answer in Vietnamese

Important rules:
- Use exact product_id values returned by tools.
- Use exact prices, stock, discount, totals, order_id, and save path from tool outputs.
- Never invent or estimate product facts.
- Do not save an order unless catalog validation, stock validation, discount, and total calculation all succeeded.
- For customer_tier, use "standard" unless the user explicitly says VIP.
- For get_discount seed_hint, prefer customer email; fallback to phone.
- In the final answer for a saved order, mention:
  - order_id
  - discount campaign/rate
  - final total
  - save path
- Keep the final answer short and grounded.
""".strip()


def build_tools(store: OrderDataStore):
    @tool(args_schema=ListProductsInput)
    def list_products(
        query: str | None = None,
        category: str | None = None,
        max_unit_price: int | None = None,
        required_tags: list[str] | None = None,
        in_stock_only: bool = True,
        limit: int = 8,
    ) -> str:
        """Search the local product catalog and return matching product summaries with product_id values."""
        payload = store.list_products(
            query=query,
            category=category,
            max_unit_price=max_unit_price,
            required_tags=required_tags or [],
            in_stock_only=in_stock_only,
            limit=limit,
        )
        return json.dumps(payload, ensure_ascii=False)

    @tool(args_schema=ProductDetailInput)
    def get_product_details(product_ids: list[str]) -> str:
        """Return exact product details, prices, stock, warranty, and detail_token for product IDs."""
        payload = store.get_product_details(product_ids)
        return json.dumps(payload, ensure_ascii=False)

    @tool(args_schema=DiscountInput)
    def get_discount(seed_hint: str, customer_tier: str = "standard") -> str:
        """Return deterministic campaign discount. Use customer email as seed_hint when available."""
        payload = store.get_discount(seed_hint=seed_hint, customer_tier=customer_tier)
        return json.dumps(payload, ensure_ascii=False)

    @tool(args_schema=CalculateTotalsInput)
    def calculate_order_totals(
        items: list[OrderLineInput],
        detail_token: str,
        discount_rate: float,
    ) -> str:
        """Validate stock/detail_token and calculate subtotal, discount amount, and final total."""
        normalized_items = _coerce_items(items)
        payload = store.calculate_order_totals(
            items=normalized_items,
            detail_token=detail_token,
            discount_rate=discount_rate,
        )
        return json.dumps(payload, ensure_ascii=False)

    @tool(args_schema=SaveOrderInput)
    def save_order(
        customer_name: str,
        customer_phone: str,
        customer_email: str,
        shipping_address: str,
        items: list[OrderLineInput],
        detail_token: str,
        discount_rate: float,
        campaign_code: str,
        customer_tier: str = "standard",
        notes: str = "",
    ) -> str:
        """Persist the final validated order to artifacts/orders and return saved_order plus path."""
        normalized_items = _coerce_items(items)
        payload = store.save_order(
            customer_name=customer_name,
            customer_phone=customer_phone,
            customer_email=customer_email,
            shipping_address=shipping_address,
            items=normalized_items,
            detail_token=detail_token,
            discount_rate=discount_rate,
            campaign_code=campaign_code,
            customer_tier=customer_tier,
            notes=notes,
        )
        return json.dumps(payload, ensure_ascii=False)

    return [
        list_products,
        get_product_details,
        get_discount,
        calculate_order_totals,
        save_order,
    ]


def build_agent(
    data_dir: Path | None = None,
    output_dir: Path | None = None,
    *,
    provider: str = "google",
    model_name: str | None = None,
    today: str | None = None,
):
    store = OrderDataStore(
        data_dir or DEFAULT_DATA_DIR,
        output_dir or DEFAULT_OUTPUT_DIR,
        today=today,
    )

    model = build_chat_model(
        provider=provider,
        model_name=model_name,
        temperature=0.0,
    )

    return create_agent(
        model=model,
        tools=build_tools(store),
        system_prompt=build_system_prompt(today or store.today),
    )


def run_agent(
    query: str,
    *,
    provider: str = "google",
    model_name: str | None = None,
    data_dir: Path | None = None,
    output_dir: Path | None = None,
    today: str | None = None,
) -> AgentResult:
    agent = build_agent(
        data_dir=data_dir,
        output_dir=output_dir,
        provider=provider,
        model_name=model_name,
        today=today,
    )

    response = agent.invoke({"messages": [{"role": "user", "content": query}]})
    messages = response["messages"] if isinstance(response, dict) else response

    tool_calls = extract_tool_calls(messages)
    saved_order, saved_order_path = extract_saved_order(tool_calls)

    return AgentResult(
        query=query,
        final_answer=extract_final_answer(messages),
        tool_calls=tool_calls,
        provider=provider,
        model_name=model_name,
        saved_order=saved_order,
        saved_order_path=saved_order_path,
    )


def extract_final_answer(messages) -> str:
    """Return the last non-empty AI answer that is not only tool calls."""
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            text = normalize_content(message.content)
            if text:
                return text
    return ""


def extract_tool_calls(messages) -> list[ToolCallRecord]:
    """Convert LangChain tool calls and tool results into grading trace records."""
    pending: dict[str, dict[str, Any]] = {}
    records: list[ToolCallRecord] = []

    for message in messages:
        if isinstance(message, AIMessage):
            for tool_call in getattr(message, "tool_calls", []) or []:
                tool_call_id = str(tool_call.get("id", ""))
                pending[tool_call_id] = {
                    "name": str(tool_call.get("name", "")),
                    "args": tool_call.get("args", {}) or {},
                }

        elif isinstance(message, ToolMessage):
            tool_call_id = str(getattr(message, "tool_call_id", ""))
            metadata = pending.pop(tool_call_id, {})

            records.append(
                ToolCallRecord(
                    name=str(getattr(message, "name", None) or metadata.get("name", "")),
                    args=metadata.get("args", {}),
                    output=normalize_content(message.content),
                )
            )

    for metadata in pending.values():
        records.append(
            ToolCallRecord(
                name=str(metadata.get("name", "")),
                args=metadata.get("args", {}),
                output="",
            )
        )

    return records


def extract_saved_order(tool_calls: list[ToolCallRecord]) -> tuple[dict | None, str | None]:
    """Parse the save_order output into saved_order and saved_order_path."""
    for record in reversed(tool_calls):
        if record.name != "save_order" or not record.output:
            continue

        try:
            payload = json.loads(record.output)
        except json.JSONDecodeError:
            continue

        if payload.get("status") != "saved":
            return None, None

        saved_order = payload.get("saved_order")
        saved_order_path = payload.get("path")

        if isinstance(saved_order, dict) and saved_order_path:
            return saved_order, str(saved_order_path)

    return None, None


def _coerce_items(items: Any) -> list[OrderLineInput]:
    """Normalize tool item inputs into OrderLineInput objects."""
    normalized: list[OrderLineInput] = []

    if not items:
        return normalized

    for item in items:
        if isinstance(item, OrderLineInput):
            normalized.append(item)
            continue

        if isinstance(item, dict):
            normalized.append(OrderLineInput(**item))
            continue

        if hasattr(item, "model_dump"):
            normalized.append(OrderLineInput(**item.model_dump()))
            continue

    return normalized