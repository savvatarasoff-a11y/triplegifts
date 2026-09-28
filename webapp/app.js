"use strict";

const tg = window.Telegram ? window.Telegram.WebApp : null;
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const STAR = "★";
const state = { me: null, config: null, screen: "home", busy: false };
const GAME_NAMES = { slots: "Слоты", dice: "Кости", mines: "Мины", crash: "Краш", case: "Кейс",
  pvp: "PvP-рулетка", hockey: "PvP-хоккей" };
const TITLES = { wallet: "Кошелёк", slots: "Слоты", crash: "Краш", mines: "Мины", dice: "Кости",
  cases: "Кейсы", case: "Кейс", pvp: "PvP-рулетка", hockey: "PvP-хоккей" };
// Оттенки фирменного фиолетового и белый: [фон, цвет текста]
const PVP_COLORS = [["#8B5CF6", "#FFFFFF"], ["#FFFFFF", "#0B0A10"], ["#6D28D9", "#FFFFFF"], ["#C4B5FD", "#0B0A10"],
  ["#4C1D95", "#FFFFFF"], ["#EDE9FE", "#0B0A10"], ["#7C3AED", "#FFFFFF"], ["#A78BFA", "#0B0A10"]];
const WD_STATUS = { pending: "на проверке", sending: "отправляется", sent: "отправлен", rejected: "отклонён" };
const BIG_WIN_X = 10;
const COLOR = { accent: "#8B5CF6", soft: "#A78BFA", pale: "#EDE9FE", deep: "#6D28D9", white: "#FFFFFF", muted: "#5E5977", bg: "#0B0A10" };
const FX_COLORS = [COLOR.accent, COLOR.soft, COLOR.white, COLOR.deep, "#C4B5FD"];

// ---------- утилиты ----------

function haptic(kind) {
  try {
    if (!tg || !tg.HapticFeedback) return;
    if (kind === "win") tg.HapticFeedback.notificationOccurred("success");
    else if (kind === "lose") tg.HapticFeedback.notificationOccurred("error");
    else if (kind === "heavy") tg.HapticFeedback.impactOccurred("heavy");
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

const fmt = (n) => Number(n).toLocaleString("ru-RU");
const stars = (n) => `${fmt(n)} ${STAR}`;
const fmtX = (x) => "×" + (x >= 100 ? Math.round(x) : Number(x).toFixed(2));

// deferBalance: баланс из ответа покажем сами — после анимации
async function api(path, body, opts) {
  const req = { method: body === undefined ? "GET" : "POST",
    headers: { Authorization: "tma " + (tg ? tg.initData : "") } };
  if (body !== undefined) {
    req.headers["Content-Type"] = "application/json";
    req.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, req);
  } catch (e) {
    throw new Error("Нет связи с сервером");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || "Ошибка " + res.status);
  if (typeof data.balance === "number" && !(opts && opts.deferBalance)) setBalance(data.balance);
  return data;
}

function setBalance(value) {
  if (state.me) state.me.balance = value;
  const el = $("#balance");
  el.textContent = fmt(value);
  el.parentElement.classList.remove("bump");
  void el.offsetWidth;
  el.parentElement.classList.add("bump");
}

// Ставка списывается на экране сразу, выигрыш добавляется после анимации
function showBetTaken(bet) {
  if (state.me) $("#balance").textContent = fmt(Math.max(0, state.me.balance - bet));
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

async function confirmAsk(text) {
  if (tg && tg.showConfirm) return new Promise((resolve) => tg.showConfirm(text, resolve));
  return window.confirm(text);
}

const initials = (name) => (name || "?").replace("@", "").slice(0, 2).toUpperCase();

// Аватарка игрока: фото из Telegram, если нет — инициалы на цвете игрока
function avatarEl(p, cls, colors) {
  const [bg, fg] = colors;
  const el = document.createElement("span");
  el.className = cls;
  el.style.background = bg;
  el.style.color = fg;
  el.textContent = initials(p.name);
  const img = new Image();
  img.alt = "";
  img.onload = () => el.append(img);
  img.src = `/avatar/${p.id}`;
  return el;
}

const avatarImages = {};
function avatarImage(id) {
  if (!avatarImages[id]) {
    const img = new Image();
    img.ok = false;
    img.onload = () => { img.ok = true; };
    img.src = `/avatar/${id}`;
    avatarImages[id] = img;
  }
  return avatarImages[id];
}

// ---------- частицы ----------

const fx = { parts: [], raf: 0 };

function fxBurst(x, y, opts) {
  const o = Object.assign({ count: 40, speed: 7, colors: FX_COLORS, gravity: 0.25, size: 4, life: 60 }, opts || {});
  for (let i = 0; i < o.count; i++) {
    const a = Math.random() * Math.PI * 2;
    const v = o.speed * (0.4 + Math.random() * 0.8);
    fx.parts.push({ x, y, vx: Math.cos(a) * v, vy: Math.sin(a) * v - o.speed * 0.3, g: o.gravity,
      s: o.size * (0.6 + Math.random() * 0.8), c: o.colors[i % o.colors.length], life: o.life, max: o.life });
  }
  if (!fx.raf) fx.raf = requestAnimationFrame(fxLoop);
}

function fxBurstAt(el, opts) {
  const r = el.getBoundingClientRect();
  fxBurst(r.left + r.width / 2, r.top + r.height / 2, opts);
}

function fxLoop() {
  const c = $("#fx");
  const dpr = window.devicePixelRatio || 1;
  if (c.width !== innerWidth * dpr) { c.width = innerWidth * dpr; c.height = innerHeight * dpr; }
  const ctx = c.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, innerWidth, innerHeight);
  fx.parts = fx.parts.filter((p) => p.life > 0);
  fx.parts.forEach((p) => {
    p.x += p.vx; p.y += p.vy; p.vy += p.g; p.vx *= 0.985; p.life--;
    ctx.globalAlpha = Math.max(0, p.life / p.max);
    ctx.fillStyle = p.c;
    ctx.beginPath();
    ctx.arc(p.x, p.y, p.s, 0, Math.PI * 2);
    ctx.fill();
  });
  ctx.globalAlpha = 1;
  fx.raf = fx.parts.length ? requestAnimationFrame(fxLoop) : 0;
  if (!fx.raf) ctx.clearRect(0, 0, innerWidth, innerHeight);
}

// ---------- большой выигрыш ----------

function bigWin(x, win) {
  $("#bigwin-label").textContent = x >= 100 ? "MEGA WIN" : x >= 50 ? "SUPER WIN" : "BIG WIN";
  $("#bigwin-x").textContent = fmtX(x);
  $("#bigwin-sum").textContent = "+" + stars(win);
  const conf = $("#confetti");
  conf.innerHTML = "";
  for (let i = 0; i < 70; i++) {
    const c = document.createElement("i");
    c.style.left = Math.random() * 100 + "%";
    c.style.background = FX_COLORS[i % FX_COLORS.length];
    c.style.animationDuration = 1.8 + Math.random() * 2 + "s";
    c.style.animationDelay = Math.random() * 0.6 + "s";
    conf.append(c);
  }
  $("#bigwin").classList.remove("hidden");
  haptic("win");
}

function celebrate(bet, win, el) {
  if (win > 0 && el) fxBurstAt(el, { count: win >= bet * 3 ? 60 : 30 });
  if (win > 0 && win >= bet * BIG_WIN_X) bigWin(win / bet, win);
}

// ---------- навигация ----------

function go(screen) {
  if (screen !== "pvp" && screen !== "hockey") pvpLeave();
  if (screen !== "crash") crashLeave();
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
  const enter = { home: homeEnter, crash: crashEnter, mines: minesEnter, cases: renderCases, wallet: walletEnter,
    pvp: () => pvpEnter("roulette"), hockey: () => pvpEnter("hockey") };
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
        haptic();
      });
      return b;
    };
    box.append(mk("½", (v) => clamp(Math.floor(v / 2))), input, mk("×2", (v) => clamp(v * 2)),
      mk("MAX", () => clamp(state.me ? state.me.balance : cfg().max_bet)));
  });
}

function getBet(game) {
  const v = parseInt($(`.betbox[data-bet="${game}"] input`).value, 10);
  if (!Number.isInteger(v) || v <= 0) throw new Error("Введите ставку");
  if (state.me && v > state.me.balance) throw new Error("Недостаточно звёзд на балансе");
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

function historyRow(label, value, positive) {
  const row = document.createElement("div");
  row.className = "h";
  const name = document.createElement("span");
  name.textContent = label;
  const res = document.createElement("b");
  res.className = positive ? "win" : "lose";
  res.textContent = value;
  row.append(name, res);
  return row;
}

function renderHistory(items) {
  const box = $("#history");
  box.innerHTML = "";
  if (!items || !items.length) {
    box.textContent = "Пока пусто — самое время сыграть!";
    return;
  }
  items.forEach((h) => {
    const diff = h.win - h.bet;
    box.append(historyRow(`${GAME_NAMES[h.game] || h.game} · ${stars(h.bet)}`,
      (diff >= 0 ? "+" : "") + stars(diff), diff >= 0));
  });
}

let tickerTimer = 0;
async function loadTicker() {
  clearTimeout(tickerTimer);
  try {
    const { wins } = await api("/api/feed");
    const track = $("#ticker-track");
    track.innerHTML = "";
    wins.concat(wins).forEach((w) => {  // дублируем для бесконечной прокрутки
      const el = document.createElement("div");
      el.className = "tk";
      const nm = document.createElement("span");
      nm.textContent = `${w.name} · ${GAME_NAMES[w.game] || ""}`;
      const x = document.createElement("span");
      x.className = "xx";
      x.textContent = fmtX(w.x);
      const sum = document.createElement("b");
      sum.textContent = "+" + stars(w.win);
      el.append(nm, x, sum);
      track.append(el);
    });
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
    toast("Чек активирован: +" + stars(r.amount));
    fxBurstAt($("#balance-btn"), { count: 30 });
    haptic("win");
  });
}

// ---------- кошелёк ----------

function walletTab(tab) {
  $$("#wallet-tabs button").forEach((b) => b.classList.toggle("sel", b.dataset.tab === tab));
  $("#tab-dep").classList.toggle("hidden", tab !== "dep");
  $("#tab-out").classList.toggle("hidden", tab !== "out");
  if (tab === "out") loadWithdraw();
}

function walletEnter() {
  renderPresets();
  walletTab("dep");
}

function renderPresets() {
  const box = $("#presets");
  box.innerHTML = "";
  (state.config ? state.config.deposit_presets : [50, 100, 250, 500, 1000, 2500]).forEach((a) => {
    const b = document.createElement("button");
    b.textContent = stars(a);
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
        fxBurstAt($("#balance-btn"), { count: 50 });
        haptic("win");
        go("home");
      } else if (status === "failed") {
        toast("Оплата не прошла", true);
      }
    });
  });
}

async function loadWithdraw() {
  const box = $("#gifts");
  box.textContent = "Загружаем подарки…";
  try {
    const data = await api("/api/withdraw");
    const wager = $("#wager");
    wager.classList.toggle("hidden", !data.wager.left);
    wager.textContent = data.wager.left
      ? `Чтобы вывести звёзды из чеков и бонусов, их нужно отыграть: осталось поставить ${stars(data.wager.left)}.`
      : "";
    box.innerHTML = "";
    if (!data.gifts.length) box.textContent = "Подарки сейчас недоступны, попробуйте позже.";
    const balance = state.me ? state.me.balance : 0;
    data.gifts.forEach((gift) => {
      const b = document.createElement("button");
      b.className = "gift";
      const em = document.createElement("span");
      em.className = "em";
      em.textContent = gift.emoji;
      const price = document.createElement("b");
      price.textContent = stars(gift.stars);
      b.append(em, price);
      b.disabled = gift.stars > balance || data.wager.left > 0;
      b.addEventListener("click", () => withdraw(gift));
      box.append(b);
    });
    const hist = $("#wd-history");
    hist.innerHTML = "";
    if (!data.history.length) hist.textContent = "Выводов пока не было.";
    data.history.forEach((w) => {
      hist.append(historyRow(`${w.emoji || "🎁"} №${w.id} · ${stars(w.amount)}`,
        WD_STATUS[w.status] || w.status, w.status === "sent"));
    });
  } catch (e) {
    box.textContent = e.message;
  }
}

async function withdraw(gift) {
  const ok = await confirmAsk(`Вывести ${gift.emoji} за ${gift.stars} ★? Звёзды спишутся сразу, подарок придёт после проверки.`);
  if (!ok) return;
  await guard(async () => {
    await api("/api/withdraw", { gift_id: gift.id });
    toast("Заявка отправлена, ждите подарок");
    haptic("win");
    loadWithdraw();
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
      const r = await api("/api/slots", { bet }, { deferBalance: true });
      showBetTaken(bet);
      haptic();
      const reels = $$("#slots .reel");
      await Promise.all(reels.map((reel, i) => new Promise((resolve) => {
        const strip = reel.querySelector(".strip");
        const items = reelStrip(r.reels[i], 22 + i * 8);
        strip.innerHTML = items.map((sym) => `<div>${sym}</div>`).join("");
        strip.style.transition = "none";
        strip.style.transform = "translateY(0)";
        reel.classList.remove("stop");
        void strip.offsetHeight;
        const ms = 1100 + i * 450;
        strip.classList.add("spin");
        strip.style.transition = `transform ${ms}ms cubic-bezier(.12,.8,.25,1)`;
        strip.style.transform = `translateY(-${(items.length - 1) * REEL_H}px)`;
        setTimeout(() => strip.classList.remove("spin"), ms * 0.75);
        setTimeout(() => { reel.classList.add("stop"); haptic(); resolve(); }, ms);
      })));
      setBalance(r.balance);
      const res = $("#slots-result");
      if (r.win > 0) {
        machine.classList.add("won");
        res.className = "result win reveal";
        res.textContent = `${fmtX(r.multiplier)} · +${stars(r.win)}`;
        haptic("win");
        celebrate(bet, r.win, machine);
      } else {
        res.className = "result lose";
        res.textContent = "Мимо, ещё раз?";
      }
    } finally {
      $("#slots-spin").disabled = false;
    }
  });
}

// ---------- ролик (кейсы, PvP-рулетка) ----------

async function roll(rollerSel, items, targetIndex, duration) {
  const roller = $(rollerSel);
  const track = roller.querySelector(".roller-track");
  track.innerHTML = "";
  items.forEach((it) => {
    const d = document.createElement("div");
    d.className = "it " + (it.cls || "");
    if (it.bg) d.style.background = it.bg;
    if (it.fg) d.style.color = it.fg;
    if (it.player) d.append(avatarEl(it.player, "av-big", it.colors));
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
      const sm = document.createElement("small");
      sm.textContent = it.sub;
      d.append(sm);
    }
    track.append(d);
  });
  const itemW = 92;
  const jitter = duration ? (Math.random() - 0.5) * 50 : 0;
  const offset = roller.clientWidth / 2 - (targetIndex * itemW + itemW / 2) + jitter;
  track.style.transition = "none";
  track.style.transform = "translateX(0)";
  void track.offsetWidth;
  track.style.transition = duration ? `transform ${duration}ms cubic-bezier(.08,.75,.12,1)` : "none";
  track.style.transform = `translateX(${offset}px)`;
  if (!duration) return;
  // Щелчки при прохождении карточек под указателем
  const start = performance.now();
  let lastIdx = -1;
  await new Promise((resolve) => {
    const tick = () => {
      const t = performance.now() - start;
      const m = new DOMMatrixReadOnly(getComputedStyle(track).transform);
      const idx = Math.floor((roller.clientWidth / 2 - m.m41) / itemW);
      if (idx !== lastIdx) { lastIdx = idx; if (t < duration - 300) haptic(); }
      t < duration + 100 ? requestAnimationFrame(tick) : resolve();
    };
    requestAnimationFrame(tick);
  });
  const target = track.children[targetIndex];
  if (target) fxBurstAt(target, { count: 24, speed: 5 });
}

// ---------- кости ----------

let diceOver = false;

function diceCfg() {
  return (state.config && state.config.dice) || { min: 0.01, max: 90, edge: 0.05 };
}

function diceChance() {
  return Math.round(parseFloat(String($("#dice-chance-input").value).replace(",", ".")) * 100) / 100;
}

function diceUpdate(fromSlider) {
  const cfg = diceCfg();
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
      const r = await api("/api/dice", { bet, chance, over: diceOver }, { deferBalance: true });
      showBetTaken(bet);
      haptic();
      const el = $("#dice-roll");
      el.className = "dice-roll";
      const marker = $("#dice-marker");
      marker.style.left = r.roll + "%";
      marker.textContent = Math.round(r.roll);
      const start = performance.now();
      await new Promise((resolve) => {
        const tick = (now) => {
          const t = Math.min(1, (now - start) / 800);
          el.textContent = t < 1 ? (Math.random() * 100).toFixed(2) : r.roll.toFixed(2);
          t < 1 ? requestAnimationFrame(tick) : resolve();
        };
        requestAnimationFrame(tick);
      });
      setBalance(r.balance);
      el.className = "dice-roll pop " + (r.won ? "win" : "lose");
      if (r.won) {
        toast(`${fmtX(r.multiplier)} · +${stars(r.win)}`);
        haptic("win");
        celebrate(bet, r.win, marker);
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
    $("#mines-win").textContent = fmt(game.opened.length ? game.cashout : 0);
    btn.textContent = game.opened.length ? "Забрать " + stars(game.cashout) : "Откройте клетку";
    btn.classList.toggle("cash", game.opened.length > 0);
  } else {
    btn.textContent = "Играть";
    btn.classList.remove("cash");
    if (!reveal) minesPreview();
  }
}

function minesPreview() {
  const edge = diceCfg().edge;
  $("#mines-mult").textContent = "×1.00";
  $("#mines-next").textContent = fmtX(Math.floor((1 - edge) * 25 / (25 - minesCount) * 10000) / 10000);
  $("#mines-win").textContent = "0";
}

async function minesEnter() {
  try {
    const r = await api("/api/mines");
    minesGame = r.game;
    minesRender(minesGame);
    $("#mines-info").className = "result";
    $("#mines-info").textContent = minesGame ? "Игра продолжается" : "";
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
      info.className = "result win reveal";
      info.textContent = `${fmtX(r.multiplier)} · +${stars(r.win)}`;
      haptic("win");
      celebrate(r.bet, r.win, $("#mines-grid"));
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
    const cellEl = () => $("#mines-grid").children[cell];
    if (r.boom !== undefined) {
      minesGame = null;
      minesRender(r, r);
      info.className = "result lose";
      info.textContent = "Мина! Ставка сгорела";
      fxBurstAt(cellEl(), { count: 50, speed: 9, colors: [COLOR.white, COLOR.muted, COLOR.accent] });
      haptic("heavy");
    } else if (r.win !== undefined) {
      minesGame = null;
      minesRender(r, r);
      info.className = "result win reveal";
      info.textContent = `Все клетки! ${fmtX(r.multiplier)} · +${stars(r.win)}`;
      celebrate(r.bet, r.win, $("#mines-grid"));
    } else {
      minesGame = r;
      minesRender(r);
      fxBurstAt(cellEl(), { count: 14, speed: 4, gravity: 0.1 });
      haptic();
    }
  });
}

// ---------- краш (общие раунды) ----------

const crash = { timer: 0, raf: 0, state: null, syncedAt: 0, lastRound: null, stars: [], trail: [], boomShown: null };

function crashCanvas() {
  const c = $("#crash-canvas");
  const dpr = window.devicePixelRatio || 1;
  if (c.width !== Math.round(c.clientWidth * dpr)) {
    c.width = Math.round(c.clientWidth * dpr);
    c.height = Math.round(c.clientHeight * dpr);
    crash.stars = Array.from({ length: 60 }, () => ({ x: Math.random(), y: Math.random(), z: 0.3 + Math.random() }));
  }
  return c;
}

// Текущий множитель на экране: считаем от момента старта, присланного сервером
function crashMultiplier() {
  const s = crash.state;
  if (!s || !s.round || s.round.phase !== "running") return 1;
  const elapsed = s.round.elapsed + (performance.now() - crash.syncedAt) / 1000;
  return Math.floor(Math.exp(s.growth * elapsed) * 100) / 100;
}

function crashDraw(mult, mode) {
  const c = crashCanvas();
  const ctx = c.getContext("2d");
  const w = c.width, h = c.height, dpr = window.devicePixelRatio || 1;
  const growth = (crash.state && crash.state.growth) || 0.07;
  ctx.clearRect(0, 0, w, h);
  // звёзды летят быстрее с ростом множителя
  const speed = mode === "running" ? 0.002 + Math.log(mult) * 0.004 : 0.0005;
  ctx.fillStyle = "rgba(237,233,254,.55)";
  crash.stars.forEach((s) => {
    s.x -= speed * s.z; s.y += speed * s.z * 0.4;
    if (s.x < 0) s.x += 1;
    if (s.y > 1) s.y -= 1;
    ctx.globalAlpha = 0.25 + s.z * 0.4;
    ctx.fillRect(s.x * w, s.y * h, 1.5 * dpr * s.z, 1.5 * dpr * s.z);
  });
  ctx.globalAlpha = 1;
  if (mode === "betting") return;
  const t = Math.log(Math.max(mult, 1)) / growth;
  const tMax = Math.max(8, t * 1.2);
  const mMax = Math.max(2, mult * 1.25);
  const pts = [];
  for (let i = 0; i <= 80; i++) {
    const ti = (t * i) / 80;
    pts.push([(ti / tMax) * w, h - ((Math.exp(growth * ti) - 1) / (mMax - 1)) * (h * 0.75) - 22 * dpr]);
  }
  const col = mode === "crashed" ? COLOR.muted : mode === "cashed" ? COLOR.soft : COLOR.accent;
  const grad = ctx.createLinearGradient(0, 0, 0, h);
  grad.addColorStop(0, col + "55");
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
  const [px, py] = pts[pts.length - 2];
  if (mode === "crashed") return;
  // ракета: белый корпус по направлению полёта и хвост из частиц
  const ang = Math.atan2(ty - py, tx - px);
  crash.trail.push({ x: tx, y: ty, life: 20 });
  crash.trail = crash.trail.filter((p) => --p.life > 0).slice(-40);
  crash.trail.forEach((p) => {
    ctx.globalAlpha = p.life / 40;
    ctx.fillStyle = COLOR.soft;
    ctx.beginPath();
    ctx.arc(p.x - Math.cos(ang) * (20 - p.life) * dpr, p.y - Math.sin(ang) * (20 - p.life) * dpr, (p.life / 5) * dpr, 0, 7);
    ctx.fill();
  });
  ctx.globalAlpha = 1;
  ctx.save();
  ctx.translate(tx, ty);
  ctx.rotate(ang);
  ctx.scale(dpr, dpr);
  ctx.fillStyle = COLOR.white;
  ctx.beginPath();
  ctx.moveTo(14, 0); ctx.quadraticCurveTo(4, -7, -10, -6); ctx.lineTo(-10, 6); ctx.quadraticCurveTo(4, 7, 14, 0);
  ctx.fill();
  ctx.fillStyle = COLOR.accent;
  ctx.beginPath(); ctx.arc(3, 0, 2.6, 0, 7); ctx.fill();
  ctx.beginPath(); ctx.moveTo(-10, -6); ctx.lineTo(-16, -11); ctx.lineTo(-7, -6); ctx.fill();
  ctx.beginPath(); ctx.moveTo(-10, 6); ctx.lineTo(-16, 11); ctx.lineTo(-7, 6); ctx.fill();
  ctx.fillStyle = COLOR.soft;
  ctx.beginPath(); ctx.moveTo(-10, -3); ctx.lineTo(-18 - Math.random() * 6, 0); ctx.lineTo(-10, 3); ctx.fill();
  ctx.restore();
  crash.rocketPos = [tx / dpr, ty / dpr];
}

function crashFrame() {
  const s = crash.state;
  if (state.screen !== "crash") { crash.raf = 0; return; }
  if (!s || !s.round) {  // первое состояние ещё не пришло
    crashDraw(1, "betting");
    crash.raf = requestAnimationFrame(crashFrame);
    return;
  }
  const phase = s.round.phase;
  const multEl = $("#crash-mult");
  const bar = $("#crash-phase");
  if (phase === "betting") {
    const left = Math.max(0, s.round.betting_left - (performance.now() - crash.syncedAt) / 1000);
    multEl.textContent = left.toFixed(1) + " с";
    multEl.className = "crash-mult";
    $("#crash-sub").textContent = "Приём ставок";
    bar.style.width = (left / 7) * 100 + "%";
    crashDraw(1, "betting");
  } else if (phase === "running") {
    const m = crashMultiplier();
    multEl.textContent = m.toFixed(2) + "×";
    multEl.className = "crash-mult";
    bar.style.width = "0";
    const my = s.my;
    crashDraw(m, my && my.cashout ? "cashed" : "running");
    if (my && !my.cashout) $("#crash-btn").textContent = "Забрать " + stars(Math.floor(my.bet * m));
  } else {
    multEl.textContent = s.round.point.toFixed(2) + "×";
    multEl.className = "crash-mult lose";
    $("#crash-sub").textContent = "Краш! Следующий раунд через " + Math.ceil(Math.max(0, s.round.next_in - (performance.now() - crash.syncedAt) / 1000)) + " с";
    crashDraw(s.round.point, "crashed");
  }
  crash.raf = requestAnimationFrame(crashFrame);
}

function crashRenderPlayers(s) {
  const box = $("#crash-players");
  const players = s.players || [];
  const phaseKey = s.round ? s.round.phase + s.round.id : "";
  const sig = phaseKey + JSON.stringify(players);
  if (sig === crash.playersSig) return;  // перерисовываем только при изменениях
  const before = crash.cashedIds || new Set();
  crash.playersSig = sig;
  crash.cashedIds = new Set(players.filter((p) => p.cashout).map((p) => p.id));
  box.innerHTML = "";
  $("#crash-count").textContent = players.length ? `· ${players.length}` : "";
  if (!players.length) {
    box.innerHTML = '<div class="note">Пока никто не поставил</div>';
    return;
  }
  const phase = s.round ? s.round.phase : "";
  players.forEach((p) => {
    const row = document.createElement("div");
    row.className = "pl" + (state.me && p.id === state.me.user.id ? " me" : "") + (p.cashout ? " cashed" : "")
      + (p.cashout && !before.has(p.id) ? " just" : "");
    const nm = document.createElement("span");
    nm.className = "nm";
    nm.textContent = p.name;
    const am = document.createElement("span");
    am.className = "am";
    am.textContent = stars(p.bet);
    const st = document.createElement("span");
    st.className = "st";
    if (p.cashout) { st.classList.add("win"); st.textContent = `${fmtX(p.cashout)} · +${fmt(p.win)}`; }
    else if (phase === "crashed") { st.classList.add("lose"); st.textContent = "сгорела"; }
    else st.textContent = phase === "running" ? "в полёте" : "ставка";
    row.append(avatarEl(p, "av", pvpColorFor(p.id)), nm, am, st);
    box.append(row);
  });
}

function crashHistory(list) {
  const box = $("#crash-history");
  box.innerHTML = "";
  (list || []).forEach((p) => {
    const c = document.createElement("span");
    c.className = "chip " + (p < 2 ? "lo" : p < 10 ? "mid" : "hi");
    c.textContent = fmtX(p);
    box.append(c);
  });
}

function crashButton(s) {
  const btn = $("#crash-btn");
  const phase = s.round ? s.round.phase : "";
  const my = s.my;
  btn.classList.remove("cash");
  btn.disabled = false;
  if (phase === "betting") {
    btn.textContent = my ? `Ставка ${stars(my.bet)} принята` : "Поставить";
    btn.disabled = !!my;
  } else if (phase === "running") {
    if (my && !my.cashout) btn.classList.add("cash");
    else { btn.textContent = my ? `Забрали ${fmtX(my.cashout)}` : "Ждём следующий раунд"; btn.disabled = true; }
  } else {
    btn.textContent = "Ждём следующий раунд";
    btn.disabled = true;
  }
}

async function crashPoll() {
  try {
    const s = await api("/api/crash", undefined, { deferBalance: true });
    const prev = crash.state;
    crash.state = s;
    crash.syncedAt = performance.now();
    const phase = s.round ? s.round.phase : "";
    // взрыв ракеты — один раз на раунд
    if (phase === "crashed" && crash.boomShown !== s.round.id) {
      crash.boomShown = s.round.id;
      if (prev && prev.round && prev.round.id === s.round.id && prev.round.phase === "running") {
        const box = $(".crash-box").getBoundingClientRect();
        const [x, y] = crash.rocketPos || [box.width * 0.7, box.height * 0.4];
        fxBurst(box.left + x, box.top + y, { count: 60, speed: 8, colors: [COLOR.white, COLOR.soft, COLOR.muted] });
        haptic(s.my && !s.my.cashout ? "lose" : "heavy");
      }
      crash.trail = [];
      if (s.my) loadMe().catch(() => {});
    }
    if (phase === "betting" && prev && prev.round && prev.round.id !== s.round.id) $("#crash-sub").textContent = "Приём ставок";
    if (phase === "running") {
      const my = s.my;
      $("#crash-sub").textContent = my && my.cashout ? `Вы забрали +${stars(my.win)}` : my ? `В полёте: ${stars(my.bet)}` : "Ракета летит";
    }
    crashRenderPlayers(s);
    crashHistory(s.history);
    crashButton(s);
  } catch (e) { /* повторим */ }
  const phase = crash.state && crash.state.round ? crash.state.round.phase : "";
  crash.timer = setTimeout(crashPoll, phase === "running" ? 150 : 400);
}

function crashEnter() {
  crashLeave();
  crashPoll();
  crash.raf = requestAnimationFrame(crashFrame);
}

function crashLeave() {
  clearTimeout(crash.timer);
  cancelAnimationFrame(crash.raf);
  crash.timer = 0;
  crash.raf = 0;
}

async function crashAction() {
  const s = crash.state;
  const phase = s && s.round ? s.round.phase : "";
  if (phase === "running" && s.my && !s.my.cashout) {
    try {
      const r = await api("/api/crash/cashout", {}, { deferBalance: true });
      setBalance(r.balance);
      s.my.cashout = r.cashout;
      s.my.win = r.win;
      $("#crash-sub").textContent = `Вы забрали +${stars(r.win)}`;
      crashButton(s);
      haptic("win");
      celebrate(r.bet, r.win, $(".crash-box"));
    } catch (e) { toast(e.message, true); }
    return;
  }
  await guard(async () => {
    const bet = getBet("crash");
    const autoRaw = $("#crash-auto").value.trim().replace(",", ".");
    const body = { bet };
    if (autoRaw) body.auto = parseFloat(autoRaw);
    await api("/api/crash/bet", body);
    haptic();
    fxBurstAt($("#crash-btn"), { count: 16, speed: 4 });
    clearTimeout(crash.timer);
    crashPoll();
  });
}

// ---------- кейсы ----------

let currentCase = null;

function renderCases() {
  const box = $("#cases-list");
  box.innerHTML = "";
  const cases = state.config ? state.config.cases : [];
  if (!cases.length) box.innerHTML = '<div class="note">Кейсы временно недоступны — обновляем цены подарков.</div>';
  cases.forEach((c) => {
    const b = document.createElement("button");
    b.className = "case-card" + (c.id === "nft" ? " nft" : "");
    const top = c.prizes.reduce((a, p) => (p.amount > a.amount ? p : a), c.prizes[0]);
    b.innerHTML = '<div class="e"></div><div class="n"></div><div class="j"></div><div class="p"></div>';
    b.querySelector(".e").textContent = c.emoji;
    b.querySelector(".n").textContent = c.name;
    b.querySelector(".j").textContent = top.kind === "nft" ? `NFT до ${stars(top.amount)}` : `до ${top.emoji} ${stars(top.amount)}`;
    b.querySelector(".p").textContent = stars(c.price);
    b.addEventListener("click", () => openCaseScreen(c));
    box.append(b);
  });
}

// Редкость — оттенками фирменного цвета: от тёмного к белому
function prizeItem(p, price) {
  const ratio = p.amount / price;
  const [bg, fg] = p.kind === "nft" || ratio >= 20 ? ["#FFFFFF", "#0B0A10"]
    : ratio >= 3 ? ["#A78BFA", "#0B0A10"]
    : ratio >= 1.5 ? ["#8B5CF6", "#FFFFFF"]
    : ratio >= 1 ? ["#6D28D9", "#FFFFFF"]
    : ["#242033", "#FFFFFF"];
  return { em: p.emoji, sub: p.kind === "nft" ? "NFT" : stars(p.amount), bg, fg };
}

function pickPrize(c) {
  let r = Math.random() * 100;
  for (const p of c.prizes) { r -= p.chance; if (r <= 0) return p; }
  return c.prizes[0];
}

function openCaseScreen(c) {
  currentCase = c;
  go("case");
  $("#title").textContent = c.name;
  $("#case-emoji").textContent = c.emoji;
  $("#case-btn").textContent = "Открыть за " + stars(c.price);
  $("#case-result").textContent = "";
  const box = $("#case-prizes");
  box.innerHTML = "";
  c.prizes.slice().sort((a, b) => b.amount - a.amount).forEach((p) => {
    const d = document.createElement("div");
    const e = document.createElement("span");
    e.className = "em";
    e.textContent = p.emoji;
    const sm = document.createElement("small");
    sm.textContent = (p.chance < 0.1 ? p.chance.toFixed(3) : p.chance.toFixed(2)) + "%";
    if (p.kind === "nft") {
      d.className = "nft";
      const ttl = document.createElement("span");
      ttl.className = "ttl";
      ttl.textContent = p.title;
      const model = document.createElement("small");
      model.textContent = `модель «${p.model || "—"}»` + (p.rarity != null ? ` · ${p.rarity}%` : "");
      d.append(e, ttl, model, document.createTextNode("≈ " + stars(p.amount)), sm);
    } else {
      d.append(e, document.createTextNode(stars(p.amount)), sm);
    }
    box.append(d);
  });
  const items = [];
  for (let i = 0; i < 12; i++) items.push(prizeItem(pickPrize(c), c.price));
  roll("#case-roller", items, 5, 0);
}

async function openCase() {
  if (!currentCase) return;
  await guard(async () => {
    if (state.me && state.me.balance < currentCase.price) throw new Error("Недостаточно звёзд на балансе");
    $("#case-btn").disabled = true;
    try {
      const r = await api("/api/case", { case: currentCase.id }, { deferBalance: true });
      showBetTaken(currentCase.price);
      haptic();
      const won = r.kind === "nft"
        ? { kind: "nft", emoji: r.gift, amount: r.prize }
        : { kind: "gift", emoji: r.gift, amount: r.prize };
      const items = [];
      for (let i = 0; i < 60; i++) items.push(prizeItem(i === 50 ? won : pickPrize(currentCase), currentCase.price));
      $("#case-result").className = "result";
      $("#case-result").textContent = "Открываем…";
      await roll("#case-roller", items, 50, 5200);
      setBalance(r.balance);
      const res = $("#case-result");
      const good = r.prize >= currentCase.price;
      res.className = "result reveal " + (good ? "win" : "lose");
      res.textContent = r.kind === "nft"
        ? `NFT ${r.nft.title}! Передаём вам в Telegram`
        : `${r.gift} ${stars(r.prize)}`;
      haptic(good ? "win" : "lose");
      celebrate(currentCase.price, r.prize, $("#case-roller"));
      if (r.kind === "nft") {
        await loadMe().catch(() => {});
        renderCases();
      }
    } finally {
      $("#case-btn").disabled = false;
    }
  });
}

// ---------- PvP: рулетка и хоккей ----------

const pvp = { game: null, timer: 0, lastSeen: {}, animating: false, colors: {}, round: null, holdUntil: 0 };
const PVP_RESULT_HOLD = 7000;  // сколько показывать итог раунда, пока новый раунд пустой

function pvpColorFor(id) {
  if (!pvp.colors[id]) pvp.colors[id] = PVP_COLORS[Object.keys(pvp.colors).length % PVP_COLORS.length];
  return pvp.colors[id];
}

function pvpEl(game, part) {
  return $(`#${game === "hockey" ? "hockey" : "pvp"}-${part}`);
}

function pvpRenderPlayers(game, round) {
  const box = pvpEl(game, "players");
  box.innerHTML = "";
  const bar = game === "roulette" ? $("#pvp-bar") : null;
  if (bar) bar.innerHTML = "";
  const players = round ? round.players : [];
  if (!players.length) {
    box.innerHTML = '<div class="note">Пока никого. Сделайте первую ставку!</div>';
    return;
  }
  players.forEach((p) => {
    const colors = pvpColorFor(p.id);
    if (bar) {
      const seg = document.createElement("span");
      seg.style.width = p.chance + "%";
      seg.style.background = colors[0];
      bar.append(seg);
    }
    const row = document.createElement("div");
    row.className = "pl" + (state.me && p.id === state.me.user.id ? " me" : "");
    const nm = document.createElement("span");
    nm.className = "nm";
    nm.textContent = p.name;
    const am = document.createElement("span");
    am.className = "am";
    am.textContent = stars(p.amount);
    const ch = document.createElement("span");
    ch.className = "ch";
    ch.textContent = p.chance + "%";
    row.append(avatarEl(p, "av", colors), nm, am, ch);
    box.append(row);
  });
}

async function pvpAnimateRoulette(last) {
  const pick = () => {
    let r = Math.random() * 100;
    for (const p of last.players) { r -= p.chance; if (r <= 0) return p; }
    return last.players[0];
  };
  const items = [];
  for (let i = 0; i < 60; i++) {
    const p = i === 50 ? last.winner : pick();
    const colors = pvpColorFor(p.id);
    items.push({ player: p, colors, sub: p.name, bg: colors[0], fg: colors[1] });
  }
  await roll("#pvp-roller", items, 50, 5500);
}

function pvpShowResult(game, last) {
  const mine = state.me && last.winner.id === state.me.user.id;
  const res = pvpEl(game, "result");
  res.className = "result reveal " + (mine ? "win" : "");
  res.textContent = mine ? `Вы забрали ${stars(last.payout)}!` : `${last.winner.name} забирает ${stars(last.payout)}`;
  if (mine) {
    const myBet = (last.players.find((p) => p.id === last.winner.id) || {}).amount || last.payout;
    celebrate(myBet, last.payout, res);
    haptic("win");
  }
  loadMe().catch(() => {});
}

async function pvpRefresh() {
  const game = pvp.game;
  if (!game) return;
  try {
    const s = await api(`/api/pvp?game=${game}`);
    if (pvp.game !== game) return;
    document.querySelectorAll(".pvp-fee").forEach((el) => { el.textContent = Math.round(s.commission * 100); });
    const round = s.round;
    pvp.round = round;
    const seen = pvp.lastSeen[game];
    if (s.last && seen !== undefined && s.last.id !== seen && s.last.finished_ago < 15 && !pvp.animating) {
      pvp.lastSeen[game] = s.last.id;
      pvp.animating = true;
      pvpEl(game, "result").className = "result";
      pvpEl(game, "result").textContent = game === "hockey" ? "Удар!" : "Крутим…";
      try {
        if (game === "hockey") await hockeyAnimate(s.last);
        else await pvpAnimateRoulette(s.last);
        pvpShowResult(game, s.last);
        pvp.holdUntil = performance.now() + PVP_RESULT_HOLD;
      } finally {
        pvp.animating = false;
      }
    } else if (seen === undefined) {
      pvp.lastSeen[game] = s.last ? s.last.id : 0;
    }
    pvpEl(game, "pot").textContent = fmt(round ? round.pot : 0);
    const timer = pvpEl(game, "timer");
    if (!round || !round.players.length) timer.textContent = "Ждём игроков…";
    else if (round.ends_in === null) timer.textContent = "Ждём второго…";
    else timer.textContent = `${Math.ceil(round.ends_in)} с`;
    const holding = performance.now() < pvp.holdUntil && (!round || !round.players.length);
    if (!pvp.animating && !holding) {
      pvpRenderPlayers(game, round);
      if (game === "hockey") hockeyDrawIdle(round);
    }
  } catch (e) { /* повторим */ }
}

function pvpEnter(game) {
  pvpLeave();
  pvp.game = game;
  pvp.holdUntil = 0;
  pvpEl(game, "result").textContent = "";
  if (game === "hockey") hockeyDrawIdle(null);
  pvpRefresh();
  pvp.timer = setInterval(pvpRefresh, 1000);
}

function pvpLeave() {
  clearInterval(pvp.timer);
  pvp.timer = 0;
  pvp.game = null;
}

async function pvpBet(game) {
  await guard(async () => {
    const amount = getBet(game === "hockey" ? "hockey" : "pvp");
    const s = await api("/api/pvp/bet", { amount, game });
    pvp.holdUntil = 0;
    loadMe().catch(() => {});
    pvpRenderPlayers(game, s.round);
    if (game === "hockey") hockeyDrawIdle(s.round);
    toast("Ставка принята");
    fxBurstAt(pvpEl(game, "btn"), { count: 16, speed: 4 });
    haptic();
  });
}

// ---------- хоккей: поле и шайба ----------

const FIELD = [100, 160];

function hockeyCtx() {
  const c = $("#hockey-canvas");
  const dpr = window.devicePixelRatio || 1;
  const w = Math.round(c.clientWidth * dpr);
  if (c.width !== w) { c.width = w; c.height = Math.round(w * FIELD[1] / FIELD[0]); }
  const ctx = c.getContext("2d");
  const k = c.width / FIELD[0];
  return { c, ctx, k, dpr };
}

// Зоны игроков из текущего раунда: доли по ставкам (как на сервере)
function zonesFromPlayers(players) {
  const total = players.reduce((a, p) => a + p.amount, 0) || 1;
  let y = 0;
  return players.map((p, i) => {
    const y1 = i === players.length - 1 ? FIELD[1] : y + FIELD[1] * p.amount / total;
    const z = [y, y1];
    y = y1;
    return z;
  });
}

function hockeyDrawField(players, zones, highlight) {
  const { ctx, k, c } = hockeyCtx();
  ctx.clearRect(0, 0, c.width, c.height);
  ctx.fillStyle = "#0E0C16";
  ctx.fillRect(0, 0, c.width, c.height);
  (players || []).forEach((p, i) => {
    const [y0, y1] = zones[i];
    const [bg] = pvpColorFor(p.id);
    ctx.globalAlpha = highlight === undefined ? 0.22 : highlight === i ? 0.5 : 0.08;
    ctx.fillStyle = bg;
    ctx.fillRect(0, y0 * k, c.width, (y1 - y0) * k);
    ctx.globalAlpha = 1;
    if (i > 0) {
      ctx.strokeStyle = "rgba(237,233,254,.35)";
      ctx.setLineDash([6 * k / 4, 6 * k / 4]);
      ctx.lineWidth = k * 0.4;
      ctx.beginPath(); ctx.moveTo(0, y0 * k); ctx.lineTo(c.width, y0 * k); ctx.stroke();
      ctx.setLineDash([]);
    }
    // аватар и имя в зоне
    const h = (y1 - y0) * k;
    if (h > 14 * k / 2) {
      const r = Math.min(7 * k, h * 0.3);
      const cx = 12 * k, cy = (y0 + y1) / 2 * k;
      const img = avatarImage(p.id);
      ctx.save();
      ctx.beginPath(); ctx.arc(cx, cy, r, 0, 7); ctx.closePath();
      ctx.fillStyle = bg; ctx.fill();
      if (img.ok) { ctx.clip(); ctx.drawImage(img, cx - r, cy - r, r * 2, r * 2); }
      else {
        ctx.fillStyle = pvpColorFor(p.id)[1];
        ctx.font = `800 ${r * 0.8}px Manrope, sans-serif`;
        ctx.textAlign = "center"; ctx.textBaseline = "middle";
        ctx.fillText(initials(p.name), cx, cy);
      }
      ctx.restore();
      ctx.fillStyle = "#FFFFFF";
      ctx.font = `800 ${Math.min(4.2 * k, h * 0.22)}px Manrope, sans-serif`;
      ctx.textAlign = "left"; ctx.textBaseline = "middle";
      ctx.fillText(`${p.name} · ${p.chance}%`, cx + r + 3 * k, cy);
    }
  });
  // разметка площадки
  ctx.strokeStyle = "rgba(139,92,246,.55)";
  ctx.lineWidth = k * 0.6;
  ctx.beginPath(); ctx.arc(50 * k, 80 * k, 12 * k, 0, 7); ctx.stroke();
  ctx.beginPath(); ctx.moveTo(0, 80 * k); ctx.lineTo(c.width, 80 * k); ctx.stroke();
  ctx.strokeStyle = "rgba(237,233,254,.25)";
  ctx.strokeRect(k, k, c.width - 2 * k, c.height - 2 * k);
}

function hockeyPuck(x, y, trail) {
  const { ctx, k } = hockeyCtx();
  (trail || []).forEach((p, i) => {
    ctx.globalAlpha = (i + 1) / trail.length * 0.5;
    ctx.fillStyle = COLOR.soft;
    ctx.beginPath(); ctx.arc(p[0] * k, p[1] * k, 2.2 * k * (i + 1) / trail.length, 0, 7); ctx.fill();
  });
  ctx.globalAlpha = 1;
  ctx.shadowColor = COLOR.accent;
  ctx.shadowBlur = 12 * k / 3;
  ctx.fillStyle = COLOR.white;
  ctx.beginPath(); ctx.arc(x * k, y * k, 3 * k, 0, 7); ctx.fill();
  ctx.shadowBlur = 0;
  ctx.strokeStyle = COLOR.accent;
  ctx.lineWidth = k * 0.7;
  ctx.beginPath(); ctx.arc(x * k, y * k, 2 * k, 0, 7); ctx.stroke();
}

function hockeyDrawIdle(round) {
  if (pvp.animating) return;
  const players = round ? round.players : [];
  hockeyDrawField(players, zonesFromPlayers(players));
  hockeyPuck(50, 80);
}

// Шайба едет по траектории с сервера с равномерным торможением
async function hockeyAnimate(last) {
  const d = last.detail;
  const players = last.players;
  const zones = d.zones;
  const pts = d.points;
  const seg = [];
  let total = 0;
  for (let i = 1; i < pts.length; i++) {
    const len = Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]);
    seg.push([total, len]);
    total += len;
  }
  const duration = 5200;
  const start = performance.now();
  const trail = [];
  let lastSeg = 0;
  const canvasEl = $("#hockey-canvas");
  await new Promise((resolve) => {
    const frame = (now) => {
      const t = Math.min(1, (now - start) / duration);
      const dist = total * (1 - Math.pow(1 - t, 2.2));
      let i = seg.findIndex(([s0, len]) => dist <= s0 + len);
      if (i < 0) i = seg.length - 1;
      const [s0, len] = seg[i];
      const f = len ? (dist - s0) / len : 1;
      const x = pts[i][0] + (pts[i + 1][0] - pts[i][0]) * f;
      const y = pts[i][1] + (pts[i + 1][1] - pts[i][1]) * f;
      if (i !== lastSeg) {  // отскок от борта
        lastSeg = i;
        haptic();
        const r = canvasEl.getBoundingClientRect();
        fxBurst(r.left + pts[i][0] / FIELD[0] * r.width, r.top + pts[i][1] / FIELD[1] * r.height,
          { count: 10, speed: 3, gravity: 0 });
      }
      trail.push([x, y]);
      if (trail.length > 14) trail.shift();
      hockeyDrawField(players, zones);
      hockeyPuck(x, y, trail);
      t < 1 ? requestAnimationFrame(frame) : resolve();
    };
    requestAnimationFrame(frame);
  });
  const winnerIdx = players.findIndex((p) => p.id === last.winner.id);
  const [ex, ey] = pts[pts.length - 1];
  for (let i = 0; i < 3; i++) {  // зона победителя мигает
    hockeyDrawField(players, zones, winnerIdx);
    hockeyPuck(ex, ey);
    await sleep(220);
    hockeyDrawField(players, zones);
    hockeyPuck(ex, ey);
    await sleep(160);
  }
  hockeyDrawField(players, zones, winnerIdx);
  hockeyPuck(ex, ey);
  const r = canvasEl.getBoundingClientRect();
  fxBurst(r.left + ex / FIELD[0] * r.width, r.top + ey / FIELD[1] * r.height, { count: 50, speed: 6 });
  haptic("heavy");
}

// ---------- запуск ----------

function bind() {
  $$("[data-go]").forEach((b) => b.addEventListener("click", () => { haptic(); go(b.dataset.go); }));
  $("#back").addEventListener("click", goBack);
  $("#brand").addEventListener("click", () => go("home"));
  $("#balance-btn").addEventListener("click", () => go("wallet"));
  $$("#wallet-tabs button").forEach((b) => b.addEventListener("click", () => { walletTab(b.dataset.tab); haptic(); }));
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
  $$("#mines-seg button").forEach((b) => b.addEventListener("click", () => {
    minesCount = parseInt(b.dataset.m, 10);
    $$("#mines-seg button").forEach((x) => x.classList.toggle("sel", x === b));
    minesPreview();
    haptic();
  }));
  $("#mines-btn").addEventListener("click", minesAction);
  $("#crash-btn").addEventListener("click", crashAction);
  $("#case-btn").addEventListener("click", openCase);
  $("#pvp-btn").addEventListener("click", () => pvpBet("roulette"));
  $("#hockey-btn").addEventListener("click", () => pvpBet("hockey"));
  $("#bigwin-ok").addEventListener("click", () => $("#bigwin").classList.add("hidden"));
  if (tg && tg.BackButton) tg.BackButton.onClick(goBack);
}

async function init() {
  if (tg) {
    tg.ready();
    tg.expand();
    try { tg.setHeaderColor("#0B0A10"); tg.setBackgroundColor("#0B0A10"); } catch (e) { /* старые клиенты */ }
  }
  betBoxes();
  bind();
  diceUpdate(false);
  minesRender(null);
  if (!tg || !tg.initData) {
    toast("Откройте Svag Gifts через кнопку в Telegram-боте", true);
    return;
  }
  await homeEnter();
  diceUpdate(false);
  minesPreview();
  renderPaytable();
  renderPresets();
}

init();
