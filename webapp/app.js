"use strict";

const tg = window.Telegram ? window.Telegram.WebApp : null;
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const STAR = "★";
const state = { me: null, config: null, screen: "home", tab: "home", busy: false, refLink: null,
  cur: "stars", bal: { stars: 0, ton: 0 } };
const GAME_NAMES = { slots: "Слоты", plinko: "Plinko", mines: "Мины", crash: "Краш", case: "Кейс",
  pvp: "PvP-рулетка", hockey: "PvP-хоккей", upgrade: "Апгрейд" };
const TABS = ["home", "games", "ref", "profile"];   // страницы нижнего меню
const TITLES = { games: "Игры", ref: "Друзья", profile: "Профиль", wallet: "Кошелёк", slots: "Слоты", crash: "Краш", mines: "Мины", plinko: "Plinko",
  cases: "Кейсы", case: "Кейс", pvp: "PvP-рулетка", hockey: "PvP-хоккей", upgrade: "Апгрейд NFT" };
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

// ---------- валюты: звёзды и TON (TON приходит с сервера в nanoTON) ----------

const NANO = 1e9;
const tonNum = (nano) => {
  const v = nano / NANO;
  const digits = Math.abs(v) >= 100 ? 1 : Math.abs(v) >= 1 ? 2 : 4;
  return Number(v.toFixed(digits)).toLocaleString("ru-RU", { maximumFractionDigits: digits });
};
function money(n, cur) {
  return (cur || state.cur) === "ton" ? `${tonNum(n)} TON` : stars(n);
}
const moneyNum = (n, cur) => ((cur || state.cur) === "ton" ? tonNum(n) : fmt(n));
const tonRate = () => (state.config && state.config.ton && state.config.ton.rate) || null;
// цена в звёздах → текущая валюта (кейсы и NFT считаются в звёздах)
function fromStars(n, cur, up) {
  if ((cur || state.cur) !== "ton") return n;
  const r = tonRate();
  if (!r) return null;
  const v = n / r * NANO;
  return up ? Math.ceil(v) : Math.floor(v);
}
// цена NFT: звёзды и TON рядом
function nftPrice(starsAmount) {
  const r = tonRate();
  return r ? `${stars(starsAmount)} · ${tonNum(starsAmount / r * NANO)} TON` : stars(starsAmount);
}
const curBalance = () => state.bal[state.cur] || 0;

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
  if (typeof data.balance === "number" && !(opts && opts.deferBalance)) setBalance(data.balance, data.cur);
  return data;
}

function renderBalance() {
  $("#balance").textContent = moneyNum(curBalance());
  $("#cur-icon").textContent = state.cur === "ton" ? "💎" : "★";
}

function setBalance(value, cur) {
  cur = cur || "stars";
  state.bal[cur] = value;
  if (state.me) state.me.balance = state.bal.stars;
  if (cur !== state.cur) return;
  renderBalance();
  const el = $("#balance");
  el.parentElement.classList.remove("bump");
  void el.offsetWidth;
  el.parentElement.classList.add("bump");
}

// Ставка списывается на экране сразу, выигрыш добавляется после анимации
function showBetTaken(bet) {
  $("#balance").textContent = moneyNum(Math.max(0, curBalance() - bet));
}

// Переключение валюты: ★ ↔ TON
function setCurrency(cur) {
  state.cur = cur === "ton" ? "ton" : "stars";
  store("cur", state.cur);
  document.body.classList.toggle("cur-ton", state.cur === "ton");
  renderBalance();
  refreshBetBoxes();
  renderPresets();
  const enter = { pvp: () => pvpEnter("roulette"), hockey: () => pvpEnter("hockey"), cases: renderCases,
    case: () => currentCase && caseSetCount(caseCount), mines: () => minesRender(minesGame),
    wallet: () => walletTab($("#wallet-tabs button.sel") ? $("#wallet-tabs button.sel").dataset.tab : "dep"),
    profile: profileEnter, home: () => renderHistory(state.me && state.me.history), ref: refEnter };
  if (enter[state.screen]) enter[state.screen]();
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
  $("#bigwin-sum").textContent = "+" + money(win);
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
  const isTab = TABS.includes(screen);
  if (isTab) state.tab = screen;
  $$(".screen").forEach((s) => s.classList.toggle("active", s.id === screen));
  const isHome = screen === "home";
  $("#brand").classList.toggle("hidden", !isHome);
  $("#title").classList.toggle("hidden", isHome);
  $("#title").textContent = TITLES[screen] || "";
  $("#back").classList.toggle("hidden", isTab || !!(tg && tg.BackButton));
  if (tg && tg.BackButton) isTab ? tg.BackButton.hide() : tg.BackButton.show();
  $("#tabbar").classList.toggle("hidden", !isTab);
  document.body.classList.toggle("has-tabbar", isTab);
  $$("#tabbar button").forEach((b) => b.classList.toggle("sel", b.dataset.tab === state.tab));
  window.scrollTo(0, 0);
  const enter = { home: homeEnter, crash: crashEnter, mines: minesEnter, cases: renderCases, wallet: walletEnter,
    pvp: () => pvpEnter("roulette"), hockey: () => pvpEnter("hockey"), ref: refEnter, profile: profileEnter,
    upgrade: upgradeEnter, plinko: plinkoEnter };
  if (enter[screen]) enter[screen]();
}

function goBack() {
  if (!$("#sheet").classList.contains("hidden")) { $("#sheet").classList.add("hidden"); return; }
  go(state.screen === "case" ? "cases" : (state.tab || "home"));
}

// ---------- друзья (реферальная программа) ----------

async function refEnter() {
  try {
    const r = await api("/api/referrals");
    state.refLink = r.link;
    const rate = Math.round(r.rate * 100);
    $("#ref-rate").textContent = rate;
    $$(".ref-rate").forEach((el) => { el.textContent = rate; });
    $("#ref-link").textContent = r.link || "Ссылка появится, когда бот будет на связи";
    $("#ref-count").textContent = fmt(r.count);
    $("#ref-earned").textContent = stars(r.earned) + (r.earned_ton ? ` + ${money(r.earned_ton, "ton")}` : "");
    const box = $("#ref-list");
    box.innerHTML = "";
    if (!r.list.length) box.innerHTML = '<div class="note">Пока никого — отправьте ссылку другу!</div>';
    r.list.forEach((p) => {
      const row = document.createElement("div");
      row.className = "pl";
      const nm = document.createElement("span");
      nm.className = "nm";
      nm.textContent = p.name;
      const am = document.createElement("span");
      am.className = "am";
      am.textContent = "+" + stars(p.earned) + (p.earned_ton ? ` + ${money(p.earned_ton, "ton")}` : "");
      row.append(avatarEl(p, "av", PVP_COLORS[p.id % PVP_COLORS.length]), nm, am);
      box.append(row);
    });
  } catch (e) { toast(e.message, true); }
}

async function refCopy() {
  if (!state.refLink) return;
  try {
    await navigator.clipboard.writeText(state.refLink);
    toast("Ссылка скопирована");
    haptic();
  } catch (e) {
    toast("Не удалось скопировать — зажмите ссылку", true);
  }
}

function refShare() {
  if (!state.refLink) return;
  const text = "Залетай в Svag Gifts — слоты, краш, PvP и NFT-подарки 🎁";
  const url = `https://t.me/share/url?url=${encodeURIComponent(state.refLink)}&text=${encodeURIComponent(text)}`;
  if (tg && tg.openTelegramLink) tg.openTelegramLink(url);
  else window.open(url, "_blank");
}

// ---------- профиль ----------

async function profileEnter() {
  loadVip();
  try {
    const [p, me] = await Promise.all([api("/api/profile"), loadMe()]);
    const av = $("#prof-av");
    av.innerHTML = "";
    av.append(avatarEl({ id: p.id, name: p.name }, "av", PVP_COLORS[0]));
    $("#prof-name").textContent = p.name;
    const since = p.joined ? new Date(p.joined * 1000).toLocaleDateString("ru-RU") : "";
    $("#prof-sub").textContent = [p.username ? "@" + p.username : null, `ID ${p.id}`, since ? `с ${since}` : null]
      .filter(Boolean).join(" · ");
    const t = state.cur === "ton" ? p.ton : p;
    $("#prof-balance").textContent = money(t.balance);
    $("#prof-games").textContent = fmt(p.games);
    $("#prof-wins").textContent = fmt(p.wins);
    $("#prof-wagered").textContent = money(t.wagered);
    $("#prof-won").textContent = money(t.won);
    $("#prof-deposited").textContent = money(t.deposited);
    $("#prof-withdrawn").textContent = money(t.withdrawn);
    const best = $("#prof-best");
    best.classList.toggle("hidden", !p.best);
    if (p.best) {
      best.innerHTML = "";
      const l = document.createElement("div");
      l.innerHTML = "<small>Лучший выигрыш</small>";
      const g = document.createElement("span");
      g.textContent = `${GAME_NAMES[p.best.game] || p.best.game} · ставка ${stars(p.best.bet)}`;
      l.append(g);
      const b = document.createElement("b");
      b.textContent = "+" + stars(p.best.win);
      best.append(l, b);
    }
    const w = $("#prof-wager");
    w.classList.toggle("hidden", !t.wager.left);
    w.textContent = t.wager.left ? `Бонусы и чеки нужно отыграть: осталось поставить ${money(t.wager.left)}.` : "";
  } catch (e) { toast(e.message, true); }
}

// ---------- ставка ----------

// Лимиты ставки в единицах, которые видит игрок: звёзды или TON
function betLimits() {
  const c = state.config || { min_bet: 1, max_bet: 10000 };
  if (state.cur === "ton") {
    const t = c.ton || { min_bet: 1e7, max_bet: 1e11 };
    return { min: t.min_bet / NANO, max: t.max_bet / NANO, step: 0.01, def: "0.1" };
  }
  return { min: c.min_bet, max: c.max_bet, step: 1, def: "10" };
}

const betKey = (box) => `bet:${box.dataset.bet}${state.cur === "ton" ? ":ton" : ""}`;

function refreshBetBoxes() {
  $$(".betbox").forEach((box) => {
    const input = box.querySelector("input");
    if (!input) return;
    const lim = betLimits();
    input.step = lim.step;
    input.inputMode = state.cur === "ton" ? "decimal" : "numeric";
    input.value = store(betKey(box)) || lim.def;
  });
  $$(".cur-label").forEach((el) => { el.textContent = state.cur === "ton" ? "TON" : "★"; });
}

function betBoxes() {
  $$(".betbox").forEach((box) => {
    const input = document.createElement("input");
    input.type = "number";
    input.addEventListener("change", () => store(betKey(box), input.value));
    const round = (v) => (state.cur === "ton" ? Math.round(v * 100) / 100 : Math.floor(v));
    const clamp = (v) => { const l = betLimits(); return round(Math.max(l.min, Math.min(l.max, v))); };
    const mk = (label, fn) => {
      const b = document.createElement("button");
      b.textContent = label;
      b.addEventListener("click", () => {
        input.value = fn(parseFloat(input.value) || 0);
        store(betKey(box), input.value);
        haptic();
      });
      return b;
    };
    const balUnits = () => (state.cur === "ton" ? Math.floor(curBalance() / NANO * 100) / 100 : curBalance());
    box.append(mk("½", (v) => clamp(v / 2)), input, mk("×2", (v) => clamp(v * 2)), mk("MAX", () => clamp(balUnits())));
  });
  refreshBetBoxes();
}

// Ставка в единицах сервера: звёзды или nanoTON
function getBet(game) {
  const raw = parseFloat($(`.betbox[data-bet="${game}"] input`).value);
  if (!(raw > 0)) throw new Error("Введите ставку");
  const v = state.cur === "ton" ? Math.round(raw * NANO) : Math.floor(raw);
  if (v <= 0) throw new Error("Введите ставку");
  if (state.me && v > curBalance()) throw new Error(state.cur === "ton" ? "Недостаточно TON на балансе" : "Недостаточно звёзд на балансе");
  return v;
}

// ---------- главная ----------

async function loadMe() {
  const me = await api("/api/me");
  state.me = me;
  state.config = me.config;
  // картинки NFT из кейсов начинают качаться сразу после входа
  setTimeout(() => nftPreload((me.config.cases || []).flatMap((c) => c.prizes.filter((p) => p.kind === "nft").map(caseNft))), 800);
  state.bal = { stars: me.balance, ton: me.ton || 0 };
  renderBalance();
  $("#hello").textContent = me.user.name;
  renderHistory(me.history);
  return me;
}

async function homeEnter() {
  try { await loadMe(); } catch (e) { toast(e.message, true); }
  loadTicker();
  loadVip();
  loadLeaders();
}

// ---------- VIP-уровень, рейкбек, ежедневный бонус ----------

const vipState = { data: null, timer: 0 };
const pctText = (x) => `${Number((x * 100).toFixed(2))}%`;
const hms = (s) => {
  s = Math.max(0, Math.ceil(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
};

function renderBonus() {
  const v = vipState.data;
  if (!v) return;
  const b = v.bonus;
  const card = $("#bonus-card");
  const btn = $("#bonus-btn");
  card.classList.toggle("ready", b.allowed && b.ready);
  btn.disabled = !b.allowed || !b.ready;
  if (!b.allowed) {
    $("#bonus-sub").textContent = "Откроется после пополнения от 50 ★ или 0.5 TON";
    btn.textContent = "🔒";
  } else if (b.ready) {
    $("#bonus-sub").textContent = "До 100 ★ — забирайте каждый день";
    btn.textContent = "Забрать";
  } else {
    const left = b.in - (performance.now() - vipState.at) / 1000;
    $("#bonus-sub").textContent = `Следующий через ${hms(left)}`;
    btn.textContent = "Ждём";
    if (left <= 0) { b.ready = true; renderBonus(); }
  }
}

function renderVip() {
  const v = vipState.data;
  if (!v || !$("#vip-card")) return;
  $("#vip-em").textContent = v.emoji;
  $("#vip-name").textContent = `Уровень: ${v.name}`;
  $("#vip-rb").textContent = `рейкбек ${pctText(v.rakeback)} с каждой ставки`;
  const from = v.levels[v.level].at;
  $("#vip-fill").style.width = v.next ? `${Math.min(100, (v.points - from) / (v.next.at - from) * 100)}%` : "100%";
  $("#vip-next").textContent = v.next
    ? `До «${v.next.name}» (${pctText(v.next.rakeback)}) — ещё ${fmt(v.next.at - v.points)} ★ ставок`
    : "Максимальный уровень";
  const rake = [v.rake.stars ? stars(v.rake.stars) : "", v.rake.ton >= 1e7 ? money(v.rake.ton, "ton") : ""].filter(Boolean).join(" + ");
  const btn = $("#rake-btn");
  btn.textContent = rake ? `Забрать ${rake}` : "Копится…";
  btn.disabled = !rake;
  const box = $("#vip-levels");
  box.innerHTML = "";
  v.levels.forEach((lv, i) => {
    const d = document.createElement("div");
    d.className = i === v.level ? "cur" : "";
    d.innerHTML = `<span>${lv.emoji}</span>${pctText(lv.rakeback)}`;
    d.title = `${lv.name}: от ${fmt(lv.at)} ★ ставок`;
    box.append(d);
  });
}

async function loadVip() {
  try {
    vipState.data = await api("/api/vip");
    vipState.at = performance.now();
    renderBonus();
    renderVip();
    clearInterval(vipState.timer);
    vipState.timer = setInterval(() => { if (state.screen === "home") renderBonus(); }, 1000);
  } catch (e) { /* не критично */ }
}

async function claimBonus() {
  await guard(async () => {
    const ic = $("#bonus-ic");
    ic.classList.add("spin");
    const r = await api("/api/bonus", {}, { deferBalance: true });
    // короткая «рулетка» суммы
    for (let i = 0; i < 14; i++) {
      ic.textContent = String(r.table[Math.floor(Math.random() * r.table.length)]);
      await sleep(60 + i * 12);
    }
    ic.classList.remove("spin");
    ic.textContent = `+${r.amount}`;
    setBalance(r.balance, "stars");
    toast(`Ежедневный бонус: +${stars(r.amount)}`);
    haptic("win");
    fxBurstAt($("#bonus-card"), { count: 40, speed: 5 });
    await loadVip();
    setTimeout(() => { ic.textContent = "🎁"; }, 2500);
  });
}

async function claimRakeback() {
  await guard(async () => {
    const r = await api("/api/vip/rakeback", {});
    if (r.ton) state.bal.ton += r.ton;
    toast(`Рейкбек: +${[r.stars ? stars(r.stars) : "", r.ton ? money(r.ton, "ton") : ""].filter(Boolean).join(" + ")}`);
    haptic("win");
    fxBurstAt($("#rake-btn"), { count: 30 });
    await loadVip();
    renderBalance();
  });
}

async function loadLeaders() {
  try {
    const lb = await api("/api/leaders");
    const box = $("#leaders");
    box.innerHTML = "";
    if (!lb.top.length) box.innerHTML = '<div class="note">На этой неделе ещё никто не играл — станьте первым!</div>';
    lb.top.slice(0, 7).forEach((p, i) => {
      const row = document.createElement("div");
      row.className = "pl" + (state.me && p.id === state.me.user.id ? " me" : "");
      const place = document.createElement("span");
      place.className = "place";
      place.textContent = i < 3 ? ["🥇", "🥈", "🥉"][i] : String(i + 1);
      const nm = document.createElement("span");
      nm.className = "nm";
      nm.textContent = p.name;
      const am = document.createElement("span");
      am.className = "am";
      am.textContent = stars(p.points);
      row.append(place, avatarEl(p, "av", PVP_COLORS[p.id % PVP_COLORS.length]), nm, am);
      box.append(row);
    });
    $("#lb-me").textContent = lb.me ? `вы #${lb.me.place}` : "";
  } catch (e) { /* не критично */ }
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
    box.append(historyRow(`${GAME_NAMES[h.game] || h.game} · ${money(h.bet, h.cur)}`,
      (diff >= 0 ? "+" : "") + money(diff, h.cur), diff >= 0));
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
      sum.textContent = "+" + money(w.win, w.cur);
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
  $("#tab-nft").classList.toggle("hidden", tab !== "nft");
  if (tab === "dep" && state.cur === "ton") loadTonDeposit();
  if (tab === "out" && state.cur === "ton") loadTonWithdraw();
  if (tab === "out" && state.cur !== "ton") loadWithdraw();
  if (tab === "nft") loadMyGifts();
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

// ---------- TON: пополнение переводом с комментарием и вывод на кошелёк ----------

const tonState = { info: null };

async function loadTonInfo() {
  tonState.info = await api("/api/ton");
  return tonState.info;
}

async function loadTonDeposit() {
  try {
    const t = await loadTonInfo();
    $("#ton-dep-body").classList.toggle("hidden", !t.wallet);
    $("#ton-dep-off").classList.toggle("hidden", !!t.wallet);
    $("#ton-wallet").textContent = t.wallet || "—";
    $("#ton-comment").textContent = t.comment;
  } catch (e) { toast(e.message, true); }
}

function tonOpenWallet() {
  const t = tonState.info;
  if (!t || !t.wallet) return;
  const link = `https://app.tonkeeper.com/transfer/${t.wallet}?text=${encodeURIComponent(t.comment)}`;
  if (tg && tg.openLink) tg.openLink(link); else window.open(link, "_blank");
  // ждём зачисления: обновляем баланс, пока открыт кошелёк
  let n = 0;
  const before = state.bal.ton;
  const timer = setInterval(async () => {
    if (++n > 40 || state.screen !== "wallet") { clearInterval(timer); return; }
    await loadMe().catch(() => {});
    if (state.bal.ton > before) {
      clearInterval(timer);
      toast(`Зачислено +${money(state.bal.ton - before, "ton")}`);
      fxBurstAt($("#balance-btn"), { count: 50 });
      haptic("win");
    }
  }, 6000);
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast("Скопировано");
    haptic();
  } catch (e) { toast("Не удалось скопировать — зажмите текст", true); }
}

const TON_WD_STATUS = { pending: "на проверке", sent: "отправлено", rejected: "отклонено" };

async function loadTonWithdraw() {
  try {
    const t = await loadTonInfo();
    $("#ton-min").textContent = tonNum(t.min_withdraw);
    const w = $("#ton-wager");
    w.classList.toggle("hidden", !t.wager.left);
    w.textContent = t.wager.left ? `Перед выводом пополнения и бонусы нужно хотя бы раз поставить в играх: осталось ${money(t.wager.left, "ton")}.` : "";
    const hist = $("#ton-history");
    hist.innerHTML = "";
    t.history.forEach((x) => hist.append(historyRow(`№${x.id} · ${money(x.amount, "ton")}`,
      TON_WD_STATUS[x.status] || x.status, x.status === "sent")));
  } catch (e) { toast(e.message, true); }
}

async function tonWithdraw() {
  const address = $("#ton-address").value.trim();
  const amount = Math.round(parseFloat($("#ton-amount").value.replace(",", ".")) * NANO);
  if (!address) return toast("Укажите адрес кошелька", true);
  if (!(amount > 0)) return toast("Укажите сумму", true);
  const ok = await confirmAsk(`Вывести ${money(amount, "ton")} на ${address.slice(0, 6)}…${address.slice(-6)}?`);
  if (!ok) return;
  await guard(async () => {
    await api("/api/ton/withdraw", { amount, address });
    $("#ton-amount").value = "";
    toast("Заявка отправлена — TON придут после проверки");
    haptic("win");
    loadTonWithdraw();
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

// ---------- мои подарки (прислали релейеру) ----------

const myGifts = { relayer: null, list: [], timer: 0 };
const GIFT_STATUS = { staked: "в игре", withdrawing: "выводится" };

// ---------- картинки NFT: превью с нашего сервера (ужатые и закэшированные) ----------

function nftImgUrl(o) {
  if (!o || !o.collection) return null;
  if (o.number) {
    return `/nftimg?c=${encodeURIComponent(o.collection)}&n=${o.number}` + (o.model ? `&m=${encodeURIComponent(o.model)}` : "");
  }
  return `/nftimg?c=${encodeURIComponent(o.collection)}` + (o.model ? `&m=${encodeURIComponent(o.model)}` : "");
}

// url → загруженная картинка (или false, если её нет): второй раз иконка появляется мгновенно
const nftImgCache = new Map();
function nftImgLoad(url) {
  if (!nftImgCache.has(url)) {
    nftImgCache.set(url, new Promise((resolve) => {
      const img = new Image();
      img.decoding = "async";
      img.onload = () => { nftImgCache.set(url, img); resolve(img); };
      img.onerror = () => { nftImgCache.set(url, false); resolve(false); };
      img.src = url;
    }));
  }
  return nftImgCache.get(url);
}

// Прогрев: картинки призов кейсов/целей апгрейда качаются заранее, по несколько за раз
function nftPreload(list) {
  const urls = [...new Set(list.map(nftImgUrl).filter((u) => u && !nftImgCache.has(u)))];
  let i = 0;
  const next = () => { if (i < urls.length) nftImgLoad(urls[i++]).then(next); };
  for (let k = 0; k < 6; k++) next();
}

function nftImgPut(el, img) {
  const c = img.cloneNode();
  c.alt = "";
  el.textContent = "";
  el.append(c);
  el.classList.add("has-img");
}

// Эмодзи — пока картинка грузится (если не загрузится, остаётся эмодзи)
function nftIcon(o, cls) {
  const el = document.createElement("span");
  el.className = (cls || "em") + " nft-ic";
  const url = nftImgUrl(o);
  const hit = url && nftImgCache.get(url);
  if (hit instanceof HTMLImageElement) {
    nftImgPut(el, hit);
    return el;
  }
  el.textContent = o.emoji || "🎁";
  if (url && hit !== false) nftImgLoad(url).then((img) => { if (img) nftImgPut(el, img); });
  return el;
}

function setNftIcon(target, o) {
  target.innerHTML = "";
  target.append(nftIcon(o, "nft-inline"));
}

function giftCard(gift, opts) {
  const el = document.createElement(opts && opts.button ? "button" : "div");
  el.className = "mg";
  const em = nftIcon(gift, "em");
  const info = document.createElement("div");
  const t = document.createElement("div");
  t.className = "t";
  t.textContent = gift.title;
  const sub = document.createElement("div");
  sub.className = "s";
  const parts = [];
  if (gift.model) parts.push(`модель «${gift.model}»` + (gift.rarity ? ` · ${gift.rarity}%` : ""));
  if (gift.demo) parts.push("демо");
  if (GIFT_STATUS[gift.status]) parts.push(GIFT_STATUS[gift.status]);
  else if (!gift.priced) parts.push("цена проверяется");
  sub.textContent = parts.join(" · ");
  info.append(t, sub);
  const v = document.createElement("span");
  v.className = "v";
  v.textContent = gift.value ? nftPrice(gift.value) : "—";
  el.append(em, info, v);
  return el;
}

async function loadMyGifts(quiet) {
  const box = $("#mygifts");
  if (!quiet) box.textContent = "Загружаем…";
  try {
    const data = await api("/api/gifts");
    myGifts.relayer = data.relayer;
    myGifts.list = data.gifts;
    $("#nft-rate").textContent = Math.round(data.sell_rate * 100);
    $("#nft-howto").textContent = data.relayer
      ? `Отправьте NFT или подарок в Telegram аккаунту @${data.relayer} — он появится здесь через несколько секунд.`
      : "Приём подарков временно недоступен.";
    $("#nft-open").classList.toggle("hidden", !data.relayer);
    box.innerHTML = "";
    if (!data.gifts.length) box.textContent = "Пока пусто. Отправьте подарок — и он появится здесь.";
    data.gifts.forEach((gift) => {
      const card = giftCard(gift);
      if (gift.status === "owned") {
        const acts = document.createElement("div");
        acts.className = "acts";
        const sell = document.createElement("button");
        sell.className = "btn";
        sell.textContent = gift.sell ? `Продать за ${stars(gift.sell)}` : "Продать";
        sell.disabled = !gift.sell;
        sell.addEventListener("click", () => sellGift(gift));
        const out = document.createElement("button");
        out.className = "btn small";
        out.textContent = "Вывести";
        out.addEventListener("click", () => withdrawGift(gift));
        acts.append(sell, out);
        card.append(acts);
      }
      box.append(card);
    });
  } catch (e) {
    if (!quiet) box.textContent = e.message;
  }
}

function openRelayer() {
  if (!myGifts.relayer) return;
  const link = `https://t.me/${myGifts.relayer}`;
  if (tg && tg.openTelegramLink) tg.openTelegramLink(link);
  else window.open(link, "_blank");
  // ждём подарок: обновляем список, пока открыт раздел
  clearInterval(myGifts.timer);
  let n = 0;
  myGifts.timer = setInterval(() => {
    if (++n > 30 || $("#tab-nft").classList.contains("hidden") || state.screen !== "wallet") {
      clearInterval(myGifts.timer);
      return;
    }
    loadMyGifts(true);
  }, 5000);
}

async function sellGift(gift) {
  const ok = await confirmAsk(`Продать ${gift.title} казино за ${gift.sell} ★?`);
  if (!ok) return;
  await guard(async () => {
    const r = await api("/api/gifts/sell", { id: gift.id });
    toast(`+${stars(r.amount)}`);
    fxBurstAt($("#balance-btn"), { count: 30 });
    haptic("win");
    loadMyGifts(true);
  });
}

async function withdrawGift(gift) {
  const ok = await confirmAsk(`Вывести ${gift.title} обратно в ваш Telegram?`);
  if (!ok) return;
  await guard(async () => {
    await api("/api/gifts/withdraw", { id: gift.id });
    toast("Подарок отправлен вам в Telegram");
    haptic("win");
    loadMyGifts(true);
  });
}

// Выбор подарков для ставки в PvP
// NFT в краш: при выводе вернутся, прибыль — звёздами; не успели — уходят казино
async function crashGiftSheet() {
  if (state.cur !== "stars") { toast("NFT ставятся на звёзды — переключите валюту на ★", true); return; }
  await giftSheet("Поставить в краш", async (ids) => {
    await guard(async () => {
      const autoRaw = $("#crash-auto").value.trim().replace(",", ".");
      const body = { bet: 0, gifts: ids, cur: "stars" };
      if (autoRaw) body.auto = parseFloat(autoRaw);
      await api("/api/crash/bet", body);
      toast("NFT в ракете! Успейте вывести");
      haptic();
      fxBurstAt($("#crash-btn"), { count: 20, speed: 4 });
      clearTimeout(crash.timer);
      crashPoll();
    });
  });
}

async function pvpGiftSheet(game) {
  await giftSheet("Поставить подарки", (ids) => pvpBet(game, ids));
}

async function giftSheet(title, onPick) {
  await guard(async () => {
    const data = await api("/api/gifts");
    myGifts.relayer = data.relayer;
    $("#sheet .panel-title").textContent = title;
    const usable = data.gifts.filter((g) => g.status === "owned");
    const list = $("#sheet-list");
    list.innerHTML = "";
    const chosen = new Set();
    const ok = $("#sheet-ok");
    const update = () => {
      const sum = usable.filter((g) => chosen.has(g.id)).reduce((a, g) => a + g.value, 0);
      ok.textContent = chosen.size ? `Поставить на ${stars(sum)}` : "Выберите подарки";
      ok.disabled = !chosen.size;
    };
    if (!usable.length) {
      list.innerHTML = "";
      const note = document.createElement("p");
      note.className = "note";
      note.textContent = data.relayer
        ? `У вас нет подарков. Отправьте NFT аккаунту @${data.relayer} — и ставьте его здесь.`
        : "Приём подарков временно недоступен.";
      list.append(note);
    }
    usable.forEach((gift) => {
      const card = giftCard(gift, { button: true });
      if (!gift.priced) card.classList.add("off");
      card.addEventListener("click", () => {
        if (!gift.priced) { toast("Цена подарка ещё проверяется", true); return; }
        if (chosen.has(gift.id)) chosen.delete(gift.id); else chosen.add(gift.id);
        card.classList.toggle("sel", chosen.has(gift.id));
        haptic();
        update();
      });
      list.append(card);
    });
    update();
    ok.onclick = async () => {
      const ids = Array.from(chosen);
      $("#sheet").classList.add("hidden");
      await onPick(ids);
    };
    $("#sheet").classList.remove("hidden");
  });
}

// ---------- апгрейд NFT ----------

const upg = { gifts: [], targets: [], chosen: new Set(), target: null, cfg: null, angle: 0, spinning: false,
  mult: null, query: "" };
const UPG_R = 84;
const UPG_C = 2 * Math.PI * UPG_R;
const upgTargetNft = (t) => ({ collection: t.title, model: t.model, emoji: t.emoji });

function upgStake() {
  return upg.gifts.filter((g) => upg.chosen.has(g.id)).reduce((a, g) => a + g.value, 0);
}

function upgChanceFor(stake, price) {
  if (!upg.cfg || !stake || !price || stake >= price) return 0;
  return Math.min(upg.cfg.max_chance, (1 - upg.cfg.edge) * stake / price);
}

const upgChance = () => upgChanceFor(upgStake(), upg.target && upg.target.price);
const pct = (c) => (c ? (c * 100).toFixed(c < 0.1 ? 2 : 1) : "0") + "%";

// Автоподбор: цель с ценой ближе всего к ставке × X (дороже ставки, шанс не меньше минимального)
function upgPick(mult) {
  const stake = upgStake();
  if (!stake) return null;
  const want = stake * mult;
  const ok = upg.targets.filter((t) => t.price > stake && upgChanceFor(stake, t.price) >= upg.cfg.min_chance);
  if (!ok.length) return null;
  return ok.reduce((best, t) => (Math.abs(Math.log(t.price / want)) < Math.abs(Math.log(best.price / want)) ? t : best));
}

function upgApplyMult() {
  if (!upg.mult) return;
  const t = upgPick(upg.mult);
  upg.target = t;
  if (!t && upgStake()) toast("Под такой множитель целей нет", true);
}

function upgUpdate() {
  const stake = upgStake();
  const chance = upgChance();
  $("#upg-arc").style.strokeDashoffset = UPG_C * (1 - chance);
  $("#upg-chance").textContent = pct(chance);
  $("#upg-x").textContent = stake && upg.target ? "×" + (upg.target.price / stake).toFixed(2) : "—";
  const picked = upg.gifts.filter((g) => upg.chosen.has(g.id));
  const from = $("#upg-from-em");
  from.innerHTML = "";
  if (!picked.length) from.textContent = "🎁";
  picked.slice(0, 3).forEach((g) => from.append(nftIcon(g, "nft-inline")));
  if (picked.length > 3) from.append(Object.assign(document.createElement("span"), { className: "more", textContent: `+${picked.length - 3}` }));
  $("#upg-stake").textContent = stake ? nftPrice(stake) : "выберите NFT";
  if (upg.target) setNftIcon($("#upg-to-em"), upgTargetNft(upg.target));
  else $("#upg-to-em").textContent = "💎";
  $("#upg-target").textContent = upg.target ? `${upg.target.title} «${upg.target.model}» · ${nftPrice(upg.target.price)}` : "выберите цель";
  $("#upg-from").classList.toggle("filled", !!stake);
  $("#upg-to").classList.toggle("filled", !!upg.target);
  $$("#upg-mults button").forEach((b) => b.classList.toggle("sel", Number(b.dataset.x) === upg.mult));
  const btn = $("#upg-btn");
  let hint = "";
  if (!stake) hint = "Выберите свои NFT";
  else if (!upg.target) hint = "Выберите цель или множитель";
  else if (stake >= upg.target.price) hint = "Цель должна быть дороже ставки";
  else if (chance < upg.cfg.min_chance) hint = "Шанс меньше 1%";
  btn.disabled = !!hint || upg.spinning;
  btn.textContent = upg.spinning ? "Крутим…" : hint || `Апгрейд · шанс ${pct(chance)}`;
  $("#upg-all").textContent = upg.gifts.length && upg.gifts.filter((g) => g.priced).every((g) => upg.chosen.has(g.id))
    ? "Снять все" : "Выбрать все";
  $$("#upg-targets .upt").forEach((el) => {
    const t = upg.targets[el.dataset.i];
    el.classList.toggle("sel", upg.target === t);
    el.classList.toggle("off", !!stake && t.price <= stake);
    const x = el.querySelector(".x");
    const c = upgChanceFor(stake, t.price);
    x.textContent = stake && t.price > stake ? `×${(t.price / stake).toFixed(1)} · ${pct(c)}` : "";
  });
}

function upgRenderTargets() {
  const tbox = $("#upg-targets");
  tbox.innerHTML = "";
  const q = upg.query.trim().toLowerCase();
  const list = upg.targets.map((t, i) => [t, i])
    .filter(([t]) => !q || `${t.title} ${t.model}`.toLowerCase().includes(q));
  $("#upg-tcount").textContent = upg.targets.length ? `· ${list.length}` : "";
  if (!upg.targets.length) tbox.innerHTML = '<div class="note">Сейчас нет NFT для апгрейда — загляните позже.</div>';
  else if (!list.length) tbox.innerHTML = '<div class="note">Ничего не найдено</div>';
  list.forEach(([t, i]) => {
    const b = document.createElement("button");
    b.className = "upt";
    b.dataset.i = i;
    const em = document.createElement("span");
    em.className = "em";
    em.append(nftIcon(upgTargetNft(t), "nft-inline big"));
    const ttl = document.createElement("b");
    ttl.textContent = t.title;
    const md = document.createElement("small");
    md.textContent = `«${t.model}»` + (t.rarity != null ? ` · ${t.rarity}%` : "") + (t.demo ? " · демо" : "");
    const pr = document.createElement("span");
    pr.className = "pr";
    pr.textContent = nftPrice(t.price);
    const x = document.createElement("span");
    x.className = "x";
    b.append(x, em, ttl, md, pr);
    b.addEventListener("click", () => {
      if (upg.spinning) return;
      upg.target = upg.target === t ? null : t;
      upg.mult = null;
      haptic();
      upgUpdate();
    });
    tbox.append(b);
  });
}

function upgRender() {
  const gbox = $("#upg-gifts");
  gbox.innerHTML = "";
  if (!upg.gifts.length) {
    const p = document.createElement("p");
    p.className = "note";
    p.textContent = "У вас нет NFT. Отправьте NFT аккаунту казино (Кошелёк → Подарки) — и апгрейдьте его здесь.";
    const b = document.createElement("button");
    b.className = "btn";
    b.textContent = "Как отправить NFT";
    b.addEventListener("click", () => { go("wallet"); walletTab("nft"); });
    gbox.append(p, b);
  }
  upg.gifts.forEach((gift) => {
    const card = giftCard(gift, { button: true });
    if (!gift.priced) card.classList.add("off");
    card.classList.toggle("sel", upg.chosen.has(gift.id));
    card.addEventListener("click", () => {
      if (upg.spinning) return;
      if (!gift.priced) { toast("Цена подарка ещё проверяется", true); return; }
      if (upg.chosen.has(gift.id)) upg.chosen.delete(gift.id); else upg.chosen.add(gift.id);
      card.classList.toggle("sel", upg.chosen.has(gift.id));
      haptic();
      upgApplyMult();
      upgUpdate();
    });
    gbox.append(card);
  });
  upgRenderTargets();
  upgUpdate();
}

function upgSelectAll() {
  if (upg.spinning) return;
  const priced = upg.gifts.filter((g) => g.priced);
  const all = priced.length && priced.every((g) => upg.chosen.has(g.id));
  upg.chosen = all ? new Set() : new Set(priced.map((g) => g.id));
  $$("#upg-gifts .mg").forEach((el, k) => el.classList.toggle("sel", upg.chosen.has(upg.gifts[k].id)));
  haptic();
  upgApplyMult();
  upgUpdate();
}

async function upgradeEnter() {
  $("#upg-arc").style.strokeDasharray = UPG_C;
  $("#upg-result").textContent = "";
  $("#upg-wheel").classList.remove("won", "lost");
  try {
    const data = await api("/api/upgrade");
    upg.cfg = data;
    upg.gifts = data.gifts;
    upg.targets = data.targets;
    nftPreload(upg.targets.map(upgTargetNft));
    const ids = new Set(upg.gifts.map((g) => g.id));
    upg.chosen = new Set([...upg.chosen].filter((id) => ids.has(id)));
    if (upg.target) upg.target = upg.targets.find((t) => t.id === upg.target.id) || null;
    upgRender();
  } catch (e) { toast(e.message, true); }
}

async function upgradeGo() {
  if (!upg.target || !upg.chosen.size) return;
  const chance = upgChance();
  const target = upg.target;
  const ok = await confirmAsk(`Поставить NFT на ${nftPrice(upgStake())} ради ${target.title} «${target.model}» `
    + `(${nftPrice(target.price)})? Шанс ${pct(chance)}. При проигрыше NFT уйдут казино.`);
  if (!ok) return;
  await guard(async () => {
    upg.spinning = true;
    upgUpdate();
    const res = $("#upg-result");
    const wheel = $("#upg-wheel");
    wheel.classList.remove("won", "lost");
    try {
      const r = await api("/api/upgrade", { gifts: [...upg.chosen], target: target.id });
      haptic();
      res.className = "result";
      res.textContent = "";
      // стрелка останавливается на roll: зона выигрыша — дуга [0, шанс) от верха по часовой
      const needle = $("#upg-needle");
      upg.angle += 360 * 6 + ((r.roll * 360 - upg.angle) % 360 + 360) % 360;
      needle.style.transition = "transform 4.6s cubic-bezier(.12,.72,.1,1)";
      needle.style.transform = `rotate(${upg.angle}deg)`;
      await sleep(4700);
      res.className = "result reveal " + (r.won ? "win" : "lose");
      wheel.classList.add(r.won ? "won" : "lost");
      if (r.won) {
        res.textContent = r.nft.demo
          ? `Апгрейд! Демо-NFT ${r.nft.title} «${r.nft.model}» — в профиле → «Мои подарки»`
          : `Апгрейд! ${r.nft.title} «${r.nft.model}» — в профиле → «Мои подарки»`;
        haptic("win");
        fxBurstAt(wheel, { count: 80, speed: 7 });
        $("#bigwin-label").textContent = "UPGRADE";
        $("#bigwin-x").textContent = "×" + (r.target / r.stake).toFixed(2);
        $("#bigwin-sum").textContent = `${r.nft.title} «${r.nft.model}»`;
        $("#bigwin").classList.remove("hidden");
      } else {
        res.textContent = `Мимо: выпало ${(r.roll * 100).toFixed(1)}, нужно было меньше ${pct(r.chance)}`;
        haptic("lose");
      }
      upg.chosen.clear();
      upg.target = null;
    } finally {
      upg.spinning = false;
    }
    const shown = [res.className, res.textContent, [...wheel.classList]];
    await upgradeEnter();
    [res.className, res.textContent] = shown;
    wheel.className = shown[2].join(" ");
  });
}

// ---------- слоты ----------

const REEL_H = 104;

const SLOT_SYMS = ["bar", "grape", "lemon", "seven"];

// Символы как в 🎰 Telegram: BAR, виноград, лимон, семёрка
function symHTML(sym) {
  if (sym === "bar") return '<span class="sym bar">BAR</span>';
  if (sym === "seven") return '<span class="sym seven">7</span>';
  return `<span class="sym">${sym === "grape" ? "🍇" : "🍋"}</span>`;
}

function renderPaytable() {
  const s = state.config && state.config.slots;
  if (!s) return;
  const box = $("#paytable");
  box.innerHTML = "";
  [
    [["seven", "seven", "seven"], `NFT ≈ ×${s["777"]}`],
    [["bar", "bar", "bar"], "×" + s.triple],
    [["grape", "grape", "grape"], "×" + s.triple],
    [["lemon", "lemon", "lemon"], "×" + s.triple],
    [["seven", "seven"], "×" + s.two_sevens, "возврат ставки"],
  ].forEach(([combo, m, note]) => {
    const d = document.createElement("div");
    const c = document.createElement("span");
    c.className = "combo";
    c.innerHTML = combo.map(symHTML).join("") + (note ? `<small>${note}</small>` : "");
    const x = document.createElement("b");
    x.textContent = m;
    d.append(c, x);
    box.append(d);
  });
}

function reelStrip(finalSymbol, count) {
  const items = [];
  for (let i = 0; i < count; i++) items.push(SLOT_SYMS[Math.floor(Math.random() * 4)]);
  items.push(finalSymbol);
  return items;
}

function slotsIdle() {
  $$("#slots .strip").forEach((strip, i) => {
    strip.innerHTML = `<div>${symHTML(["seven", "seven", "seven"][i])}</div>`;
    strip.style.transition = "none";
    strip.style.transform = "translateY(0)";
  });
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
      const r = await api("/api/slots", { bet, cur: state.cur }, { deferBalance: true });
      showBetTaken(bet);
      haptic();
      const reels = $$("#slots .reel");
      await Promise.all(reels.map((reel, i) => new Promise((resolve) => {
        const strip = reel.querySelector(".strip");
        const items = reelStrip(r.reels[i], 22 + i * 8);
        strip.innerHTML = items.map((sym) => `<div>${symHTML(sym)}</div>`).join("");
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
      setBalance(r.balance, r.cur);
      const res = $("#slots-result");
      if (r.nft) {
        machine.classList.add("won");
        res.className = "result win reveal";
        res.textContent = r.nft.demo
          ? `ДЖЕКПОТ! Демо-NFT ${r.nft.title} · «${r.nft.model}» — в профиле → «Мои подарки»`
          : `ДЖЕКПОТ! NFT ${r.nft.emoji} ${r.nft.title} · «${r.nft.model}» — в профиле → «Мои подарки»`;
        haptic("win");
        fxBurstAt(machine, { count: 80, speed: 9 });
        bigWin(r.win / bet, r.win);
        $("#bigwin-label").textContent = "NFT JACKPOT";
        $("#bigwin-sum").textContent = `${r.nft.emoji} ${r.nft.title} ≈ ${nftPrice(r.nft.price)}`;
      } else if (r.win === bet) {
        res.className = "result";
        res.textContent = "Две семёрки — ставка возвращена";
      } else if (r.win > 0) {
        machine.classList.add("won");
        res.className = "result win reveal";
        res.textContent = `${r.value === 64 ? "777! " : ""}${fmtX(r.multiplier)} · +${money(r.win, r.cur)}`;
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
  const roller = typeof rollerSel === "string" ? $(rollerSel) : rollerSel;
  const track = roller.querySelector(".roller-track");
  track.innerHTML = "";
  items.forEach((it) => {
    const d = document.createElement("div");
    d.className = "it " + (it.cls || "");
    if (it.bg) d.style.background = it.bg;
    if (it.fg) d.style.color = it.fg;
    if (it.player) d.append(avatarEl(it.player, "av-big", it.colors));
    if (it.nft) {
      d.append(nftIcon(it.nft, "em"));
    } else if (it.em) {
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

// ---------- Plinko ----------

const plk = { rows: 12, risk: "medium", balls: [], raf: 0, inFlight: 0, hits: {}, pending: 0 };
const PLK_MAX_BALLS = 6;

function plkTable() {
  const t = state.config && state.config.plinko && state.config.plinko.tables;
  return t ? t[`${plk.rows}:${plk.risk}`] : null;
}

function plkCanvas() {
  const c = $("#plinko-canvas");
  const w = c.clientWidth || 360;
  const h = Math.round(w * 0.95);
  const dpr = window.devicePixelRatio || 1;
  if (c.width !== w * dpr || c.height !== h * dpr) {
    c.width = w * dpr;
    c.height = h * dpr;
    c.style.height = h + "px";
  }
  const ctx = c.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, w, h };
}

// Геометрия поля: ряд i (0..rows-1) — i+3 штырька; лунок rows+1
function plkGeom(w, h) {
  const rows = plk.rows;
  const gap = w / (rows + 2);
  const top = gap * 0.9;
  const rowH = (h - top - gap * 1.3) / rows;
  const peg = (i, j) => ({ x: w / 2 + (j - (i + 2) / 2) * gap, y: top + i * rowH });
  const bucketX = (k) => w / 2 + (k - rows / 2) * gap;
  return { gap, top, rowH, peg, bucketX, bucketY: top + rows * rowH - rowH * 0.2 };
}

function plkColor(m) {
  if (m >= 10) return ["#FFFFFF", "#0B0A10"];
  if (m >= 3) return ["#C4B5FD", "#0B0A10"];
  if (m >= 1.2) return ["#8B5CF6", "#FFFFFF"];
  if (m >= 1) return ["#6D28D9", "#FFFFFF"];
  return ["#2A2440", "#C4B5FD"];
}

function plkDraw(now) {
  const { ctx, w, h } = plkCanvas();
  const G = plkGeom(w, h);
  ctx.clearRect(0, 0, w, h);
  // штырьки
  ctx.fillStyle = "rgba(237, 233, 254, .85)";
  const pr = Math.max(2, G.gap * 0.09);
  for (let i = 0; i < plk.rows; i++) {
    for (let j = 0; j < i + 3; j++) {
      const p = G.peg(i, j);
      ctx.beginPath();
      ctx.arc(p.x, p.y, pr, 0, Math.PI * 2);
      ctx.fill();
    }
  }
  // лунки с множителями
  const table = plkTable() || [];
  const bw = G.gap * 0.92;
  const bh = Math.max(20, G.gap * 0.8);
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.font = `800 ${Math.max(8, Math.min(12, G.gap * 0.34))}px Manrope, sans-serif`;
  table.forEach((m, k) => {
    const x = G.bucketX(k);
    const hit = plk.hits[k] && now - plk.hits[k] < 350;
    const [bg, fg] = plkColor(m);
    const y = G.bucketY + (hit ? 5 : 0);
    ctx.fillStyle = bg;
    ctx.globalAlpha = hit ? 1 : 0.92;
    ctx.beginPath();
    ctx.roundRect(x - bw / 2, y, bw, bh, 6);
    ctx.fill();
    ctx.globalAlpha = 1;
    ctx.fillStyle = fg;
    ctx.fillText(m >= 100 ? Math.round(m) : m >= 10 ? m.toFixed(0) : String(m), x, y + bh / 2);
  });
  // шарики
  plk.balls.forEach((b) => {
    const pos = plkBallPos(b, now, G);
    ctx.fillStyle = "#FFFFFF";
    ctx.shadowColor = "rgba(167, 139, 250, .9)";
    ctx.shadowBlur = 12;
    ctx.beginPath();
    ctx.arc(pos.x, pos.y, Math.max(4, G.gap * 0.17), 0, Math.PI * 2);
    ctx.fill();
    ctx.shadowBlur = 0;
  });
}

const PLK_STEP_MS = 115;

// Позиция шарика: между рядами — прыжок по дуге к следующему штырьку
function plkBallPos(b, now, G) {
  const t = (now - b.start) / PLK_STEP_MS;
  const step = Math.min(Math.floor(t), plk.rows);
  const f = Math.min(1, t - step);
  const xAt = (s) => {
    let off = 0;
    for (let i = 0; i < s; i++) off += b.path[i] ? 0.5 : -0.5;
    return G.peg(0, 1).x + off * G.gap;
  };
  const yAt = (s) => (s >= plk.rows ? G.bucketY - G.gap * 0.2 : G.peg(s, 0).y - G.gap * 0.28);
  if (step >= plk.rows) return { x: xAt(plk.rows), y: yAt(plk.rows) };
  const x0 = xAt(step);
  const x1 = xAt(step + 1);
  const y0 = step === 0 ? G.top - G.gap * 0.9 : yAt(step);
  const y1 = yAt(step + 1);
  const ease = f * f;
  return { x: x0 + (x1 - x0) * f, y: y0 + (y1 - y0) * ease - Math.sin(f * Math.PI) * G.rowH * 0.35 };
}

function plkLoop(now) {
  const done = plk.balls.filter((b) => now - b.start >= PLK_STEP_MS * (plk.rows + 1));
  done.forEach((b) => {
    plk.hits[b.r.bucket] = now;
    b.resolve();
  });
  plk.balls = plk.balls.filter((b) => !done.includes(b));
  plkDraw(now);
  const active = plk.balls.length || Object.values(plk.hits).some((t) => now - t < 400);
  plk.raf = active && state.screen === "plinko" ? requestAnimationFrame(plkLoop) : 0;
}

function plkKick() {
  if (!plk.raf) plk.raf = requestAnimationFrame(plkLoop);
}

function plinkoEnter() {
  plkDraw(performance.now());
}

async function plinkoDrop() {
  if (plk.inFlight >= PLK_MAX_BALLS) return;
  let bet;
  try { bet = getBet("plinko"); } catch (e) { toast(e.message, true); return; }
  plk.inFlight += 1;
  $$("#plinko-rows button, #plinko-risk button").forEach((b) => { b.disabled = true; });
  try {
    const r = await api("/api/plinko", { bet, rows: plk.rows, risk: plk.risk, cur: state.cur }, { deferBalance: true });
    // выигрыш показываем, только когда шарик упадёт в лунку
    state.bal[r.cur] = r.balance;
    plk.pending += r.win;
    showBetTaken(plk.pending);
    haptic();
    await new Promise((resolve) => { plk.balls.push({ path: r.path, r, start: performance.now(), resolve }); plkKick(); });
    plk.pending -= r.win;
    showBetTaken(plk.pending);
    const res = $("#plinko-result");
    res.className = "result reveal " + (r.multiplier >= 1 ? "win" : "lose");
    res.textContent = `${fmtX(r.multiplier)} · ${r.win > bet ? "+" : ""}${money(r.win, r.cur)}`;
    if (r.multiplier >= 1) haptic("win");
    if (r.win >= bet * BIG_WIN_X) celebrate(bet, r.win, $("#plinko-canvas"));
  } catch (e) {
    toast(e.message, true);
  } finally {
    plk.inFlight -= 1;
    if (!plk.inFlight) $$("#plinko-rows button, #plinko-risk button").forEach((b) => { b.disabled = false; });
  }
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
    btn.textContent = game.opened.length ? "Забрать " + money(game.cashout, game.cur) : "Откройте клетку";
    btn.classList.toggle("cash", game.opened.length > 0);
  } else {
    btn.textContent = "Играть";
    btn.classList.remove("cash");
    if (!reveal) minesPreview();
  }
}

function minesPreview() {
  const edge = state.config && state.config.mines_edge !== undefined ? state.config.mines_edge : 0.1;
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
      info.textContent = `${fmtX(r.multiplier)} · +${money(r.win, r.cur)}`;
      haptic("win");
      celebrate(r.bet, r.win, $("#mines-grid"));
      return;
    }
    const bet = getBet("mines");
    minesGame = await api("/api/mines/start", { bet, mines: minesCount, cur: state.cur });
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
      info.textContent = `Все клетки! ${fmtX(r.multiplier)} · +${money(r.win, r.cur)}`;
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
    if (my && !my.cashout) $("#crash-btn").textContent = "Забрать " + money(Math.floor(my.bet * m), my.cur);
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
    am.textContent = money(p.bet, p.cur);
    if (p.gifts && p.gifts.length) {
      const gl = document.createElement("span");
      gl.className = "gl";
      p.gifts.slice(0, 3).forEach((g) => gl.append(nftIcon(g, "nft-inline")));
      nm.append(gl);
    }
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
    btn.textContent = my ? `Ставка ${money(my.bet, my.cur)} принята` : "Поставить";
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
      $("#crash-sub").textContent = my && my.cashout ? `Вы забрали +${money(my.win, my.cur)}` : my ? `В полёте: ${money(my.bet, my.cur)}` : "Ракета летит";
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
      setBalance(r.balance, r.cur);
      s.my.cashout = r.cashout;
      s.my.win = r.win;
      $("#crash-sub").textContent = `Вы забрали +${money(r.win, r.cur)}`;
      crashButton(s);
      haptic("win");
      celebrate(r.bet, r.win, $(".crash-box"));
    } catch (e) { toast(e.message, true); }
    return;
  }
  await guard(async () => {
    const bet = getBet("crash");
    const autoRaw = $("#crash-auto").value.trim().replace(",", ".");
    const body = { bet, cur: state.cur };
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
let casesFilter = "all";
const caseLive = { timer: 0, seen: new Set() };

const caseNft = (p) => ({ collection: p.title, model: p.model, emoji: p.emoji });
const caseTop = (c) => c.prizes.reduce((a, p) => (p.amount > a.amount ? p : a), c.prizes[0]);
const caseNftChance = (c) => c.prizes.filter((p) => p.kind === "nft").reduce((a, p) => a + p.chance, 0);

// Иконка кейса: картинка самого дорогого NFT внутри, иначе эмодзи кейса
function caseIcon(c, cls) {
  const top = caseTop(c);
  if (top.kind === "nft" && top.title) return nftIcon(caseNft(top), cls);
  const e = document.createElement("span");
  e.className = cls;
  e.textContent = c.emoji;
  return e;
}

function renderCases() {
  const box = $("#cases-list");
  box.innerHTML = "";
  const all = state.config ? state.config.cases : [];
  // картинки всех NFT из кейсов качаем заранее — к открытию они уже в кэше
  nftPreload(all.flatMap((c) => c.prizes.filter((p) => p.kind === "nft").map(caseNft)));
  const cases = all.filter((c) => casesFilter === "all" || (casesFilter === "nft") === c.id.startsWith("nft"));
  if (!all.length) box.innerHTML = '<div class="note">Кейсы временно недоступны — обновляем цены подарков.</div>';
  cases.forEach((c) => {
    const b = document.createElement("button");
    const nft = c.id.startsWith("nft");
    b.className = "case-card" + (nft ? " nft" : "");
    const top = caseTop(c);
    b.innerHTML = '<div class="e"></div><div class="n"></div><div class="j"></div><div class="p"></div>';
    b.querySelector(".e").append(caseIcon(c, "case-ic"));
    b.querySelector(".n").textContent = c.name;
    b.querySelector(".j").textContent = top.kind === "nft" ? `NFT до ${nftPrice(top.amount)}`
      : `до ${top.emoji} ${casePriceText(top.amount)}`;
    b.querySelector(".p").textContent = casePriceText(c.price, true);
    if (nft) {
      const badge = document.createElement("span");
      badge.className = "case-badge";
      badge.textContent = `NFT ${caseNftChance(c).toFixed(caseNftChance(c) < 1 ? 2 : 1)}%`;
      b.append(badge);
    }
    b.addEventListener("click", () => openCaseScreen(c));
    box.append(b);
  });
  caseLiveStart();
}

// ---------- лента дропов (live) ----------

function caseDropEl(d) {
  const el = document.createElement("div");
  el.className = "ld" + (d.good ? " good" : "") + (d.nft ? " nft" : "");
  const ic = d.nft && d.nft.title ? nftIcon({ collection: d.nft.title, model: d.nft.model, emoji: d.emoji }, "ld-ic")
    : Object.assign(document.createElement("span"), { className: "ld-ic", textContent: d.emoji });
  const t = document.createElement("div");
  t.className = "ld-t";
  const v = document.createElement("b");
  v.textContent = d.nft ? (d.nft.model || "NFT") : money(d.prize, d.cur);
  const nm = document.createElement("small");
  nm.textContent = d.name;
  t.append(v, nm);
  el.append(ic, t);
  return el;
}

async function caseLiveTick() {
  if (state.screen !== "cases" && state.screen !== "case") { caseLiveStop(); return; }
  try {
    const r = await api("/api/case/drops");
    const box = $("#case-live");
    const fresh = r.drops.filter((d) => !caseLive.seen.has(d.id));
    if (!caseLive.seen.size) box.innerHTML = "";
    fresh.reverse().forEach((d) => {
      caseLive.seen.add(d.id);
      const el = caseDropEl(d);
      if (caseLive.seen.size > r.drops.length) el.classList.add("new");
      box.prepend(el);
    });
    while (box.children.length > 20) box.lastChild.remove();
    if (!box.children.length) box.innerHTML = '<div class="note">Здесь появятся дропы игроков</div>';
  } catch (_) { /* лента не критична */ }
}

function caseLiveStart() {
  if (caseLive.timer) return;
  caseLiveTick();
  caseLive.timer = setInterval(caseLiveTick, 6000);
}

function caseLiveStop() {
  clearInterval(caseLive.timer);
  caseLive.timer = 0;
}

// Цены кейсов — в звёздах; в режиме TON показываем по курсу
function casePriceText(starsAmount, up) {
  const v = fromStars(starsAmount, null, up);
  return v === null ? "курс TON недоступен" : money(v);
}

// Редкость приза по отношению к цене кейса
function prizeTier(p, price) {
  const ratio = p.amount / price;
  if (p.kind === "nft" || ratio >= 20) return { cls: "t-leg", name: "легендарный", bg: "#FFFFFF", fg: "#0B0A10" };
  if (ratio >= 3) return { cls: "t-epic", name: "эпический", bg: "#A78BFA", fg: "#0B0A10" };
  if (ratio >= 1.5) return { cls: "t-rare", name: "редкий", bg: "#8B5CF6", fg: "#FFFFFF" };
  if (ratio >= 1) return { cls: "t-unc", name: "окупает", bg: "#6D28D9", fg: "#FFFFFF" };
  return { cls: "t-com", name: "обычный", bg: "#242033", fg: "#FFFFFF" };
}

function prizeItem(p, price) {
  const t = prizeTier(p, price);
  return { em: p.emoji, sub: p.label || (p.kind === "nft" ? (p.model || "NFT") : casePriceText(p.amount)), bg: t.bg, fg: t.fg,
    cls: t.cls, nft: p.kind === "nft" && p.title ? caseNft(p) : null };
}

function pickPrize(c) {
  let r = Math.random() * 100;
  for (const p of c.prizes) { r -= p.chance; if (r <= 0) return p; }
  return c.prizes[0];
}

function openCaseScreen(c) {
  currentCase = c;
  go("case");
  caseLiveStart();
  $("#title").textContent = c.name;
  const hero = $("#case-emoji");
  hero.innerHTML = "";
  hero.append(caseIcon(c, "case-ic big"));
  const nftCh = caseNftChance(c);
  $("#case-meta").innerHTML = "";
  [`RTP ${(c.rtp * 100).toFixed(1)}%`, nftCh ? `шанс NFT ${nftCh.toFixed(nftCh < 1 ? 2 : 1)}%` : null,
    `${c.prizes.length} призов`].filter(Boolean).forEach((txt) => {
    const s = document.createElement("span");
    s.textContent = txt;
    $("#case-meta").append(s);
  });
  $("#case-result").textContent = "";
  const box = $("#case-prizes");
  box.innerHTML = "";
  c.prizes.slice().sort((a, b) => b.amount - a.amount).forEach((p) => {
    const tier = prizeTier(p, c.price);
    const d = document.createElement("div");
    d.className = tier.cls;
    const e = document.createElement("span");
    e.className = "em";
    if (p.kind === "nft") e.append(nftIcon(caseNft(p), "nft-inline big"));
    else e.textContent = p.emoji;
    const sm = document.createElement("small");
    sm.textContent = (p.chance < 0.1 ? p.chance.toFixed(3) : p.chance.toFixed(2)) + "%";
    if (p.kind === "nft") {
      d.classList.add("nft");
      const ttl = document.createElement("span");
      ttl.className = "ttl";
      ttl.textContent = p.title;
      const model = document.createElement("small");
      model.textContent = `модель «${p.model || "—"}»` + (p.rarity != null ? ` · ${p.rarity}%` : "")
        + (p.demo ? " · демо" : "");
      d.append(e, ttl, model, document.createTextNode("флор " + nftPrice(p.amount)), sm);
    } else {
      d.append(e, document.createTextNode(casePriceText(p.amount)), sm);
    }
    box.append(d);
  });
  caseSetCount(caseCount);
}

let caseCount = 1;
let caseFast = false;
let caseBusy = false;

// Рулетки кейса: по одной на каждый открываемый кейс
function caseRollers(n) {
  const box = $("#case-rollers");
  box.classList.toggle("multi", n > 1);
  while (box.children.length > n) box.lastChild.remove();
  while (box.children.length < n) {
    const r = document.createElement("div");
    r.className = "roller tall";
    r.innerHTML = '<div class="roller-track"></div><div class="roller-pointer"></div>';
    box.append(r);
  }
  return Array.from(box.children);
}

function caseSetCount(n) {
  caseCount = n;
  store("case:count", String(n));
  $$("#case-count button").forEach((b) => b.classList.toggle("sel", parseInt(b.dataset.n, 10) === n));
  if (!currentCase) return;
  $("#case-btn").textContent = `Открыть ${n > 1 ? n + " шт. " : ""}за ${casePriceText(currentCase.price * n, true)}`;
  $("#case-drops").classList.add("hidden");
  caseRollers(n).forEach((r) => {
    const items = [];
    for (let i = 0; i < 12; i++) items.push(prizeItem(pickPrize(currentCase), currentCase.price));
    roll(r, items, 5, 0);
  });
}

function caseSetFast(on) {
  caseFast = on;
  store("case:fast", on ? "1" : "");
  $("#case-fast").classList.toggle("hi", on);
}

// Прокрутка рулеток до выпавших призов; рулетки останавливаются по очереди
function caseSpin(prizes) {
  const rollers = caseRollers(prizes.length);
  const base = caseFast ? 1300 : 4600;
  const step = caseFast ? 150 : 350;
  return Promise.all(prizes.map((p, k) => {
    const items = [];
    for (let i = 0; i < 60; i++) items.push(prizeItem(i === 50 ? p : pickPrize(currentCase), currentCase.price));
    return roll(rollers[k], items, 50, base + k * step);
  }));
}

function caseLock(on) {
  caseBusy = on;
  ["#case-btn", "#case-demo"].forEach((s) => { $(s).disabled = on; });
  $$("#case-count button").forEach((b) => { b.disabled = on; });
}

// Бесплатная прокрутка: ничего не списывает, просто показывает, что могло бы выпасть
async function caseDemo() {
  if (!currentCase || caseBusy) return;
  caseLock(true);
  try {
    const n = caseCount;
    const prizes = Array.from({ length: n }, () => pickPrize(currentCase));
    $("#case-drops").classList.add("hidden");
    $("#case-result").className = "result";
    $("#case-result").textContent = "Демо-прокрутка…";
    haptic();
    await caseSpin(prizes);
    const total = prizes.reduce((a, p) => a + p.amount, 0);
    const nft = prizes.find((p) => p.kind === "nft");
    $("#case-result").className = "result reveal";
    $("#case-result").textContent = "Демо: " + (nft ? `выпал бы NFT ${nft.title} «${nft.model}»`
      : `выпало бы ${n > 1 ? "на " : ""}${casePriceText(total)}`) + " — откройте по-настоящему!";
  } finally {
    caseLock(false);
  }
}

function caseDropsList(r) {
  const drops = $("#case-drops");
  drops.innerHTML = "";
  r.items.forEach((it) => {
    const s = document.createElement("span");
    if (it.kind === "nft" && it.nft) {
      s.append(nftIcon({ collection: it.nft.title, model: it.nft.model, emoji: it.gift }, "nft-inline"),
        ` «${it.nft.model}»`);
    } else {
      s.textContent = `${it.gift} ${money(it.prize, r.cur)}`;
    }
    if (it.kind === "nft" || it.prize >= r.price) s.className = "good";
    drops.append(s);
  });
  drops.classList.remove("hidden");
}

async function openCase() {
  if (!currentCase || caseBusy) return;
  await guard(async () => {
    const n = caseCount;
    const cost = fromStars(currentCase.price, null, true) * n;
    if (!(cost > 0)) throw new Error("Курс TON недоступен — откройте кейс за звёзды");
    if (state.me && curBalance() < cost) throw new Error(state.cur === "ton" ? "Недостаточно TON на балансе" : "Недостаточно звёзд на балансе");
    caseLock(true);
    try {
      const r = await api("/api/case", { case: currentCase.id, count: n, cur: state.cur }, { deferBalance: true });
      showBetTaken(cost);
      haptic();
      $("#case-drops").classList.add("hidden");
      $("#case-result").className = "result";
      $("#case-result").textContent = n > 1 ? `Открываем ${n} кейса…` : "Открываем…";
      await caseSpin(r.items.map((it) => ({ kind: it.kind, emoji: it.gift,
        label: it.kind === "nft" ? (it.nft && it.nft.model) || "NFT" : money(it.prize, r.cur),
        title: it.nft && it.nft.title, model: it.nft && it.nft.model,
        amount: r.cur === "ton" ? Math.round(it.prize / NANO * (tonRate() || 0)) : it.prize })));
      setBalance(r.balance, r.cur);
      const res = $("#case-result");
      const good = r.total >= r.cost;
      const nfts = r.items.filter((it) => it.kind === "nft");
      res.className = "result reveal " + (good ? "win" : "lose");
      if (n === 1) {
        res.textContent = nfts.length
          ? (nfts[0].nft.demo
            ? `Демо-NFT ${nfts[0].nft.title} · «${nfts[0].nft.model}» — в профиле → «Мои подарки»`
            : `NFT ${nfts[0].nft.title} · «${nfts[0].nft.model}»! Он в профиле → «Мои подарки»`)
          : `${r.gift} ${money(r.prize, r.cur)}`;
      } else {
        res.textContent = `Выпало на ${money(r.total, r.cur)} из ${money(r.cost, r.cur)}` + (nfts.length ? " · NFT в профиле → «Мои подарки»" : "");
        caseDropsList(r);
      }
      haptic(good ? "win" : "lose");
      celebrate(r.cost, r.total, $("#case-rollers"));
      caseLiveTick();
      if (nfts.length) {
        await loadMe().catch(() => {});
        renderCases();
      }
    } finally {
      caseLock(false);
    }
  });
}

// ---------- PvP: рулетка и хоккей ----------

const pvp = { game: null, cur: "stars", timer: 0, lastSeen: {}, animating: false, colors: {}, round: null, holdUntil: 0 };
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
    am.textContent = money(p.amount, pvp.cur);
    if (p.gifts && p.gifts.length) {
      const gl = document.createElement("span");
      gl.className = "gl";
      p.gifts.slice(0, 5).forEach((g) => gl.append(nftIcon(g, "nft-inline")));
      if (p.gifts.length > 5) gl.append("…");
      gl.title = p.gifts.map((g) => g.title).join(", ");
      nm.append(gl);
    }
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
  res.textContent = mine ? `Вы забрали ${money(last.payout, pvp.cur)}!` : `${last.winner.name} забирает ${money(last.payout, pvp.cur)}`;
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
    const s = await api(`/api/pvp?game=${game}&cur=${pvp.cur}`);
    if (pvp.game !== game) return;
    document.querySelectorAll(".pvp-fee").forEach((el) => { el.textContent = Math.round(s.commission * 100); });
    const round = s.round;
    pvp.round = round;
    const seen = pvp.lastSeen[game + pvp.cur];
    if (s.last && seen !== undefined && s.last.id !== seen && s.last.finished_ago < 15 && !pvp.animating) {
      pvp.lastSeen[game + pvp.cur] = s.last.id;
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
      pvp.lastSeen[game + pvp.cur] = s.last ? s.last.id : 0;
    }
    pvpEl(game, "pot").textContent = moneyNum(round ? round.pot : 0, pvp.cur);
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
  pvp.cur = state.cur;        // у звёзд и TON раунды раздельные
  pvp.holdUntil = 0;
  $$(".gift-btn").forEach((b) => b.classList.toggle("hidden", pvp.cur !== "stars"));
  $$(".pot-cur").forEach((el) => { el.textContent = pvp.cur === "ton" ? "TON" : "★"; });
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

async function pvpBet(game, gifts) {
  await guard(async () => {
    const body = gifts && gifts.length ? { amount: 0, game, gifts, cur: "stars" }
      : { amount: getBet(game === "hockey" ? "hockey" : "pvp"), game, cur: state.cur };
    const s = await api("/api/pvp/bet", body);
    pvp.holdUntil = 0;
    loadMe().catch(() => {});
    pvpRenderPlayers(game, s.round);
    if (game === "hockey") hockeyDrawIdle(s.round);
    toast(gifts && gifts.length ? "Подарки в банке!" : "Ставка принята");
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
  $("#plinko-btn").addEventListener("click", plinkoDrop);
  $("#bonus-btn").addEventListener("click", claimBonus);
  $("#rake-btn").addEventListener("click", claimRakeback);
  $$("#plinko-risk button").forEach((b) => b.addEventListener("click", () => {
    plk.risk = b.dataset.risk;
    $$("#plinko-risk button").forEach((x) => x.classList.toggle("sel", x === b));
    haptic();
    plinkoEnter();
  }));
  $$("#plinko-rows button").forEach((b) => b.addEventListener("click", () => {
    plk.rows = Number(b.dataset.rows);
    $$("#plinko-rows button").forEach((x) => x.classList.toggle("sel", x === b));
    haptic();
    plinkoEnter();
  }));
  $$("#mines-seg button").forEach((b) => b.addEventListener("click", () => {
    minesCount = parseInt(b.dataset.m, 10);
    $$("#mines-seg button").forEach((x) => x.classList.toggle("sel", x === b));
    minesPreview();
    haptic();
  }));
  $("#mines-btn").addEventListener("click", minesAction);
  $("#crash-btn").addEventListener("click", crashAction);
  $("#case-btn").addEventListener("click", openCase);
  $("#case-demo").addEventListener("click", caseDemo);
  caseSetFast(!!store("case:fast"));
  $("#case-fast").addEventListener("click", () => caseSetFast(!caseFast));
  $$("#cases-filter button").forEach((b) => b.addEventListener("click", () => {
    casesFilter = b.dataset.f;
    $$("#cases-filter button").forEach((x) => x.classList.toggle("sel", x === b));
    renderCases();
  }));
  caseCount = Math.min(5, Math.max(1, parseInt(store("case:count"), 10) || 1));
  $$("#case-count button").forEach((b) => b.addEventListener("click", () => {
    haptic();
    caseSetCount(parseInt(b.dataset.n, 10));
  }));
  $("#pvp-btn").addEventListener("click", () => pvpBet("roulette"));
  $("#hockey-btn").addEventListener("click", () => pvpBet("hockey"));
  $("#pvp-gifts").addEventListener("click", () => pvpGiftSheet("roulette"));
  $("#crash-gifts").addEventListener("click", crashGiftSheet);
  $("#hockey-gifts").addEventListener("click", () => pvpGiftSheet("hockey"));
  $("#nft-open").addEventListener("click", openRelayer);
  $$("#tabbar button").forEach((b) => b.addEventListener("click", () => {
    if (b.dataset.tab !== state.screen) { haptic(); go(b.dataset.tab); }
  }));
  $$("[data-wallet]").forEach((b) => b.addEventListener("click", () => {
    haptic();
    go("wallet");
    walletTab(b.dataset.wallet);
  }));
  $("#upg-btn").addEventListener("click", upgradeGo);
  $("#upg-all").addEventListener("click", upgSelectAll);
  $("#upg-search").addEventListener("input", (e) => { upg.query = e.target.value; upgRenderTargets(); upgUpdate(); });
  $$("#upg-mults button").forEach((b) => b.addEventListener("click", () => {
    if (upg.spinning) return;
    haptic();
    const x = Number(b.dataset.x);
    upg.mult = upg.mult === x ? null : x;
    if (upg.mult && !upgStake()) { toast("Сначала выберите свои NFT", true); upg.mult = null; }
    upgApplyMult();
    upgUpdate();
  }));
  $$("#cur-switch button").forEach((b) => b.addEventListener("click", () => {
    if (b.dataset.cur === state.cur || state.busy) return;
    haptic();
    $$("#cur-switch button").forEach((x) => x.classList.toggle("sel", x === b));
    setCurrency(b.dataset.cur);
  }));
  $$("[data-copy]").forEach((b) => b.addEventListener("click", () => copyText($("#" + b.dataset.copy).textContent)));
  $("#ton-open").addEventListener("click", tonOpenWallet);
  $("#ton-withdraw").addEventListener("click", tonWithdraw);
  $("#ton-max").addEventListener("click", () => { $("#ton-amount").value = Math.floor(state.bal.ton / NANO * 100) / 100; });
  $("#ref-copy").addEventListener("click", refCopy);
  $("#ref-share").addEventListener("click", refShare);
  $("#sheet-cancel").addEventListener("click", () => $("#sheet").classList.add("hidden"));
  $("#sheet").addEventListener("click", (e) => { if (e.target.id === "sheet") $("#sheet").classList.add("hidden"); });
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
  minesRender(null);
  slotsIdle();
  if (!tg || !tg.initData) {
    toast("Откройте Svag Gifts через кнопку в Telegram-боте", true);
    return;
  }
  document.body.classList.add("has-tabbar");
  await homeEnter();
  if (store("cur") === "ton") {
    $$("#cur-switch button").forEach((x) => x.classList.toggle("sel", x.dataset.cur === "ton"));
    setCurrency("ton");
  }
  minesPreview();
  renderPaytable();
  renderPresets();
}

init();
