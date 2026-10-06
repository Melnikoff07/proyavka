"""Все тесты подряд: python tests/run_all.py [имя ...]. Тесты печатают OK/FAIL и завершаются harness.done() (гасит
процессы-работники и выходит), поэтому итог считаем по строкам FAIL и коду выхода. Сети не нужно: Telegram подменён, каталог сообщества берётся из репозитория."""
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ORDER = ["test_stage0", "test_stage1", "test_stage2", "test_stage3", "test_crop", "test_stage4", "test_luts", "test_looks",
         "test_guards", "test_raw", "test_devices", "test_web", "test_push", "test_albums", "test_media", "test_hub", "test_film", "test_viewcache", "test_trash"]
TIMEOUT = 600


def run(name):
    t = time.time()
    try:
        p = subprocess.run([sys.executable, "-u", str(HERE / f"{name}.py")], cwd=HERE, capture_output=True, timeout=TIMEOUT,
                           env=dict(os.environ, PYTHONIOENCODING="utf-8", COMMUNITY_URL=""))
        out = p.stdout.decode("utf-8", "replace")
        err = p.stderr.decode("utf-8", "replace")
    except subprocess.TimeoutExpired as e:
        return name, 0, ["TIMEOUT"], (e.stdout or b"").decode("utf-8", "replace"), "", time.time() - t
    lines = out.splitlines()
    ok = sum(1 for l in lines if l.startswith("OK"))
    bad = [l for l in lines if l.startswith("FAIL")]
    if p.returncode not in (0, None) and not bad:
        bad = [f"exit code {p.returncode}"]
    return name, ok, bad, out, err, time.time() - t


if __name__ == "__main__":
    try:                                    # консоль Windows (cp1251) не должна падать на «→» в выводе упавшего теста
        sys.stdout.reconfigure(errors="replace")
    except AttributeError:
        pass
    sys.path.insert(0, str(HERE))
    import synth
    synth.ensure()
    names = [n.removesuffix(".py") for n in sys.argv[1:]] or ORDER
    failed = 0
    for n in names:
        name, ok, bad, out, err, dt = run(n)
        print(f"{'FAIL' if bad else 'ok  '} {name}: {ok} проверок, {dt:.0f} с", flush=True)
        if bad:
            failed += 1
            print("\n".join("    " + l for l in bad))
            print("    --- вывод ---\n" + "\n".join("    " + l for l in out.splitlines()[-25:]))
            print("    --- stderr ---\n" + "\n".join("    " + l for l in err.splitlines()[-25:]))
    print(f"\nнаборов с ошибками: {failed} из {len(names)}")
    sys.exit(1 if failed else 0)
