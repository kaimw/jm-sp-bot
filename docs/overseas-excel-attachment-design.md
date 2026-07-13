# 海外电商/独立站订单附件解析 — 设计

---

## 一、附件格式说明

来自海外电商平台（亚马逊/独立站等）的CRM订单，附件中包含一个 **物料-物流合并的Excel**：

```
Excel结构（示例）：
┌──────────┬────────┬──────┬──────────┬──────────────┬──────────────────┐
│ 物料编码  │ 物料名  │ 数量 │ 收货人   │ 联系电话      │ 收货地址          │
├──────────┼────────┼──────┼──────────┼──────────────┼──────────────────┤
│ SKU-001  │ 扫描仪  │  2   │ John     │ +1-xxx-xxxx  │ 123 Main St, NY  │
│ SKU-002  │ 支架    │  5   │ John     │ +1-xxx-xxxx  │ 123 Main St, NY  │
│ SKU-001  │ 扫描仪  │  1   │ Alice    │ +1-yyy-yyyy  │ 456 Oak Ave, LA  │
│ SKU-003  │ 标定板  │  3   │ Bob      │ +1-zzz-zzzz  │ 789 Pine Rd, SF   │
└──────────┴────────┴──────┴──────────┴──────────────┴──────────────────┘
```

**每行 = 一个发货单元**：物料+数量+发给谁+发到哪

**跟目前流程的区别：**

| 对比 | 目前的流程（国内/渠道订单） | 海外电商/独立站订单 |
|------|--------------------------|-------------------|
| 附件格式 | PI/合同PDF，一个订单一个收货方 | Excel，多行多收货方 |
| 收货信息 | 从PI/合同正文中正则提取 | 从Excel结构化列提取 |
| 拆单 | 不需要拆（一个收货方） | 按收货方分拆，每人一个发货通知 |
| 物料明细 | 订单行直接关联物料 | Excel每行是一个独立的发货单元 |

---

## 二、系统处理流程

### 2.1 附件类型识别

```
附件上传到CRM → 中台同步附件
  → 判断附件类型：
  ├─ .xlsx/.xls → 尝试解析为"物料-物流Excel"
  │   ├─ 有"收货人""物料"列 → 走Excel拆单流程
  │   └─ 不是该格式 → 走原流程（PI/合同文本提取）
  └─ .pdf/.docx → 走原流程
```

### 2.2 Excel解析

```
解析Excel每一行：
  Row 1: {material_code, material_name, qty, contact, phone, address}
  Row 2: {material_code, material_name, qty, contact, phone, address}
  ...

分组（按收货人+地址）：
  Group 1 (John, 123 Main St):
    ├─ SKU-001 扫描仪 * 2
    └─ SKU-002 支架 * 5
  Group 2 (Alice, 456 Oak Ave):
    └─ SKU-001 扫描仪 * 1
  Group 3 (Bob, 789 Pine Rd):
    └─ SKU-003 标定板 * 3
```

### 2.3 订单与发货通知的对应关系

```
CRM订单 #ORD-001
  │
  ├─ 中台订单 #MP-001（汇总单）
  │   ├─ 总物料：SKU-001*3, SKU-002*5, SKU-003*3
  │   ├─ 总金额：汇总
  │   └─ 总数量：汇总
  │
  ├─ 发货通知 #DN-001（→ John, 123 Main St）
  │   ├─ SKU-001 * 2
  │   └─ SKU-002 * 5
  │
  ├─ 发货通知 #DN-002（→ Alice, 456 Oak Ave）
  │   └─ SKU-001 * 1
  │
  └─ 发货通知 #DN-003（→ Bob, 789 Pine Rd）
      └─ SKU-003 * 3
```

### 2.4 ERP制单

**方案A（一期推荐）：按收货方拆分为多个ERP单**

```
MP-001汇总单 → 预审通过
  ├─ ERP单 #1（对应DN-001，深圳主体，备注"发至 John，123 Main St"）
  ├─ ERP单 #2（对应DN-002，深圳主体，备注"发至 Alice，456 Oak Ave"）
  └─ ERP单 #3（对应DN-003，深圳主体，备注"发至 Bob，789 Pine Rd"）
```

**方案B（简化）：一个ERP单，明细行备注收货方**

```
MP-001 → 一个ERP单
  ├─ 明细行1：SKU-001*2（备注：John/123 Main St）
  ├─ 明细行2：SKU-002*5（备注：John/123 Main St）
  ├─ 明细行3：SKU-001*1（备注：Alice/456 Oak Ave）
  └─ 明细行4：SKU-003*3（备注：Bob/789 Pine Rd）
```

**推荐方案A**——虽然ERP单多，但OMS下推时可以按单拆分发货，物流操作更清晰。

---

## 三、数据模型扩展

### 3.1 附件解析结果扩展

```python
# 新增：物料-物流行
@dataclass
class LogisticsRow:
    material_code: str
    material_name: str
    quantity: int
    receipt_contact: str
    receipt_phone: str
    receipt_address: str
    group_key: str  # 按(收货人+地址)生成的唯一key，用于分组

# 新增：Excel解析结果
@dataclass
class ExtractedExcelRows:
    rows: list[LogisticsRow]
    groups: dict[str, list[LogisticsRow]]  # group_key → rows
    total_materials: dict[str, int]  # 物料汇总（用于预审）
    row_count: int
    confidence: int
```

### 3.2 发货通知生成扩展

```python
# 现有 create_delivery_notice(order) 扩展：
# 如果订单有Excel附件：
#   → 按group拆分为多个notice
#   → 每个notice有自己的收货人+地址+物料明细
# 如果没有Excel附件：
#   → 走原逻辑，一个order一个notice
```

---

## 四、预审影响

### 4.1 物料匹配

```
物料匹配按汇总后的物料总量走（跟现有逻辑一致）
  ├─ 汇总：SKU-001*3, SKU-002*5, SKU-003*3
  └─ 别名匹配 → 金蝶料号
```

### 4.2 库存检查

```
按汇总总量检查（跟现有逻辑一致）
  ├─ 总共需要SKU-001*3
  └─ 查库存是否够3台
```

### 4.3 特殊需求处理

```
如果Excel中有"备注"列：
  ├─ 提取特殊需求（如"贴中文标签"）
  └─ 关联到对应收货行/DN
```

---

## 五、一期实现方案

| 模块 | 改动量 | 说明 |
|------|--------|------|
| 附件类型识别 | 小 | PDF/Excel分流 |
| Excel解析（openpyxl） | 中 | 读取列标题、提取行数据 |
| 按收货方分组 | 小 | (收货人+地址)去重 |
| 汇总物料（用于预审） | 小 | 多行汇总为总需求 |
| 生成多个发货通知 | 中 | 现有create_delivery_notice扩展 |
| 管理台预览拆单 | 中 | 展示分组结果，B确认后下推 |
| ERP制单 | 大 | 按发货通知分拆为多个ERP单（方案A）或一个ERP单多行（方案B） |
