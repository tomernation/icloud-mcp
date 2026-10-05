# Reminders web API investigation

Target: the existing HTTP MCP server on the Linux Pi. The native macOS
EventKit experiment has been removed. No Reminders tools are currently enabled.

## Observed from upstream code

Reference revision: `timlaing/pyicloud` commit
`86c4bc90d5632bcaf7507e5335e127f7450a177a`.

- [Reminders service](https://github.com/timlaing/pyicloud/blob/86c4bc90d5632bcaf7507e5335e127f7450a177a/pyicloud/services/reminders/service.py)
  constructs `/database/1/com.apple.reminders/production/private` beneath the
  authenticated account's discovered `ckdatabasews` service URL.
- [Client](https://github.com/timlaing/pyicloud/blob/86c4bc90d5632bcaf7507e5335e127f7450a177a/pyicloud/services/reminders/client.py)
  supports query, lookup, changes and modify operations.
- [Authentication](https://github.com/timlaing/pyicloud/blob/86c4bc90d5632bcaf7507e5335e127f7450a177a/pyicloud/base.py)
  uses persisted web-session tokens/cookies, SRP for fresh authentication,
  verification codes and session trust. Accessing Reminders invokes its PCS
  consent flow, which can require approval on an Apple device.
- [Writes](https://github.com/timlaing/pyicloud/blob/86c4bc90d5632bcaf7507e5335e127f7450a177a/pyicloud/services/reminders/_writes.py)
  encode CloudKit fields, CRDT documents and resolution tokens; due-date writes
  include timezone handling. Creating a reminder is more than posting plain JSON.

This is an unofficial web interface, not a documented public Apple Reminders
developer API. Upstream implementation evidence is not proof that this account
or Pi can authenticate successfully.

## Next smallest test

Run the isolated read-only discovery probe. It prompts in the terminal, does not
accept terms automatically, suppresses upstream logs and prints only the list
count. Session files are sensitive and must remain outside Git in a private
directory. The script performs no reminder mutations.

```sh
python3 -m venv /tmp/icloud-reminders-probe-venv
/tmp/icloud-reminders-probe-venv/bin/pip install -r scripts/reminders-probe-requirements.txt
/tmp/icloud-reminders-probe-venv/bin/python scripts/reminders_probe.py --session-dir ~/.local/state/icloud-reminders-probe
```

Use the Apple Account password for this experiment. The deployed CalDAV
app-specific password failed the web authentication experiment below. Security-key accounts and
legacy two-step verification are not implemented by this probe.

## Acceptance and implementation order

1. Verify account authentication and read-only list discovery.
2. Verify session reuse from the Pi and document expiry/device-approval behavior.
3. Choose a Go implementation or pinned Python adapter based on that evidence.
   The probe dependency is not part of the production server.
4. Add list discovery and reminder reads with bounded results, credential
   redaction and fixture tests; make them available over existing HTTP hosting.
5. Add creation with date/timezone fixtures, global read-only gating, mutation
   audit and unknown-outcome handling. Verify a designated test reminder through
   Apple's client before treating creation as complete.

## Existing app-password test (2026-10-03)

The deployed Pi credential was read over SSH into process memory, without
printing it or writing it to a local file. Requests were sent from the development
Mac using the pinned upstream client and temporary session storage.

- CalDAV current-user-principal PROPFIND: HTTP 207 (credential accepted).
- Service-specific web login with `appName=reminders`: failed with
  `PyiCloudFailedLoginException`.
- Standard web-session authentication with the same credential: failed with
  `PyiCloudFailedLoginException`.

No reminder read or mutation succeeded; no main Apple Account password was used.
Temporary session storage was removed. This demonstrates failure for this
credential and the tested client flows, not every possible Apple authentication
route or future behavior. The server on the Pi was not modified.

## CalDAV VTODO test (2026-10-03)

Using the same deployed app-specific credential, read-only principal, home-set
and collection discovery succeeded (HTTP 207). Of 27 calendar collections,
20 advertise VTODO and seven advertise VEVENT. VTODO-filtered REPORT requests
succeeded. Requesting calendar-data yielded 269 actual VTODO components across
six collections. Collection responses without calendar-data were excluded from
the task count; counting every successful propstat overcounts resources.

The newest returned CREATED/LAST-MODIFIED/DTSTAMP year was 2019; some collections
had only 2018 metadata. One resource matched a migration/upgrade text heuristic.
The evidence suggests a legacy store, but does not prove whether these records
sync with current Apple Reminders. No titles, IDs or task bodies were logged;
no writes were attempted. Before implementing VTODO support as current Reminders
access, compare a known recent reminder with these results or perform an
explicitly authorized sync experiment.

## Authorized creation test (2026-10-03)

Created one VTODO in the collection named `Reminders` with title
`CalDAV sync test — October 3, 2026`, status NEEDS-ACTION, no due date and no
alarm. Used a new UUID resource and `If-None-Match: *`; the PUT was dispatched
once. Apple returned HTTP 201. GET of the exact resource returned HTTP 200 and
the same UID/title. This confirms CalDAV storage creation and read-back with
the existing app-specific password. Sync to the current Reminders app remains
unverified. The test task remains present for the user to inspect.

Unverified at the time of the October 3 CalDAV test: successful web login, this account's PCS state, Pi session reuse, the precise
E2EE/web-access requirements for this account, and live creation/sync semantics.

## Confirmed modern Reminders connection (2026-10-04)

The Pi authenticated with an Apple Account password and interactive 2FA, saved
its trusted session outside the repository, and reused it without resupplying
the password. CloudKit list discovery succeeded. Both the CloudKit sync test
and the all-day October 11 “Do taxes” reminder were read back from iCloud; the
user confirmed the sync test appeared in Apple Reminders. The earlier CalDAV
test reminders did not appear there. Use CloudKit for modern Reminders and
retain CalDAV for Calendar.

## MCP integration

Optional tools: `list_reminder_lists`, `list_reminders`, `search_reminders`,
`get_reminder`, `create_reminder`, `update_reminder`, `complete_reminder`,
`delete_reminder`. The global read-only setting removes mutation tools.
Capability reporting includes the Reminders domain and effective write gate.
Search matches titles and notes. Listing/search accept `limit` (1–100) and
`offset`; completed reminders are excluded unless requested. List discovery
excludes deleted lists and group containers.

Create/edit support title, notes, due date, timezone, all-day, priority
(0 none, 1 high, 5 medium, 9 low), flags, completion, and a parent reminder ID
for subtasks. Date-only values mean all-day in the configured timezone.
Omitted edit fields are preserved; empty notes or due date clears that field.
Complete accepts `completed=false` to reopen a reminder. Delete soft-deletes.
Mutations can check a `record_change_tag` obtained from `get_reminder`.

Every mutation requires a stable `idempotency_key`. Private durable receipts
are saved before dispatch. Identical successful retries return the saved
result, including across process restarts. A pending or uncertain receipt
blocks another dispatch; reconcile by reading/searching the affected reminder
before creating another operation. Reusing a key with different parameters
returns a conflict. Upstream errors and response bodies are never logged or
returned to the MCP caller. The worker serializes session access; cancelled
calls kill the worker and the next request starts a fresh process.

The worker uses the pinned Python client from
`scripts/reminders-probe-requirements.txt`. No macOS APIs are used. Enable with:

```
ICLOUD_MCP_ENABLE_REMINDERS=true
ICLOUD_MCP_REMINDERS_SESSION_DIR=/run/reminders-session
ICLOUD_MCP_REMINDERS_WORKER=/opt/reminders_worker.py
ICLOUD_MCP_REMINDERS_PYTHON=/opt/reminders/bin/python
```

The session directory must be private and writable by the service user.
The Pi compose file mounts `/var/lib/icloud-mcp/reminders-session`; it contains
sensitive Apple session tokens and receipts and must never enter Git.
The worker does not store an Apple Account password or prompt from HTTP.
CloudKit calls have a 120-second deadline to accommodate initial PCS access;
other domains retain their existing deadline.

To deploy from the working checkout:

```
cd deploy/ansible
ansible-playbook deploy-reminders.yml
```

To renew authentication, run `scripts/reminders_probe.py` interactively in the
Pi probe venv against `/home/mcpadmin/.local/state/icloud-reminders`, complete
Apple's 2FA/device approval, then stop the MCP container and run the playbook
with `-e refresh_reminders_session=true`. Stop it before replacing session
files so the worker cannot overwrite refreshed credentials. Refresh preserves
production receipts. Other iCloud domains remain available if the Reminders
web session expires.

Not exposed in this integration: editing reminder lists, custom repeat rules,
location triggers, tags, attachments, or urgent alarm scheduling. Standard notifications use the scheduled
due_date and all_day fields as described below.

## Deployment verification (2026-10-04)

Ansible deployed the image to the Pi. The existing public HTTP connector
reported 18 tools including all eight Reminders tools and both read/write
capability gates. A live MCP lifecycle test using that same image verified
list discovery, search, creation with an all-day date, identical-key replay,
editing while preserving notes, a 10 a.m. Pacific timed due date, clearing
notes/date, completion, reopening, pagination, and deletion. The disposable
reminder was removed. The existing “Do taxes” reminder was read without
modification. Calendar listing still succeeded through the public connector.

One first search attempt returned a generic read error. The direct Pi client,
the container worker, and a second full MCP lifecycle run subsequently passed
without a code change. Its cause remains unconfirmed; no write occurred in
the failed run. Offline contract tests, Go tests, relevant Go race checks,
Go vet, Ansible syntax checking, and diff whitespace checking passed.

## Standard notifications and urgent alarms

A live test established that saving a timed `DueDate` alone did not deliver a
notification on the user's device. The failed test had zero alarm records.
An existing native Apple reminder provided the missing representation:
`Reminder.AlarmIDs` links to an `Alarm` record, which links through `TriggerID`
to an `AlarmTrigger` of type `Date`. `DateComponentsData` is BYTES containing
JSON calendar components and an IANA timezone. The alarm's
`DueDateResolutionTokenAsNonce` equals `counter * 100000000000 + modificationTime`
from the reminder's `dueDate` resolution token. These facts were read directly
from the user's existing iCloud records, without changing them.

The worker now creates the linked Date alarm for a timed due date. It moves
that alarm when the time changes, removes the matching timed alarm when the
date is cleared or made all-day, preserves other triggers, and keeps existing
alarms' nonce values in sync after SDK edits. Alarm and trigger changes plus
parent linking are submitted atomically. Creating the reminder and adding its
alarm are separate phases; durable receipts block duplicate creation if the
second phase fails. `get_reminder` and successful mutation responses expose
`notification_alarms` read from actual records, rather than inferring them from
the due date. Date-only reminders do not get an explicit timed alarm from this
integration. Device delivery still requires an end-to-end notification test.

Urgent ringing alarms are a different Apple feature. The pinned client has no
Urgent field or method, so those are not supported. Apple's device behavior is
documented at https://support.apple.com/en-us/102484.

### Alarm verification

The first live atomic alarm batch was rejected with “Record updated multiple
times in one batch.” Moving the reminder update before its Alarm and
AlarmTrigger operations was the only changed variable; iCloud then accepted
it. Readback verified one Date alarm. A subsequent MCP edit moved that same
alarm without duplicating it. A separate live lifecycle test verified adding
the alarm for a timed date and removing it when the due date was cleared;
the disposable task was deleted. Eleven Python contract tests and the Go
suite passed. After deployment, the user confirmed that the 7:32 p.m. Pacific
“Notification alarm fix test” alarm fired. Timed reminder notification delivery
is therefore verified end to end, in addition to the API readback checks.
This confirmation does not establish support for urgent ringing alarms or
explicit alarms on date-only reminders.

## Simple repeats

Create and update accept `repeat_frequency`: `daily`, `weekly`, `monthly`,
`yearly`, or `none` to remove repeat rules. `repeat_interval` specifies every
N periods (1–1000); it defaults to 1 when setting a repeat and requires a
repeat frequency. Repeats require a due date. Omitted settings preserve the
existing rule. Reads and successful writes include `recurrence_rules`.

The deployed image passed live timed-reminder creation with a weekly interval
of two, identical retry replay, title-edit preservation, a monthly rule edit,
and repeat removal readback. The disposable reminder was deleted. Automatic
next-occurrence behavior after completion remains unverified in Apple Reminders.
