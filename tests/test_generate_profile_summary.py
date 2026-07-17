import unittest
from collections import OrderedDict
from datetime import date, timedelta
from xml.etree import ElementTree

from scripts.generate_profile_summary import calculate_metrics, render_svg, shift_month


class ProfileSummaryTests(unittest.TestCase):
    def test_shift_month_crosses_year_boundary(self) -> None:
        self.assertEqual(shift_month(date(2026, 1, 1), -2), date(2025, 11, 1))

    def test_metrics_count_activity_and_longest_streak(self) -> None:
        start = date(2026, 1, 1)
        counts = [1, 2, 0, 3, 4, 5, 0]
        days = {start + timedelta(days=index): count for index, count in enumerate(counts)}

        metrics = calculate_metrics(days, start, start + timedelta(days=6))

        self.assertEqual(metrics.total, 15)
        self.assertEqual(metrics.active_days, 5)
        self.assertEqual(metrics.longest_streak, 3)
        self.assertEqual(metrics.monthly, OrderedDict([("2026-01", 15)]))

    def test_rendered_cards_are_valid_svg_without_error_text(self) -> None:
        metrics = calculate_metrics(
            {date(2026, 1, 1): 2, date(2026, 1, 2): 3},
            date(2026, 1, 1),
            date(2026, 1, 2),
        )

        for dark in (False, True):
            svg = render_svg("example", metrics, date(2026, 1, 2), dark)
            root = ElementTree.fromstring(svg)
            self.assertTrue(root.tag.endswith("svg"))
            self.assertNotIn("ERROR", svg)
            self.assertIn("5 contributions", svg)


if __name__ == "__main__":
    unittest.main()
