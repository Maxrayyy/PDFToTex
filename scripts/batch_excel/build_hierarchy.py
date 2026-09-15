"""用途：将约定的英文批次 JSON 导出为单工作表层级 Excel。

展示方式：批次、工序、阶段、步骤、表单在左侧分层，类别、对象和属性向右展开。
父级在其实际所属范围内合并单元格；不同表单即使同名，也不会误合并。
依赖：Python 3.10+、openpyxl；复制脚本时需保留同目录 common.py 和 temporal.py。
安装依赖（在本脚本目录执行）：python -m pip install -r requirements.txt

命令行用法（以下示例在本脚本目录执行；其他目录请使用脚本的完整路径）：
    python build_hierarchy.py /path/to/batch.json
    python build_hierarchy.py /path/to/batch.json --output-dir /path/to/output
    python build_hierarchy.py /path/to/batch.json --output-dir /path/to/output --overwrite

路径配置：也可修改下方 INPUT_JSON 和 OUTPUT_DIR，然后直接运行 python build_hierarchy.py。
建议填写绝对路径；相对路径以运行命令时的当前目录为准，而不是脚本所在目录。
命令行参数优先于脚本配置；OUTPUT_DIR 为 None 时，输出到输入 JSON 所在目录。
输出文件：<batch>_hierarchy.xlsx，其中 batch 自动从 JSON 中读取。
默认不覆盖已有文件；确认要替换时添加 --overwrite。不会修改输入 JSON。

输入结构：batch -> process[{subprocess, step: [{id, stage, form, 四类对象数组}]}]。
subprocess 是工序名称，form 是表单名称字符串；四类对象为
personnel/materials/equipment/environment，每个对象为 {id, name, attributes}。
兼容旧版各层 name 包装的数组结构；完整 JSON 示例见同目录 README.md。
日期显示为 YYYY-MM-DD，带时间时用连字符连接；只有时分时不补秒。
attributes 使用英文 snake_case 字段，值为文本、数字、布尔值或 null，不支持嵌套数组或对象。
新属性自动展开为 attribute/value 行，不需要修改字段清单。
仅合并同一表单、同一类别内完全一致的对象；角色或其他属性不同的保留。
空对象和空表单仍会显示；没有表单的空父级分支不单独增加行。
不添加表单次序、颜色或装饰样式；仅设置必要换行、列宽、日期格式和层级合并。
写入后回读核对成功才发布最终文件。
"""

INPUT_JSON = None  # 输入 JSON 的绝对路径，例如 "/absolute/path/to/batch.json"。
OUTPUT_DIR = None  # 输出目录的绝对路径；None 表示使用输入 JSON 所在目录。

from openpyxl import Workbook

from common import CATEGORIES, append, cli, excel_value, flat_schema, forms, layout


def build(data):
    flat = flat_schema(data)
    wb = Workbook()
    ws = wb.active
    ws.title = 'Hierarchy'
    rows = [['batch', 'subprocess', 'stage', 'step_id', 'form', 'category', 'id', 'name', 'attribute', 'value'] if flat else ['batch', 'process', 'subprocess', 'step', 'form', 'category', 'name', 'attribute', 'value']]
    identities = []
    for identity, prefix, form in forms(data):
        populated = False
        for ci, category in enumerate(CATEGORIES):
            for oi, obj in enumerate(form[category]):
                populated = True
                for key, value in (obj['attributes'].items() or [(None, None)]):
                    rows.append(prefix + [category] + ([obj['id']] if flat else []) + [obj['name'], key, excel_value(key or '', value)])
                    identities.append((0, *identity, ci, oi))
        if not populated:
            rows.append(prefix + [None] * (5 if flat else 4))
            identities.append((0, *identity, None, None))
    if len(rows) == 1:
        rows.append([data['batch']] + [None] * (len(rows[0]) - 1))
        identities.append((0, None, None, None, None, None, None))
    for row in rows:
        append(ws, row)
    merges = []
    # Prefix identities prevent equal names in different forms from being merged.
    for col in range(1, 9 if flat else 8):
        # 阶段文字即使相同，也仅在同一工序内合并；对象编号与名称共享对象边界。
        depth = min(col, 7)
        if flat and col == 3:
            depth = 4
        start = 2
        for index in range(1, len(identities) + 1):
            if index == len(identities) or identities[index][:depth] != identities[index - 1][:depth]:
                end = index + 1
                if end > start and rows[start - 1][col - 1] not in (None, ''):
                    merges.append((col, start, end))
                start = end + 1
    layout(ws, [25, 42, 34, 16, 38, 15, 16, 48, 38, 66] if flat else [25, 42, 34, 34, 38, 15, 48, 38, 66], merges)
    ws.freeze_panes = 'A2'
    ws.sheet_view.zoomScale = 75
    return wb, {'Hierarchy': rows}


def main(argv=None):
    return cli(build, 'hierarchy', argv, INPUT_JSON, OUTPUT_DIR)


if __name__ == '__main__':
    main()
