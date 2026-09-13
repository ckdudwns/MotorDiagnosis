"""Pinned v3 guards; synthetic cases, never production ingest or field metrics."""
import copy
import hashlib
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from motor_diagnosis.pump_models import (read_forecast, forecast_vector,
    forecast_input_check, forecast_output_valid, predict_features)
from tests.test_pump_dual_models import FORECAST, FORECAST_HASH, STREAM, DualServingTest


class ForecastGuardTest(unittest.TestCase):
    def setUp(self):
        self.artifact = read_forecast(FORECAST, FORECAST_HASH)
        self.stream = self.artifact['streams'][STREAM]

    def test_artifact_is_v3_with_guard_limits_for_both_streams(self):
        self.assertEqual(self.artifact['modelType'], 'pump-summary-experiment-v3')
        for stream in self.artifact['streams'].values():
            self.assertGreater(stream['forecast']['inputRobustLimit'], 0)
            self.assertEqual(len(stream['forecast']['targetLower']), 9)

    def test_invalid_guard_artifacts_fail_closed(self):
        def forecast(m): return m['streams'][STREAM]['forecast']
        changes = [lambda m: forecast(m).update(inputRobustLimit=0),
                   lambda m: forecast(m).update(inputRobustLimit=True),
                   lambda m: forecast(m).update(targetLower=[0]*8),
                   lambda m: forecast(m)['targetUpper'].__setitem__(0, -1),
                   lambda m: forecast(m)['targetLower'].__setitem__(0, float('nan')),
                   lambda m: m['streams'][STREAM].update(active=[]),
                   lambda m: m['streams'][STREAM].update(active=[True]),
                   lambda m: m['streams'][STREAM].update(active=[1,1]),
                   lambda m: m['streams'][STREAM].update(active=[36]),
                   lambda m: m['streams'][STREAM]['scale'].__setitem__(1, 0),
                   lambda m: m['streams'][STREAM].update(center=[0]*35)]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'model.json'
            for change in changes:
                model=copy.deepcopy(self.artifact); change(model)
                # Temporary malformed artifact, not the pinned source.
                raw=json.dumps(model).encode(); path.write_bytes(raw)
                with self.subTest(change=change), self.assertRaises(ValueError):
                    read_forecast(path, hashlib.sha256(raw).hexdigest())

    def test_guard_uses_forecast_active_mad_scales_not_ridge_standard_deviations(self):
        history=[[2.]*9 for _ in range(13)]
        vector=forecast_vector(history)
        stream=copy.deepcopy(self.stream)
        stream.update(active=[0],center=list(vector),scale=[1.]*36)
        stream['center'][0]=0.; stream['scale'][0]=2.
        stream['forecast']['inputRobustLimit']=1.
        self.assertEqual(forecast_input_check(stream,history),(1.,1.))
        stream['center'][0]=-math.ulp(2.)
        self.assertGreater(forecast_input_check(stream,history)[0],1.)

    def test_output_bounds_are_inclusive_and_never_clipped(self):
        f=self.stream['forecast']
        for key in ('targetLower','targetUpper'):
            self.assertTrue(forecast_output_valid(f,list(f[key])))
        for index in range(9):
            for key,direction in [('targetLower',-math.inf),('targetUpper',math.inf)]:
                values=list(f['targetMean'])
                values[index]=math.nextafter(f[key][index],direction)
                original=list(values)
                self.assertFalse(forecast_output_valid(f,values))
                self.assertEqual(values,original)
        for value in (True,float('nan'),float('inf')):
            values=list(f['targetMean']);values[0]=value
            self.assertFalse(forecast_output_valid(f,values))

    def test_real_ridge_can_pass_both_guards_for_synthetic_in_range_history(self):
        # Synthetic contract check, not a field performance measurement.
        means=[1.045,1.1,1.03,0.,-.01,0.,3.,3.,3.]
        history=[[m+math.sqrt(2)*self.stream['center'][18+j]*
                  math.sin(2*math.pi*i/13+j) for j,m in enumerate(means)]
                 for i in range(13)]
        robust,limit=forecast_input_check(self.stream,history)
        self.assertLessEqual(robust,limit)
        output=predict_features(self.stream['forecast'],history)
        self.assertEqual(len(output),9)
        self.assertTrue(forecast_output_valid(self.stream['forecast'],output))

    def test_physical_cf_and_pearson_limits_cannot_be_relaxed_by_artifact_bounds(self):
        f=copy.deepcopy(self.stream['forecast'])
        f.update(targetLower=[-100.]*9,targetUpper=[100.]*9)
        for index in (0,1,2,6,7,8):
            values=[2.,2.,2.,0.,0.,0.,3.,3.,3.];values[index]=.9
            self.assertFalse(forecast_output_valid(f,values))

    def test_observed_input_diagnostic_is_rejected_without_modifying_coefficients(self):
        # One observed input repeated 13 times: diagnostic, NOT real-history replay.
        values=[3.42549,4.374991,3.210389,-.292914,-.370362,-.250972,3.35174,3.975669,2.790582]
        history=[values]*13
        before=copy.deepcopy(self.stream)
        robust,limit=forecast_input_check(self.stream,history)
        self.assertGreater(robust,limit)
        output=predict_features(self.stream['forecast'],history)
        self.assertTrue(any(v<0 for v in output[6:]))
        self.assertFalse(forecast_output_valid(self.stream['forecast'],output))
        self.assertEqual(self.stream,before)


class ForecastGuardServingTest(DualServingTest):
    # Reuse test setup helpers without re-running the inherited lifecycle suite.
    def test_guard_blocks_prediction_but_keeps_verifier_and_durable_ack(self):
        self.history(24)
        p=self.payload(24)
        p['window']['features']['cf_a_2']=1000.
        from motor_diagnosis.edge_feature_snapshots import feature_digest
        p['window']['integrity']['digest']=feature_digest(p['window']['features'])
        ack,code=self.send(p)
        with patch('motor_diagnosis.pump_models.predict_features') as predictor:
            self.process();predictor.assert_not_called()
        a=self.latest()['analysis']
        self.assertEqual((code,ack['accepted'],a['status']),(202,1,'completed'))
        self.assertEqual(a['forecast']['reason'],'FORECAST_INPUT_OUT_OF_DISTRIBUTION')
        self.assertIsNone(a['forecast']['features'])
        self.assertFalse(a['forecast']['affectsAlerts'])
        before=copy.deepcopy(self.latest())
        ack,code=self.send(p);self.process()
        self.assertEqual((code,ack['accepted']),(200,0))
        self.assertEqual(self.latest(),before)

    def test_out_of_range_prediction_stays_null_and_verifier_finishes(self):
        self.history(24);self.send(self.payload(24))
        with patch('motor_diagnosis.pump_models.predict_features',return_value=[-1.]*9):
            self.process()
        a=self.latest()['analysis']
        self.assertEqual(a['status'],'completed')
        self.assertEqual(a['forecast']['reason'],'FORECAST_OUTPUT_OUT_OF_RANGE')
        self.assertIsNone(a['forecast']['features'])
        self.assertFalse(a['forecast']['guard']['outputClipped'])


# Only the two new methods here; inherited cases run in test_pump_dual_models.
def load_tests(loader, tests, pattern):
    suite=loader.loadTestsFromTestCase(ForecastGuardTest)
    for name in ForecastGuardServingTest.__dict__:
        if name.startswith('test_'):
            suite.addTest(ForecastGuardServingTest(name))
    return suite
