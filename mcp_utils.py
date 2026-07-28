
"""
Shared MCP utilities for the Dealschedule agent
================================================
Fetches purchase deals + unscheduled tags from ``get_purchase_deal_data``, rules
out ineligible tag sources, allocates the remainder across Product A / Product B
(150 MW/hour cap), resolves the matching sale deal per product via
``get_sale_deal_data``, builds the final purchase/sale schedule plan, and
renders the Amazon dashboard HTML.

The MCP tool calls themselves stay out of this module (agents own MCP access);
``fetch_sale_deal`` is injected so the pipeline here is pure and testable.
"""

import math
from collections import defaultdict
from collections.abc import Callable
from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from genie.observability.logging import get_logger

from genie_agent_dealschedule import build_dashboard, build_preview_dashboard
from genie_agent_dealschedule.final_deal_schedule_plan import DealSchedulePlan
from genie_agent_dealschedule.rule_engine import RuleEngine, RuleResult, RuleSet

logger = get_logger(__name__)

LIMIT = 150
SALE_COUNTERPARTY = "Amazon Energy LLC"

# ``fetch_sale_deal(start_date, end_date, trans_type, counterparty, product, trade_date) -> dict``
SaleDealFetcher = Callable[..., Any]


def parse_mcp_output(
    purchase_output: Any,
    fetch_sale_deal: SaleDealFetcher,
    scheduled_output: Any = None,
) -> dict:
    """Turn the ``get_purchase_deal_data`` output into the agent's full response.

    ``purchase_output`` is the raw MCP result: three datasets, ``[saleDealProduct,
    purchase_deals, unscheduled_tags]``. ``fetch_sale_deal`` calls
    ``get_sale_deal_data`` for one product/trade-date combination and returns the
    matching row (or ``{}``). ``scheduled_output`` is the raw result of
    ``get_scheduled_purchase_sale_deals`` (already-scheduled purchase AND sale
    deals, i.e. existing tags); its sale rows net down the calculated allocation
    (used vs available) and its purchase rows drive the Schedule Status column.
    """
    if not purchase_output:
        return {"result": {}, "message": "No Deal and Tag found."}

    if len(purchase_output) < 3:
        return {
            "result": {},
            "message": f"Invalid MCP response. Expected 3 datasets, got {len(purchase_output)}",
        }

    saleDealProduct = purchase_output[0]
    purchase_deals = purchase_output[1]
    tag_rows = purchase_output[2]

    # Already-scheduled purchase + sale deals (existing tags). Sale rows are used
    # to net the allocation; purchase rows drive the Schedule Status annotation.
    scheduled_deals = _extract_deal_rows(scheduled_output)
    used_sale_product = [r for r in scheduled_deals if _trans_type(r) == "Sale"]
    used_purchase = [r for r in scheduled_deals if _trans_type(r) == "Purchase"]

    # Diagnostic: shows why a scheduled tag might render empty (no deals extracted,
    # or the deal columns aren't what the linker expects). Logs to the app logger.
    _scheduled_tags = [t for t in tag_rows if _is_scheduled_tag_row(t)]
    logger.info("dealschedule RAW scheduled_output = %s", _describe_shape(scheduled_output))
    logger.info("dealschedule RAW purchase_output  = %s", _describe_shape(purchase_output))
    logger.info(
        "dealschedule scheduled_deals=%d used_purchase=%d used_sale=%d deal_cols=%s scheduled_tags=%d",
        len(scheduled_deals),
        len(used_purchase),
        len(used_sale_product),
        sorted(scheduled_deals[0].keys()) if scheduled_deals else [],
        len(_scheduled_tags),
    )

    rule_result = apply_rules(tag_rows)

    response_message = ""
    if not rule_result[0]:
        response_message = "Unscheduled tags found, but no matching purchase deals exist."

    aggregated_list = product_calculator(rule_result, source_temp=False, used_sale_product=used_sale_product)
    product_list = product_calculator(rule_result, source_temp=True, used_sale_product=used_sale_product)

    # New sale deals are built ONLY from available product (used is never consumed).
    sale_deals = fetch_sales_deal(fetch_sale_deal, product_list)

    # Annotate purchase deals with Schedule Status (Scheduled/Unscheduled).
    annotated_purchase = annotate_purchase_schedule_status(purchase_deals, used_purchase)

    final_plan = DealSchedulePlan.deal_schedule_plan(
        product_list, annotated_purchase, tag_rows, sale_deals, used_sale_product, used_purchase
    )

    # Purchase Deals tab: newly-generated purchase deals PLUS the already-scheduled
    # purchase deals (from get_scheduled_purchase_sale_deals), sorted by DealNumber.
    # The final plan above still uses `annotated_purchase` only, so scheduled deals
    # are not re-matched to unscheduled tags.
    scheduled_purchase_display = [{**d, "Schedule Status": "Scheduled"} for d in used_purchase]
    purchase_deals_display = sorted(
        annotated_purchase + scheduled_purchase_display, key=_deal_number_key
    )

    final_result = {
        "Unschedule Tags": tag_rows,
        "Purchase Deals": purchase_deals_display,
        "Location Agrregation & Allocation": aggregated_list,
        "FINAL_PLAN": final_plan,
        "Sale_Deal_Product_List": saleDealProduct,
        "Scheduled Deals": scheduled_deals,
    }
    final_result = build_dashboard.replace_nan(final_result, 0)

    preview_html = ""
    final_html = ""
    try:
        preview_html = build_preview_dashboard.build_preview_dashboard_html(final_result)
        final_html = build_dashboard.build_final_dashboard_html(final_result)
    except (OSError, ValueError, RuntimeError) as exc:
        # Dashboard HTML templates aren't deployed yet — skip rendering rather
        # than fail the whole response; the plan/data is still returned.
        logger.warning("Dashboard HTML build skipped: %s", exc)

    return {
        "result": final_result,
        "message": response_message,
        "previewHtml": preview_html,
        "finalHtml": final_html,
    }


def apply_rules(tag_rows: list[dict]) -> list[list[dict[str, Any]]]:
    """Rule out ineligible tag sources; return ``[kept, ruled_out, kept_detailed]``."""
    here = Path(__file__).parent
    ruleset = RuleSet.from_json(here / "ruleset.json")
    df = pd.DataFrame.from_records(tag_rows)
    result: RuleResult = RuleEngine(ruleset).apply(df)
    kept: list[dict[str, Any]] = result.kept.to_dict(orient="records")
    ruled_out: list[dict[str, Any]] = result.ruled_out.to_dict(orient="records")
    kept_detailed: list[dict[str, Any]] = result.kept_detailed.to_dict(orient="records")
    return [kept, ruled_out, kept_detailed]


def product_calculator(
    rule_result: list[list[dict]],
    source_temp: bool,
    used_sale_product: list[dict] | None = None,
) -> dict:
    """Aggregate kept tags (grouped or ungrouped) plus ruled-out tags under the
    "All"/"E" bucket, net out the MW already consumed by already-scheduled sale
    deals (``used_sale_product``), then split into Product A / Product B.

    Returns, each keyed ``{"ProductA": [...], "ProductB": [...]}``:
      * top-level ``ProductA``/``ProductB`` — the TOTAL (the 150-MW/hr cap split).
      * ``used`` — the MW consumed by already-scheduled sale deals, carved OUT OF
        the total's A/B split so it is always consistent with the total. The
        product it is drawn from follows the scheduled sale deal's IndexName leg
        (``_Product_B`` -> from Product B first, ``_Product_A`` -> from A first).
      * ``available`` — total minus used, per product per hour (clamped >= 0). New
        sale deals draw only from here.
    This keeps total = available + used per source/hour/product (so e.g. when the
    total for a source has 0 in Product A, its used never shows up in Product A).
    """
    aggregated = convert_model_data(rule_result[2] if source_temp else rule_result[0])
    ruled_out = rule_result[1]
    if ruled_out:
        for item in ruled_out:
            item["Source"] = "All"
            item["Product"] = "E"
            item["product"] = "E"
        aggregated.extend(convert_model_data(ruled_out))

    total = assign_product(aggregated)
    used_by_source, leg_by_source = _used_and_leg_by_source(used_sale_product or [])
    available, used = _carve_used_available(total, used_by_source, leg_by_source)
    return {
        "ProductA": total["ProductA"],
        "ProductB": total["ProductB"],
        "available": available,
        "used": used,
    }


def _used_and_leg_by_source(used_sale_product: list[dict]) -> tuple[dict, dict]:
    """From the already-scheduled SALE deals, build:
      * used_by_source[source][he]  -> MW consumed per source per hour
      * leg_by_source[source]       -> 'A' / 'B' from the deal's IndexName suffix
    """
    used_by_source: dict[str, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    leg_by_source: dict[str, str] = {}
    for deal in used_sale_product or []:
        if not isinstance(deal, dict):
            continue
        src = str(deal.get("Source", "") or "").strip().upper()
        idx = str(deal.get("IndexName", "") or "").strip().lower()
        leg = "B" if idx.endswith("_product_b") else ("A" if idx.endswith("_product_a") else "")
        if leg and src not in leg_by_source:
            leg_by_source[src] = leg
        for he in range(1, 25):
            value = deal.get(f"MW{he}")
            value = float(value) if value not in (None, "") else 0.0
            value = 0.0 if math.isnan(value) else value
            used_by_source[src][he] += value
    return used_by_source, leg_by_source


def _carve_used_available(total: dict, used_by_source: dict, leg_by_source: dict) -> tuple[dict, dict]:
    """Split each source's TOTAL Product A / Product B into used vs available.

    For each source and hour the used MW (from the scheduled sale deals) is carved
    out of the total, taking from the product that matches the sale deal's leg
    first (``_Product_B`` -> B first, overflow to A; ``_Product_A`` -> A first),
    then available = total - used. This guarantees used/available never place MW
    in a product where the total is 0.
    """
    a_list = total.get("ProductA") or []
    b_list = total.get("ProductB") or []
    available: dict[str, list[dict]] = {"ProductA": [], "ProductB": []}
    used: dict[str, list[dict]] = {"ProductA": [], "ProductB": []}

    for i in range(min(len(a_list), len(b_list))):
        ta, tb = a_list[i], b_list[i]
        temp = str(ta.get("Source_Temp", "") or "").strip()
        sources = [s.strip().upper() for s in temp.split(",") if s.strip()]
        if not sources:
            sources = [str(ta.get("CP", "") or "").strip().upper()]
        leg = next((leg_by_source[s] for s in sources if s in leg_by_source), "")

        def _blank() -> dict:
            return {
                "CP": ta.get("CP"),
                "Product": ta.get("Product"),
                "ProfileDate": ta.get("ProfileDate"),
                "Source_Temp": ta.get("Source_Temp", ""),
                "data": [],
            }

        ua, ub, aa, ab = _blank(), _blank(), _blank(), _blank()
        ta_data = {int(d["HE"]): float(d.get("values", 0.0) or 0.0) for d in ta.get("data") or []}
        tb_data = {int(d["HE"]): float(d.get("values", 0.0) or 0.0) for d in tb.get("data") or []}
        for he in range(1, 25):
            tot_a = ta_data.get(he, 0.0)
            tot_b = tb_data.get(he, 0.0)
            used_mw = sum(used_by_source.get(s, {}).get(he, 0.0) for s in sources)
            if leg == "B":
                u_b = min(used_mw, tot_b)
                u_a = min(max(0.0, used_mw - u_b), tot_a)
            else:  # 'A' or unknown -> Product A first
                u_a = min(used_mw, tot_a)
                u_b = min(max(0.0, used_mw - u_a), tot_b)
            ua["data"].append({"HE": he, "values": round(u_a, 4)})
            ub["data"].append({"HE": he, "values": round(u_b, 4)})
            aa["data"].append({"HE": he, "values": round(max(0.0, tot_a - u_a), 4)})
            ab["data"].append({"HE": he, "values": round(max(0.0, tot_b - u_b), 4)})
        used["ProductA"].append(ua)
        used["ProductB"].append(ub)
        available["ProductA"].append(aa)
        available["ProductB"].append(ab)
    return available, used


def _trans_type(row: Any) -> str:
    """Normalize a deal row's transaction type to 'Sale' / 'Purchase' / ''."""
    if not isinstance(row, dict):
        return ""
    for key in ("TransactionType", "TransType", "TransactionTypeName", "Trans Type", "DealType"):
        v = str(row.get(key, "") or "").strip().lower()
        if v.startswith("sale") or v == "s":
            return "Sale"
        if v.startswith("purchase") or v.startswith("buy") or v == "p":
            return "Purchase"
    return ""


def _describe_shape(obj: Any) -> str:
    """Compact description of an MCP tool output: how many result sets, each set's
    length, and the columns of its first row. Used only for diagnostics."""
    if isinstance(obj, dict):
        return f"dict(keys={list(obj.keys())})"
    if not isinstance(obj, list):
        return f"{type(obj).__name__}={obj!r}"
    parts = []
    for i, rs in enumerate(obj):
        if isinstance(rs, list):
            if rs and isinstance(rs[0], dict):
                parts.append(f"[{i}] list len={len(rs)} cols={sorted(rs[0].keys())}")
            else:
                parts.append(f"[{i}] list len={len(rs)} first={type(rs[0]).__name__ if rs else 'EMPTY'}")
        elif isinstance(rs, dict):
            parts.append(f"[{i}] dict cols={sorted(rs.keys())}")
        else:
            parts.append(f"[{i}] {type(rs).__name__}={rs!r}")
    return f"list len={len(obj)} :: " + " || ".join(parts) if parts else "list EMPTY"


def _deal_number_key(deal: Any) -> tuple:
    """Sort key for purchase deals by DealNumber (numeric when possible)."""
    dn = str((deal or {}).get("DealNumber", "") or "").strip() if isinstance(deal, dict) else ""
    try:
        return (0, int(dn))
    except (TypeError, ValueError):
        return (1, dn)


def _is_scheduled_tag_row(tag: Any) -> bool:
    """True when a raw tag row's IsScheduledTag column marks it already-scheduled."""
    if not isinstance(tag, dict):
        return False
    return str(tag.get("IsScheduledTag", "") or "").strip().lower() in ("yes", "y", "true", "1")


def _extract_deal_rows(scheduled_output: Any) -> list[dict]:
    """Pull the deal rows out of ``get_scheduled_purchase_sale_deals`` output.

    Tolerant of: a flat list of dicts, a list of result sets (like
    ``get_purchase_deal_data``), and a dict wrapper such as ``{"result": [...]}``
    that some MCP structured-output paths add around a list return.
    """
    if not scheduled_output:
        return []
    # Unwrap a dict wrapper (e.g. FastMCP structured output {"result": [...]}).
    if isinstance(scheduled_output, dict):
        for key in ("result", "deals", "data", "sale_deals"):
            v = scheduled_output.get(key)
            if isinstance(v, list) and v:
                scheduled_output = v
                break
        else:
            lists = [v for v in scheduled_output.values() if isinstance(v, list) and v]
            scheduled_output = lists[0] if len(lists) == 1 else []
    if not isinstance(scheduled_output, list) or not scheduled_output:
        return []
    if isinstance(scheduled_output[0], dict):
        return scheduled_output
    fallback: list[dict] = []
    for rs in scheduled_output:
        if not isinstance(rs, list) or not rs or not isinstance(rs[0], dict):
            continue
        if any(_trans_type(r) for r in rs) or any(isinstance(r, dict) and "DealNumber" in r for r in rs):
            return rs
        fallback = fallback or rs
    return fallback


def _deal_hour(deal: dict, he: int) -> float:
    """MW value for hour ``he`` from a deal dict; None/NaN/missing -> 0.0."""
    value = deal.get(f"MW{he}") if isinstance(deal, dict) else None
    try:
        value = float(value)
        return 0.0 if math.isnan(value) else value
    except (TypeError, ValueError):
        return 0.0


def annotate_purchase_schedule_status(
    purchase_deals: list[dict], used_purchase: list[dict]
) -> list[dict]:
    """Add a ``Schedule Status`` column to purchase deals by matching the
    already-scheduled purchase deals (``used_purchase``) on deal number.

    - deal number matches, MW1..MW24 identical            -> 1 row  'Scheduled'
    - deal number matches, available - used > 0 (any hour) -> 2 rows:
        used-MW row 'Scheduled', remaining-MW row 'Unscheduled'
    - no matching deal number                             -> 1 row  'Unscheduled'
    Always at most two rows per deal — never split per hour.
    """
    tol = 1e-6
    used_by_deal: dict[str, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    for deal in used_purchase or []:
        if not isinstance(deal, dict):
            continue
        dn = str(deal.get("DealNumber", "") or "").strip()
        if not dn:
            continue
        for he in range(1, 25):
            used_by_deal[dn][he] += _deal_hour(deal, he)

    out: list[dict] = []
    for deal in purchase_deals or []:
        if not isinstance(deal, dict):
            continue
        dn = str(deal.get("DealNumber", "") or "").strip()
        used = used_by_deal.get(dn)
        if not used:
            out.append({**deal, "Schedule Status": "Unscheduled"})
            continue

        avail = {he: _deal_hour(deal, he) for he in range(1, 25)}
        used_capped = {he: min(avail[he], used.get(he, 0.0)) for he in range(1, 25)}
        remaining = {he: round(max(0.0, avail[he] - used_capped[he]), 4) for he in range(1, 25)}

        if all(abs(avail[he] - used_capped[he]) <= tol for he in range(1, 25)):
            out.append({**deal, "Schedule Status": "Scheduled"})
        elif any(remaining[he] > tol for he in range(1, 25)):
            scheduled_row = {**deal, "Schedule Status": "Scheduled"}
            unscheduled_row = {**deal, "Schedule Status": "Unscheduled"}
            for he in range(1, 25):
                scheduled_row[f"MW{he}"] = round(used_capped[he], 4)
                unscheduled_row[f"MW{he}"] = remaining[he]
            out.append(scheduled_row)
            out.append(unscheduled_row)
        else:
            out.append({**deal, "Schedule Status": "Scheduled"})
    return out


def convert_model_data(tags: list[dict]) -> list[dict]:
    groups: dict[str, dict] = defaultdict(
        lambda: {
            "Product": "",
            "ProfileDate": "",
            "CP": "",
            "count": 0,
            "Source_Temp": [],  # actual sources aggregated into this group
        }
    )
    for tag in tags:
        source = tag.get("Source", "").strip()
        profile_date = tag.get("ProfileDate", "").strip()
        product = tag.get("product", "").strip() if tag.get("product", "All") else "All"
        source_temp = tag.get("Source_Temp", "").strip()
        key = source
        groups[key]["CP"] = source
        groups[key]["ProfileDate"] = profile_date
        groups[key]["Product"] = product
        groups[key]["count"] += 1
        if source_temp:
            groups[key]["Source_Temp"].append(source_temp)
        for he in range(1, 25):
            mw_key = f"MW{he}"
            value = tag.get(mw_key)
            value = float(value) if value not in (None, "") else 0.0
            value = 0.0 if math.isnan(value) else value
            groups[key][mw_key] = groups[key].get(mw_key, 0.0) + value

    result_aggregate = []
    for group in groups.values():
        group.pop("count")
        temps = list(dict.fromkeys(t.strip() for t in group.pop("Source_Temp") if t.strip()))
        aggregated = {
            "CP": group["CP"],
            "Product": group["Product"],
            "ProfileDate": group["ProfileDate"],
            "Source_Temp": ",".join(temps),
        }
        for he in range(1, 25):
            mw_key = f"MW{he}"
            aggregated[he] = round(group.get(mw_key, 0.0), 4)
        result_aggregate.append(aggregated)
    return result_aggregate


def assign_product(rows: list[dict]) -> dict:
    he_running_totals: dict[int, float] = defaultdict(float)
    result: dict[str, list[dict]] = {"ProductA": [], "ProductB": []}
    for row in rows:
        product_a = {
            "CP": row["CP"],
            "Product": row["Product"],
            "ProfileDate": row["ProfileDate"],
            "Source_Temp": row.get("Source_Temp", ""),
            "data": [],
        }
        product_b = {
            "CP": row["CP"],
            "Product": row["Product"],
            "ProfileDate": row["ProfileDate"],
            "Source_Temp": row.get("Source_Temp", ""),
            "data": [],
        }
        for he in range(1, 25):
            value = row.get(he, 0.0)
            current_total = he_running_totals[he]
            remaining_capacity = max(0.0, LIMIT - current_total)
            value_a = round(min(value, remaining_capacity), 4)
            value_b = round(max(0.0, value - value_a), 4)
            he_running_totals[he] += value
            product_a["data"].append({"HE": he, "values": value_a})
            product_b["data"].append({"HE": he, "values": value_b})
        result["ProductA"].append(product_a)
        result["ProductB"].append(product_b)
    return result


def fetch_sales_deal(fetch_sale_deal: SaleDealFetcher, product_list: dict) -> dict | str:
    """Resolve a sale deal template per Product-A entry, then stamp it into A/B rows.

    Falls back to today-minus-2-days for the trade date when nothing matches the
    tag's own profile date."""
    available = product_list.get("available", product_list)
    templates: dict[str, Any] = {}
    for item in available["ProductA"]:
        if item["Product"] in templates:
            continue
        sale_date = parse_date(item["ProfileDate"]) - timedelta(days=1)
        trade_date = parse_date(item["ProfileDate"]) - timedelta(days=2)
        result = fetch_sale_deal(
            start_date=sale_date.strftime("%m/%d/%Y"),
            end_date=sale_date.strftime("%m/%d/%Y"),
            trans_type="S",
            counterparty=SALE_COUNTERPARTY,
            product=item["Product"],
            trade_date=trade_date.strftime("%m/%d/%Y"),
        )
        if result:
            templates[item["Product"]] = result
        else:
            fallback_date = date.today() - timedelta(days=2)
            result = fetch_sale_deal(
                start_date=fallback_date.strftime("%m/%d/%Y"),
                end_date=fallback_date.strftime("%m/%d/%Y"),
                trans_type="S",
                counterparty=SALE_COUNTERPARTY,
                product=item["Product"],
                trade_date=trade_date.strftime("%m/%d/%Y"),
            )
            if result:
                templates[item["Product"]] = result

    if not templates:
        logger.info("Skipping build_sale_deals: no sale deal found")
        return ""

    return build_deals(product_list, templates)


def _apply_product_index(deal: dict, leg: str) -> dict:
    """The sale query returns one template shared by both A/B legs; stamp the
    correct ``_Product_A`` / ``_Product_B`` suffix onto ``IndexName`` per leg.

    Suffix-safe: strips any existing ``_Product_A`` / ``_Product_B`` before
    appending, so it's correct regardless of which leg the template came from.
    """
    if not isinstance(deal, dict):
        return deal
    idx = str(deal.get("IndexName", "") or "").strip()
    for suffix in ("_Product_A", "_Product_B"):
        if idx.endswith(suffix):
            idx = idx[: -len(suffix)]
            break
    deal["IndexName"] = f"{idx}_Product_{leg}"
    return deal


def build_deals(product_list: dict, templates: dict[str, Any]) -> dict:
    """Stamp each product's allocated MW1..MW25 onto its own copy of the matching
    sale deal template row (``get_sale_deal_data`` already returns deserialized
    SQL columns directly — no nested JSON payload to unwrap)."""
    stage: dict[str, dict] = {}
    available = product_list.get("available", product_list)
    product_a = available["ProductA"]
    product_b = available["ProductB"]
    for i in range(min(len(product_a), len(product_b))):
        item_a = product_a[i]
        item_b = product_b[i]
        sale_deal_a: dict | list = []
        sale_deal_b: dict | list = []
        template = templates.get(item_a["Product"])
        if isinstance(template, list):
            template = template[0] if template else None
        if not isinstance(template, dict):
            continue
        deal_a = deepcopy(template)
        deal_b = deepcopy(template)
        if isinstance(deal_a, dict) and deal_a and any(item["values"] > 0 for item in item_a["data"]):
            for data in item_a["data"]:
                he = int(data["HE"])  # 1..25
                if 1 <= he <= 25:
                    deal_a[f"MW{he}"] = data["values"]
            deal_a["Deal Date"] = (date.today() - timedelta(days=1)).strftime("%m/%d/%Y")
            deal_a["Trade Date"] = (date.today() - timedelta(days=2)).strftime("%m/%d/%Y")
            _apply_product_index(deal_a, "A")
            sale_deal_a = deal_a
        if isinstance(deal_b, dict) and deal_b and any(item["values"] > 0 for item in item_b["data"]):
            for data in item_b["data"]:
                he = int(data["HE"])  # 1..25
                if 1 <= he <= 25:
                    deal_b[f"MW{he}"] = data["values"]
            deal_b["Deal Date"] = (date.today() - timedelta(days=1)).strftime("%m/%d/%Y")
            deal_b["Trade Date"] = (date.today() - timedelta(days=2)).strftime("%m/%d/%Y")
            _apply_product_index(deal_b, "B")
            sale_deal_b = deal_b
        stage[f"{item_a['CP']} | {item_a['Product']}"] = {"SDeal_A": sale_deal_a, "SDeal_B": sale_deal_b}
    return {"sale_deals": stage}


def parse_date(s: str) -> date:
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Unrecognized date format: {s!r}")
