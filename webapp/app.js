"use strict";

const tg = window.Telegram ? window.Telegram.WebApp : null;
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const state = { me: null, config: null, screen: "home", busy: false };
const GAME_NAMES = { slots: "🎰 Слоты", dice: "🎲 Кости", roulette: "🎡 Рулетка", mines: "💣 Мины",
  crash: "🚀 Краш", case: "🎁 Кейс", pvp: "⚔️ PvP" };
const TITLES = { deposit: "⭐ Пополнение", slots: "🎰 Слоты", crash: "🚀 Краш", mines: "💣 Мины",
  dice: "🎲 Кости", roulette: "🎡 Рулетка", cases: "🎁 Кейсы", case: "🎁 Кейс", pvp: "⚔️ PvP-рулетка" };
const PVP_COLORS = ["#FFC53D", "#2EE59D", "#4DA3FF", "#FF4D6D", "#C084FC", "#FF8A00", "#2EE6D6", "#FF6FCF"];
const BIG_WIN_X = 10;

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
  return null;
}

function fmt(n) {
  return Number(n).toLocaleString("ru-RU");
}

function fmtX(x) {
  return "×" + (x >= 100 ? Math.round(x) : Number(x).toFixed(2));
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
  $("#balance").textContent = fmt(value);
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

// ---------- большой выигрыш ----------

function bigWin(x, win) {
  const box = $("#bigwin");
  $("#bigwin-label").textContent = x >= 100 ? "MEGA WIN" : x >= 50 ? "SUPER WIN" : "BIG WIN";
  $("#bigwin-x").textContent = fmtX(x);
  $("#bigwin-sum").textContent = `+${fmt(win)} ⭐`;
  const conf = $("#confetti");
  conf.innerHTML = "";
  const colors = ["#FFC53D", "#A855F7", "#EC4899", "#2EE59D", "#FFFFFF"];
  for (let i = 0; i < 70; i++) {
    const c = document.createElement("i");
    c.style.left = Math.random() * 100 + "%";
    c.style.background = colors[i % colors.length];
    c.style.animationDuration = 1.8 + Math.random() * 2 + "s";
    c.style.animationDelay = Math.random() * 0.6 + "s";
    conf.append(c);
  }
  box.classList.remove("hidden");
  haptic("win");
}

function celebrate(bet, win) {
  if (win > 0 && win >= bet * BIG_WIN_X) bigWin(win / bet, win);
}

// ---------- навигация ----------

function go(screen) {
  if (screen !== "pvp") pvpLeave();
  if (screen !== "home") tickerStop();
  state.screen = screen;
  $$(".screen").forEach((s) => s.classList.toggle("active", s.id === screen));
  const isHome = screen === "home";
  $("#brand").classList.toggle("hidden", !isHome);
  $("#title").classList.toggle("hidden", isHome);
  $("#title").textContent = TITLES[screen] || "";
  $("#back").classList.toggle("hidden", isHome || !!(tg && tg.BackButton));
  if (tg && tg.BackButton) isHome ? tg.BackButton.hide() : tg.BackButton.show();
  window.scrollTo(0, 0);
  const enter = { home: homeEnter, crash: crashEnter, mines: minesEnter, pvp: pvpEnter, cases: renderCases, deposit: renderPresets };
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
    const cfg = () => state.config || { min_bet: 1, max_bet: 10000 };
    const clamp = (v) => Math.max(cfg().min_bet, Math.min(cfg().max_bet, v));
    const mk = (label, fn) => {
      const b = document.createElement("button");
      b.textContent = label;
      b.addEventListener("click", () => {
        input.value = fn(parseInt(input.value, 10) || 0);
        store(key, input.value);
        input.dispatchEvent(new Event("input"));
        haptic();
      });
      return b;
    };
    box.append(mk("½", (v) => clamp(Math.floor(v / 2))), input, mk("×2", (v) => clamp(v * 2)),
      mk("MAX", () => clamp(state.me ? state.me.balance : cfg().max_bet)));
  });
}

function betInput(game) {
  return $(`.betbox[data-bet="${game}"] input`);
}

function getBet(game) {
  const v = parseInt(betInput(game).value, 10);
  if (!Number.isInteger(v) || v <= 0) throw new Error("Введите ставку");
  return v;
}

// ---------- главная ----------

async function loadMe() {
  const me = await api("/api/me");
  state.me = me;
  state.config = me.config;
  setBalance(me.balance);
  $("#hello").textContent = me.user.name;
  renderHistory(me.history);
}

async function homeEnter() {
  try { await loadMe(); } catch (e) { toast(e.message, true); }
  loadTicker();
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
    name.textContent = `${GAME_NAMES[h.game] || h.game} · ${fmt(h.bet)} ⭐`;
    const res = document.createElement("b");
    const diff = h.win - h.bet;
    res.className = diff >= 0 ? "win" : "lose";
    res.textContent = (diff >= 0 ? "+" : "") + fmt(diff) + " ⭐";
    row.append(name, res);
    box.append(row);
  });
}

let tickerTimer = 0;
async function loadTicker() {
  clearTimeout(tickerTimer);
  try {
    const { wins } = await api("/api/feed");
    const track = $("#ticker-track");
    track.innerHTML = "";
    if (wins.length) {
      const items = wins.concat(wins);  // дублируем для бесконечной прокрутки
      items.forEach((w) => {
        const el = document.createElement("div");
        el.className = "tk";
        const nm = document.createElement("span");
        nm.textContent = `${(GAME_NAMES[w.game] || "").split(" ")[0]} ${w.name}`;
        const x = document.createElement("span");
        x.className = "xx";
        x.textContent = fmtX(w.x);
        const sum = document.createElement("b");
        sum.textContent = `+${fmt(w.win)} ⭐`;
        el.append(nm, x, sum);
        track.append(el);
      });
    }
  } catch (e) { /* лента не критична */ }
  if (state.screen === "home") tickerTimer = setTimeout(loadTicker, 20000);
}

function tickerStop() {
  clearTimeout(tickerTimer);
}

async function activateCheck() {
  await guard(async () => {
    const code = $("#check-code").value.trim();
    if (!code) throw new Error("Введите код чека");
    const r = await api("/api/check", { code });
    $("#check-code").value = "";
    toast(`🎁 Чек активирован: +${fmt(r.amount)} ⭐`);
    haptic("win");
  });
}

// ---------- пополнение ----------

function renderPresets() {
  const box = $("#presets");
  box.innerHTML = "";
  (state.config ? state.config.deposit_presets : [50, 100, 250, 500, 1000, 2500]).forEach((a) => {
    const b = document.createElement("button");
    b.textContent = fmt(a) + " ⭐";
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
          await loadMe().catch(() => {});
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

const REEL_H = 104;

function renderPaytable() {
  const s = state.config && state.config.slots;
  if (!s) return;
  const box = $("#paytable");
  box.innerHTML = "";
  const rows = Object.entries(s.triple).reverse().map(([sym, m]) => [sym + sym + sym, m]);
  rows.push(["💎💎", s.two_diamonds], ["🍒🍒", s.two_cherries]);
  rows.forEach(([combo, m]) => {
    const d = document.createElement("div");
    const c = document.createElement("span");
    c.textContent = combo;
    const x = document.createElement("b");
    x.textContent = "×" + m;
    d.append(c, x);
    box.append(d);
  });
}

function reelStrip(finalSymbol, count) {
  const syms = (state.config && state.config.slots.symbols) || ["🍒", "🍋", "🍇", "🔔", "⭐", "7️⃣", "💎"];
  const items = [];
  for (let i = 0; i < count; i++) items.push(syms[Math.floor(Math.random() * syms.length)]);
  items.push(finalSymbol);
  return items;
}

async function slotsSpin() {
  await guard(async () => {
    const bet = getBet("slots");
    const machine = $(".slot-machine");
    machine.classList.remove("won");
    $("#slots-spin").disabled = true;
    $("#slots-result").className = "result";
    $("#slots-result").textContent = "Крутим…";
    try {
      const r = await api("/api/slots", { bet });
      haptic();
      const reels = $$("#slots .strip");
      await Promise.all(reels.map((strip, i) => new Promise((resolve) => {
        const items = reelStrip(r.reels[i], 20 + i * 7);
        strip.innerHTML = items.map((s) => `<div>${s}</div>`).join("");
        strip.style.transition = "none";
        strip.style.transform = "translateY(0)";
        void strip.offsetHeight;
        const ms = 1000 + i * 400;
        strip.style.transition = `transform ${ms}ms cubic-bezier(.15,.85,.25,1.02)`;
        strip.style.transform = `translateY(-${(items.length - 1) * REEL_H}px)`;
        setTimeout(() => { haptic(); resolve(); }, ms);
      })));
      const res = $("#slots-result");
      if (r.win > 0) {
        machine.classList.add("won");
        res.className = "result win";
        res.textContent = `${fmtX(r.multiplier)} · +${fmt(r.win)} ⭐`;
        haptic("win");
        celebrate(bet, r.win);
      } else {
        res.className = "result lose";
        res.textContent = "Мимо, ещё раз?";
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
    if (it.em) {
      const e = document.createElement("span");
      e.className = "em";
      e.textContent = it.em;
      d.append(e);
    }
    if (it.text) {
      const big = document.createElement("span");
      big.textContent = it.text;
      d.append(big);
    }
    if (it.sub) {
      const s = document.createElement("small");
      s.textContent = it.sub;
      d.append(s);
    }
    track.append(d);
  });
  const itemW = 92;
  const jitter = duration ? (Math.random() - 0.5) * 60 : 0;
  const offset = roller.clientWidth / 2 - (targetIndex * itemW + itemW / 2) + jitter;
  track.style.transition = "none";
  track.style.transform = "translateX(0)";
  void track.offsetWidth;
  track.style.transition = duration ? `transform ${duration}ms cubic-bezier(.08,.75,.12,1)` : "none";
  track.style.transform = `translateX(${offset}px)`;
  if (duration) await sleep(duration + 100);
}

// ---------- рулетка ----------

let rouletteType = "red";

function rouletteColor(n) {
  if (n === 0) return "green";
  const red = (state.config && state.config.red) || [];
  return red.includes(n) ? "red" : "black";
}

function rouletteIdle() {
  const items = [];
  for (let i = 0; i < 12; i++) {
    const n = Math.floor(Math.random() * 37);
    items.push({ text: String(n), cls: rouletteColor(n) });
  }
  roll("#roulette-roller", items, 5, 0);
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
      await roll("#roulette-roller", items, 50, 3800);
      const res = $("#roulette-result");
      if (r.win > 0) {
        res.className = "result win";
        res.textContent = `Выпало ${r.number} · +${fmt(r.win)} ⭐`;
        haptic("win");
        celebrate(bet, r.win);
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

let diceOver = false;

function diceChance() {
  return Math.round(parseFloat(String($("#dice-chance-input").value).replace(",", ".")) * 100) / 100;
}

function diceUpdate(fromSlider) {
  const cfg = (state.config && state.config.dice) || { min: 0.01, max: 98, edge: 0.01 };
  if (fromSlider) $("#dice-chance-input").value = $("#dice-chance").value;
  let chance = diceChance();
  if (!Number.isFinite(chance)) return;
  chance = Math.min(cfg.max, Math.max(cfg.min, chance));
  if (!fromSlider) $("#dice-chance").value = chance;
  const mult = Math.floor((1 - cfg.edge) * 100 / chance * 10000) / 10000;
  $("#dice-mult").textContent = fmtX(mult);
  const target = diceOver ? 100 - chance : chance;
  $("#dice-cond-label").textContent = diceOver ? "Больше" : "Меньше";
  $("#dice-under").textContent = target.toFixed(2);
  const win = $("#dice-win");
  win.style.left = diceOver ? target + "%" : "0";
  win.style.width = chance + "%";
}

async function diceRoll() {
  await guard(async () => {
    const bet = getBet("dice");
    const chance = diceChance();
    $("#dice-btn").disabled = true;
    try {
      const r = await api("/api/dice", { bet, chance, over: diceOver });
      haptic();
      const el = $("#dice-roll");
      el.className = "dice-roll";
      const marker = $("#dice-marker");
      marker.style.left = r.roll + "%";
      marker.textContent = Math.round(r.roll);
      const start = performance.now();
      await new Promise((resolve) => {
        const tick = (now) => {
          const t = Math.min(1, (now - start) / 700);
          el.textContent = t < 1 ? (Math.random() * 100).toFixed(2) : r.roll.toFixed(2);
          t < 1 ? requestAnimationFrame(tick) : resolve();
        };
        requestAnimationFrame(tick);
      });
      el.className = "dice-roll " + (r.won ? "win" : "lose");
      if (r.won) {
        toast(`${fmtX(r.multiplier)} · +${fmt(r.win)} ⭐`);
        haptic("win");
        celebrate(bet, r.win);
      } else {
        haptic("lose");
      }
    } finally {
      $("#dice-btn").disabled = false;
    }
  });
}

// ---------- мины ----------

let minesGame = null;
let minesCount = 3;

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
  const active = game && game.active;
  $("#mines-seg").classList.toggle("hidden", !!active);
  if (active) {
    $("#mines-mult").textContent = fmtX(game.multiplier);
    $("#mines-next").textContent = game.next_multiplier ? fmtX(game.next_multiplier) : "—";
    $("#mines-win").textContent = fmt(game.opened.length ? game.cashout : 0) + " ⭐";
    btn.textContent = game.opened.length ? `Забрать ${fmt(game.cashout)} ⭐` : "Откройте клетку";
    btn.classList.toggle("cash", game.opened.length > 0);
  } else {
    btn.textContent = "Играть";
    btn.classList.remove("cash");
    if (!reveal) minesPreview();
  }
}

function minesPreview() {
  // Множители до начала игры: 1-я клетка при выбранном числе мин
  const edge = (state.config && state.config.dice && state.config.dice.edge) || 0.01;
  let fair = 25 / (25 - minesCount);
  $("#mines-mult").textContent = "×1.00";
  $("#mines-next").textContent = fmtX(Math.round((1 - edge) * fair * 10000) / 10000);
  $("#mines-win").textContent = "0 ⭐";
}

async function minesEnter() {
  try {
    const r = await api("/api/mines");
    minesGame = r.game;
    minesRender(minesGame);
    $("#mines-info").textContent = minesGame ? "Игра продолжается" : "";
    $("#mines-info").className = "result";
  } catch (e) { toast(e.message, true); }
}

async function minesAction() {
  await guard(async () => {
    const info = $("#mines-info");
    if (minesGame && minesGame.active) {
      if (!minesGame.opened.length) return;
      const r = await api("/api/mines/cashout", {});
      minesGame = null;
      minesRender(r, r);
      info.className = "result win";
      info.textContent = `${fmtX(r.multiplier)} · +${fmt(r.win)} ⭐`;
      haptic("win");
      celebrate(r.bet, r.win);
      return;
    }
    const bet = getBet("mines");
    minesGame = await api("/api/mines/start", { bet, mines: minesCount });
    info.className = "result";
    info.textContent = "";
    minesRender(minesGame);
    haptic();
  });
}

async function minesOpen(cell) {
  if (!minesGame || !minesGame.active) return;
  await guard(async () => {
    const r = await api("/api/mines/open", { cell });
    const info = $("#mines-info");
    if (r.boom !== undefined) {
      minesGame = null;
      minesRender(r, r);
      info.className = "result lose";
      info.textContent = "💥 Мина! Ставка сгорела";
      haptic("lose");
    } else if (r.win !== undefined) {
      minesGame = null;
      minesRender(r, r);
      info.className = "result win";
      info.textContent = `Все клетки! ${fmtX(r.multiplier)} · +${fmt(r.win)} ⭐`;
      celebrate(r.bet, r.win);
    } else {
      minesGame = r;
      minesRender(r);
      haptic();
    }
  });
}

// ---------- краш ----------

const crash = { running: false, start: 0, growth: 0.07, raf: 0, poll: 0, bet: 0 };

function crashHistory(point) {
  let list = [];
  try { list = JSON.parse(store("crash:hist") || "[]"); } catch (e) { list = []; }
  if (point !== undefined) {
    list.unshift(point);
    list = list.slice(0, 20);
    store("crash:hist", JSON.stringify(list));
  }
  const box = $("#crash-history");
  box.innerHTML = "";
  list.forEach((p) => {
    const c = document.createElement("span");
    c.className = "chip " + (p < 2 ? "lo" : p < 10 ? "mid" : "hi");
    c.textContent = fmtX(p);
    box.append(c);
  });
}

function crashCanvas() {
  const c = $("#crash-canvas");
  const dpr = window.devicePixelRatio || 1;
  if (c.width !== Math.round(c.clientWidth * dpr)) {
    c.width = Math.round(c.clientWidth * dpr);
    c.height = Math.round(c.clientHeight * dpr);
  }
  return c;
}

function crashDraw(mult, color) {
  const c = crashCanvas();
  const ctx = c.getContext("2d");
  const w = c.width, h = c.height, dpr = window.devicePixelRatio || 1;
  ctx.clearRect(0, 0, w, h);
  // сетка
  ctx.strokeStyle = "rgba(255,255,255,.05)";
  ctx.lineWidth = dpr;
  for (let i = 1; i < 4; i++) {
    ctx.beginPath(); ctx.moveTo(0, (h * i) / 4); ctx.lineTo(w, (h * i) / 4); ctx.stroke();
  }
  const t = Math.log(Math.max(mult, 1)) / crash.growth;
  const tMax = Math.max(8, t * 1.2);
  const mMax = Math.max(2, mult * 1.25);
  const pts = [];
  for (let i = 0; i <= 80; i++) {
    const ti = (t * i) / 80;
    const mi = Math.exp(crash.growth * ti);
    pts.push([(ti / tMax) * w, h - ((mi - 1) / (mMax - 1)) * (h * 0.8) - 8 * dpr]);
  }
  const col = color || "#EC4899";
  const grad = ctx.createLinearGradient(0, 0, 0, h);
  grad.addColorStop(0, col + "66");
  grad.addColorStop(1, col + "00");
  ctx.beginPath();
  pts.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
  ctx.lineTo(pts[pts.length - 1][0], h);
  ctx.lineTo(0, h);
  ctx.fillStyle = grad;
  ctx.fill();
  ctx.beginPath();
  pts.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
  ctx.strokeStyle = col;
  ctx.lineWidth = 4 * dpr;
  ctx.lineCap = "round";
  ctx.shadowColor = col;
  ctx.shadowBlur = 16 * dpr;
  ctx.stroke();
  ctx.shadowBlur = 0;
  const [tx, ty] = pts[pts.length - 1];
  ctx.font = `${28 * dpr}px serif`;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(color === "#FF4D6D" ? "💥" : "🚀", tx, ty);
}

function crashShow(mult, cls, sub) {
  const el = $("#crash-mult");
  el.textContent = Number(mult).toFixed(2) + "×";
  el.className = "crash-mult " + (cls || "");
  if (sub !== undefined) $("#crash-sub").textContent = sub;
}

function crashStop() {
  crash.running = false;
  cancelAnimationFrame(crash.raf);
  clearInterval(crash.poll);
  $("#crash-btn").textContent = "Старт";
  $("#crash-btn").classList.remove("cash");
}

function crashFinish(r) {
  crashStop();
  if (r.status === "cashed") {
    crashShow(r.multiplier, "win", `Вы забрали +${fmt(r.win)} ⭐ · краш на ${fmtX(r.point)}`);
    crashDraw(r.multiplier, "#2EE59D");
    haptic("win");
    celebrate(r.bet, r.win);
    crashHistory(r.point);
  } else if (r.status === "crashed") {
    crashShow(r.point, "lose", "Краш! Ставка сгорела");
    crashDraw(r.point, "#FF4D6D");
    haptic("lose");
    crashHistory(r.point);
  } else {
    loadMe().catch(() => {});
  }
}

function crashRun(r) {
  crash.running = true;
  crash.growth = r.growth || crash.growth;
  crash.start = performance.now() - (r.elapsed || 0) * 1000;
  crash.bet = r.bet;
  $("#crash-btn").classList.add("cash");
  $("#crash-sub").textContent = r.auto ? `Автовывод на ${fmtX(r.auto)}` : "Успейте забрать!";
  const frame = () => {
    if (!crash.running) return;
    const t = (performance.now() - crash.start) / 1000;
    const m = Math.floor(Math.exp(crash.growth * t) * 100) / 100;
    crashShow(m);
    crashDraw(m);
    $("#crash-btn").textContent = `Забрать ${fmt(Math.floor(crash.bet * m))} ⭐`;
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
  crashShow(1, "", "Сделайте ставку");
  crashDraw(1);
  crashHistory();
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
    const top = c.prizes[c.prizes.length - 1];
    b.innerHTML = '<div class="e"></div><div class="n"></div><div class="j"></div><div class="p"></div>';
    b.querySelector(".e").textContent = c.emoji;
    b.querySelector(".n").textContent = c.name;
    b.querySelector(".j").textContent = `джекпот ${top.gift} ${fmt(top.amount)} ⭐`;
    b.querySelector(".p").textContent = fmt(c.price) + " ⭐";
    b.addEventListener("click", () => openCaseScreen(c));
    box.append(b);
  });
}

function prizeItem(p, price) {
  const ratio = p.amount / price;
  const bg = ratio >= 20 ? "linear-gradient(180deg,#FFD166,#C77800)"
    : ratio >= 5 ? "linear-gradient(180deg,#F06292,#9C1C5B)"
    : ratio >= 2 ? "linear-gradient(180deg,#B67DFF,#6A2BC2)"
    : ratio >= 1 ? "linear-gradient(180deg,#4DA3FF,#1F5BB8)"
    : "linear-gradient(180deg,#3A3350,#241E36)";
  return { em: p.gift, sub: fmt(p.amount) + " ⭐", bg };
}

function pickPrize(c) {
  let r = Math.random() * 100;
  for (const p of c.prizes) { r -= p.chance; if (r <= 0) return p; }
  return c.prizes[0];
}

function openCaseScreen(c) {
  currentCase = c;
  go("case");
  $("#title").textContent = `${c.emoji} ${c.name}`;
  $("#case-emoji").textContent = c.emoji;
  $("#case-btn").textContent = `Открыть за ${fmt(c.price)} ⭐`;
  $("#case-result").textContent = "";
  const box = $("#case-prizes");
  box.innerHTML = "";
  c.prizes.slice().reverse().forEach((p) => {
    const d = document.createElement("div");
    const e = document.createElement("span");
    e.className = "em";
    e.textContent = p.gift;
    const s = document.createElement("small");
    s.textContent = p.chance + "%";
    d.append(e, document.createTextNode(fmt(p.amount) + " ⭐"), s);
    box.append(d);
  });
  const items = [];
  for (let i = 0; i < 12; i++) items.push(prizeItem(pickPrize(c), c.price));
  roll("#case-roller", items, 5, 0);
}

async function openCase() {
  if (!currentCase) return;
  await guard(async () => {
    $("#case-btn").disabled = true;
    try {
      const r = await api("/api/case", { case: currentCase.id });
      haptic();
      const won = currentCase.prizes.find((p) => p.amount === r.prize) || { amount: r.prize, gift: r.gift };
      const items = [];
      for (let i = 0; i < 60; i++) items.push(prizeItem(i === 50 ? won : pickPrize(currentCase), currentCase.price));
      $("#case-result").className = "result";
      $("#case-result").textContent = "Открываем…";
      await roll("#case-roller", items, 50, 5000);
      const res = $("#case-result");
      const good = r.prize >= currentCase.price;
      res.className = "result " + (good ? "win" : "lose");
      res.textContent = `${r.gift} ${fmt(r.prize)} ⭐`;
      haptic(good ? "win" : "lose");
      celebrate(currentCase.price, r.prize);
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

function initials(name) {
  return (name || "?").replace("@", "").slice(0, 2).toUpperCase();
}

function pvpRenderPlayers(round) {
  const box = $("#pvp-players");
  const bar = $("#pvp-bar");
  box.innerHTML = "";
  bar.innerHTML = "";
  const players = round ? round.players : [];
  if (!players.length) {
    box.innerHTML = '<div class="note">Пока никого. Сделайте первую ставку!</div>';
    return;
  }
  players.forEach((p) => {
    const seg = document.createElement("span");
    seg.style.width = p.chance + "%";
    seg.style.background = pvpColor(p.id);
    bar.append(seg);
    const row = document.createElement("div");
    row.className = "pl" + (state.me && p.id === state.me.user.id ? " me" : "");
    const av = document.createElement("span");
    av.className = "av";
    av.style.background = pvpColor(p.id);
    av.textContent = initials(p.name);
    const nm = document.createElement("span");
    nm.className = "nm";
    nm.textContent = p.name;
    const am = document.createElement("span");
    am.className = "am";
    am.textContent = fmt(p.amount) + " ⭐";
    const ch = document.createElement("span");
    ch.className = "ch";
    ch.textContent = p.chance + "%";
    row.append(av, nm, am, ch);
    box.append(row);
  });
}

async function pvpAnimate(last) {
  pvp.animating = true;
  try {
    const pick = () => {
      let r = Math.random() * 100;
      for (const p of last.players) { r -= p.chance; if (r <= 0) return p; }
      return last.players[0];
    };
    const items = [];
    for (let i = 0; i < 60; i++) {
      const p = i === 50 ? last.winner : pick();
      items.push({ text: initials(p.name), sub: p.name, bg: pvpColor(p.id) });
    }
    $("#pvp-result").className = "result";
    $("#pvp-result").textContent = "Крутим…";
    await roll("#pvp-roller", items, 50, 5500);
    const mine = state.me && last.winner.id === state.me.user.id;
    $("#pvp-result").className = "result " + (mine ? "win" : "");
    $("#pvp-result").textContent = mine
      ? `🏆 Вы забрали ${fmt(last.payout)} ⭐!`
      : `🏆 ${last.winner.name} забирает ${fmt(last.payout)} ⭐`;
    if (mine) {
      const myBet = (last.players.find((p) => p.id === last.winner.id) || {}).amount || last.payout;
      celebrate(myBet, last.payout);
      haptic("win");
      loadMe().catch(() => {});
    }
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
    $("#pvp-pot").textContent = fmt(round ? round.pot : 0);
    const timer = $("#pvp-timer");
    if (!round || !round.players.length) timer.textContent = "Ждём игроков…";
    else if (round.ends_in === null) timer.textContent = "Ждём второго…";
    else timer.textContent = `⏱ ${Math.ceil(round.ends_in)} с`;
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
    loadMe().catch(() => {});
    pvpRenderPlayers(s.round);
    toast("Ставка принята");
    haptic();
  });
}

// ---------- запуск ----------

function bind() {
  $$("[data-go]").forEach((b) => b.addEventListener("click", () => { haptic(); go(b.dataset.go); }));
  $("#back").addEventListener("click", goBack);
  $("#brand").addEventListener("click", () => go("home"));
  $("#balance-btn").addEventListener("click", () => go("deposit"));
  $("#dep-btn").addEventListener("click", () => deposit(parseInt($("#dep-amount").value, 10)));
  $("#check-btn").addEventListener("click", activateCheck);
  $("#slots-spin").addEventListener("click", slotsSpin);
  $("#dice-chance").addEventListener("input", () => diceUpdate(true));
  $("#dice-chance-input").addEventListener("input", () => diceUpdate(false));
  $$("#dice-mode button").forEach((b) => b.addEventListener("click", () => {
    diceOver = b.dataset.over === "1";
    $$("#dice-mode button").forEach((x) => x.classList.toggle("sel", x === b));
    diceUpdate(false);
    haptic();
  }));
  $("#dice-btn").addEventListener("click", diceRoll);
  $("#roulette-btn").addEventListener("click", rouletteSpin);
  $$("#rbets .rb").forEach((b) => b.addEventListener("click", () => {
    rouletteType = b.dataset.type;
    $$("#rbets .rb").forEach((x) => x.classList.toggle("sel", x === b));
    $("#rnum-row").classList.toggle("hidden", rouletteType !== "number");
    haptic();
  }));
  $("#rbets .rb").classList.add("sel");
  $$("#mines-seg button").forEach((b) => b.addEventListener("click", () => {
    minesCount = parseInt(b.dataset.m, 10);
    $$("#mines-seg button").forEach((x) => x.classList.toggle("sel", x === b));
    minesPreview();
    haptic();
  }));
  $("#mines-btn").addEventListener("click", minesAction);
  $("#crash-btn").addEventListener("click", crashAction);
  $("#case-btn").addEventListener("click", openCase);
  $("#pvp-btn").addEventListener("click", pvpBet);
  $("#bigwin-ok").addEventListener("click", () => $("#bigwin").classList.add("hidden"));
  if (tg && tg.BackButton) tg.BackButton.onClick(goBack);
}

async function init() {
  if (tg) {
    tg.ready();
    tg.expand();
    try { tg.setHeaderColor("#0A0612"); tg.setBackgroundColor("#0A0612"); } catch (e) { /* старые клиенты */ }
  }
  betBoxes();
  bind();
  diceUpdate(false);
  minesRender(null);
  crashDraw(1);
  rouletteIdle();
  if (!tg || !tg.initData) {
    toast("Откройте Svag Gifts через кнопку в Telegram-боте", true);
    return;
  }
  await homeEnter();
  renderPaytable();
  renderPresets();
}

init();
