"""Focused database tests for local product editing and batched card hydration."""
import os
import shutil
import sys
import tempfile
import types
import unittest

_IMPORT_ROOT = tempfile.mkdtemp(prefix='xianyu-db-import-')
os.environ['DB_PATH'] = os.path.join(_IMPORT_ROOT, 'singleton.sqlite')


def _install_lightweight_dependency_stubs():
    """Let DB-only tests run from a bare Python interpreter."""
    if 'aiohttp' not in sys.modules:
        sys.modules['aiohttp'] = types.ModuleType('aiohttp')
    if 'loguru' not in sys.modules:
        loguru = types.ModuleType('loguru')
        loguru.logger = types.SimpleNamespace(**{
            level: (lambda *args, **kwargs: None)
            for level in ('debug', 'info', 'warning', 'error')
        })
        sys.modules['loguru'] = loguru
    if 'PIL' not in sys.modules:
        pil = types.ModuleType('PIL')
        for name in ('Image', 'ImageDraw', 'ImageFont'):
            module = types.ModuleType(f'PIL.{name}')
            setattr(pil, name, module)
            sys.modules[f'PIL.{name}'] = module
        sys.modules['PIL'] = pil
    if 'cryptography.fernet' not in sys.modules:
        crypto = types.ModuleType('cryptography')
        fernet = types.ModuleType('cryptography.fernet')

        class InvalidToken(Exception):
            pass

        class Fernet:
            def __init__(self, key):
                self.key = key

            @staticmethod
            def generate_key():
                return b'unit-test-key'

            def encrypt(self, value):
                return value

            def decrypt(self, value):
                return value

        fernet.Fernet = Fernet
        fernet.InvalidToken = InvalidToken
        sys.modules['cryptography'] = crypto
        sys.modules['cryptography.fernet'] = fernet


_install_lightweight_dependency_stubs()
from db_manager import DBManager  # noqa: E402


class ItemLocalDataTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='xianyu-item-test-')
        self.db = DBManager(os.path.join(self.root, 'items.sqlite'))
        self.cookie_id = 'account-demo'
        self.item_id = '12345678901'
        self.assertTrue(self.db.save_cookie(self.cookie_id, 'test-cookie', user_id=1))
        self.assertTrue(self.db.save_item_basic_info(
            self.cookie_id, self.item_id, item_title='Original title',
            item_description='Original description', item_category='digital',
            item_price='10', item_detail='{"source":"synced"}',
        ))

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_structured_local_edit_updates_only_requested_fields(self):
        self.assertTrue(self.db.update_item_local_fields(self.cookie_id, self.item_id, {
            'item_title': 'Edited title', 'item_price': 12.5,
            'item_detail': '{"seller_notes":"delivery after payment"}',
        }))
        item = self.db.get_item_info(self.cookie_id, self.item_id)
        self.assertEqual(item['item_title'], 'Edited title')
        self.assertEqual(item['item_price'], '12.5')
        self.assertEqual(item['item_description'], 'Original description')
        self.assertEqual(item['item_detail_parsed']['seller_notes'], 'delivery after payment')

    def test_batch_item_query_hydrates_delivery_card_without_per_item_lookup(self):
        card_id = self.db.create_card('Demo delivery', 'text', text_content='hello', user_id=1)
        self.assertTrue(self.db.update_item_delivery_card(self.cookie_id, self.item_id, card_id))
        self.assertTrue(self.db.get_item_info(self.cookie_id, self.item_id)['auto_delivery_enabled'])

        items = self.db.get_items_with_delivery_cards([self.cookie_id], user_id=1)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['delivery_card_name'], 'Demo delivery')
        self.assertTrue(items[0]['delivery_card_enabled'])
        isolated_items = self.db.get_items_with_delivery_cards([self.cookie_id], user_id=999)
        self.assertIsNone(isolated_items[0]['delivery_card_name'])
        self.assertFalse(isolated_items[0]['delivery_card_enabled'])

        self.assertTrue(self.db.update_item_auto_delivery_enabled(self.cookie_id, self.item_id, False))
        disabled = self.db.get_item_info(self.cookie_id, self.item_id)
        self.assertFalse(disabled['auto_delivery_enabled'])
        self.assertIsNone(disabled['delivery_card_id'])


if __name__ == '__main__':
    unittest.main()
