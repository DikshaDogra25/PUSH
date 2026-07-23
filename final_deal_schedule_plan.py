import math, random
from typing import Any

# CHANGED (review): named tolerances instead of magic numbers scattered in the code.
EPS = 1e-9          # "effectively zero" for allocation loops
BALANCE_TOL = 0.01  # tolerance when deciding PLANNED vs IMBALANCE


# CHANGED (review): dropped @dataclass — the class has no fields, only
# static/class methods, so the decorator was a no-op.
class Deal_Schedule_Plan:

    @staticmethod
    def _mw(d, he):
        """MW value for hour he from a deal/tag dict; None/NaN/missing -> 0.0."""
        if not isinstance(d, dict):
            return 0.0
        v = d.get(f"MW{he}")
        try:
            v = float(v)
            return 0.0 if math.isnan(v) else v
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _deal_mw_list(deal):
        """MW1..MW25 from a matched sale deal as a 25-length list.

        CHANGED (review, answer #2): was MW1..MW24 (24 entries) while the tag
        allocation arrays run HE1..HE25 (25 entries), so deal-sourced sale rows
        had a shorter `mw` list and their volume silently dropped HE25.
        Missing / None / NaN MW25 (or any hour) still resolves to 0.0 via _mw.
        """
        return [Deal_Schedule_Plan._mw(deal, he) for he in range(1, 26)]

    @staticmethod
    def _clean(s):
        return str(s).strip() if s is not None else ""

    # ------------------------------------------------------------------ #
    # Resolve the ACTUAL sale deal (from get_sale_deal_data via          #
    # fetch_sales_deal/build_deals) for a given CP + Product + leg.      #
    #                                                                    #
    # actualsaledeal shape (built in nodes.build_deals):                 #
    #   {"sale_deals": {"<CP> | <Product>": {"SDeal_A": {...},           #
    #                                        "SDeal_B": {...}}}}        #
    # Each SDeal_* dict is the SQL row and carries "CP", "Product",      #
    # "IndexName" (e.g. PPA_BRTM_AWS_Product_A / _B), MW1..MW25, etc.    #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _find_actual_sale_deal(actualsaledeal, cp, product, leg):
        """Return the matching SDeal_A / SDeal_B dict for (cp, product), else None."""
        if not isinstance(actualsaledeal, dict):
            return None
        stage = actualsaledeal.get("sale_deals")
        if not isinstance(stage, dict) or not stage:
            return None

        want_cp = Deal_Schedule_Plan._clean(cp)
        want_prod = Deal_Schedule_Plan._clean(product)
        leg_key = "SDeal_A" if leg == "A" else "SDeal_B"

        def _valid(d):
            return isinstance(d, dict) and len(d) > 0

        # 1) direct key hit: build_deals keys the stage by f"{CP} | {Product}"
        direct = stage.get(f"{want_cp} | {want_prod}")
        if isinstance(direct, dict) and _valid(direct.get(leg_key)):
            return direct[leg_key]

        # 2) fallback: scan entries and match on the deal's own CP / Product columns
        for key, legs in stage.items():
            if not isinstance(legs, dict):
                continue
            deal = legs.get(leg_key)
            if not _valid(deal):
                continue
            k_cp, k_prod = (key.split("|", 1) + [""])[:2] if isinstance(key, str) else ("", "")
            d_cp = Deal_Schedule_Plan._clean(deal.get("CP")) or Deal_Schedule_Plan._clean(k_cp)
            d_prod = Deal_Schedule_Plan._clean(deal.get("Product")) or Deal_Schedule_Plan._clean(k_prod)
            if d_cp == want_cp and d_prod == want_prod:
                return deal
        return None

    # ------------------------------------------------------------------ #
    # CHANGED (review): the SALE leg-A and leg-B row blocks were ~30      #
    # duplicated lines; factored into this single helper. Output shape    #
    # and fallbacks are identical to the old inline blocks.               #
    # Sale deal_number stays random per answer #4.                        #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _sale_row(leg, entry, alloc, sd):
        mw = Deal_Schedule_Plan._deal_mw_list(sd) if sd else alloc
        return {
            "type": "SALE",
            "leg": leg,                                     # authoritative A/B marker
            "deal_number": f"P-{random.randint(10000000, 99999999)}",
            # fall back to "Amazon Energy Services" / allocation entry's product
            "counterparty": (Deal_Schedule_Plan._clean(sd.get("CP")) or "Amazon Energy Services") if sd else "Amazon Energy Services",
            "product":      (Deal_Schedule_Plan._clean(sd.get("Product")) or entry.get("product")) if sd else entry.get("product"),
            "index":        (Deal_Schedule_Plan._clean(sd.get("IndexName")) or "N/A") if sd else "N/A",
            # sale hover: Book<-Path, Contract<-ContractName, Zone<-DPName when the
            # actual sale deal is present; empty otherwise so the UI keeps its fallback.
            "book":     Deal_Schedule_Plan._clean(sd.get("Path")) if sd else "",
            "contract": Deal_Schedule_Plan._clean(sd.get("ContractName")) if sd else "",
            "zone":     Deal_Schedule_Plan._clean(sd.get("DPName")) if sd else "",
            # when the actual sale deal is present, MW1..MW25 and the total volume
            # come from it; otherwise keep the allocation (productList) values.
            "mw":     mw,
            "volume": round(sum(mw), 4),
        }

    @classmethod
    def deal_schedule_plan(cls, saledeals, purchasedeals, tagdata, actualsaledeal) -> list[dict[str, Any]]:
        """
        Build the Final Deal Schedule Plan.

        - Per tag, per hour HE1..25 (None->0).
        - PURCHASE: match deals by tag.PurchaseCounterPartyName == deal.CounterParty,
          in list order; consumed once touched.
        - SALE: allocation split from Product A pool first, overflow to Product B.
          Each sale leg is bound to the matching ACTUAL sale deal (matched on
          CP + Product against `actualsaledeal`); the row's "counterparty",
          "product" and "index" are taken from that deal's "CP", "Product" and
          "IndexName" columns. If no matching deal is found: counterparty falls
          back to "Amazon Energy Services", product to the allocation entry's
          product, and index to "N/A".
        - CHANGED (review, answer #3): EVERY source pool (not just "All") is
          drawn down as tags allocate against it, so two tags sharing a source
          split the pool instead of double-counting it. Example: source pool
          A=100 MW; tag1 takes 80 -> pool 20 left; tag2 needing 40 gets 20
          from A and overflows 20 to B.
        - Purchase volume negative, sale volume positive. Shortfalls -> IMBALANCE.
        - CHANGED (review, answer #1): a tag is PLANNED when no purchase
          shortfall remains for any hour (was: purchase total ~ 0 AND ~ need
          total, which could never pass for a non-zero tag).
        """
        HES = list(range(1, 26))
        purchasedeals = purchasedeals or []
        tagdata = tagdata or []
        a_by_source, b_by_source = Deal_Schedule_Plan._build_sale_curves(saledeals)
        consumed = [False] * len(purchasedeals)

        plan = []
        for tag in tagdata:
            cp = Deal_Schedule_Plan._clean(tag.get("PurchaseCounterPartyName"))
            tag_code = Deal_Schedule_Plan._clean(tag.get("TagCode"))
            tag_index = tag.get("TagIndex")
            source = Deal_Schedule_Plan._clean(tag.get("Source"))
            sink = Deal_Schedule_Plan._clean(tag.get("Sink"))
            market_path = Deal_Schedule_Plan._clean(tag.get("MarketPath"))
            need = [Deal_Schedule_Plan._mw(tag, he) for he in HES]
            schedule_number = f"P-{random.randint(10000000, 99999999)}"
            rows = []

            if cp == "":
                plan.append({
                    "schedule_number": schedule_number,
                    "tag_code": tag_code,
                    "tag_index": tag_index,
                    "source": source,
                    "sink": sink,
                    "path": market_path,
                    "status": "IMBALANCE",
                    # CHANGED (review): was [] (a list) while every other tag
                    # emits a float -> inconsistent type for the HTML consumer.
                    "net_volume": 0.0,
                    "rows": [],
                })
                continue

            # ---------- PURCHASE: consume matching-CP deals, carry shortfall ----------
            remaining = need[:]
            purchase_total = [0.0] * len(HES)
            for i, pd in enumerate(purchasedeals):
                if all(r <= EPS for r in remaining):
                    break
                if consumed[i] or Deal_Schedule_Plan._clean(pd.get("CounterParty")) != cp:
                    continue
                alloc = [0.0] * len(HES)
                used = False
                for j, he in enumerate(HES):
                    take = min(remaining[j], Deal_Schedule_Plan._mw(pd, he))
                    if take > 0:
                        alloc[j] = round(take, 4)
                        remaining[j] = round(remaining[j] - take, 6)
                        # CHANGED (review): keep the running total rounded the
                        # same way as `remaining` to avoid float drift.
                        purchase_total[j] = round(purchase_total[j] + take, 6)
                        used = True
                consumed[i] = True  # consumed once touched (documented behavior)
                if used:
                    rows.append({
                        "type": "PURCHASE",
                        "deal_number": pd.get("DealNumber"),
                        "counterparty": Deal_Schedule_Plan._clean(pd.get("CounterParty")),
                        "product": Deal_Schedule_Plan._clean(pd.get("Product") or pd.get("DealType")),
                        "index": Deal_Schedule_Plan._clean(pd.get("IndexName")),
                        # purchase hover: source-column values for the tooltip.
                        "contract": Deal_Schedule_Plan._clean(pd.get("Contract")),   # Contract column
                        "market":   Deal_Schedule_Plan._clean(pd.get("Market")),     # Market column
                        "zone":     Deal_Schedule_Plan._clean(pd.get("Zone")),       # Zone column
                        "mw": alloc,
                        "volume": round(-sum(alloc), 4),
                    })

            # ---------- SALE: Product A first, overflow to Product B ----------
            # CHANGED (review): `src_key` was the same value as `source`
            # computed twice; reuse `source`.
            a_entry = a_by_source.get(source)
            b_entry = b_by_source.get(source)
            source_all = a_entry is None and b_entry is None
            if source_all:
                a_entry = a_by_source.get("All")
                b_entry = b_by_source.get("All")
            a_hours = a_entry["hours"] if a_entry else {}
            b_hours = b_entry["hours"] if b_entry else {}

            saleA = [0.0] * len(HES)
            saleB = [0.0] * len(HES)
            sale_total = [0.0] * len(HES)
            for j, he in enumerate(HES):
                n = need[j]
                avail_a = a_hours.get(he, 0.0)
                a = min(n, avail_a)
                a = a if a > 0 else 0.0
                saleA[j] = round(a, 4)
                avail_b = b_hours.get(he, 0.0)
                b = min(n - a, avail_b)
                b = b if b > 0 else 0.0
                saleB[j] = round(b, 4)
                sale_total[j] = a + b
                # CHANGED (review, answer #3): draw down EVERY pool, not just
                # the shared "All" pool, so a later tag with the same source
                # only sees what is left.
                if a_hours:
                    a_hours[he] = round(max(0.0, avail_a - a), 4)
                if b_hours:
                    b_hours[he] = round(max(0.0, avail_b - b), 4)

            # CP used for the actual-sale-deal lookup ("All" pool tags have
            # no per-CP sale deal, so they resolve to N/A by design).
            lookup_cp = "All" if source_all else source

            if a_entry is not None and sum(saleA) > 0:
                sdA = Deal_Schedule_Plan._find_actual_sale_deal(
                    actualsaledeal, lookup_cp, a_entry.get("product"), "A")
                rows.append(Deal_Schedule_Plan._sale_row("A", a_entry, saleA, sdA))

            if b_entry is not None and sum(saleB) > 0:
                sdB = Deal_Schedule_Plan._find_actual_sale_deal(
                    actualsaledeal, lookup_cp, b_entry.get("product"), "B")
                rows.append(Deal_Schedule_Plan._sale_row("B", b_entry, saleB, sdB))

            # ---------- balance / status ----------
            # CHANGED (review, answer #1): PLANNED == "no shortfall remains
            # after purchase allocation" (per-hour, within tolerance).
            balanced = all(r <= BALANCE_TOL for r in remaining)
            net = round(sum(r["volume"] for r in rows), 4)

            plan.append({
                "schedule_number": schedule_number,
                "tag_code": tag_code,
                "tag_index": tag_index,
                "source": source,
                "sink": sink,
                "path": market_path,
                "status": "PLANNED" if balanced else "IMBALANCE",
                "net_volume": net,
                "rows": rows,
            })
        return plan

    @staticmethod
    def _build_sale_curves(product_list):
        """
        From Location Aggregation ProductA/ProductB, build per-source hourly
        supply curves: { source: {"product": str, "hours": {he -> mw}} }.
        First occurrence of a source wins; the aggregate 'All' row is USED
        (shared pool) but never emits a matched sale deal.
        """
        def index(entries):
            out = {}
            for e in (entries or []):
                if not isinstance(e, dict):
                    continue
                src = Deal_Schedule_Plan._clean(e.get("CP"))
                if src in out:
                    continue
                hours = {}
                for pt in (e.get("data") or []):
                    if not isinstance(pt, dict):
                        continue
                    try:
                        he = int(pt.get("HE"))
                    except (TypeError, ValueError):
                        continue
                    try:
                        val = float(pt.get("values"))
                    except (TypeError, ValueError):
                        val = 0.0
                    if val != val:  # NaN
                        val = 0.0
                    hours[he] = val
                out[src] = {"product": Deal_Schedule_Plan._clean(e.get("Product")), "hours": hours}
            return out

        pl = product_list or {}
        return index(pl.get("ProductA")), index(pl.get("ProductB"))
