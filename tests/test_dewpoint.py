import pytest

from localhome.services.dewpoint import dew_point_c


def test_matches_a_well_known_reference_value():
    # 20°C / 50% RH is a commonly cited textbook example - Magnus-Tetens
    # gives ~9.3°C, matching the standard "quick doubling" rule of thumb
    # used to sanity-check this kind of calculation by hand.
    assert dew_point_c(20.0, 50.0) == pytest.approx(9.3, abs=0.1)


def test_at_100_percent_humidity_dew_point_equals_air_temperature():
    # Saturated air (RH=100%) is air already at its dew point by
    # definition - true for any temperature, not just this one, since
    # it falls out of the formula algebraically (ln(1) == 0).
    assert dew_point_c(19.5, 100.0) == pytest.approx(19.5, abs=1e-9)


def test_higher_humidity_at_the_same_temperature_raises_the_dew_point():
    assert dew_point_c(24.0, 70.0) > dew_point_c(24.0, 40.0)
