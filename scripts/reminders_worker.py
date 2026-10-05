#!/usr/bin/env python3
"""Private JSON-lines worker. Never prompts or emits upstream errors/secrets."""
import argparse
import base64
import time
import uuid
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

class Failure(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message

MUTATIONS = {"create_reminder", "update_reminder", "complete_reminder", "delete_reminder"}
EDITABLE = {"title", "notes", "due_date", "all_day", "time_zone", "priority", "flagged", "completed", "parent_reminder_id"}

def date_value(value, tz):
    if value in (None, ""):
        return None
    value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value if value.tzinfo else value.replace(tzinfo=ZoneInfo(tz))

def save(path, data):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        json.dump(data, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)

def dump(model):
    return model.model_dump(mode="json")

def alarm_name(value, prefix):
    return value if value.startswith(prefix + "/") else prefix + "/" + value

def date_components(value, tz):
    local = value.astimezone(ZoneInfo(tz))
    return {"day": local.day, "minute": local.minute, "era": 1,
            "year": local.year, "timeZone": {"identifier": tz},
            "month": local.month, "hour": local.hour, "second": local.second}

def component_date(record):
    value = record.fields.get_value("DateComponentsData")
    data = json.loads(base64.b64decode(value) if isinstance(value, str) else value)
    tz = data.get("timeZone", {}).get("identifier", "UTC")
    return datetime(data["year"], data["month"], data["day"],
                    data.get("hour", 0), data.get("minute", 0),
                    data.get("second", 0), tzinfo=ZoneInfo(tz))

def alarm_snapshot(service, reminder):
    if not reminder.alarm_ids:
        return [], {}
    from pyicloud.services.reminders._reads import _REMINDERS_ZONE_REQ, _assert_read_success
    response = service._raw.lookup(record_names=[alarm_name(x, "Alarm") for x in reminder.alarm_ids], zone_id=_REMINDERS_ZONE_REQ)
    _assert_read_success(response.records, "Lookup notification alarms")
    alarms = [x for x in response.records if getattr(x, "recordType", None) == "Alarm" and not x.fields.get_value("Deleted")]
    names = [alarm_name(x.fields.get_value("TriggerID"), "AlarmTrigger") for x in alarms if x.fields.get_value("TriggerID")]
    triggers = {}
    if names:
        response = service._raw.lookup(record_names=names, zone_id=_REMINDERS_ZONE_REQ)
        _assert_read_success(response.records, "Lookup notification triggers")
        triggers = {x.recordName: x for x in response.records if getattr(x, "recordType", None) == "AlarmTrigger" and not x.fields.get_value("Deleted")}
    return alarms, triggers

def notification_rows(service, reminder):
    alarms, triggers = alarm_snapshot(service, reminder)
    rows = []
    for alarm in alarms:
        trigger_id = alarm.fields.get_value("TriggerID")
        trigger = triggers.get(alarm_name(trigger_id, "AlarmTrigger")) if trigger_id else None
        if trigger and trigger.fields.get_value("Type") == "Date":
            rows.append({"alarm_id": alarm.recordName, "at": component_date(trigger).isoformat()})
    return rows

def sync_date_alarm(service, reminder, old_due, schedule_changed):
    """Mirror the observed native Date alarm, preserving other alarm triggers."""
    from pyicloud.common.cloudkit import CKModifyOperation
    from pyicloud.services.reminders._reads import _REMINDERS_ZONE_REQ, _assert_read_success
    from pyicloud.services.reminders._writes import _assert_modify_success
    response = service._raw.lookup(record_names=[reminder.id], zone_id=_REMINDERS_ZONE_REQ)
    _assert_read_success(response.records, "Lookup reminder resolution token")
    parent = next(x for x in response.records if getattr(x, "recordName", None) == reminder.id)
    token_map = json.loads(parent.fields.get_value("ResolutionTokenMap") or '{"map":{}}')
    due_token = token_map.get("map", {}).get("dueDate")
    alarms, triggers = alarm_snapshot(service, reminder)
    if not due_token:
        raise Failure("alarm_error", "Reminder has no due-date resolution token; notification cannot be linked safely.")
    nonce = due_token["counter"] * 100_000_000_000 + due_token["modificationTime"]
    operations = []
    primary = None
    for alarm in alarms:
        trigger_id = alarm.fields.get_value("TriggerID")
        trigger = triggers.get(alarm_name(trigger_id, "AlarmTrigger")) if trigger_id else None
        if trigger and trigger.fields.get_value("Type") == "Date" and old_due is not None and component_date(trigger) == old_due.replace(microsecond=0):
            primary = (alarm, trigger)
            break
    def operation(kind, name, record_type, fields, tag=None, parent_id=None):
        operations.append(CKModifyOperation(operationType=kind, record=service._writes._write_record(
            record_name=name, record_type=record_type, fields=fields,
            record_change_tag=tag, parent_record_name=parent_id)))
    # A regular SDK update rewrites dueDate's resolution token even for a title
    # edit. Keep every existing alarm's nonce aligned without changing its trigger.
    for alarm in alarms:
        operation("update", alarm.recordName, "Alarm", {"DueDateResolutionTokenAsNonce": {"type": "DOUBLE", "value": nonce}}, alarm.recordChangeTag)
    ids = [x.split("/", 1)[-1] for x in reminder.alarm_ids]
    if schedule_changed:
        timed = reminder.due_date is not None and not reminder.all_day
        if primary and not timed:
            alarm, trigger = primary
            # Replace the nonce-only operation with a tombstone for this alarm.
            operations = [op for op in operations if op.record.recordName != alarm.recordName]
            operation("update", alarm.recordName, "Alarm", {"Deleted": {"type": "INT64", "value": 1}}, alarm.recordChangeTag)
            operation("update", trigger.recordName, "AlarmTrigger", {"Deleted": {"type": "INT64", "value": 1}}, trigger.recordChangeTag)
            ids.remove(alarm.recordName.split("/", 1)[-1])
        elif timed:
            tz = reminder.time_zone or "UTC"
            components = base64.b64encode(json.dumps(date_components(reminder.due_date, tz), separators=(",", ":")).encode()).decode()
            if primary:
                alarm, trigger = primary
                operation("update", trigger.recordName, "AlarmTrigger", {"DateComponentsData": {"type": "BYTES", "value": components}}, trigger.recordChangeTag)
            else:
                alarm_uid, trigger_uid = str(uuid.uuid4()).upper(), str(uuid.uuid4()).upper()
                alarm_id, trigger_id = "Alarm/" + alarm_uid, "AlarmTrigger/" + trigger_uid
                ids.append(alarm_uid)
                operation("create", alarm_id, "Alarm", {
                    "AlarmUID": {"type": "STRING", "value": alarm_uid},
                    "Deleted": {"type": "INT64", "value": 0}, "Imported": {"type": "INT64", "value": 0},
                    "Reminder": {"type": "REFERENCE", "value": {"recordName": reminder.id, "action": "VALIDATE"}},
                    "TriggerID": {"type": "STRING", "value": trigger_uid},
                    "DueDateResolutionTokenAsNonce": {"type": "DOUBLE", "value": nonce}}, parent_id=reminder.id)
                operation("create", trigger_id, "AlarmTrigger", {
                    "Alarm": {"type": "REFERENCE", "value": {"recordName": alarm_id, "action": "VALIDATE"}},
                    "DateComponentsData": {"type": "BYTES", "value": components},
                    "Deleted": {"type": "INT64", "value": 0}, "Imported": {"type": "INT64", "value": 0},
                    "Type": {"type": "STRING", "value": "Date"}}, parent_id=alarm_id)
    if not operations:
        return
    fresh = json.loads(service._generate_resolution_token_map(["alarmIDs", "lastModifiedDate"]))
    token_map.setdefault("map", {}).update(fresh["map"])
    operation("update", parent.recordName, "Reminder", {
        "AlarmIDs": {"type": "STRING_LIST", "value": ids},
        "LastModifiedDate": {"type": "TIMESTAMP", "value": int(time.time() * 1000)},
        "ResolutionTokenMap": {"type": "STRING", "value": json.dumps(token_map, separators=(",", ":"))}}, parent.recordChangeTag)
    # Apple rejects a child-first batch as updating its parent twice.
    # Match the native/SDK parent-first ordering.
    operations.insert(0, operations.pop())
    response = service._raw.modify(operations=operations, zone_id=_REMINDERS_ZONE_REQ, atomic=True)
    _assert_modify_success(response, "Synchronize date notification")

def recurrence_rows(service, reminder):
    if not getattr(reminder, "recurrence_rule_ids", []):
        return []
    return [dump(rule) for rule in service.recurrence_rules_for(reminder)]

def sync_recurrence(service, reminder, args):
    if "repeat_frequency" not in args:
        return
    from pyicloud.services.reminders.models import RecurrenceFrequency
    rules = service.recurrence_rules_for(reminder)
    if args["repeat_frequency"] == "none":
        for rule in rules:
            service.delete_recurrence_rule(service.get(reminder.id), rule)
        return
    if len(rules) > 1:
        raise Failure("validation", "Multiple repeat rules require explicit reconciliation before replacement.")
    values = {"frequency": RecurrenceFrequency[args["repeat_frequency"].upper()],
              "interval": args.get("repeat_interval", 1)}
    if rules:
        service.update_recurrence_rule(rules[0], **values)
    else:
        service.create_recurrence_rule(reminder, **values)

class Worker:
    def __init__(self, session_dir, email, tz, service=None):
        self.session_dir, self.email, self.tz = Path(session_dir), email, tz
        self.service = service
        self.receipts = self.session_dir / "mcp-receipts"
        self.receipts.mkdir(mode=0o700, exist_ok=True)

    def connect(self):
        if self.service is None:
            from pyicloud import PyiCloudService
            try:
                api = PyiCloudService(self.email, password=None, cookie_directory=str(self.session_dir), authenticate=False, with_family=False, accept_terms=False)
                api.authenticate()
                if api.requires_2fa:
                    raise RuntimeError("2fa")
                self.service = api.reminders
            except Exception:
                raise Failure("authentication_required", "Renew the Apple login/2FA session with scripts/reminders_probe.py on the host.") from None
        return self.service

    def call(self, operation, args):
        path = None
        fingerprint = hashlib.sha256(json.dumps([operation, args], sort_keys=True).encode()).hexdigest()
        if operation in MUTATIONS:
            key = args.get("idempotency_key", "")
            if not key:
                raise Failure("validation", "A stable idempotency_key is required for mutations.")
            path = self.receipts / (hashlib.sha256(key.encode()).hexdigest() + ".json")
            if path.exists():
                receipt = json.loads(path.read_text())
                if receipt["fingerprint"] != fingerprint:
                    raise Failure("conflict", "idempotency_key already used with different parameters.")
                if "result" in receipt:
                    return receipt["result"]
                raise Failure("outcome_unknown", "A prior write may have been applied. Reconcile its target before issuing another write.")
        service = self.connect()
        if operation == "list_reminder_lists":
            return {"lists": [dump(x) for x in service.lists() if not x.deleted and not x.is_group]}
        if operation in {"list_reminders", "search_reminders"}:
            ids = [args["list_id"]] if args.get("list_id") else [x.id for x in service.lists() if not x.deleted and not x.is_group]
            rows = []
            for lid in ids:
                batch = service.list_reminders(lid, include_completed=args.get("include_completed", False))
                for item in batch.reminders:
                    if not item.deleted and (not args.get("query") or args["query"].casefold() in (item.title + "\n" + item.desc).casefold()):
                        rows.append(dump(item))
            offset, limit = args.get("offset", 0), args.get("limit", 100)
            return {"reminders": rows[offset:offset+limit], "total": len(rows), "next_offset": offset+limit if len(rows)>offset+limit else None}
        if operation == "get_reminder":
            item = service.get(args["reminder_id"])
            result = dump(item)
            result["notification_alarms"] = notification_rows(service, item)
            result["recurrence_rules"] = recurrence_rows(service, item)
            return result
        item = None
        if operation != "create_reminder":
            item = service.get(args["reminder_id"])
            if args.get("record_change_tag") and args["record_change_tag"] != item.record_change_tag:
                raise Failure("conflict", "Reminder changed since it was read; fetch it again.")
        old_due = item.due_date if item else None
        tz = args.get("time_zone") or (item.time_zone if item else None) or self.tz
        changes = {k: v for k, v in args.items() if k in EDITABLE}
        if "due_date" in changes:
            changes["due_date"] = date_value(changes["due_date"], tz)
            changes.setdefault("all_day", bool(args["due_date"] and len(args["due_date"]) == 10))
            changes.setdefault("time_zone", tz)
        if "notes" in changes:
            changes["desc"] = changes.pop("notes")
        if operation == "complete_reminder":
            changes["completed"] = args.get("completed", True)
        if item:
            for key, value in changes.items():
                setattr(item, key, value)
            if "completed" in changes:
                item.completed_date = datetime.now(timezone.utc) if item.completed else None
        if "repeat_frequency" in args:
            if args["repeat_frequency"] not in {"none", "daily", "weekly", "monthly", "yearly"}:
                raise Failure("validation", "Unsupported repeat frequency.")
            interval = args.get("repeat_interval", 1)
            if type(interval) is not int or not 1 <= interval <= 1000:
                raise Failure("validation", "repeat_interval must be between 1 and 1000.")
            effective_due = changes.get("due_date", item.due_date if item else None)
            if args["repeat_frequency"] != "none" and effective_due is None:
                raise Failure("validation", "Repeating reminders require a due date.")
            if item and len(recurrence_rows(service, item)) > 1 and args["repeat_frequency"] != "none":
                raise Failure("validation", "Multiple repeat rules require explicit reconciliation before replacement.")
        elif "repeat_interval" in args:
            raise Failure("validation", "repeat_interval requires repeat_frequency.")
        if path:
            save(path, {"fingerprint": fingerprint, "state": "dispatching", "reminder_id": args.get("reminder_id")})
        try:
            if operation == "create_reminder":
                item = service.create(list_id=args["list_id"], **changes)
                # Persist the ID before readback so uncertain outcomes are reconcilable.
                save(path, {"fingerprint": fingerprint, "state": "created", "reminder_id": item.id})
            elif operation in {"update_reminder", "complete_reminder"}:
                service.update(item)
            elif operation == "delete_reminder":
                if not item.deleted:
                    service.delete(item)
                result = {"id": item.id, "deleted": True}
            else:
                raise Failure("validation", "Unsupported operation.")
            if operation != "delete_reminder":
                schedule_changed = operation == "create_reminder" or bool({"due_date", "all_day", "time_zone"}.intersection(args))
                item = service.get(item.id)
                if (schedule_changed and item.due_date is not None and not item.all_day) or item.alarm_ids:
                    sync_date_alarm(service, item, old_due, schedule_changed)
                item = service.get(item.id)
                sync_recurrence(service, item, args)
                item = service.get(item.id)
                result = dump(item)
                result["notification_alarms"] = notification_rows(service, item)
                result["recurrence_rules"] = recurrence_rows(service, item)
            save(path, {"fingerprint": fingerprint, "result": result})
            return result
        except Exception:
            raise Failure("outcome_unknown", "Write outcome is uncertain. Reconcile the target and retain its idempotency_key.") from None

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--timezone", required=True)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    os.umask(0o077)
    directory = Path(args.session_dir)
    if not directory.is_dir() or directory.stat().st_mode & 0o077:
        return 1
    worker = Worker(directory, args.email, args.timezone)
    for line in sys.stdin:
        try:
            if len(line) > 65536:
                raise Failure("validation", "Request too large.")
            request = json.loads(line)
            response = {"result": worker.call(request["operation"], request["args"])}
        except Failure as error:
            response = {"error": {"code": error.code, "message": error.message}}
        except Exception:
            response = {"error": {"code": "reminders_error", "message": "Reminders request failed. Renew the session if expired; upstream details are withheld."}}
            worker.service = None
        payload = json.dumps(response)
        if len(payload.encode()) > 1500000:
            payload = json.dumps({"error": {"code": "response_too_large", "message": "Use a smaller page size or one list."}})
        print(payload, flush=True)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
