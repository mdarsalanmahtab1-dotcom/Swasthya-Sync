#!/usr/bin/env bash
# Checks that staff endpoints reject anonymous requests (401) and kiosk endpoints stay open.
# Usage: bash backend/scripts/smoke_auth.sh https://swasthyasync-api-XXXX.asia-south1.run.app
set -u
API="${1:-http://localhost:8000}"
fail=0
check() { # method path expected
  code=$(curl -s -o /dev/null -w "%{http_code}" -X "$1" "$API$2")
  if [ "$code" = "$3" ]; then echo "OK   $1 $2 -> $code"; else echo "FAIL $1 $2 -> $code (expected $3)"; fail=1; fi
}
# Protected (no token -> 401)
check GET  /api/admin/export-db 401
check GET  /api/admin/patients 401
check GET  /api/triage/queue 401
check GET  /api/queue 401
check GET  /api/session/00000000-0000-0000-0000-000000000000 401
check GET  /api/patient/x/summaries 401
check GET  /api/record/00000000-0000-0000-0000-000000000000 401
# Kiosk / public (stay open)
check GET  /health 200
check GET  /api/departments 200
exit $fail
