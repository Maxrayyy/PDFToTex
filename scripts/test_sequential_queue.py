"""用途：测试串行队列脚本的发布路径和模块引用，不执行真实 PDF 处理任务。

运行方法（在 PDFToTex 根目录执行）：
    PYTHONPATH=Lexoid:pipeline python -m unittest discover -s scripts -p test_sequential_queue.py
若环境已安装项目包，也可省略 PYTHONPATH；其他目录请改用对应绝对路径。
依赖：unittest 为 Python 标准库，但导入被测脚本仍需要 pypdfium2、texopt、lexoid
及其导入依赖；推荐使用项目已有的完整流水线环境。

无需队列 JSON、实际 PDF 或模型凭据，不需要修改脚本内的路径配置。
测试使用构造的路径检查跨单元目录保留、旧单批次路径兼容、根目录外文件拒绝，
并确认脚本引用重构后的包路径；不调用 main，不写正式产物，不启动模型请求。
退出码 0 表示测试通过；若是依赖导入报错，请先配置测试环境，不要直接运行生产队列代替测试。
"""

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
