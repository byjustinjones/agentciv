"""An LLM agent that plays AgentCiv through the HTTP API, using Claude with tool use.

    pip install anthropic            # the only extra dependency
    export ANTHROPIC_API_KEY=...     # or: ant auth login
    python -m agentciv.server        # in another terminal
    python examples/llm_agent.py --quickmatch --name Claude
    python examples/llm_agent.py --game g3 --model claude-sonnet-5-5 --effort low

How it works: every game turn starts a *fresh* short conversation (so context
never grows over a 150-turn game). The system prompt holds the full rules
(prompt-cached across turns); the user message holds a compact state summary
(including open deals, contracts and reputation), the ASCII map, what arrived
in the inbox and the agent's own notes from last turn. Claude may inspect the
full JSON state and **barter** — propose deals, counter/accept/reject offers,
send messages and wait a few seconds for replies (``wait_for_replies``) —
then calls ``submit_orders`` (it can resubmit to fix rejected orders). After
the orders are in, the agent keeps watching the inbox until the turn ends:
anything addressed to it (a new offer, a counter, a message) is handed back to
Claude in the same conversation so it can answer in real time. Its ``notes``
are carried to the next turn as memory.

Synchronous games (``"sync": true``; ``--sync`` asks quickmatch for one):
each turn is a few negotiation rounds, then an orders phase. Deal tools
queue their action for the end of the round; ``wait_for_replies`` ends the
agent's round and waits until every seat has ended it, then returns what
the round produced (results of its own actions, new offers and messages).
When the model stops talking, the remaining rounds are ended for it; once
the orders phase opens it is asked for its orders.

Model: ``--model`` or ``$AGENTCIV_MODEL`` (default: claude-opus-5-5).
Games with LLM players should use a generous ``turn_timeout`` (e.g. 120 s).

Provenance: the agent joins with a manifest (model, effort, harness, a sha256
of its prompt template and tool definitions, tools, memory) that the server
shows in the game summary and replay; ``$AGENTCIV_AGENT`` (a JSON object) adds
or overrides fields. Server-side refusal fallbacks are **off** unless
``--fallback`` is given, so every answer comes from the requested model.
``--log-dir DIR`` writes one JSON line per model call to
``DIR/<game>-<name>.jsonl`` (turn, requested model, the model that answered,
stop reason, token usage, latency, tool calls, refusal/fallback flags, API
errors) and a final ``summary`` line with totals.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))  # run from a checkout
from agentciv.client import (AgentCivClient, ApiError, agent_from_env, ascii_map, describe_event,  # noqa: E402
                             phase_result_lines, summarize_view)

HARNESS = "agentciv examples/llm_agent.py"
HARNESS_VERSION = "2"
DEFAULT_MODEL = "claude-opus-5-5"
# Models that accept server-side refusal fallbacks (fallbacks="default").
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-fable-5", "claude-sonnet-5-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"
DEADLINE_MARGIN = 3.0  # seconds kept free before the turn deadline

SYSTEM_PROMPT = """You are {name}, a player in AgentCiv, competing against other AI agents.
Your goal is to win (finish 1st). The victory conditions are conquest, wonder, influence, economic and
score; the rules below describe what is allowed.

Each turn you receive a state summary, an ASCII map, new diplomacy from your inbox and your notes from last
turn. Use get_full_state if you need exact details (tile owners, armies, improvements, deals). In games created
with fog: true, some fields of other players are null and armies are listed only within your sight
(rules §14).

{diplomacy}

Finally call submit_orders with ALL of this turn's orders (you may call it again to fix rejected orders or
after a deal changed your resources; the latest call replaces earlier ones), and put your plan for the next
turns in `notes`. Orders execute in the order given, paying costs when executed.

THE RULES:
{rules}"""

LIVE_DIPLOMACY = """Diplomacy is live: deals settle the moment they are accepted. Tools: propose_deal and respond_to_deal
(accept | reject | counter | withdraw), say for messages, and wait_for_replies to wait a few seconds for answers.
The turn resolves as soon as every player has submitted orders; diplomacy after that applies to the next turn."""

SYNC_DIPLOMACY = """This game is synchronous: each turn has {rounds} negotiation round(s), then an orders phase.
In a negotiation round, propose_deal, respond_to_deal (accept | reject | counter | withdraw) and say are queued,
not applied. wait_for_replies ends your round; when every player has ended it, all queued actions are applied in
the turn's rotating seat order and wait_for_replies returns the results of yours plus new offers and messages.
An accept settles at that point if the deal is still open and both sides can deliver. When you stop calling
tools, your remaining rounds end. After the last round diplomacy is closed and you submit orders."""

BUNDLE = {
    "type": "object",
    "description": 'resources {"food","wood","stone","gold"} (integers), "tiles": [[x,y],...] (owned non-city '
                   'tiles), and/or a contract "per_turn": {"gold": 5} with "turns": 1-30; with peace, "bond": banked '
                   'gold that side pledges on the treaty (rules §9). {} = nothing.',
    "properties": {
        "food": {"type": "integer"}, "wood": {"type": "integer"}, "stone": {"type": "integer"},
        "gold": {"type": "integer"},
        "tiles": {"type": "array", "items": {"type": "array", "items": {"type": "integer"}}},
        "per_turn": {"type": "object", "additionalProperties": {"type": "integer"}},
        "turns": {"type": "integer"},
        "bond": {"type": "integer"},
    },
}
DEAL_TERMS = {
    "give": {**BUNDLE, "description": "what YOU hand over. " + BUNDLE["description"]},
    "get": {**BUNDLE, "description": "what YOU receive. " + BUNDLE["description"]},
    "peace": {"type": "integer", "description": "optional: 20-40 turns of binding peace on acceptance, subject to "
                                               "treaty slots, cooldowns and bonds (rules §9)"},
    "message": {"type": "string", "description": "optional short note (<= 300 chars)"},
}

TOOLS = [
    {
        "name": "get_full_state",
        "description": "Return parts of the full, current JSON game view. Sections: you, players, map, cities, "
                       "armies, market, treaties, treaty_cooldowns, treaty_proposals, deals, contracts, messages, "
                       "events, victory, "
                       "costs.",
        "input_schema": {
            "type": "object",
            "properties": {"sections": {"type": "array", "items": {"type": "string"},
                                        "description": "which top-level sections to return"}},
            "required": ["sections"],
        },
    },
    {
        "name": "propose_deal",
        "description": "Propose a deal to another player now. It stays open (2 turns) until they accept, reject or "
                       "counter it. Example: to p2, give {\"wood\":60}, get {\"gold\":45}.",
        "input_schema": {"type": "object", "properties": {"to": {"type": "string"}, **DEAL_TERMS},
                         "required": ["to"]},
    },
    {
        "name": "respond_to_deal",
        "description": "Answer a deal: accept | reject | counter (deals proposed TO you; for counter give/get are "
                       "YOUR new terms from YOUR point of view) or withdraw (your own proposal).",
        "input_schema": {"type": "object",
                         "properties": {"deal": {"type": "string"},
                                        "response": {"type": "string",
                                                     "enum": ["accept", "reject", "counter", "withdraw"]},
                                        **DEAL_TERMS},
                         "required": ["deal", "response"]},
    },
    {
        "name": "say",
        "description": "Send a message now to a player id (private) or \"all\" (public).",
        "input_schema": {"type": "object", "properties": {"to": {"type": "string"}, "text": {"type": "string"}},
                         "required": ["to", "text"]},
    },
    {
        "name": "wait_for_replies",
        "description": "Wait up to `seconds` (max 20) for diplomacy addressed to you (answers to your offers, new "
                       "offers, messages) and return it. Use after proposing, before submitting orders. In a "
                       "synchronous game: end your negotiation round and wait for the round to close (`seconds` "
                       "is ignored), then return its results.",
        "input_schema": {"type": "object", "properties": {"seconds": {"type": "number"}}, "required": ["seconds"]},
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


def prompt_sha256() -> str:
    """sha256 of what this harness sends besides the game: the system prompt
    template (before the rules and name are filled in), both diplomacy
    paragraphs and the tool definitions."""
    blob = (SYSTEM_PROMPT + "\0" + LIVE_DIPLOMACY + "\0" + SYNC_DIPLOMACY + "\0"
            + json.dumps(TOOLS, sort_keys=True, separators=(",", ":")))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def agent_manifest(args, sdk_version: str | None = None) -> dict:
    """The manifest sent with join/quickmatch; ``$AGENTCIV_AGENT`` fields override."""
    out = {
        "model": args.model,
        "effort": args.effort,
        "harness": HARNESS,
        "harness_version": HARNESS_VERSION + (f"; anthropic {sdk_version}" if sdk_version else ""),
        "prompt_sha256": prompt_sha256(),
        "tools": ",".join(t["name"] for t in TOOLS),
        "memory": "fresh conversation each turn; own notes (<= 4000 chars) carried to the next turn",
        "notes": f"refusal fallbacks {'on' if args.fallback else 'off'}; max {args.max_steps} model calls per turn",
    }
    out.update(agent_from_env() or {})
    return out


def _get(obj, name, default=None):
    """Attribute or dict key (SDK objects and plain dicts alike)."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


class CallLog:
    """Per-call JSONL log of the model calls (``--log-dir``). ``path`` None:
    totals are still kept (for the summary), nothing is written."""

    USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")

    def __init__(self, path: str | None = None):
        self.path = path
        self.calls = 0
        self.errors = 0
        self.refusals = 0
        self.fallbacks = 0
        self.other_model = 0          # calls answered by a model other than the requested one
        self.models: dict[str, int] = {}
        self.usage = {k: 0 for k in self.USAGE_FIELDS}
        self.latency = 0.0
        self.tool_calls = 0

    def _write(self, rec: dict) -> None:
        if self.path:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, separators=(",", ":"), default=str) + "\n")

    @staticmethod
    def fallback_used(response) -> bool:
        """True if a server-side fallback model ran for this response: a
        ``fallback`` content block, or a ``fallback_message`` usage iteration."""
        if any(_get(b, "type") == "fallback" for b in _get(response, "content") or []):
            return True
        iterations = _get(_get(response, "usage"), "iterations") or []
        return any(_get(it, "type") == "fallback_message" for it in iterations)

    def record(self, turn, requested: str, response, latency: float) -> dict:
        usage_obj = _get(response, "usage")
        usage = {k: _get(usage_obj, k) for k in self.USAGE_FIELDS}
        model = _get(response, "model")
        stop = _get(response, "stop_reason")
        tools = [_get(b, "name") for b in _get(response, "content") or [] if _get(b, "type") == "tool_use"]
        fallback = self.fallback_used(response)
        different = bool(model) and model != requested
        rec = {"type": "call", "time": round(time.time(), 3), "turn": turn, "requested_model": requested,
               "model": model, "stop_reason": stop, "usage": usage, "latency_s": round(latency, 3),
               "tool_calls": tools, "refusal": stop == "refusal", "fallback": fallback,
               "different_model": different}
        self.calls += 1
        self.refusals += stop == "refusal"
        self.fallbacks += fallback
        self.other_model += different
        if model:
            self.models[model] = self.models.get(model, 0) + 1
        for k, v in usage.items():
            if isinstance(v, int):
                self.usage[k] += v
        self.latency += latency
        self.tool_calls += len(tools)
        self._write(rec)
        return rec

    def record_error(self, turn, requested: str, exc: BaseException, latency: float) -> dict:
        rec = {"type": "call", "time": round(time.time(), 3), "turn": turn, "requested_model": requested,
               "latency_s": round(latency, 3),
               "error": {"type": type(exc).__name__, "status": getattr(exc, "status_code", None),
                         "message": str(getattr(exc, "message", exc))[:1000]}}
        self.calls += 1
        self.errors += 1
        self.latency += latency
        self._write(rec)
        return rec

    def summary(self, **extra) -> dict:
        rec = {"type": "summary", "time": round(time.time(), 3), "calls": self.calls, "errors": self.errors,
               "refusals": self.refusals, "fallback_calls": self.fallbacks,
               "calls_answered_by_other_model": self.other_model,
               "any_call_answered_by_other_model": self.other_model > 0 or self.fallbacks > 0,
               "models": dict(self.models), "usage": dict(self.usage), "latency_s": round(self.latency, 3),
               "tool_calls": self.tool_calls, **extra}
        self._write(rec)
        return rec


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
        self.calls = CallLog()
        self.turn = None  # the game turn being played (for the call log)
        self.cur = None   # synchronous games: the latest view (its phase and phase deadline)

    # ------------------------------------------------------------ Claude call
    def create(self, messages: list):
        t0 = time.monotonic()
        try:
            response = self._create(messages)
        except Exception as e:
            self.calls.record_error(self.turn, self.args.model, e, time.monotonic() - t0)
            raise
        self.calls.record(self.turn, self.args.model, response, time.monotonic() - t0)
        return response

    def _create(self, messages: list):
        kwargs = dict(
            model=self.args.model,
            max_tokens=16000,
            system=self.system,
            tools=TOOLS,
            messages=messages,
            thinking={"type": "adaptive"},
            output_config={"effort": self.args.effort},
        )
        if self.args.fallback and self.args.model in FALLBACK_MODELS:
            # Opt-in server-side refusal fallback: a declined request is re-run on a fallback model,
            # so some turns may be played by another model (the call log records which).
            return self.llm.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
        return self.llm.messages.create(**kwargs)

    # ------------------------------------------------------------ tools
    def diplomacy(self, action: dict, turn: int) -> tuple[str, bool]:
        ph = (self.cur or {}).get("phase")
        try:
            res = self.game.diplomacy([action], turn=turn, phase=ph["id"] if ph else None)
        except ApiError as e:
            return f"failed: {e.message}", True
        r = (res.get("results") or [{}])[0]
        if not r.get("ok"):
            return f"rejected: {r.get('error')}", True
        if r.get("status") == "queued":
            p = res.get("phase") or {}
            return (f"queued for the end of negotiation round {p.get('round')} of {p.get('of')}; "
                    "wait_for_replies ends your round and returns the results"), False
        extra = f" (replaces {r['countered']})" if r.get("countered") else ""
        return "ok" + (f", deal {r['deal']}{extra}" if r.get("deal") else "") + \
            (f", {r['status']}" if r.get("status") else ""), False

    @staticmethod
    def terms(action: dict, args: dict) -> dict:
        action["give"] = args.get("give") or {}
        action["get"] = args.get("get") or {}
        for k in ("peace", "message"):
            if args.get(k):
                action[k] = args[k]
        return action

    def run_tool(self, name: str, args: dict, view: dict) -> tuple[str, bool]:
        turn = view["turn"]
        if name == "get_full_state":
            try:
                fresh = self.game.state()  # deals may have changed since the turn started
            except (ApiError, OSError):
                fresh = view
            sections = args.get("sections") or []
            return json.dumps({k: fresh.get(k) for k in sections if k in fresh}, separators=(",", ":")), False
        if name == "propose_deal":
            return self.diplomacy(self.terms({"type": "propose", "to": args.get("to")}, args), turn)
        if name == "respond_to_deal":
            resp = args.get("response")
            if resp == "counter":
                return self.diplomacy(self.terms({"type": "counter", "deal": args.get("deal")}, args), turn)
            action = {"type": resp, "deal": args.get("deal")}
            if resp == "reject" and args.get("message"):
                action["message"] = args["message"]
            return self.diplomacy(action, turn)
        if name == "say":
            return self.diplomacy({"type": "say", "to": args.get("to"), "text": args.get("text")}, turn)
        if name == "wait_for_replies" and self.cur is not None:
            return self.end_round_and_wait(turn)
        if name == "wait_for_replies":
            try:
                seconds = float(args.get("seconds") or 10)
            except (TypeError, ValueError):
                seconds = 10.0
            seconds = max(0.0, min(seconds, 20.0, self.time_left(view) - DEADLINE_MARGIN))
            box = self.game.inbox(timeout=seconds, turn=turn)
            if box.get("turn") != turn or box.get("status") != "running":
                return "The turn is already over.", True
            lines = self.describe(box)
            return "\n".join(lines) if lines else f"Nothing new after {seconds:.0f}s.", False
        if name == "submit_orders":
            orders = args.get("orders")
            if not isinstance(orders, list):
                return "orders must be an array", True
            if isinstance(args.get("notes"), str):
                self.notes = args["notes"][:4000]
            before = []
            if self.cur is not None and (self.cur.get("phase") or {}).get("kind") == "negotiate":
                before = self.finish_negotiation(turn)  # orders open after the last round
                if self.cur is None or self.cur.get("turn") != turn:
                    return "The turn is already over.", True
            try:
                res = self.game.submit_orders(orders, turn=turn)
            except ApiError as e:
                return f"submit failed: {e.message}", True
            lines = before + [f"{res['accepted']} accepted, {len(res['errors'])} rejected."]
            lines += [f"order #{e['index']}: {e['error']}" for e in res["errors"]]
            return "\n".join(lines), False
        return f"unknown tool {name}", True

    def describe(self, box: dict) -> list[str]:
        pid = self.game.player_id
        return [describe_event(e, pid) for e in box.get("items") or []]

    @staticmethod
    def time_left(view: dict) -> float:
        return view["deadline"] - time.time() if view.get("deadline") else float("inf")

    # ------------------------------------------------------------ synchronous games
    def end_round_and_wait(self, turn: int) -> tuple[str, bool]:
        """End our negotiation round, wait for its barrier, describe what it produced."""
        ph = (self.cur or {}).get("phase") or {}
        if ph.get("kind") != "negotiate":
            return "Diplomacy is closed in the orders phase; call submit_orders.", True
        seq = self.game.inbox_seq
        if not ph.get("you_done"):
            try:
                self.game.end_round(phase=ph["id"], turn=turn)
            except ApiError as e:
                if e.status != 409:  # 409: the round already closed
                    return f"failed: {e.message}", True
        view = self.wait_phase_change(ph["id"])
        if view.get("turn") != turn or view.get("status") != "running":
            return "The turn is already over.", True
        lines = [f"Negotiation round {ph.get('round')} of {ph.get('of')} closed."]
        mine = phase_result_lines(view.get("phase") or {}, {ph.get("round")})
        if mine:
            lines += ["Your actions:"] + mine
        try:
            box = self.game.inbox(since=seq, timeout=0)
            news = self.describe(box)
        except (ApiError, OSError):
            news = []
        if news:
            lines += ["New diplomacy:"] + [f"  {x}" for x in news]
        nxt = view.get("phase") or {}
        if nxt.get("kind") == "negotiate":
            lines.append(f"Now: negotiation round {nxt.get('round')} of {nxt.get('of')} (your open deals are in "
                         "get_full_state 'deals').")
        else:
            lines.append("Negotiation is over for this turn: call submit_orders.")
        return "\n".join(lines), False

    def wait_phase_change(self, phase_id: int) -> dict:
        """Long-poll until another phase is open (or the game moved on); returns the fresh view."""
        while True:
            try:
                w = self.game.wait_phase(phase_id, timeout=30)
            except (ApiError, OSError):
                time.sleep(1)
                continue
            if w.get("status") != "running" or (w.get("phase") or {}).get("id") != phase_id:
                break
        self.cur = self.game.state()
        return self.cur

    def finish_negotiation(self, turn: int) -> list[str]:
        """End every remaining negotiation round of ``turn``; returns result lines for the model."""
        lines: list[str] = []
        while self.cur is not None and self.cur.get("turn") == turn and self.cur.get("status") == "running":
            ph = self.cur.get("phase") or {}
            if ph.get("kind") != "negotiate":
                break
            text, _ = self.end_round_and_wait(turn)
            lines += [x for x in text.splitlines() if not x.startswith("Now:")]
        return lines

    def play_turn_sync(self, view: dict) -> None:
        pid = view["you"]["id"]
        turn = view["turn"]
        self.turn = turn
        self.cur = view
        ph = view["phase"]
        try:
            inbox = self.describe(self.game.inbox(timeout=0))
        except (ApiError, OSError):
            inbox = []
        news = "\n".join(inbox[-30:]) or "(nothing new)"
        step = (f"Negotiation round {ph.get('round')} of {ph.get('of')} is open: queue diplomacy if useful and "
                "call wait_for_replies to end the round, or call submit_orders." if ph.get("kind") == "negotiate"
                else "Negotiation is over for this turn: call submit_orders.")
        prompt = (f"Turn {turn} of {view['max_turns']}.\n\nYOUR NOTES FROM LAST TURN:\n{self.notes}\n\n"
                  f"NEW DIPLOMACY (inbox):\n{news}\n\nSTATE:\n{summarize_view(view, pid)}\n\n"
                  f"MAP:\n{ascii_map(view, pid)}\n\n{step}")
        messages = [{"role": "user", "content": prompt}]
        steps = self.args.max_steps
        used, submitted = self.converse(messages, view, steps)
        steps -= used
        if not submitted:
            lines = self.finish_negotiation(turn)
            if self.cur is not None and self.cur.get("turn") == turn and self.cur.get("status") == "running":
                if steps > 0:
                    messages.append({"role": "user", "content": "\n".join(
                        lines + ["Negotiation is over for this turn: call submit_orders with all of this "
                                 "turn's orders."])})
                    _, submitted = self.converse(messages, view, steps)
                if not submitted:
                    try:
                        self.game.submit_orders([], turn=turn)  # don't hold the game up
                    except ApiError:
                        pass

    # ------------------------------------------------------------ one turn
    def converse(self, messages: list, view: dict, steps: int) -> tuple[int, bool]:
        """Let Claude call tools until it stops. Returns (steps used, orders submitted)."""
        submitted = False
        used = 0
        while used < steps:
            if self.time_left(self.cur if self.cur is not None else view) < DEADLINE_MARGIN:
                print("  (turn deadline passed)", file=sys.stderr)
                break
            used += 1
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
            clean = False
            for block in tool_uses:
                text, is_error = self.run_tool(block.name, block.input if isinstance(block.input, dict) else {},
                                               view)
                if block.name == "submit_orders" and not is_error:
                    submitted = True
                    clean = " 0 rejected" in text.splitlines()[0]
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": text,
                                "is_error": is_error})
                if block.name in ("submit_orders", "propose_deal", "respond_to_deal"):
                    print(f"  {block.name}: {text.splitlines()[0]}", file=sys.stderr)
            messages.append({"role": "user", "content": results})
            if clean and all(b.name == "submit_orders" for b in tool_uses):
                break  # every order accepted: no need for another round trip
        return used, submitted

    def play_turn(self, view: dict) -> None:
        if view.get("phase"):
            return self.play_turn_sync(view)
        pid = view["you"]["id"]
        turn = view["turn"]
        self.turn = turn
        # what arrived since we last looked (answers to last turn's offers, messages...)
        try:
            inbox = self.describe(self.game.inbox(timeout=0))
        except (ApiError, OSError):
            inbox = []
        news = "\n".join(inbox[-30:]) or "(nothing new)"
        prompt = (f"Turn {turn} of {view['max_turns']}.\n\nYOUR NOTES FROM LAST TURN:\n{self.notes}\n\n"
                  f"NEW DIPLOMACY (inbox):\n{news}\n\nSTATE:\n{summarize_view(view, pid)}\n\n"
                  f"MAP:\n{ascii_map(view, pid)}\n\n"
                  "Negotiate if useful, then call submit_orders.")
        messages = [{"role": "user", "content": prompt}]
        steps = self.args.max_steps
        used, submitted = self.converse(messages, view, steps)
        steps -= used
        if not submitted:
            try:
                self.game.submit_orders([], turn=turn)  # don't hold the game up
            except ApiError:
                pass
        # Orders are in; until the turn ends, answer whatever is addressed to us in real time.
        while steps > 0 and self.time_left(view) > DEADLINE_MARGIN:
            try:
                box = self.game.inbox(timeout=min(30.0, self.time_left(view) - DEADLINE_MARGIN), turn=turn)
            except (ApiError, OSError):
                break
            if box.get("turn") != turn or box.get("status") != "running":
                break
            mine = [e for e in box.get("items") or [] if pid in (e.get("to"), e.get("from"))
                    and e.get("type") in ("deal_proposed", "deal_countered", "deal_executed", "deal_rejected",
                                          "deal_failed", "say")]
            if not mine:
                continue
            lines = [describe_event(e, pid) for e in mine]
            messages.append({"role": "user", "content":
                             "Your orders are submitted, but the turn is still open. New diplomacy:\n"
                             + "\n".join(lines) + "\nRespond if useful (respond_to_deal / propose_deal / say). "
                             "If a deal changed your resources, resubmit your full orders; otherwise just stop."})
            print(f"  inbox: {lines[0]}" + (f" (+{len(lines) - 1})" if len(lines) > 1 else ""), file=sys.stderr)
            used, _ = self.converse(messages, view, steps)
            steps -= max(1, used)

    # ------------------------------------------------------------ game loop
    def run(self) -> None:
        a = self.args
        try:
            agent = agent_manifest(a, getattr(self.anthropic, "__version__", None))
        except ValueError as e:
            fail(f"AGENTCIV_AGENT: {e}")
        if a.game:
            joined = self.game.join(a.game, a.name, agent=agent)
        else:
            qm = {"sync": True} if getattr(a, "sync", False) else {}
            joined = self.game.quickmatch(a.name, players=a.players, turn_timeout=a.turn_timeout, agent=agent, **qm)
        print(f"Joined {joined['game_id']} as {joined['player_id']} with model {a.model}"
              f"{' (refusal fallbacks on)' if a.fallback else ''}", file=sys.stderr)
        if a.log_dir:
            os.makedirs(a.log_dir, exist_ok=True)
            safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in a.name)
            self.calls.path = os.path.join(a.log_dir, f"{joined['game_id']}-{safe}.jsonl")
            print(f"Logging model calls to {self.calls.path}", file=sys.stderr)
        info = self.game.game()
        diplomacy = (SYNC_DIPLOMACY.format(rounds=info.get("negotiation_rounds")) if info.get("sync")
                     else LIVE_DIPLOMACY)
        self.system = [{"type": "text", "text": SYSTEM_PROMPT.format(name=a.name, rules=self.game.rules(),
                                                                     diplomacy=diplomacy),
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
        totals = self.calls.summary(game_id=self.game.game_id, player_id=me, requested_model=a.model,
                                    fallback_enabled=bool(a.fallback),
                                    place=places.index(me) + 1 if me in places else None)
        if totals["any_call_answered_by_other_model"]:
            print(f"note: {totals['calls_answered_by_other_model']} call(s) were answered by a model other than "
                  f"{a.model} ({totals['fallback_calls']} via fallback)", file=sys.stderr)
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
    ap.add_argument("--sync", action="store_true", help="quickmatch: join a synchronous lobby")
    ap.add_argument("--max-steps", type=int, default=10, help="max model calls per game turn (incl. negotiation)")
    ap.add_argument("--fallback", action="store_true",
                    help="opt in to server-side refusal fallbacks (a declined request is re-run on another model; "
                         "off by default so every call is answered by --model)")
    ap.add_argument("--no-fallback", action="store_true", help=argparse.SUPPRESS)  # old flag: now the default
    ap.add_argument("--log-dir", default=os.environ.get("AGENTCIV_LOG_DIR") or None,
                    help="write a JSONL log of every model call to DIR/<game>-<name>.jsonl (default $AGENTCIV_LOG_DIR)")
    LLMAgent(ap.parse_args()).run()


if __name__ == "__main__":
    main()
