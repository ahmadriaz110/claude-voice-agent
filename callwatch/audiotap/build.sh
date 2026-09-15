#!/bin/sh
# Build audiotap. Embeds embedded-Info.plist (usage strings + bundle id) into the binary so TCC
# can show a proper prompt. Signs with the Apple Development identity when one exists
# (stable identity: TCC grants survive rebuilds), else ad-hoc (grants are per-build).
set -e
cd "$(dirname "$0")"
swiftc -O -swift-version 5 -target arm64-apple-macos14.2 \
  -framework CoreAudio -framework AVFoundation -framework Foundation \
  -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist -Xlinker embedded-Info.plist \
  -o audiotap main.swift
IDENT=$(security find-identity -v -p codesigning 2>/dev/null | grep -o '"Apple Development[^"]*"' | head -1 | tr -d '"')
SIGNED=""
if [ -n "$IDENT" ]; then
  # perl alarm: never hang on a keychain dialog
  if perl -e 'alarm 25; exec @ARGV' -- codesign --force --sign "$IDENT" --identifier com.voicemode.audiotap --timestamp=none audiotap 2>/dev/null; then
    SIGNED="$IDENT"
  fi
fi
if [ -z "$SIGNED" ]; then
  codesign --force --sign - --identifier com.voicemode.audiotap audiotap; SIGNED="ad-hoc"
fi
echo "signed: $SIGNED"
codesign -dv audiotap 2>&1 | grep -E "^Identifier|TeamIdentifier" || true
ls -la audiotap
# Note: the plist is deliberately NOT named Info.plist. A directory holding an
# Info.plist next to the executable is taken for a bundle by codesign, which then
# seals the whole directory (_CodeSignature/CodeResources) and any later edit to
# main.swift invalidates the signature.
