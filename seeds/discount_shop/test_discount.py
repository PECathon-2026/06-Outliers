from discount import apply_discount

def test_percent_off():
    assert apply_discount(200, 10) == 180

def test_zero_discount():
    assert apply_discount(50, 0) == 50
