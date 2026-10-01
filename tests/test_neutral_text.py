"""Agent-facing text must describe rules, not recommend strategies (DESIGN §1)."""
import importlib.util
import inspect
from pathlib import Path

import pytest

from agentciv import mcp_server
from agentciv.client import (ascii_map, describe_event, order_warnings, peace_deal_notes, summarize_view,
                             summarize_compact, view_alerts, view_changes)
from agentciv.engine import rulesdoc
from agentciv.server import guide

ROOT = Path(__file__).resolve().parent.parent

# Phrases that give advice or frame choices; extend when new copy is written.
ADVICE = [
    "strategy hint", "economy first", "defence is efficient", "you should", "should you", "watch your",
    "watch winter", "stop the leader", "trade surplus", "don't haggle", "worth to you", "whom to trust",
    "threats near", "cheap talk", "skill decides", "plan several turns", "expert", "don't overspend",
    "negotiate before", "useful to", "split big trades",
]


def _fog_summary() -> str:
    from test_fog_clients import fog_view
    _, view = fog_view()
    return summarize_view(view) + ascii_map(view)


def _llm_prompt() -> str:
    spec = importlib.util.spec_from_file_location("llm_agent", ROOT / "examples" / "llm_agent.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return "\n".join((module.SYSTEM_PROMPT, module.LIVE_DIPLOMACY, module.SYNC_DIPLOMACY,
                      module.TRACK_IDENTITY))


def _cli_summaries() -> str:
    from test_play_cli_summaries import alert_view
    view = alert_view()
    return "\n".join(view_alerts(view) + [summarize_compact(view), summarize_view(view)]
                     + order_warnings(view, [{"type": "market", "side": "sell", "resource": "food", "qty": 100}]))


TEXTS = {
    "rules": lambda: rulesdoc.render(),
    "api_index": lambda: str(guide.api_index("http://x")),
    "mcp_instructions": lambda: mcp_server.INSTRUCTIONS + mcp_server.ORDER_HELP,
    "llm_prompt": _llm_prompt,
    "agent_prompt_template": lambda: (ROOT / "examples" / "agent_prompt.md").read_text(),
    "client_fog_summary": _fog_summary,
    "play_cli_output_literals": lambda: (ROOT / "examples" / "play_cli.py").read_text(),
    "play_cli_summaries": _cli_summaries,
    "client_new_text_branches": lambda: "\n".join(inspect.getsource(f) for f in
        (view_alerts, view_changes, summarize_compact, describe_event, order_warnings, peace_deal_notes)),
}


@pytest.mark.parametrize("name", sorted(TEXTS))
def test_agent_facing_text_gives_no_advice(name):
    text = TEXTS[name]().lower()
    found = [p for p in ADVICE if p in text]
    assert not found, f"{name} contains advice phrases: {found}"
