from users import connect, find_user

def test_find_existing():
    assert find_user(connect(), "ana") == [("ana", "ana@x.io")]

def test_injection_returns_nothing():
    assert find_user(connect(), "x' OR '1'='1") == []
