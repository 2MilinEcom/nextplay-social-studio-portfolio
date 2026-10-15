#!/bin/sh
set -eu
mkdir -p /data
chown nextplay:nextplay /data
exec gosu nextplay python -m social_platform.server --host 0.0.0.0 --data /data
