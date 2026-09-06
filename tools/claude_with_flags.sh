#!/bin/bash
# Start the Claude desktop app with Chromium's occlusion throttling off, so a
# covered window still takes a background accessibility write. No-op if running.
pgrep -x Claude >/dev/null && exit 0
sleep 5
exec open -a Claude --args --disable-backgrounding-occluded-windows --disable-renderer-backgrounding
