from paperwhisper.calibreweb import _as_frac


def test_as_frac_accepts_percent_or_fraction():
    assert _as_frac(0.412) == 0.412
    assert abs(_as_frac(41.2) - 0.412) < 1e-9
    assert _as_frac(0) == 0.0
    assert _as_frac(100) == 1.0
    assert _as_frac(150) == 1.0
