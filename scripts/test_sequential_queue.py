import importlib.util
from pathlib import Path
import unittest
from types import SimpleNamespace


spec = importlib.util.spec_from_file_location('queue_runner', Path(__file__).with_name('run-sequential-queue.py'))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class PublicationPaths(unittest.TestCase):
    def test_queue_scripts_use_refactored_package_paths(self):
        root = Path(__file__).parent
        for relative in ('run-sequential-queue.py', 'resume-none/run.py'):
            source = (root / relative).read_text(encoding='utf-8')
            self.assertNotIn('from texopt.stages', source)
            self.assertNotIn('from texopt.textio', source)
            self.assertIn('from texopt.runtime.stages', source)
            self.assertIn('from texopt.core.textio', source)

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
