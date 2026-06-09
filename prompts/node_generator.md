# Node Generator Prompt

请把用户需求转换为 AI RPA workflow 节点。

节点格式：

```json
{
  "id": "唯一ID",
  "type": "web.open | web.click | web.input | web.wait_for | web.select | web.extract | ai.ask | flow.wait",
  "title": "人类可读标题",
  "params": {}
}
```

要求：

1. 节点必须逐步展开，不允许用“处理所有商品”这类宏节点。
2. 每个点击节点要尽量包含 `selector`、`target` 和失败兜底描述。
3. 页面跳转后必须有 `web.wait_for`。
4. 每次进入新页面后必须考虑弹窗清理节点。
5. 对验证码节点只生成等待人工处理，不生成点击或输入。
6. 每个 workflow 至少生成 3 个测试用例：成功路径、元素缺失、弹窗干扰。

