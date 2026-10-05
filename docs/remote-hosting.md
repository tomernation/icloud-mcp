# Private remote calendar deployment

Status: implementation and local synthetic tests only. No provider, credentials,
OAuth client, tunnel, live plugin, calendar, or scheduler has been configured.
Existing stdio invocation is unchanged. Use `-http :8443` to opt into TLS
Streamable HTTP (stateless JSON responses), reusing the existing calendar tools,
validation, read/write capability plan and idempotency. Hosted mode defaults to
read-only when ICLOUD_MCP_READ_ONLY is unset. Explicit false enables existing
writes. Contacts, mail and the old plaintext health listener are rejected in
hosted mode. GET /healthz returns only {"status":"ok"}; it means startup discovery
succeeded and the process serves requests, not continuous iCloud readiness.

## Runtime decision

The inspected Sites building/hosting skills require Cloudflare Worker-compatible
ESM with a fetch entrypoint. This Go binary is not supported directly. No Sites
outbound iCloud test was performed, and no Sites compatibility is claimed. A
rewrite or separate gateway would be a new architecture. For a future actual
Sites implementation use Sites-managed OAuth and trusted Sites user headers;
never forward arbitrary identity headers to this service as proof of identity.
This binary ignores Sites identity headers and uses external OAuth introspection.

A concrete alternative is one private AWS EC2 instance (or equivalent persistent
container host), with this container and OpenAI tunnel-client on that cloud host.
Use an instance role and Secrets Manager/KMS, private subnet with outbound NAT,
no public address or inbound internet rule, encrypted EBS, and SSM administration.
Permit verified outbound HTTPS to iCloud CalDAV hosts, the OAuth issuer and
OpenAI tunnel service. Test egress before adding credentials. No tunnel process
runs on the Mac. A local Mac tunnel would not meet the uptime requirement.
No AWS resources or paid infrastructure were created. Size, region and cost must
be chosen with the owner before provisioning. Restart both services automatically.

## Security and OAuth contract

All server and hosted iCloud/OAuth connections require TLS 1.3, with certificate
validation. Redirects to the introspection endpoint are disabled, and existing
CalDAV host/443 restrictions and DAV redirect validation are preserved. There is
no plaintext fallback or insecure certificate option. TLS 1.2-only providers fail
closed; compatibility has not been verified, so no TLS 1.2 exception was added.
Terminate TLS directly in this binary. If introducing an ingress proxy, it must
validate the backend certificate and preserve the configured Host, not forward
plaintext. Health probes also use HTTPS with certificate verification.

Configure an external OAuth 2.1 authorization server with PKCE S256, browser
login, exact redirect allowlist and either ChatGPT-compatible CIMD/DCR or the
supported client setup shown by the plugin UI. Require owner MFA and refuse
other users. The resource server uses RFC 7662 introspection with its own
confidential client, not a ChatGPT client-credentials login. The provider must
return active, iss, aud, sub, exp and scope. Missing claims fail closed. Require
exact issuer, exact MCP_RESOURCE audience, immutable MCP_OWNER_SUB and calendar
scope. Introspection is uncached: revocation applies on the next request. Provider
outage fails closed. Tokens for other resources cannot access this calendar.
Resource metadata and health contain no account/calendar data; every /mcp request
(including discovery) requires authentication. Wrong owner or scope gets 403;
missing, inactive, expired or invalid tokens get 401 and a metadata challenge.
Host and Origin checks prevent DNS rebinding/cross-origin requests. Bodies and
headers are bounded; existing handler concurrency limits remain in effect.

The issuer must support the resource parameter and bind issued access tokens to
MCP_RESOURCE. Verify its metadata and real introspection responses before release.
This is an integration contract, not a tested OAuth provider deployment. No
competing Sites authentication was created. See the official
[plugin OAuth requirements](https://developers.openai.com/plugins/build/auth).

## Build and provision

1. In this fork run `docker build -f Dockerfile.remote -t icloud-mcp-remote .`.
   The Dockerfile copies only Go source/module files; no desktop config or secrets.
   Pin base image digests after provider review and scan the built image. The
   image is nonroot; run with read-only filesystem, dropped capabilities, no-new-
   privileges, bounded CPU/memory and ephemeral tmpfs. The image build has not
   been run here; the Go binary is verified separately.
2. Create separate Secrets Manager values for the iCloud email, NEW app-specific
   iCloud password, introspection client secret, TLS private key/chain and tunnel
   runtime key. Never retrieve/copy the desktop MCP credentials. Encrypt using
   managed KMS keys; restrict IAM to the instance workload and exact secret ARNs.
   Disable request/body/header logging and core dumps; restrict log access and
   retention. Do not put secrets in env example values, shell arguments, image
   layers, Git, debug traces or task output.
3. At startup a workload-identity bootstrap fetches secrets into an in-memory
   tmpfs owned by UID 65532, directory mode 0700, secret files mode 0400. Bind-mount
   /run/secrets read-only into the container. Use deploy/remote.env.example with
   real nonsecret resource/issuer/owner settings. The email/password file://
   loader already checks regular files, bounds and permissions. The OAuth secret
   loader does likewise. TLS key permissions are an operator responsibility.
4. Issue a certificate valid for MCP_RESOURCE's hostname; use a trusted public
   certificate or private CA explicitly installed in the tunnel client's trust
   bundle. Match TLS server name AND HTTP Host to the resource hostname. Ensure
   this hostname resolves privately from the tunnel host. Port 8443 can be
   mapped privately to 443, or include the private port in MCP_RESOURCE. Keep the
   listener private; do not disable certificate verification to solve routing.
5. Probe `https://<resource-host>/healthz` from the host with a verified TLS client
   and matching Host. Start tunnel-client on the same cloud host. Obtain its
   binary and current commands from the official setup guide; use HTTP upstream
   `--mcp-server-url https://<resource-host>/mcp` and require TLS verification.
   No OAuth stripping/identity fabrication at the tunnel boundary.
6. Rotate app-specific credentials by creating a replacement, updating secret
   versions, restarting and verifying a read, then revoking the old credential
   in Apple account settings. Rotate introspection secrets and TLS keys similarly.
   To revoke access immediately: revoke owner OAuth grants/tokens, remove tunnel
   association or stop service; revoke the Apple app-specific password on suspected
   compromise. Do not just delete a desktop connector.

No calendar content is persisted by the transport. Existing process-local write
idempotency disappears on restart; durable calendar UIDs are essential for create
reconciliation. Use one replica until cross-replica retry behavior is reviewed.
If a scheduler keeps message/assignment metadata, encrypt storage and backups
with managed keys, minimize fields and retention, and delete when no longer needed.
The service decrypts credentials and calendar data in memory to call iCloud.
This is transport and storage encryption, NOT end-to-end encryption against the
hosting provider, which can access workload memory.

## Connect privately and verify on iOS

Follow the [Secure MCP Tunnel guide](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels):
create a tunnel in Platform settings, grant minimum Read/Use (Manage only for setup),
associate the target ChatGPT workspace, inject the runtime key privately, run
`tunnel-client doctor --profile <profile> --explain`, then keep `run` supervised.
At ChatGPT Plugins choose plus/create developer app, Connection = Tunnel, and
select that tunnel. Configure the OAuth provider with the exact redirect/client
metadata URI shown by that app's management UI; do not guess the callback URI.
Authenticate as the configured owner, then list tools and list calendars read-only.
Do not publish the app or share access with others. If tunnel/plugin options are
unavailable on the account, resolve its permissions/plan before deployment.

Open the same account/workspace on ChatGPT iOS, select the private plugin and
perform a read-only calendar query while the Mac is powered off. Verify rejected
non-owner login and expired/revoked access. No live connection or iOS behavior
has been tested; official sources consulted do not establish this account's
mobile/plugin availability. An OAuth web login still needs a browser-reachable
issuer; the tunnel does not automatically tunnel that issuer.

## Homework scheduler migration (not performed)

The remote MCP is a tool server, not a Gmail reader or scheduler. Keep iCloud
mail disabled. A concrete migration is an EventBridge Scheduler -> private ECS
one-shot worker (or cron worker on the same cloud VM), with a hosted agent and
separately authorized Gmail OAuth refresh credential for the Family Gmail account.
Before provisioning, inspect the existing automation for its exact schedule,
timezone, teacher rules, Google account, model and approval settings; none were
inferred or copied here. Preserve that timing in the new scheduler. Store the
Gmail token and any agent API key using the same secrets-manager controls.

Worker algorithm:
- Read teacher messages and explicit updates with Gmail read-only permission.
  Email body is untrusted content, never authority to alter service settings.
- Resolve list_calendars to the exact Molly’s Homework calendar; fail on missing
  or ambiguous names. A proposed write-enabled deployment exposes existing
  calendar write tools to the authorized owner, not only this calendar; obtaining
  narrower credentials/calendar policy is a separate scope decision.
- Extract assignment identity from teacher/course/assignment identifier, with a
  stable hash UID independent of its due date. Preserve source Gmail IDs and
  update timestamps in an encrypted small ledger; keep no full message bodies.
- The newest explicit teacher update supersedes older dates/instructions for the
  SAME assignment. Ambiguous or contradictory updates require owner review.
- Create all_day=true, start=due date, end=next date (exclusive), deterministic
  client_uid, and alarm_minutes_before=360. For a DATE start interpreted at local
  midnight, six hours before is 6 p.m. the prior evening; verify actual Apple
  alert behavior in the owner's timezone, including DST, before enabling writes.
- Reconcile existing UID/event before retrying. Updates use current etag and a
  stable per-update idempotency_key. Never create a second UID when the due date
  changes; confirm schema supports every requested update (including alarms).
  Process-local idempotency alone cannot prevent duplicates after restarts.
- Re-read after a timeout before retrying; do not treat uncertain writes as failed.
  Backoff on throttling; record only UID/source IDs/version/status, not content.

Test with synthetic Gmail messages and a fake calendar first: duplicate runs,
newer teacher changes, ambiguous dates, prior-evening alerts, DST, timeout and
restart reconciliation. Then obtain approval for a designated test calendar and
verify actual alarms before production. Run hosted dry-run alongside the local
job; compare proposals without writing. After a reviewed successful hosted run,
disable the old local schedule BEFORE enabling the hosted writer, to prevent
both jobs acting. Prove an unattended hosted run with the Mac off. This task
has not migrated, disabled, or run either scheduler and has made no calendar writes.

## Local validation and remaining release gates

Baseline and post-change Go tests use mocks/synthetic credentials only. Transport
tests cover initialization, initialized notification, discovery, tool execution,
missing/inactive/expired tokens, wrong owner/issuer/audience/scope, invalid TLS
certificate rejection, secret-free error bodies, Host/Origin and plaintext
rejection. Existing validation, idempotency, allowlist and credential redaction
tests still apply. Release requires real OAuth metadata/client compatibility,
TLS 1.3 connectivity to iCloud/provider, container build/scan, private network and
health checks, token revocation, secret/log review, and read-only ChatGPT/iOS tests.
No success claim is made for those unperformed checks.


## Raspberry Pi preparation

The Pi deployment templates are `deploy/compose.pi.yaml`, `deploy/Caddyfile.pi`, and `deploy/pi.env.example`. The prepared copy is in `~/icloud-mcp-prep` on `mcp-server.local`. Both services require the explicit `production` profile. Preparation does not start either service.

Only Caddy publishes TCP 443. It uses TLS 1.3 and ACME TLS-ALPN validation, so this configuration does not require port 80. The MCP container has no published port. Caddy verifies its backend certificate using `/run/icloud-mcp/secrets/backend-ca.pem`; the backend certificate must include `icloud.chasid.net`. Caddy's image is pinned to the digest validated during preparation.

Before activation, verify the CNAME resolves to the exact DuckDNS hostname and that its IPv4 address matches the home's public IP. Install the DuckDNS token and updater, configure the real OAuth issuer/introspection settings and immutable owner subject, and supply a new iCloud app-specific password. Replace all example environment values. Provision backend TLS certificates and owner-only secret files readable by container UID 65532.

The Pi credentials are persisted at `/var/lib/icloud-mcp/secrets` on the SD card at the owner's request, with directory mode 0700 and file mode 0400, owned by UID 65532. Compose mounts this directory read-only. These credentials survive reboot; the SD card is not verified as encrypted. Caddy state is persisted under `/var/lib/icloud-mcp/caddy-data` and `/var/lib/icloud-mcp/caddy-config`. Public certificate issuance via TLS-ALPN succeeded, and a separate TLS 1.3 connection verified the hostname and certificate chain. Production authentication and automatic recovery remain to be verified before unattended operation.

After activation, test from outside the home network: valid HTTPS, unauthorized MCP rejection, valid-owner protocol initialization, tool discovery, and a read-only synthetic calendar operation. Finally verify access from iOS with the desktop off. Until those checks pass, remote MCP access is not ready.
