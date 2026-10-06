import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tools.research_handoff.core.research_time import audit_session


class SessionCreditTests(unittest.TestCase):
    def audit(self, rows, **kw):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'session.jsonl'
            path.write_text('\n'.join(json.dumps({'timestamp': datetime.fromtimestamp(at, timezone.utc).isoformat(),
                'type': event, 'payload': payload}) for at,event,payload in rows) + '\n')
            return audit_session(path, feedback_reader=lambda output: [json.loads(output)] if output == '{"feedback":true}' else [], **kw)

    def test_interrupted_credit_requires_completed_tool_and_feedback(self):
        rows = [(100, 'event_msg', {'type':'task_started'}),
                (110, 'response_item', {'type':'function_call','call_id':'a','name':'exec'}),
                (140, 'response_item', {'type':'function_call_output','call_id':'a','output':'{"feedback":true}'}),
                (150, 'response_item', {'type':'function_call','call_id':'b','name':'exec'}),
                (180, 'event_msg', {'type':'token_count'})]
        self.assertEqual(self.audit(rows)['credited_seconds'], 0)
        result = self.audit(rows, allow_partial=True)
        self.assertEqual(result['intervals'], [[100,150]])
        self.assertTrue(result['partial'])
        rows[2][2]['output'] = 'No GPU feedback'
        self.assertEqual(self.audit(rows, allow_partial=True)['credited_seconds'], 0)

    def test_idle_and_infrastructure_intervals_are_excluded(self):
        rows = [(100, 'event_msg', {'type':'turn_started'}),
                (110, 'response_item', {'type':'function_call','call_id':'a'}),
                (120, 'response_item', {'type':'function_call_output','call_id':'a','output':'{"feedback":true}'}),
                (500, 'response_item', {'type':'function_call','call_id':'b'}),
                (550, 'response_item', {'type':'function_call_output','call_id':'b','output':'ModuleNotFoundError'}),
                (600, 'event_msg', {'type':'turn_complete'})]
        self.assertEqual(self.audit(rows)['intervals'], [[100,120],[550,600]])

    def test_method_only_is_explicit_and_keeps_feedback_false(self):
        rows = [(100, 'event_msg', {'type':'task_started'}),
                (110, 'response_item', {'type':'function_call','call_id':'a','name':'exec'}),
                (140, 'response_item', {'type':'function_call_output','call_id':'a','output':'method edited'}),
                (150, 'response_item', {'type':'function_call','call_id':'b','name':'exec'}),
                (180, 'event_msg', {'type':'token_count'})]
        self.assertEqual(self.audit(rows, allow_partial=True)['credited_seconds'], 0)
        result = self.audit(rows, allow_partial=True, method_reader=lambda call, output: output == 'method edited')
        self.assertEqual(result['intervals'], [[110,140]])
        self.assertFalse(result['has_real_feedback'])
        self.assertTrue(result['has_method_evidence'])


if __name__ == '__main__':
    unittest.main()
