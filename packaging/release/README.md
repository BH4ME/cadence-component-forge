# Signed Windows Updates

The updater never modifies the active installation directly. It validates an
Ed25519-signed release manifest, downloads an immutable installer into a separate
cache, checks its exact size and SHA-256, and launches Inno Setup with
`/SILENT /NORESTART` without a shell. The installer owns installation and rollback.
Existing Capture sessions are not closed. No public production URL or production
key has been supplied yet; `trusted_keys.example.json` intentionally trusts no key.

## Capture Integration Contract

Run the installed Python 3.11 runtime with the installed application on its module
path. The installer configuration is `%ProgramData%/JLC2Cadence/config.json`:
`update_manifest_url`, `update_trusted_keys`, `update_cache_dir`. Public keys are
fixed by the installation, not downloaded from the update server.

```powershell
python -m jlc2cadence.updater check --manifest-url https://YOUR-HOST/manifest.json --current-version 0.46 --trusted-keys C:/ProgramData/JLC2Cadence/update-public-keys.json --output check.json
python -m jlc2cadence.updater download --manifest-url https://YOUR-HOST/manifest.json --current-version 0.46 --expected-version 0.47.0 --trusted-keys C:/ProgramData/JLC2Cadence/update-public-keys.json --cache-dir C:/Users/USER/AppData/Local/JLC2Cadence/updates --output download.json
python -m jlc2cadence.updater apply --manifest-url https://YOUR-HOST/manifest.json --current-version 0.46 --expected-version 0.47.0 --trusted-keys C:/ProgramData/JLC2Cadence/update-public-keys.json --cache-dir C:/Users/USER/AppData/Local/JLC2Cadence/updates --output apply.json
```

All results are UTF-8 JSON with `ok`, `status`, `user_message`; CLI exit code is 0
for `ok=true`, 1 for errors. `--output` writes the same response atomically.

- `check`: `update_available`, `current_version`, `latest_version`, `notes`,
  `published_at`, `artifact`. Same version is normal `ok=true,status=up_to_date`.
- `download`: additionally `installer_path`, `version`, `sha256`, status `downloaded`.
- `apply`: re-fetches and verifies the manifest and cached bytes before launch;
  returns `status=started`, `installer_pid`, `restart_capture_required=true`,
  `installed=false`. Started does not mean installed.
- `apply --wait --wait-timeout 900`: observes installer exit and returns
  `installer_exit_code` with `installer_finished` or `installer_failed`. A wait
  timeout reports the installer still running and does not kill it. `installed`
  remains false because this module does not independently verify the active
  installation pointer. Check the new runtime/installer state separately.
- Errors include `error` and a stable `error_code`, for example
  `trusted_keys_unavailable`, `invalid_signature`, `downgrade_rejected`,
  `invalid_download`, `insecure_url`, `installer_start_failed`.

Pass the `latest_version` that the user reviewed in `check` as `--expected-version`
to both download and apply. If the newly fetched, signed manifest has changed
version, the command returns `ok=false,error_code=release_changed` before package
download or launch. The UI must run check and ask for confirmation again. Each
command re-fetches and re-verifies the manifest; manifests are not trusted from a
local unsigned cache. Package files are cached by signed version/filename and
their size/hash are rechecked every time they are reused.

Manifest release versions are exactly `MAJOR.MINOR.PATCH`, numeric with no leading
zeros. Only the installed-version input also accepts the existing `0.46` format
as `0.46.0`. Downgrades and unsupported prerelease/build labels are rejected.
Production requires HTTPS, including redirects. `--allow-loopback-http` permits
HTTP only for literal loopback/localhost test endpoints, never arbitrary hosts.

Python integration functions in `jlc2cadence.updater`:
`load_trusted_keys(path)`, `check_update(url,current_version,keys,...)`,
`download_update(url,current_version,keys,cache_dir,...)`,
`apply_update(url,current_version,keys,cache_dir,wait=False,...)`.
Functions raise `UpdateError`; `failure_response(exc)` converts it to the JSON
error contract. Runtime dependencies: `cryptography==50.0.2`, `cffi==2.1.1`,
`pycparser==3.12` for the tested Windows Python 3.11 payload.

## Keys And Publication

Generate the private key in a separate release-signing environment, outside this
checkout and every public distribution directory. Only distribute the public
JSON. Set `JLC2CAPTURE_SIGNING_KEY_PASSWORD` in the signing environment to encrypt
the private PEM; do not store the password in scripts or public files. Preserve
the production private key securely across releases. Do not use temporary test
keys as production trust anchors.

```powershell
python packaging/release/sign_release.py keygen --private-key D:/PRIVATE-RELEASE-KEYS/update.pem --public-keys D:/PUBLIC-OUTPUT/update-public-keys.json --key-id production-2026
python packaging/release/sign_release.py sign --installer dist/JLC2Capture-Setup-0.47.0.exe --version 0.47.0 --base-url https://YOUR-HOST --private-key D:/PRIVATE-RELEASE-KEYS/update.pem --key-id production-2026 --output-dir packaging/release/public --notes-file release-notes.txt
```

The public root contains only `manifest.json` and
`packages/<version>/<installer.exe>`. Existing release files cannot be replaced
with different bytes, and publishing an older version is rejected. The package
is copied and verified before the latest manifest is atomically replaced.

Envelope schema:

```json
{
  "manifest": {
    "schema": 1,
    "product": "jlc2capture",
    "channel": "stable",
    "platform": "windows-x64",
    "version": "0.47.0",
    "artifact": {"filename": "JLC2Capture-Setup-0.47.0.exe", "url": "https://YOUR-HOST/packages/0.47.0/JLC2Capture-Setup-0.47.0.exe", "size": 123456, "sha256": "64 lowercase hex characters"},
    "notes": "Release notes as plain text",
    "published_at": "2026-11-09T12:00:00+00:00"
  },
  "key_id": "production-2026",
  "signature": "base64 Ed25519 signature"
}
```

The signed bytes are UTF-8 `json.dumps(manifest, ensure_ascii=False, sort_keys=True,
separators=(",",":"), allow_nan=False)`. Public trust JSON is
`{"schema":1,"keys":{"production-2026":"base64 raw 32-byte Ed25519 public key"}}`.

For local end-to-end testing, use a new private/public key pair only in an
independent temporary test directory, sign with
`--base-url http://127.0.0.1:8766 --allow-loopback-http`, start
`python tools/update_server/server.py --root TEST-PUBLIC --port 8766`, and pass
`--allow-loopback-http` to the updater. See
[`tools/update_server/README.md`](../../tools/update_server/README.md) for the
release page and production HTTPS hosting. The local server is not a production
TLS endpoint. Deploy the signed public root only after the real server address
and production public-key configuration are supplied.
