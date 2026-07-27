"""Deal schedule Agent: Return Deals and Tags data from Database."""

from genie.agents.base import BaseAgent
from genie.application.state import AgentState
from genie.registry import AgentMeta, FieldSpec, Skill

from genie_agent_dealschedule.mcp_utils import parse_mcp_output


class DealscheduleAgent(BaseAgent):
    """Retrieves dealschedule data, rules out ineligible tag sources, allocates
    the remainder across Product A / Product B, resolves the matching sale deal
    per product, and builds the final purchase/sale schedule plan + dashboard.

    Calls ``get_purchase_deal_data`` once for the date range, then
    ``get_sale_deal_data`` once per product to resolve its matching sale deal.
    """

    system_prompt = "Retrieves dealschedule data"
    tool_names: list[str] = [
        "get_purchase_deal_data",
        "get_scheduled_purchase_sale_deals",
        "get_sale_deal_data",
    ]

    def run(self, state: AgentState) -> AgentState:
        start_date = (state.get("start_date") or "").strip()
        end_date = (state.get("end_date") or "").strip()

        def work() -> tuple[str, dict]:
            purchase_result = self.call_mcp_tool_structured(
                "get_purchase_deal_data",
                {"start_date": start_date, "end_date": end_date},
            )
            scheduled_result = self.call_mcp_tool_structured(
                "get_scheduled_purchase_sale_deals",
                {"start_date": start_date, "end_date": end_date},
            )
            parsed = parse_mcp_output(
                purchase_result.structured,
                self._fetch_sale_deal,
                scheduled_result.structured,
            )
            view = {
                "type": "dealschedule",
                "result": parsed["result"],
                "PreviewHTML": parsed["previewHtml"],
                "FinalHTML": parsed["finalHtml"],
            }
            return parsed["message"] or "Success", view

        return self.answer_with(state, work, start_date=start_date, end_date=end_date)

    def _fetch_sale_deal(
        self,
        start_date: str,
        end_date: str,
        trade_date: str,
        trans_type: str,
        counterparty: str,
        product: str,
    ) -> list[dict]:
        """Call ``get_sale_deal_data`` for one product/trade-date combination."""
        result = self.call_mcp_tool_structured(
            "get_sale_deal_data",
            {
                "start_date": start_date,
                "end_date": end_date,
                "trade_date": trade_date,
                "trans_type": trans_type,
                "counterparty": counterparty,
                "product": product,
            },
        )
        structured = result.structured or {}
        return structured.get("sale_deals", [])


META = AgentMeta(
    agent_id="dealschedule_data_agent",
    version="1.0.0",
    capability_tags=["dealschedule", "webTrader", "amazon"],
    description="Retrieves dealschedule data by duration.",
    # Explicit A2A skills (served verbatim in the Agent Card). This agent does one
    # thing, so it advertises a single, well-described skill rather than the
    # auto-derived mirror of capability_tags.
    skills=[
        Skill(
            id="get_deal_schedule",
            name="Deal Schedule Information",
            description="Retrieves deal schedules.",
            tags=["dealschedule", "webTrader", "amazon"],
            examples=["webTrader deal today"],
        ),
    ],
    input_schema={
        "start_date": FieldSpec(type="string", required=True, description="start date."),
        "end_date": FieldSpec(type="string", required=True, description="end date."),
    },
    output_schema={
        "text": FieldSpec(
            type="string",
            description="Response text",
            persist=True,
        ),
        "view": FieldSpec(
            type="object",
            description="Response view",
            persist=True,
        ),
    },
    sla_ms=10000,
)


if __name__ == "__main__":
    # Run this agent as an independent service that self-registers with the
    # Registry Service and exposes the A2A endpoint POST /a2a. Set AGENT_PORT (e.g. 8007).
    from genie.agents.server import run_agent

    run_agent(DealscheduleAgent, META, port=8007)