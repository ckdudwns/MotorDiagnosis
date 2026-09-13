"""Opt-in mixed-moment contract; synthetic input, no field-equivalence claim."""
import copy
import unittest
from unittest.mock import patch

from motor_diagnosis import data, edge_feature_snapshots as contract
from motor_diagnosis.pump_model_settings import RAW_CF_INPUT_MODE
from motor_diagnosis.pump_models import configured_model
from motor_diagnosis.snapshot_input import prepare_input
from motor_diagnosis.snapshot_model import SnapshotModelAdapter
from tests.test_pump_dual_models import DualServingTest, environment, DEVICE, SCOPE


def raw_model():
    env = environment()
    env['PUMP_DUAL_INPUT_MODE'] = RAW_CF_INPUT_MODE
    return configured_model(env)


class RawCfContractTest(unittest.TestCase):
    def test_explicit_mode_pins_distinct_contract_without_changing_artifacts(self):
        old, new = configured_model(environment()), raw_model()
        self.assertNotEqual(old.binding_id, new.binding_id)
        self.assertEqual(old.metadata()['modelVersion'], new.metadata()['modelVersion'])
        self.assertEqual(old.metadata()['forecastModel'], new.metadata()['forecastModel'])
        self.assertEqual(old._forecast_stream, new._forecast_stream)
        self.assertNotIn('meanRemoved', new.metadata()['inputContract'])
        self.assertEqual(new.metadata()['inputContract'], contract.RAW_CF_HISTORY_INPUT_CONTRACT)
        self.assertFalse(new.metadata()['sourceFeatureEquivalenceVerified'])
        for model, profile in ((old, contract.HISTORY_PROFILE_ID), (new, contract.RAW_CF_HISTORY_PROFILE_ID)):
            self.assertTrue(model.matches({**SCOPE, 'profileId': profile}))
        self.assertFalse(new.matches({**SCOPE, 'profileId': contract.HISTORY_PROFILE_ID}))
        self.assertFalse(old.matches({**SCOPE, 'profileId': contract.RAW_CF_HISTORY_PROFILE_ID}))

    def test_inconsistent_formulas_or_legacy_global_centering_rejected(self):
        metadata = raw_model()._metadata
        for change in (lambda c: c.update(meanRemoved=True),
                       lambda c: c['featureDefinitions'].update(cf='max(abs(centered))/sqrt(mean(centered**2))'),
                       lambda c: c.update(sourceProfileId=contract.HISTORY_PROFILE_ID)):
            m = copy.deepcopy(metadata)
            change(m['inputContract'])
            with self.assertRaises(ValueError):
                SnapshotModelAdapter(m, lambda *_: {'score': 0.})


class RawCfServingTest(DualServingTest):
    def setUp(self):
        super().setUp()
        self.model = raw_model()
        self.store.inference.model = self.model

    def payload(self, *args, **kwargs):
        p = super().payload(*args, **kwargs)
        p['window']['profileId'] = contract.RAW_CF_HISTORY_PROFILE_ID
        return p

    def test_prepare_preserves_all_nine_values_and_does_not_recompute_cf(self):
        p = self.payload(0)
        p['window']['features']['cf_a_1'] = 1.04855
        p['window']['integrity']['digest'] = contract.feature_digest(p['window']['features'])
        before = copy.deepcopy(p)
        prepared = prepare_input(p['window'])
        self.assertEqual(prepared['values'], [p['window']['features'][k] for k in contract.FEATURES])
        self.assertEqual(prepared['featureDefinitions'], contract.RAW_CF_HISTORY_INPUT_CONTRACT['featureDefinitions'])
        self.assertEqual(p, before)
        prepared['featureDefinitions']['cf'] = 'mutated'
        self.assertNotEqual(prepared, prepare_input(p['window']))

    def test_mismatch_is_stored_and_visible_but_never_sent_to_legacy_model(self):
        self.store.inference.model = configured_model(environment())
        ack, code = self.send(self.payload(0))
        self.process()
        listing = self.store.list_device(self.admin, DEVICE)
        self.assertEqual((code, ack['accepted']), (202, 1))
        self.assertFalse(listing['inferenceEnabled'])
        self.assertEqual(listing['inputAdapterId'], contract.RAW_CF_ADAPTER_ID)
        self.assertNotEqual(listing['modelCompatibility']['status'], 'ready')
        self.assertEqual(len(listing['items']), 1)
        self.assertEqual(listing['items'][0]['window']['features'], self.payload(0)['window']['features'])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM snapshot_inference_jobs').fetchone()[0], 0)

    def test_legacy_backlog_does_not_count_and_profile_switch_requires_new_boot(self):
        self.base -= 600
        for seq in range(24):
            old = self.payload(seq)
            old['window']['profileId'] = contract.HISTORY_PROFILE_ID
            self.send(old)
        self.process()
        with self.assertRaises(data.ApiError):
            self.send(self.payload(24))
        self.send(self.payload(24, boot='b'*32))
        self.process()
        result = self.latest()['analysis']
        self.assertEqual(result['reason'], 'VERIFIER_HISTORY_INSUFFICIENT')
        self.assertEqual(result['evidence']['historicalRecordsUsed'], 0)
        self.assertEqual(result['forecast']['reason'], 'FORECAST_HISTORY_INSUFFICIENT')
        for seq in range(25, 49):
            self.send(self.payload(seq, boot='b'*32))
        self.process()
        self.assertEqual(self.latest()['analysis']['status'], 'completed')
        self.assertEqual(self.latest()['analysis']['evidence']['historyOrdinals'], list(range(25, 49)))

    def test_forecast_guard_stays_enforced_with_raw_cf_profile(self):
        self.history(24)
        p = self.payload(24)
        p['window']['features']['cf_a_2'] = 1000.
        p['window']['integrity']['digest'] = contract.feature_digest(p['window']['features'])
        self.send(p)
        with patch('motor_diagnosis.pump_models.predict_features') as predictor:
            self.process()
            predictor.assert_not_called()
        a = self.latest()['analysis']
        self.assertEqual(a['status'], 'completed')
        self.assertEqual(a['forecast']['reason'], 'FORECAST_INPUT_OUT_OF_DISTRIBUTION')
        self.assertIsNone(a['forecast']['features'])
        self.assertFalse(a['forecast']['affectsAlerts'])


def load_tests(loader, tests, pattern):
    suite = loader.loadTestsFromTestCase(RawCfContractTest)
    # Re-run serving/ACK/retry/continuity/real API-to-UI cases for the v2 profile.
    for name in loader.getTestCaseNames(RawCfServingTest):
        suite.addTest(RawCfServingTest(name))
    return suite
