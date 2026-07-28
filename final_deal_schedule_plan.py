
import math
from typing import Any

EPS = 1e-9  # "effectively zero" for allocation loops
BALANCE_TOL = 0.01  # tolerance when deciding PLANNED vs IMBALANCE


class DealSchedulePlan:
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

        The tag allocation arrays run HE1..HE25 (25 entries), so a deal-sourced
        sale row must match that length or its volume silently drops HE25.
        Missing / None / NaN MW25 (or any hour) still resolves to 0.0 via _mw.
        """
        return [DealSchedulePlan._mw(deal, he) for he in range(1, 26)]

    @staticmethod
    def _clean(s):
        return str(s).strip() if s is not None else ""

    @staticmethod
    def _is_scheduled_tag(tag) -> bool:
        """True when the tag's IsScheduledTag column marks it already-scheduled."""
        if not isinstance(tag, dict):
            return False
        v = str(tag.get("IsScheduledTag", "") or "").strip().lower()
        return v in ("yes", "y", "true", "1")

    @staticmethod
    def _sale_leg_from_index(index_name) -> str:
        """Product leg from a sale IndexName postfix (_Product_A/_Product_B)."""
        idx = str(index_name or "").strip().lower()
        if idx.endswith("_product_a"):
            return "A"
        if idx.endswith("_product_b"):
            return "B"
        return ""

    @staticmethod
    def _deal_matches_tag(deal, tag_code, sched_no, cp, is_sale) -> bool:
        """Associate a scheduled deal row with a scheduled tag.

        The scheduled result set is a flat tag+deal join, so each deal row carries
        its own ``TagCode`` -- that is the authoritative link and is required to
        keep two scheduled tags' deals apart (e.g. two purchases with the same
        CounterParty). Falls back to ScheduleNumber, then CounterParty (purchase)
        / order (sale) only when TagCode isn't present.
        """
        clean = DealSchedulePlan._clean
        dtc = clean(deal.get("TagCode"))
        if tag_code and dtc:
            return dtc == tag_code
        dn = clean(deal.get("ScheduleNumber"))
        if sched_no and dn:
            return dn == sched_no
        if not is_sale and cp:
            return clean(deal.get("CounterParty")) == cp
        # No usable join key at all: associate by order, consumed once used.
        return True

    @staticmethod
    def _scheduled_purchase_row(deal):
        """A PURCHASE row for an already-scheduled purchase deal (from the
        get_scheduled_purchase_sale_deals result set). Values come from the DB
        columns: DealNumber, CounterParty, Product, IndexName, Contract, Status.
        Book is left blank (unconfirmed -> rendered 'Not Available')."""
        clean = DealSchedulePlan._clean
        mw = DealSchedulePlan._deal_mw_list(deal)
        return {
            "type": "PURCHASE",
            "schedule_status": "Scheduled",
            "deal_number": deal.get("DealNumber"),
            "counterparty": clean(deal.get("CounterParty")),
            "product": clean(deal.get("Product") or deal.get("DealType")),
            "index": clean(deal.get("IndexName")),
            "book": "",  # purchase book unconfirmed
            "contract": clean(deal.get("Contract") or deal.get("ContractName")),
            "market": clean(deal.get("Market")),
            "zone": clean(deal.get("Zone")),
            "status": clean(deal.get("Status") or deal.get("DealStatus")),
            "mw": mw,
            "volume": round(-sum(mw), 4),
        }

    # ------------------------------------------------------------------ #
    # Resolve the ACTUAL sale deal (from get_sale_deal_data via          #
    # fetch_sales_deal/build_deals) for a given CP + Product + leg.      #
    #                                                                    #
    # actualsaledeal shape (built in mcp_utils.build_deals):             #
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

        want_cp = DealSchedulePlan._clean(cp)
        want_prod = DealSchedulePlan._clean(product)
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
            d_cp = DealSchedulePlan._clean(deal.get("CP")) or DealSchedulePlan._clean(k_cp)
            d_prod = DealSchedulePlan._clean(deal.get("Product")) or DealSchedulePlan._clean(k_prod)
            if d_cp == want_cp and d_prod == want_prod:
                return deal
        return None

    @staticmethod
    def _scheduled_sale_row(deal):
        """A SALE row for an already-scheduled (existing) sale deal.

        Real values come straight from the scheduled result set: Book<-Path,
        Contract<-ContractName, Zone<-DPName, Counterparty<-CounterParty.
        """
        clean = DealSchedulePlan._clean
        mw = DealSchedulePlan._deal_mw_list(deal)
        return {
            "type": "SALE",
            # leg from IndexName postfix (_Product_A/_Product_B) so the tooltip
            # attributes the used MW to Product A or Product B correctly (item 5).
            "leg": DealSchedulePlan._sale_leg_from_index(deal.get("IndexName")),
            "schedule_status": "Scheduled",
            "deal_number": deal.get("DealNumber"),
            "counterparty": clean(deal.get("CounterParty")),  # sale CP <- CounterParty column
            "product": clean(deal.get("Product")),
            "index": clean(deal.get("IndexName")) or "N/A",
            "book": clean(deal.get("Path")),
            "contract": clean(deal.get("ContractName")),
            "zone": clean(deal.get("DPName")),
            "mw": mw,
            "volume": round(sum(mw), 4),
        }

    @staticmethod
    def _sale_row(leg, entry, alloc, sd, schedule_status="Unscheduled"):
        mw = alloc  # DealSchedulePlan._deal_mw_list(sd) if sd else alloc
        return {
            "type": "SALE",
            "leg": leg,  # authoritative A/B marker
            "schedule_status": schedule_status,  # newly planned sale -> Unscheduled
            "deal_number": "PENDING",  # f"P-{random.randint(10000000, 99999999)}",
            # sale CP <- CounterParty column (fall back to CP, then a friendly default)
            "counterparty": (
                DealSchedulePlan._clean(sd.get("CounterParty"))
                or DealSchedulePlan._clean(sd.get("CP"))
                or "Amazon Energy Services"
            )
            if sd
            else "Amazon Energy Services",
            "product": (
                (DealSchedulePlan._clean(sd.get("Product")) or entry.get("product")) if sd else entry.get("product")
            ),
            "index": (DealSchedulePlan._clean(sd.get("IndexName")) or "N/A") if sd else "N/A",
            # sale hover: Book<-Path, Contract<-ContractName, Zone<-DPName when the
            # actual sale deal is present; empty otherwise so the UI keeps its fallback.
            "book": DealSchedulePlan._clean(sd.get("Path")) if sd else "",
            "contract": DealSchedulePlan._clean(sd.get("ContractName")) if sd else "",
            "zone": DealSchedulePlan._clean(sd.get("DPName")) if sd else "",
            # when the actual sale deal is present, MW1..MW25 and the total volume
            # come from it; otherwise keep the allocation (productList) values.
            "mw": mw,
            "volume": round(sum(mw), 4),
        }

    @classmethod
    def deal_schedule_plan(
        cls, saledeals, purchasedeals, tagdata, actualsaledeal, used_sale_product=None, used_purchase=None
    ) -> list[dict[str, Any]]:
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
        - EVERY source pool (not just "All") is drawn down as tags allocate
          against it, so two tags sharing a source split the pool instead of
          double-counting it. Example: source pool A=100 MW; tag1 takes 80 ->
          pool 20 left; tag2 needing 40 gets 20 from A and overflows 20 to B.
        - Purchase volume negative, sale volume positive. Shortfalls -> IMBALANCE.
        - A tag is PLANNED when no purchase shortfall remains for any hour.
        """

        HES = list(range(1, 26))
        purchasedeals = purchasedeals or []
        tagdata = tagdata or []
        used_sale_product = used_sale_product or []
        used_purchase = used_purchase or []
        a_by_source, b_by_source = DealSchedulePlan._build_sale_curves(saledeals)
        consumed = [False] * len(purchasedeals)
        # scheduled (already-tagged) deals consumed per scheduled tag
        sched_pur_consumed = [False] * len(used_purchase)
        sched_sale_consumed = [False] * len(used_sale_product)

        plan = []
        for tag in tagdata:
            cp = DealSchedulePlan._clean(tag.get("PurchaseCounterPartyName"))
            tag_code = DealSchedulePlan._clean(tag.get("TagCode"))
            tag_index = tag.get("TagIndex")
            source = DealSchedulePlan._clean(tag.get("Source"))
            sink = DealSchedulePlan._clean(tag.get("Sink"))
            market_path = DealSchedulePlan._clean(tag.get("MarketPath"))
            need = [DealSchedulePlan._mw(tag, he) for he in HES]
            # Schedule number from the ScheduleNumber column (item 4); PENDING for
            # newly-planned (unscheduled) tags that have none.
            sched_no = DealSchedulePlan._clean(tag.get("ScheduleNumber"))
            schedule_number = sched_no or "PENDING"
            # An already-scheduled tag (IsScheduledTag = Yes) is shown as Scheduled
            # with no accept/reject actions; drives the Status & Actions column.
            is_scheduled = DealSchedulePlan._is_scheduled_tag(tag)
            rows = []

            # ---------- SCHEDULED tag: rows come straight from the DB deals ----------
            # (get_scheduled_purchase_sale_deals) linked by ScheduleNumber; no new
            # allocation is planned for an already-scheduled tag.
            if is_scheduled:
                deal_sched_no = ""  # ScheduleNumber lives on the scheduled deal rows
                for i, pdl in enumerate(used_purchase):
                    if sched_pur_consumed[i]:
                        continue
                    if not DealSchedulePlan._deal_matches_tag(pdl, tag_code, sched_no, cp, is_sale=False):
                        continue
                    sched_pur_consumed[i] = True
                    deal_sched_no = deal_sched_no or DealSchedulePlan._clean(pdl.get("ScheduleNumber"))
                    rows.append(DealSchedulePlan._scheduled_purchase_row(pdl))
                for i, sdl in enumerate(used_sale_product):
                    if sched_sale_consumed[i]:
                        continue
                    if not DealSchedulePlan._deal_matches_tag(sdl, tag_code, sched_no, cp, is_sale=True):
                        continue
                    sched_sale_consumed[i] = True
                    deal_sched_no = deal_sched_no or DealSchedulePlan._clean(sdl.get("ScheduleNumber"))
                    rows.append(DealSchedulePlan._scheduled_sale_row(sdl))
                # Prefer the tag's own ScheduleNumber, then the linked deal's; the
                # main tag rows don't carry ScheduleNumber, the scheduled deals do.
                if not sched_no and deal_sched_no:
                    schedule_number = deal_sched_no
                net = round(sum(r["volume"] for r in rows), 4)
                plan.append(
                    {
                        "schedule_number": schedule_number,
                        "tag_code": tag_code,
                        "tag_index": tag_index,
                        "source": source,
                        "sink": sink,
                        "path": market_path,
                        "status": "SCHEDULED",
                        "scheduled": True,
                        "net_volume": net,
                        "rows": rows,
                    }
                )
                continue

            if cp == "":
                plan.append(
                    {
                        "schedule_number": schedule_number,
                        "tag_code": tag_code,
                        "tag_index": tag_index,
                        "source": source,
                        "sink": sink,
                        "path": market_path,
                        "status": "IMBALANCE",
                        "scheduled": is_scheduled,
                        "net_volume": 0.0,
                        "rows": [],
                    }
                )
                continue

            # ---------- PURCHASE: consume matching-CP deals, carry shortfall ----------
            remaining = need[:]
            purchase_total = [0.0] * len(HES)
            for i, pd in enumerate(purchasedeals):
                if all(r <= EPS for r in remaining):
                    break
                if consumed[i] or DealSchedulePlan._clean(pd.get("CounterParty")) != cp:
                    continue
                alloc = [0.0] * len(HES)
                used = False
                for j, he in enumerate(HES):
                    take = min(remaining[j], DealSchedulePlan._mw(pd, he))
                    if take > 0:
                        alloc[j] = round(take, 4)
                        remaining[j] = round(remaining[j] - take, 6)
                        purchase_total[j] = round(purchase_total[j] + take, 6)
                        used = True
                consumed[i] = True  # consumed once touched (documented behavior)
                if used:
                    clean = DealSchedulePlan._clean
                    rows.append(
                        {
                            "type": "PURCHASE",
                            # Schedule Status computed in mcp_utils.annotate_purchase_schedule_status
                            "schedule_status": clean(pd.get("Schedule Status")) or "Unscheduled",
                            "deal_number": pd.get("DealNumber"),
                            "counterparty": clean(pd.get("CounterParty")),
                            "product": clean(pd.get("Product") or pd.get("DealType")),
                            "index": clean(pd.get("IndexName")),
                            # purchase hover: source-column values for the tooltip.
                            # Book on the purchase result set is unresolved (no book-like
                            # column seen); left blank + flagged rather than substituted.
                            "book": clean(pd.get("Book")),
                            "status": (clean(pd.get("Status") or pd.get("DealStatus") or pd.get("DealState"))),
                            "contract": clean(pd.get("Contract")),
                            "market": clean(pd.get("Market")),
                            "zone": clean(pd.get("Zone")),
                            "mw": alloc,
                            "volume": round(-sum(alloc), 4),
                        }
                    )

            # ---------- SALE: Product A first, overflow to Product B ----------
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
                # draw down EVERY pool, not just the shared "All" pool, so a
                # later tag with the same source only sees what is left.
                if a_hours:
                    a_hours[he] = round(max(0.0, avail_a - a), 4)
                if b_hours:
                    b_hours[he] = round(max(0.0, avail_b - b), 4)

            # CP used for the actual-sale-deal lookup ("All" pool tags have
            # no per-CP sale deal, so they resolve to N/A by design).
            lookup_cp = "All" if source_all else source
            if a_entry is not None and sum(saleA) > 0:
                sdA = DealSchedulePlan._find_actual_sale_deal(actualsaledeal, lookup_cp, a_entry.get("product"), "A")
                rows.append(DealSchedulePlan._sale_row("A", a_entry, saleA, sdA))

            if b_entry is not None and sum(saleB) > 0:
                sdB = DealSchedulePlan._find_actual_sale_deal(actualsaledeal, lookup_cp, b_entry.get("product"), "B")
                rows.append(DealSchedulePlan._sale_row("B", b_entry, saleB, sdB))

            # (Already-scheduled SALE deals are attached to their scheduled tag in
            # the is_scheduled branch above, not here, so unscheduled tags only
            # ever show newly-planned sale rows.)

            # ---------- balance / status ----------
            # PLANNED == "no shortfall remains after purchase allocation" (per-hour, within tolerance).
            balanced = all(r <= BALANCE_TOL for r in remaining)
            net = round(sum(r["volume"] for r in rows), 4)

            plan.append(
                {
                    "schedule_number": schedule_number,
                    "tag_code": tag_code,
                    "tag_index": tag_index,
                    "source": source,
                    "sink": sink,
                    "path": market_path,
                    "status": "PLANNED" if balanced else "IMBALANCE",
                    "scheduled": is_scheduled,
                    "net_volume": net,
                    "rows": rows,
                }
            )
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
            for e in entries or []:
                if not isinstance(e, dict):
                    continue
                src = DealSchedulePlan._clean(e.get("CP"))
                if src in out:
                    continue
                hours = {}
                for pt in e.get("data") or []:
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
                out[src] = {"product": DealSchedulePlan._clean(e.get("Product")), "hours": hours}
            return out

        # New sale deals are allocated only from AVAILABLE product (used sale MW
        # already netted out); fall back to the flat shape for older callers.
        pl = product_list or {}
        pl = pl.get("available", pl)
        return index(pl.get("ProductA")), index(pl.get("ProductB"))
