import unittest
import os
from circus.config import get_config
from circus.arbiter import Arbiter
from circus.watcher import Watcher
from circus.process import Process
from circus.sockets import CircusSocket

HERE = os.path.join(os.path.dirname(__file__))

_CONF = {
    'reload1': os.path.join(HERE, 'reload1.ini'),
    'reload2': os.path.join(HERE, 'reload2.ini'),
    'reload3': os.path.join(HERE, 'reload3.ini'),
}


def hook(watcher, hook_name):
    pass


class TestConfig(unittest.TestCase):

    def test_reload(self):
        a = Arbiter.load_from_config(_CONF['reload1'])
        a.initialize()
        self.assertEqual(len(a.watchers), 1)
        a.reload_from_config(_CONF['reload2'])
        self.assertEqual(len(a.watchers), 2)
        a.reload_from_config(_CONF['reload3'])
        self.assertEqual(len(a.watchers), 1)
        a.stop()
