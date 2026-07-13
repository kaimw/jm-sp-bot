# 商务 AI Agent 订单中台 — 主流程总图（最终版）

> **基于 2026-06-09 会议 + 2026-06-27 商务部调研 + CFO讲话**
> 完整覆盖一期核心链路

```mermaid
flowchart TD
    classDef crm fill:#e1f5fe,stroke:#0288d1,color:#01579b
    classDef platform fill:#e8f5e9,stroke:#388e3c,color:#1b5e20
    classDef rule fill:#fff3e0,stroke:#f57c00,color:#e65100
    classDef exception fill:#fce4ec,stroke:#d32f2f,color:#b71c1c
    classDef oms fill:#f3e5f5,stroke:#7b1fa2,color:#4a148c
    classDef kingdee fill:#e0f2f1,stroke:#00796b,color:#004d40
    classDef notify fill:#fff8e1,stroke:#f9a825,color:#f57f17
    classDef done fill:#e8eaf6,stroke:#3949ab,color:#1a237e
    classDef new fill:#fbe9e7,stroke:#d84315,color:#bf360c

    subgraph P1["① 感知层：CRM 订单同步"]
        A["🔁 定时轮询<br/>纷享销客 CDP"]:::crm --> B["获取订单列表+附件+快照"]:::crm
        B --> C["幂等入库 CrmSalesOrder<br/>payload_hash 去重"]:::crm
        C --> D{"一期范围?"}:::rule
        D -->|InScope| E["入队处理"]:::notify
        D -->|OutOfScope| F["跳过中台"]:::exception
    end

    subgraph P2["② 订单类型识别与前置检查"]
        E --> G["订单类型自动识别"]:::rule
        G --> H{"金额>0 且有附件?"}:::rule
        H -->|是→销售订单| I["前置条件检查<br/>合同审批+邮箱+范围"]:::rule
        H -->|否→备货订单| J["跳过合同/金额检查<br/>价格取自产品价格表"]:::rule
        I --> K{"通过?"}:::rule
        K -->|阻断| L["异常→通知销售补正"]:::exception
    end

    subgraph P3["③ 中台建单"]
        K -->|通过| M["订单变更检测<br/>payload_hash 对比"]:::platform
        M -->|新单/无变更| N["附件字段提取<br/>收货人/地址/日期"]:::platform
        M -->|有变更| O["变更处理链<br/>已闭环→新建<br/>未闭环→重审<br/>已部分发→增量"]:::exception
        O --> N
        N --> P["中台建单 (暂不分配号)<br/>状态: IMPORTED"]:::platform
        P --> Q["同步明细(OrderItems)"]:::platform
    end

    subgraph Rules["▼ 11条预审规则链 ▼"]
        R1["① 必填字段<br/>销售:全字段 备货:简化"]:::rule
        R2["② 一期完整性"]:::rule
        R3["③ 客户映射"]:::rule
        R4["④ 金额正数(销售)"]:::rule
        R5["⑤ 金额一致性(销售)"]:::rule
        R6["⑥ 明细存在性"]:::rule
        R7["⑦ SKU别名匹配<br/>规则+LLM语义"]:::rule
        R8["⑧ BOM匹配(销售)"]:::rule
        R9["⑨ 合同金额一致(销售)"]:::rule
        R10["⑩ 附件一致性(销售)"]:::rule
        R11["⑪ 库存三步判断(阻断式)"]:::rule
        Q --> R1 -.- R2 -.- R3 -.- R4 -.- R5 -.- R6 -.- R7 -.- R8 -.- R9 -.- R10 -.- R11
    end

    R11 --> S{"责任链<br/>结果?"}:::rule

    S -->|阻断| T["❌ VALIDATION_BLOCKED<br/>ExceptionCase<br/>AI诊断+邮件通知"]:::exception
    T -.->|Step2:通知B调货| U1["B确认后重审"]:::exception
    T -.->|Step3:通知销售| U2["销售在CRM修改后重审"]:::exception

    subgraph Stock["库存三步判断（Q5决策）"]
        ST1["Step 1: 查主体对应仓库<br/>根据PI/CRM识别销售主体<br/>→查关联仓库库存"]:::rule
        ST1 --> ST2{"够?"}:::rule
        ST2 -->|够→继续规则链| S
        ST2 -->|不够| ST3["Step 2: 查其他主体仓库"]:::rule
        ST3 --> ST4{"其他有货?"}:::rule
        ST4 -->|有→非阻断| ST5["📧 生成调货通知邮件<br/>发送全部干系人<br/>然后正常通过"]:::notify
        ST4 -->|全缺→阻断| ST6["❌ 通知销售重新提交<br/>告知缺货物料+参考库存"]:::exception
        ST5 --> S
    end

    subgraph P4["④ 预审通过 → ERP制单 → 发货通知"]
        S -->|通过| N1["✅ 分配中台订单号<br/>MP-{年份}{序号}"]:::platform
        N1 --> N2["ERP_PENDING<br/>→ ERP_SAVING<br/>Save → Submit → Audit"]:::kingdee
        N2 --> N3{"制单结果"}:::rule
        N3 -->|成功→ERP_SAVED| N4["✅ 发货通知邮件发出<br/>国内→带单号<br/>海外→不带单号<br/><b>一期闭环完结</b>"]:::done
        N3 -->|失败→ERP_FAILED| N5["😡 ExceptionCase<br/>详细失败原因<br/>错误码+字段名+建议"]:::exception
        N5 -->|修复后重试| N2
    end

    N2 -..->|"⚠️ Q6：制单中CRM变更<br/>完成制单→Cancel→退回"| N2
    
    subgraph P5["⑤ 【二期】OMS 下推与追踪（一期范围外）"]
        N4 -.->|二期| V["⏳ OMS 发货单API推送<br/>吉客云 wms.order.create<br/>指数退避重试"]:::new
        V -..-> W{"⏳ 【二期】结果?"}:::rule
        W -..->|成功| X["⏳ OMS_ACCEPTED"]:::new
        W -..->|重试耗尽| Y["⏳ OMS_BLOCKED<br/>死信+AI诊断"]:::new
        X -..-> Z["⏳ 轮询状态<br/>→PICKING→SHIPPED→归档"]:::new
    end

    subgraph NEW["调研新增需求状态"]
        N1["🗸 物料别名匹配 🔶"]:::new
        N2["🗸 一单多收货人(Excel) 🔶<br/>👉🏻 **不拆单**，发货通知内每行标注收货人"]:::new
        N3["🗸 订单变更跟踪 🔶"]:::new
        N4["⏳ 特殊需求分类 🔷"]:::new
        N5["⏳ 分批发货 🔷"]:::new
        N6["🗸 库存三步判断 🔶"]:::new
        N7["🗸 预审通过后分配订单号 🔶"]:::new
        N8["🗸 备货订单(纳入一期) 🔶"]:::new
        N9["🗸 商务审核前置 🔶"]:::new
        N10["🗸 订单类型自动识别 🔶"]:::new
        N11["🗸 ERP制单状态机+失败原因 🔶"]:::new
    end

    N7 -..-> N2
    N10 -..-> G
    N1 -..-> R7
    N9 -..-> I
    N6 -..-> R11
    N11 -..-> N3
```

## 图例

| 颜色 | 含义 |
|------|------|
| 💙 | CRM/外部系统 |
| 💚 | 中台处理 |
| 🧡 | 规则引擎/决策 |
| ❤️ | 异常/阻断 |
| 💜 | OMS/履约 |
| 🤍 | 金蝶ERP |
| 💛 | 通知/邮件 |
| 💙(深) | 完结 |
| 🔶 | 已明确纳入一期的调研新增 |
| 🔷 | 一期建议/二期优先 |
