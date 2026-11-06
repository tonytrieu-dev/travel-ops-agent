#!/bin/sh
# Run only against the isolated Compose deployment.
set -eu
compose() { docker compose -f compose.zero-trust.yml "$@"; }
# The gateway can reach the API, but has neither a database route nor database DNS.
compose exec -T gateway wget -qO- http://api:8000/api/auth/config >/dev/null
database_address=$(compose exec -T postgres hostname -i)
if compose exec -T gateway nc -z -w2 "$database_address" 5432; then
    echo 'FAIL: gateway reached database' >&2
    exit 1
fi
device_id=${1:?pass an issued device ID}
curl --fail --silent --cacert .device-certificates/ca.crt --cert ".device-certificates/$device_id.crt" --key ".device-certificates/$device_id.key" https://localhost:8443/api/auth/config >/dev/null
# No certificate is refused even when the caller supplies forged gateway headers.
status=$(curl -sk -o /dev/null -w '%{http_code}' -H 'X-Device-Verified: SUCCESS' https://localhost:8443/api/auth/config)
[ "$status" != 200 ]
# Runtime credentials may read/append evidence but cannot mutate or dismantle it.
compose exec -T api python -m scripts.check_runtime_database
