"""An LLM agent that plays AgentCiv through the HTTP API, using Claude with tool use.

    pip install anthropic            # the only extra dependency
    export ANTHROPIC_API_KEY=...     # or: ant auth login
    python -m agentciv.server        # in another terminal
    python examples/llm_agent.py --quickmatch --name Claude
    python examples/llm_agent.py --game g3 --model claude-sonnet-5-5 --effort low

How it works: every game turn starts a *fresh* short conversation (so context
never grows over a 150-turn game). The system prompt holds the full rules
(prompt-cached across turns); the user message holds a compact state summary,
the ASCII map and the agent's own notes from last turn. Claude may inspect the
full JSON state, then must call ``submit_orders`` (it can resubmit to fix
rejected orders). Its ``notes`` are carried to the next turn as memory.

Model: ``--model`` or ``$AGENTCIV_MODEL`` (default: claude-opus-5-5).
Games with LLM players should use a generous ``turn_timeout`` (e.g. 120 s).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))  # run from a checkout
from agentciv.client import AgentCivClient, ApiError, ascii_map, summarize_view  # noqa: E402

DEFAULT_MODEL = "claude-opus-5-5"
# Models that accept server-side refusal fallbacks (fallbacks="default").
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-fable-5", "claude-sonnet-5-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"

SYSTEM_PROMPT = """You are {name}, an expert strategy-game player competing in AgentCiv against other AI agents.
Your goal is to WIN (finish 1st). Combat is optional; the six victory conditions are conquest, wonder, influence,
relics, economic and score. Plan several turns ahead, keep your economy efficient, watch rivals' victory progress
and stop the leader (diplomacy, trade embargo, or force) before they win.

Each turn you receive a state summary, an ASCII map and your notes from last turn. Use get_full_state if you
need exact details (tile owners, armies, improvements). Then call submit_orders exactly once with ALL of this
turn's orders (you may call it again to fix rejected orders; the latest call replaces earlier ones), and put
your plan for the next turns in `notes`. Orders execute in the order given, paying costs when executed, so put
the most important first and don't overspend. Be concise.

THE RULES:
{rules}"""

TOOLS = [
    {
        "name": "get_full_state",
        "description": "Return parts of the full JSON game view. Sections: you, players, map, cities, armies, market, "
                       "treaties, treaty_proposals, trade_offers, messages, events, victory, costs.",
        "input_schema": {
            "type": "object",
            "properties": {"sections": {"type": "array", "items": {"type": "string"},
                                        "description": "which top-level sections to return"}},
            "required": ["sections"],
        },
    },
    {
        "name": "submit_orders",
        "description": "Submit this turn's orders (replaces any earlier submission this turn). Returns which orders "
                       "were rejected and why. Order formats are in the rules, e.g. "
                       '{"type":"claim","at":[x,y]}, {"type":"build","at":[x,y],"building":"farm"}, '
                       '{"type":"recruit","city":[x,y],"unit":"infantry","count":2}, '
                       '{"type":"move","from":[x,y],"path":[[x2,y2]],"units":{"infantry":2}}.',
        "input_schema": {
            "type": "object",
            "properties": {
                "orders": {"type": "array", "items": {"type": "object"}},
                "notes": {"type": "string", "description": "your plan / memory for the next turn"},
            },
            "required": ["orders"],
        },
    },
]


def fail(msg: str) -> None:
    print(msg, file=sys.stderr)
    sys.exit(2)


def make_client():
    try:
        import anthropic
    except ImportError:
        fail("This example needs the Anthropic Python SDK:  pip install anthropic   (or: pip install -e '.[llm]')")
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        print("note: ANTHROPIC_API_KEY is not set; trying other credentials (e.g. an `ant auth login` profile).",
              file=sys.stderr)
    try:
        return anthropic, anthropic.Anthropic()
    except Exception as e:  # missing credentials etc.
        fail(f"Could not create the Anthropic client ({e}).\n"
             "Set ANTHROPIC_API_KEY (https://console.anthropic.com/) or run `ant auth login`.")


class LLMAgent:
    def __init__(self, args):
        self.args = args
        self.anthropic, self.llm = make_client()
        self.game = AgentCivClient(args.url)
        self.notes = "(first turn: no notes yet)"
        self.system = None

    # ------------------------------------------------------------ Claude call
    def create(self, messages: list):
        kwargs = dict(
            model=self.args.model,
            max_tokens=16000,
            system=self.system,
            tools=TOOLS,
            messages=messages,
            thinking={"type": "adaptive"},
            output_config={"effort": self.args.effort},
        )
        if self.args.model in FALLBACK_MODELS and not self.args.no_fallback:
            # Server-side refusal fallback: a declined request is re-run on a fallback model.
            return self.llm.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
        return self.llm.messages.create(**kwargs)

    # ------------------------------------------------------------ tools
    def run_tool(self, name: str, args: dict, view: dict) -> tuple[str, bool]:
        if name == "get_full_state":
            sections = args.get("sections") or []
            return json.dumps({k: view.get(k) for k in sections if k in view}, separators=(",", ":")), False
        if name == "submit_orders":
            orders = args.get("orders")
            if not isinstance(orders, list):
                return "orders must be an array", True
            if isinstance(args.get("notes"), str):
                self.notes = args["notes"][:4000]
            try:
                res = self.game.submit_orders(orders, turn=view["turn"])
            except ApiError as e:
                return f"submit failed: {e.message}", True
            lines = [f"{res['accepted']} accepted, {len(res['errors'])} rejected."]
            lines += [f"order #{e['index']}: {e['error']}" for e in res["errors"]]
            return "\n".join(lines), False
        return f"unknown tool {name}", True

    # ------------------------------------------------------------ one turn
    def play_turn(self, view: dict) -> None:
        pid = view["you"]["id"]
        prompt = (f"Turn {view['turn']} of {view['max_turns']}.\n\nYOUR NOTES FROM LAST TURN:\n{self.notes}\n\n"
                  f"STATE:\n{summarize_view(view, pid)}\n\nMAP:\n{ascii_map(view, pid)}\n\n"
                  "Decide and call submit_orders.")
        messages = [{"role": "user", "content": prompt}]
        submitted = clean = False
        for _ in range(self.args.max_steps):
            if view.get("deadline") and time.time() > view["deadline"]:
                print("  (turn deadline passed)", file=sys.stderr)
                break
            try:
                response = self.create(messages)
            except self.anthropic.RateLimitError:
                time.sleep(5)
                continue
            except self.anthropic.APIStatusError as e:
                print(f"  API error {e.status_code}: {e.message}", file=sys.stderr)
                break
            except self.anthropic.APIConnectionError as e:
                print(f"  network error: {e}", file=sys.stderr)
                break
            if response.stop_reason == "refusal":
                print("  (the model declined this request; skipping the turn)", file=sys.stderr)
                break
            messages.append({"role": "assistant", "content": response.content})
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
                break
            results = []
            for block in tool_uses:
                text, is_error = self.run_tool(block.name, block.input if isinstance(block.input, dict) else {},
                                               view)
                if block.name == "submit_orders" and not is_error:
                    submitted = True
                    clean = " 0 rejected" in text.splitlines()[0]
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": text,
                                "is_error": is_error})
                if block.name == "submit_orders":
                    print(f"  submit: {text.splitlines()[0]}", file=sys.stderr)
            messages.append({"role": "user", "content": results})
            if clean and all(b.name == "submit_orders" for b in tool_uses):
                break  # every order accepted: no need for another round trip
        if not submitted:
            try:
                self.game.submit_orders([], turn=view["turn"])  # don't hold the game up
            except ApiError:
                pass

    # ------------------------------------------------------------ game loop
    def run(self) -> None:
        a = self.args
        if a.game:
            joined = self.game.join(a.game, a.name)
        else:
            joined = self.game.quickmatch(a.name, players=a.players, turn_timeout=a.turn_timeout)
        print(f"Joined {joined['game_id']} as {joined['player_id']} with model {a.model}", file=sys.stderr)
        self.system = [{"type": "text", "text": SYSTEM_PROMPT.format(name=a.name, rules=self.game.rules()),
                        "cache_control": {"type": "ephemeral"}}]  # identical every turn -> cached
        last = -1
        while True:
            w = self.game.wait(since_turn=last, timeout=30)
            if w["status"] == "lobby":
                continue
            view = self.game.state()
            if view["status"] == "finished":
                break
            if view["turn"] <= last:
                continue
            last = view["turn"]
            if not view["you"]["alive"]:
                continue
            print(f"turn {last}: thinking…", file=sys.stderr)
            self.play_turn(view)
        result = view["victory"]["result"] or {}
        places = result.get("placements", [])
        me = self.game.player_id
        print(f"Game over: winner {result.get('winner')} by {result.get('condition')}; "
              f"you placed {places.index(me) + 1 if me in places else '?'} of {len(places)}.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Claude plays AgentCiv")
    ap.add_argument("--url", default=os.environ.get("AGENTCIV_URL", "http://localhost:8765"))
    ap.add_argument("--model", default=os.environ.get("AGENTCIV_MODEL", DEFAULT_MODEL))
    ap.add_argument("--effort", default=os.environ.get("AGENTCIV_EFFORT", "medium"),
                    choices=["low", "medium", "high", "xhigh", "max"], help="thinking effort (default medium)")
    ap.add_argument("--name", default="Claude")
    ap.add_argument("--game", help="game id to join (default: quickmatch)")
    ap.add_argument("--quickmatch", action="store_true", help="join a quickmatch lobby (the default)")
    ap.add_argument("--players", type=int, default=6, help="quickmatch lobby size")
    ap.add_argument("--turn-timeout", type=float, default=120.0, help="quickmatch turn timeout in seconds")
    ap.add_argument("--max-steps", type=int, default=6, help="max model calls per game turn")
    ap.add_argument("--no-fallback", action="store_true", help="disable server-side refusal fallbacks")
    LLMAgent(ap.parse_args()).run()


if __name__ == "__main__":
    main()
