#!/usr/bin/env bash
# Run the whole suite. Add POSTGRES_URL=... to test against Postgres instead
# of SQLite, e.g.
#   POSTGRES_URL="postgresql://user:pw@host:5432/db" ./run_tests.sh
set -u
backend="sqlite"; [ -n "${POSTGRES_URL:-}" ] && backend="postgres"
echo "Running tests against: $backend"; echo
fail=0
for t in test_smoke test_limits test_notify test_pw test_concurrency; do
  if out=$(python3 "$t.py" 2>&1) && ! grep -q "FAIL" <<<"$out"; then
    printf '  %-18s ok\n' "$t"
  else
    printf '  %-18s FAILED\n' "$t"; echo "$out" | tail -12; fail=1
  fi
done
echo; [ $fail -eq 0 ] && echo "All tests passed on $backend." || echo "Some tests failed."
exit $fail
