from genie_platform.core.models import FinalResponseFormat
from agents.dealschedule.src.genie_platform.agents.dealschedule.state import AgentState
from agents.dealschedule.src.genie_platform.agents.dealschedule.prompts import SYSTEM_PROMPT, FORMAT_INSTRUCTIONS
from langchain_core.messages import AIMessage, ToolMessage, BaseMessage, SystemMessage
from genie_platform.llm_clients.openai_client import openapi_model
from typing import Literal
from langgraph.prebuilt import ToolNode
from langchain_core.tools import BaseTool
from langchain_core.runnables import RunnableConfig
from collections import defaultdict
import json
import requests
from datetime import date, datetime, timedelta
from sqlalchemy import text
from typing import Any
import math
import sys
import xml.etree.ElementTree as ET
from agents.dealschedule.src.genie_platform.agents.dealschedule.rule_engine import RuleEngine, RuleSet, RuleResult
from agents.dealschedule.src.genie_platform.agents.dealschedule.build_dashboard import Build_Dashboard
from agents.dealschedule.src.genie_platform.agents.dealschedule.build_preview_dashboard import Build_Preview_Dashboard
from agents.dealschedule.src.genie_platform.agents.dealschedule.final_deal_schedule_plan import Deal_Schedule_Plan
from pathlib import Path
import pandas as pd
import copy
import html

LIMIT = 150


def call_model(state: AgentState, config: RunnableConfig) -> dict:
    start_date = state.get("Start_Date")
    end_date = state.get("End_Date")
    prompt_data = {
        "start_date": start_date,
        "end_date": end_date
    }
    call_messages = [SystemMessage(SYSTEM_PROMPT.render(prompt_data))] + state["messages"]
    tools = config.get("configurable", {}).get("tool_list", [])
    purchase_tool = [t for t in tools if t.name == "get_purchase_deal_data"]
    response = openapi_model.bind_tools(purchase_tool).invoke(call_messages)
    return {"messages": [response]}


def decide_route(state: AgentState) -> Literal["tools", "format"]:
    last_message = state["messages"][-1]
    if last_message.tool_calls:
        return "tools"
    else:
        return "format"


async def do_format(state: AgentState, config: RunnableConfig) -> dict:
    last_message = state["messages"][-1]
    if isinstance(last_message, AIMessage) and (not last_message.tool_calls or len(last_message.tool_calls) <= 0):
        resp = last_message.content
        if len(resp) == 0:
            resp = 'Unable to process request'
        response_format = FinalResponseFormat(message=resp, status='completed')
        return {'final_response': response_format}

    if isinstance(last_message, ToolMessage):
        if isinstance(last_message.content, list):
            if len(last_message.content) == 0:
                saveOutputRes("", "ruledata")
                saveOutputRes("", "Aggregatedata")
                saveOutputRes("", "productAandB")
                saveOutputRes("", "purchasedeal")
                saveOutputRes("", "ruledtagdata")
                saveOutputRes("", "saledeal")
                saveOutputRes("", "dealschedule")
                response_format = FinalResponseFormat(
                    message="",
                    status="completed"
                )
                return {"final_response": response_format}
            else:
                data = json.loads(last_message.content[0]["text"])
        else:
            data = json.loads(last_message.content)

        responsemessage = ""
        # CHANGED (review): the previous order made the second branch
        # unreachable (len<2 caught len==0 first); check the emptier case first.
        if len(data) < 1:
            responsemessage = "Purchase deals not found for this date."
        elif len(data) < 2:
            responsemessage = "Unscheduled tags not found for this date."
        else:
            ruledata = AppyRules(data[1])
            saveOutputRes(ruledata, "ruledata")
            saveOutputRes(ruledata[0], "ruledtagdata")
            # purchasedeal = get_purchase_deal(ruledata[0], data[0])
            saveOutputRes(data[0], "purchasedeal")
            AggregatedList = ProductCalculatorTool(ruledata, False)
            saveOutputRes(AggregatedList, "productAandB")
            productList = ProductCalculatorTool(ruledata, True)
            tools = config.get("configurable", {}).get("tool_list", [])
            saledeals = await fetch_sales_deal(tools, productList)
            saveOutputRes(saledeals, "saledeal")
            deal_Schedule_final_plan = Deal_Schedule_Plan().deal_schedule_plan(productList, data[0], data[1], saledeals)
            finalresult = {"Unschedule Tags": data[1],
                           "Purchase Deals": data[0],
                           "Location Agrregation & Allocation": AggregatedList,
                           "FINAL_PLAN": deal_Schedule_final_plan}
            data = Build_Dashboard().replace_nan(finalresult, 0)
            # print(deal_Schedule_final_plan)
            base_dir = Path("C:/inetpub/wwwroot")
            output_file = base_dir / "dealschedule"
            preview_path = output_file / f"Amazon_Deal_Schedule_Planner_Preview.html"
            final_path = output_file / f"Amazon_Deal_Schedule_Planner_new.html"
            preview_html_data = Build_Preview_Dashboard().build_preview_dashboard_html(data, preview_path)
            final_html_data = Build_Dashboard().build_final_dashboard_html(data, final_path)
            with open("Amazon_Deal_Schedule_Planner_Preview.html", "w", encoding="utf-8") as f:
                f.write(preview_html_data)
            with open("Amazon_Deal_Schedule_Planner.html", "w", encoding="utf-8") as f:
                f.write(final_html_data)
            responsemessage = json.dumps({"PreviewHTML": preview_html_data, "FinalHTML": final_html_data})

        response_format = FinalResponseFormat(
            message=responsemessage,
            status="completed"
        )
        return {"final_response": response_format}
    return {"messages": []}


def get_purchase_deal(tagdata, purchasedeal) -> dict:
    product_list = {row["product"] for row in tagdata if "product" in row}
    filtered_purchase_deal = [deal for deal in purchasedeal if deal.get("Product") in product_list]
    return filtered_purchase_deal


# --------------------------------------------------------------------------- #
# UPDATED per screenshots:                                                    #
#   - sale deals are now fetched by counterparty ("Amazon Energy LLC")        #
#     + product, instead of by source CP                                      #
#   - the `if item['CP'] == "All": break` guard is removed (every ProductA    #
#     entry is queried)                                                       #
#   - primary hit is keyed by CP; the fallback (T-2 window) hit is keyed by   #
#     Product, exactly as in the screenshot. build_deals below resolves both  #
#     keys so fallback templates are still found.                             #
# --------------------------------------------------------------------------- #
async def fetch_sales_deal(tools: list[BaseTool], productList) -> dict:
    sales_tool = next(t for t in tools if t.name == "get_sale_deal_data")
    templates = {}
    for item in productList["ProductA"]:
        sale_date = parse_date(item["ProfileDate"]) - timedelta(days=1)
        trade_date = parse_date(item["ProfileDate"]) - timedelta(days=2)
        result = await sales_tool.ainvoke({
            "start_date": sale_date.strftime("%m/%d/%Y"),
            "end_date": sale_date.strftime("%m/%d/%Y"),
            "trans_type": "S",
            "counterparty": "Amazon Energy LLC",
            "product": item['Product'],
            "trade_date": trade_date.strftime("%m/%d/%Y")
        })
        if result:
            templates[item['Product']] = result[0]
        else:
            result = await sales_tool.ainvoke({
                "start_date": (date.today() - timedelta(days=2)).strftime("%m/%d/%Y"),
                "end_date": (date.today() - timedelta(days=2)).strftime("%m/%d/%Y"),
                "trans_type": "S",
                "counterparty": "Amazon Energy LLC",
                "product": item['Product'],
                "trade_date": trade_date.strftime("%m/%d/%Y")
            })
            if result:
                templates[item['Product']] = result[0]

    # CHANGED (review): `templates` is always a dict (never None), so the old
    # `is not None` check always passed even when no sale deal was found.
    if templates:
        return build_deals(productList, templates)
    else:
        print(f"Skipping build_sale_deals: no sale deal found")
        return ""


def _apply_product_index(deal, leg):
    """
    Option B: the sale query returns two otherwise-identical rows that differ only
    in IndexName (…_Product_A vs …_Product_B). We keep a single template (result[0])
    and stamp the correct suffix onto IndexName per leg so Sale Deal A gets
    …_Product_A and Sale Deal B gets …_Product_B.

    Suffix-safe: strips any existing _Product_A / _Product_B before appending, so
    it is correct whether result[0] happened to be the A row or the B row.
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


def build_deals(productList, templates) -> dict:
    stage = {}
    productA = productList["ProductA"]
    productB = productList["ProductB"]
    for i in range(min(len(productA), len(productB))):
        item_a = productA[i]
        item_b = productB[i]
        saledealA = []
        saledealB = []
        # templates is now keyed by Product name (see fetch_sales_deal)
        template = templates.get(item_a['Product'])
        # CHANGED (review): the old `deal_a = deal_b = copy.deepcopy(template)`
        # aliased ONE object to both names (harmless only because each leg
        # re-parses via json.loads below); give each leg its own copy.
        deal_a = copy.deepcopy(template)
        deal_b = copy.deepcopy(template)
        if isinstance(deal_a, dict) and "text" in deal_a:
            if (any(item["values"] > 0 for item in item_a['data'])):
                dealA = json.loads(deal_a["text"])
                if dealA:
                    for data in item_a['data']:
                        he = int(data["HE"])  # 1..25
                        if 1 <= he <= 25:
                            dealA[f"MW{he}"] = data["values"]
                    dealA["Deal Date"] = (date.today() - timedelta(days=1)).strftime("%m/%d/%Y")
                    dealA["Trade Date"] = (date.today() - timedelta(days=2)).strftime("%m/%d/%Y")
                    _apply_product_index(dealA, "A")   # Option B: IndexName -> …_Product_A
                    saledealA = dealA
        if isinstance(deal_b, dict) and "text" in deal_b:
            if (any(item["values"] > 0 for item in item_b['data'])):
                dealB = json.loads(deal_b["text"])
                if dealB:
                    for data in item_b['data']:
                        he = int(data["HE"])  # 1..25
                        if 1 <= he <= 25:
                            dealB[f"MW{he}"] = data["values"]
                    dealB["Deal Date"] = (date.today() - timedelta(days=1)).strftime("%m/%d/%Y")
                    dealB["Trade Date"] = (date.today() - timedelta(days=2)).strftime("%m/%d/%Y")
                    _apply_product_index(dealB, "B")   # Option B: IndexName -> …_Product_B
                    saledealB = dealB
        stage[f"{item_a['CP']} | {item_a['Product']}"] = {"SDeal_A": saledealA, "SDeal_B": saledealB}
        # result = insert_deal(deal, "https://i-dikshad.dev.oati.local/cgi-bin/webplus.dll?script=/webtrader_base/WT_Interfaces/webservice/ws_main.wml")
    return {"sale_deals": stage}


def do_tools(tools: list[BaseTool]) -> ToolNode:
    return ToolNode(tools)


def ProductCalculatorTool(data, source_temp: bool = False):  # CHANGED (review): `False` was used as a type annotation
    res1 = ConvertModelData(data[2]) if source_temp else ConvertModelData(data[0])
    if len(data) >= 1 and data[1]:
        for item in data[1]:
            item["Source"] = "All"
            item["Product"] = "E"
            item["product"] = "E"  # mirror: ConvertModelData reads the lowercase key
        newRow = ConvertModelData(data[1])
        res1.extend(newRow)
    saveOutputRes(res1, "Aggregatedata")
    res2 = AssigningProduct(res1)
    return res2


def AppyRules(data):
    here = Path(__file__).parent
    ruleSet = RuleSet.from_json(here / "ruleset.json")
    df = pd.DataFrame.from_records(data)
    result: RuleResult = RuleEngine(ruleSet).apply(df)
    tagtable: list[dict[str, Any]] = result.kept.to_dict(orient="records")
    ruledouttagtable: list[dict[str, Any]] = result.ruled_out.to_dict(orient="records")
    tagtemptable: list[dict[str, Any]] = result.kept_detailed.to_dict(orient="records")
    return [tagtable, ruledouttagtable, tagtemptable]


def ConvertModelData(tags):
    groups = defaultdict(
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
        profiledate = tag.get("ProfileDate", "").strip()
        product = tag.get("product", "").strip() if tag.get("product", "All") else "All"
        source_temp = tag.get("Source_Temp", "").strip()
        key = (source)
        groups[key]["CP"] = source
        groups[key]["ProfileDate"] = profiledate
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

    resultAggregate = []
    for group in groups.values():
        group.pop("count")
        temps = list(dict.fromkeys(
            t.strip() for t in group.pop("Source_Temp") if t.strip()
        ))
        aggregated = {
            "CP": group["CP"],
            "Product": group["Product"],
            "ProfileDate": group["ProfileDate"],
            "Source_Temp": ",".join(temps),
        }
        for he in range(1, 25):
            mw_key = f"MW{he}"
            aggregated[he] = round(group.get(mw_key, 0.0), 4)
        resultAggregate.append(aggregated)
    return resultAggregate


def AssigningProduct(rows):
    he_running_totals = defaultdict(float)
    result = {"ProductA": [], "ProductB": []}
    for row in rows:
        product_a = {"CP": row["CP"], "Product": row["Product"], "ProfileDate": row["ProfileDate"],
                     "Source_Temp": row.get("Source_Temp", ""), "data": []}
        product_b = {"CP": row["CP"], "Product": row["Product"], "ProfileDate": row["ProfileDate"],
                     "Source_Temp": row.get("Source_Temp", ""), "data": []}
        for he in range(1, 25):
            value = row.get(he, 0.0)
            current_total = he_running_totals[he]
            remaining_capacity = max(0.0, 150 - current_total)
            value_a = round(min(value, remaining_capacity), 4)
            value_b = round(max(0.0, value - value_a), 4)
            he_running_totals[he] += value
            product_a["data"].append({"HE": he, "values": value_a})
            product_b["data"].append({"HE": he, "values": value_b})
        result["ProductA"].append(product_a)
        result["ProductB"].append(product_b)
    return result


def agent_response(data):
    product_a_md = create_text_table("Product A", data["ProductA"])
    product_b_md = create_text_table("Product B", data["ProductB"])
    return f"{product_a_md}\n\n{product_b_md}"


def create_text_table(title, products):
    col_width = 10

    def split_header(text, width):
        if len(text) <= width:
            return text, ""
        return text[:width], text[width:width * 2]

    header1 = ["HE"]
    header2 = [""]
    for p in products:
        h1, h2 = split_header(p["CP"], col_width - 3)
        header1.append(h1)
        header2.append(h2)

    result = f"{title}\n\n"
    result += "".join(f"{h:<{col_width}}" for h in header1) + "\n"
    result += "".join(f"{h:<{col_width}}" for h in header2) + "\n"
    result += "-" * (col_width * len(header1)) + "\n"

    max_he = max(d["HE"] for p in products for d in p["data"])
    for he in range(1, max_he + 1):
        row = [str(he)]
        for p in products:
            item = next((x for x in p["data"] if x["HE"] == he), None)
            value = f"{item['values']:.4f}" if item else "0.0000"
            row.append(value)
        result += "".join(f"{v:<{col_width}}" for v in row) + "\n"
    return f"```text\n{result}\n```"


def saveOutputRes(res, filename):
    base_dir = Path("C:/inetpub/wwwroot")
    output_file = base_dir / "dealschedule"
    path = output_file / f"{filename}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as file:
        json.dump(res, file, indent=4)


def insert_deal(deal: dict, endpoint: str, verify_tls: bool = False) -> dict:
    # CHANGED (review): TLS verification is now an explicit parameter instead of a
    # buried verify=False. Default kept False for the internal .local endpoint
    # (self-signed cert), but flip to True if this ever targets a public host.
    # !!! RESTORE FROM YOUR REPO: the original SOAP/XML envelope in this function
    # was stripped by the file upload (XML tags were lost), so the literal below
    # is a placeholder. The function is currently unused (its call site in
    # build_deals is commented out), so this does not affect the pipeline.
    xml_body = f"""<!-- restore original WTDealImport SOAP envelope here -->"""
    headers = {
        "Content-Type": "text/xml; charset=utf-8",
        "SOAPAction": "http://www.oati.net/namespace/WTDealImport",  # confirm exact value
    }
    response = requests.post(endpoint, data=xml_body.encode("utf-8"), headers=headers, verify=verify_tls)
    response.raise_for_status()
    final_result = extract_return_desc(response.text)  # the SOAP response XML
    return final_result


def extract_return_desc(xml_text: str) -> dict:
    root = ET.fromstring(xml_text)
    nodecode = root.find(".//{*}ReturnCode")
    nodecodedesc = root.find(".//{*}ReturnCodeDesc")
    code = nodecode.text.strip() if nodecode is not None and nodecode.text else ""
    desc = nodecodedesc.text.strip() if nodecodedesc is not None and nodecodedesc.text else ""
    return {"Return Code": code, "Message": desc}


def parse_date(s):
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Unrecognized date format: {s!r}")
