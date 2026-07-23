
import copy
import html
import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any

_MW_KEYS = [f"MW{i}" for i in range(1, 25)]  # MW19 absent -> 0; MW25 ignored

# --- injection anchors (all present in Amazon_new.html) ---
_TAGS_ASSIGN = re.compile(r"const\s+ORIGINAL_TAGS_DB\s*=\s*[^;]*;", re.S)
_ENGINE_DEF = re.compile(r"function\s+runAllocationEngine\s*\(\s*\)\s*\{")
_LOC_ASSIGN = re.compile(r"STATE\.locations\s*=\s*sortedLocations\s*;")
_ALLOC_KEYS = (
    "Location Agrregation & Allocation",  # exact key in the result
    "Location Aggregation & Allocation",  # correctly-spelled fallback
)
# NEW: the sale-deal product list result set returned by the purchase-deal proc
# (data[0]) and carried on the final result as "Sale_Deal_Product_List".
_SALE_PRODUCT_KEYS = (
    "Sale_Deal_Product_List",
    "SaleDealProductList",
    "Sale Deal Product List",
)
PRODUCT_A_CAP = 150.0

# Default dashboard template, expected alongside this module. Supplied separately
# (not checked in yet) — build_final_dashboard_html raises FileNotFoundError until
# it's dropped in place.
_TEMPLATE_PATH = Path(__file__).with_name("Amazon_Deal_Schedule_Planner.html")

_OVERRIDE_JS = r"""
const RESULT_ALLOCATION = __ALLOC_JSON__;
const FINAL_PLAN = [];
const PURCHASE_DEALS = [];
const KPI_TOTALS = {};
// NEW: distinct SaleDealProduct values from the SQL proc; drives the Product
// dropdown on SALE rows of the Final Deal Schedule Plan grid.
const SALE_DEAL_PRODUCTS = [];
function __resAllocToArr(series) {
  const a = Array(24).fill(0);
  ((series && series.data) || []).forEach(function (d) {
    const i = (d.HE | 0) - 1;
    if (i >= 0 && i < 24) a[i] = Number(d.values) || 0;
  });
  return a;
}

function renderFinalPlan() {
    if (typeof FINAL_PLAN === "undefined" || !FINAL_PLAN) return;
    const table = document.getElementById("schedules-tabular-grid");
    const tbody = table ? table.querySelector("tbody") : null;
    if (!tbody) return;
    tbody.innerHTML = FINAL_PLAN.map(function (g) {
      const span = g.rows.length || 1;
      return g.rows.map(function (r, i) {
        const tagCells = i === 0
          ? `<td rowspan="${span}">${g.schedule_number}</td>
             <td rowspan="${span}" style="color:var(--orange)">${g.tag_code}</td>
             <td rowspan="${span}">${g.path}</td>` : "";
        const badge = r.type === "PURCHASE"
          ? `<span class="badge purchase">PURCHASE</span>`
          : `<span class="badge sale">SALE</span>`;
        const vol = `<span style="color:${r.volume < 0 ? 'var(--orange)' : 'var(--green)'}">
${r.volume > 0 ? '+' : ''}${r.volume.toFixed(1)}</span>`;
        const statusCell = i === 0
          ? `<td rowspan="${span}">${g.status}</td>` : "";
        // NEW: sale rows render a Product dropdown; helper lives in the template.
        const prodCell = (typeof __saleProductCell === "function")
          ? __saleProductCell(r, g.schedule_number) : r.product;
        return `<tr>${tagCells}<td>${badge}</td><td>${r.deal_number}</td><td>${r.counterparty}</td>
<td>${prodCell}</td><td>${r.index}</td><td style="text-align:right">${vol}</td>${statusCell}</tr>`;
      }).join("");
    }).join("");
  }
function applyResultAllocationOverride() {
  if (typeof RESULT_ALLOCATION === "undefined" || !RESULT_ALLOCATION) return;
  const byCP = {};
  (RESULT_ALLOCATION.ProductA || []).forEach(function (x) {
    const cp = (x.CP || "").trim();
    const e = (byCP[cp] = byCP[cp] || {});
    e.A = __resAllocToArr(x);
    if (x.Source_Temp != null) e.Source_Temp = x.Source_Temp;
  });
  (RESULT_ALLOCATION.ProductB || []).forEach(function (x) {
    const cp = (x.CP || "").trim();
    const e = (byCP[cp] = byCP[cp] || {});
    e.B = __resAllocToArr(x);
    if (x.Source_Temp != null) e.Source_Temp = x.Source_Temp;
  });
  STATE.locations.forEach(function (loc) {
    const cp = byCP[loc.name];
    if (!cp) return;
    loc.Source_Temp = (cp && cp.Source_Temp) || loc.Source_Temp || loc.name;
    const A = cp.A || Array(24).fill(0), B = cp.B || Array(24).fill(0);
    // Show the result's A/B split VERBATIM (already allocated by your rule engine)
    loc.profileA = A.map(function (v) { return Math.round(v * 10) / 10; });
    loc.profileB = B.map(function (v) { return Math.round(v * 10) / 10; });
    loc.profileTotal = A.map(function (v, i) { return Math.round((v + B[i]) * 10) / 10; });
    // Table columns = per-hour AVERAGE over HE1-24
    loc.allocA = Math.round((A.reduce(function (s, v) { return s + v; }, 0) / 24) * 10) / 10;
    loc.allocB = Math.round((B.reduce(function (s, v) { return s + v; }, 0) / 24) * 10) / 10;
    loc.totalMW = Math.round(loc.profileTotal.reduce(function (s, v) { return s + v; }, 0) * 10) / 10;
  });
  STATE.hourlyProfileA = Array(24).fill(0);
  STATE.hourlyProfileB = Array(24).fill(0);
  STATE.locations.forEach(function (loc) {
    if (!loc.profileA) return;
    for (let h = 0; h < 24; h++) {
      STATE.hourlyProfileA[h] += loc.profileA[h];
      STATE.hourlyProfileB[h] += loc.profileB[h];
    }
  });
  for (let h = 0; h < 24; h++) {
    STATE.hourlyProfileA[h] = Math.round(STATE.hourlyProfileA[h] * 10) / 10;
    STATE.hourlyProfileB[h] = Math.round(STATE.hourlyProfileB[h] * 10) / 10;
  }
}
"""


def replace_nan(obj: Any, fill: Any = 0) -> Any:
    """Recursively replace NaN / Infinity with `fill` in a nested dict/list.

    Leaves str, int, bool, None untouched; handles numpy floats too."""
    if isinstance(obj, dict):
        return {k: replace_nan(v, fill) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [replace_nan(v, fill) for v in obj]
    if isinstance(obj, (str, bool, int)) or obj is None:
        return obj
    try:
        if math.isnan(obj) or math.isinf(obj):
            return fill
    except (TypeError, ValueError):
        pass
    return obj


def extract_result(payload: Any) -> dict:
    """Return the inner 3-key object. Accepts the A2A envelope, a raw JSON string,
    or an already-parsed dict. json.loads handles NaN natively; no re.sub on dicts."""
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8")
    if isinstance(payload, str):
        payload = json.loads(payload)
    try:
        text = payload["result"]["artifacts"][0]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        return payload  # already the inner object
    if isinstance(text, (bytes, bytearray)):
        text = text.decode("utf-8")
    if isinstance(text, str):
        text = json.loads(text)
    return text


def _s(v: Any) -> str:
    return v.strip() if isinstance(v, str) else ("" if v is None else str(v))


def _fmt_ts(raw: Any) -> str:
    if not raw or not isinstance(raw, str):
        return ""
    try:
        return datetime.fromisoformat(raw.strip()).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return raw.strip()


def _por_pod(market_path: Any) -> tuple[str, str]:
    parts = [p.strip() for p in _s(market_path).split(",") if p.strip()]
    if not parts:
        return "", ""
    return parts[0], parts[-1]


def _tag_hourly(tag: dict) -> list[float]:
    """MW1..MW24 -> list of 24 floats. NULL/None/'' -> 0.0"""
    out = []
    for h in range(1, 25):
        v = tag.get(f"MW{h}")
        try:
            out.append(0.0 if v is None or v == "" else float(v))
        except (TypeError, ValueError):
            out.append(0.0)
    return out


# --------------------------------------------------------------------------- #
#  Tag mapping: JSON "Unschedule Tags" -> Amazon_new.html tag objects
# --------------------------------------------------------------------------- #
def _map_tags(unschedule_tags) -> list[dict]:
    seen: dict[str, int] = {}
    out: list[dict] = []
    for t in unschedule_tags:
        code = _s(t.get("TagCode")) or f"TAG-{t.get('TagIndex', '')}"
        # de-duplicate id (two market-path legs can share a TagCode)
        if code in seen:
            seen[code] += 1
            uid = f"{code}#{seen[code]}"
        else:
            seen[code] = 1
            uid = code
        por, pod = _por_pod(t.get("MarketPath"))
        hrs = _tag_hourly(t)  # real 24h curve for charts
        out.append(
            {
                "id": uid,
                "schedule_num": f"P-{t.get('TagIndex', '')}",
                "tag_code": _s(t.get("TagCode")),
                "source": _s(t.get("GCA")),  # control area (RTO-ish)
                "sink": _s(t.get("Sink")),
                "por": por,
                "pod": pod,
                "cpse": _s(t.get("CPSE")),
                "location": _s(t.get("Source")),  # CP the allocation keys on
                "_curve": "baseload",  # payload has no curve -> flat
                "shape": "baseload",
                "mw": round(sum(hrs), 4),
                "hourly": hrs,
                "start": _fmt_ts(t.get("StartTime")),
                "end": _fmt_ts(t.get("StopTime")),
                "status": "Unscheduled",
                "product": _s(t.get("product")),
                "profileDate": _s(t.get("ProfileDate")),
            }
        )
    return out


# --------------------------------------------------------------------------- #
#  NEW: SaleDealProduct list -> Product dropdown on SALE rows
# --------------------------------------------------------------------------- #
def _sale_products(result: dict) -> list[str]:
    """Flatten the SaleDealProduct result set into an ordered, de-duplicated
    list of display strings.

    Accepts the shape the proc returns -- [{"SaleDealProduct": "ACS Energy"}, ...] --
    and is tolerant of plain strings or dicts using a differently named column
    (first non-empty value of the row is used). Empty / NULL rows are dropped.
    """
    raw = None
    for k in _SALE_PRODUCT_KEYS:
        v = (result or {}).get(k)
        if v:
            raw = v
            break
    if not raw:
        return []
    if isinstance(raw, dict):          # single row handed over un-wrapped
        raw = [raw]

    out: list[str] = []
    seen: set[str] = set()
    for row in raw:
        if isinstance(row, dict):
            val = row.get("SaleDealProduct")
            if val is None:            # fall back to the first non-empty column
                val = next((x for x in row.values() if _s(x)), "")
        else:
            val = row
        val = _s(val)
        if not val:
            continue
        key = val.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(val)
    return out


def _resolve_sale_product(current: Any, products: list[str]) -> str:
    """Pick the option that should be pre-selected for a SALE row.

    Order (as agreed):
      1. case-insensitive exact match on the row's own product
      2. case-insensitive prefix match either way ("ACS" <-> "ACS Energy")
      3. the FIRST value in the SaleDealProduct list
      4. the row's own product, when the list is empty
    """
    cur = _s(current)
    if not products:
        return cur
    cur_cf = cur.casefold()
    if cur_cf:
        for p in products:                                   # 1) exact
            if p.casefold() == cur_cf:
                return p
        for p in products:                                   # 2) prefix, either way
            p_cf = p.casefold()
            if p_cf.startswith(cur_cf) or cur_cf.startswith(p_cf):
                return p
    return products[0]                                       # 3) first value


def _apply_sale_product_selection(final_plan, products: list[str]):
    """Return a COPY of FINAL_PLAN where every SALE row carries
    "product_selected" -- the option the dropdown opens on.

    Resolution happens here (Python) so the HTML only has to render the
    <select>; no matching logic runs in the browser. PURCHASE rows are
    untouched and keep rendering as plain text.
    """
    plan = copy.deepcopy(final_plan or [])
    for group in plan:
        if not isinstance(group, dict):
            continue
        for row in group.get("rows") or []:
            if isinstance(row, dict) and row.get("type") == "SALE":
                row["product_selected"] = _resolve_sale_product(row.get("product"), products)
    return plan


def _get_allocation(result: dict):
    for k in _ALLOC_KEYS:
        v = (result or {}).get(k)
        if v:
            return v
    return None


def _inject_tags(template: str, tags_json: str, tags_src: list[dict]) -> str:
    new, n = _TAGS_ASSIGN.subn(lambda m: f"const ORIGINAL_TAGS_DB = {tags_json};", template, count=1)
    if n == 0:
        raise ValueError(
            "Could not find 'const ORIGINAL_TAGS_DB = ...;' in the template. "
            "Check that template_path points to the Amazon dashboard HTML."
        )

    # Trade date: ISO from the data's ProfileDate ("MM/DD/YYYY" -> "YYYY-MM-DD")
    profile_date = str(tags_src[0].get("ProfileDate") or "").strip()
    parts = profile_date.split("/")
    if len(parts) == 3:
        mm, dd, yyyy = parts
        iso = f"{yyyy}-{mm}-{dd}"
        new = re.sub(r'const TRADE_DATE = "[^"]*";', f'const TRADE_DATE = "{iso}";', new, count=1)

    return new


def _inject_allocation(template: str, allocation, result: dict, sale_products: list[str] | None = None) -> str:
    if not allocation:
        return template
    block = _OVERRIDE_JS.replace("__ALLOC_JSON__", json.dumps(allocation, allow_nan=False, ensure_ascii=False))
    template, n1 = _ENGINE_DEF.subn(lambda m: block + "\n  " + m.group(0), template, count=1)
    if n1 == 0:
        raise ValueError("Could not find runAllocationEngine() to attach the allocation override.")
    template, n2 = _LOC_ASSIGN.subn(lambda m: m.group(0) + "\n    applyResultAllocationOverride();", template, count=1)
    if n2 == 0:
        raise ValueError("Could not find 'STATE.locations = sortedLocations;' to call the override.")

    # NEW: stamp the pre-selected Product onto every SALE row before injecting,
    # so the dropdown opens on the right value with zero logic in the browser.
    sale_products = sale_products if sale_products is not None else _sale_products(result)
    final_plan = (result or {}).get("FINAL_PLAN") or []
    final_plan = _apply_sale_product_selection(final_plan, sale_products)
    final_json = json.dumps(final_plan, allow_nan=False, ensure_ascii=False)
    template = re.sub(r"const FINAL_PLAN = \[\];", f"const FINAL_PLAN = {final_json};", template, count=1)

    # NEW: the option list itself (distinct SaleDealProduct values).
    sp_json = json.dumps(sale_products, allow_nan=False, ensure_ascii=False)
    template, n = re.subn(r"const SALE_DEAL_PRODUCTS = \[\];", f"const SALE_DEAL_PRODUCTS = {sp_json};", template, count=1)
    if n != 1:
        raise RuntimeError("Anchor 'const SALE_DEAL_PRODUCTS = [];' not found in template")

    # --- raw purchase deals, passed through verbatim (no allocation, no clubbing) ---
    purchase_deals = (result or {}).get("Purchase Deals") or []
    pd_json = json.dumps(purchase_deals, allow_nan=False, ensure_ascii=False)
    template, n = re.subn(r"const PURCHASE_DEALS = \[\];", f"const PURCHASE_DEALS = {pd_json};", template, count=1)
    if n != 1:
        raise RuntimeError("Anchor 'const PURCHASE_DEALS = [];' not found in template")

    kpi = _kpi_totals((result or {}).get("Location Agrregation & Allocation"))
    kpi_json = json.dumps(kpi, allow_nan=False, ensure_ascii=False)
    template, n = re.subn(r"const KPI_TOTALS = \{\};", f"const KPI_TOTALS = {kpi_json};", template, count=1)
    if n != 1:
        raise RuntimeError("Anchor 'const KPI_TOTALS = {};' not found in template")
    return template


def _kpi_totals(product_list, cap: float = PRODUCT_A_CAP) -> dict:
    pl = product_list or {}

    def hourly_sum(entries):
        hrs = [0.0] * 24
        for e in entries or []:
            if not isinstance(e, dict):
                continue
            cp = (e.get("CP") or "").strip()
            if cp.lower() == "all":  # skip the aggregate row
                continue
            for pt in e.get("data") or []:
                try:
                    h = int(pt.get("HE")) - 1
                except (TypeError, ValueError):
                    continue
                if 0 <= h < 24:
                    try:
                        hrs[h] += float(pt.get("values") or 0.0)
                    except (TypeError, ValueError):
                        pass
        return hrs

    a_raw = hourly_sum(pl.get("ProductA"))
    b_raw = hourly_sum(pl.get("ProductB"))

    a_hourly, b_hourly = [], []
    for h in range(24):
        total = a_raw[h] + b_raw[h]
        a = min(a_raw[h], cap)  # A never exceeds the cap in any hour
        b = max(0.0, total - a)  # everything above the cap -> B
        a_hourly.append(round(a, 4))
        b_hourly.append(round(b, 4))

    return {
        "product_a_mw": round(sum(a_hourly) / 24.0, 1),  # average MW across HE1-24
        "product_b_mw": round(sum(b_hourly) / 24.0, 1),
        "product_a_hourly": a_hourly,  # for the KPI sparklines
        "product_b_hourly": b_hourly,
        "cap": cap,
    }


# --------------------------------------------------------------------------- #
#  Public API
# --------------------------------------------------------------------------- #
def build_final_dashboard_html(result: dict, template_path: str | Path | None = None) -> str:
    """Return the RAW dashboard HTML with your tags + your Location A/B injected.

    Open this directly in a browser or write it to a .html file."""
    tags_src = (result or {}).get("Unschedule Tags") or []
    if not tags_src:
        return ""
    tags_json = json.dumps(_map_tags(tags_src), allow_nan=False, ensure_ascii=False)
    template = Path(template_path or _TEMPLATE_PATH).read_text(encoding="utf-8")
    template = _inject_tags(template, tags_json, tags_src)
    # NEW: SaleDealProduct options, resolved once and reused for the per-row default.
    sale_products = _sale_products(result)
    template = _inject_allocation(template, _get_allocation(result), result, sale_products)
    return template


def convert_to_table(result: dict, template_path: str | Path | None = None) -> str:
    """Return the <iframe srcdoc> HTML string (embed into another page as MARKUP,
    e.g. innerHTML / dangerouslySetInnerHTML — never write it out as plain text)."""
    raw = build_final_dashboard_html(result, template_path)
    if not raw:
        return ""
    encoded = html.escape(raw, quote=True)
    iframe = f"<iframe width='100%' height='800' srcdoc=\"{encoded}\"></iframe>"
    return iframe.replace("\r\n", "")
