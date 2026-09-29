/* AgentCiv spectator GUI.
 *
 * Vanilla JS, no build step, no external dependencies. Talks to the server's
 * HTTP API (docs/DESIGN.md §12) using relative URLs, and renders spectator
 * views (§10). When opened with ?mock=1, or when the API is unreachable, it
 * serves the bundled data in web/mock/ instead so the page works standalone.
 *
 * Layout of this file:
 *   1. utilities & constants
 *   2. API layer (+ mock backend)
 *   3. router / top bar
 *   4. lobby view
 *   5. game view: data (frames, live, replay)
 *   6. game view: map rendering + tooltip
 *   7. game view: sidebar panels
 *   8. boot
 */
'use strict';

(() => {
  // ================================================================ 1. utils
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const params = new URLSearchParams(location.search);

  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
  const num = (v, d = 0) => (typeof v === 'number' && isFinite(v) ? v : d);
  const fmt = (v) => {
    if (typeof v !== 'number' || !isFinite(v)) return '–';
    if (Math.abs(v) >= 100000) return (v / 1000).toFixed(0) + 'k';
    if (Math.abs(v) >= 10000) return (v / 1000).toFixed(1) + 'k';
    return String(Math.round(v));
  };
  const pct = (p) => Math.round(Math.max(0, Math.min(1, num(p))) * 100) + '%';
  const cap = (s) => String(s || '').charAt(0).toUpperCase() + String(s || '').slice(1);
  const human = (s) => cap(String(s || '').replace(/_/g, ' '));
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const fmtDur = (sec) => {
    const s = Math.max(0, Math.ceil(sec));
    if (s < 90) return s + 's';
    if (s < 3600) return Math.floor(s / 60) + 'm ' + String(s % 60).padStart(2, '0') + 's';
    return Math.floor(s / 3600) + 'h ' + String(Math.floor((s % 3600) / 60)).padStart(2, '0') + 'm';
  };
  const bagText = (b) => Object.entries(b || {}).filter(([, n]) => n).map(([k, n]) => `${n} ${k}`).join(', ');

  const RESOURCES = ['food', 'wood', 'stone', 'gold', 'influence'];
  const TRADABLE = ['food', 'wood', 'stone'];
  const RES_COLOR = { food: '#9fd356', wood: '#c08a55', stone: '#a9b3c1', gold: '#f3c969', influence: '#c084fc' };
  const RES_SHORT = { food: 'Food', wood: 'Wood', stone: 'Stone', gold: 'Gold', influence: 'Inf' };
  const UNITS = ['infantry', 'archer', 'cavalry', 'siege'];
  const STRENGTH = { infantry: 10, archer: 8, cavalry: 12, siege: 4 };
  const PALETTE = ['#e6194b', '#3cb44b', '#ffe119', '#4363d8', '#f58231', '#911eb4',
    '#46f0f0', '#f032e6', '#bcf60c', '#fabebe', '#008080', '#e6beff'];
  const CONDITIONS = ['conquest', 'wonder', 'influence', 'relics', 'economic'];
  const COND_LABEL = {
    conquest: 'Conquest', wonder: 'Wonder', influence: 'Influence', relics: 'Relics',
    economic: 'Economic', score: 'Score (turn limit)', last_standing: 'Last standing',
  };
  const SEASON_COLOR = { spring: '#9fd356', summer: '#fbbf24', autumn: '#f97316', winter: '#93c5fd' };
  const TERRAIN = {
    '.': { name: 'Plains', color: '#5b7a3c', yield: '2 food' },
    f: { name: 'Forest', color: '#2c5234', yield: '2 wood' },
    h: { name: 'Hills', color: '#7a6a4b', yield: '2 stone' },
    g: { name: 'Gold', color: '#86702f', yield: '1 gold' },
    m: { name: 'Mountain', color: '#4a4d58', yield: 'impassable' },
    '~': { name: 'Water', color: '#1c3d5a', yield: 'impassable' },
  };
  const BUILDING_INFO = {
    farm: '+2 food', lumber_mill: '+2 wood', quarry: '+2 stone', mine: '+2 gold', temple: '+2 influence',
  };
  const DEPOSIT_MAX = { stone: 300, gold: 150 };

  /** Rule numbers come from the view's `costs` (GET /api/rules.json) when present, so the GUI
   *  follows rule changes; the constants above are only fallbacks. */
  const Rules = {
    costs: null,
    use(v) { if (v && v.costs && typeof v.costs === 'object') this.costs = v.costs; },
    terrain(ch) {
      const t = this.costs?.map?.terrain?.[ch];
      const base = TERRAIN[ch] || { name: human(t?.name || ch), color: '#555' };
      if (!t) return base;
      const y = bagText(t.yield);
      return { ...base, name: human(t.name || base.name), yield: t.passable === false ? 'impassable' : (y || 'nothing') };
    },
    strength(u) { return num(this.costs?.units?.[u]?.strength, STRENGTH[u] || 0); },
    depositMax(resource) {
      for (const t of Object.values(this.costs?.map?.terrain || {})) {
        if (t?.deposit?.resource === resource && t.deposit.amount) return t.deposit.amount;
      }
      return DEPOSIT_MAX[resource];
    },
    building(b) {
      const imp = this.costs?.buildings?.improvements?.[b];
      return imp?.bonus ? '+' + bagText(imp.bonus) : (BUILDING_INFO[b] || '');
    },
    relicInfluence() { return num(this.costs?.influence?.relic, 3); },
    wonderMax(th) { return num(th?.wonder_stage, num(this.costs?.buildings?.city?.wonder?.max, 5)); },
    hallFee() { return this.costs?.market?.market_hall_fee; },
  };

  /** Small inline SVG icon set (16x16, stroke = currentColor). */
  const ICON_PATHS = {
    battle: 'M3 3l10 10M13 3L3 13M2 10l4 4M10 14l4-4',
    flag: 'M4 14V2M4 3h8l-2 3 2 3H4',
    city: 'M2 14h12M3 14V7l3-2 3 2v7M9 14V4l4 2v8',
    claim: 'M8 3v10M3 8h10',
    build: 'M3 13l6-6M8 3l5 5-2 2-5-5z',
    unit: 'M8 6.5a2.2 2.2 0 1 0 0-4.4 2.2 2.2 0 0 0 0 4.4zM3.5 14c0-3 2-5 4.5-5s4.5 2 4.5 5',
    star: 'M8 1.8l1.9 4 4.3.5-3.2 3 .9 4.3L8 11.4l-3.9 2.2.9-4.3-3.2-3 4.3-.5z',
    skull: 'M8 2a6 6 0 1 0 0 12A6 6 0 0 0 8 2zM5.5 5.5l5 5M10.5 5.5l-5 5',
    treaty: 'M9 8a3 3 0 1 1-6 0 3 3 0 0 1 6 0zM13 8a3 3 0 1 1-6 0 3 3 0 0 1 6 0z',
    broken: 'M9 1L4 9h4l-1 6 5-8H8z',
    trade: 'M2 5h11l-3-3M14 11H3l3 3',
    market: 'M2 13l4-4 3 2 5-6M10 5h4v4',
    fail: 'M8 2l6.5 12h-13zM8 6.5v3.5M8 12v.6',
    trophy: 'M5 2h6v4a3 3 0 0 1-6 0zM8 9v3M5 14h6M5 3H2.5a2.5 2.5 0 0 0 2.7 3M11 3h2.5a2.5 2.5 0 0 1-2.7 3',
    starve: 'M2 8h12a6 6 0 0 1-12 0zM6 5V2M10 5V2',
    relic: 'M8 1.5l5 6.5-5 6.5-5-6.5z',
    dot: 'M8 6a2 2 0 1 0 0 4 2 2 0 0 0 0-4z',
    lock: 'M4.5 7.5h7v6h-7zM6 7.5V5a2 2 0 0 1 4 0v2.5',
    plug: 'M6 2v3M10 2v3M4.5 5h7v2.5a3.5 3.5 0 0 1-7 0zM8 11v3',
  };
  const icon = (name, color) =>
    `<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="${color || 'currentColor'}" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${ICON_PATHS[name] || ICON_PATHS.dot}"/></svg>`;

  function hexToRgb(hex) {
    let h = String(hex || '').replace('#', '');
    if (h.length === 3) h = h.split('').map((c) => c + c).join('');
    const n = parseInt(h, 16);
    if (h.length !== 6 || isNaN(n)) return [160, 160, 160];
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }
  const rgba = (hex, a) => { const [r, g, b] = hexToRgb(hex); return `rgba(${r},${g},${b},${a})`; };
  const luminance = (hex) => { const [r, g, b] = hexToRgb(hex); return (0.299 * r + 0.587 * g + 0.114 * b) / 255; };
  /** Deterministic per-tile pseudo random in [0,1). */
  const hash = (x, y, k = 0) => {
    let h = (x * 374761393 + y * 668265263 + k * 2147483647) | 0;
    h = Math.imul(h ^ (h >>> 13), 1274126177);
    return ((h ^ (h >>> 16)) >>> 0) / 4294967296;
  };

  function toast(msg, isError = false, ms = 3500) {
    const el = $('#toast');
    el.textContent = msg;
    el.className = 'toast' + (isError ? ' error' : '');
    el.hidden = false;
    clearTimeout(toast.t);
    toast.t = setTimeout(() => { el.hidden = true; }, ms);
  }

  function setConn(state, text) {
    const el = $('#conn');
    el.className = 'conn ' + state;
    $('span', el).textContent = text;
  }

  // ============================================================ 2. API layer
  const App = {
    mock: params.get('mock') === '1',
    mockReason: '',
    booting: false,     // true only during the first API probe: the one moment we may switch to mock data
    gamesCache: [],
    skew: 0,            // server clock − local clock (s), from the X-Server-Time header
  };
  const serverNow = () => Date.now() / 1000 + App.skew;

  class HttpError extends Error {
    constructor(status, message) { super(message); this.status = status; }
  }

  async function http(method, path, body, token) {
    const opts = { method, headers: {} };
    if (token) opts.headers.Authorization = 'Bearer ' + token;
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    const t0 = Date.now();
    const res = await fetch(path, opts);
    const st = parseFloat(res.headers.get('X-Server-Time'));
    if (isFinite(st)) App.skew = st - (t0 + Date.now()) / 2000;
    const text = await res.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch (_) { /* non-JSON */ }
    if (!res.ok) {
      const msg = (data && (data.error || data.detail || data.message)) || text.slice(0, 200) || res.statusText;
      throw new HttpError(res.status, `${res.status}: ${msg}`);
    }
    if (data === null && text) throw new HttpError(res.status, 'invalid JSON from ' + path);
    return data;
  }

  /** Offline backend serving web/mock/*.json in the same shapes as the real API. */
  const Mock = {
    cache: {},
    async file(name) {
      if (!this.cache[name]) {
        this.cache[name] = fetch(`mock/${name}.json`).then((r) => {
          if (!r.ok) throw new Error('missing mock/' + name + '.json');
          return r.json();
        }).catch((e) => {
          delete this.cache[name]; // never cache a failure: the next call retries
          throw e;
        });
      }
      return this.cache[name];
    },
    async gameMeta(id) {
      const games = await this.file('games');
      return games.find((g) => g.game_id === id) || { game_id: id, status: 'running', players: [] };
    },
    /** Frames available "so far" for a mock game, depending on its status. */
    async framesFor(id) {
      const meta = await this.gameMeta(id);
      const replay = await this.file('replay');
      const frames = replay.frames.map((f) => ({ ...f, game_id: id }));
      if (meta.status === 'finished') return { frames, pending: [] };
      if (meta.status === 'lobby') return { frames: [this.lobbyView(meta)], pending: [] };
      const cut = clamp(parseInt(params.get('mockcut') || '4', 10), 1, frames.length);
      return { frames: frames.slice(0, cut), pending: frames.slice(cut) };
    },
    lobbyView(meta) {
      return {
        game_id: meta.game_id, turn: 0, max_turns: 150, status: 'lobby', season: null, you: null,
        players: meta.players.map((p, i) => ({ ...p, color: PALETTE[i % PALETTE.length], alive: true })),
        max_players: meta.max_players, map: null, cities: [], armies: [], events: [], messages: [],
        victory: { thresholds: {}, result: null },
      };
    },
    async get(path) {
      await new Promise((r) => setTimeout(r, 40));
      if (path === 'api/games') return this.file('games');
      if (path === 'api/bots') return this.file('bots');
      if (path === 'api/leaderboard') return this.file('leaderboard');
      let m = path.match(/^api\/games\/([^/?]+)\/replay(\?.*)?$/);
      if (m) {
        const { frames } = await this.framesFor(decodeURIComponent(m[1]));
        const last = frames[frames.length - 1];
        return { frames, result: last?.victory?.result || null };
      }
      m = path.match(/^api\/games\/([^/?]+)\/state/);
      if (m) {
        const { frames } = await this.framesFor(decodeURIComponent(m[1]));
        return frames[frames.length - 1];
      }
      m = path.match(/^api\/games\/([^/?]+)$/);
      if (m) return this.gameMeta(decodeURIComponent(m[1]));
      throw new HttpError(404, 'mock: no route for ' + path);
    },
    async post(path, body) {
      await new Promise((r) => setTimeout(r, 120));
      if (path === 'api/games') return { game_id: 'g8', mock_body: body };
      if (/\/start$/.test(path)) return { ok: true };
      throw new HttpError(404, 'mock: no route for ' + path);
    },
  };

  function enableMock(reason) {
    if (App.mock) return;
    App.mock = true;
    App.mockReason = reason;
    toast('Server API unreachable — showing bundled mock data.', false, 5000);
  }

  const api = {
    async get(path) {
      if (App.mock) return Mock.get(path);
      try {
        return await http('GET', path);
      } catch (e) {
        // A static file server (no API) answers 404/501 or non-JSON: fall back to mock data. Only the
        // boot probe may switch: a later network blip (server restart) must not strand a live GUI in mock mode.
        if (App.booting && path === 'api/games' && (!(e instanceof HttpError) || e.status === 404 || e.status === 501 || /invalid JSON/.test(e.message))) {
          enableMock(e.message);
          return Mock.get(path);
        }
        throw e;
      }
    },
    async post(path, body, token) {
      if (App.mock) return Mock.post(path, body);
      return http('POST', path, body ?? {}, token);
    },
  };

  /** creator_token of games created from this browser: POST /start needs it once agents have joined. */
  const CreatorTokens = {
    key: 'agentciv.creatorTokens',
    all() {
      try { return JSON.parse(localStorage.getItem(this.key) || '{}') || {}; } catch (_) { return {}; }
    },
    get(id) { return this.all()[id] || null; },
    set(id, token) {
      if (!token) return;
      const all = this.all();
      all[id] = token;
      const ids = Object.keys(all);
      ids.slice(0, Math.max(0, ids.length - 200)).forEach((k) => delete all[k]); // keep the newest 200
      try { localStorage.setItem(this.key, JSON.stringify(all)); } catch (_) { /* storage blocked */ }
    },
  };

  // ========================================================== 3. router
  let current = null; // active view controller

  function route() {
    const m = (location.hash || '').match(/^#\/game\/([^/?#]+)/);
    const next = m ? GameView : Lobby;
    if (current && current !== next) current.stop();
    $('#lobby').hidden = next !== Lobby;
    $('#game').hidden = next !== GameView;
    current = next;
    if (m) GameView.open(decodeURIComponent(m[1]));
    else Lobby.start();
  }

  function setCrumbs(html) { $('#crumbs').innerHTML = html; }

  // =========================================================== 4. lobby
  const Lobby = {
    timer: null,
    lbTimer: null,
    bots: [],
    seats: [],
    formReady: false,
    session: 0,          // bumps on start()/stop(): a start() that resumes after stop() must not start timers

    async start() {
      const session = ++this.session;
      setCrumbs('<span class="cur">Lobby</span>');
      document.title = 'AgentCiv — Lobby';
      this.renderConnect();
      if (!this.formReady) this.initForm();
      await this.refreshGames();
      if (session !== this.session || current !== Lobby) return; // navigated away meanwhile
      this.refreshLeaderboard();
      this.loadBots();
      clearInterval(this.timer);
      clearInterval(this.lbTimer);
      this.timer = setInterval(() => this.refreshGames(), 3000);
      this.lbTimer = setInterval(() => this.refreshLeaderboard(), 15000);
    },

    stop() {
      this.session++;
      clearInterval(this.timer);
      clearInterval(this.lbTimer);
      this.timer = this.lbTimer = null;
    },

    async refreshGames() {
      try {
        const games = await api.get('api/games');
        App.gamesCache = Array.isArray(games) ? games : [];
        this.renderGames(App.gamesCache);
        $('#games-updated').textContent = 'updated ' + new Date().toLocaleTimeString();
        if (current !== Lobby) return; // a poll that finished after navigating to a game: #conn is not ours
        if (App.mock) setConn('mock', 'mock data');
        else setConn('ok', 'connected');
      } catch (e) {
        $('#games-updated').textContent = 'error: ' + e.message;
        if (current === Lobby) setConn('bad', 'offline');
      }
    },

    renderGames(games) {
      const order = { running: 0, lobby: 1, waiting: 1, finished: 2 };
      const list = games.slice().sort((a, b) =>
        (order[a.status] ?? 3) - (order[b.status] ?? 3) || num(b.created) - num(a.created));
      const body = $('#games-body');
      if (!list.length) {
        body.innerHTML = '<tr><td colspan="5" class="muted">No games yet — create one, or have an agent call <code>POST /api/quickmatch</code>.</td></tr>';
        return;
      }
      body.innerHTML = list.map((g) => {
        const players = g.players || [];
        const open = Math.max(0, num(g.max_players, players.length) - players.length);
        const pills = players.map((p) =>
          `<span class="pill ${p.is_bot ? 'bot' : 'human'}" title="${esc(p.id)}${p.is_bot ? ' · house bot' : ' · remote agent'}">${esc(p.name)}${p.is_bot ? ' ·bot' : ''}</span>`).join('');
        const openPill = g.status === 'lobby' && open ? `<span class="pill open">+${open} open</span>` : '';
        const join = g.status === 'lobby'
          ? `<button class="btn ghost small" data-joincmd="${esc(g.game_id)}" title="Copy a curl command that joins this game">Join cmd</button>` : '';
        return `<tr>
          <td><span class="game-name">${esc(g.name || g.game_id)}</span><span class="game-id">${esc(g.game_id)}</span></td>
          <td><span class="badge ${esc(g.status)}">${esc(g.status)}</span></td>
          <td class="num">${g.status === 'lobby' ? '–' : fmt(num(g.turn))}</td>
          <td><div class="plist">${pills}${openPill}</div></td>
          <td class="row-actions">${join}<a class="btn small" href="#/game/${encodeURIComponent(g.game_id)}">Watch</a></td>
        </tr>`;
      }).join('');
      $$('[data-joincmd]', body).forEach((b) => b.addEventListener('click', () => {
        const cmd = `curl -s -X POST ${apiBase()}/api/games/${b.dataset.joincmd}/join -H 'Content-Type: application/json' -d '{"name":"my-agent"}'`;
        copyText(cmd, 'Join command copied');
      }));
    },

    async refreshLeaderboard() {
      try {
        const rows = await api.get('api/leaderboard');
        const body = $('#lb-body');
        if (!rows || !rows.length) {
          body.innerHTML = '<tr><td colspan="8" class="muted">No rated games yet.</td></tr>';
          return;
        }
        body.innerHTML = rows.map((r, i) => {
          const games = num(r.games);
          const winPct = games ? Math.round(100 * num(r.wins) / games) + '%' : '–';
          return `<tr><td class="num muted">${i + 1}</td><td>${esc(r.name)}</td>
            <td class="num"><b>${num(r.rating).toFixed(1)}</b></td>
            <td class="num muted">${num(r.mu).toFixed(1)} ± ${num(r.sigma).toFixed(1)}</td>
            <td class="num">${games}</td><td class="num">${num(r.wins)}</td><td class="num">${winPct}</td>
            <td class="num">${r.avg_place == null ? '–' : num(r.avg_place).toFixed(2)}</td></tr>`;
        }).join('');
      } catch (e) {
        $('#lb-body').innerHTML = `<tr><td colspan="8" class="muted">Leaderboard unavailable (${esc(e.message)})</td></tr>`;
      }
    },

    async loadBots() {
      try {
        const bots = await api.get('api/bots');
        this.bots = (bots || []).map((b) => (typeof b === 'string' ? b : b.name)).filter(Boolean);
      } catch (_) {
        this.bots = ['strategist', 'economist', 'rusher', 'turtle', 'random', 'idle'];
      }
      if (!this.seats.length) this.fillSeats('mix');
      this.renderSeats();
    },

    initForm() {
      this.formReady = true;
      const form = $('#newgame-form');
      const np = form.elements.players;
      np.addEventListener('input', () => {
        $('#np-out').textContent = np.value;
        this.renderSeats();
      });
      $$('[data-fill]', form).forEach((b) => b.addEventListener('click', () => {
        this.fillSeats(b.dataset.fill);
        this.renderSeats();
      }));
      form.addEventListener('submit', (e) => { e.preventDefault(); this.submit(); });
      $('#games-refresh').onclick = () => this.refreshGames();
    },

    fillSeats(kind) {
      const mix = ['strategist', 'economist', 'rusher', 'turtle', 'strategist', 'economist', 'random'];
      // Prefer "real" strategies over the baseline bots when filling seats.
      const strong = this.bots.filter((b) => b !== 'idle' && b !== 'random');
      const avail = strong.length ? strong : (this.bots.length ? this.bots : mix);
      this.seats = Array.from({ length: 12 }, (_, i) => {
        if (kind === '') return '';
        if (kind === 'mix') {
          const want = mix[i % mix.length];
          return avail.includes(want) ? want : avail[i % avail.length];
        }
        return avail.includes(kind) ? kind : avail[0];
      });
      this.syncStartNow();
    },

    syncStartNow() {
      const n = parseInt($('#newgame-form').elements.players.value, 10);
      const anyOpen = this.seats.slice(0, n).some((s) => !s);
      $('#newgame-form').elements.start_now.checked = !anyOpen;
    },

    renderSeats() {
      const n = parseInt($('#newgame-form').elements.players.value, 10);
      const opts = ['<option value="">— open seat (remote agent) —</option>']
        .concat(this.bots.map((b) => `<option value="${esc(b)}">${esc(b)}</option>`)).join('');
      $('#seats').innerHTML = Array.from({ length: n }, (_, i) =>
        `<div class="seat"><span class="swatch" style="background:${PALETTE[i]}"></span><span class="seat-n">p${i + 1}</span><select data-seat="${i}" aria-label="Seat ${i + 1}">${opts}</select></div>`).join('');
      $$('#seats select').forEach((sel) => {
        sel.value = this.seats[+sel.dataset.seat] || '';
        sel.addEventListener('change', () => {
          this.seats[+sel.dataset.seat] = sel.value;
          this.syncStartNow();
        });
      });
    },

    async submit() {
      const f = $('#newgame-form').elements;
      const msg = $('#newgame-msg');
      const n = parseInt(f.players.value, 10);
      const bots = this.seats.slice(0, n).filter(Boolean);
      const body = {
        max_players: n,
        min_players: 2,
        turn_timeout: parseInt(f.turn_timeout.value, 10) || 30,
        max_turns: parseInt(f.max_turns.value, 10) || 150,
        bots,
        fill_with_bots: f.fill_with_bots.checked,
      };
      if (f.name.value.trim()) body.name = f.name.value.trim();
      if (f.seed.value !== '') body.seed = parseInt(f.seed.value, 10);
      if (f.lobby_timeout.value !== '' && parseFloat(f.lobby_timeout.value) > 0) body.lobby_timeout = parseFloat(f.lobby_timeout.value);
      msg.className = 'form-msg';
      msg.textContent = 'Creating…';
      try {
        const res = await api.post('api/games', body);
        const id = res.game_id;
        CreatorTokens.set(id, res.creator_token);
        if (f.start_now.checked) {
          msg.textContent = 'Starting…';
          await api.post(`api/games/${encodeURIComponent(id)}/start`, {}, res.creator_token);
        }
        msg.textContent = `Created ${id}.`;
        location.hash = '#/game/' + encodeURIComponent(id);
      } catch (e) {
        msg.className = 'form-msg error';
        msg.textContent = 'Failed: ' + e.message;
      }
    },

    renderConnect() {
      const B = apiBase();
      $('#connect-code').innerHTML = [
        '<span class="c"># 1. Join the open quickmatch lobby (auto-starts when full)</span>',
        `curl -s -X POST ${B}/api/quickmatch -H 'Content-Type: application/json' \\`,
        `     -d '{"name":"my-agent"}'`,
        '<span class="c"># → {"game_id":"g3","player_id":"p4","token":"TOKEN"}</span>',
        '',
        '<span class="c"># 2. Read your view of the game</span>',
        `curl -s -H 'Authorization: Bearer TOKEN' ${B}/api/games/g3/state`,
        '',
        '<span class="c"># 3. Submit this turn\'s orders</span>',
        `curl -s -X POST ${B}/api/games/g3/orders -H 'Authorization: Bearer TOKEN' \\`,
        `     -H 'Content-Type: application/json' \\`,
        `     -d '{"turn":0,"orders":[{"type":"claim","at":[6,4]}]}'`,
        '',
        '<span class="c"># 4. Block until the turn resolves, then repeat 2–4</span>',
        `curl -s "${B}/api/games/g3/wait?since_turn=0&amp;timeout=30"`,
      ].join('\n');
      $$('[data-copy]').forEach((b) => {
        b.onclick = () => copyText($('#' + b.dataset.copy).textContent, 'Copied');
      });
    },
  };

  function apiBase() {
    const u = new URL('.', location.href);
    return (u.origin + u.pathname).replace(/\/$/, '');
  }

  function copyText(text, okMsg) {
    const done = () => toast(okMsg || 'Copied');
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done, () => fallback());
    } else fallback();
    function fallback() {
      const ta = document.createElement('textarea');
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand('copy'); done(); } catch (_) { toast('Copy failed', true); }
      ta.remove();
    }
  }

  // ============================================== 5. game view: data flow
  const GameView = {
    id: null,
    frames: [],          // spectator views sorted by turn
    idx: 0,              // index of the displayed frame
    live: true,          // follow the newest frame
    playing: false,
    playTimer: null,
    es: null,
    pollTimer: null,
    mockTimer: null,
    clockTimer: null,
    highlight: null,     // highlighted player id
    hover: null,         // {x, y} hovered tile
    flash: null,         // {x, y, until}
    overlayDismissed: false,
    filter: '',
    tab: 'events',
    session: 0,          // bumps on every open() to drop stale async results
    layout: null,
    terrainCache: null,
    bound: false,
    meta: null,          // game summary: seat types, max_players, lobby settings
    history: { loading: false, loaded: 0, total: null },
    feedLimit: 150,

    get view() { return this.frames[this.idx] || null; },
    get latest() { return this.frames[this.frames.length - 1] || null; },
    get finished() { return this.latest?.status === 'finished'; },

    async open(id) {
      this.stop();
      this.bindOnce();
      const session = ++this.session;
      Object.assign(this, {
        id, frames: [], idx: 0, live: true, playing: false, highlight: null, hover: null,
        flash: null, overlayDismissed: false, filter: '', terrainCache: null, meta: null,
        history: { loading: false, loaded: 0, total: null }, streamAttempt: 0, metaAt: 0, feedLimit: 150,
      });
      $('#feed-filter').value = '';
      this.clearPanels();
      this.meta = App.gamesCache.find((g) => g.game_id === id) || null;
      this.setTitle();
      this.showEmpty('Loading game…');
      setConn('warn', 'loading…');

      try {
        const state = await api.get(`api/games/${encodeURIComponent(id)}/state`);
        if (session !== this.session) return;
        this.addFrame(state);
      } catch (e) {
        if (session !== this.session) return;
        this.showEmpty(`Could not load game <b>${esc(id)}</b>`, esc(e.message));
        setConn('bad', 'error');
        return;
      }
      this.refreshMeta(session, true);

      // Deterministic screenshots / deep links: ?frame=N selects a frame index.
      if (params.has('frame')) this.pendingFrame = parseInt(params.get('frame'), 10);
      // History loads in the background in compact chunks; live frames keep arriving meanwhile.
      this.loadReplay(session);
      if (!this.finished) this.connectLive(session);
      else setConn(App.mock ? 'mock' : 'warn', App.mock ? 'mock · replay' : 'finished · replay');
      clearInterval(this.clockTimer);
      this.clockTimer = setInterval(() => this.renderDeadline(), 1000);
    },

    /** Blank every panel that shows per-game data, so a newly opened game (or one that fails to
     *  load) never shows the previous game's sidebar, banner, overlay or player filter. */
    clearPanels() {
      ['#status-panel', '#players-table', '#race', '#market', '#events', '#diplo', '#race-note', '#market-fee']
        .forEach((sel) => { const el = $(sel); if (el) el.innerHTML = ''; });
      ['#banner', '#overlay', '#tooltip'].forEach((sel) => { const el = $(sel); if (el) { el.hidden = true; el.innerHTML = ''; } });
      const f = $('#feed-filter');
      delete f.dataset.ids;
      f.innerHTML = '<option value="">All players</option>';
    },

    setTitle() {
      const name = this.meta?.name || this.latest?.name || 'Game';
      setCrumbs(`<a href="#/">Lobby</a><span class="sep">/</span><span class="cur">${esc(name)} <span class="game-id">${esc(this.id)}</span></span>`);
      document.title = `AgentCiv — ${name}`;
    },

    /** Game summary (seat types, max_players, lobby settings). Refreshed on seat/status changes. */
    async refreshMeta(session, force = false) {
      const now = Date.now();
      if (!force && now - this.metaAt < 1500) return;
      this.metaAt = now;
      try {
        const meta = await api.get(`api/games/${encodeURIComponent(this.id)}`);
        if (session !== this.session || !meta) return;
        this.meta = meta;
        this.setTitle();
        const v = this.view;
        if (v) {
          this.renderPlayers(v);
          if (v.status === 'lobby') this.showLobbyState(v);
          this.renderDeadline();
        }
      } catch (_) { /* optional */ }
    },

    seat(pid) { return (this.meta?.players || []).find((p) => p.id === pid) || null; },
    isBot(pid) { return !!this.seat(pid)?.is_bot; },

    /** Re-attach the parts the compact replay format shares across frames. */
    restoreFrame(f, st) {
      if (!f || typeof f !== 'object') return f;
      if (st) {
        if (f.map && !f.map.terrain && st.terrain) f.map.terrain = st.terrain;
        if (!f.costs && st.costs) f.costs = st.costs;
      }
      return f;
    },

    async loadReplay(session, from = 0, to = null) {
      const CHUNK = 40;
      const id = encodeURIComponent(this.id);
      const H = this.history;
      H.loading = true;
      try {
        let lo = from;
        for (;;) {
          const hi = to != null ? Math.min(to, lo + CHUNK - 1) : lo + CHUNK - 1;
          const rep = await api.get(`api/games/${id}/replay?compact=1&from=${lo}&to=${hi}`);
          if (session !== this.session) return;
          const frames = Array.isArray(rep?.frames) ? rep.frames : [];
          const shownTurn = this.view?.turn;
          for (const f of frames) {
            // Never overwrite a newer live frame of the same turn (it has fresher "submitted" flags).
            if (!this.frames.some((x) => x.turn === f.turn)) this.insertFrame(this.restoreFrame(f, rep.static));
          }
          if (typeof rep?.total_frames === 'number') H.total = rep.total_frames;
          H.loaded = this.frames.length;
          if (this.live) this.idx = this.frames.length - 1;
          else this.idx = Math.max(0, this.frames.findIndex((f) => f.turn === shownTurn));
          if (this.pendingFrame != null && (this.frames.length > this.pendingFrame || !rep?.total_frames || hi + 1 >= rep.total_frames)) {
            this.idx = clamp(this.pendingFrame, 0, this.frames.length - 1);
            this.live = this.idx === this.frames.length - 1 && !this.finished;
            this.pendingFrame = null;
            this.render();
          } else if (this.pendingFrame == null) {
            this.renderControls();
            this.renderFeed();
          }
          const end = rep?.to ?? (lo + frames.length - 1);
          if (typeof rep?.total_frames !== 'number' || !frames.length || end + 1 >= rep.total_frames ||
              (to != null && end >= to)) break;
          lo = end + 1;
        }
      } catch (_) {
        /* replay not available (e.g. lobby) — live frames still accumulate */
      } finally {
        if (session === this.session) {
          H.loading = false;
          if (this.pendingFrame != null && this.frames.length) {
            this.idx = clamp(this.pendingFrame, 0, this.frames.length - 1);
            this.live = false;
            this.pendingFrame = null;
          }
          this.render();
        }
      }
    },

    connectLive(session) {
      if (App.mock) {
        setConn('mock', 'mock · live');
        Mock.framesFor(this.id).then(({ pending }) => {
          const step = parseInt(params.get('mockstep') || '6000', 10);
          if (!step || session !== this.session) return;
          const queue = pending.slice();
          this.mockTimer = setInterval(() => {
            const f = queue.shift();
            if (!f) return clearInterval(this.mockTimer);
            this.onLiveFrame(f);
          }, step);
        });
        return;
      }
      if ('EventSource' in window) this.openStream(session);
      else this.startPolling(session, 1000);
    },

    /** Server-sent events with reconnect + exponential backoff; polls /state while disconnected. */
    openStream(session) {
      if (session !== this.session || this.finished) return;
      clearTimeout(this.reconnectTimer);
      const es = new EventSource(`api/games/${encodeURIComponent(this.id)}/stream`);
      this.es = es;
      const onState = (e) => {
        try { this.onLiveFrame(JSON.parse(e.data)); } catch (_) { /* ignore bad frame */ }
      };
      es.addEventListener('state', onState);
      es.onmessage = onState;
      es.addEventListener('finished', () => { es.close(); if (this.es === es) this.es = null; });
      es.onopen = () => {
        if (session !== this.session) return;
        this.streamAttempt = 0;
        this.stopPolling();
        if (!this.finished) setConn('ok', 'live · stream');
      };
      es.onerror = () => {
        if (session !== this.session || this.es !== es) return;
        es.close();
        this.es = null;
        if (this.finished) return;
        const n = this.streamAttempt++;
        const delay = Math.min(30000, 1000 * 2 ** n) * (0.8 + 0.4 * Math.random());
        setConn('warn', `reconnecting (${Math.round(delay / 1000)}s)…`);
        this.startPolling(session, 3000); // keep the view fresh meanwhile
        this.reconnectTimer = setTimeout(() => this.openStream(session), delay);
      };
    },

    startPolling(session, every) {
      clearInterval(this.pollTimer);
      let inflight = false;
      this.pollTimer = setInterval(async () => {
        if (inflight || session !== this.session) return;
        inflight = true;
        try {
          const v = await api.get(`api/games/${encodeURIComponent(this.id)}/state`);
          if (session === this.session) {
            if (!this.finished && !this.es) setConn('ok', 'live · polling');
            this.onLiveFrame(v);
          }
        } catch (_) {
          setConn('bad', 'offline · retrying…');
        } finally { inflight = false; }
      }, every);
    },

    stopPolling() { clearInterval(this.pollTimer); this.pollTimer = null; },

    onLiveFrame(v) {
      if (!v || typeof v !== 'object') return;
      const prev = this.latest;
      // Frames missed while disconnected: fetch the gap from the replay.
      if (prev && prev.status !== 'lobby' && typeof v.turn === 'number' && v.turn > prev.turn + 1 && !App.mock) {
        this.loadReplay(this.session, prev.turn + 1, v.turn - 1);
      }
      this.addFrame(v);
      const seats = (v.players || []).length;
      if (!this.meta || v.status !== this.meta.status || seats !== (this.meta.players || []).length ||
          v.status === 'lobby') this.refreshMeta(this.session);
      if (this.finished) {
        this.stopLive();
        setConn(App.mock ? 'mock' : 'warn', 'finished');
        if (!this.meta?.result) this.refreshMeta(this.session, true);
      }
    },

    /** Insert/replace a frame by turn without re-rendering. Returns its index. */
    insertFrame(v) {
      if (!v || typeof v !== 'object' || typeof v.turn !== 'number') return -1;
      Rules.use(v);
      let i = this.frames.findIndex((f) => f.turn === v.turn);
      if (i >= 0) this.frames[i] = v;
      else {
        i = this.frames.findIndex((f) => f.turn > v.turn);
        if (i < 0) { this.frames.push(v); i = this.frames.length - 1; } else this.frames.splice(i, 0, v);
      }
      return i;
    },

    addFrame(v) {
      const shownTurn = this.view?.turn;
      const i = this.insertFrame(v);
      if (i < 0) return;
      if (this.live || shownTurn == null) this.idx = this.frames.length - 1;
      else this.idx = Math.max(0, this.frames.findIndex((f) => f.turn === shownTurn));
      if (this.live || i === this.idx) this.render();
      else this.renderControls();
    },

    stopLive() {
      if (this.es) { this.es.close(); this.es = null; }
      clearTimeout(this.reconnectTimer);
      this.stopPolling();
      clearInterval(this.mockTimer);
    },

    stop() {
      this.session++;
      this.stopLive();
      this.pause();
      clearInterval(this.clockTimer);
    },

    // --- replay controls -----------------------------------------------
    seek(i, { fromPlay = false } = {}) {
      if (!this.frames.length) return;
      this.idx = clamp(i, 0, this.frames.length - 1);
      const atEnd = this.idx === this.frames.length - 1;
      this.live = atEnd && !this.finished && !fromPlay ? this.live : false;
      if (atEnd && fromPlay && this.finished) this.overlayDismissed = false;
      this.render();
    },
    goLive() {
      this.pause();
      this.live = !this.finished;
      this.idx = this.frames.length - 1;
      this.render();
    },
    play() {
      if (this.frames.length < 2) return;
      if (this.idx >= this.frames.length - 1) this.idx = 0;
      this.playing = true;
      this.live = false;
      this.overlayDismissed = true;
      this.tick();
      this.renderControls();
    },
    tick() {
      clearTimeout(this.playTimer);
      if (!this.playing) return;
      const speed = parseFloat($('#speed').value) || 1;
      this.playTimer = setTimeout(() => {
        if (!this.playing) return;
        if (this.idx >= this.frames.length - 1) {
          this.pause();
          if (!this.finished) this.goLive();
          return;
        }
        this.seek(this.idx + 1, { fromPlay: true });
        if (this.idx >= this.frames.length - 1) {
          this.pause();
          if (!this.finished) this.goLive();
          return;
        }
        this.tick();
      }, 800 / speed);
    },
    pause() {
      this.playing = false;
      clearTimeout(this.playTimer);
      if ($('#btn-play')) this.renderControls();
    },

    bindOnce() {
      if (this.bound) return;
      this.bound = true;
      $('#btn-first').onclick = () => { this.pause(); this.seek(0); };
      $('#btn-prev').onclick = () => { this.pause(); this.live = false; this.seek(this.idx - 1); };
      $('#btn-next').onclick = () => { this.pause(); this.seek(this.idx + 1); };
      $('#btn-play').onclick = () => (this.playing ? this.pause() : this.play());
      $('#btn-live').onclick = () => { this.overlayDismissed = false; this.goLive(); };
      $('#slider').addEventListener('input', (e) => {
        this.pause();
        this.live = false;
        this.seek(parseInt(e.target.value, 10));
      });
      $('#speed').addEventListener('change', () => { if (this.playing) this.tick(); });
      $('#feed-filter').addEventListener('change', (e) => {
        this.filter = e.target.value;
        this.renderFeed();
        this.renderDiplomacy();
      });
      $$('.tab').forEach((t) => t.addEventListener('click', () => {
        this.tab = t.dataset.tab;
        $$('.tab').forEach((x) => x.classList.toggle('active', x === t));
        $('#tab-events').hidden = this.tab !== 'events';
        $('#tab-diplo').hidden = this.tab !== 'diplo';
      }));
      document.addEventListener('keydown', (e) => {
        if ($('#game').hidden || /INPUT|SELECT|TEXTAREA/.test(document.activeElement?.tagName)) return;
        if (e.key === 'ArrowLeft') { this.pause(); this.live = false; this.seek(this.idx - 1); }
        else if (e.key === 'ArrowRight') { this.pause(); this.seek(this.idx + 1); }
        else if (e.key === ' ') { e.preventDefault(); this.playing ? this.pause() : this.play(); }
        else if (e.key === 'Home') { this.pause(); this.seek(0); }
        else if (e.key === 'End') this.goLive();
        else if (e.key === 'Escape') { this.highlight = null; this.overlayDismissed = true; this.render(); }
        else return;
      });
      // Delegated clicks: player chips and coordinates anywhere in the sidebar / overlay.
      $('#game').addEventListener('click', (e) => {
        const chip = e.target.closest('[data-pid]');
        if (chip) { this.toggleHighlight(chip.dataset.pid); return; }
        const at = e.target.closest('[data-x]');
        if (at) this.flashTile(+at.dataset.x, +at.dataset.y);
      });
      const canvas = $('#map');
      canvas.addEventListener('mousemove', (e) => this.onHover(e));
      canvas.addEventListener('mouseleave', () => { this.hover = null; $('#tooltip').hidden = true; this.drawMap(); });
      canvas.addEventListener('click', (e) => {
        const t = this.tileAt(e);
        const v = this.view;
        if (!t || !v?.map?.owner) return;
        const o = v.map.owner[t.y]?.[t.x];
        this.toggleHighlight(o || null);
      });
      if ('ResizeObserver' in window) new ResizeObserver(() => this.drawMap()).observe($('#map-wrap'));
      else window.addEventListener('resize', () => this.drawMap());
      matchMedia('(resolution: 1dppx)').addEventListener?.('change', () => this.drawMap());
    },

    toggleHighlight(pid) {
      this.highlight = pid && this.highlight !== pid ? pid : null;
      this.renderPlayers(this.view);
      this.renderRace(this.view);
      this.drawMap();
    },

    flashTile(x, y) {
      this.flash = { x, y, until: performance.now() + 1600 };
      const loop = () => {
        this.drawMap();
        if (this.flash && performance.now() < this.flash.until) requestAnimationFrame(loop);
        else { this.flash = null; this.drawMap(); }
      };
      requestAnimationFrame(loop);
      if (matchMedia('(max-width: 1050px)').matches) $('#map-wrap').scrollIntoView({ behavior: 'smooth', block: 'center' });
    },

    // --- top-level render -----------------------------------------------
    render() {
      const v = this.view;
      if (!v) return;
      this.renderControls();
      this.renderBanner();
      this.renderStatus(v);
      this.renderPlayers(v);
      this.renderRace(v);
      this.renderMarket(v);
      this.renderFilterOptions(v);
      this.renderFeed();
      this.renderDiplomacy();
      this.renderLegend();
      const hasMap = Array.isArray(v.map?.terrain) && v.map.terrain.length > 0 && num(v.map.width, 1) > 0;
      $$('.needs-map').forEach((el) => { el.hidden = !hasMap; });
      if (!hasMap) {
        this.showLobbyState(v);
      } else {
        $('#map-empty').hidden = true;
        $('#map').hidden = false;
        this.drawMap();
      }
      this.renderOverlay();
    },

    showEmpty(title, sub = '') {
      $('#map').hidden = true;
      const el = $('#map-empty');
      el.hidden = false;
      el.innerHTML = `<h2>${title}</h2>${sub ? `<div class="small muted">${sub}</div>` : ''}`;
    },

    showLobbyState(v) {
      const players = v.players || [];
      const meta = this.meta || {};
      const maxP = num(meta.max_players, 0) || v.max_players || players.length;
      const open = Math.max(0, maxP - players.length);
      const B = apiBase();
      const gid = this.id;
      const pills = players.map((p) => {
        const bot = this.isBot(p.id);
        return `<span class="pill ${bot ? 'bot' : 'human'}" title="${esc(p.id)} · ${bot ? 'house bot' : 'remote agent'}">${esc(p.name)}${bot ? ' ·bot' : ''}</span>`;
      }).join('') + (open ? `<span class="pill open">+${open} open</span>` : '');
      const lt = num(meta.lobby_timeout, 0);
      const created = num(meta.created, 0);
      const auto = lt && created ? Math.max(0, created + lt - serverNow()) : null;
      const when = open === 0 ? 'Starting…'
        : auto != null ? `Starts automatically in <b id="lobby-auto">${fmtDur(auto)}</b>${meta.fill_with_bots ? ' — empty seats get house bots' : ''}.`
          : 'Starts when every seat is taken, or when you press Start.';
      const creatorToken = CreatorTokens.get(gid);
      // once remote agents are seated, only the creator (or a seated agent) may start the lobby
      const hasRemote = (meta.players || []).some((p) => !p.is_bot);
      const mayStart = !hasRemote || !!creatorToken;
      const canStart = mayStart && (players.length >= Math.max(1, num(meta.min_players, 1)) || meta.fill_with_bots);
      const code = [
        '<span class="c"># raw HTTP: join, then loop state → orders → wait (see GET /api)</span>',
        esc(`curl -s -X POST ${B}/api/games/${gid}/join -H 'Content-Type: application/json' -d '{"name":"my-agent"}'`),
        '',
        '<span class="c"># Python SDK: a built-in bot (or your own) playing remotely</span>',
        esc(`python -m agentciv.client --url ${B} --game ${gid} --bot strategist --name my-bot`),
        '',
        '<span class="c"># MCP (Claude Code): add the server, then ask Claude to "join game ' + esc(gid) + ' and play it"</span>',
        esc(`claude mcp add agentciv -e AGENTCIV_URL=${B} -- python -m agentciv.mcp_server`),
      ].join('\n');
      this.showEmpty(`Waiting for players <span class="muted">(${players.length}/${maxP || '?'})</span>`,
        `<div class="plist lobby-plist">${pills}</div>
         <div class="lobby-when">${when}</div>
         ${open && !App.mock ? `<div class="lobby-actions"><button class="btn primary" id="lobby-start" ${canStart ? '' : 'disabled'} ${mayStart ? '' : 'title="Only the game\'s creator or a seated agent can start it"'}>Start now${meta.fill_with_bots ? ` <span class="muted">(fill ${open} seat${open === 1 ? '' : 's'} with bots)</span>` : ''}</button></div>` : ''}
         <div class="code-block lobby-code"><button class="btn ghost small copy" id="lobby-copy">Copy</button><pre id="lobby-code">${code}</pre></div>`);
      const btn = $('#lobby-start');
      if (btn) btn.onclick = async () => {
        btn.disabled = true;
        try {
          await api.post(`api/games/${encodeURIComponent(gid)}/start`, {}, creatorToken);
          toast('Game started');
        } catch (e) {
          toast('Could not start: ' + e.message, true);
          btn.disabled = false;
        }
      };
      $('#lobby-copy').onclick = () => copyText($('#lobby-code').textContent, 'Commands copied');
    },

    renderControls() {
      const n = this.frames.length;
      const s = $('#slider');
      s.max = Math.max(0, n - 1);
      s.value = this.idx;
      s.disabled = n < 2;
      const v = this.view;
      const H = this.history || {};
      const loading = H.loading && H.total && n < H.total ? ` · loading ${Math.round((100 * n) / H.total)}%` : '';
      $('#turn-label').textContent = v ? `Turn ${v.turn}${n > 1 ? ` · ${this.idx + 1}/${n}` : ''}${loading}` : '—';
      const play = $('#btn-play');
      play.innerHTML = this.playing ? '&#10074;&#10074;' : '&#9654;';
      play.title = this.playing ? 'Pause (space)' : 'Play replay (space)';
      const live = $('#btn-live');
      const atLatest = this.idx === n - 1;
      live.classList.toggle('on', this.live && !this.finished && atLatest);
      live.lastChild.textContent = this.finished ? 'End' : 'Live';
      $('#btn-prev').disabled = this.idx <= 0;
      $('#btn-first').disabled = this.idx <= 0;
      $('#btn-next').disabled = atLatest;
    },

    renderBanner() {
      const res = this.latest?.victory?.result;
      const b = $('#banner');
      if (!this.finished || !res) { b.hidden = true; return; }
      const w = this.player(res.winner);
      b.hidden = false;
      b.innerHTML = `<span class="trophy">${icon('trophy', 'var(--accent)')}</span>
        <span><b>${this.chip(res.winner)}</b> won by <b>${esc(COND_LABEL[res.condition] || human(res.condition))}</b> victory on turn ${esc(res.turn ?? this.latest.turn)}${w ? '' : ''}.</span>
        <button class="btn small" id="show-results">Final standings</button>`;
      $('#show-results').onclick = () => {
        this.overlayDismissed = false;
        this.idx = this.frames.length - 1;
        this.render();
      };
    },

    renderOverlay() {
      const el = $('#overlay');
      const v = this.view;
      const res = v?.victory?.result;
      if (!res || v.status !== 'finished' || this.overlayDismissed || this.idx !== this.frames.length - 1) {
        el.hidden = true;
        return;
      }
      const placements = res.placements || (v.players || []).map((p) => p.id);
      const scores = res.scores || {};
      el.hidden = false;
      el.innerHTML = `<div class="victory-card" role="dialog" aria-label="Game over">
        <svg class="crown" viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linejoin="round"><path d="M8 34l-3-20 11 9 8-14 8 14 11-9-3 20z" fill="rgba(243,201,105,.15)"/><path d="M9 40h30"/></svg>
        <div class="kicker">Game over · turn ${esc(res.turn ?? v.turn)}</div>
        <h2>${this.chip(res.winner)} wins</h2>
        <div class="cond">${esc(COND_LABEL[res.condition] || human(res.condition))} victory</div>
        <ol class="placements">${placements.map((pid, i) => {
          const p = this.player(pid);
          const dead = p && p.alive === false;
          return `<li><span class="place">${i + 1}</span><span>${this.chip(pid)}${dead ? ` <span class="dead-tag">eliminated${p.eliminated_turn != null ? ' T' + p.eliminated_turn : ''}</span>` : ''}</span><b>${fmt(num(scores[pid], p?.score ?? 0))}</b></li>`;
        }).join('')}</ol>
        <div class="actions"><button class="btn" id="ov-close">View map</button><button class="btn primary" id="ov-replay">Replay from start</button></div>
      </div>`;
      $('#ov-close').onclick = () => { this.overlayDismissed = true; el.hidden = true; };
      $('#ov-replay').onclick = () => { this.overlayDismissed = true; this.idx = 0; this.render(); this.play(); };
    },

    // --- helpers ----------------------------------------------------------
    player(pid) {
      const v = this.view || this.latest;
      return (v?.players || []).find((p) => p.id === pid) || (this.latest?.players || []).find((p) => p.id === pid) || null;
    },
    color(pid) {
      const p = this.player(pid);
      if (p?.color) return p.color;
      const n = parseInt(String(pid).replace(/\D/g, ''), 10);
      return PALETTE[((n || 1) - 1) % PALETTE.length];
    },
    pname(pid) { return this.player(pid)?.name || pid || '?'; },
    chip(pid) {
      if (!pid) return '<span class="muted">nobody</span>';
      if (pid === 'all') return '<span class="muted">everyone</span>';
      const p = this.player(pid);
      return `<span class="pchip${p && p.alive === false ? ' dead' : ''}" data-pid="${esc(pid)}" title="${esc(pid)}"><i class="swatch" style="background:${esc(this.color(pid))}"></i>${esc(this.pname(pid))}</span>`;
    },

    // ======================================= 6. map rendering + tooltip
    computeLayout() {
      const v = this.view;
      const wrap = $('#map-wrap');
      if (!v?.map?.terrain) return null;
      const W = v.map.width || v.map.terrain[0].length;
      const H = v.map.height || v.map.terrain.length;
      const availW = wrap.clientWidth - 16;
      const availH = wrap.clientHeight - 16;
      const tile = Math.max(6, Math.floor(Math.min(availW / W, availH / H)));
      return { W, H, tile, dpr: window.devicePixelRatio || 1 };
    },

    drawMap() {
      const v = this.view;
      const canvas = $('#map');
      if (!v?.map?.terrain || canvas.hidden) return;
      const L = this.computeLayout();
      if (!L) return;
      this.layout = L;
      const { W, H, tile: t, dpr } = L;
      const cssW = W * t;
      const cssH = H * t;
      if (canvas.width !== Math.round(cssW * dpr) || canvas.height !== Math.round(cssH * dpr)) {
        canvas.width = Math.round(cssW * dpr);
        canvas.height = Math.round(cssH * dpr);
        canvas.style.width = cssW + 'px';
        canvas.style.height = cssH + 'px';
      }
      const ctx = canvas.getContext('2d');
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, cssW, cssH);
      ctx.drawImage(this.terrainLayer(v, L), 0, 0, cssW, cssH);

      const owner = v.map.owner || [];
      const hl = this.highlight;
      const own = (x, y) => (x < 0 || y < 0 || x >= W || y >= H ? undefined : owner[y]?.[x] ?? null);

      // Territory tint.
      for (let y = 0; y < H; y++) {
        for (let x = 0; x < W; x++) {
          const o = own(x, y);
          if (!o) continue;
          const a = hl ? (o === hl ? 0.46 : 0.14) : 0.32;
          ctx.fillStyle = rgba(this.color(o), a);
          ctx.fillRect(x * t, y * t, t, t);
        }
      }
      // Subtle grid.
      if (t >= 14) {
        ctx.strokeStyle = 'rgba(0,0,0,0.13)';
        ctx.lineWidth = 1;
        ctx.beginPath();
        for (let x = 1; x < W; x++) { ctx.moveTo(x * t + 0.5, 0); ctx.lineTo(x * t + 0.5, cssH); }
        for (let y = 1; y < H; y++) { ctx.moveTo(0, y * t + 0.5); ctx.lineTo(cssW, y * t + 0.5); }
        ctx.stroke();
      }
      // Deposits (thin remaining-amount bar) and improvements.
      const depLookup = new Map((v.map.deposits || []).map((d) => [d.x + ',' + d.y, d]));
      for (const d of depLookup.values()) this.drawDeposit(ctx, d, t);
      for (const imp of v.map.improvements || []) this.drawImprovement(ctx, imp, t, hl && own(imp.x, imp.y) !== hl);

      // Dim non-highlighted territory.
      if (hl) {
        ctx.fillStyle = 'rgba(6,8,12,0.42)';
        for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) if (own(x, y) !== hl) ctx.fillRect(x * t, y * t, t, t);
      }

      // Borders: inner coloured edge per owner + dark seam between different owners.
      const bw = Math.max(1.5, t * 0.1);
      for (let y = 0; y < H; y++) {
        for (let x = 0; x < W; x++) {
          const o = own(x, y);
          if (!o) continue;
          ctx.strokeStyle = this.color(o);
          ctx.globalAlpha = hl && o !== hl ? 0.45 : 1;
          ctx.lineWidth = bw;
          ctx.beginPath();
          const h = bw / 2;
          if (own(x, y - 1) !== o) { ctx.moveTo(x * t, y * t + h); ctx.lineTo(x * t + t, y * t + h); }
          if (own(x, y + 1) !== o) { ctx.moveTo(x * t, y * t + t - h); ctx.lineTo(x * t + t, y * t + t - h); }
          if (own(x - 1, y) !== o) { ctx.moveTo(x * t + h, y * t); ctx.lineTo(x * t + h, y * t + t); }
          if (own(x + 1, y) !== o) { ctx.moveTo(x * t + t - h, y * t); ctx.lineTo(x * t + t - h, y * t + t); }
          ctx.stroke();
        }
      }
      ctx.globalAlpha = 1;
      ctx.strokeStyle = 'rgba(0,0,0,0.55)';
      ctx.lineWidth = 1;
      ctx.beginPath();
      for (let y = 0; y < H; y++) {
        for (let x = 0; x < W; x++) {
          const o = own(x, y);
          const r = own(x + 1, y);
          const d = own(x, y + 1);
          if (r !== undefined && o !== r && (o || r)) { ctx.moveTo(x * t + t, y * t); ctx.lineTo(x * t + t, y * t + t); }
          if (d !== undefined && o !== d && (o || d)) { ctx.moveTo(x * t, y * t + t); ctx.lineTo(x * t + t, y * t + t); }
        }
      }
      ctx.stroke();

      for (const r of v.map.relics || []) this.drawRelic(ctx, r, t, hl && r.owner !== hl);
      for (const c of v.cities || []) this.drawCity(ctx, c, t, hl && c.owner !== hl);
      const perTile = new Map();
      for (const a of v.armies || []) {
        const k = a.x + ',' + a.y;
        const n = perTile.get(k) || 0;
        perTile.set(k, n + 1);
        this.drawArmy(ctx, a, t, n, hl && a.owner !== hl);
      }
      for (const e of this.recentEvents(v)) {
        if (e.type === 'battle') this.drawBattle(ctx, e, t);
        else if (e.type === 'city_captured') this.drawCapture(ctx, e, t);
      }
      if (this.hover) {
        ctx.strokeStyle = 'rgba(255,255,255,0.95)';
        ctx.lineWidth = 2;
        ctx.strokeRect(this.hover.x * t + 1, this.hover.y * t + 1, t - 2, t - 2);
      }
      if (this.flash) {
        const k = 0.5 + 0.5 * Math.sin(performance.now() / 110);
        ctx.strokeStyle = `rgba(96,165,250,${0.5 + 0.5 * k})`;
        ctx.lineWidth = 3;
        const g = 3 + k * 4;
        ctx.strokeRect(this.flash.x * t - g, this.flash.y * t - g, t + 2 * g, t + 2 * g);
      }
    },

    /** Last turn's events carried by frame v (events of turn v.turn-1, or unlabelled ones). */
    recentEvents(v) {
      return (v.events || []).filter((e) => e.turn == null || e.turn >= v.turn - 1);
    },

    /** Terrain is static for a game, so it is rendered once per size into an offscreen canvas. */
    terrainLayer(v, L) {
      const size = `${L.W}x${L.H}@${L.tile}x${L.dpr}`;
      const tc = this.terrainCache;
      if (tc && tc.size === size && (tc.ref === v.map.terrain || tc.text === v.map.terrain.join(''))) {
        tc.ref = v.map.terrain;
        return tc.canvas;
      }
      const key = v.map.terrain.join('');
      const t = L.tile;
      const c = document.createElement('canvas');
      c.width = Math.round(L.W * t * L.dpr);
      c.height = Math.round(L.H * t * L.dpr);
      const ctx = c.getContext('2d');
      ctx.setTransform(L.dpr, 0, 0, L.dpr, 0, 0);
      for (let y = 0; y < L.H; y++) {
        const row = v.map.terrain[y] || '';
        for (let x = 0; x < L.W; x++) this.drawTerrainTile(ctx, row[x] || '.', x, y, t);
      }
      this.terrainCache = { size, ref: v.map.terrain, text: key, canvas: c };
      return c;
    },

    drawTerrainTile(ctx, ch, x, y, t) {
      const info = TERRAIN[ch] || TERRAIN['.'];
      const px = x * t;
      const py = y * t;
      const r1 = hash(x, y, 1);
      const r2 = hash(x, y, 2);
      ctx.fillStyle = info.color;
      ctx.fillRect(px, py, t, t);
      ctx.fillStyle = r1 > 0.5 ? `rgba(255,255,255,${(r1 - 0.5) * 0.08})` : `rgba(0,0,0,${(0.5 - r1) * 0.12})`;
      ctx.fillRect(px, py, t, t);
      if (t < 9) return;
      ctx.lineWidth = Math.max(1, t / 18);
      ctx.lineCap = 'round';
      if (ch === '.') {
        ctx.strokeStyle = 'rgba(160,200,110,0.35)';
        ctx.beginPath();
        for (let i = 0; i < 3; i++) {
          const gx = px + t * (0.2 + 0.6 * hash(x, y, 10 + i));
          const gy = py + t * (0.25 + 0.6 * hash(x, y, 20 + i));
          ctx.moveTo(gx, gy); ctx.lineTo(gx + t * 0.04, gy - t * 0.09);
        }
        ctx.stroke();
      } else if (ch === 'f') {
        const trees = [[0.3, 0.42], [0.68, 0.36], [0.5, 0.74]];
        for (const [tx, ty] of trees) {
          const cx = px + t * (tx + (r2 - 0.5) * 0.1);
          const cy = py + t * ty;
          const s = t * 0.2;
          ctx.fillStyle = 'rgba(14,38,20,0.85)';
          ctx.beginPath();
          ctx.moveTo(cx, cy - s); ctx.lineTo(cx + s * 0.8, cy + s * 0.6); ctx.lineTo(cx - s * 0.8, cy + s * 0.6);
          ctx.closePath(); ctx.fill();
          ctx.fillStyle = 'rgba(88,140,80,0.5)';
          ctx.beginPath();
          ctx.moveTo(cx, cy - s); ctx.lineTo(cx + s * 0.25, cy - s * 0.2); ctx.lineTo(cx - s * 0.5, cy + s * 0.3);
          ctx.closePath(); ctx.fill();
        }
      } else if (ch === 'h' || ch === 'g') {
        ctx.strokeStyle = 'rgba(40,30,15,0.55)';
        ctx.lineWidth = Math.max(1.2, t / 14);
        ctx.beginPath();
        ctx.arc(px + t * 0.33, py + t * 0.7, t * 0.2, Math.PI * 1.1, Math.PI * 1.9);
        ctx.moveTo(px + t * 0.52, py + t * 0.55);
        ctx.arc(px + t * 0.68, py + t * 0.58, t * 0.17, Math.PI * 1.1, Math.PI * 1.9);
        ctx.stroke();
        if (ch === 'g') {
          ctx.fillStyle = '#f3c969';
          ctx.shadowColor = 'rgba(243,201,105,0.8)';
          ctx.shadowBlur = t * 0.2;
          for (let i = 0; i < 3; i++) {
            ctx.beginPath();
            ctx.arc(px + t * (0.25 + 0.5 * hash(x, y, 30 + i)), py + t * (0.2 + 0.3 * hash(x, y, 40 + i)), Math.max(1, t * 0.055), 0, Math.PI * 2);
            ctx.fill();
          }
          ctx.shadowBlur = 0;
        }
      } else if (ch === 'm') {
        ctx.fillStyle = '#6a6e7a';
        ctx.beginPath();
        ctx.moveTo(px + t * 0.12, py + t * 0.86); ctx.lineTo(px + t * 0.5, py + t * 0.14); ctx.lineTo(px + t * 0.88, py + t * 0.86);
        ctx.closePath(); ctx.fill();
        ctx.fillStyle = '#e8ebf2';
        ctx.beginPath();
        ctx.moveTo(px + t * 0.5, py + t * 0.14); ctx.lineTo(px + t * 0.62, py + t * 0.36); ctx.lineTo(px + t * 0.5, py + t * 0.32);
        ctx.lineTo(px + t * 0.39, py + t * 0.36); ctx.closePath(); ctx.fill();
        ctx.fillStyle = 'rgba(0,0,0,0.22)';
        ctx.beginPath();
        ctx.moveTo(px + t * 0.5, py + t * 0.14); ctx.lineTo(px + t * 0.88, py + t * 0.86); ctx.lineTo(px + t * 0.58, py + t * 0.86);
        ctx.closePath(); ctx.fill();
      } else if (ch === '~') {
        ctx.strokeStyle = 'rgba(120,180,230,0.35)';
        ctx.lineWidth = Math.max(1, t / 16);
        for (let i = 0; i < 2; i++) {
          const wy = py + t * (0.35 + 0.35 * i) + (r2 - 0.5) * t * 0.1;
          const wx = px + t * (0.15 + 0.2 * i);
          ctx.beginPath();
          ctx.moveTo(wx, wy);
          ctx.quadraticCurveTo(wx + t * 0.12, wy - t * 0.08, wx + t * 0.25, wy);
          ctx.quadraticCurveTo(wx + t * 0.38, wy + t * 0.08, wx + t * 0.5, wy);
          ctx.stroke();
        }
      }
    },

    drawDeposit(ctx, d, t) {
      if (t < 10) return;
      const max = Rules.depositMax(d.resource) || Math.max(1, num(d.remaining));
      const frac = clamp(num(d.remaining) / max, 0, 1);
      const x = d.x * t + t * 0.14;
      const y = d.y * t + t * 0.86;
      const w = t * 0.72;
      ctx.fillStyle = 'rgba(0,0,0,0.45)';
      ctx.fillRect(x, y, w, Math.max(2, t * 0.07));
      ctx.fillStyle = frac > 0 ? (d.resource === 'gold' ? '#f3c969' : '#cfd6e2') : '#f87171';
      ctx.fillRect(x, y, frac > 0 ? w * frac : Math.max(2, t * 0.08), Math.max(2, t * 0.07));
    },

    drawImprovement(ctx, imp, t, dim) {
      const s = t * 0.32;
      const x = imp.x * t + t * 0.08;
      const y = imp.y * t + t * 0.08;
      ctx.save();
      if (dim) ctx.globalAlpha = 0.45;
      ctx.fillStyle = 'rgba(10,12,16,0.55)';
      ctx.beginPath();
      ctx.roundRect ? ctx.roundRect(x - 1, y - 1, s + 2, s + 2, 3) : ctx.rect(x - 1, y - 1, s + 2, s + 2);
      ctx.fill();
      ctx.lineWidth = Math.max(1, s / 7);
      ctx.lineCap = 'round';
      switch (imp.building) {
        case 'farm':
          ctx.strokeStyle = '#e9d67a';
          ctx.beginPath();
          for (let i = 0; i < 3; i++) { ctx.moveTo(x + s * 0.18, y + s * (0.25 + 0.25 * i)); ctx.lineTo(x + s * 0.82, y + s * (0.25 + 0.25 * i)); }
          ctx.stroke();
          break;
        case 'lumber_mill':
          ctx.fillStyle = '#c08a55';
          ctx.fillRect(x + s * 0.15, y + s * 0.38, s * 0.7, s * 0.26);
          ctx.fillStyle = '#e8c49a';
          ctx.beginPath(); ctx.arc(x + s * 0.8, y + s * 0.51, s * 0.13, 0, Math.PI * 2); ctx.fill();
          break;
        case 'quarry':
          ctx.fillStyle = '#cfd6e2';
          ctx.fillRect(x + s * 0.15, y + s * 0.5, s * 0.32, s * 0.32);
          ctx.fillRect(x + s * 0.53, y + s * 0.5, s * 0.32, s * 0.32);
          ctx.fillRect(x + s * 0.34, y + s * 0.15, s * 0.32, s * 0.32);
          break;
        case 'mine':
          ctx.fillStyle = '#111';
          ctx.strokeStyle = '#f3c969';
          ctx.beginPath();
          ctx.moveTo(x + s * 0.15, y + s * 0.85); ctx.lineTo(x + s * 0.15, y + s * 0.5);
          ctx.arc(x + s * 0.5, y + s * 0.5, s * 0.35, Math.PI, 0); ctx.lineTo(x + s * 0.85, y + s * 0.85);
          ctx.closePath(); ctx.fill(); ctx.stroke();
          break;
        case 'temple':
          ctx.fillStyle = '#f1eadb';
          ctx.beginPath();
          ctx.moveTo(x + s * 0.1, y + s * 0.38); ctx.lineTo(x + s * 0.5, y + s * 0.1); ctx.lineTo(x + s * 0.9, y + s * 0.38);
          ctx.closePath(); ctx.fill();
          for (let i = 0; i < 3; i++) ctx.fillRect(x + s * (0.18 + 0.27 * i), y + s * 0.42, s * 0.12, s * 0.36);
          ctx.fillRect(x + s * 0.1, y + s * 0.8, s * 0.8, s * 0.1);
          break;
        default:
          ctx.fillStyle = '#ddd';
          ctx.beginPath(); ctx.arc(x + s / 2, y + s / 2, s * 0.2, 0, Math.PI * 2); ctx.fill();
      }
      ctx.restore();
    },

    drawRelic(ctx, r, t, dim) {
      const cx = r.x * t + t / 2;
      const cy = r.y * t + t / 2;
      ctx.save();
      if (dim) ctx.globalAlpha = 0.5;
      ctx.beginPath();
      ctx.arc(cx, cy, t * 0.4, 0, Math.PI * 2);
      if (r.owner) {
        ctx.strokeStyle = this.color(r.owner);
        ctx.lineWidth = Math.max(2, t * 0.09);
        ctx.setLineDash([]);
      } else {
        ctx.strokeStyle = 'rgba(220,230,255,0.55)';
        ctx.lineWidth = Math.max(1, t * 0.05);
        ctx.setLineDash([t * 0.12, t * 0.08]);
      }
      ctx.stroke();
      ctx.setLineDash([]);
      const s = t * 0.24;
      const g = ctx.createLinearGradient(cx, cy - s, cx, cy + s);
      g.addColorStop(0, '#e0f7ff');
      g.addColorStop(0.5, '#67e8f9');
      g.addColorStop(1, '#a78bfa');
      ctx.shadowColor = '#67e8f9';
      ctx.shadowBlur = t * 0.5;
      ctx.fillStyle = g;
      ctx.beginPath();
      ctx.moveTo(cx, cy - s * 1.2); ctx.lineTo(cx + s * 0.8, cy); ctx.lineTo(cx, cy + s * 1.2); ctx.lineTo(cx - s * 0.8, cy);
      ctx.closePath();
      ctx.fill();
      ctx.shadowBlur = 0;
      ctx.strokeStyle = 'rgba(255,255,255,0.8)';
      ctx.lineWidth = 1;
      ctx.stroke();
      ctx.restore();
    },

    drawCity(ctx, c, t, dim) {
      const cx = c.x * t + t / 2;
      const cy = c.y * t + t / 2;
      const col = this.color(c.owner);
      const s = t * 0.66;
      ctx.save();
      if (dim) ctx.globalAlpha = 0.5;
      // Walls: one concentric stone outline per level.
      const walls = num(c.buildings?.walls);
      for (let i = walls; i >= 1; i--) {
        const g = s / 2 + i * Math.max(1.6, t * 0.07);
        ctx.strokeStyle = i % 2 ? '#d7dbe3' : '#8f96a3';
        ctx.lineWidth = Math.max(1.2, t * 0.05);
        ctx.beginPath();
        ctx.roundRect ? ctx.roundRect(cx - g, cy - g, 2 * g, 2 * g, t * 0.12) : ctx.rect(cx - g, cy - g, 2 * g, 2 * g);
        ctx.stroke();
      }
      ctx.shadowColor = 'rgba(0,0,0,0.6)';
      ctx.shadowBlur = t * 0.2;
      ctx.fillStyle = col;
      ctx.beginPath();
      ctx.roundRect ? ctx.roundRect(cx - s / 2, cy - s / 2, s, s, t * 0.1) : ctx.rect(cx - s / 2, cy - s / 2, s, s);
      ctx.fill();
      ctx.shadowBlur = 0;
      ctx.strokeStyle = 'rgba(10,10,14,0.9)';
      ctx.lineWidth = Math.max(1, t * 0.05);
      ctx.stroke();
      const ink = luminance(col) > 0.6 ? '#141414' : '#ffffff';
      if (c.capital) {
        // Star; captured capitals show the original owner's colour inside the star.
        const r = s * 0.36;
        ctx.beginPath();
        for (let i = 0; i < 10; i++) {
          const a = -Math.PI / 2 + (i * Math.PI) / 5;
          const rr = i % 2 ? r * 0.45 : r;
          ctx.lineTo(cx + Math.cos(a) * rr, cy + Math.sin(a) * rr);
        }
        ctx.closePath();
        const captured = c.original_owner && c.original_owner !== c.owner;
        ctx.fillStyle = captured ? this.color(c.original_owner) : ink;
        ctx.fill();
        if (captured) { ctx.strokeStyle = ink; ctx.lineWidth = 1; ctx.stroke(); }
      } else {
        // House glyph.
        const r = s * 0.28;
        ctx.fillStyle = ink;
        ctx.beginPath();
        ctx.moveTo(cx - r, cy); ctx.lineTo(cx, cy - r); ctx.lineTo(cx + r, cy);
        ctx.lineTo(cx + r, cy + r * 0.9); ctx.lineTo(cx - r, cy + r * 0.9);
        ctx.closePath(); ctx.fill();
      }
      // Wonder: 5-segment arc above the city.
      const ws = num(c.wonder_stage);
      const WS = Math.max(1, Rules.wonderMax(this.view?.victory?.thresholds));
      if (ws > 0) {
        const R = s * 0.5 + Math.max(3, t * 0.14) + walls * Math.max(1.6, t * 0.07);
        ctx.lineWidth = Math.max(2, t * 0.09);
        for (let i = 0; i < WS; i++) {
          const a0 = Math.PI * 1.08 + i * (Math.PI * 0.84 / WS);
          ctx.beginPath();
          ctx.arc(cx, cy, R, a0, a0 + Math.PI * 0.84 / WS - 0.07);
          ctx.strokeStyle = i < ws ? '#f3c969' : 'rgba(243,201,105,0.2)';
          if (i < ws) { ctx.shadowColor = '#f3c969'; ctx.shadowBlur = t * 0.25; }
          ctx.stroke();
          ctx.shadowBlur = 0;
        }
      }
      ctx.restore();
    },

    drawArmy(ctx, a, t, stackIdx, dim) {
      const total = UNITS.reduce((s, u) => s + num(a.units?.[u]), 0) ||
        Object.values(a.units || {}).reduce((s, n) => s + num(n), 0);
      if (!total) return;
      const col = this.color(a.owner);
      const fs = Math.max(8, Math.round(t * 0.34));
      ctx.save();
      if (dim) ctx.globalAlpha = 0.5;
      ctx.font = `700 ${fs}px ${getComputedStyle(document.body).fontFamily}`;
      const label = String(total);
      const w = Math.max(fs * 1.25, ctx.measureText(label).width + fs * 0.6);
      const h = fs * 1.25;
      const x = a.x * t + t - w - 1;
      const y = a.y * t + t - h - 1 - stackIdx * (h + 1);
      ctx.shadowColor = 'rgba(0,0,0,0.7)';
      ctx.shadowBlur = 3;
      ctx.fillStyle = col;
      ctx.beginPath();
      ctx.roundRect ? ctx.roundRect(x, y, w, h, h / 2) : ctx.rect(x, y, w, h);
      ctx.fill();
      ctx.shadowBlur = 0;
      ctx.strokeStyle = 'rgba(0,0,0,0.85)';
      ctx.lineWidth = 1;
      ctx.stroke();
      ctx.fillStyle = luminance(col) > 0.6 ? '#111' : '#fff';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(label, x + w / 2, y + h / 2 + 0.5);
      ctx.restore();
    },

    drawBattle(ctx, e, t) {
      if (typeof e.x !== 'number') return;
      const px = e.x * t;
      const py = e.y * t;
      ctx.save();
      ctx.strokeStyle = 'rgba(248,113,113,0.95)';
      ctx.shadowColor = '#f87171';
      ctx.shadowBlur = t * 0.4;
      ctx.lineWidth = Math.max(2, t * 0.08);
      ctx.strokeRect(px + 1.5, py + 1.5, t - 3, t - 3);
      // Crossed swords badge in the top-right corner.
      const r = t * 0.22;
      const cx = px + t - r - 1;
      const cy = py + r + 1;
      ctx.shadowBlur = 0;
      ctx.fillStyle = '#7f1d1d';
      ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2); ctx.fill();
      ctx.strokeStyle = '#fee2e2';
      ctx.lineWidth = Math.max(1.2, t * 0.05);
      ctx.lineCap = 'round';
      const k = r * 0.55;
      ctx.beginPath();
      ctx.moveTo(cx - k, cy - k); ctx.lineTo(cx + k, cy + k);
      ctx.moveTo(cx + k, cy - k); ctx.lineTo(cx - k, cy + k);
      ctx.stroke();
      ctx.restore();
    },

    drawCapture(ctx, e, t) {
      if (typeof e.x !== 'number') return;
      ctx.save();
      ctx.strokeStyle = '#fb923c';
      ctx.shadowColor = '#fb923c';
      ctx.shadowBlur = t * 0.5;
      ctx.lineWidth = Math.max(2, t * 0.09);
      ctx.setLineDash([t * 0.18, t * 0.1]);
      ctx.strokeRect(e.x * t - 2, e.y * t - 2, t + 4, t + 4);
      ctx.restore();
    },

    tileAt(ev) {
      const L = this.layout;
      if (!L) return null;
      const r = $('#map').getBoundingClientRect();
      const x = Math.floor((ev.clientX - r.left) / L.tile);
      const y = Math.floor((ev.clientY - r.top) / L.tile);
      if (x < 0 || y < 0 || x >= L.W || y >= L.H) return null;
      return { x, y };
    },

    onHover(ev) {
      const t = this.tileAt(ev);
      const tip = $('#tooltip');
      if (!t) { tip.hidden = true; return; }
      if (!this.hover || this.hover.x !== t.x || this.hover.y !== t.y) {
        this.hover = t;
        tip.innerHTML = this.tileInfo(t.x, t.y);
        this.drawMap();
      }
      tip.hidden = false;
      const wrap = $('#map-wrap').getBoundingClientRect();
      const tw = tip.offsetWidth;
      const th = tip.offsetHeight;
      let left = ev.clientX - wrap.left + 16;
      let top = ev.clientY - wrap.top + 16;
      if (left + tw > wrap.width - 6) left = ev.clientX - wrap.left - tw - 14;
      if (top + th > wrap.height - 6) top = Math.max(6, wrap.height - th - 6);
      tip.style.left = Math.max(6, left) + 'px';
      tip.style.top = top + 'px';
    },

    tileInfo(x, y) {
      const v = this.view;
      const ch = v.map.terrain[y]?.[x] || '.';
      const ter = Rules.terrain(ch);
      const o = v.map.owner?.[y]?.[x] || null;
      const at = (arr) => (arr || []).filter((i) => i.x === x && i.y === y);
      const imp = at(v.map.improvements)[0];
      const dep = at(v.map.deposits)[0];
      const city = at(v.cities)[0];
      const relic = at(v.map.relics)[0];
      const armies = at(v.armies);
      const battles = this.recentEvents(v).filter((e) => e.x === x && e.y === y && (e.type === 'battle' || e.type === 'city_captured'));
      const parts = [];
      parts.push(`<div class="tt-head"><span>${esc(ter.name)}</span><span class="muted">(${x}, ${y})</span></div>`);
      parts.push(`<div><span class="tt-k">Owner:</span> ${o ? this.chip(o) : '<span class="muted">unclaimed</span>'}</div>`);
      if (!city && ter.yield) parts.push(`<div><span class="tt-k">Base yield:</span> ${esc(ter.yield)}</div>`);
      if (imp) parts.push(`<div><span class="tt-k">Improvement:</span> ${esc(human(imp.building))} <span class="muted">${esc(Rules.building(imp.building))}</span></div>`);
      if (dep) parts.push(`<div><span class="tt-k">Deposit:</span> ${fmt(num(dep.remaining))} ${esc(dep.resource)} left${num(dep.remaining) === 0 ? ' <span class="betray">(depleted)</span>' : ''}</div>`);
      if (city) {
        const b = city.buildings || {};
        const bits = [];
        if (num(b.walls)) bits.push(`walls ${b.walls}`);
        if (num(b.warehouse)) bits.push('warehouse');
        if (num(b.market_hall)) bits.push('market hall');
        if (num(city.wonder_stage)) bits.push(`<b style="color:var(--accent)">wonder ${city.wonder_stage}/${Rules.wonderMax(v.victory?.thresholds)}</b>`);
        parts.push(`<div class="tt-sec"><b>${esc(city.name || 'City')}</b>${city.capital ? ' <span style="color:var(--accent)">★ capital</span>' : ''}
          <div><span class="tt-k">Held by</span> ${this.chip(city.owner)}${city.original_owner && city.original_owner !== city.owner ? ` <span class="muted">(founded by</span> ${this.chip(city.original_owner)}<span class="muted">)</span>` : ''}</div>
          <div class="muted">${bits.length ? bits.join(' · ') : 'no buildings'}${city.garrison != null ? ` · garrison ${city.garrison}` : ''}</div></div>`);
      }
      if (relic) parts.push(`<div class="tt-sec"><b style="color:#67e8f9">◆ Relic</b> — ${relic.owner ? 'held by ' + this.chip(relic.owner) : '<span class="muted">unclaimed</span>'}<div class="muted">+${Rules.relicInfluence()} influence/turn to its holder</div></div>`);
      for (const a of armies) {
        const u = a.units || {};
        const power = Object.entries(u).reduce((s, [k, n]) => s + num(n) * Rules.strength(k), 0);
        const list = Object.entries(u).filter(([, n]) => n).map(([k, n]) => `${n} ${k}`).join(', ');
        parts.push(`<div class="tt-sec">${this.chip(a.owner)} army <span class="muted">· power ${power}</span><div class="units">${esc(list)}</div></div>`);
      }
      for (const e of battles) parts.push(`<div class="tt-sec" style="color:#fca5a5">${this.describe(e).html}</div>`);
      return parts.join('');
    },

    // ======================================================= 7. sidebar
    renderStatus(v) {
      const s = v.season || {};
      const mods = s.modifiers || {};
      const modHtml = Object.keys(mods).map((r) => {
        const m = num(mods[r], 1);
        return `<span class="mod ${m > 1 ? 'up' : m < 1 ? 'down' : ''}" title="${esc(r)} yield ×${m}">${esc(RES_SHORT[r] || r)} ×${m}</span>`;
      }).join('');
      const maxT = num(v.max_turns, 150);
      $('#status-panel').innerHTML = `
        <div class="status-top">
          <span class="turn">Turn ${esc(v.turn)} <small>/ ${esc(maxT)}</small></span>
          <span class="badge ${esc(v.status)}">${esc(v.status || '?')}</span>
          ${!this.live && !this.finished ? '<span class="badge">replay</span>' : ''}
          <span class="deadline" id="deadline"></span>
        </div>
        <div class="progress" title="${pct(v.turn / maxT)} of the turn limit"><i style="width:${pct(v.turn / maxT)}"></i></div>
        ${v.season ? `<div class="season">
          <span class="sname"><i style="background:${SEASON_COLOR[s.name] || '#888'}"></i>${esc(s.name)}</span>
          ${modHtml}
          <span class="muted">${esc(s.turns_left ?? '?')} turn${s.turns_left === 1 ? '' : 's'} left → ${esc(s.next || '')}</span>
        </div>` : ''}`;
      this.renderDeadline();
    },

    renderDeadline() {
      const auto = $('#lobby-auto');
      if (auto && this.meta?.lobby_timeout && this.meta?.created) {
        auto.textContent = fmtDur(this.meta.created + this.meta.lobby_timeout - serverNow());
      }
      const el = $('#deadline');
      if (!el) return;
      const v = this.view;
      if (!this.live || v?.status !== 'running' || this.idx !== this.frames.length - 1) { el.textContent = ''; el.title = ''; return; }
      const alive = (v.players || []).filter((p) => p.alive !== false);
      const sub = alive.filter((p) => p.submitted).length;
      const waiting = alive.filter((p) => !p.submitted && this.meta && !this.isBot(p.id)).map((p) => p.name || p.id);
      const dl = num(v.deadline, 0);
      const left = dl ? dl - serverNow() : null;
      let txt = `${sub}/${alive.length} submitted`;
      if (left != null) txt += left > 0 ? ` · next turn ≤ ${fmtDur(left)}` : ' · resolving…';
      else if (waiting.length) txt += ' · no deadline';
      if (waiting.length) txt += ` · waiting for ${waiting.length <= 2 ? waiting.join(', ') : waiting.length + ' agents'}`;
      el.textContent = txt;
      el.title = waiting.length ? 'Remote agents yet to submit: ' + waiting.join(', ') : '';
    },

    sortedPlayers(v) {
      return (v.players || []).slice().sort((a, b) =>
        (b.alive !== false) - (a.alive !== false) || num(b.score) - num(a.score) ||
        num(b.eliminated_turn) - num(a.eliminated_turn));
    },

    renderPlayers(v) {
      if (!v) return;
      const rows = this.sortedPlayers(v);
      const resHead = RESOURCES.map((r) =>
        `<th class="num" title="${r} (per-turn income below)"><span class="rh"><i style="background:${RES_COLOR[r]}"></i>${RES_SHORT[r]}</span></th>`).join('');
      const cell = (p, r) => {
        const val = p.resources?.[r];
        const inc = p.income?.[r];
        const incHtml = typeof inc === 'number' && p.alive !== false
          ? `<span class="inc${inc < 0 ? ' neg' : ''}">${inc >= 0 ? '+' : ''}${fmt(inc)}</span>` : '';
        return `<td class="num">${fmt(val)}${incHtml}</td>`;
      };
      $('#players-table').innerHTML = `<thead><tr><th>Player</th>
          <th class="num" title="Cities">City</th><th class="num" title="Tiles owned">Tiles</th>${resHead}
          <th class="num" title="Military power (Σ count × strength)">Mil</th><th class="num">Score</th></tr></thead>
        <tbody>${rows.map((p) => {
          const dead = p.alive === false;
          const title = `${p.id}${p.betrayals ? ` · ${p.betrayals} betrayal(s)` : ''}${dead ? ` · eliminated turn ${p.eliminated_turn}` : ''}`;
          const units = p.units ? UNITS.filter((u) => p.units[u]).map((u) => `${p.units[u]} ${u}`).join(', ') : '';
          return `<tr data-pid="${esc(p.id)}" class="${dead ? 'dead' : ''}${this.highlight === p.id ? ' hl' : ''}" title="${esc(title)}">
            <td><div class="pname"><span class="swatch" style="background:${esc(this.color(p.id))}"></span>
              <b>${esc(p.name || p.id)}</b>${this.meta && this.seat(p.id) && !this.isBot(p.id) ? `<span class="tag agent" title="remote agent (HTTP / SDK / MCP)">${icon('plug')}</span>` : ''}${dead ? `<span class="dead-tag">✝${p.eliminated_turn != null ? 'T' + esc(p.eliminated_turn) : ''}</span>`
                : `<span class="sub${p.submitted ? ' yes' : ''}" title="${p.submitted ? 'orders submitted for this turn' : 'no orders yet this turn'}"></span>`}
              ${num(p.betrayals) ? `<span class="betray" title="betrayals">⚑${p.betrayals}</span>` : ''}</div></td>
            <td class="num">${fmt(p.cities)}</td><td class="num">${fmt(p.tiles)}</td>
            ${RESOURCES.map((r) => cell(p, r)).join('')}
            <td class="num" title="${esc(units)}">${fmt(p.military_power)}</td>
            <td class="num score">${fmt(p.score)}</td></tr>`;
        }).join('')}</tbody>`;
    },

    renderRace(v) {
      if (!v) return;
      const th = v.victory?.thresholds || {};
      const wmax = Rules.wonderMax(th);
      const rows = this.sortedPlayers(v);
      const need = {
        conquest: `${th.conquest_capitals ?? '?'} capitals`,
        wonder: `stage ${wmax}`,
        influence: `${fmt(th.influence)} inf`,
        relics: `${th.relics_needed ?? '?'}× ${th.relic_turns ?? '?'}t`,
        economic: `${fmt(th.economic_gold)} gold`,
      };
      const detail = (p, c) => {
        const r = p.resources || {};
        switch (c) {
          case 'conquest': return `${num(p.capitals_held)}/${th.conquest_capitals ?? '?'} original capitals`;
          case 'wonder': return `wonder stage ${num(p.wonder_stage)}/${wmax}`;
          case 'influence': return `${fmt(r.influence)}/${fmt(th.influence)} influence`;
          case 'relics': return `${num(p.relics_held)}/${th.relics_needed ?? '?'} relics held, streak ${num(p.relic_streak)}/${th.relic_turns ?? '?'}`;
          case 'economic': return `${fmt(r.gold)}/${fmt(th.economic_gold)} gold`;
          default: return '';
        }
      };
      const lead = {};
      for (const c of CONDITIONS) {
        let best = null;
        for (const p of rows) {
          if (p.alive === false) continue;
          const val = num(p.victory_progress?.[c]);
          if (val >= 0.005 && (!best || val > best.val)) best = { pid: p.id, val };
        }
        lead[c] = best;
      }
      const chips = CONDITIONS.filter((c) => lead[c]).sort((a, b) => lead[b].val - lead[a].val).map((c) =>
        `<span class="race-chip${lead[c].val >= 0.75 ? ' hot' : ''}">${esc(COND_LABEL[c])}: ${this.chip(lead[c].pid)} <b>${pct(lead[c].val)}</b></span>`).join('');
      const header = `<div></div>${CONDITIONS.map((c) => `<div class="rhd" title="${esc(COND_LABEL[c])}: ${esc(need[c])}">${esc(COND_LABEL[c])}<small>${esc(need[c])}</small></div>`).join('')}`;
      const body = rows.map((p) => {
        const dead = p.alive === false;
        const col = this.color(p.id);
        return `<div class="rp${dead ? ' dead' : ''}" data-pid="${esc(p.id)}"><i class="swatch" style="background:${esc(col)}"></i><span>${esc(p.name || p.id)}</span></div>` +
          CONDITIONS.map((c) => {
            const val = clamp(num(p.victory_progress?.[c]), 0, 1);
            const isLead = !dead && lead[c]?.pid === p.id;
            return `<div class="bar${isLead ? ' lead' : ''}${val === 0 ? ' zero' : ''}" title="${esc(p.name)} — ${esc(detail(p, c))}"><i style="width:${(val * 100).toFixed(1)}%;background:${esc(col)}"></i><b>${pct(val)}</b></div>`;
          }).join('');
      }).join('');
      const scoreLeader = rows.find((p) => p.alive !== false);
      const maxT = num(th.max_turns, num(v.max_turns, 150));
      $('#race').innerHTML = `${chips ? `<div class="race-summary">${chips}</div>` : ''}
        <div class="race">${header}${body}</div>
        <div class="race-foot">Score victory at turn ${maxT} (${pct(v.turn / maxT)} elapsed)${scoreLeader ? ` — leading: ${this.chip(scoreLeader.id)} with ${fmt(scoreLeader.score)} pts` : ''}.</div>`;
      $('#race-note').textContent = 'hover a bar for details';
    },

    renderMarket(v) {
      const m = v.market;
      if (!m) { $('#market').innerHTML = '<div class="empty">No market data.</div>'; $('#market-fee').textContent = ''; return; }
      const hall = Rules.hallFee();
      $('#market-fee').textContent = `fee ${Math.round(num(m.fee, 0.05) * 100)}%${typeof hall === 'number' ? ` · ${Math.round(hall * 100)}% with a market hall` : ''}`;
      const resources = Object.keys(m.prices || m.pools || {}).length ? Object.keys(m.prices || m.pools) : TRADABLE;
      // Compact replay frames carry no market.history: rebuild it from the loaded frames' prices.
      let hist = (m.history || []).filter((h) => h && h.prices);
      if (!hist.length) {
        hist = [];
        for (let i = Math.max(0, this.idx - 49); i <= this.idx && i < this.frames.length; i++) {
          const pr = this.frames[i]?.market?.prices;
          if (pr) hist.push({ turn: this.frames[i].turn, prices: pr });
        }
      }
      $('#market').innerHTML = resources.map((r) => {
        const col = RES_COLOR[r] || '#9ca3af';
        const price = num(m.prices?.[r]);
        const series = hist.map((h) => h.prices[r]).filter((x) => typeof x === 'number');
        if (!series.length || series[series.length - 1] !== price) series.push(price);
        const prev = series.length > 1 ? series[series.length - 2] : price;
        const d = price - prev;
        const dp = prev ? (100 * d) / prev : 0;
        const pool = m.pools?.[r];
        return `<div class="mk">
          <div class="mk-top"><span class="mk-name"><i class="swatch" style="background:${col}"></i>${esc(r)}</span>
            <span class="mk-price">${price.toFixed(2)}</span></div>
          <div class="mk-delta ${d > 0.0005 ? 'up' : d < -0.0005 ? 'down' : ''}">${d > 0.0005 ? '▲' : d < -0.0005 ? '▼' : '•'} ${Math.abs(dp).toFixed(1)}% <span class="muted">gold / unit</span></div>
          ${this.sparkline(series, col)}
          ${pool ? `<div class="mk-pool" title="AMM pool reserves">pool ${fmt(pool.resource)} ${esc(r)} / ${fmt(pool.gold)} g</div>` : ''}
        </div>`;
      }).join('');
    },

    sparkline(series, color) {
      if (series.length < 2) return '<svg viewBox="0 0 100 32"></svg>';
      const min = Math.min(...series);
      const max = Math.max(...series);
      const span = max - min || 1;
      const pts = series.map((y, i) => [(i / (series.length - 1)) * 100, 29 - ((y - min) / span) * 26]);
      const line = pts.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(' ');
      const [lx, ly] = pts[pts.length - 1];
      return `<svg viewBox="0 0 100 32" preserveAspectRatio="none" aria-label="price history">
        <polygon points="0,32 ${line} 100,32" fill="${color}" opacity="0.12"/>
        <polyline points="${line}" fill="none" stroke="${color}" stroke-width="1.6" vector-effect="non-scaling-stroke" stroke-linejoin="round"/>
        <circle cx="${lx}" cy="${ly}" r="1.8" fill="${color}" vector-effect="non-scaling-stroke"/>
      </svg>`;
    },

    renderFilterOptions(v) {
      const sel = $('#feed-filter');
      // ids are p1..pN in every game: key on names too, or another game's names would stick
      const ids = JSON.stringify((v.players || []).map((p) => [p.id, p.name || '']));
      if (sel.dataset.ids === ids) return;
      sel.dataset.ids = ids;
      sel.innerHTML = '<option value="">All players</option>' +
        (v.players || []).map((p) => `<option value="${esc(p.id)}">${esc(p.name || p.id)}</option>`).join('');
      sel.value = this.filter;
    },

    involves(obj, pid) {
      return !pid || JSON.stringify(obj).includes(`"${pid}"`);
    },

    /** Events from the frames up to the displayed one, newest first (each frame carries the
     *  events of the turn before it), stopping after `limit` matches. */
    collectEvents(limit, pred) {
      const out = [];
      let more = false;
      for (let i = Math.min(this.idx, this.frames.length - 1); i >= 0; i--) {
        const f = this.frames[i];
        const evs = f.events || [];
        for (let k = evs.length - 1; k >= 0; k--) {
          const e = evs[k];
          const ev = e.turn == null ? { ...e, turn: f.turn - 1 } : e;
          if (pred && !pred(ev)) continue;
          if (out.length >= limit) { more = true; break; }
          out.push(ev);
        }
        if (more) break;
      }
      return { events: out, more };
    },

    renderFeed() {
      const el = $('#events');
      if (!el) return;
      const LIMIT = this.feedLimit || 150;
      const { events, more } = this.collectEvents(LIMIT, (e) => this.involves(e, this.filter));
      if (!events.length) {
        el.innerHTML = `<div class="empty">${this.history?.loading ? 'Loading history…' : 'No events yet.'}</div>`;
        return;
      }
      events.sort((a, b) => num(b.turn) - num(a.turn));
      let html = '';
      let lastTurn = null;
      for (const e of events) {
        if (e.turn !== lastTurn) {
          lastTurn = e.turn;
          html += `<div class="feed-turn">Turn ${esc(e.turn)}</div>`;
        }
        let d;
        try { d = this.describe(e); } catch (_) { d = { icon: 'dot', html: esc(human(e.type)) }; }
        html += `<div class="ev ${esc(e.type)}${d.major ? ' major' : ''}${e.type === 'order_failed' ? ' failed' : ''}"><span class="ico" style="color:${d.color || 'var(--muted)'}">${icon(d.icon, d.color)}</span><span class="txt">${d.html}</span></div>`;
      }
      if (more) html += '<div class="empty"><button class="btn ghost small" id="feed-more">Show older events</button></div>';
      const keepTop = el.scrollTop < 8;
      el.innerHTML = html;
      if (keepTop) el.scrollTop = 0;
      const btn = $('#feed-more');
      if (btn) btn.onclick = () => { this.feedLimit = LIMIT + 300; this.renderFeed(); };
    },

    /** Human-readable text for one event. Field names follow the engine (docs/DESIGN.md §10) with
     *  tolerant fallbacks; unknown event types render as `key=value` so nothing is ever lost. */
    describe(e) {
      const P = (pid) => this.chip(pid);
      const at = (x, y) => (typeof x === 'number' && typeof y === 'number'
        ? ` <span class="at" data-x="${x}" data-y="${y}" title="show on map">(${x},${y})</span>` : '');
      const xy = (o) => (Array.isArray(o) ? at(o[0], o[1]) : '');
      const who = e.player ?? e.by ?? e.owner ?? e.pid;
      const bag = (b) => esc(typeof b === 'object' && b ? bagText(b) : (b ?? ''));
      const loc = at(e.x, e.y) || xy(e.at) || (Array.isArray(e.city) ? xy(e.city) : '');
      const cityName = typeof e.city === 'string' ? e.city : (e.name || e.city_name);
      const wmax = Rules.wonderMax(this.view?.victory?.thresholds);
      switch (e.type) {
        case 'battle': {
          const sides = Array.isArray(e.sides) ? e.sides : Object.keys(e.losses || {});
          const losses = Object.entries(e.losses || {}).map(([pid, l]) => {
            const n = typeof l === 'number' ? l : Object.values(l || {}).reduce((s, k) => s + num(k), 0);
            return n ? `${esc(this.pname(pid))} −${n}` : '';
          }).filter(Boolean).join(', ');
          const powers = e.powers && typeof e.powers === 'object'
            ? Object.entries(e.powers).map(([pid, pw]) => `${esc(this.pname(pid))} ${fmt(num(pw))}`).join(' vs ') : '';
          const where = e.clash && Array.isArray(e.to) ? `${loc} ↔${xy(e.to)} <span class="muted">(border clash)</span>` : loc;
          return {
            icon: 'battle', color: '#f87171', major: true,
            html: `Battle${where}: ${sides.map(P).join(' vs ')}${e.winner ? ` — ${P(e.winner)} wins` : ' — mutual destruction'}${losses || powers ? ` <span class="muted">(${[powers && 'power ' + powers, losses && 'losses ' + losses].filter(Boolean).join('; ')})</span>` : ''}`,
          };
        }
        case 'city_captured': {
          const by = e.to ?? e.player ?? e.by ?? e.new_owner;
          const from = e.from ?? e.previous_owner ?? e.old_owner;
          const extra = [e.plunder && bagText(e.plunder) ? 'plunder ' + bag(e.plunder) : '',
            num(e.tiles) ? `${e.tiles} tiles` : '', num(e.wonder_destroyed) ? `wonder stage ${e.wonder_destroyed} destroyed` : '']
            .filter(Boolean).join(' · ');
          return {
            icon: 'flag', color: '#fb923c', major: true,
            html: `${P(by)} captured ${e.capital ? 'the capital ' : ''}<b>${esc(cityName || 'a city')}</b>${from ? ` from ${P(from)}` : ''}${loc}${extra ? ` <span class="muted">${extra}</span>` : ''}`,
          };
        }
        case 'city_founded':
          return { icon: 'city', color: '#60a5fa', html: `${P(who)} founded <b>${esc(cityName || 'a city')}</b>${loc}` };
        case 'tile_captured': {
          const by = e.to ?? e.player ?? e.by;
          const from = e.from ?? e.previous_owner;
          return { icon: e.relic ? 'relic' : 'flag', color: e.relic ? '#67e8f9' : '#fdba74', major: !!e.relic, html: `${P(by)} captured ${e.relic ? 'a <b>relic</b>' : 'a tile'}${loc}${from ? ` from ${P(from)}` : ''}` };
        }
        case 'claim':
          return { icon: e.relic ? 'relic' : 'claim', color: e.relic ? '#67e8f9' : '#94a3b8', major: !!e.relic, html: `${P(who)} claimed ${e.relic ? 'a <b>relic</b>' : 'a tile'}${loc}${e.cost != null ? ` <span class="muted">(${esc(e.cost)} inf)</span>` : ''}` };
        case 'build':
          return { icon: 'build', color: '#cbd5e1', html: `${P(who)} built ${e.building === 'walls' && e.level ? `walls (level ${esc(e.level)})` : esc(human(e.building)).toLowerCase()}${loc}` };
        case 'recruit':
          return { icon: 'unit', color: '#cbd5e1', html: `${P(who)} recruited ${esc(e.count ?? '')} ${esc(e.unit || 'units')}${loc}` };
        case 'disband':
          return { icon: 'unit', color: '#94a3b8', html: `${P(who)} disbanded ${bag(e.units) || 'units'}${loc}` };
        case 'wonder_stage':
          return { icon: 'star', color: '#f3c969', major: true, html: `${P(who)} completed <b>wonder stage ${esc(e.stage)}/${wmax}</b>${cityName ? ` in ${esc(cityName)}` : ''}${loc}` };
        case 'starvation': {
          const lost = e.lost ?? e.units_lost;
          return { icon: 'starve', color: '#fbbf24', html: `${P(who)}'s troops starved${e.deficit ? ` <span class="muted">(short ${esc(e.deficit)} food)</span>` : ''}${lost && (typeof lost !== 'object' || bagText(lost)) ? `: lost ${bag(lost)}` : ''}` };
        }
        case 'eliminated':
          return { icon: 'skull', color: '#f87171', major: true, html: `${P(who)} was <b>eliminated</b>${e.by && e.by !== who ? ` by ${P(e.by)}` : ''}` };
        case 'treaty_proposed':
          return { icon: 'treaty', color: '#86efac', html: `${P(e.from ?? who)} proposed peace to ${P(e.to)}${e.turns ? ` <span class="muted">(${esc(e.turns)} turns)</span>` : ''}` };
        case 'treaty_signed': {
          const [a, b] = Array.isArray(e.players) ? e.players : [e.a ?? e.from, e.b ?? e.to];
          return { icon: 'treaty', color: '#4ade80', html: `${P(a)} and ${P(b)} signed a peace treaty${e.until_turn != null ? ` <span class="muted">until turn ${esc(e.until_turn)}</span>` : ''}` };
        }
        case 'treaty_expired': {
          const [a, b] = Array.isArray(e.players) ? e.players : [e.a ?? e.from, e.b ?? e.to];
          return { icon: 'treaty', color: '#94a3b8', html: `The treaty between ${P(a)} and ${P(b)} expired` };
        }
        case 'treaty_broken': {
          const breaker = e.by ?? e.player ?? e.breaker ?? e.a;
          const other = e.with ?? e.other ?? e.b ?? e.victim;
          return { icon: 'broken', color: '#f87171', major: true, html: `${P(breaker)} <span class="betray">broke</span> their treaty with ${P(other)} <span class="muted">(betrayal)</span>` };
        }
        case 'trade_offered':
          return { icon: 'trade', color: '#bef264', html: `${P(e.from)} offered ${P(e.to)} ${bag(e.give) || '?'} for ${bag(e.want) || '?'}${e.id ? ` <span class="muted">(${esc(e.id)})</span>` : ''}` };
        case 'trade_executed':
          return { icon: 'trade', color: '#a3e635', html: `${P(e.from)} traded ${bag(e.give) || '?'} to ${P(e.to)} for ${bag(e.want) || '?'}` };
        case 'market': {
          const price = e.price ?? e.avg_price;
          const side = e.side === 'buy' ? 'bought' : e.side === 'sell' ? 'sold' : (e.side || 'traded');
          const gold = typeof e.gold === 'number' ? ` <span class="muted">(${e.side === 'buy' ? 'paid' : 'got'} ${fmt(Math.abs(e.gold))} gold)</span>` : '';
          return { icon: 'market', color: '#f3c969', html: `${who ? P(who) + ' ' : 'Market: '}${esc(side)} ${esc(e.qty ?? '')} <b>${esc(e.resource || '')}</b>${typeof price === 'number' ? ` @ ${price.toFixed(2)}` : ''}${gold}` };
        }
        case 'order_failed': {
          const kind = e.order_type ?? e.order?.type ?? 'order';
          return { icon: 'fail', color: '#fbbf24', html: `${P(who)}: ${esc(kind)} order${e.index != null && e.index >= 0 ? ` #${esc(e.index)}` : ''} failed <span class="muted">— ${esc(e.reason || e.error || 'unknown reason')}</span>` };
        }
        case 'victory':
          return { icon: 'trophy', color: '#f3c969', major: true, html: `${P(e.winner ?? who)} wins by <b>${esc(COND_LABEL[e.condition] || human(e.condition))}</b> victory!` };
        default: {
          const rest = Object.entries(e).filter(([k]) => !['type', 'turn', 'x', 'y'].includes(k))
            .map(([k, val]) => `${esc(k)}=${esc(typeof val === 'object' ? JSON.stringify(val) : val)}`).join(' ');
          return { icon: 'dot', html: `${esc(human(e.type || 'event'))}${loc} <span class="muted">${rest}</span>` };
        }
      }
    },

    collectMessages() {
      const seen = new Set();
      const out = [];
      for (let i = 0; i <= this.idx && i < this.frames.length; i++) {
        for (const m of this.frames[i].messages || []) {
          const k = `${m.turn}|${m.from}|${m.to}|${m.text}`;
          if (seen.has(k)) continue;
          seen.add(k);
          out.push(m);
        }
      }
      return out.sort((a, b) => num(b.turn) - num(a.turn));
    },

    renderDiplomacy() {
      const v = this.view;
      if (!v) return;
      const f = this.filter;
      const inv = (o) => this.involves(o, f);
      const treaties = (v.treaties || []).filter(inv);
      const proposals = (v.treaty_proposals || []).filter(inv);
      const offers = (v.trade_offers || []).filter(inv);
      const betrayers = (v.players || []).filter((p) => num(p.betrayals) > 0 && (!f || p.id === f));
      const msgs = this.collectMessages().filter((m) => !f || m.from === f || m.to === f);
      const bag = (b) => Object.entries(b || {}).map(([k, n]) => `${n} ${k}`).join(', ') || '—';
      const hidden = v.status === 'running'
        ? '<div class="dsec small muted">Private messages, trade offers and treaty proposals stay hidden while the game runs; the replay reveals them once it is over.</div>' : '';
      $('#diplo').innerHTML = `${hidden}
        <div class="dsec"><h4>Active treaties (${treaties.length})</h4>
          ${treaties.length ? treaties.map((t) => `<div class="treaty">${icon('treaty', '#4ade80')} ${this.chip(t.a)} <span class="muted">⇄</span> ${this.chip(t.b)}
            <span class="left">until turn ${esc(t.until_turn)}${t.until_turn != null ? ` · ${Math.max(0, t.until_turn - v.turn)} left` : ''}</span></div>`).join('')
            : '<div class="empty">No active treaties.</div>'}
        </div>
        ${proposals.length ? `<div class="dsec"><h4>Pending proposals</h4>${proposals.map((p) =>
          `<div class="treaty">${this.chip(p.from)} <span class="muted">→</span> ${this.chip(p.to)} <span class="left">${esc(p.turns)} turns · proposed T${esc(p.turn)}</span></div>`).join('')}</div>` : ''}
        ${offers.length ? `<div class="dsec"><h4>Open trade offers</h4>${offers.map((o) =>
          `<div class="treaty">${icon('trade', '#a3e635')} ${this.chip(o.from)} offers ${this.chip(o.to)} <b>${esc(bag(o.give))}</b> for <b>${esc(bag(o.want))}</b>
           <span class="left">expires T${esc(o.expires_turn)}</span></div>`).join('')}</div>` : ''}
        <div class="dsec"><h4>Betrayals</h4>${betrayers.length ? betrayers.map((p) =>
          `<div class="treaty">${icon('broken', '#f87171')} ${this.chip(p.id)} <span class="betray">broke ${p.betrayals} treat${p.betrayals === 1 ? 'y' : 'ies'}</span></div>`).join('')
          : '<div class="empty">Nobody has broken a treaty.</div>'}</div>
        <div class="dsec"><h4>Messages (${msgs.length})</h4><div class="msgs">
          ${msgs.length ? msgs.map((m) => {
            const pub = m.to === 'all' || m.to == null;
            return `<div class="msg ${pub ? 'public' : 'private'}"><div class="mh">${this.chip(m.from)} <span>→</span> ${pub ? '<span>everyone</span>' : `${icon('lock')} ${this.chip(m.to)}`}<span class="t">T${esc(m.turn)}</span></div>
              <div class="body">${esc(m.text)}</div></div>`;
          }).join('') : '<div class="empty">No messages.</div>'}
        </div></div>`;
    },

    renderLegend() {
      const el = $('#legend');
      if (el.dataset.done) return;
      el.dataset.done = '1';
      el.innerHTML = Object.values(TERRAIN).map((t) => `<span><i style="background:${t.color}"></i>${t.name}</span>`).join('') +
        `<span>${icon('star', '#fff')} capital</span><span>${icon('relic', '#67e8f9')} relic</span>
         <span><i style="background:transparent;border:2px solid #d7dbe3"></i>walls</span>
         <span style="color:var(--accent)">◠ wonder stages</span>
         <span>${icon('battle', '#f87171')} battle last turn</span><span>▭ bar: deposit left</span>`;
    },
  };

  // ============================================================ 8. boot
  async function boot() {
    if (App.mock) setConn('mock', 'mock data');
    else {
      // Probe once so every later call knows whether to use the mock backend.
      App.booting = true;
      try { await api.get('api/games'); } catch (_) { /* handled per view */ }
      App.booting = false;
    }
    window.addEventListener('hashchange', route);
    route();
  }

  // Expose for debugging and automated checks.
  window.AgentCivGUI = { App, GameView, Lobby, Mock };
  boot();
})();
