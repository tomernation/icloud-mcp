# Pi recovery and maintenance

This kit rebuilds the existing iCloud MCP and Keycloak installation from an encrypted backup. It preserves the owner identity, password hash, authenticator enrollment, OAuth client secrets, subject/audience mappers, iCloud app password, DuckDNS token, backend certificates, public certificate state, and deployed source/configuration. Calendar events remain in iCloud; Gmail and the ChatGPT cloud schedule are managed separately.

## Controller setup

Run Ansible from a trusted Mac or Linux computer with SSH access to the Pi. The Pi only needs Python and passwordless sudo (or add `--ask-become-pass`). Keep SSH host-key checking enabled. After reimaging, verify the new SSH fingerprint locally before replacing its known-host entry.

```sh
python3 -m venv .venv
.venv/bin/pip install 'ansible-core>=2.19,<2.20'
cd deploy/ansible
```

Adjust `inventory.ini` for the target hostname/IP and SSH username. The default source path is `/home/mcpadmin/icloud-mcp-prep`. Paths inside the current Compose files are fixed to `/var/lib/icloud-mcp`; retain this state path when recovering. Docker and Compose are installed from Debian's repositories on a fresh Debian 13 ARM64 Pi. Existing Docker installations are retained.

Use `ansible-playbook` and `ansible-vault` from the same virtual environment. Examples below assume they are on PATH.

## Verify or repair an existing installation

```sh
ansible-playbook maintain.yml
ansible-playbook maintain.yml -e repair_services=true
```

The default run is read-only. Repair reconciles the existing Compose definitions and boot services; it does not reset accounts, restore a database, upgrade pinned application images, or repair arbitrary corrupted files. It can restart stopped services. Configuration corruption or damaged database storage requires recovery from a known-good backup on a fresh target.

Verification checks containers, PostgreSQL readiness, OAuth issuer metadata over validated TLS 1.3, anonymous MCP rejection, and the DuckDNS timer. It does not sign in as the owner or create calendar events. After recovery, complete an actual ChatGPT OAuth login and calendar read to verify end-to-end access.

## Encrypted backup

Store a strong Vault password in a separate file with mode `0600`, outside the repository, and keep a recovery copy in your password manager. Do not send it in chat. The backup destination's parent directory must exist; use a new output filename for each backup.

```sh
ansible-playbook backup.yml \
  -e backup_output=/absolute/private/backups/pi-2026-10-02.tar.gz.vault \
  -e vault_password_file=/absolute/private/vault-password
```

The playbook makes a transactionally consistent PostgreSQL custom-format dump. It does **not** copy PostgreSQL's live data directory. It collects deployed source/configuration, credentials, certificate state and the DuckDNS token, fetches through SSH, and encrypts with Ansible Vault on the controller. Plaintext temporary files have private permissions and are removed in an `always` block. An abruptly terminated process or power loss can leave staging files: check `/tmp/icloud-recovery-*` on both hosts. Private staging permissions still apply. Filesystem deletion does not guarantee secure erasure from flash storage.

Backups contain sensitive identities and credentials even though encrypted. Keep the encrypted archive off the Pi and keep its password separately. Vault protects the backup at rest; the restored server's SD card remains unencrypted. See [Ansible Vault documentation](https://docs.ansible.com/projects/ansible/latest/vault_guide/vault_encrypting_content.html).

A database snapshot and file snapshot are not one atomic transaction. Avoid changing passwords, OAuth settings, deployment configuration or certificate files during backup. Make a fresh backup after significant configuration/credential changes. No recurring backup schedule is installed by this kit.

## Fresh-target recovery

1. Reimage a Pi with 64-bit Debian 13/Raspberry Pi OS based on Debian 13. Enable SSH, networking and your administrator account.
2. Verify its SSH fingerprint, update the Ansible inventory and eero IP reservation/443 forwarding if the LAN address changed.
3. Supply a trusted encrypted archive and its Vault password. The restore refuses any target where `/var/lib/icloud-mcp` already exists; it never wipes a working installation.
4. Run:

```sh
ansible-playbook restore.yml --ask-vault-pass \
  -e restore_confirm=true \
  -e recovery_bundle=/absolute/private/backups/pi-2026-10-02.tar.gz.vault
```

Enter the archive's Vault password privately. Alternatively, use `--vault-password-file /absolute/private/vault-password`.

Restore installs Docker if needed, decrypts the archive, restores recorded file ownership/permissions, starts PostgreSQL alone, imports the preserved database, builds the preserved MCP source, and starts Keycloak, MCP, Caddy and DuckDNS. Docker and service restart policies provide reboot startup. Ansible removes plaintext restore staging afterward.

The Go builder/distroless base tags in the existing Dockerfile are not digest-pinned; rebuilding requires registry/network access and is not byte-for-byte reproducible. Caddy, Keycloak and PostgreSQL application images are digest-pinned. Registry images must remain available. For fully offline recovery, separately preserve those images and the built MCP image with `docker save`; this kit does not include image archives.

If restore fails after creating application state, preserve the failed target for diagnosis and retry on a fresh image. Do not bypass the fresh-target guard and repeatedly import into a partially restored database.

DNS, eero/Frontier settings and the cloud schedule are not recreated by Ansible. Existing DNS records remain valid if the home public address is unchanged; DuckDNS updates it after startup. Public certificates can renew through port 443. Expired backend certificates must be replaced; the backup does not extend their validity.

## Validation performed October 2, 2026

- All playbooks passed Ansible Core 2.19 syntax checks.
- `verify.yml` passed against the live Pi: five task groups, zero changes.
- `backup.yml` completed against the live Pi and produced an encrypted off-Pi archive; plaintext staging cleanup passed.
- The encrypted archive was decrypted in memory, required recovery files checked, and its database dump restored into an isolated temporary PostgreSQL container with no network or published ports.
- Restored owner identity, password/OTP credentials and the three subject/audience OAuth mappers were verified; the temporary container was removed.
- A complete fresh-Pi restore has **not** yet been tested. Neither restore nor repair was run against the live installation.

### Deploy Reminders integration

`ansible-playbook deploy-reminders.yml` copies application/build sources from
this checkout, imports the previously authenticated Apple web session on first
deployment, builds the ARM64 image on the Pi, and replaces only the MCP
container. The existing `deploy/pi.env`, TLS secrets, proxy and OAuth services
are preserved. The production session is owned by container UID 65532 and
never copied back into the repository. See `docs/reminders-web-api.md` for
session renewal and tool semantics.
