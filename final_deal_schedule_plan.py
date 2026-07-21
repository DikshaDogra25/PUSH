import math, random
from dataclasses import dataclass
from typing import Any


@dataclass
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
        """MW1..MW24 from a matched sale deal as a 24-length list (None/NaN -> 0.0).
        (Currently unused: sale MW/volume were reverted to allocation values.)"""
        return [Deal_Schedule_Plan._mw(deal, he) for he in range(1, 25)]

    @staticmethod
    def _clean(s):
        return str(s).strip() if s is not None else ""

    # ------------------------------------------------------------------ #
    # NEW: resolve the ACTUAL sale deal (from get_sale_deal_data via     #
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

    @classmethod
    def deal_schedule_plan(self, saledeals, purchasedeals, tagdata, actualsaledeal) -> Any:
        """
        Build the Final Deal Schedule Plan.

        - Per tag, per hour HE1..25 (None->0).
        - PURCHASE: match deals by tag.PurchaseCounterPartyName == deal.CounterParty,
          in list order; consumed once touched.
        - SALE: allocation split from Product A pool first, overflow to Product B.
          NEW: each sale leg is bound to the matching ACTUAL sale deal
          (matched on CP + Product against `actualsaledeal`); the row's
          "counterparty", "product" and "index" are taken from that deal's
          "CP", "Product" and "IndexName" columns. If no matching deal is
          found: counterparty falls back to "Amazon Energy Services",
          product to the allocation entry's product, and index to "N/A".
        - Purchase volume negative, sale volume positive. Shortfalls -> IMBALANCE.
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
                    "net_volume": [],
                    "rows": [],
                })
                continue

            # ---------- PURCHASE: consume matching-CP deals, carry shortfall ----------
            remaining = need[:]
            purchase_total = [0.0] * len(HES)
            for i, pd in enumerate(purchasedeals):
                if all(r <= 1e-9 for r in remaining):
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
                        purchase_total[j] += take
                        used = True
                consumed[i] = True
                if used:
                    rows.append({
                        "type": "PURCHASE",
                        "deal_number": pd.get("DealNumber"),
                        "counterparty": Deal_Schedule_Plan._clean(pd.get("CounterParty")),
                        "product": Deal_Schedule_Plan._clean(pd.get("Product") or pd.get("DealType")),
                        "index": Deal_Schedule_Plan._clean(pd.get("IndexName")),
                        # NEW (purchase hover): source-column values for the tooltip.
                        "contract": Deal_Schedule_Plan._clean(pd.get("Contract")),   # Contract column
                        "market":   Deal_Schedule_Plan._clean(pd.get("Market")),     # Market column
                        "zone":     Deal_Schedule_Plan._clean(pd.get("Zone")),       # Zone column
                        "mw": alloc,
                        "volume": round(-sum(alloc), 4),
                    })

            # ---------- SALE: Product A first, overflow to Product B ----------
            src_key = Deal_Schedule_Plan._clean(tag.get("Source"))
            a_entry = a_by_source.get(src_key)
            b_entry = b_by_source.get(src_key)
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
                if source_all:
                    if a_hours:
                        a_hours[he] = round(max(0.0, avail_a - a), 4)
                    if b_hours:
                        b_hours[he] = round(max(0.0, avail_b - b), 4)

            # NEW: CP used for the actual-sale-deal lookup ("All" pool tags have
            # no per-CP sale deal, so they resolve to N/A by design).
            lookup_cp = "All" if source_all else src_key

            if a_entry is not None and sum(saleA) > 0:
                # NEW: bind the leg to the fetched sale deal matched on CP + Product
                sdA = Deal_Schedule_Plan._find_actual_sale_deal(
                    actualsaledeal, lookup_cp, a_entry.get("product"), "A")
                rows.append({
                    "type": "SALE",
                    "leg": "A",                                     # authoritative A marker
                    "deal_number": f"P-{random.randint(10000000, 99999999)}",
                    # per screenshot: fall back to "Amazon Energy Services" / allocation product
                    "counterparty": (Deal_Schedule_Plan._clean(sdA.get("CP")) or "Amazon Energy Services") if sdA else "Amazon Energy Services",
                    "product":      (Deal_Schedule_Plan._clean(sdA.get("Product")) or a_entry.get("product")) if sdA else a_entry.get("product"),
                    "index":        (Deal_Schedule_Plan._clean(sdA.get("IndexName")) or "N/A") if sdA else "N/A",
                    # NEW (sale hover): Book<-Path, Contract<-ContractName, Zone<-DPName when the
                    # actual Sale Deal A is present; empty otherwise so the UI keeps its fallback.
                    "book":     Deal_Schedule_Plan._clean(sdA.get("Path")) if sdA else "",
                    "contract": Deal_Schedule_Plan._clean(sdA.get("ContractName")) if sdA else "",
                    "zone":     Deal_Schedule_Plan._clean(sdA.get("DPName")) if sdA else "",
                    # REVERTED: sale MW/volume come from the allocation (productList),
                    # NOT the actual deal's MW1..MW24. The per-hour graph (tag - purchases
                    # + sales) only balances when sales share the tag's hourly profile;
                    # the deal's own curve differs and made the graph read "Short".
                    "mw": saleA,
                    "volume": round(sum(saleA), 4),
                })

            if b_entry is not None and sum(saleB) > 0:
                sdB = Deal_Schedule_Plan._find_actual_sale_deal(
                    actualsaledeal, lookup_cp, b_entry.get("product"), "B")
                rows.append({
                    "type": "SALE",
                    "leg": "B",                                     # authoritative B marker
                    "deal_number": f"P-{random.randint(10000000, 99999999)}",  # Deal_Schedule_Plan._sale_deal_number("B", src_key)
                    # per screenshot: same fallbacks as leg A (bound to SDeal_B, see note)
                    "counterparty": (Deal_Schedule_Plan._clean(sdB.get("CP")) or "Amazon Energy Services") if sdB else "Amazon Energy Services",
                    "product":      (Deal_Schedule_Plan._clean(sdB.get("Product")) or b_entry.get("product")) if sdB else b_entry.get("product"),
                    "index":        (Deal_Schedule_Plan._clean(sdB.get("IndexName")) or "N/A") if sdB else "N/A",
                    # NEW (sale hover): Book<-Path, Contract<-ContractName, Zone<-DPName (Sale Deal B).
                    "book":     Deal_Schedule_Plan._clean(sdB.get("Path")) if sdB else "",
                    "contract": Deal_Schedule_Plan._clean(sdB.get("ContractName")) if sdB else "",
                    "zone":     Deal_Schedule_Plan._clean(sdB.get("DPName")) if sdB else "",
                    # REVERTED: allocation values (see leg A note).
                    "mw": saleB,
                    "volume": round(sum(saleB), 4),
                })

            # ---------- balance / status ----------
            ptot = round(sum(purchase_total), 4)
            need_total = round(sum(need), 4)
            balanced = abs(ptot - 0) < 0.01 and abs(ptot - need_total) < 0.01
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
