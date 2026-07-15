from maxgaffer.core.colortemp import kelvin_to_rgb, wb_color_for_kelvin


def test_kelvin_to_rgb_endpoints():
    warm = kelvin_to_rgb(2500)
    cool = kelvin_to_rgb(12000)
    assert warm[0] > warm[2]            # candlelight is red-heavy
    assert cool[2] > cool[0]            # skylight is blue-heavy
    r, g, b = kelvin_to_rgb(6600)       # near-neutral
    assert abs(r - g) < 0.15 and abs(g - b) < 0.15
    assert kelvin_to_rgb(500) == kelvin_to_rgb(1000)      # clamped low
    assert kelvin_to_rgb(99000) == kelvin_to_rgb(40000)   # clamped high


def test_wb_swatch_is_the_illuminant_color():
    """Spinner K and swatch must be the SAME convention: higher K → bluer swatch →
    the camera divides out blue → the image renders warmer (matches the solver)."""
    warmer_image = wb_color_for_kelvin(9000)   # blue-ish swatch
    cooler_image = wb_color_for_kelvin(3600)   # orange-ish swatch
    assert warmer_image[2] > warmer_image[0] * 0.9      # blue-leaning
    assert cooler_image[0] > cooler_image[2]            # orange-leaning
    assert warmer_image[2] > cooler_image[2]


def test_monotone_blue_channel():
    blues = [wb_color_for_kelvin(k)[2] for k in (2500, 4000, 6500, 9000, 12000)]
    assert blues == sorted(blues)
