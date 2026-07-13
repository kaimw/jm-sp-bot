#!/usr/bin/env python3
"""生成中台主流程 SVG 流程图"""
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

# ── 工具函数 ──
def rect(x, y, w, h, r=6, fill="#ffffff", stroke="#333333", label="", cls=""):
    el = ET.Element("g", attrib={"class": cls})
    r_el = ET.SubElement(el, "rect", attrib={
        "x": str(x), "y": str(y), "width": str(w), "height": str(h),
        "rx": str(r), "fill": fill, "stroke": stroke, "stroke-width": "1.5"
    })
    lines = label.split("\n")
    for i, line in enumerate(lines):
        t = ET.SubElement(el, "text", attrib={
            "x": str(x + w/2), "y": str(y + 22 + i * 18),
            "text-anchor": "middle", "font-size": "12", "fill": "#333",
            "font-family": "Arial, sans-serif"
        })
        t.text = line
    return el

def arrow(x1, y1, x2, y2, label="", dashed=False):
    g = ET.Element("g")
    dash = "5,4" if dashed else "none"
    ET.SubElement(g, "line", attrib={
        "x1": str(x1), "y1": str(y1), "x2": str(x2), "y2": str(y2),
        "stroke": "#666", "stroke-width": "1.5", "stroke-dasharray": dash
    })
    # 箭头
    ax, ay = x2, y2
    if x2 > x1: ax -= 8
    elif x2 < x1: ax += 8
    if y2 > y1: ay -= 8
    elif y2 < y1: ay += 8
    ET.SubElement(g, "polygon", attrib={
        "points": f"{x2},{y2} {ax-4},{ay-4} {ax+4},{ay+4}",
        "fill": "#666", "stroke": "#666", "stroke-width": "1"
    })
    if label:
        t = ET.SubElement(g, "text", attrib={
            "x": str((x1 + x2) / 2), "y": str((y1 + y2) / 2 - 6),
            "text-anchor": "middle", "font-size": "10", "fill": "#666",
            "font-family": "Arial, sans-serif", "font-style": "italic"
        })
        t.text = label
    return g

def diamond(x, y, w, h, label="", fill="#fff3e0", stroke="#f57c00"):
    g = ET.Element("g")
    points = f"{x+w/2},{y} {x+w},{y+h/2} {x+w/2},{y+h} {x},{y+h/2}"
    ET.SubElement(g, "polygon", attrib={
        "points": points, "fill": fill, "stroke": stroke, "stroke-width": "1.5"
    })
    for i, line in enumerate(label.split("\n")):
        t = ET.SubElement(g, "text", attrib={
            "x": str(x + w/2), "y": str(y + h/2 - 5 + i * 14),
            "text-anchor": "middle", "font-size": "11", "fill": "#333",
            "font-family": "Arial, sans-serif"
        })
        t.text = line
    return g

def title_bar(y, label, color="#1976d2"):
    g = ET.Element("g")
    ET.SubElement(g, "rect", attrib={
        "x": "30", "y": str(y), "width": "940", "height": "30",
        "fill": color, "rx": "4"
    })
    t = ET.SubElement(g, "text", attrib={
        "x": "500", "y": str(y + 20), "text-anchor": "middle",
        "font-size": "14", "fill": "white", "font-weight": "bold",
        "font-family": "Arial, sans-serif"
    })
    t.text = label
    return g

# ── 建 SVG ──
svg = ET.Element("svg", attrib={
    "xmlns": "http://www.w3.org/2000/svg",
    "width": "1000", "height": "2600",
    "viewBox": "0 0 1000 2600"
})
ET.SubElement(svg, "rect", attrib={
    "width": "1000", "height": "2200", "fill": "#fafafa"
})

y_off = 0

# ===== Phase 1: CRM =====
y_off += 10
svg.append(title_bar(y_off, "① 感知层：CRM 订单同步 (crm_sync.py)", "#0288d1"))

y_off += 40
svg.append(rect(150, y_off, 280, 50, fill="#e1f5fe", stroke="#0288d1",
    label="定时轮询/手动触发\n纷享销客 CDP 接口"))
svg.append(rect(550, y_off, 280, 50, fill="#e1f5fe", stroke="#0288d1",
    label="获取销售订单列表\n详情 + 附件 + 快照"))
svg.append(arrow(430, y_off + 25, 550, y_off + 25))

y_off += 70
svg.append(rect(150, y_off, 280, 50, fill="#e1f5fe", stroke="#0288d1",
    label="CRM 订单入库 CrmSalesOrder\n幂等去重 (payload_hash)"))
svg.append(arrow(430, y_off - 45, 430, y_off + 25))

y_off += 70
svg.append(diamond(150, y_off, 280, 50, fill="#fff3e0", stroke="#f57c00",
    label="一期范围检查?"))
svg.append(arrow(430, y_off - 45, 430, y_off - 10))
# InScope 右分支
svg.append(arrow(430, y_off + 25, 430, y_off + 60, label="InScope"))

y_off += 80
svg.append(rect(150, y_off, 280, 40, fill="#fff8e1", stroke="#f9a825",
    label="入队处理: enqueue_crm_order_parsed_event"))

y_off += 60
svg.append(arrow(290, y_off - 40, 290, y_off))

# ===== Phase 2: 认知层 =====
y_off += 10
svg.append(title_bar(y_off, "② 认知层：中台建单与预审 (order_middle_platform.py)", "#388e3c"))

y_off += 40
svg.append(rect(150, y_off, 280, 50, fill="#e8f5e9", stroke="#388e3c",
    label="process_crm_order_parsed_event\n变更检测 + 幂等判断"))
svg.append(arrow(290, y_off - 20, 290, y_off))

y_off += 70
svg.append(diamond(150, y_off, 280, 50, fill="#fff3e0", stroke="#f57c00",
    label="订单变更检测?\npayload_hash 对比"))
svg.append(arrow(290, y_off - 20, 290, y_off))

# 变更分支
y_off += 70
svg.append(rect(550, y_off - 70, 300, 60, fill="#fce4ec", stroke="#d32f2f",
    label="变更处理链条\nhandle_crm_snapshot_changed\n→ 标记 CHANGED → 新建快照"))
tg = ET.Element("g")
ET.SubElement(tg, "line", attrib={"x1": "430", "y1": str(y_off - 45), "x2": "550", "y2": str(y_off - 45),
    "stroke": "#d32f2f", "stroke-width": "1.5"})
lb = ET.SubElement(tg, "text", attrib={"x": "490", "y": str(y_off - 52), "text-anchor": "middle",
    "font-size": "10", "fill": "#d32f2f", "font-family": "Arial, sans-serif"})
lb.text = "有变更"
svg.append(tg)
# 回流
ET.SubElement(tg, "line", attrib={"x1": "700", "y1": str(y_off - 10), "x2": "700", "y2": str(y_off + 10),
    "stroke": "#d32f2f", "stroke-width": "1.5", "stroke-dasharray": "5,4"})

# 无变更继续
svg.append(rect(150, y_off, 280, 50, fill="#e8f5e9", stroke="#388e3c",
    label="附件字段提取\n收货人/地址/日期"))
svg.append(arrow(290, y_off - 20, 290, y_off))

y_off += 70
svg.append(rect(150, y_off, 280, 50, fill="#e8f5e9", stroke="#388e3c",
    label="upsert_middle_platform_order\n生成 MP-中台单号\n同步明细 (OrderItems)"))
svg.append(arrow(290, y_off - 20, 290, y_off))

y_off += 70
svg.append(rect(150, y_off, 280, 40, fill="#e8f5e9", stroke="#388e3c",
    label="状态: CRM_APPROVED → IMPORTED"))
svg.append(arrow(290, y_off - 20, 290, y_off))

y_off += 60
# 规则链
svg.append(title_bar(y_off, "▼ 11条预审规则链 (责任链模式 + 熔断) ▼", "#f57c00"))

y_off += 40
rules_x = 80
rule_labels = [
    "① RequiredHeadFieldsRule\n必填字段校验",
    "② PhaseOneCompletenessRule\n一期完整性",
    "③ CustomerMappingRule\n客户主数据映射",
    "④ PositiveAmountRule\n金额正数",
    "⑤ AmountConsistencyRule\n金额一致性",
    "⑥ HasOrderItemsRule\n明细存在性",
    "⑦ KnownSkuRule\n已知SKU匹配",
    "⑧ SkuBomMatchRule\nBOM匹配",
    "⑨ ContractAmountConsistencyRule\n合同金额一致",
    "⑩ AttachmentProductConsistencyRule\n附件一致性",
    "⑪ LocalInventoryAvailableRule\n本地库存可用",
]
for i, lab in enumerate(rule_labels):
    rx = rules_x + i * 80
    svg.append(rect(rx, y_off, 75, 45, fill="#fff3e0", stroke="#f57c00", label=lab))
    if i > 0:
        ET.SubElement(svg, "line", attrib={
            "x1": str(rx - 2), "y1": str(y_off + 22),
            "x2": str(rx + 3), "y2": str(y_off + 22),
            "stroke": "#f57c00", "stroke-width": "1", "stroke-dasharray": "3,3"
        })

# 结果菱形
y_off += 65
svg.append(diamond(330, y_off, 240, 50, fill="#fff3e0", stroke="#f57c00",
    label="责任链结果?"))
dy = y_off

y_off += 80
svg.append(rect(560, y_off - 50, 280, 45, fill="#fce4ec", stroke="#d32f2f",
    label="❌ VALIDATION_BLOCKED\n→ 创建 ExceptionCase"))
ET.SubElement(svg, "line", attrib={
    "x1": "570", "y1": str(dy + 25), "x2": "560", "y2": str(dy + 30),
    "stroke": "#d32f2f", "stroke-width": "1.5"
})
lb2 = ET.SubElement(svg, "text", attrib={
    "x": "565", "y": str(dy + 20), "text-anchor": "end",
    "font-size": "10", "fill": "#d32f2f", "font-family": "Arial, sans-serif"
})
lb2.text = "阻断/CRITICAL"

y_off += 60
svg.append(rect(560, y_off, 280, 40, fill="#fce4ec", stroke="#d32f2f",
    label="AI 诊断入队 + 邮件通知"))
ET.SubElement(svg, "line", attrib={
    "x1": "700", "y1": str(y_off - 5), "x2": "700", "y2": str(y_off + 5),
    "stroke": "#d32f2f", "stroke-width": "1"
})

y_off += 55
svg.append(rect(560, y_off, 280, 40, fill="#fce4ec", stroke="#d32f2f",
    label="商务/销售补正 → 重新预审"))
ET.SubElement(svg, "line", attrib={
    "x1": "560", "y1": str(y_off + 20), "x2": "450", "y2": str(dy - 28),
    "stroke": "#d32f2f", "stroke-width": "1", "stroke-dasharray": "5,4"
})

# 通过分支
y_off = dy
svg.append(rect(100, y_off + 60, 240, 40, fill="#e8eaf6", stroke="#3949ab",
    label="✅ VALIDATED / RULES_PASSED"))
tl = ET.SubElement(svg, "text", attrib={
    "x": "320", "y": str(y_off + 20), "text-anchor": "start",
    "font-size": "10", "fill": "#388e3c", "font-family": "Arial, sans-serif"
})
tl.text = "全部通过"
ET.SubElement(svg, "line", attrib={
    "x1": "330", "y1": str(y_off + 50), "x2": "220", "y2": str(y_off + 55),
    "stroke": "#388e3c", "stroke-width": "1.5"
})

# ===== Phase 2.5: 金蝶制单（新增） =====
y_kingdee = y_off + 120
svg.append(title_bar(y_kingdee, "②⑤ 金蝶 ERP 制单与提交（N-007 新增）", "#00796b"))

y_kingdee += 40
svg.append(rect(150, y_kingdee, 280, 50, fill="#e0f2f1", stroke="#00796b",
    label="ERP 制单 (Save)\n构建销售订单 JSON\n含客户/物料/部门/金额"))
ET.SubElement(svg, "line", attrib={
    "x1": "220", "y1": str(y_off + 100), "x2": "220", "y2": str(y_kingdee + 5),
    "stroke": "#388e3c", "stroke-width": "1.5"
})

y_kingdee += 70
svg.append(rect(150, y_kingdee, 280, 40, fill="#e0f2f1", stroke="#00796b",
    label="ERP 提交 (Submit)"))
ET.SubElement(svg, "line", attrib={
    "x1": "290", "y1": str(y_kingdee - 20), "x2": "290", "y2": str(y_kingdee),
    "stroke": "#00796b", "stroke-width": "1.5"
})

y_kingdee += 60
svg.append(rect(150, y_kingdee, 280, 40, fill="#e0f2f1", stroke="#00796b",
    label="ERP 审核 (Audit)"))
ET.SubElement(svg, "line", attrib={
    "x1": "290", "y1": str(y_kingdee - 20), "x2": "290", "y2": str(y_kingdee),
    "stroke": "#00796b", "stroke-width": "1.5"
})

y_kingdee += 60
svg.append(diamond(150, y_kingdee, 280, 50, fill="#fff3e0", stroke="#f57c00",
    label="ERP 制单结果?"))
ET.SubElement(svg, "line", attrib={
    "x1": "290", "y1": str(y_kingdee - 15), "x2": "290", "y2": str(y_kingdee),
    "stroke": "#00796b", "stroke-width": "1.5"
})

# 失败
svg.append(rect(560, y_kingdee, 280, 40, fill="#fce4ec", stroke="#d32f2f",
    label="❌ 制单异常 → ExceptionCase"))
ET.SubElement(svg, "line", attrib={
    "x1": "430", "y1": str(y_kingdee + 25), "x2": "560", "y2": str(y_kingdee + 25),
    "stroke": "#d32f2f", "stroke-width": "1.5"
})
t = ET.SubElement(svg, "text", attrib={"x": "490", "y": str(y_kingdee + 20),
    "text-anchor": "middle", "font-size": "10", "fill": "#d32f2f",
    "font-family": "Arial, sans-serif"})
t.text = "制单失败"

# 成功 → 继续到执行层
y_kingdee += 65
# 通过标记
tl2 = ET.SubElement(svg, "text", attrib={
    "x": "90", "y": str(y_kingdee - 10), "text-anchor": "start",
    "font-size": "10", "fill": "#00796b", "font-family": "Arial, sans-serif",
    "font-weight": "bold"
})
tl2.text = "制单成功 → 继续发货"

# 连线：金蝶制单成功 → 执行层发货通知
ET.SubElement(svg, "line", attrib={
    "x1": "290", "y1": str(y_kingdee - 15), "x2": "290", "y2": str(y_kingdee + 65),
    "stroke": "#00796b", "stroke-width": "1.5"
})

# ===== Phase 3: 执行层 =====
y_off2 = y_kingdee + 10
svg.append(title_bar(y_off2, "③ 执行层：发货通知与 OMS 下推", "#7b1fa2"))

y_off2 += 40
svg.append(rect(150, y_off2, 280, 45, fill="#f3e5f5", stroke="#7b1fa2",
    label="create_delivery_notice\n生成发货通知单"))
ET.SubElement(svg, "line", attrib={
    "x1": "220", "y1": str(y_off + 100), "x2": "220", "y2": str(y_off2 + 5),
    "stroke": "#388e3c", "stroke-width": "1.5"
})

y_off2 += 65
svg.append(diamond(150, y_off2, 280, 50, fill="#fff3e0", stroke="#f57c00",
    label="平台履约订单?"))
ET.SubElement(svg, "line", attrib={
    "x1": "290", "y1": str(y_off2 - 15), "x2": "290", "y2": str(y_off2),
    "stroke": "#7b1fa2", "stroke-width": "1.5"
})

# 是
svg.append(rect(550, y_off2, 280, 40, fill="#e8eaf6", stroke="#3949ab",
    label="直接归档"))
ET.SubElement(svg, "line", attrib={
    "x1": "430", "y1": str(y_off2 + 25), "x2": "550", "y2": str(y_off2 + 25),
    "stroke": "#7b1fa2", "stroke-width": "1.5"
})
t = ET.SubElement(svg, "text", attrib={"x": "490", "y": str(y_off2 + 20),
    "text-anchor": "middle", "font-size": "10", "fill": "#7b1fa2",
    "font-family": "Arial, sans-serif"})
t.text = "是"

# 否
y_off2 += 70
svg.append(rect(150, y_off2, 280, 40, fill="#f3e5f5", stroke="#7b1fa2",
    label="状态: DELIVERY_NOTICE_READY"))
ET.SubElement(svg, "line", attrib={
    "x1": "290", "y1": str(y_off2 - 20), "x2": "290", "y2": str(y_off2),
    "stroke": "#7b1fa2", "stroke-width": "1.5"
})

y_off2 += 60
svg.append(diamond(150, y_off2, 280, 50, fill="#fff3e0", stroke="#f57c00",
    label="自动确认?"))
ET.SubElement(svg, "line", attrib={
    "x1": "290", "y1": str(y_off2 - 20), "x2": "290", "y2": str(y_off2),
    "stroke": "#7b1fa2", "stroke-width": "1.5"
})

y_off2 += 70
svg.append(rect(150, y_off2, 280, 40, fill="#f3e5f5", stroke="#7b1fa2",
    label="confirm_delivery_notice\n→ OMS_PENDING"))
ET.SubElement(svg, "line", attrib={
    "x1": "290", "y1": str(y_off2 - 20), "x2": "290", "y2": str(y_off2),
    "stroke": "#7b1fa2", "stroke-width": "1.5"
})

y_off2 += 60
svg.append(rect(150, y_off2, 280, 40, fill="#f3e5f5", stroke="#7b1fa2",
    label="enqueue_oms_push → 入队"))
ET.SubElement(svg, "line", attrib={
    "x1": "290", "y1": str(y_off2 - 20), "x2": "290", "y2": str(y_off2),
    "stroke": "#7b1fa2", "stroke-width": "1.5"
})

y_off2 += 60
svg.append(rect(150, y_off2, 280, 45, fill="#f3e5f5", stroke="#7b1fa2",
    label="调用吉客云 API\nwms.order.create\n指数退避 60s/180s/540s"))
ET.SubElement(svg, "line", attrib={
    "x1": "290", "y1": str(y_off2 - 20), "x2": "290", "y2": str(y_off2),
    "stroke": "#7b1fa2", "stroke-width": "1.5"
})

y_off2 += 65
svg.append(diamond(150, y_off2, 280, 50, fill="#fff3e0", stroke="#f57c00",
    label="下推结果?"))
ET.SubElement(svg, "line", attrib={
    "x1": "290", "y1": str(y_off2 - 15), "x2": "290", "y2": str(y_off2),
    "stroke": "#7b1fa2", "stroke-width": "1.5"
})

# 成功分支
svg.append(rect(550, y_off2, 280, 40, fill="#e8eaf6", stroke="#3949ab",
    label="✅ OMS_ACCEPTED"))
ET.SubElement(svg, "line", attrib={
    "x1": "430", "y1": str(y_off2 + 25), "x2": "550", "y2": str(y_off2 + 25),
    "stroke": "#7b1fa2", "stroke-width": "1.5"
})
t = ET.SubElement(svg, "text", attrib={"x": "490", "y": str(y_off2 + 20),
    "text-anchor": "middle", "font-size": "10", "fill": "#388e3c",
    "font-family": "Arial, sans-serif"})
t.text = "成功"

# 失败分支
svg.append(rect(100, y_off2, 130, 45, fill="#fce4ec", stroke="#d32f2f",
    label="❌ OMS_BLOCKED\n死信 + AI诊断"))
ET.SubElement(svg, "line", attrib={
    "x1": "150", "y1": str(y_off2 + 25), "x2": "100", "y2": str(y_off2 + 25),
    "stroke": "#d32f2f", "stroke-width": "1.5"
})
t = ET.SubElement(svg, "text", attrib={"x": "130", "y": str(y_off2 + 20),
    "text-anchor": "end", "font-size": "10", "fill": "#d32f2f",
    "font-family": "Arial, sans-serif"})
t.text = "重试耗尽"

# ===== Phase 4: 履约追踪 =====
y_off3 = y_off2 + 70
svg.append(title_bar(y_off3, "④ 履约追踪", "#3949ab"))

y_off3 += 40
svg.append(rect(150, y_off3, 280, 40, fill="#e8eaf6", stroke="#3949ab",
    label="poll_oms_status_updates\n轮询 OMS 执行状态"))
ET.SubElement(svg, "line", attrib={
    "x1": "690", "y1": str(y_off2 + 20), "x2": "690", "y2": str(y_off3),
    "stroke": "#3949ab", "stroke-width": "1.5"
})

y_off3 += 60
svg.append(rect(150, y_off3, 280, 40, fill="#e8eaf6", stroke="#3949ab",
    label="PICKING (拣货)"))
y_off3 += 60
svg.append(rect(150, y_off3, 280, 40, fill="#e8eaf6", stroke="#3949ab",
    label="SHIPPED (发货)"))
y_off3 += 60
svg.append(diamond(150, y_off3, 280, 50, fill="#fff3e0", stroke="#f57c00",
    label="平台回传?"))
y_off3 += 70
svg.append(rect(550, y_off3 - 70, 280, 40, fill="#e8eaf6", stroke="#3949ab",
    label="回传平台履约"))
svg.append(rect(150, y_off3, 280, 40, fill="#e8eaf6", stroke="#3949ab",
    label="FULFILLMENT_ARCHIVED"))

# ===== Phase 5: 归档 =====
y_off4 = y_off3 + 65
svg.append(title_bar(y_off4, "⑤ 归档与下一期", "#9e9e9e"))

y_off4 += 40
svg.append(rect(150, y_off4, 280, 40, fill="#fbe9e7", stroke="#d84315",
    label="下一期: 物流轨迹/签收"))
y_off4 += 60
svg.append(rect(150, y_off4, 280, 40, fill="#e0f2f1", stroke="#00796b",
    label="下一期: 财务核验 (金蝶ERP)"))
y_off4 += 60
svg.append(rect(150, y_off4, 280, 40, fill="#e8eaf6", stroke="#3949ab",
    label="下一期: 订单 CLOSED"))

# ===== 脚注 =====
y_off4 += 90
note = ET.SubElement(svg, "text", attrib={
    "x": "500", "y": str(y_off4), "text-anchor": "middle",
    "font-size": "11", "fill": "#888",
    "font-family": "Arial, sans-serif"
})
note.text = "🟦 CRM/外部系统  🟩 中台处理  🟧 规则引擎  🟥 异常/阻断  🟪 OMS/履约  🟩🟦 归档  🟫 金蝶ERP  🟧 新增/待完善"
y_off4 += 20
note2 = ET.SubElement(svg, "text", attrib={
    "x": "500", "y": str(y_off4), "text-anchor": "middle",
    "font-size": "11", "fill": "#888",
    "font-family": "Arial, sans-serif"
})
note2.text = "✅ 金蝶自动制单 (N-007) 已测试通过：Save → Submit → Audit → (UnAudit → Cancel → Delete)"

# ── 输出 ──
out_path = "/sessions/amazing-nifty-gauss/mnt/jm-sp-bot/docs/workflow-flowchart.svg"
tree = ET.ElementTree(svg)
tree.write(out_path, encoding="utf-8", xml_declaration=True)
print(f"OK: {out_path}")
