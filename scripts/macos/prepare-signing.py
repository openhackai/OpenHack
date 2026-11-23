"""Import GitHub Actions signing secrets into an isolated, temporary keychain."""
import base64
import os
from pathlib import Path
import secrets
import shlex
import subprocess
import sys

if sys.platform != 'darwin':
    raise SystemExit('macOS is required.')
root = Path(os.environ['RUNNER_TEMP']) / 'openhack-signing'
root.mkdir(mode=0o700)
os.umask(0o077)
(root / 'identity.p12').write_bytes(base64.b64decode(os.environ['MACOS_CERTIFICATE_P12'], validate=True))
(root / 'notary.p8').write_text(os.environ['APPLE_NOTARY_KEY_P8'])
keychain = str(root / 'signing.keychain-db')
password = secrets.token_urlsafe(48)

def security(*args):
    result = subprocess.run(['security', *args], capture_output=True)
    if result.returncode:
        # Do not emit arguments or output that might include credentials.
        raise SystemExit('Signing keychain setup failed: ' + args[0])

security('create-keychain', '-p', password, keychain)
security('set-keychain-settings', '-lut', '21600', keychain)
security('unlock-keychain', '-p', password, keychain)
security('import', str(root / 'identity.p12'), '-k', keychain,
         '-P', os.environ['MACOS_CERTIFICATE_PASSWORD'], '-T', '/usr/bin/codesign')
security('set-key-partition-list', '-S', 'apple-tool:,apple:,codesign:', '-s', '-k', password, keychain)
existing = shlex.split(subprocess.check_output(['security', 'list-keychains', '-d', 'user'], text=True))
security('list-keychains', '-d', 'user', '-s', *existing, keychain)
(root / 'identity.p12').unlink()
with open(os.environ['GITHUB_ENV'], 'a') as file:
    file.write('OPENHACK_SIGNING_KEYCHAIN=' + keychain + '\n')
    file.write('OPENHACK_NOTARY_KEY_FILE=' + str(root / 'notary.p8') + '\n')
print('Imported the signing identity and notarization key into temporary storage.')
