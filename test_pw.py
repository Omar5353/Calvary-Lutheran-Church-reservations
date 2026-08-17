import os, tempfile
os.environ["CALVARY_DB"] = os.path.join(tempfile.mkdtemp(), "p.db")
import app
def check(l, c): print(("PASS  " if c else "FAIL  ") + l); assert c, l

cases = {
    "plain 16 chars":        ("rjcbpgpfctylsltm", "rjcbpgpfctylsltm"),
    "normal spaces":         ("rjcb pgpf ctyl sltm", "rjcbpgpfctylsltm"),
    "non-breaking spaces":   ("rjcb\xa0pgpf\xa0ctyl\xa0sltm", "rjcbpgpfctylsltm"),
    "mixed spaces":          ("rjcb pgpf\xa0ctyl sltm", "rjcbpgpfctylsltm"),
    "leading/trailing":      ("  rjcb pgpf ctyl sltm \n", "rjcbpgpfctylsltm"),
    "tabs":                  ("rjcb\tpgpf\tctyl\tsltm", "rjcbpgpfctylsltm"),
}
for label, (raw, want) in cases.items():
    os.environ["GMAIL_APP_PASSWORD"] = raw
    got = app.app_password()
    check(f"{label} -> clean", got == want)
    got.encode("ascii")  # would raise if not sanitized

os.environ["GMAIL_APP_PASSWORD"] = "   "
check("whitespace only is treated as unset", app.app_password() is None)
os.environ["GMAIL_APP_PASSWORD"] = "pass-wörd-with-accents"
check("truly non-ascii is rejected, not crashed", app.app_password() is None)
os.environ.pop("GMAIL_APP_PASSWORD")
check("unset returns None", app.app_password() is None)

# end to end: the exact string from the screenshot must now log in cleanly
os.environ["GMAIL_APP_PASSWORD"] = "rjcb\xa0pgpf ctyl sltm"
import importlib; importlib.reload(app); app.init_db()

# Start from a clean table. With SQLite each run gets its own temp file, but a
# shared Postgres keeps rows between test files, so clear it explicitly.
with app.Db() as _db:
    _db.execute("DELETE FROM reservations")
sent = []
class Fake:
    def __init__(s,h,p,timeout=None): pass
    def __enter__(s): return s
    def __exit__(s,*a): return False
    def login(s,u,p):
        ("\0"+u+"\0"+p).encode("ascii")   # this is what smtplib does
        sent.append(p)
    def send_message(s,m): pass
app.smtplib.SMTP_SSL = Fake
ok, msg = app.send_test_email()
check("test email path succeeds", ok)
check("password reached gmail clean", sent == ["rjcbpgpfctylsltm"])
print("\nAll password tests passed.")
