import pytest
from app.agent import ChatRequest, llm_answer


class FakeSession:
    def __init__(self, el):
        self.el = el
        self.trace = []

    async def call(self, name, args=None):
        ref = f"T{len(self.trace) + 1}"
        if name == "get_dataset_context":
            result = {"latest_date": "2026-10-31", "dates": ["2026-10-31"]}
        elif name == "get_portfolio_summary":
            result = {"metrics": {"el_rub": self.el}}
        else:
            raise AssertionError(name)
        self.trace.append({"tool": name, "ok": True, "ref": ref, "result": result})
        return result


class Settings:
    llm_configured = True


class ForbiddenProvider:
    async def complete(self, *args):
        raise AssertionError("LLM must not be invoked for simple EL query")


@pytest.mark.asyncio
@pytest.mark.parametrize("value, expected", [(290.0, "290.00"), (None, "не рассчитан")])
async def test_el_from_mcp_without_model(value, expected):
    from app.tool_registry import TOOLS
    from app.agent import _scope_tool_args
    session = FakeSession(value)
    tool = {"name": "get_portfolio_summary", "description": "Summary", "inputSchema": TOOLS["get_portfolio_summary"][0].model_json_schema()}
    request = ChatRequest(dataset_id="upload_00000000000000000000000000000001", question="РљР°РєРѕРІС‹ РѕР¶РёРґР°РµРјС‹Рµ РїРѕС‚РµСЂРё EL Р·Р°РіСЂСѓР¶РµРЅРЅРѕРіРѕ РєСЂРµРґРёС‚РЅРѕРіРѕ РїРѕСЂС‚С„РµР»СЏ РЅР° РїРѕСЃР»РµРґРЅСЋСЋ РґР°С‚Сѓ? РЈРєР°Р¶Рё СЃСѓРјРјСѓ РІ СЂСѓР±Р»СЏС… Рё РёСЃС‚РѕС‡РЅРёРє MCP.")
    answer, usage = await llm_answer(session, [tool], request, Settings(), ForbiddenProvider())
    assert expected in answer
    assert "[T2]" in answer
    assert usage["llm_requests"] == 0
    assert [e["tool"] for e in session.trace] == ["get_dataset_context", "get_portfolio_summary"]

