---
name: g03-reviewer-dry
description: 空转联调用的假 reviewer 定义（仅 P3 dry run / 测试）
tools: read, grep, find, ls
model: fake/reviewer
---

空转联调 reviewer：不调用模型，由 fake_reviewer.py 直接输出 PASS。
