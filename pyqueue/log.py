"""Logging setup (port of server/log.js: info/warn/error files + console)."""

import logging
import os

_counter = 0


def make_logger(params):
    """Build a logger writing info.log / warn.log / error.log in LOG_DIR."""
    global _counter
    _counter += 1
    logger = logging.getLogger('pyqueue.%d' % _counter)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')

    log_dir = params.get('LOG_DIR')
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
        for name, level in (('info', logging.INFO),
                            ('warn', logging.WARNING),
                            ('error', logging.ERROR)):
            handler = logging.FileHandler(os.path.join(log_dir, name + '.log'))
            handler.setLevel(level)
            handler.setFormatter(formatter)
            logger.addHandler(handler)

    if params.get('PRINT_LOGS'):
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    if not logger.handlers:
        logger.addHandler(logging.NullHandler())

    return logger
