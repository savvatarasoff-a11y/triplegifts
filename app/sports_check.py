"""Диагностика футбола на сервере: что отдаёт ESPN и почему матчи получают (или не получают) цену.

    cd /opt/triple-gifts && ./venv/bin/python -m app.sports_check
"""
from __future__ import annotations

import asyncio
import json
from collections import Counter
from datetime import datetime, timedelta, timezone

from . import sports as sp


async def main() -> None:
    client = sp.EspnClient()
    power: dict[str, dict[str, float]] = {}
    print("== Таблицы (сила команд для модели)")
    for code, level in sp.STRENGTH_LEAGUES.items():
        try:
            data = await client.standings(code)
            got = sp.strengths(data, level)
            power.update(got)
            sample = json.dumps(data)[:300]
            print(f"{code}: команд с данными {len(got)}" + ("" if got else f" | начало ответа: {sample}"))
        except Exception as e:
            print(f"{code}: ОШИБКА {type(e).__name__}: {e}")
    today = datetime.now(timezone.utc)
    book = sp.Sportsbook.__new__(sp.Sportsbook)
    book.power = power
    print("\n== Матчи на", sp.AHEAD_DAYS, "дней вперёд")
    for league, (code, name, _) in sp.LEAGUES.items():
        try:
            events = await client.scoreboard(code, today, today + timedelta(days=sp.AHEAD_DAYS))
        except Exception as e:
            print(f"{name} ({code}): ОШИБКА {type(e).__name__}: {e}")
            continue
        states, priced, reasons = Counter(), Counter(), Counter()
        first_odds = None
        for ev in events:
            comp = (ev.get("competitions") or [{}])[0]
            states[((comp.get("status") or {}).get("type") or {}).get("state")] += 1
            if comp.get("odds") and first_odds is None:
                first_odds = comp["odds"][0]
            kickoff = sp._ts(comp.get("date") or ev.get("date"))
            if not kickoff:
                reasons["не разобрал дату: " + str(comp.get("date") or ev.get("date"))] += 1
                continue
            try:
                odds, source = book.price(comp, bool(comp.get("neutralSite")))
            except Exception as e:
                reasons[f"ошибка разбора: {type(e).__name__}"] += 1
                continue
            if odds:
                priced[source] += 1
                if priced[source] <= 3:
                    names = {c.get("homeAway"): ((c.get("team") or {}).get("displayName") or "?")
                             for c in comp.get("competitors") or []}
                    print(f"   {names.get('home')} — {names.get('away')}: П1 {odds[0]}  Х {odds[1]}  П2 {odds[2]}"
                          f"  ({source}; линия {sp.book_odds(comp)})")
            else:
                reasons["нет линии и нет данных о командах"] += 1
        print(f"{name} ({code}): матчей {len(events)}, статусы {dict(states)}, с ценой {dict(priced)}, "
              f"пропущено {dict(reasons)}")
        if first_odds is not None:
            print("   пример линии:", json.dumps(first_odds)[:400])
        elif events:
            comp = (events[0].get("competitions") or [{}])[0]
            print("   линий нет; поля матча:", sorted(comp.keys()))


if __name__ == "__main__":
    asyncio.run(main())
