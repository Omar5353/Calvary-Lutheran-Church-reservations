"""
Tiny SMTP server that captures messages to a JSON file instead of delivering
them. Used by the tests so nothing real is ever emailed.

    python3 mailcatcher.py 8025 /tmp/mail.json
"""
import asyncio, email, json, sys
from aiosmtpd.controller import Controller

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8025
OUT = sys.argv[2] if len(sys.argv) > 2 else "/tmp/mail.json"

class Handler:
    async def handle_DATA(self, server, session, envelope):
        msg = email.message_from_bytes(envelope.content)
        parts = {}
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_maintype() == "multipart":
                    continue
                parts[part.get_content_subtype()] = part.get_payload(decode=True).decode("utf-8", "replace")
        else:
            parts["plain"] = msg.get_payload(decode=True).decode("utf-8", "replace")
        rec = {
            "to": envelope.rcpt_tos, "from": envelope.mail_from,
            "subject": msg["Subject"], "reply_to": msg["Reply-To"],
            "cc": msg["Cc"], "text": parts.get("plain", ""), "html": parts.get("html", ""),
        }
        try:
            data = json.load(open(OUT))
        except Exception:
            data = []
        data.append(rec)
        json.dump(data, open(OUT, "w"), indent=1)
        return "250 Message accepted"

if __name__ == "__main__":
    json.dump([], open(OUT, "w"))
    c = Controller(Handler(), hostname="127.0.0.1", port=PORT)
    c.start()
    print(f"mailcatcher listening on {PORT}, writing {OUT}", flush=True)
    try:
        asyncio.get_event_loop().run_forever()
    except KeyboardInterrupt:
        c.stop()
