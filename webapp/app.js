"use strict";

const tg = window.Telegram ? window.Telegram.WebApp : null;
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const state = { me: null, config: null, screen: "home", busy: false };
const GAME_NAMES = { slots: "🎰 Слоты", dice: "🎲 Кости", roulette: "🎡 Рулетка", mines: "💣 Мины",
  crash: "🚀 Краш", case: "🎁 Кейс", pvp: "⚔️ PvP" };
const TITLES = { home: "🎰 Казино", deposit: "⭐ Пополнение", slots: "🎰 Слоты", crash: "🚀 Краш",
  mines: "💣 Мины", dice: "🎲 Кости", roulette: "🎡 Рулетка", cases: "🎁 Кейсы", case: "🎁 Кейс", pvp: "⚔️ PvP-рулетка" };
const PVP_COLORS = ["#ffc83d", "#3ddc84", "#4da3ff", "#ff4d6a", "#b67dff", "#ff8a00", "#2ee6d6", "#ff6fcf"];

// ---------- утилиты ----------

function haptic(kind) {
  try {
    if (!tg || !tg.HapticFeedback) return;
    if (kind === "win") tg.HapticFeedback.notificationOccurred("success");
    else if (kind === "lose") tg.HapticFeedback.notificationOccurred("error");
    else tg.HapticFeedback.impactOccurred("light");
  } catch (e) { /* не критично */ }
}

let toastTimer = null;
function toast(text, isError) {
  const el = $("#toast");
  el.textContent = text;
  el.classList.toggle("err", !!isError);
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 2600);
}

function store(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, value);
  } catch (e) { return null; }
}

async function api(path, body) {
  const opts = { method: body === undefined ? "GET" : "POST",
    headers: { Authorization: "tma " + (tg ? tg.initData : "") } };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch (e) {
    throw new Error("Нет связи с сервером");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || "Ошибка " + res.status);
  if (typeof data.balance === "number") setBalance(data.balance);
  return data;
}

function setBalance(value) {
  if (state.me) state.me.balance = value;
  $("#balance").textContent = value;
}

async function guard(fn) {
  if (state.busy) return;
  state.busy = true;
  try {
    await fn();
  } catch (e) {
    toast(e.message, true);
    haptic("lose");
  } finally {
    state.busy = false;
  }
}

// ---------- навигация ----------

function go(screen) {
  state.screen = screen;
  $$(".screen").forEach((s) => s.classList.toggle("active", s.id === screen));
  $("#title").textContent = TITLES[screen] || TITLES.home;
  const isHome = screen === "home";
  $("#back").classList.toggle("hidden", isHome || !!(tg && tg.BackButton));
  if (tg && tg.BackButton) isHome ? tg.BackButton.hide() : tg.BackButton.show();
  window.scrollTo(0, 0);
  const enter = { home: loadMe, crash: crashEnter, mines: minesEnter, pvp: pvpEnter, cases: renderCases };
  if (screen !== "pvp") pvpLeave();
  if (enter[screen]) enter[screen]();
}

function goBack() {
  go(state.screen === "case" ? "cases" : "home");
}

// ---------- ставка ----------

function betBoxes() {
  $$(".betbox").forEach((box) => {
    const key = "bet:" + box.dataset.bet;
    const input = document.createElement("input");
    input.type = "number";
    input.inputMode = "numeric";
    input.value = store(key) || "10";
    input.addEventListener("change", () => store(key, input.value));
    const mk = (label, fn) => {
      const b = document.createElement("button");
      b.textContent = label;
      b.addEventListener("click", () => { input.value = fn(parseInt(input.value, 10) || 0); store(key, input.value); haptic(); });
      return b;
    };
    const cfg = () => state.config || { min_bet: 1, max_bet: 10000 };
    const clamp = (v) => Math.max(cfg().min_bet, Math.min(cfg().max_bet, v));
    box.append(mk("½", (v) => clamp(Math.floor(v / 2))), input, mk("×2", (v) => clamp(v * 2)),
      mk("MAX", () => clamp(state.me ? state.me.balance : cfg().max_bet)));
  });
}

function getBet(game) {
  const input = $(`.betbox[data-bet="${game}"] input`);
  const v = parseInt(input.value, 10);
  if (!Number.isInteger(v) || v <= 0) throw new Error("Введите ставку");
  return v;
}

// ---------- главная ----------

async function loadMe() {
  try {
    const me = await api("/api/me");
    state.me = me;
    state.config = me.config;
    setBalance(me.balance);
    $("#hello").textContent = `Привет, ${me.user.name}!`;
    renderHistory(me.history);
  } catch (e) {
    toast(e.message, true);
  }
}

function renderHistory(items) {
  const box = $("#history");
  box.innerHTML = "";
  if (!items || !items.length) {
    box.textContent = "Пока пусто — самое время сыграть!";
    return;
  }
  items.forEach((h) => {
    const row = document.createElement("div");
    row.className = "h";
    const name = document.createElement("span");
    name.textContent = `${GAME_NAMES[h.game] || h.game} · ставка ${h.bet}`;
    const res = document.createElement("b");
    const diff = h.win - h.bet;
    res.className = diff >= 0 ? "win" : "lose";
    res.textContent = (diff >= 0 ? "+" : "") + diff + " ⭐";
    row.append(name, res);
    box.append(row);
  });
}

async function activateCheck() {
  await guard(async () => {
    const code = $("#check-code").value.trim();
    if (!code) throw new Error("Введите код чека");
    const r = await api("/api/check", { code });
    $("#check-code").value = "";
    toast(`🎁 +${r.amount} ⭐`);
    haptic("win");
  });
}

// ---------- пополнение ----------

function renderPresets() {
  const box = $("#presets");
  box.innerHTML = "";
  (state.config ? state.config.deposit_presets : [50, 100, 250, 500, 1000, 2500]).forEach((a) => {
    const b = document.createElement("button");
    b.textContent = a + " ⭐";
    b.addEventListener("click", () => deposit(a));
    box.append(b);
  });
}

async function deposit(amount) {
  await guard(async () => {
    if (!Number.isInteger(amount) || amount <= 0) throw new Error("Введите сумму");
    const { link } = await api("/api/deposit", { amount });
    if (!tg || !tg.openInvoice) throw new Error("Откройте приложение в Telegram");
    tg.openInvoice(link, async (status) => {
      if (status === "paid") {
        toast("Оплата прошла, зачисляем…");
        for (let i = 0; i < 6; i++) {
          await sleep(1200);
          const before = state.me ? state.me.balance : 0;
          await loadMe();
          if (state.me && state.me.balance !== before) break;
        }
        haptic("win");
        go("home");
      } else if (status === "failed") {
        toast("Оплата не прошла", true);
      }
    });
  });
}

// ---------- слоты ----------

const REEL_H = 92;

function renderPaytable() {
  const s = state.config && state.config.slots;
  if (!s) return;
  const box = $("#paytable");
  box.innerHTML = "";
  Object.entries(s.triple).forEach(([sym, m]) => {
    const d = document.createElement("div");
    d.textContent = `${sym}${sym}${sym} — ×${m}`;
    box.append(d);
  });
  [["🍒🍒 — ×" + s.two_cherries], ["🍒 — ×" + s.one_cherry]].forEach(([t]) => {
    const d = document.createElement("div");
    d.textContent = t;
    box.append(d);
  });
}

function reelStrip(finalSymbol, count) {
  const syms = (state.config && state.config.slots.symbols) || ["🍒", "🍋", "🍇", "🔔", "⭐", "7️⃣"];
  const items = [];
  for (let i = 0; i < count; i++) items.push(syms[Math.floor(Math.random() * syms.length)]);
  items.push(finalSymbol);
  return items;
}

async function slotsSpin() {
  await guard(async () => {
    const bet = getBet("slots");
    $("#slots-spin").disabled = true;
    $("#slots-result").className = "result";
    $("#slots-result").textContent = "…";
    try {
      const r = await api("/api/slots", { bet });
      haptic();
      const reels = $$("#slots .strip");
      await Promise.all(reels.map((strip, i) => new Promise((resolve) => {
        const items = reelStrip(r.reels[i], 18 + i * 6);
        strip.innerHTML = items.map((s) => `<div>${s}</div>`).join("");
        strip.style.transition = "none";
        strip.style.transform = "translateY(0)";
        void strip.offsetHeight;
        const ms = 900 + i * 350;
        strip.style.transition = `transform ${ms}ms cubic-bezier(.15,.85,.25,1)`;
        strip.style.transform = `translateY(-${(items.length - 1) * REEL_H}px)`;
        setTimeout(resolve, ms);
      })));
      const res = $("#slots-result");
      if (r.win > 0) {
        res.className = "result win";
        res.textContent = `+${r.win} ⭐ (×${r.multiplier})`;
        haptic("win");
      } else {
        res.className = "result lose";
        res.textContent = "Мимо 😔";
      }
    } finally {
      $("#slots-spin").disabled = false;
    }
  });
}

// ---------- ролик (рулетка, кейсы, PvP) ----------

async function roll(rollerSel, items, targetIndex, duration) {
  const roller = $(rollerSel);
  const track = roller.querySelector(".roller-track");
  track.innerHTML = "";
  items.forEach((it) => {
    const d = document.createElement("div");
    d.className = "it " + (it.cls || "");
    if (it.bg) d.style.background = it.bg;
    const big = document.createElement("span");
    big.textContent = it.text;
    d.append(big);
    if (it.sub) {
      const s = document.createElement("small");
      s.textContent = it.sub;
      d.append(s);
    }
    track.append(d);
  });
  const itemW = 92;
  const jitter = (Math.random() - 0.5) * 60;
  const offset = roller.clientWidth / 2 - (targetIndex * itemW + itemW / 2) + jitter;
  track.style.transition = "none";
  track.style.transform = "translateX(0)";
  void track.offsetWidth;
  track.style.transition = `transform ${duration}ms cubic-bezier(.08,.75,.12,1)`;
  track.style.transform = `translateX(${offset}px)`;
  await sleep(duration + 100);
}

// ---------- рулетка ----------

let rouletteType = "red";

function rouletteColor(n) {
  if (n === 0) return "green";
  const red = (state.config && state.config.red) || [];
  return red.includes(n) ? "red" : "black";
}

async function rouletteSpin() {
  await guard(async () => {
    const bet = getBet("roulette");
    const body = { bet, type: rouletteType };
    if (rouletteType === "number") body.value = parseInt($("#rnum").value, 10);
    $("#roulette-btn").disabled = true;
    try {
      const r = await api("/api/roulette", body);
      haptic();
      const items = [];
      for (let i = 0; i < 60; i++) {
        const n = i === 50 ? r.number : Math.floor(Math.random() * 37);
        items.push({ text: String(n), cls: rouletteColor(n) });
      }
      $("#roulette-result").className = "result";
      $("#roulette-result").textContent = "Крутим…";
      await roll("#roulette-roller", items, 50, 3500);
      const res = $("#roulette-result");
      if (r.win > 0) {
        res.className = "result win";
        res.textContent = `Выпало ${r.number} — +${r.win} ⭐`;
        haptic("win");
      } else {
        res.className = "result lose";
        res.textContent = `Выпало ${r.number}`;
        haptic("lose");
      }
    } finally {
      $("#roulette-btn").disabled = false;
    }
  });
}

// ---------- кости ----------

function diceUpdate() {
  const chance = parseInt($("#dice-chance").value, 10);
  const edge = (state.config && state.config.dice.edge) || 0.03;
  $("#dice-chance-label").textContent = chance + "%";
  $("#dice-mult").textContent = ((1 - edge) * 100 / chance).toFixed(2);
  $("#dice-under").textContent = chance;
  $("#dice-fill").style.width = chance + "%";
}

async function diceRoll() {
  await guard(async () => {
    const bet = getBet("dice");
    const chance = parseInt($("#dice-chance").value, 10);
    $("#dice-btn").disabled = true;
    try {
      const r = await api("/api/dice", { bet, chance });
      haptic();
      const el = $("#dice-roll");
      el.className = "dice-roll";
      const start = performance.now();
      await new Promise((resolve) => {
        const tick = (now) => {
          const t = Math.min(1, (now - start) / 700);
          el.textContent = t < 1 ? (Math.random() * 100).toFixed(2) : r.roll.toFixed(2);
          t < 1 ? requestAnimationFrame(tick) : resolve();
        };
        requestAnimationFrame(tick);
      });
      $("#dice-marker").style.left = r.roll + "%";
      el.className = "dice-roll " + (r.won ? "win" : "lose");
      if (r.won) { toast(`+${r.win} ⭐`); haptic("win"); } else { haptic("lose"); }
    } finally {
      $("#dice-btn").disabled = false;
    }
  });
}

// ---------- мины ----------

let minesGame = null;

function minesRender(game, reveal) {
  const grid = $("#mines-grid");
  grid.innerHTML = "";
  const opened = game ? game.opened : [];
  const layout = reveal && reveal.layout ? reveal.layout : [];
  for (let i = 0; i < 25; i++) {
    const b = document.createElement("button");
    if (opened.includes(i)) { b.className = "safe"; b.textContent = "💎"; }
    if (layout.includes(i)) {
      b.textContent = "💣";
      b.className = reveal.boom === i ? "boom" : "dim";
    } else if (reveal && !opened.includes(i)) {
      b.className = "dim";
      b.textContent = "💎";
    }
    b.addEventListener("click", () => minesOpen(i));
    grid.append(b);
  }
  const btn = $("#mines-btn");
  const info = $("#mines-info");
  if (game && game.active) {
    btn.textContent = game.opened.length ? `Забрать ${game.cashout} ⭐` : "Откройте клетку";
    btn.classList.toggle("alt", game.opened.length > 0);
    info.textContent = `Множитель ×${game.multiplier}` + (game.next_multiplier ? ` · следующий ×${game.next_multiplier}` : "");
  } else {
    btn.textContent = "Играть";
    btn.classList.remove("alt");
  }
}

async function minesEnter() {
  try {
    const r = await api("/api/mines");
    minesGame = r.game;
    minesRender(minesGame);
  } catch (e) { toast(e.message, true); }
}

async function minesAction() {
  await guard(async () => {
    if (minesGame && minesGame.active) {
      if (!minesGame.opened.length) return;
      const r = await api("/api/mines/cashout", {});
      minesGame = null;
      minesRender(r, r);
      $("#mines-info").textContent = `Выигрыш +${r.win} ⭐ (×${r.multiplier})`;
      haptic("win");
      return;
    }
    const bet = getBet("mines");
    const mines = parseInt($("#mines-count").value, 10);
    minesGame = await api("/api/mines/start", { bet, mines });
    minesRender(minesGame);
    haptic();
  });
}

async function minesOpen(cell) {
  if (!minesGame || !minesGame.active) return;
  await guard(async () => {
    const r = await api("/api/mines/open", { cell });
    if (r.boom !== undefined) {
      minesGame = null;
      minesRender(r, r);
      $("#mines-info").textContent = "💥 Мина! Ставка сгорела";
      haptic("lose");
    } else if (r.win !== undefined) {
      minesGame = null;
      minesRender(r, r);
      $("#mines-info").textContent = `Все клетки открыты! +${r.win} ⭐`;
      haptic("win");
    } else {
      minesGame = r;
      minesRender(r);
      haptic();
    }
  });
}

// ---------- краш ----------

const crash = { running: false, start: 0, growth: 0.07, raf: 0, poll: 0, points: [] };

function crashCanvas() {
  const c = $("#crash-canvas");
  const dpr = window.devicePixelRatio || 1;
  if (c.width !== c.clientWidth * dpr) { c.width = c.clientWidth * dpr; c.height = c.clientHeight * dpr; }
  return c;
}

function crashDraw(mult, color) {
  const c = crashCanvas();
  const ctx = c.getContext("2d");
  const w = c.width, h = c.height;
  ctx.clearRect(0, 0, w, h);
  const t = Math.log(Math.max(mult, 1)) / crash.growth;
  const tMax = Math.max(8, t * 1.15);
  const mMax = Math.max(2, mult * 1.15);
  ctx.beginPath();
  for (let i = 0; i <= 60; i++) {
    const ti = (t * i) / 60;
    const mi = Math.exp(crash.growth * ti);
    const x = (ti / tMax) * w;
    const y = h - ((mi - 1) / (mMax - 1)) * (h * 0.85);
    i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  }
  ctx.strokeStyle = color || "#ffc83d";
  ctx.lineWidth = 4 * (window.devicePixelRatio || 1);
  ctx.stroke();
}

function crashShow(mult, cls) {
  const el = $("#crash-mult");
  el.textContent = mult.toFixed(2) + "×";
  el.className = "crash-mult " + (cls || "");
}

function crashStop() {
  crash.running = false;
  cancelAnimationFrame(crash.raf);
  clearInterval(crash.poll);
  $("#crash-btn").textContent = "Старт";
  $("#crash-btn").classList.remove("alt");
}

function crashFinish(r) {
  crashStop();
  if (r.status === "cashed") {
    crashShow(r.multiplier, "win");
    crashDraw(r.multiplier, "#3ddc84");
    toast(`+${r.win} ⭐ (×${r.multiplier})`);
    haptic("win");
  } else if (r.status === "crashed") {
    crashShow(r.point, "lose");
    crashDraw(r.point, "#ff4d6a");
    haptic("lose");
  } else {
    loadMe();
  }
}

function crashRun(r) {
  crash.running = true;
  crash.growth = r.growth || crash.growth;
  crash.start = performance.now() - (r.elapsed || 0) * 1000;
  crash.bet = r.bet;
  $("#crash-btn").classList.add("alt");
  const frame = () => {
    if (!crash.running) return;
    const t = (performance.now() - crash.start) / 1000;
    const m = Math.floor(Math.exp(crash.growth * t) * 100) / 100;
    crashShow(m);
    crashDraw(m);
    $("#crash-btn").textContent = `Забрать ${Math.floor(crash.bet * m)} ⭐`;
    crash.raf = requestAnimationFrame(frame);
  };
  frame();
  clearInterval(crash.poll);
  crash.poll = setInterval(async () => {
    if (!crash.running) return;
    try {
      const s = await api("/api/crash");
      if (s.status !== "running") crashFinish(s);
    } catch (e) { /* повторим */ }
  }, 400);
}

async function crashEnter() {
  crashStop();
  crashShow(1, "");
  crashDraw(1);
  try {
    const s = await api("/api/crash");
    if (s.status === "running") crashRun(s);
  } catch (e) { toast(e.message, true); }
}

async function crashAction() {
  if (crash.running) {
    try {
      const r = await api("/api/crash/cashout", {});
      if (r.status !== "running") crashFinish(r);
    } catch (e) { toast(e.message, true); }
    return;
  }
  await guard(async () => {
    const bet = getBet("crash");
    const autoRaw = $("#crash-auto").value.trim().replace(",", ".");
    const body = { bet };
    if (autoRaw) body.auto = parseFloat(autoRaw);
    const r = await api("/api/crash/start", body);
    haptic();
    crashRun(r);
  });
}

// ---------- кейсы ----------

let currentCase = null;

function renderCases() {
  const box = $("#cases-list");
  box.innerHTML = "";
  (state.config ? state.config.cases : []).forEach((c) => {
    const b = document.createElement("button");
    b.className = "case-card";
    b.innerHTML = `<div class="e"></div><div class="n"></div><div class="p"></div>`;
    b.querySelector(".e").textContent = c.emoji;
    b.querySelector(".n").textContent = c.name;
    b.querySelector(".p").textContent = c.price + " ⭐";
    b.addEventListener("click", () => openCaseScreen(c));
    box.append(b);
  });
}

function prizeItem(amount, price) {
  const ratio = amount / price;
  const bg = ratio >= 5 ? "#b8860b" : ratio >= 2 ? "#7b3fe4" : ratio >= 1 ? "#2f6fd6" : "#3a3550";
  return { text: amount + "⭐", bg };
}

function openCaseScreen(c) {
  currentCase = c;
  go("case");
  $("#title").textContent = `${c.emoji} ${c.name} кейс`;
  $("#case-btn").textContent = `Открыть за ${c.price} ⭐`;
  $("#case-result").textContent = "";
  const box = $("#case-prizes");
  box.innerHTML = "";
  c.prizes.forEach((p) => {
    const d = document.createElement("div");
    d.textContent = p.amount + " ⭐";
    const s = document.createElement("small");
    s.textContent = p.chance + "%";
    d.append(s);
    box.append(d);
  });
  const items = [];
  for (let i = 0; i < 12; i++) items.push(prizeItem(pickPrize(c), c.price));
  roll("#case-roller", items, 5, 0);
}

function pickPrize(c) {
  let r = Math.random() * 100;
  for (const p of c.prizes) { r -= p.chance; if (r <= 0) return p.amount; }
  return c.prizes[0].amount;
}

async function openCase() {
  if (!currentCase) return;
  await guard(async () => {
    $("#case-btn").disabled = true;
    try {
      const r = await api("/api/case", { case: currentCase.id });
      haptic();
      const items = [];
      for (let i = 0; i < 60; i++) items.push(prizeItem(i === 50 ? r.prize : pickPrize(currentCase), currentCase.price));
      $("#case-result").className = "result";
      $("#case-result").textContent = "…";
      await roll("#case-roller", items, 50, 4500);
      const res = $("#case-result");
      const good = r.prize >= currentCase.price;
      res.className = "result " + (good ? "win" : "lose");
      res.textContent = `Выпало ${r.prize} ⭐`;
      haptic(good ? "win" : "lose");
    } finally {
      $("#case-btn").disabled = false;
    }
  });
}

// ---------- PvP ----------

const pvp = { timer: 0, lastSeen: null, animating: false, colors: {} };

function pvpColor(id) {
  if (!pvp.colors[id]) pvp.colors[id] = PVP_COLORS[Object.keys(pvp.colors).length % PVP_COLORS.length];
  return pvp.colors[id];
}

function pvpRenderPlayers(round) {
  const box = $("#pvp-players");
  box.innerHTML = "";
  const players = round ? round.players : [];
  if (!players.length) {
    box.innerHTML = '<div class="note">Пока никого. Сделайте первую ставку!</div>';
    return;
  }
  players.forEach((p) => {
    const row = document.createElement("div");
    row.className = "pl" + (state.me && p.id === state.me.user.id ? " me" : "");
    const dot = document.createElement("span");
    dot.className = "dot";
    dot.style.background = pvpColor(p.id);
    const nm = document.createElement("span");
    nm.className = "nm";
    nm.textContent = p.name;
    const am = document.createElement("span");
    am.className = "am";
    am.textContent = p.amount + " ⭐";
    const ch = document.createElement("span");
    ch.className = "ch";
    ch.textContent = p.chance + "%";
    row.append(dot, nm, am, ch);
    box.append(row);
  });
}

async function pvpAnimate(last) {
  pvp.animating = true;
  try {
    const items = [];
    const pick = () => {
      let r = Math.random() * 100;
      for (const p of last.players) { r -= p.chance; if (r <= 0) return p; }
      return last.players[0];
    };
    for (let i = 0; i < 60; i++) {
      const p = i === 50 ? last.winner : pick();
      items.push({ text: p.name.slice(0, 2).toUpperCase(), sub: p.name, bg: pvpColor(p.id) });
    }
    $("#pvp-result").className = "result";
    $("#pvp-result").textContent = "Крутим…";
    await roll("#pvp-roller", items, 50, 5000);
    const mine = state.me && last.winner.id === state.me.user.id;
    $("#pvp-result").className = "result " + (mine ? "win" : "");
    $("#pvp-result").textContent = mine
      ? `🏆 Вы выиграли ${last.payout} ⭐!`
      : `🏆 ${last.winner.name} забирает ${last.payout} ⭐`;
    haptic(mine ? "win" : undefined);
    if (mine) loadMe();
  } finally {
    pvp.animating = false;
  }
}

async function pvpRefresh() {
  try {
    const s = await api("/api/pvp");
    $("#pvp-fee").textContent = Math.round(s.commission * 100);
    const round = s.round;
    if (s.last && pvp.lastSeen !== null && s.last.id !== pvp.lastSeen && s.last.finished_ago < 15 && !pvp.animating) {
      pvp.lastSeen = s.last.id;
      pvpAnimate(s.last);
    } else if (pvp.lastSeen === null) {
      pvp.lastSeen = s.last ? s.last.id : 0;
    }
    $("#pvp-pot").textContent = round ? round.pot : 0;
    if (!round || !round.players.length) $("#pvp-timer").textContent = "Ждём игроков…";
    else if (round.ends_in === null) $("#pvp-timer").textContent = "Ждём второго игрока…";
    else $("#pvp-timer").textContent = `⏱ ${Math.ceil(round.ends_in)} с`;
    if (!pvp.animating) pvpRenderPlayers(round);
  } catch (e) { /* повторим */ }
}

function pvpEnter() {
  pvpLeave();
  pvpRefresh();
  pvp.timer = setInterval(pvpRefresh, 1000);
}

function pvpLeave() {
  clearInterval(pvp.timer);
  pvp.timer = 0;
}

async function pvpBet() {
  await guard(async () => {
    const amount = getBet("pvp");
    const s = await api("/api/pvp/bet", { amount });
    await loadMe();
    pvpRenderPlayers(s.round);
    toast("Ставка принята");
    haptic();
  });
}

// ---------- запуск ----------

function bind() {
  $$("[data-go]").forEach((b) => b.addEventListener("click", () => { haptic(); go(b.dataset.go); }));
  $("#back").addEventListener("click", goBack);
  $("#balance-btn").addEventListener("click", () => { renderPresets(); go("deposit"); });
  $("#dep-btn").addEventListener("click", () => deposit(parseInt($("#dep-amount").value, 10)));
  $("#check-btn").addEventListener("click", activateCheck);
  $("#slots-spin").addEventListener("click", slotsSpin);
  $("#dice-chance").addEventListener("input", diceUpdate);
  $("#dice-btn").addEventListener("click", diceRoll);
  $("#roulette-btn").addEventListener("click", rouletteSpin);
  $$("#rbets .rb").forEach((b) => b.addEventListener("click", () => {
    rouletteType = b.dataset.type;
    $$("#rbets .rb").forEach((x) => x.classList.toggle("sel", x === b));
    $("#rnum-row").classList.toggle("hidden", rouletteType !== "number");
    haptic();
  }));
  $("#rbets .rb").classList.add("sel");
  $("#mines-count").addEventListener("input", (e) => { $("#mines-count-label").textContent = e.target.value; });
  $("#mines-btn").addEventListener("click", minesAction);
  $("#crash-btn").addEventListener("click", crashAction);
  $("#case-btn").addEventListener("click", openCase);
  $("#pvp-btn").addEventListener("click", pvpBet);
  if (tg && tg.BackButton) tg.BackButton.onClick(goBack);
}

async function init() {
  if (tg) {
    tg.ready();
    tg.expand();
    try { tg.setHeaderColor("#0f0b1e"); tg.setBackgroundColor("#0f0b1e"); } catch (e) { /* старые клиенты */ }
  }
  betBoxes();
  bind();
  diceUpdate();
  minesRender(null);
  crashDraw(1);
  if (!tg || !tg.initData) {
    toast("Откройте казино через кнопку в Telegram-боте", true);
    return;
  }
  await loadMe();
  renderPaytable();
  renderPresets();
}

init();
