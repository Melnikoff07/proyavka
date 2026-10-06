"""Пулы процессов для отрисовки: быстрый (экран, превью), тяжёлый (полный размер) и потоки для загрузок в Telegram."""

import os
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

from .config import FAST_WORKERS, HEAVY_WORKERS, WORKER_NICE, log
from .jobs import job_warm


FAST = HEAVY = None


NET = ThreadPoolExecutor(max_workers=2, thread_name_prefix="net")   # загрузки в Telegram


def _worker_init(nice):
    try:
        os.nice(nice)
    except (OSError, AttributeError):   # AttributeError — Windows, где нет nice (запуск для разработки)
        pass


def init_pools():
    global FAST, HEAVY
    import multiprocessing as mp
    ctx = mp.get_context("spawn")
    FAST = ProcessPoolExecutor(max_workers=FAST_WORKERS, mp_context=ctx,
                               initializer=_worker_init, initargs=(WORKER_NICE,))
    HEAVY = ProcessPoolExecutor(max_workers=HEAVY_WORKERS, mp_context=ctx, max_tasks_per_child=8,
                                initializer=_worker_init, initargs=(WORKER_NICE + 5,))
    warm = [FAST.submit(job_warm) for _ in range(FAST_WORKERS)] + [HEAVY.submit(job_warm)]
    for f in warm:
        f.result()
    log.info("render pools ready: fast=%d heavy=%d", FAST_WORKERS, HEAVY_WORKERS)
