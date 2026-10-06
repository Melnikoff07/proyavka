"""Стенд «Проявки» без Telegram: http://127.0.0.1:8099/?browser — вход кодом, который печатается здесь."""
import tempfile
import time
from pathlib import Path
import harness

if __name__ == "__main__":
    conf = Path(tempfile.mkdtemp()) / "config.env"
    conf.write_text("CHAT_ID=900000000000000\n", encoding="utf-8")
    h = harness.start(uid=900_000_000_000_000, extra_env={"BOT_TOKEN": "", "CONFIG_FILE": str(conf), "DOMAIN": "test.sslip.io"})
    for f in sorted((Path(__file__).resolve().parent / "testdata").glob("*.JPG")):
        h.drop(f)
    code, link = h.fb.new_pair(h.fb.ADMIN)
    print("стенд:", h.url + "/?browser", "код:", h.fb.show_code(code), flush=True)
    while True:
        time.sleep(3600)
