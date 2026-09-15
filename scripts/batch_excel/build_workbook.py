"""用途：将约定的英文批次 JSON 导出为分类 Excel，每个对象占一行。

输出工作表：personnel（人员）、materials（物料）、equipment（设备）、environment（环境）。
依赖：Python 3.10+、openpyxl；复制脚本时需保留同目录 common.py 和 temporal.py。
安装依赖（在本脚本目录执行）：python -m pip install -r requirements.txt

命令行用法（以下示例在本脚本目录执行；其他目录请使用脚本的完整路径）：
    python build_workbook.py /path/to/batch.json
    python build_workbook.py /path/to/batch.json --output-dir /path/to/output
    python build_workbook.py /path/to/batch.json --output-dir /path/to/output --overwrite

路径配置：也可修改下方 INPUT_JSON 和 OUTPUT_DIR，然后直接运行 python build_workbook.py。
建议填写绝对路径；相对路径以运行命令时的当前目录为准，而不是脚本所在目录。
命令行参数优先于脚本配置；OUTPUT_DIR 为 None 时，输出到输入 JSON 所在目录。
输出文件：<batch>_categories.xlsx，其中 batch 从 JSON 中读取，不需要另填批次号。
默认不覆盖已有文件；确认要替换时添加 --overwrite。不会修改输入 JSON。

输入结构：batch -> process[{subprocess, step: [{id, stage, form, 四类对象数组}]}]。
subprocess 是工序名称，form 是表单名称字符串；对象为 {id, name, attributes}。
兼容旧版各层 name 包装的数组结构；完整 JSON 示例见同目录 README.md。
日期显示为 YYYY-MM-DD，带时间时用连字符连接；只有时分时不补秒。
attributes 使用英文 snake_case 字段，值为文本、数字、布尔值或 null，不支持嵌套数组或对象。
新增属性会自动生成新列；与固定表头重名的属性列会增加 attribute_ 前缀。
仅合并同一表单、同一类别内名称和全部属性完全一致的对象；角色或日期不同的不会合并。
不添加表单次序、来源列、颜色或装饰样式。写入后回读核对成功才发布最终文件。
"""

INPUT_JSON = None  # 输入 JSON 的绝对路径，例如 "/absolute/path/to/batch.json"。
OUTPUT_DIR = None  # 输出目录的绝对路径；None 表示使用输入 JSON 所在目录。

from openpyxl import Workbook

from common import CATEGORIES, PREFIX, append, cli, excel_value, flat_schema, forms, layout


def build(data):
    flat = flat_schema(data)
    groups = {category: [] for category in CATEGORIES}
    for _, prefix, form in forms(data):
        for category in CATEGORIES:
            for obj in form[category]:
                groups[category].append((prefix + ([obj['id']] if flat else []) + [obj['name']], obj['attributes']))
    wb = Workbook()
    wb.remove(wb.active)
    expected = {}
    for category, objects in groups.items():
        fields = list(dict.fromkeys(key for _, attrs in objects for key in attrs))
        headers = ['batch', 'subprocess', 'stage', 'step_id', 'form', 'id', 'name'] if flat else list(PREFIX)
        prefix_size = len(headers)
        for key in fields:
            label = key
            while label in headers:
                label = 'attribute_' + label
            headers.append(label)
        ws = wb.create_sheet(category)
        expected[category] = [headers]
        append(ws, headers)
        for prefix, attrs in objects:
            row = prefix + [excel_value(key, attrs.get(key)) for key in fields]
            append(ws, row)
            expected[category].append(row)
        widths = ([25, 42, 34, 16, 38, 16, 48] if flat else [25, 42, 34, 34, 38, 48]) + [min(60, max(26, len(h) + 8)) for h in headers[prefix_size:]]
        layout(ws, widths)
        ws.freeze_panes = 'H2' if flat else 'G2'
        ws.auto_filter.ref = ws.dimensions
    return wb, expected


def main(argv=None):
    return cli(build, 'categories', argv, INPUT_JSON, OUTPUT_DIR)


if __name__ == '__main__':
    main()
