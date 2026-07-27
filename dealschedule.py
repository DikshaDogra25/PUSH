
import datetime
import decimal
from typing import Any
from uuid import UUID

from genie.platform.sqlserver import get_async_sqlserver_connection
from genie_service_mcp.web_trader.queries.get_deal_schedule_query import (
    GET_DEAL_SCHEDULE_QUERY,
    GET_DEAL_SCHEDULE_SCHEDULED_QUERY,
)
from genie_service_mcp.web_trader.queries.get_tag_for_schedule_query import (
    GET_TAG_FOR_SCHEDULE_QUERY,
)


def serialize_row(row: dict[str, Any]) -> dict[str, Any]:
    return {k: serialize_value(v) for k, v in dict(row).items()}


def serialize_value(v: Any) -> Any:
    if isinstance(v, decimal.Decimal):
        return float(v)
    elif isinstance(v, (datetime.datetime, datetime.date, datetime.time)):
        return v.isoformat()
    elif isinstance(v, bytes):
        return v.hex()
    elif isinstance(v, UUID):
        return str(v)
    return v


async def _execute_result_sets(query: str, *args: Any) -> list[list[dict[str, Any]]]:
    async with get_async_sqlserver_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(query, *args)
            all_result_sets = []
            while True:
                columns = [col[0] for col in cur.description] if cur.description else []
                batch_rows = []
                while True:
                    batch = await cur.fetchmany(200)
                    if not batch:
                        break

                    batch_rows.extend(serialize_row(dict(zip(columns, row))) for row in batch)

                if batch_rows:
                    all_result_sets.append(batch_rows)

                if not await cur.nextset():
                    break

        return all_result_sets


async def dealschedule_data(*args: Any) -> list[list[dict[str, Any]]]:
    return await _execute_result_sets(GET_TAG_FOR_SCHEDULE_QUERY, *args)


async def dealschedule_purchase_data(start_date: str, end_date: str) -> list[list[dict[str, Any]]]:
    """Fetch purchase deals + unscheduled tags for the given date range."""
    return await _execute_result_sets(
        GET_DEAL_SCHEDULE_QUERY,
        start_date,
        end_date,
        "ALL",
        "ALL",
        "P",
        start_date,
        end_date,
        1,
        0,
    )


async def dealschedule_scheduled_purchase_sale_data(
    start_date: str, end_date: str
) -> list[list[dict[str, Any]]]:
    """Fetch ALREADY-scheduled purchase AND sale deals (existing tags).

    Same procedure as ``dealschedule_purchase_data`` but with @TransType='ALL'
    (both purchase and sale) and @ShowScheduledTags=1 (return existing tags).
    """
    return await _execute_result_sets(
        GET_DEAL_SCHEDULE_SCHEDULED_QUERY,
        start_date,
        end_date,
        "ALL",
        "ALL",
        "ALL",  # @TransType: both purchase and sale (was "P")
        start_date,
        end_date,
        1,
        0,
        1,  # @ShowScheduledTags = 1
    )


async def dealschedule_sale_data(
    start_date: str,
    end_date: str,
    trade_date: str,
    trans_type: str = "S",
    counterparty: str = "ALL",
    product: str = "ALL",
) -> list[dict[str, Any]]:
    """Fetch the matching sale deal template row for one product/counterparty on a trade date.

    Returns the first row of the first result set (the sale deal template), or
    ``{}`` when the stored procedure returns no matching row.
    """
    tables = await _execute_result_sets(
        GET_DEAL_SCHEDULE_QUERY,
        start_date,
        end_date,
        product,
        counterparty,
        trans_type,
        trade_date,
        trade_date,
        0,
        1,
    )
    if len(tables) > 1 and tables[1]:
        return {"sale_deals": tables[1]}
    return {}
