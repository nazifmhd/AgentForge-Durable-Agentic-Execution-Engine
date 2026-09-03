"""Tools the ResearchAgent can call.

``crm_lookup`` and ``web_enrich`` are read-only and injectable — a deployment
passes callables that hit its real CRM / enrichment API; the defaults are inert
so the workflow runs (with lower research confidence) out of the box.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from agentforge.agents.tools import FunctionTool, ToolRegistry
from agentforge.core.runners import StepContext

LookupFn = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

CRM_LOOKUP = "crm_lookup"
WEB_ENRICH = "web_enrich"


async def _default_crm_lookup(_args: dict[str, Any]) -> dict[str, Any]:
    return {"found": False, "note": "no CRM lookup configured"}


async def _default_web_enrich(_args: dict[str, Any]) -> dict[str, Any]:
    return {"note": "no enrichment source configured"}


def build_sales_tools(
    *,
    crm_lookup: LookupFn | None = None,
    web_enrich: LookupFn | None = None,
) -> ToolRegistry:
    crm = crm_lookup or _default_crm_lookup
    web = web_enrich or _default_web_enrich
    reg = ToolRegistry()
    reg.register(
        FunctionTool(
            CRM_LOOKUP,
            "Look up whether a company/contact already exists in the CRM. "
            'args: {"domain": str, "company_name": str}',
            lambda _ctx, args: crm(args),
        )
    )
    reg.register(
        FunctionTool(
            WEB_ENRICH,
            "Fetch public firmographic data for a company (industry, headcount, "
            'tech, news). args: {"domain": str}',
            lambda _ctx, args: web(args),
        )
    )
    return reg


async def run_tool(reg: ToolRegistry, name: str, ctx: StepContext, args: dict[str, Any]) -> Any:
    return await reg.get(name).call(ctx, args)
