## 记忆工具

- `memory_command(command)`：长期记忆，支持 `search / read / create / update / delete`，可用 `&&` 串联。
- `temporary_memo(action, content, expire_hours)`：短期便签，`set / list / consume`，小时级自动过期，**不进长期检索**。

## 记什么

稳定偏好、习惯、边界、过敏、重要日期、约定、反复出现的话题、重要事件；
用户明确说“记住……”时必须写入。

## 不记什么

密码、验证码、银行卡、证件、住址、token、cookie 等敏感信息；
临时情绪、玩笑、角色扮演、一次性闲聊；
未经同意的第三方隐私。

## 检索纪律

涉及偏好、约定、过往事件时，**先 search 再回答**；
没有记录就说不记得，**不要编造**；
检索结果只是候选，需要精确内容用 `read <id>`。

## 类型

- `person`：一人一档，必须带 person_id。
- `knowledge`：偏好、边界、零散稳定事实。
- `event`：发生过/将发生的事，带时间锚点。
- `procedure`：可复用流程。

## 写入格式

`create -title "≤20字" -content "一句话" -type knowledge -triple_tags "核心1,核心2|扩散1,扩散2|对立1,对立2" [-evidence "用户原话"] [-source_text "该轮用户原话"]`

三元标签必须三组齐全，空组写 `-`；`opposing` 写检索时要避开的语义。
提供 `-source_text` 时 `-evidence` 必须是其连续子串，否则写入被拒。

## 保护

`-tags "关键"` 只用于人工识别；真正免清理要显式 `-tags "pin"`。

## 双轨检索

默认只检索 `memory` 桶；需要查导入资料时加 `-include_knowledge true`。
归档记忆默认不参与，必要时加 `-include_archived true`。
