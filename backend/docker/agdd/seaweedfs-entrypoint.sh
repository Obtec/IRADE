#!/bin/sh
# S3-compatible Object Storage (SeaweedFS). Credentials come from env; keys are SHA-256 content addresses.
set -eu
mkdir -p /data /tmp/seaweed
cat > /tmp/seaweed/s3.json <<JSON
{"identities": [{"name": "agdd",
  "credentials": [{"accessKey": "${S3_ACCESS_KEY}", "secretKey": "${S3_SECRET_KEY}"}],
  "actions": ["Admin", "Read", "Write", "List", "Tagging"]}]}
JSON
exec weed server -dir=/data -ip.bind=0.0.0.0 -master.volumeSizeLimitMB=1024 -volume.max=0 \
  -s3 -s3.port=8333 -s3.config=/tmp/seaweed/s3.json
