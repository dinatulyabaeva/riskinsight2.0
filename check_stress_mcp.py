"""Independent RiskInsight stress MCP check. Run from the project root."""
import asyncio
import json
from app.config import Settings
from app.mcp_client import MCPClient, EvidenceSession

async def main():
    settings = Settings.from_env()
    async with MCPClient(settings.db_path, dataset_id="builtin") as client:
        session = EvidenceSession(client)
        context = await session.call("get_dataset_context")
        if "error" in context:
            raise RuntimeError(f"Context MCP failed: {context['error']}")
        args = {"as_of": context["latest_date"], "pd_multiplier": 1.5, "lgd_add_pp": 5}
        stress = await session.call("run_stress_scenario", args)
        print("PARAMETERS:", json.dumps(args, ensure_ascii=False))
        print("MCP STATUS:", "ERROR" if "error" in stress else "OK")
        print("MCP RESPONSE:", json.dumps(stress, ensure_ascii=False, indent=2))
        print("TRACE:", json.dumps([{"tool": e["tool"], "ok": e["ok"], "error": e.get("error")} for e in session.trace], ensure_ascii=False, indent=2))

if __name__ == "__main__":
    asyncio.run(main())
