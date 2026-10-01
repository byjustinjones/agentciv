"""The puzzle framework: :class:`Puzzle` (a saved position with an
objective and a 0-100 score), :class:`PuzzleGame` (the engine game it is
played in) and :class:`ScriptedBot` (fixed opponents).

A puzzle game is an ordinary :class:`~agentciv.engine.game.Game` whose map
is replaced by a hand-made position when it starts. Nothing in the rules
changes: the solver plays it with the normal state view and orders, and the
other seats are deterministic in-process bots. The only additions are

* a ``puzzle`` block in every view (id, title, objective, horizon, how the
  score is computed and, once the game is over, the score);
* :meth:`Puzzle.observe`, called after every resolved turn with the turn's
  events, so a puzzle can count things the final state no longer shows
  (starvation, a broken streak);
* the score, computed once when the game finishes and stored on the game
  (so it survives a checkpoint like the rest of the state).

Everything a puzzle keeps lives on the game object (``puzzle_state``,
``puzzle_outcome``), so pickling the game (server checkpoints) keeps it.
"""
from __future__ import annotations

from ..bots.base import Bot
from ..engine import constants as C
from ..engine.game import Game, GameConfig

SOLVER = "solver"

# Diplomacy actions; puzzle opponents never send them (see ScriptedBot).
DEAL_ORDER_TYPES = frozenset({"propose", "counter", "accept", "reject", "withdraw",
                              "offer_trade", "accept_trade"})


class Puzzle:
    """One diagnostic position. Subclasses set the class attributes and
    implement :meth:`build`, :meth:`score` and :meth:`solution`.

    ``roles`` lists the seats in seat order as ``(role, player name)``; the
    role ``"solver"`` is the one remote/test seat, every other role gets the
    bot returned by :meth:`opponent`. The solver's seat is always the last
    one (on the server the house seats are created with the game and the
    solver joins last, so the offline runner uses the same order).
    """

    id = "puzzle"
    title = "Puzzle"
    objective = ""
    scoring = ""
    seed = 1                    # map seed (the map is wiped; it only fixes the relic ring)
    start_turn = 0
    horizon = 10                # turns the solver plays: start_turn .. start_turn + horizon - 1
    roles: tuple = ((SOLVER, "Solver"),)
    fill = "m"                  # terrain of the wiped map before build() carves the position

    @property
    def max_turns(self) -> int:
        return self.start_turn + self.horizon

    @property
    def last_turn(self) -> int:
        return self.start_turn + self.horizon - 1

    def info(self) -> dict:
        """The static part of the ``puzzle`` block."""
        return {"puzzle": self.id, "title": self.title, "objective": self.objective,
                "horizon": self.horizon, "start_turn": self.start_turn, "last_turn": self.last_turn,
                "scoring": self.scoring}

    # ------------------------------------------------------------ to implement
    def build(self, g: "PuzzleGame", pids: dict) -> None:  # pragma: no cover - abstract
        """Lay out the position on the wiped map (``pids``: role -> player id)."""
        raise NotImplementedError

    def opponent(self, role: str) -> Bot:
        """A fresh bot for the house seat ``role`` (fixed seed: deterministic)."""
        return ScriptedBot()

    def observe(self, g: "PuzzleGame", events: list) -> None:
        """Called after every resolved turn with that turn's events."""

    def score(self, g: "PuzzleGame") -> tuple[int, str]:  # pragma: no cover - abstract
        """``(score 0-100, one-line explanation)`` of a finished game."""
        raise NotImplementedError

    def solution(self) -> Bot:  # pragma: no cover - abstract
        """The shipped reference solution (scores >= 90)."""
        raise NotImplementedError

    def baseline(self) -> Bot:
        """The do-nothing baseline."""
        from ..bots.base import IdleBot
        return IdleBot()

    # ------------------------------------------------------------ helpers
    def pids(self) -> dict:
        return {role: f"p{k + 1}" for k, (role, _) in enumerate(self.roles)}

    def solver_pid(self) -> str:
        return self.pids()[SOLVER]


def clamp_score(x: float) -> int:
    return int(max(0, min(100, round(x))))


# ---------------------------------------------------------------- the game
class PuzzleGame(Game):
    """A game that starts from a puzzle's position (see the module docstring)."""

    def __init__(self, puzzle_id: str, game_id: str = "puzzle", name: str | None = None):
        from . import get_puzzle
        pz = get_puzzle(puzzle_id)
        super().__init__(GameConfig(seed=pz.seed, max_turns=pz.max_turns, game_id=game_id,
                                    name=name or f"Puzzle: {pz.title}", max_players=len(pz.roles)))
        self.puzzle_id = pz.id
        self.puzzle_state: dict = {}
        self.puzzle_outcome: dict | None = None   # {"score", "explanation"} once finished

    @property
    def puzzle(self) -> Puzzle:
        from . import get_puzzle
        return get_puzzle(self.puzzle_id)

    def start(self) -> None:
        pz = self.puzzle
        if len(self.players) != len(pz.roles):
            raise RuntimeError(f"puzzle {pz.id} needs exactly {len(pz.roles)} players")
        super().start()
        wipe(self, pz.fill)
        self.turn = pz.start_turn
        self.market_history = [{"turn": self.turn - 1, "prices": self._prices()}]
        pz.build(self, pz.pids())
        self._invalidate()

    def step(self) -> list:
        if self.status != "running":
            return []
        events = super().step()
        pz = self.puzzle
        pz.observe(self, events)
        if self.status == "finished" and self.puzzle_outcome is None:
            try:
                score, why = pz.score(self)
            except Exception as e:  # pragma: no cover - a scoring bug must not take the game down
                score, why = 0, f"scoring failed ({type(e).__name__}: {e})"
            self.puzzle_outcome = {"score": clamp_score(score), "explanation": str(why)}
        return events

    def puzzle_block(self) -> dict:
        out = self.puzzle.info()
        out["solver"] = self.puzzle.solver_pid()
        oc = self.puzzle_outcome or {}
        out["score"] = oc.get("score")
        out["explanation"] = oc.get("explanation")
        return out

    def player_view(self, pid: str) -> dict:
        view = super().player_view(pid)
        view["puzzle"] = self.puzzle_block()
        return view

    def spectator_view(self, full: bool = False) -> dict:
        view = super().spectator_view(full)
        view["puzzle"] = self.puzzle_block()
        return view


def wipe(g: Game, fill: str = "m") -> None:
    """Clear a started game's map like :func:`agentciv.engine.testing.sandbox`,
    but fill it with ``fill`` and drop the relics too."""
    size = g.width * g.height
    g.terrain = [fill] * size
    g.owner = [None] * size
    g.improvement = [None] * size
    g.deposits = [C.DEPOSITS[fill][1] if fill in C.DEPOSITS else 0] * size
    g.relics = []
    g.relic_set = frozenset()
    g.cities.clear()
    g.armies.clear()
    for p in g.players:
        p.tiles = 0
        p.capital = None
        p.city_counter = 0
    g._invalidate()


def paint(g: Game, x0: int, y0: int, rows: list) -> None:
    """Set terrain from ``rows`` (strings of terrain characters; ``" "``
    leaves a tile as it is) with the top-left corner at (x0, y0)."""
    for dy, row in enumerate(rows):
        for dx, ch in enumerate(row):
            if ch == " ":
                continue
            i = g.idx(x0 + dx, y0 + dy)
            g.terrain[i] = ch
            g.deposits[i] = C.DEPOSITS[ch][1] if ch in C.DEPOSITS else 0
    g._invalidate()


def set_player(g: Game, pid: str, **fields) -> None:
    """Set a player's resources (``food=..``, ``gold=..``) and attributes
    (``bank``, ``legacy``, ``economic_streak``, ...)."""
    p = g.player(pid)
    for k, v in fields.items():
        if k in C.RESOURCES:
            p.resources[k] = v
        else:
            setattr(p, k, v)
    g._invalidate()


# ---------------------------------------------------------------- opponents
class ScriptedBot(Bot):
    """A puzzle opponent with a fixed script: never negotiates (so nothing
    depends on when the server runs its negotiation rounds), and ``act``
    returns ``script(view)``. Default: no orders."""

    name = "scripted"

    def __init__(self, script=None, seed: int = 0):
        super().__init__(seed)
        self.script = script

    def act(self, view: dict) -> list:
        if self.script is None or not view or view.get("status") != "running":
            return []
        return [o for o in (self.script(view) or []) if o.get("type") not in DEAL_ORDER_TYPES]


class QuietBot(Bot):
    """Wraps a house bot (fixed seed) as a puzzle opponent that never
    negotiates: open deals are hidden from it and its deal orders dropped,
    so no deal with it can ever execute and it acts exactly once per turn
    on the server as offline."""

    name = "quiet"

    def __init__(self, inner: Bot):
        super().__init__(inner.seed)
        self.inner = inner

    def act(self, view: dict) -> list:
        if not view:
            return []
        view = dict(view)
        deals = dict(view.get("deals") or {})
        deals["open"] = []
        view["deals"] = deals
        view["trade_offers"] = []
        orders = self.inner.act(view) or []
        return [o for o in orders if isinstance(o, dict) and o.get("type") not in DEAL_ORDER_TYPES]
