#!/bin/bash
set -euo pipefail

: "${OPENHACK_SIGNING_IDENTITY:?Missing signing identity}"
: "${OPENHACK_SIGNING_KEYCHAIN:?Missing signing keychain}"
: "${OPENHACK_NOTARY_KEY_FILE:?Missing notarization key file}"
: "${OPENHACK_NOTARY_KEY_ID:?Missing notarization key ID}"
: "${OPENHACK_NOTARY_ISSUER:?Missing notarization issuer}"

binary="${1:?Usage: sign-and-notarize.sh /path/to/openhack}"
[[ "$(uname -s)" == Darwin && -f "$binary" ]] || { echo 'A macOS host and built executable are required.' >&2; exit 1; }
script_dir="$(cd "$(dirname "$0")" && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

codesign --force --options runtime --timestamp \
  --entitlements "$script_dir/entitlements.plist" \
  --keychain "$OPENHACK_SIGNING_KEYCHAIN" \
  --sign "$OPENHACK_SIGNING_IDENTITY" "$binary"
codesign --verify --strict --verbose=2 "$binary"
ditto -c -k --keepParent "$binary" "$work/notarization.zip"
xcrun notarytool submit "$work/notarization.zip" \
  --key "$OPENHACK_NOTARY_KEY_FILE" \
  --key-id "$OPENHACK_NOTARY_KEY_ID" \
  --issuer "$OPENHACK_NOTARY_ISSUER" \
  --output-format json > "$work/submitted.json"
submission_id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["id"])' "$work/submitted.json")"
echo "Submitted for notarization: $submission_id"
xcrun notarytool wait "$submission_id" \
  --key "$OPENHACK_NOTARY_KEY_FILE" \
  --key-id "$OPENHACK_NOTARY_KEY_ID" \
  --issuer "$OPENHACK_NOTARY_ISSUER" \
  --timeout 20m --output-format json > "$work/submission.json"
python3 - "$work/submission.json" <<'PY'
import json, sys
with open(sys.argv[1]) as file:
    result = json.load(file)
if result.get('status') != 'Accepted':
    raise SystemExit('Apple did not accept notarization. Submission: ' + str(result.get('id')))
print('Apple notarization accepted. Submission: ' + result['id'])
PY
# Bare command-line executables cannot have a ticket stapled. Apple records
# their signed code hashes; do not modify or re-sign them after acceptance.
codesign --verify --strict --verbose=2 "$binary"
