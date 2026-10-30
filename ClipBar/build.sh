#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_NAME="ClipBar"
BUNDLE="$SCRIPT_DIR/$APP_NAME.app"
BINARY="$BUNDLE/Contents/MacOS/$APP_NAME"
SDK="$(xcrun --sdk macosx --show-sdk-path)"
BUNDLE_ID="app.clipbar.ClipBar"
LOCAL_CERT="ClipBar Local"
SIGNING_KEYCHAIN="${CLIPBAR_SIGNING_KEYCHAIN:-$HOME/Library/Keychains/login.keychain-db}"

find_local_identity() {
    security find-identity -p codesigning "$SIGNING_KEYCHAIN" \
        | awk -v label="$LOCAL_CERT" 'index($0, "\"" label "\"") {print $2}' \
        | sort -u
}

echo "==> Building $APP_NAME..."

mkdir -p "$BUNDLE/Contents/MacOS"
mkdir -p "$BUNDLE/Contents/Resources"

cp "$SCRIPT_DIR/Info.plist" "$BUNDLE/Contents/"

swiftc \
    -sdk "$SDK" \
    -target "arm64-apple-macosx13.0" \
    -framework Cocoa \
    -framework Carbon \
    -O \
    "$SCRIPT_DIR/Sources/ClipBar/AppDelegate.swift" \
    "$SCRIPT_DIR/Sources/ClipBar/ImageStore.swift" \
    "$SCRIPT_DIR/Sources/ClipBar/PeerStatus.swift" \
    "$SCRIPT_DIR/Sources/ClipBar/PrivateState.swift" \
    "$SCRIPT_DIR/Sources/ClipBar/main.swift" \
    -o "$BINARY"

# Sign with a stable local cert so TCC anchors trust on the cert identity
# (not the cdhash). Without this, Accessibility revokes on every rebuild.
# The cert is created once in the login keychain and reused thereafter.
# `-v` is intentionally omitted: self-signed certs are flagged
# CSSMERR_TP_NOT_TRUSTED but codesign still uses them. We sign by SHA1
# hash to disambiguate if multiple matching identities ever exist.
CERT_HASH=$(find_local_identity)

if [ -z "$CERT_HASH" ]; then
    echo "==> Creating local code-signing cert ($LOCAL_CERT)..."
    umask 077
    TMP=$(mktemp -d)
    trap 'rm -rf "$TMP"' EXIT
    cat > "$TMP/cert.conf" <<EOF
[req]
distinguished_name = req_dn
prompt = no
x509_extensions = v3
[req_dn]
CN = $LOCAL_CERT
[v3]
basicConstraints = critical, CA:false
keyUsage = critical, digitalSignature
extendedKeyUsage = critical, codeSigning
EOF
    openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
        -keyout "$TMP/key.pem" -out "$TMP/cert.pem" \
        -config "$TMP/cert.conf" -extensions v3 >/dev/null 2>&1
    P12_PASS=$(openssl rand -hex 16)
    openssl pkcs12 -export -inkey "$TMP/key.pem" -in "$TMP/cert.pem" \
        -out "$TMP/cert.p12" -name "$LOCAL_CERT" \
        -passout "pass:$P12_PASS" -macalg sha1 -keypbe PBE-SHA1-3DES -certpbe PBE-SHA1-3DES >/dev/null 2>&1
    # Grant access only while importing this new key. Never rewrite partition
    # lists for other signing keys, or guess the user's keychain password.
    security import "$TMP/cert.p12" -k "$SIGNING_KEYCHAIN" -P "$P12_PASS" \
        -T /usr/bin/codesign >/dev/null
    rm -rf "$TMP"
    trap - EXIT
    CERT_HASH=$(find_local_identity)
fi

if [[ ! "$CERT_HASH" =~ ^[[:xdigit:]]{40}$ ]]; then
    echo 'Expected exactly one ClipBar Local signing identity in the selected keychain.' >&2
    exit 1
fi
echo "==> Codesigning with $LOCAL_CERT ($CERT_HASH)..."
codesign --force --sign "$CERT_HASH" \
    --keychain "$SIGNING_KEYCHAIN" \
    --identifier "$BUNDLE_ID" \
    --timestamp=none \
    "$BINARY"

echo "==> Built: $BUNDLE"
echo ""
echo "Install: cp -r '$BUNDLE' /Applications/ && open /Applications/$APP_NAME.app"
