"""Профиль стиля по примерам и подбор похожих примеров (TF-IDF на символьных n-граммах)."""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any, Iterable

EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF\U00002B00-\U00002BFF‍️]+"
)
WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)

GREETINGS = ("привет", "прив", "приветик", "здарова", "здаров", "здорово", "хай", "ку", "салам",
             "йо", "здравствуй", "здравствуйте", "добрый", "доброе", "hello", "hi", "hey", "хелло")
FAREWELLS = ("пока", "давай", "бб", "бай", "до связи", "до завтра", "спокойной", "увидимся",
             "на связи", "обнял", "целую", "bye")
MAT_ROOTS = ("хуй", "хуе", "хуё", "пизд", "еба", "ебу", "ебл", "ёб", "бля", "сука", "нахуй", "похуй")
STOPWORDS = set(
    "и в во не что он на я с со как а то все всё она так его но да ты к у же вы за бы по только ее её мне "
    "было вот от меня еще ещё нет о из ему теперь когда даже ну вдруг ли если уже или ни быть был него до "
    "вас нибудь опять уж вам ведь там потом себя ничего ей может они тут где есть надо ней для мы тебя их "
    "чем была сам чтоб без будто чего раз тоже себе под будет ж тогда кто этот того потому этого какой "
    "совсем ним здесь этом один почти мой тем чтобы нее неё сейчас были куда зачем всех никогда можно при "
    "наконец два об другой хоть после над больше тот через эти нас про всего них какая много разве три "
    "эту моя впрочем хорошо свою этой перед иногда лучше чуть том нельзя такой им более всегда конечно "
    "всю между это".split()
)

INCOMING_PREFIXES = ("мне", "он", "она", "они", "собеседник", "друг", "подруга", "вопрос", "им", "ему", "ей")
REPLY_PREFIXES = ("я", "ответ", "мой ответ")
_PREFIX_RE = re.compile(
    r"^\s*(?P<who>" + "|".join(re.escape(p) for p in INCOMING_PREFIXES + REPLY_PREFIXES) + r")\s*[:\-—–>]\s*(?P<text>.*)$",
    re.IGNORECASE,
)


# ---------- разбор пар «мне написали → я ответил» ----------

def parse_pairs(text: str) -> list[tuple[str | None, str]]:
    """Разбирает текст на пары (входящее, мой ответ).

    Понимает строки вида «мне: ...» / «я: ...». Строки без метки продолжают
    предыдущую реплику. Если меток нет вообще, весь текст считается моим сообщением.
    """
    lines = [l for l in text.strip().splitlines()]
    if not any(_PREFIX_RE.match(l) for l in lines):
        cleaned = text.strip()
        return [(None, cleaned)] if cleaned else []

    turns: list[tuple[str, list[str]]] = []  # (in|me, строки)
    for line in lines:
        m = _PREFIX_RE.match(line)
        if m:
            who = "me" if m.group("who").lower() in REPLY_PREFIXES else "in"
            body = m.group("text").strip()
            if turns and turns[-1][0] == who:
                turns[-1][1].append(body)
            else:
                turns.append((who, [body]))
        elif turns and line.strip():
            turns[-1][1].append(line.strip())

    pairs: list[tuple[str | None, str]] = []
    pending_in: str | None = None
    for who, parts in turns:
        body = "\n".join(p for p in parts if p)
        if not body:
            continue
        if who == "in":
            pending_in = body
        else:
            pairs.append((pending_in, body))
            pending_in = None
    return pairs


# ---------- статистика стиля ----------

def _split_messages(replies: Iterable[str]) -> list[list[str]]:
    """Каждый ответ — «пачка» сообщений; сообщения внутри разделены переводом строки."""
    bursts = []
    for reply in replies:
        msgs = [m.strip() for m in reply.split("\n") if m.strip()]
        if msgs:
            bursts.append(msgs)
    return bursts


def compute_profile(replies: list[str]) -> dict[str, Any]:
    bursts = _split_messages(replies)
    messages = [m for b in bursts for m in b]
    if not messages:
        return {}
    n = len(messages)
    lengths = sorted(len(m) for m in messages)
    letters_start = [m for m in messages if m[0].isalpha()]
    lower_start = sum(1 for m in letters_start if m[0].islower())
    ends_period = sum(1 for m in messages if m.rstrip().endswith(".") and not m.rstrip().endswith(".."))
    no_end_punct = sum(1 for m in messages if m.rstrip()[-1].isalnum())
    ellipsis = sum(1 for m in messages if ".." in m or "…" in m)
    exclaim = sum(1 for m in messages if "!" in m)
    question = sum(1 for m in messages if "?" in m)
    caps_words = sum(1 for m in messages for w in WORD_RE.findall(m) if len(w) > 2 and w.isupper())
    with_emoji = sum(1 for m in messages if EMOJI_RE.search(m))
    with_brackets = sum(1 for m in messages if re.search(r"\){1,}\s*$|\({2,}", m))

    words: Counter[str] = Counter()
    for m in messages:
        for w in WORD_RE.findall(m.lower()):
            if w not in STOPWORDS and len(w) > 1:
                words[w] += 1
    emojis: Counter[str] = Counter()
    for m in messages:
        for e in EMOJI_RE.findall(m):
            e = e.replace("️", "")
            if e:
                emojis[e] += 1

    greetings: Counter[str] = Counter()
    farewells: Counter[str] = Counter()
    for m in messages:
        low = m.lower()
        for g in GREETINGS:
            if re.match(rf"^{re.escape(g)}\b", low):
                greetings[_first_words(m, g)] += 1
                break
        for f in FAREWELLS:
            if re.search(rf"(^|\s){re.escape(f)}\b", low) and len(low) < 40:
                farewells[f] += 1
                break
    mat = sum(1 for m in messages if any(root in m.lower() for root in MAT_ROOTS))

    return {
        "messages": n,
        "replies": len(bursts),
        "avg_len": round(sum(lengths) / n, 1),
        "median_len": lengths[n // 2],
        "lowercase_start": _ratio(lower_start, len(letters_start)),
        "period_end": _ratio(ends_period, n),
        "no_end_punct": _ratio(no_end_punct, n),
        "ellipsis": _ratio(ellipsis, n),
        "exclaim": _ratio(exclaim, n),
        "question": _ratio(question, n),
        "caps_words_per_msg": round(caps_words / n, 2),
        "emoji_msgs": _ratio(with_emoji, n),
        "bracket_smiles": _ratio(with_brackets, n),
        "mat": _ratio(mat, n),
        "top_words": [w for w, _ in words.most_common(15)],
        "top_emoji": [e for e, _ in emojis.most_common(8)],
        "greetings": [g for g, _ in greetings.most_common(5)],
        "farewells": [f for f, _ in farewells.most_common(5)],
        "avg_msgs_per_reply": round(n / len(bursts), 2),
        "multi_msg_ratio": _ratio(sum(1 for b in bursts if len(b) > 1), len(bursts)),
    }


def _first_words(message: str, greeting: str) -> str:
    # «Прив))» -> «прив))», «Здарова, бро» -> «здарова, бро»
    return message.strip().lower()[: max(len(greeting) + 6, 12)].strip()


def _ratio(a: int, b: int) -> float:
    return round(a / b, 2) if b else 0.0


def _pct(x: float) -> str:
    return f"{round(x * 100)}%"


def profile_to_text(p: dict[str, Any]) -> str:
    """Описание профиля человеческим языком — для промта и для /profile."""
    if not p or not p.get("messages"):
        return "Пока нет данных: добавь примеры через /examples."
    lines = [
        f"Проанализировано сообщений: {p['messages']} (ответов: {p['replies']}).",
        f"Длина сообщения: в среднем {p['avg_len']} символов, медиана {p['median_len']}.",
        f"С маленькой буквы: {_pct(p['lowercase_start'])} сообщений.",
        f"Точка в конце: {_pct(p['period_end'])}; без знака в конце: {_pct(p['no_end_punct'])}.",
        f"Многоточия: {_pct(p['ellipsis'])}; «!»: {_pct(p['exclaim'])}; «?»: {_pct(p['question'])}.",
        f"Эмодзи: в {_pct(p['emoji_msgs'])} сообщений; скобочки-смайлы «)»: {_pct(p['bracket_smiles'])}.",
        f"Мат: в {_pct(p['mat'])} сообщений.",
        f"Слова КАПСОМ: {p['caps_words_per_msg']} на сообщение.",
        f"Сообщений подряд за один ответ: в среднем {p['avg_msgs_per_reply']}; "
        f"несколько сообщений подряд в {_pct(p['multi_msg_ratio'])} ответов.",
    ]
    if p.get("top_words"):
        lines.append("Частые слова: " + ", ".join(p["top_words"]))
    if p.get("top_emoji"):
        lines.append("Любимые эмодзи: " + " ".join(p["top_emoji"]))
    if p.get("greetings"):
        lines.append("Приветствия: " + "; ".join(p["greetings"]))
    if p.get("farewells"):
        lines.append("Прощания: " + "; ".join(p["farewells"]))
    return "\n".join(lines)


# ---------- поиск похожих примеров ----------

def _ngrams(text: str, sizes: tuple[int, ...] = (2, 3, 4)) -> Counter[str]:
    text = " " + re.sub(r"\s+", " ", text.lower()).strip() + " "
    grams: Counter[str] = Counter()
    for n in sizes:
        for i in range(len(text) - n + 1):
            grams[text[i : i + n]] += 1
    return grams


def _vector(tf: Counter[str], idf: dict[str, float]) -> dict[str, float]:
    vec = {g: (1 + math.log(c)) * idf.get(g, 0.0) for g, c in tf.items()}
    norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
    return {g: v / norm for g, v in vec.items()}


def select_examples(query: str, examples: list[dict[str, Any]], k: int = 15) -> list[dict[str, Any]]:
    """Возвращает до k примеров, наиболее похожих на входящее сообщение.

    Сравниваем по входящей реплике примера (если она есть), иначе по самому ответу.
    Если похожих мало, добиваем самыми свежими примерами — они всё равно показывают стиль.
    """
    if not examples:
        return []
    docs = [_ngrams(e.get("incoming") or e["reply"]) for e in examples]
    df: Counter[str] = Counter()
    for d in docs:
        df.update(d.keys())
    total = len(docs)
    idf = {g: math.log((1 + total) / (1 + c)) + 1 for g, c in df.items()}
    q = _vector(_ngrams(query), idf)
    scored = []
    for idx, d in enumerate(docs):
        v = _vector(d, idf)
        score = sum(val * v.get(g, 0.0) for g, val in q.items())
        # пары с входящим сообщением ценнее для подражания
        if examples[idx].get("incoming"):
            score *= 1.1
        scored.append((score, idx))
    scored.sort(reverse=True)
    chosen = [idx for score, idx in scored if score > 0.05][:k]
    if len(chosen) < k:
        for idx in range(total - 1, -1, -1):
            if idx not in chosen:
                chosen.append(idx)
            if len(chosen) >= k:
                break
    return [examples[i] for i in chosen]
