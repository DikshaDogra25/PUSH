
"""Standalone MCP server exposing sample outage/weather/docs tools via SSE.

Run: python -m genie_service_mcp.genie_mcp_server
Endpoint: http://127.0.0.1:8001/sse

Each tool declares a typed (Pydantic) return model, so FastMCP advertises an
``outputSchema`` and populates ``structuredContent`` on the wire — clients get
back machine-readable objects, not stringified JSON. Tools are read-only
(``readOnlyHint``) and signal failures by raising ``ToolError`` (MCP renders
that as ``CallToolResult(isError=true)``) rather than returning error payloads.
"""

import json
import pathlib
from typing import Any
from urllib.parse import urlparse

import zen
from dotenv import load_dotenv
from genie.platform.config import get_settings
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from genie_service_mcp.database.sql_db_utility import SqlDBUtility
from genie_service_mcp.rag_index import get_index

load_dotenv()

# Connection string is built lazily on first use, so constructing this at
# import time doesn't need the .env values yet.
db = SqlDBUtility("BROOKFIELD_OPERATIONAL")


WEATHER_DATA: dict[str, str] = {
    "london": "Cloudy, 14°C, light rain expected",
    "paris": "Sunny, 22°C, clear skies",
    "new york": "Partly cloudy, 18°C, mild winds",
    "tokyo": "Humid, 28°C, chance of thunderstorm",
    "dubai": "Hot and sunny, 41°C, no cloud cover",
    "minneapolis": "warm and humid, 28-30°C, small chance of showers or thunderstorms",
    "bloomington": "warm and mostly cloudy, 28-30°C, scattered thunderstorms or showers possible",
}

TOP_OUTAGES_LIMIT = 5

# All tools here are read-only queries; advertise that to clients.
_READ_ONLY = ToolAnnotations(readOnlyHint=True)


# ---------------------------------------------------------------------------
# Sample outage data. Today this is a hardcoded report; the real system will
# fetch the same records from a database — swap the bodies of report()/
# list_outages()/get_outage()/linked_outages() for DB queries and the tool
# surface above stays untouched.
#
# Five fabricated outage records — enough to demo list_outage_ids' "top 5" view
# and per-outage lookups without a bundled data file. None have attributewise
# inconsistencies, so get_outage_attribute_analysis correctly reports "not
# found" for any attribute — that's an honest empty state, not a stub.
# ---------------------------------------------------------------------------
_SAMPLE_REPORT: dict[str, Any] = {
    "id": 1,
    "name": "Sample_Outage_Report",
    "status": "completed",
    "created_dt": "2026-01-01T00:00:00Z",
    "time_period": {"start": "2026-01-01T00:00:00Z", "stop": "2026-01-02T00:00:00Z"},
    "total_outages": 5,
    "total_significant_outages": 2,
    "total_report_inconsistencies": 0,
    "total_keywords_detected": 0,
    "outagewise_analysis": [
        {
            "id": 1001,
            "metadata": {
                "short_description": "Transformer fault on feeder 12",
                "outage_type": "unplanned",
                "participant": "North Substation",
                "status": "resolved",
            },
            "analysis": {
                "total_outage_inconsistencies": 0,
                "is_significant": True,
                "summary": "No inconsistencies detected for this sample outage.",
                "critical_criteria": None,
                "attributewise_analysis": [],
            },
        },
        {
            "id": 1002,
            "metadata": {
                "short_description": "Scheduled line maintenance",
                "outage_type": "planned",
                "participant": "East Grid Co-op",
                "status": "resolved",
            },
            "analysis": {
                "total_outage_inconsistencies": 0,
                "is_significant": False,
                "summary": "No inconsistencies detected for this sample outage.",
                "critical_criteria": None,
                "attributewise_analysis": [],
            },
        },
        {
            "id": 1003,
            "metadata": {
                "short_description": "Storm damage to distribution line",
                "outage_type": "unplanned",
                "participant": "South Valley Utility",
                "status": "in_progress",
            },
            "analysis": {
                "total_outage_inconsistencies": 0,
                "is_significant": True,
                "summary": "No inconsistencies detected for this sample outage.",
                "critical_criteria": None,
                "attributewise_analysis": [],
            },
        },
        {
            "id": 1004,
            "metadata": {
                "short_description": "Substation equipment upgrade",
                "outage_type": "planned",
                "participant": "West Metro Power",
                "status": "resolved",
            },
            "analysis": {
                "total_outage_inconsistencies": 0,
                "is_significant": False,
                "summary": "No inconsistencies detected for this sample outage.",
                "critical_criteria": None,
                "attributewise_analysis": [],
            },
        },
        {
            "id": 1005,
            "metadata": {
                "short_description": "Vegetation contact trip",
                "outage_type": "unplanned",
                "participant": "North Substation",
                "status": "resolved",
            },
            "analysis": {
                "total_outage_inconsistencies": 0,
                "is_significant": False,
                "summary": "No inconsistencies detected for this sample outage.",
                "critical_criteria": None,
                "attributewise_analysis": [],
            },
        },
    ],
    "linked_outages_detected": [],
}

_INDEX: dict[int, dict[str, Any]] = {int(item["id"]): item for item in _SAMPLE_REPORT["outagewise_analysis"]}


def report() -> dict[str, Any]:
    """Return the full report record (caller projects the fields it needs)."""
    return _SAMPLE_REPORT


def list_outages() -> list[dict[str, Any]]:
    """Return all per-outage analysis records, in report order."""
    return _SAMPLE_REPORT["outagewise_analysis"]


def get_outage(outage_id: int) -> dict[str, Any] | None:
    """Return a single outage record by id, or ``None`` if there is no such outage."""
    return _INDEX.get(int(outage_id))


def linked_outages() -> list[dict[str, Any]]:
    """Return the linked-outage detections from the report."""
    return _SAMPLE_REPORT["linked_outages_detected"]


# ---------------------------------------------------------------------------
# Output models — drive FastMCP's outputSchema + structuredContent. Models that
# wrap rich, evolving report blocks allow extra keys so new source fields flow
# through without a schema change here.
# ---------------------------------------------------------------------------
class WeatherReport(BaseModel):
    """Current weather for a city."""

    city: str
    report: str


class OutageReportSummary(BaseModel):
    """Top-level summary of the outage analysis report."""

    id: int | None = None
    name: str | None = None
    status: str | None = None
    created_dt: str | None = None
    time_period: Any = None
    total_outages: int | None = None
    total_significant_outages: int | None = None
    total_report_inconsistencies: int | None = None
    total_keywords_detected: int | None = None
    linked_outages_count: int = 0


class OutageListItem(BaseModel):
    """One row in the top-N outage list."""

    id: int | None = None
    short_description: str | None = None
    outage_type: str | None = None
    participant: str | None = None
    status: str | None = None
    is_significant: bool | None = None


class OutageList(BaseModel):
    """The top-N outage list plus totals."""

    total: int
    returned: int
    items: list[OutageListItem]


class OutageMetadata(BaseModel):
    """Metadata block for one outage (known fields typed; extras passed through)."""

    model_config = ConfigDict(extra="allow")
    outage_type: str | None = None
    participant: str | None = None
    status: str | None = None
    short_description: str | None = None
    nature_of_work: str | None = None
    planned_duration: Any = None


class AttributewiseInconsistency(BaseModel):
    """Whether one analyzed attribute was found inconsistent."""

    attribute: str | None = None
    is_inconsistent: bool | None = None


class OutageAnalysisSummary(BaseModel):
    """Analysis summary for one outage (omits the long-form per-attribute blobs)."""

    id: int | None = None
    total_outage_inconsistencies: int | None = None
    is_significant: bool | None = None
    summary: str | None = None
    critical_criteria: Any = None
    attributewise_inconsistencies: list[AttributewiseInconsistency] = Field(default_factory=list)


class OutageAttributeAnalysis(BaseModel):
    """Full analysis text for one attribute of an outage."""

    model_config = ConfigDict(extra="allow")
    attribute: str | None = None
    is_inconsistent: bool | None = None
    analysis: Any = None


class LinkedOutage(BaseModel):
    """One linked-outage detection (a group of related outages plus its analysis)."""

    model_config = ConfigDict(extra="allow")
    linked_outages: Any = None
    analysis: Any = None


class LinkedOutages(BaseModel):
    """Wrapper so the linked-outage list is an object (clean structuredContent)."""

    total: int
    items: list[LinkedOutage]


class DocChunk(BaseModel):
    """One retrieved documentation chunk."""

    model_config = ConfigDict(extra="allow")
    source: str | None = None
    text: str | None = None
    score: float | None = None


class DocSearchResult(BaseModel):
    """Result of a documentation search."""

    query: str
    returned: int
    chunks: list[DocChunk]


class ValidationResponse(BaseModel):
    """Deals validation response"""

    Sheet: Any
    MainContract: Any
    Contract: Any
    DealDate: Any = Field(alias="Deal/Date")
    Date: Any
    Tag: Any
    RA_Tag: Any
    Schedule: Any
    DollarMWh: Any = Field(alias="$/MWh")
    EAdjustment: Any = Field(alias="E Adjustment")
    MultipleTagsperDeal: Any = Field(alias="Multiple Tags per Deal")
    PathLoadSinkDoubleCheck: Any = Field(alias="Path Load Sink DoubleCheck")
    PathGenSourceDoubleCheck: Any = Field(alias="Path Gen Source DoubleCheck")
    Deal: Any
    Type: Any
    SourceProduct: Any = Field(alias="Source Product")
    SourceCheck: Any = Field(alias="Source Check")
    Correctiontobemade: Any = Field(alias="Correction to be made")
    SinkCheck: Any = Field(alias="Sink Check")
    CityofSanJoseMRTUCounterparty: Any = Field(alias="City of San Jose / MRTU / Counterparty")
    ScheduleStatus2: Any = Field(alias="Schedule Status 2")
    Sink: Any


# Bind to the host/port advertised by mcp_server_url so the server's bind address
# and the URL clients connect to share one source of truth and can never drift.
# Falls back to 127.0.0.1:8001 when the setting is unset or carries no port.
_mcp_url = urlparse(get_settings().mcp_server_url or "")
mcp = FastMCP(
    "genie-mcp-server",
    host=_mcp_url.hostname or "127.0.0.1",
    port=_mcp_url.port or 8001,
)


@mcp.tool(annotations=_READ_ONLY)
def get_weather(city: str) -> WeatherReport:
    """Return the current weather report for the given city.

    Supported cities (case-insensitive): london, paris, new york, tokyo, dubai,
    minneapolis, bloomington. Raises if the city is not known.
    """
    key = (city or "").strip().lower()
    report = WEATHER_DATA.get(key)
    if report is None:
        raise ToolError(f"No weather data available for '{city}'.")
    return WeatherReport(city=key, report=report)


@mcp.tool(annotations=_READ_ONLY)
def get_outage_report_summary() -> OutageReportSummary:
    """Return the top-level summary of the outage analysis report.

    Includes report id/name, time period, status, and aggregate counts
    (total outages, significant outages, report inconsistencies, keywords).
    """
    data = report()
    return OutageReportSummary(
        id=data.get("id"),
        name=data.get("name"),
        status=data.get("status"),
        created_dt=data.get("created_dt"),
        time_period=data.get("time_period"),
        total_outages=data.get("total_outages"),
        total_significant_outages=data.get("total_significant_outages"),
        total_report_inconsistencies=data.get("total_report_inconsistencies"),
        total_keywords_detected=data.get("total_keywords_detected"),
        linked_outages_count=len(data.get("linked_outages_detected", [])),
    )


@mcp.tool(annotations=_READ_ONLY)
def list_outage_ids() -> OutageList:
    """Return the top 5 outages from the report with short descriptions."""
    items = list_outages()
    top = [
        OutageListItem(
            id=item.get("id"),
            short_description=item.get("metadata", {}).get("short_description"),
            outage_type=item.get("metadata", {}).get("outage_type"),
            participant=item.get("metadata", {}).get("participant"),
            status=item.get("metadata", {}).get("status"),
            is_significant=item.get("analysis", {}).get("is_significant"),
        )
        for item in items[:TOP_OUTAGES_LIMIT]
    ]
    return OutageList(total=len(items), returned=len(top), items=top)


@mcp.tool(annotations=_READ_ONLY)
def get_outage_metadata(outage_id: int) -> OutageMetadata:
    """Return the metadata block for a specific outage id.

    Raises if there is no outage with that id.
    """
    item = get_outage(outage_id)
    if item is None:
        raise ToolError(f"No outage found with id {outage_id}.")
    return OutageMetadata(**item.get("metadata", {}))


@mcp.tool(annotations=_READ_ONLY)
def get_outage_analysis_summary(outage_id: int) -> OutageAnalysisSummary:
    """Return the analysis summary for a specific outage id.

    Includes total inconsistencies, the markdown summary, significance flag,
    and the list of attributewise inconsistencies (without the full long-form
    analysis blob). Raises if there is no outage with that id.
    """
    item = get_outage(outage_id)
    if item is None:
        raise ToolError(f"No outage found with id {outage_id}.")
    analysis = item.get("analysis", {})
    return OutageAnalysisSummary(
        id=item.get("id"),
        total_outage_inconsistencies=analysis.get("total_outage_inconsistencies"),
        is_significant=analysis.get("is_significant"),
        summary=analysis.get("summary"),
        critical_criteria=analysis.get("critical_criteria"),
        attributewise_inconsistencies=[
            AttributewiseInconsistency(
                attribute=a.get("attribute"),
                is_inconsistent=a.get("is_inconsistent"),
            )
            for a in analysis.get("attributewise_analysis", [])
        ],
    )


@mcp.tool(annotations=_READ_ONLY)
def get_outage_attribute_analysis(outage_id: int, attribute: str) -> OutageAttributeAnalysis:
    """Return the full analysis text for one attribute of a specific outage.

    Use list_outage_ids and get_outage_analysis_summary first to discover which
    attributes have inconsistencies. Raises if the outage or attribute is absent.
    """
    item = get_outage(outage_id)
    if item is None:
        raise ToolError(f"No outage found with id {outage_id}.")
    target = (attribute or "").strip().lower()
    for a in item.get("analysis", {}).get("attributewise_analysis", []):
        if (a.get("attribute") or "").lower() == target:
            return OutageAttributeAnalysis(**a)
    raise ToolError(f"No attribute '{attribute}' found for outage {outage_id}.")


@mcp.tool(annotations=_READ_ONLY)
def get_linked_outages() -> LinkedOutages:
    """Return the list of linked-outage detections from the report."""
    items = linked_outages()
    return LinkedOutages(
        total=len(items),
        items=[LinkedOutage(**item) for item in items],
    )


@mcp.tool(annotations=_READ_ONLY)
def search_docs(query: str, k: int = 4) -> DocSearchResult:
    """Retrieve the top-k most relevant documentation chunks for a query.

    Backs the RAG agent: searches this framework's own markdown docs
    (README, SETUP, docs/*) with BM25 ranking and returns the matching
    chunks with their source path and relevance score. Use to answer
    'what is', 'how does', 'explain', 'why' questions about the system
    (A2A, router, registry, planner, blackboard, synthesizer, ...).
    """
    chunks = get_index().search(query or "", k=max(1, min(int(k or 4), 10)))
    return DocSearchResult(
        query=query,
        returned=len(chunks),
        chunks=[DocChunk(**c) for c in chunks],
    )


@mcp.tool(annotations=_READ_ONLY)
async def get_deal_schedule_data(start_date: str, end_date: str) -> list[list[dict[str, Any]]]:
    """Retrieve deal schedule data.

    Args:
        start_date:    start date.
        end_date:      end date.
    """
    from genie_service_mcp.web_trader.dealschedule import dealschedule_data

    return await dealschedule_data(start_date, end_date)


@mcp.tool(annotations=_READ_ONLY)
async def get_purchase_deal_data(start_date: str, end_date: str) -> list[list[dict[str, Any]]]:
    """Retrieve purchase deals and unscheduled tags for the given date range.

    Args:
        start_date: start date.
        end_date:   end date.
    """
    from genie_service_mcp.web_trader.dealschedule import dealschedule_purchase_data

    return await dealschedule_purchase_data(start_date, end_date)


@mcp.tool(annotations=_READ_ONLY)
async def get_scheduled_purchase_sale_deals(
    start_date: str, end_date: str
) -> list[list[dict[str, Any]]]:
    """Retrieve already-scheduled purchase AND sale deals (existing tags).

    Same procedure as ``get_purchase_deal_data`` but with @TransType='ALL' and
    @ShowScheduledTags=1, so it returns the deals that are already scheduled.

    Args:
        start_date: start date.
        end_date:   end date.
    """
    from genie_service_mcp.web_trader.dealschedule import dealschedule_scheduled_purchase_sale_data

    return await dealschedule_scheduled_purchase_sale_data(start_date, end_date)


@mcp.tool(annotations=_READ_ONLY)
async def get_sale_deal_data(
    start_date: str,
    end_date: str,
    trade_date: str,
    trans_type: str = "S",
    counterparty: str = "ALL",
    product: str = "ALL",
) -> list[dict[str, Any]]:
    """Retrieve the sale deal template matching a product/counterparty on a trade date.

    Args:
        start_date:    start date.
        end_date:      end date.
        trade_date:    trade date used for both TDate and TEndDate.
        trans_type:    transaction type filter (default 'S' for Sale).
        counterparty:  counterparty filter (default 'ALL').
        product:       product filter (default 'ALL').
    """
    from genie_service_mcp.web_trader.dealschedule import dealschedule_sale_data

    return await dealschedule_sale_data(start_date, end_date, trade_date, trans_type, counterparty, product)


def _load_market_power_data(
    start_date: str | None = None,
    end_date: str | None = None,
    market_name: str | None = None,
) -> list[dict[str, Any]]:
    """Execute the stored procedure with optional date and market filters.

    Returns an empty list when no records match — never raises on empty results.
    Callers are responsible for catching exceptions.
    """
    params: dict[str, Any] = {}
    if start_date:
        params["@StartDate"] = int(start_date.replace("-", ""))
    if end_date:
        params["@EndDate"] = int(end_date.replace("-", ""))
    if market_name:
        params["@MarketName"] = market_name.upper()
    else:
        params["@MarketName"] = "ALL"

    return db.execute_procedure("dbo.usp_Genie_getMeterDataStatus", params=params)


@mcp.tool(annotations=_READ_ONLY)
def list_power_availability() -> list[dict[str, Any]] | dict[str, str]:
    """Return power percentage availability for ALL markets from the latest report.

    No arguments required. Returns a list of records with fields:
    MarketName, OperatingDate, TotalMeters, MetersWithData, CompletionPercent.

    Call this when no specific market or date filter is provided.
    """
    try:
        return _load_market_power_data()
    except Exception as exc:
        return {"error": str(exc)}


@mcp.tool(annotations=_READ_ONLY)
def get_power_availability_detail(
    MarketName: str | None = None,
    StartDate: str | None = None,
    EndDate: str | None = None,
) -> list[dict[str, Any]] | dict[str, str]:
    """Return power percentage availability filtered by market and/or date range.

    Args:
        MarketName: Optional market identifier (e.g. 'PJM', 'MISO', 'ERCOT').
        StartDate:  Optional start date in YYYY-MM-DD format (e.g. '2026-06-01').
        EndDate:    Optional end date in YYYY-MM-DD format (e.g. '2026-06-25').

    Returns a list of records with fields:
    MarketName, OperatingDate, TotalMeters, MetersWithData, CompletionPercent.

    Call this when the user specifies a market name, a date range, or both.
    """
    try:
        return _load_market_power_data(
            start_date=StartDate,
            end_date=EndDate,
            market_name=MarketName,
        )
    except Exception as exc:
        return {"error": str(exc)}


@mcp.tool(annotations=_READ_ONLY)
def get_market_power_summary(
    MarketName: str | None = None,
    StartDate: str | None = None,
    EndDate: str | None = None,
) -> list[dict[str, Any]] | dict[str, str]:
    """Return a high-level settlement/outage summary for a given market and duration.

    Args:
        MarketName: Optional market identifier (e.g. 'PJM', 'MISO', 'ERCOT').
        StartDate:  Optional start date in YYYY-MM-DD format.
        EndDate:    Optional end date in YYYY-MM-DD format.

    Returns summary-level meter completion data for the specified filters.
    Called alongside get_power_availability_detail to enrich the detail view.
    """
    try:
        return _load_market_power_data(
            start_date=StartDate,
            end_date=EndDate,
            market_name=MarketName,
        )
    except Exception as exc:
        return {"error": str(exc)}


class DealsValidationOutput(BaseModel):
    validations: list[ValidationResponse]
    deal: list[dict[str, Any]]
    schedule_tag: list[dict[str, Any]]
    purchase_vs_sale_tag: list[dict[str, Any]] = []
    bo: list[dict[str, Any]] = []
    counterparty: str = "City of San Jose"
    start_date: str = ""
    end_date: str = ""


def _strip_padded_strings(obj: Any) -> Any:
    """Recursively strip CHAR-column padding from proc results.

    SQL Server CHAR columns come back space-padded to their declared width
    (e.g. DealTypeName == 'Index                    '), but ruleset.json's
    decision tables compare against exact literals like 'Index'. Untrimmed
    padding silently fails every such match and falls through to the
    ruleset's catch-all ERROR row.
    """
    if isinstance(obj, str):
        return obj.strip()
    if isinstance(obj, list):
        return [_strip_padded_strings(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _strip_padded_strings(v) for k, v in obj.items()}
    return obj


def _coerce_numeric_field(rows: list[dict], field: str) -> None:
    """Convert a proc column that comes back as a formatted numeric string
    (e.g. IndexAdder == '0.000000') to an actual float, in place.

    ruleset.json's decision tables compare some columns against unquoted
    numeric literals (e.g. Dollar_MWh: 0); the ZEN engine does a strict
    type-checked equality there, so a numeric-looking string never matches
    a numeric literal and silently falls through to the catch-all ERROR row.
    Only call this on columns that are genuinely always-numeric — unlike
    e.g. Schedule or Date, which are numeric-looking strings but must stay
    strings (Schedule can hold the literal 'NO Schedule'; Date is formatted
    text elsewhere).
    """
    for row in rows:
        v = row.get(field)
        if isinstance(v, str):
            try:
                row[field] = float(v)
            except ValueError:
                pass


@mcp.tool(annotations=_READ_ONLY)
def get_deals_validation_data(
    start_date: str, end_date: str, counterparty: str = "City of San Jose"
) -> DealsValidationOutput:
    """Get deals validation data.
    Input parameters:
    start_date: Start date in format yyyy-mm-dd to fetch deals validation data.
    end_date: End date in format yyyy-mm-dd to fetch deals validation data.
    counterparty: Deal counterparty name to filter on (default 'City of San Jose').
    """
    params: dict[str, str] = {
        "@StartDate": start_date,
        "@EndDate": end_date,
        "@TagStatus": "ALL",
        "@DealCounterparty": counterparty,
    }
    result_sets = db.execute_procedure_all_resultsets(procedure_name="usp_WT_GetDealScheduleTagBO_Data", params=params)
    result_sets = [_strip_padded_strings(rs) for rs in result_sets]
    _coerce_numeric_field(result_sets[0], "IndexAdder")  # deal set; feeds Dollar_MWh in deal_stage1
    all_result_sets = {}
    all_result_sets["deal"] = result_sets[0]
    all_result_sets["schedule_tag"] = result_sets[1]
    all_result_sets["purchase_vs_sale_tag"] = result_sets[2]
    all_result_sets["bo"] = result_sets[3]
    all_result_sets["ra_tag"] = result_sets[4]

    all_result_sets["mappings"] = [
        {
            "Date": "2026-01-01 00:00:00",
            "Plant_Name": "Lois & Powell Lake Damn",
            "Path_Gen_Source": "POWELL.RIVER",
            "Product": "Carbon Free Energy",
            "1": "",
            "OFFICIAL_COUNTERPARTY": "City of San Jose",
            "CUSTOMER_IFS": "CISAJO",
            "2": "",
            "DEAL_TYPE": "Sale",
            "3": "",
            "PRODUCT": "Carbon Free Energy",
        },
        {
            "Date": "2026-01-02 00:00:00",
            "Plant_Name": "Boundary Hydroelectric  Units",
            "Path_Gen_Source": "BNDY",
            "Product": "Carbon Free Energy",
            "1": "",
            "OFFICIAL_COUNTERPARTY": "Counterparty",
            "CUSTOMER_IFS": "",
            "2": "",
            "DEAL_TYPE": "Purchase",
            "3": "",
            "PRODUCT": "ACS Energy",
        },
        {
            "Date": "2026-01-03 00:00:00",
            "Plant_Name": "Kerr Hydro",
            "Path_Gen_Source": "KERR",
            "Product": "Carbon Free Energy",
            "1": "",
            "OFFICIAL_COUNTERPARTY": "",
            "CUSTOMER_IFS": "",
            "2": "",
            "DEAL_TYPE": "",
            "3": "",
            "PRODUCT": "E",
        },
        {
            "Date": "2026-01-04 00:00:00",
            "Plant_Name": "Smith Falls Hydro",
            "Path_Gen_Source": "SmithCreek",
            "Product": "Carbon Free Energy",
            "1": "",
            "OFFICIAL_COUNTERPARTY": "",
            "CUSTOMER_IFS": "",
            "2": "",
            "DEAL_TYPE": "",
            "3": "",
            "PRODUCT": "",
        },
        {
            "Date": "2026-01-05 00:00:00",
            "Plant_Name": "Mid-C Hydro - Hydro Rock Reach  (Chelan County PUD)",
            "Path_Gen_Source": "MIDC",
            "Product": "Carbon Free Energy",
            "1": "",
            "OFFICIAL_COUNTERPARTY": "",
            "CUSTOMER_IFS": "",
            "2": "",
            "DEAL_TYPE": "",
            "3": "",
            "PRODUCT": "",
        },
        {
            "Date": "2026-01-06 00:00:00",
            "Plant_Name": "Mid-C Hydro - Hydro Rock Island  (Chelan County PUD)",
            "Path_Gen_Source": "MIDC",
            "Product": "Carbon Free Energy",
            "1": "",
            "OFFICIAL_COUNTERPARTY": "",
            "CUSTOMER_IFS": "",
            "2": "",
            "DEAL_TYPE": "",
            "3": "",
            "PRODUCT": "",
        },
        {
            "Date": "2026-01-07 00:00:00",
            "Plant_Name": "MID-C Hydro - Priest Rapids and Wanapum (Wanapum Dams - Grant County)",
            "Path_Gen_Source": "BRTM_PRP",
            "Product": "Carbon Free Energy",
            "1": "",
            "OFFICIAL_COUNTERPARTY": "",
            "CUSTOMER_IFS": "",
            "2": "",
            "DEAL_TYPE": "",
            "3": "",
            "PRODUCT": "",
        },
        {
            "Date": "2026-01-08 00:00:00",
            "Plant_Name": "Lake Chelan Hydro",
            "Path_Gen_Source": "MIDC",
            "Product": "Carbon Free Energy",
            "1": "",
            "OFFICIAL_COUNTERPARTY": "",
            "CUSTOMER_IFS": "",
            "2": "",
            "DEAL_TYPE": "",
            "3": "",
            "PRODUCT": "",
        },
        {
            "Date": "2026-01-09 00:00:00",
            "Plant_Name": "Brownlee",
            "Path_Gen_Source": "OBBLPR",
            "Product": "Carbon Free Energy",
            "1": "",
            "OFFICIAL_COUNTERPARTY": "",
            "CUSTOMER_IFS": "",
            "2": "",
            "DEAL_TYPE": "",
            "3": "",
            "PRODUCT": "",
        },
        {
            "Date": "2026-01-10 00:00:00",
            "Plant_Name": "Oxbow",
            "Path_Gen_Source": "",
            "Product": "Carbon Free Energy",
            "1": "",
            "OFFICIAL_COUNTERPARTY": "",
            "CUSTOMER_IFS": "",
            "2": "",
            "DEAL_TYPE": "",
            "3": "",
            "PRODUCT": "",
        },
        {
            "Date": "2026-01-11 00:00:00",
            "Plant_Name": "McNair Creek Hydro",
            "Path_Gen_Source": "MCNAIR.CREEK",
            "Product": "Carbon Free Energy",
            "1": "",
            "OFFICIAL_COUNTERPARTY": "",
            "CUSTOMER_IFS": "",
            "2": "",
            "DEAL_TYPE": "",
            "3": "",
            "PRODUCT": "",
        },
        {
            "Date": "2026-01-12 00:00:00",
            "Plant_Name": "Furry Creek",
            "Path_Gen_Source": "FURRY.CREEK",
            "Product": "Carbon Free Energy",
            "1": "",
            "OFFICIAL_COUNTERPARTY": "",
            "CUSTOMER_IFS": "",
            "2": "",
            "DEAL_TYPE": "",
            "3": "",
            "PRODUCT": "",
        },
        {
            "Date": "2026-01-13 00:00:00",
            "Plant_Name": "Tacoma Power - ACS",
            "Path_Gen_Source": "TPWR",
            "Product": "ACS Energy",
            "1": "",
            "OFFICIAL_COUNTERPARTY": "",
            "CUSTOMER_IFS": "",
            "2": "",
            "DEAL_TYPE": "",
            "3": "",
            "PRODUCT": "",
        },
        {
            "Date": "2026-01-14 00:00:00",
            "Plant_Name": "Bonneville Power Administration BPA - ACS",
            "Path_Gen_Source": "BPAPOWER",
            "Product": "ACS Energy",
            "1": "",
            "OFFICIAL_COUNTERPARTY": "",
            "CUSTOMER_IFS": "",
            "2": "",
            "DEAL_TYPE": "",
            "3": "",
            "PRODUCT": "",
        },
        {
            "Date": "2026-01-15 00:00:00",
            "Plant_Name": "Bonneville Power Administration BPA - ACS",
            "Path_Gen_Source": "BPASLICE",
            "Product": "ACS Energy",
            "1": "",
            "OFFICIAL_COUNTERPARTY": "",
            "CUSTOMER_IFS": "",
            "2": "",
            "DEAL_TYPE": "",
            "3": "",
            "PRODUCT": "",
        },
        {
            "Date": "2026-01-16 00:00:00",
            "Plant_Name": "MID-C Hydro - Priest Rapids and Wanapum (Wanapum Dams - Grant County)",
            "Path_Gen_Source": "GCPD",
            "Product": "Carbon Free Energy",
            "1": "",
            "OFFICIAL_COUNTERPARTY": "",
            "CUSTOMER_IFS": "",
            "2": "",
            "DEAL_TYPE": "",
            "3": "",
            "PRODUCT": "",
        },
    ]
    all_result_sets["summary"] = [
        {
            "CFE_Contract": "SJCE ID: 25-124-01",
            "Deal_Type": "Index",
            "Product": "Carbon Free Energy",
            "Type": "Sale",
            "Schedule": "Y",
            "TAG": "Y",
            "RA_TAG": "NO RA Tag",
            "Index": "MRTU_TH_NP15_GEN-APND",
            "Type_F/P": "Physical",
            "$/MWh": "LMP + $20",
            "Path_Gen_Source": "See list below",
            "Sink": "SJCE.NP15",
            "Invoicing": "BRTMLP to CISAJO",
            "System": "Wtrader",
        },
        {
            "CFE_Contract": "SJCE ID: 25-124-01",
            "Deal_Type": "Index",
            "Product": "ACS Energy",
            "Type": "Sale",
            "Schedule": "Y",
            "TAG": "Y",
            "RA_TAG": "NO RA Tag",
            "Index": "MRTU_TH_NP15_GEN-APND",
            "Type_F/P": "Physical",
            "$/MWh": "LMP + $20",
            "Path_Gen_Source": "See list below",
            "Sink": "SJCE.NP15",
            "Invoicing": "BRTMLP to CISAJO",
            "System": "Wtrader",
        },
        {
            "CFE_Contract": "SJCE ID: 25-124-01 (E Adjustment)",
            "Deal_Type": "Index",
            "Product": "E",
            "Type": "Purchase",
            "Schedule": "NO Schedule",
            "TAG": "NO Tag",
            "RA_TAG": "NO RA Tag",
            "Index": "MRTU_TH_NP15_GEN-APND",
            "Type_F/P": "Physical",
            "$/MWh": "LMP",
            "Path_Gen_Source": "-",
            "Sink": "SJCE.NP15",
            "Invoicing": "BRTMLP to CISAJO",
            "System": "Wtrader",
        },
    ]
    engine = zen.ZenEngine()
    decision = engine.create_decision(content=(pathlib.Path(__file__).parent / "ruleset.json").read_text())
    input = json.dumps(all_result_sets)
    pre_result = decision.evaluate(input)["result"]
    result_cols = [
        "Sheet",
        "MainContract",
        "Contract",
        "Deal/Date",
        "Date",
        "Tag",
        "RA_Tag",
        "Schedule",
        "$/MWh",
        "E Adjustment",
        "Multiple Tags per Deal",
        "Path Load Sink DoubleCheck",
        "Path Gen Source DoubleCheck",
        "Deal",
        "Type",
        "Source Product",
        "Source Check",
        "Correction to be made",
        "Sink Check",
        "City of San Jose / MRTU / Counterparty",
        "Schedule Status 2",
        "Sink",
    ]
    result: list[ValidationResponse] = []
    for r in pre_result["validations"]:
        obj = {}
        for key in result_cols:
            if key in r:
                obj[key] = r[key]
            else:
                obj[key] = ""
        resp = ValidationResponse.model_validate(obj)
        result.append(resp)
    return DealsValidationOutput(
        validations=result,
        deal=pre_result.get("deal", []),
        schedule_tag=pre_result.get("schedule_tag", []),
        purchase_vs_sale_tag=pre_result.get("purchase_vs_sale_tag", []),
        bo=pre_result.get("bo", []),
        counterparty=counterparty,
        start_date=start_date,
        end_date=end_date,
    )


@mcp.tool(annotations=_READ_ONLY)
def get_rate_change_data() -> dict[str, Any]:
    """Retrieve rate data.
    - webTrader Data
    - OASIS Data
    """
    from genie_service_mcp.web_trader.ratechange import ratechange_data

    return ratechange_data()


if __name__ == "__main__":
    mcp.run(transport="sse")
