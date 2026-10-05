"""Offline contract tests: no Apple credentials or network required."""
import tempfile
import json
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from reminders_worker import Worker, Failure, date_value

class Model(SimpleNamespace):
    def model_dump(self, **_):
        return {key: value.isoformat() if isinstance(value, datetime) else value for key, value in vars(self).items()}

class Fake:
    def __init__(self):
        self.calls = 0
        self.item = Model(id="r1", list_id="l1", title="Original", desc="Keep notes", time_zone="America/Los_Angeles", record_change_tag="v1", deleted=False, completed=False, due_date=None, alarm_ids=[])
    def get(self, _): return self.item
    def create(self, **args):
        self.calls += 1
        for key, value in args.items(): setattr(self.item, key, value)
        return self.item
    def update(self, item): self.calls += 1
    def delete(self, item):
        self.calls += 1
        item.deleted = True

class Tests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.service = Fake()
        self.worker = Worker(self.directory.name, "unused", "America/Los_Angeles", self.service)
    def test_create_receipt_survives_worker_restart(self):
        args = {"list_id": "l1", "title": "Test", "due_date": "2026-10-11", "idempotency_key": "one"}
        result = self.worker.call("create_reminder", args)
        other = Worker(self.directory.name, "unused", "UTC", self.service)
        self.assertEqual(result, other.call("create_reminder", args))
        self.assertEqual(1, self.service.calls)
        self.assertTrue(result["all_day"])
        self.assertEqual("2026-10-11T00:00:00-07:00", result["due_date"])
        with self.assertRaises(Failure): other.call("create_reminder", {**args, "title": "Different"})
    def test_patch_preserves_notes_and_clear_date(self):
        self.worker.call("update_reminder", {"reminder_id": "r1", "title": "Edited", "due_date": "", "idempotency_key": "patch"})
        self.assertEqual("Keep notes", self.service.item.desc)
        self.assertIsNone(self.service.item.due_date)
    def test_precondition_before_dispatch(self):
        with self.assertRaises(Failure) as caught:
            self.worker.call("update_reminder", {"reminder_id": "r1", "title": "Edited", "record_change_tag": "old", "idempotency_key": "conflict"})
        self.assertEqual("conflict", caught.exception.code)
        self.assertEqual(0, self.service.calls)
    def test_uncertain_write_never_redispatched(self):
        def fail(**_):
            self.service.calls += 1
            raise RuntimeError("secret upstream response")
        self.service.create = fail
        args = {"list_id": "l1", "title": "Test", "idempotency_key": "uncertain"}
        for _ in range(2):
            with self.assertRaises(Failure) as caught: self.worker.call("create_reminder", args)
            self.assertEqual("outcome_unknown", caught.exception.code)
            self.assertNotIn("secret", caught.exception.message)
        self.assertEqual(1, self.service.calls)
    def test_complete_reopen_delete(self):
        for completed in (True, False):
            self.worker.call("complete_reminder", {"reminder_id": "r1", "completed": completed, "idempotency_key": str(completed)})
            self.assertEqual(completed, self.service.item.completed)
        result = self.worker.call("delete_reminder", {"reminder_id": "r1", "idempotency_key": "delete"})
        self.assertTrue(result["deleted"])
    def test_dst_dates(self):
        self.assertEqual(-7*3600, date_value("2026-10-11", "America/Los_Angeles").utcoffset().total_seconds())
        self.assertEqual(-8*3600, date_value("2026-12-11", "America/Los_Angeles").utcoffset().total_seconds())


# These exercise the pinned SDK request models as well as our alarm logic.
import importlib.util
import base64
from datetime import timezone, timedelta
from unittest.mock import patch
from reminders_worker import sync_date_alarm, component_date

@unittest.skipUnless(importlib.util.find_spec('pyicloud'), 'Pinned pyicloud venv required')
class DateAlarmTests(unittest.TestCase):
    def setUp(self):
        from pyicloud.common.cloudkit import CKRecord
        from pyicloud.services.reminders._writes import RemindersWriteAPI
        from pyicloud.services.reminders._protocol import _generate_resolution_token_map
        self.Record = CKRecord
        self.due = date_value('2026-10-11T10:00:00', 'America/Los_Angeles')
        self.item = Model(id='Reminder/test', due_date=self.due, all_day=False,
                          time_zone='America/Los_Angeles', alarm_ids=[])
        self.records = {'Reminder/test': CKRecord(recordName='Reminder/test', recordType='Reminder', recordChangeTag='v1', fields={
            'ResolutionTokenMap': {'type':'STRING', 'value':json.dumps({'map':{'dueDate':{'counter':3,'modificationTime':1234,'replicaID':'fixture'}}})}})}
        self.batches = []
        def lookup(record_names, **_):
            return SimpleNamespace(records=[self.records[key] for key in record_names])
        def modify(operations, atomic, **_):
            self.assertTrue(atomic)
            self.batches.append(operations)
            for op in operations:
                payload = op.record.model_dump(mode='json', exclude_none=True)
                name = payload['recordName']
                old = self.records.get(name)
                fields = dict(old.model_dump(mode='json')['fields']) if old else {}
                fields.update(payload['fields'])
                self.records[name] = CKRecord(recordName=name, recordType=payload['recordType'], recordChangeTag='v2', fields=fields)
                if name == self.item.id:
                    self.item.alarm_ids = self.records[name].fields.get_value('AlarmIDs')
            return SimpleNamespace(records=[])
        self.service = SimpleNamespace(_raw=SimpleNamespace(lookup=lookup,modify=modify),
            _writes=SimpleNamespace(_write_record=RemindersWriteAPI._write_record),
            _generate_resolution_token_map=_generate_resolution_token_map)
    def test_native_date_alarm_and_nonce_are_linked_atomically(self):
        sync_date_alarm(self.service, self.item, None, True)
        self.assertEqual(1,len(self.item.alarm_ids))
        alarm = next(r for r in self.records.values() if r.recordType=='Alarm')
        trigger = next(r for r in self.records.values() if r.recordType=='AlarmTrigger')
        self.assertEqual(300000001234,alarm.fields.get_value('DueDateResolutionTokenAsNonce'))
        self.assertEqual('Date',trigger.fields.get_value('Type'))
        self.assertEqual(self.due,component_date(trigger))
        token=json.loads(self.records[self.item.id].fields.get_value('ResolutionTokenMap'))
        self.assertEqual(3,token['map']['dueDate']['counter'])
        self.assertEqual(3,len(self.batches[0]))
        self.assertEqual("Reminder",self.batches[0][0].record.recordType)
    def test_edit_reuses_alarm_and_clear_removes_it(self):
        sync_date_alarm(self.service,self.item,None,True)
        first_id=self.item.alarm_ids[0]
        original=self.due
        self.item.due_date += timedelta(hours=1)
        sync_date_alarm(self.service,self.item,original,True)
        self.assertEqual([first_id],self.item.alarm_ids)
        trigger=next(r for r in self.records.values() if r.recordType=='AlarmTrigger')
        self.assertEqual(self.item.due_date,component_date(trigger))
        old=self.item.due_date
        self.item.due_date=None
        sync_date_alarm(self.service,self.item,old,True)
        self.assertEqual([],self.item.alarm_ids)
        self.assertTrue(self.records['Alarm/'+first_id].fields.get_value('Deleted'))
    def test_title_edit_preserves_trigger_and_refreshes_nonce(self):
        sync_date_alarm(self.service,self.item,None,True)
        trigger=next(r for r in self.records.values() if r.recordType=='AlarmTrigger')
        initial=trigger.fields.get_value('DateComponentsData')
        parent=self.records[self.item.id]
        fields=dict(parent.model_dump(mode='json')['fields'])
        fields['ResolutionTokenMap']={'type':'STRING','value':json.dumps({'map':{'dueDate':{'counter':4,'modificationTime':5678}}})}
        self.records[self.item.id]=self.Record(recordName=parent.recordName,recordType='Reminder',recordChangeTag='v3',fields=fields)
        sync_date_alarm(self.service,self.item,self.due,False)
        alarm=next(r for r in self.records.values() if r.recordType=='Alarm')
        self.assertEqual(400000005678,alarm.fields.get_value('DueDateResolutionTokenAsNonce'))
        self.assertEqual(initial,self.records[trigger.recordName].fields.get_value('DateComponentsData'))

    def test_location_alarm_is_preserved(self):
        self.item.alarm_ids = ['location']
        self.records['Alarm/location'] = self.Record(recordName='Alarm/location', recordType='Alarm', recordChangeTag='v1', fields={
            'TriggerID':{'type':'STRING','value':'location-trigger'},
            'DueDateResolutionTokenAsNonce':{'type':'DOUBLE','value':0}})
        self.records['AlarmTrigger/location-trigger'] = self.Record(recordName='AlarmTrigger/location-trigger', recordType='AlarmTrigger', recordChangeTag='v1', fields={
            'Type':{'type':'STRING','value':'Location'}})
        sync_date_alarm(self.service,self.item,None,True)
        self.assertIn('location',self.item.alarm_ids)
        self.assertEqual('Location',self.records['AlarmTrigger/location-trigger'].fields.get_value('Type'))
        self.assertFalse(any(op.record.recordName=='AlarmTrigger/location-trigger' for op in self.batches[0]))


class AlarmReceiptTests(unittest.TestCase):
    setUp = Tests.setUp
    def test_alarm_failure_after_create_blocks_duplicate_creation(self):
        args={'list_id':'l1','title':'Test','due_date':'2026-10-11T10:00:00','idempotency_key':'partial'}
        with patch('reminders_worker.sync_date_alarm',side_effect=RuntimeError('failed alarm')):
            for _ in range(2):
                with self.assertRaises(Failure) as caught:self.worker.call('create_reminder',args)
                self.assertEqual('outcome_unknown',caught.exception.code)
        self.assertEqual(1,self.service.calls)

if __name__ == "__main__": unittest.main()

@unittest.skipUnless(importlib.util.find_spec('pyicloud'), 'Pinned pyicloud venv required')
class RecurrenceTests(Tests):
    def setUp(self):
        super().setUp()
        self.rules=[]
        self.service.item.recurrence_rule_ids=[]
        self.service.recurrence_rules_for=lambda item: self.rules
        def create(item, **values):
            self.rules.append(Model(id='rule', **values))
            item.recurrence_rule_ids=['rule']
        def update(rule, **values):
            for key,value in values.items(): setattr(rule,key,value)
        def delete(item,rule):
            self.rules.remove(rule)
            item.recurrence_rule_ids=[]
        self.service.create_recurrence_rule=create
        self.service.update_recurrence_rule=update
        self.service.delete_recurrence_rule=delete
    def test_repeat_lifecycle_and_preservation(self):
        args={'list_id':'l1','title':'Repeat','due_date':'2026-10-11','repeat_frequency':'weekly','repeat_interval':2,'idempotency_key':'repeat'}
        result=self.worker.call('create_reminder',args)
        self.assertEqual(2,result['recurrence_rules'][0]['interval'])
        self.worker.call('create_reminder',args)
        self.assertEqual(1,len(self.rules))
        self.worker.call('update_reminder',{'reminder_id':'r1','title':'Keep repeats','idempotency_key':'title'})
        self.assertEqual(2,self.rules[0].interval)
        self.worker.call('update_reminder',{'reminder_id':'r1','repeat_frequency':'monthly','idempotency_key':'monthly'})
        self.assertEqual(3,self.rules[0].frequency)
        self.assertEqual(1,self.rules[0].interval)
        result=self.worker.call('update_reminder',{'reminder_id':'r1','repeat_frequency':'none','idempotency_key':'none'})
        self.assertEqual([],result['recurrence_rules'])
    def test_invalid_repeat_before_write(self):
        for values in ({'repeat_frequency':'daily'}, {'repeat_interval':2}, {'repeat_frequency':'invalid'}, {'repeat_frequency':'weekly','repeat_interval':0}):
            with self.assertRaises(Failure):
                self.worker.call('create_reminder',{'list_id':'l1','title':'Invalid','idempotency_key':str(values),**values})
        self.assertEqual(0,self.service.calls)
    def test_uncertain_repeat_not_redispatched(self):
        def fail(*args,**kwargs): raise RuntimeError('private error')
        self.service.create_recurrence_rule=fail
        args={'list_id':'l1','title':'Repeat','due_date':'2026-10-11','repeat_frequency':'weekly','idempotency_key':'failure'}
        for _ in range(2):
            with self.assertRaises(Failure) as error: self.worker.call('create_reminder',args)
            self.assertEqual('outcome_unknown',error.exception.code)
        self.assertEqual(1,self.service.calls)
