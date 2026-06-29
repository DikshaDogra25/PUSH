def get_product(result_dict, name):
    """name = 'a' or 'b'. Matches ProductA, product_a, PRODUCT_A, etc."""
    target = name.lower().replace("_", "")
    for k, v in result_dict.items():
        normalized = k.lower().replace("_", "")
        if normalized == f"product{target}":
            return v
    return []

result = test_result.get("view", {}).get("result", {}) or {}
product_a = get_product(result, "a")
product_b = get_product(result, "b")

print("product_a:", product_a)
print("product_b:", product_b)
