from app.llm import format_examples, format_history, pairs_from_transcript


def test_pairs_from_transcript_groups_sides():
    items = [
        {"side": "them", "text": "привет"},
        {"side": "them", "text": "ты где"},
        {"side": "me", "text": "дома"},
        {"side": "me", "text": "а чо"},
        {"side": "them", "text": "го гулять"},
        {"side": "me", "text": "го"},
        {"side": "them", "text": "без ответа"},
    ]
    assert pairs_from_transcript(items) == [
        ("привет\nты где", "дома\nа чо"),
        ("го гулять", "го"),
    ]


def test_pairs_from_transcript_starts_with_me():
    assert pairs_from_transcript([{"side": "me", "text": "ку"}]) == [(None, "ку")]


def test_formatters():
    hist = [{"from_owner": 0, "text": "ку"}, {"from_owner": 1, "text": "прив"}]
    assert format_history(hist, "Савва") == "Собеседник: ку\nСавва: прив"
    out = format_examples([{"incoming": "а", "reply": "б"}, {"incoming": None, "reply": "в"}], "Савва")
    assert "Собеседник: а\nСавва: б" in out and "Савва: в" in out
