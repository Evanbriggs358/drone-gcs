"""Camera geometry tests.

The absolute values here are checked against hand calculation rather than
against the implementation, so a refactor that changes the answer fails loudly.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from gcs.planning.camera import PI_CAMERA_MODULE_3 as CAM


class TestGroundSampleDistance:
    def test_gsd_at_100m(self):
        # 100 m * 0.0014 mm / 4.74 mm = 0.029536 m/px
        assert CAM.gsd_m_per_px(100.0) == pytest.approx(0.029536, abs=1e-6)
        assert CAM.gsd_cm_per_px(100.0) == pytest.approx(2.954, abs=1e-3)

    def test_gsd_scales_linearly_with_altitude(self):
        assert CAM.gsd_m_per_px(200.0) == pytest.approx(2 * CAM.gsd_m_per_px(100.0))

    def test_altitude_for_gsd_round_trips(self):
        altitude = CAM.altitude_for_gsd(2.0)
        assert CAM.gsd_cm_per_px(altitude) == pytest.approx(2.0)

    def test_rejects_nonpositive_altitude(self):
        with pytest.raises(ValueError, match="altitude_m"):
            CAM.gsd_m_per_px(0.0)


class TestFootprint:
    def test_footprint_at_100m(self):
        across, along = CAM.footprint_m(100.0)
        assert across == pytest.approx(136.1, abs=0.1)
        assert along == pytest.approx(76.6, abs=0.1)

    def test_long_axis_across_track_widens_the_swath(self):
        rotated = replace(CAM, long_axis_across_track=False)
        assert rotated.footprint_m(100.0)[0] < CAM.footprint_m(100.0)[0]

    def test_field_of_view_matches_datasheet(self):
        # Datasheet quotes ~66 deg horizontal; the thin-lens model gives 68.5.
        assert CAM.hfov_deg == pytest.approx(68.5, abs=0.5)
        assert CAM.vfov_deg < CAM.hfov_deg


class TestSurveySpacing:
    def test_spacing_at_70_percent_overlap(self):
        assert CAM.photo_spacing_m(100.0, 0.70) == pytest.approx(22.97, abs=0.05)
        assert CAM.line_spacing_m(100.0, 0.70) == pytest.approx(40.83, abs=0.05)

    def test_zero_overlap_spacing_equals_footprint(self):
        across, along = CAM.footprint_m(100.0)
        assert CAM.photo_spacing_m(100.0, 0.0) == pytest.approx(along)
        assert CAM.line_spacing_m(100.0, 0.0) == pytest.approx(across)

    def test_higher_overlap_tightens_spacing(self):
        assert CAM.photo_spacing_m(100.0, 0.80) < CAM.photo_spacing_m(100.0, 0.70)

    @pytest.mark.parametrize("bad", [-0.1, 1.0, 1.5])
    def test_rejects_out_of_range_overlap(self, bad):
        with pytest.raises(ValueError, match="front_overlap"):
            CAM.photo_spacing_m(100.0, bad)

    def test_the_old_hardcoded_10m_trigger_was_oversampling(self):
        """Regression guard for the finding that motivated this rewrite.

        capture_daemon.py fired every 10 m regardless of altitude. At 100 m the
        correct spacing for 70% front overlap is ~23 m, so the old system shot
        well over twice as many photos as the survey needed.
        """
        assert CAM.photo_spacing_m(100.0, 0.70) > 2 * 10.0


class TestFlightConstraints:
    def test_max_ground_speed_falls_as_altitude_drops(self):
        assert CAM.max_ground_speed_ms(50.0, 0.70) < CAM.max_ground_speed_ms(100.0, 0.70)

    def test_fast_shutter_keeps_blur_subpixel(self):
        # 1/2000 s at 8 m/s, the configuration the capture daemon already used.
        assert CAM.motion_blur_px(100.0, 8.0, 1 / 2000) < 1.0

    def test_slow_shutter_smears(self):
        assert CAM.motion_blur_px(100.0, 8.0, 1 / 60) > 1.0
