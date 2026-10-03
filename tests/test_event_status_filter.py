"""Open-incident filters operate before pagination and retain site authorization."""
from unittest import mock

from motor_diagnosis import data, server
from tests.test_measured_rpm import RpmSetup


class EventStatusFilterTest(RpmSetup):
    def setUp(self):
        super().setUp()
        events = [
            {"id": "open-old", "siteId": "SITE-01", "assetId": "SITE-01-MOT-02", "status": "open", "occurredAt": "2026-01-01T00:00:00Z"},
            {"id": "closed-new", "siteId": "SITE-01", "assetId": "SITE-01-MOT-02", "status": "closed", "occurredAt": "2026-10-03T00:00:00Z"},
            {"id": "open-new", "siteId": "SITE-01", "assetId": "SITE-01-MOT-02", "status": "open", "occurredAt": "2026-10-02T00:00:00Z"},
            {"id": "another-site", "siteId": "SITE-02", "assetId": "SITE-02-MOT-02", "status": "open", "occurredAt": "2026-10-03T00:00:00Z"},
        ]
        patcher = mock.patch.object(server, "EVENTS", events)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.query = {"siteId": ["SITE-01"], "assetId": ["SITE-01-MOT-02"], "sort": ["occurredAt_desc"]}

    def test_status_filter_is_applied_before_total_and_pagination(self):
        result = server.paginated_events(self.admin, {**self.query, "status": ["open"], "size": ["1"], "page": ["2"]})
        self.assertEqual(result["total"], 2)
        self.assertEqual([row["id"] for row in result["items"]], ["open-old"])
        closed = server.paginated_events(self.admin, {**self.query, "status": ["closed"]})
        self.assertEqual([row["id"] for row in closed["items"]], ["closed-new"])
        self.assertEqual(server.paginated_events(self.admin, self.query)["total"], 3)
        self.assertEqual(server.paginated_events(self.admin, {**self.query, "status": [""]})["total"], 3)

    def test_unknown_status_rejected_and_permissions_preserved(self):
        with self.assertRaises(data.ApiError) as error:
            server.paginated_events(self.admin, {**self.query, "status": ["unknown"]})
        self.assertEqual((error.exception.status, error.exception.code), (400, "INVALID_STATUS"))
        with self.assertRaises(data.ApiError) as error:
            server.paginated_events({**self.admin, "allowedSiteIds": ["SITE-02"]}, {**self.query, "status": ["open"]})
        self.assertEqual(error.exception.code, "SITE_FORBIDDEN")
        with mock.patch.object(data, "role_policy", return_value={"permissions": []}), self.assertRaises(data.ApiError) as error:
            server.paginated_events(self.admin, {**self.query, "status": ["open"]})
        self.assertEqual(error.exception.status, 403)
