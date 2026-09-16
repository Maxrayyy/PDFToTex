"""用途：两个 Excel 导出脚本共用的中文显示字典，不修改 JSON 的英文键或值。

无需单独运行，也无需配置批次路径。复制导出脚本时请保留本文件。
新增属性时，在 ATTRIBUTE_LABELS 中添加 'english_key': '中文名称'，然后重新导出。
未登记的属性会明确报错，不猜测翻译、不静默丢弃数据。仅修改显示文字无需改提取脚本。
STRUCTURAL_LABELS 用于旧版多层结构；扁平结构的 subprocess 表示大工序。
"""

CATEGORY_LABELS = {'personnel': '人员', 'materials': '物料', 'equipment': '设备', 'environment': '环境'}
STRUCTURAL_LABELS = {
    'batch': '批次', 'process': '大工序', 'subprocess': '阶段', 'stage': '阶段',
    'step': '步骤', 'step_id': '步骤ID', 'form': '步骤/表单', 'id': '记录ID',
    'name': '对象名称', 'category': '类别', 'attribute': '属性', 'value': '值',
}
ATTRIBUTE_LABELS = {
    'chamber_stable_duration_at_minus_80_c': '腔室温度持续稳定在-80℃时长',
    'carbon_dioxide_pressure': '二氧化碳压力', 'code': '代码',
    'associated_freezing_rack': '关联冻存架', 'cryobag_count': '冻存袋数量（袋）',
    'manufacturer': '厂家', 'differential_pressure': '压差', 'compressed_air_pressure': '压缩空气压力',
    'sampling_location': '取样地点', 'name': '名称', 'brand': '品牌', 'brand_model': '品牌/型号',
    'model': '型号', 'room_name_and_code': '房间名称及编码', 'lot_number': '批号',
    'lot_or_identifier': '批号/编号', 'operating_room': '操作间', 'expiry_date': '效期至',
    'quantity': '数量', 'date': '日期', 'time': '时间', 'meets_requirements': '是否符合要求',
    'validity': '有效期', 'valid_until': '有效期至', 'effective_until': '有效至',
    'label_count': '标签数量', 'calibration_validity': '校准有效期',
    'calibration_valid_until': '校准有效期至', 'sample_code': '样品代码',
    'sample_lot_number': '样品批号', 'sample_category': '样品类别',
    'sample_specification': '样品规格', 'sample_specification_quantity': '样品规格/数量',
    'sample_transport_conditions': '样品运输条件', 'inspection_confirmation': '检查确认',
    'oxygen_pressure': '氧气压力', 'test_date': '测试日期', 'test_program': '测试程序',
    'test_result': '测试结果', 'rinsing_duration': '润洗时长', 'cleaning_valid_until': '清洁效期',
    'temperature': '温度', 'humidity': '湿度', 'filter_status': '滤器状态',
    'sample_temperature_at_next_step': '点击下一步时样品温度', 'next_step_time': '点击进入下一步时间',
    'condition_intact': '状态是否完好', 'production_manufacturer': '生产厂家',
    'production_date': '生产日期', 'bioreactor_vessel_id': '生物反应器罐体编号',
    'compliance_confirmation': '确认是否符合要求', 'confirmation_result': '确认结果',
    'program_start_time': '程序启动时间', 'controlled_freezing_end_time': '程控降温结束时间',
    'specification': '规格', 'role': '角色', 'equipment_model': '设备型号',
    'equipment_id': '设备编号', 'equipment_code': '设备编码', 'reagent_source': '试剂来源',
    'catalog_number': '货号', 'storage_conditions': '储存条件', 'filtered_product_name': '过滤产品名称',
    'filtered_product_lot_number': '过滤产品批号', 'filter_serial_number': '过滤器序列号',
    'requested_tests': '送检项目', 'transferred_bag_count': '递交数量（袋）',
    'department': '部门', 'preparation_lot_number': '配制批号',
    'pretreatment_start_date': '预处理开始日期', 'pretreatment_end_date': '预处理结束日期',
    'strain_id': '菌种编号', 'passage': '代次', 'strain_lot_number': '菌种批号',
    'maximum_range': '最大量程', 'metrology_valid_until': '计量有效期至',
    'total_quantity': '总数量', 'sampling_quantity': '取样量', 'test_quantity': '检验量',
    'retained_quantity': '留样量', 'sampling_date': '取样日期', 'sampling_time': '取样时间',
    'production_lot_number': '生产批号', 'test_article_name': '检品名称',
    'transport_temperature_requirement': '样品运输温度要求', 'room_id': '房间编号',
    'room_name': '房间名称', 'sampling_position': '取样位置', 'sample_condition': '样品状态',
    'maximum_value': '最大值', 'minimum_value': '最小值', 'maximum_time': '最大值时间',
    'minimum_time': '最小值时间', 'equipment_name': '设备名称', 'software_version': '软件版本',
    'recording_interval': '记录间隔', 'recording_start_time': '开始记录',
    'recording_end_time': '停止记录', 'start_time': '起始时间', 'end_time': '结束时间',
    'maximum_temperature': '温度最大值', 'minimum_temperature': '温度最小值',
    'position': '岗位', 'testing_organization': '委托检验单位/机构',
    'sampling_method': '取样方法', 'average_temperature': '平均温度',
    'notes': '备注', 'approved': '是否批准',
}


def structural_label(key, flat=False):
    return '大工序' if flat and key == 'subprocess' else STRUCTURAL_LABELS[key]


def require_labels(form_records):
    missing = sorted({key for _, _, categories in form_records
                      for objects in categories.values() for obj in objects
                      for key in obj['attributes']} - ATTRIBUTE_LABELS.keys())
    if missing:
        raise ValueError('请在 labels_zh.py 的 ATTRIBUTE_LABELS 中补充中文映射: ' + ', '.join(missing))
