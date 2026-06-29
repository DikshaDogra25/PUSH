def extract_products(obj):
    """Return (product_a, product_b) from answer_with_tool output, whatever the wrapping."""

    def _find_view_products(o):
        # direct hit: a dict holding product_a / product_b (e.g. the 'view' dict)
        if isinstance(o, dict):
            if "product_a" in o or "product_b" in o:
                return (o.get("product_a", []), o.get("product_b", []))
            if "view" in o and isinstance(o["view"], dict):
                v = o["view"]
                if "product_a" in v or "product_b" in v:
                    return (v.get("product_a", []), v.get("product_b", []))
            # recurse into values
            for val in o.values():
                found = _find_view_products(val)
                if found:
                    return found
        elif isinstance(o, (list, tuple)):
            for item in o:
                found = _find_view_products(item)
                if found:
                    return found
        return None

    return _find_view_products(obj) or ([], [])

productList = self.answer_with_tool(
    state,
    tool_name="get_deal_schedule_data",
    args={"start_date": start_date, "end_date": end_date, "trans_type": "P", "source": "ALL"},
    format_text=format_output,
    start_date=start_date,
    end_date=end_date,
)

product_a, product_b = extract_products(productList)
print(f"product_a: {len(product_a)} items, product_b: {len(product_b)} items")

return {"product_a": product_a, "product_b": product_b}

    
