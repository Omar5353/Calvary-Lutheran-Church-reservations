#!/usr/bin/env bash
# Run the whole suite. Add POSTGRES_URL=... to test against Postgres instead
# of SQLite, e.g.
#   POSTGRES_URL="postgresql://user:pw@host:5432/db" ./run_tests.sh
#
# test_approval needs a local mail capture server, which this script starts
# and stops for you. No real email is ever sent by the tests.
set -u
backend="sqlite"; [ -n "${POSTGRES_URL:-}" ] && backend="postgres"
echo "Running tests against: $backend"; echo

python3 mailcatcher.py 8025 /tmp/mail.json >/tmp/catcher.log 2>&1 &
CATCHER=$!
sleep 2

fail=0
for t in test_smoke test_limits test_notify test_pw test_concurrency test_approval test_pending_flow test_mail_failures; do
  # match only a real failure line ("FAIL  label"), not the word FAILED
  # appearing inside a passing check's output
  if out=$(python3 "$t.py" 2>&1) && ! grep -qE "^FAIL " <<<"$out"; then
    printf '  %-18s ok\n' "$t"
  else
    printf '  %-18s FAILED\n' "$t"; echo "$out" | tail -14; fail=1
  fi
done

kill $CATCHER 2>/dev/null
wait $CATCHER 2>/dev/null
echo; [ $fail -eq 0 ] && echo "All tests passed on $backend." || echo "Some tests failed."
exit $fail
