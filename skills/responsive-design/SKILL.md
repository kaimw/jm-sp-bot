---
name: responsive-design
description: Ensure all page designs, implementations, and element modifications meet responsive design requirements and are optimized for mobile screen viewing.
---

# 响应式设计与移动端适配规范

## 核心指令
- **响应式设计**：所有页面的设计和实现都必须满足响应式布局要求，确保在不同分辨率（桌面端、平板、移动端）下正常显示。
- **移动端优先兼顾**：任何 UI 元素的改动、新增或修补，都必须兼顾移动端屏幕的正常查看、触控与交互体验。
- **适配技术要求**：
  - 优先采用 Flexbox 或 CSS Grid 等现代弹性布局方案。
  - 使用媒体查询（Media Queries）进行断点微调，防止内容溢出或重叠。
  - 确保交互元素（按钮、链接等）在移动端有足够大的点击区域（如 44x44px 以上）。
