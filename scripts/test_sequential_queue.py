import importlib.util
from pathlib import Path
import unittest
from types import SimpleNamespace


spec = importlib.util.spec_from_file_location('queue_runner', Path(__file__).with_name('run-sequential-queue.py'))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class PublicationPaths(unittest.TestCase):
    def test_cross_unit_batches_keep_source_directories(self):
        config = SimpleNamespace(publish_root='/data/optimized')
        for relative in ('U1/batches/A37Z201202605032/one.pdf',
                         'U3/20260808/A37Z201202605027/two.pdf'):
            self.assertEqual(runner.publication_path(Path('/input') / relative,
                             {'source_root': '/input'}, config),
                             Path('/data/optimized') / Path(relative).with_suffix('.tex'))

    def test_existing_single_batch_queue_keeps_output_path(self):
        config = SimpleNamespace(publish_root='/data/optimized/U1/batch')
        self.assertEqual(runner.publication_path(Path('/input/U1/batch/a.pdf'), {}, config),
                         Path('/data/optimized/U1/batch/a.tex'))

    def test_source_outside_declared_root_is_rejected(self):
        with self.assertRaises(ValueError):
            runner.publication_path(Path('/elsewhere/a.pdf'), {'source_root': '/input'},
                                    SimpleNamespace(publish_root='/data/optimized'))


if __name__ == '__main__':
    unittest.main()
