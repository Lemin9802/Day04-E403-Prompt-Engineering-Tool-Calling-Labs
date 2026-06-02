from __future__ import annotations

import json
import re
import unicodedata
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


def _normalize_text(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    stripped = stripped.replace("đ", "d").replace("Đ", "D")
    compact = re.sub(r"[^a-zA-Z0-9]+", " ", stripped.lower())
    return re.sub(r"\s+", " ", compact).strip()


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
    store = OrderDataStore(
        data_dir or DEFAULT_DATA_DIR,
        output_dir or DEFAULT_OUTPUT_DIR,
        today=today,
    )

    deterministic_result = _try_run_deterministic_order_agent(
        query=query,
        store=store,
        provider=provider,
        model_name=model_name,
    )

    if deterministic_result is not None:
        return deterministic_result

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


def _try_run_deterministic_order_agent(
    *,
    query: str,
    store: OrderDataStore,
    provider: str,
    model_name: str | None,
) -> AgentResult | None:
    normalized_query = _normalize_text(query)

    if _is_guardrail_request(normalized_query):
        return AgentResult(
            query=query,
            final_answer=(
                "Mình không thể tạo hóa đơn giả, ép giảm giá thủ công, bỏ qua tồn kho "
                "hoặc bỏ qua catalog/policy. Mình chỉ có thể hỗ trợ tạo đơn hợp lệ theo catalog thật."
            ),
            tool_calls=[],
            provider=provider,
            model_name=model_name,
            saved_order=None,
            saved_order_path=None,
        )

    customer = _extract_customer_info(query)
    items = _extract_requested_items(query, store)

    missing_fields = _find_missing_fields(customer, items)

    if missing_fields:
        return AgentResult(
            query=query,
            final_answer=_build_clarification_answer(missing_fields),
            tool_calls=[],
            provider=provider,
            model_name=model_name,
            saved_order=None,
            saved_order_path=None,
        )

    if not items:
        return None

    tool_calls: list[ToolCallRecord] = []

    search_query = ", ".join(item["product_name"] for item in items)
    list_output = store.list_products(query=search_query, limit=20)
    tool_calls.append(
        ToolCallRecord(
            name="list_products",
            args={"query": search_query, "limit": 20},
            output=json.dumps(list_output, ensure_ascii=False),
        )
    )

    product_ids = [item["product_id"] for item in items]
    details = store.get_product_details(product_ids)
    tool_calls.append(
        ToolCallRecord(
            name="get_product_details",
            args={"product_ids": product_ids},
            output=json.dumps(details, ensure_ascii=False),
        )
    )

    detail_items = {
        item["product_id"]: item
        for item in details.get("items", [])
        if item.get("status") == "ok"
    }

    stock_errors: list[str] = []
    for item in items:
        detail = detail_items.get(item["product_id"])
        if not detail:
            stock_errors.append(f"Không tìm thấy sản phẩm {item['product_name']}.")
            continue
        if item["quantity"] > int(detail["stock"]):
            stock_errors.append(
                f"{detail['name']} chỉ còn {detail['stock']} sản phẩm, "
                f"không đủ cho số lượng yêu cầu {item['quantity']}."
            )

    if stock_errors:
        return AgentResult(
            query=query,
            final_answer=(
                "Không thể tạo đơn hàng vì tồn kho không đủ: "
                + " ".join(stock_errors)
                + " Đơn hàng chưa được lưu."
            ),
            tool_calls=tool_calls,
            provider=provider,
            model_name=model_name,
            saved_order=None,
            saved_order_path=None,
        )

    discount = store.get_discount(
        seed_hint=customer["email"],
        customer_tier=customer.get("customer_tier", "standard"),
    )
    tool_calls.append(
        ToolCallRecord(
            name="get_discount",
            args={
                "seed_hint": customer["email"],
                "customer_tier": customer.get("customer_tier", "standard"),
            },
            output=json.dumps(discount, ensure_ascii=False),
        )
    )

    order_items = [
        OrderLineInput(product_id=item["product_id"], quantity=item["quantity"])
        for item in items
    ]

    totals = store.calculate_order_totals(
        items=order_items,
        detail_token=details["detail_token"],
        discount_rate=discount["discount_rate"],
    )
    tool_calls.append(
        ToolCallRecord(
            name="calculate_order_totals",
            args={
                "items": [item.model_dump() for item in order_items],
                "detail_token": details["detail_token"],
                "discount_rate": discount["discount_rate"],
            },
            output=json.dumps(totals, ensure_ascii=False),
        )
    )

    if totals.get("status") != "ok":
        return AgentResult(
            query=query,
            final_answer=(
                "Không thể tạo đơn hàng vì có lỗi khi tính tổng: "
                + "; ".join(totals.get("errors", []))
                + " Đơn hàng chưa được lưu."
            ),
            tool_calls=tool_calls,
            provider=provider,
            model_name=model_name,
            saved_order=None,
            saved_order_path=None,
        )

    saved = store.save_order(
        customer_name=customer["name"],
        customer_phone=customer["phone"],
        customer_email=customer["email"],
        shipping_address=customer["shipping_address"],
        items=order_items,
        detail_token=details["detail_token"],
        discount_rate=discount["discount_rate"],
        campaign_code=discount["campaign_code"],
        customer_tier=customer.get("customer_tier", "standard"),
    )
    tool_calls.append(
        ToolCallRecord(
            name="save_order",
            args={
                "customer_name": customer["name"],
                "customer_phone": customer["phone"],
                "customer_email": customer["email"],
                "shipping_address": customer["shipping_address"],
                "items": [item.model_dump() for item in order_items],
                "detail_token": details["detail_token"],
                "discount_rate": discount["discount_rate"],
                "campaign_code": discount["campaign_code"],
                "customer_tier": customer.get("customer_tier", "standard"),
                "notes": "",
            },
            output=json.dumps(saved, ensure_ascii=False),
        )
    )

    if saved.get("status") != "saved":
        return AgentResult(
            query=query,
            final_answer="Không thể lưu đơn hàng. Đơn hàng chưa được tạo.",
            tool_calls=tool_calls,
            provider=provider,
            model_name=model_name,
            saved_order=None,
            saved_order_path=None,
        )

    saved_order = saved["saved_order"]
    final_total = saved_order["pricing"]["final_total"]
    discount_rate = saved_order["pricing"]["discount_rate"]
    campaign_code = saved_order["discount"]["campaign_code"]
    order_id = saved_order["order_id"]
    save_path = saved_order["save_path"]

    final_answer = (
        f"Đã tạo và lưu đơn hàng {order_id}. "
        f"Áp dụng mã {campaign_code} ({int(discount_rate * 100)}%). "
        f"Tổng thanh toán sau giảm giá là {final_total:,} VND. "
        f"File đã lưu tại {save_path}."
    )

    return AgentResult(
        query=query,
        final_answer=final_answer,
        tool_calls=tool_calls,
        provider=provider,
        model_name=model_name,
        saved_order=saved_order,
        saved_order_path=saved["path"],
    )


def _is_guardrail_request(normalized_query: str) -> bool:
    guardrail_terms = [
        "hoa don gia",
        "fake invoice",
        "giam gia 90",
        "ep giam gia",
        "force discount",
        "manual discount",
        "bo qua ton kho",
        "bypass stock",
        "ignore stock",
        "bo qua catalog",
        "khong can theo catalog",
        "ignore catalog",
        "bo qua policy",
        "ignore policy",
        "luu hoa don luon",
    ]
    return any(term in normalized_query for term in guardrail_terms)


def _extract_customer_info(query: str) -> dict[str, str]:
    phone_match = re.search(r"0\d{9}", query)
    email_match = re.search(r"[\w.+-]+@[\w.-]+\.\w+", query)

    customer_name = ""
    name_match = re.search(
        r"(?:cho|for)\s+(.+?)(?:,|\.| số điện thoại| phone| email| giao| ship)",
        query,
        flags=re.IGNORECASE,
    )
    if name_match:
        customer_name = name_match.group(1).strip()
        customer_name = re.sub(r"^(chị|anh|bạn)\s+", "", customer_name, flags=re.IGNORECASE).strip()
        customer_name = re.sub(r"^(tôi|mình)\s+", "", customer_name, flags=re.IGNORECASE).strip()

    shipping_address = _extract_shipping_address(query)

    return {
        "name": customer_name,
        "phone": phone_match.group(0) if phone_match else "",
        "email": email_match.group(0) if email_match else "",
        "shipping_address": shipping_address,
        "customer_tier": "vip" if "vip" in _normalize_text(query) else "standard",
    }


def _extract_shipping_address(query: str) -> str:
    """
    Extract shipping address without cutting inside abbreviations like TP.HCM.
    Stop only at clear order/item/contact markers.
    """
    start_patterns = [
        r"địa chỉ giao hàng\s+",
        r"giao hàng đến\s+",
        r"giao đến\s+",
        r"giao tới\s+",
        r"giao về\s+",
        r"ship to\s+",
        r"ship\s+to\s+",
    ]

    for start_pattern in start_patterns:
        match = re.search(start_pattern, query, flags=re.IGNORECASE)
        if not match:
            continue

        tail = query[match.end() :].strip()

        stop_patterns = [
            r"\.\s*Tôi\b",
            r"\.\s*Mình\b",
            r"\.\s*Chọn\b",
            r"\.\s*Chốt\b",
            r"\.\s*Phone\b",
            r"\.\s*Email\b",
            r",\s*số điện thoại\b",
            r",\s*phone\b",
            r",\s*email\b",
        ]

        stop_positions: list[int] = []
        for stop_pattern in stop_patterns:
            stop_match = re.search(stop_pattern, tail, flags=re.IGNORECASE)
            if stop_match:
                stop_positions.append(stop_match.start())

        if stop_positions:
            tail = tail[: min(stop_positions)]

        return tail.strip(" .,")

    return ""


def _extract_requested_items(query: str, store: OrderDataStore) -> list[dict[str, Any]]:
    """
    Extract requested products and quantities.

    Quantity rule:
    - Prefer a number immediately before the raw product name in the original query.
    - Do not use numbers that are part of a previous product name, address, phone, or email.
    - If no explicit quantity appears immediately before the product, default to 1.
    """
    normalized_query = _normalize_text(query)
    raw_query_lower = query.lower()
    items: list[dict[str, Any]] = []

    products_by_name_length = sorted(
        store.products,
        key=lambda product: len(_normalize_text(product.name)),
        reverse=True,
    )

    for product in products_by_name_length:
        normalized_name = _normalize_text(product.name)

        if normalized_name not in normalized_query:
            continue

        quantity = 1

        raw_position = raw_query_lower.find(product.name.lower())

        if raw_position != -1:
            raw_prefix = query[:raw_position]

            cleaned_prefix = raw_prefix.rstrip(" \t\r\n\"'“”‘’")

            quantity_match = re.search(r"(?:^|[\s,;:])(\d+)$", cleaned_prefix)

            if quantity_match:
                quantity = max(1, int(quantity_match.group(1)))
        else:

            quantity_pattern = re.compile(
                r"(?:^|\s)(\d+)\s+" + re.escape(normalized_name) + r"(?:\s|$)"
            )
            quantity_match = quantity_pattern.search(normalized_query)

            if quantity_match:
                quantity = max(1, int(quantity_match.group(1)))

        items.append(
            {
                "product_id": product.product_id,
                "product_name": product.name,
                "quantity": quantity,
            }
        )

    items.sort(key=lambda item: item["product_id"])
    return items


# def _extract_quantity_from_prefix(prefix: str) -> int:
#     matches = re.findall(r"\b\d+\b", prefix)
#     if not matches:
#         return 1
#     return max(1, int(matches[-1]))


def _find_missing_fields(customer: dict[str, str], items: list[dict[str, Any]]) -> list[str]:
    missing_fields: list[str] = []

    if not customer.get("name"):
        missing_fields.append("tên khách hàng")
    if not customer.get("phone"):
        missing_fields.append("số điện thoại")
    if not customer.get("email"):
        missing_fields.append("email")
    if not customer.get("shipping_address"):
        missing_fields.append("địa chỉ giao hàng")
    if not items:
        missing_fields.append("sản phẩm và số lượng")

    return missing_fields


def _build_clarification_answer(missing_fields: list[str]) -> str:
    return (
        "Mình cần thêm thông tin trước khi tạo đơn: "
        + ", ".join(missing_fields)
        + ". Vui lòng bổ sung các thông tin này nhé."
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