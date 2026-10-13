import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).parents[1]))
import pytest
import tracker as T

def mk(direction="long", entry=14.19, stop=12, targets="17 50%\n19.5 25%", status="open"):
    tg = T.parse_targets(targets)
    T.validate(direction, entry, stop, tg)
    return dict(direction=direction, entry=entry, stop=stop, orig_stop=stop, targets=tg,
                status=status, remaining=100.0, banked=0.0, result_r=None, events=[],
                created_at=0, opened_at=0, last_price=entry)

def test_parse_targets_even_split_and_runner():
    assert [t["pct"] for t in T.parse_targets("1\n2")] == [50, 50]
    assert [t["pct"] for t in T.parse_targets("T1: 1 30%, TP2 2")] == [30, 70]
    assert sum(t["pct"] for t in T.parse_targets("1 50\n2 25")) == 75
    with pytest.raises(T.TradeError): T.parse_targets("1 80\n2 40")

def test_validate():
    with pytest.raises(T.TradeError): T.validate("long", 10, 11, T.parse_targets("12"))
    with pytest.raises(T.TradeError): T.validate("short", 10, 11, T.parse_targets("12"))
    with pytest.raises(T.TradeError): T.validate("long", 10, 9, T.parse_targets("12\n11"))

def test_long_full_lifecycle_like_screenshot():
    t = mk()
    ev = T.process_candle(t, 17.2, 15, candle_ms=1)          # T1 -> stop to BE
    assert ev[0]["kind"] == "tp" and t["stop"] == 14.19 and t["remaining"] == 50
    ev = T.process_candle(t, 15, 14.0, candle_ms=1)          # same candle re-read: BE must NOT trigger
    assert t["status"] == "open"
    T.process_candle(t, 19.6, 17, candle_ms=2)               # T2
    assert t["remaining"] == 25
    T.move_stop(t, 22.0) if False else None
    t["last_price"] = 25; T.move_stop(t, 22, now_ms=120_000)
    ev = T.process_candle(t, 23, 21.9, candle_ms=180_000)    # trailed stop
    assert t["status"] == "closed" and t["close_reason"] == "trailed stop"
    r = lambda p: (p - 14.19) / 2.19
    assert t["result_r"] == round(.5 * r(17) + .25 * r(19.5) + .25 * r(22), 2)

def test_stop_out_is_minus_one():
    t = mk()
    T.process_candle(t, 14.5, 11.9, candle_ms=1)
    assert t["status"] == "closed" and t["result_r"] == -1.0

def test_same_candle_stop_and_target_is_conservative():
    t = mk()
    T.process_candle(t, 18, 11, candle_ms=1)
    assert t["result_r"] == -1.0

def test_short_and_all_targets():
    t = mk("short", 100, 110, "90\n80")
    T.process_candle(t, 101, 79, candle_ms=1)
    assert t["status"] == "closed" and t["result_r"] == round(.5 * 1 + .5 * 2, 2)

def test_limit_fill():
    t = mk(status="pending", entry=14, stop=12, targets="17")
    assert T.process_candle(t, 15, 14.5, candle_ms=1) == [] and t["status"] == "pending"
    T.process_candle(t, 14.5, 13.9, candle_ms=2)
    assert t["status"] == "open"

def test_manual_partial_and_close():
    t = mk(targets="20")
    T.partial(t, 16.38, 50)                       # +1R on half
    T.manual_close(t, 14.19)                      # rest at BE
    assert t["result_r"] == 0.5

def test_fmt():
    assert T.fmt_price(0.00001234) == "$0.00001234"
    assert T.fmt_price(14.19) == "$14.19" and T.fmt_price(65432.1) == "$65,432.1"
