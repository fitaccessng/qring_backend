from __future__ import annotations

import unittest

from app.services.payment_service import ALL_FEATURE_FLAGS, DEFAULT_PLAN_CATALOG


class PlanFeatureCatalogTests(unittest.TestCase):
    def test_plan_catalog_features_are_in_global_feature_flags(self):
        advertised_features = {
            feature
            for plan in DEFAULT_PLAN_CATALOG
            for feature in plan.get("enabledFeatures", [])
        }
        restricted_features = {
            feature
            for plan in DEFAULT_PLAN_CATALOG
            for feature in plan.get("restrictions", [])
        }

        self.assertLessEqual(advertised_features | restricted_features, ALL_FEATURE_FLAGS)


if __name__ == "__main__":
    unittest.main()
