from app.style import compute_profile, parse_pairs, profile_to_text, select_examples


def test_parse_pairs_with_markers():
    text = "мне: го гулять?\nя: ща\nне могу пока\nон: а когда\nя: вечером мб"
    assert parse_pairs(text) == [
        ("го гулять?", "ща\nне могу пока"),
        ("а когда", "вечером мб"),
    ]


def test_parse_pairs_without_markers_is_my_message():
    assert parse_pairs("  ну давай завтра  ") == [(None, "ну давай завтра")]
    assert parse_pairs("   ") == []


def test_parse_pairs_reply_without_incoming():
    assert parse_pairs("я: окей") == [(None, "окей")]


def test_compute_profile_basic():
    replies = ["прив))\nкак сам", "да норм 😂", "ща гляну", "Пока."]
    p = compute_profile(replies)
    assert p["messages"] == 5
    assert p["replies"] == 4
    assert p["lowercase_start"] == 0.8
    assert p["multi_msg_ratio"] == 0.25
    assert "😂" in p["top_emoji"]
    assert p["greetings"] and p["greetings"][0].startswith("прив")
    assert "пока" in p["farewells"]
    text = profile_to_text(p)
    assert "маленькой буквы" in text


def test_compute_profile_empty():
    assert compute_profile([]) == {}
    assert "Пока нет данных" in profile_to_text({})


def test_select_examples_prefers_similar():
    examples = [
        {"incoming": "пойдём в кино?", "reply": "го"},
        {"incoming": "скинь домашку", "reply": "ща скину"},
        {"incoming": "как дела", "reply": "норм"},
    ]
    top = select_examples("скинь пожалуйста домашку по алгебре", examples, k=1)
    assert top[0]["reply"] == "ща скину"


def test_select_examples_fills_up_to_k():
    examples = [{"incoming": None, "reply": f"пример {i}"} for i in range(20)]
    assert len(select_examples("абвгд", examples, k=15)) == 15
    assert select_examples("что угодно", [], k=5) == []
