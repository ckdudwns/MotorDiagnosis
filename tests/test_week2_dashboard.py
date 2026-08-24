from __future__ import annotations

import unittest

from motor_diagnosis.web import render_page


class Week2DashboardTest(unittest.TestCase):
    def test_chart_handles_empty_and_single_point_states(self) -> None:
        page = render_page()

        self.assertIn("No telemetry is available for the selected period.", page)
        self.assertIn("if (values.length === 1)", page)
        self.assertIn("ctx.arc(x, y, 5, 0, Math.PI * 2)", page)

    def test_event_query_uses_the_same_selection_conditions_as_chart(self) -> None:
        page = render_page()

        self.assertIn("/api/events?siteId=${site.id}&assetId=${assetId}&from=", page)

    def test_site_summary_displays_all_operational_fields(self) -> None:
        page = render_page()

        for text in (
            "<th>Region</th>",
            "site.normalAssets",
            "site.warningAssets",
            "site.criticalAssets",
            "site.unreviewedEvents",
            "site.lastReceivedAt",
        ):
            with self.subTest(text=text):
                self.assertIn(text, page)

    def test_event_display_uses_occurred_at_as_the_single_time_source(self) -> None:
        page = render_page()

        self.assertIn("formatLocalTime(event.occurredAt)", page)
        self.assertIn("function formatLocalTime(timestamp)", page)
        self.assertNotIn("event.time", page)

    def test_event_selection_is_cleared_when_filter_removes_it(self) -> None:
        page = render_page()

        self.assertIn("if (!events.some(event => event.id === selectedEventId))", page)
        self.assertIn("function clearEventSelection()", page)
        self.assertIn('$("saveReview").disabled = true', page)
