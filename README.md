# Life Projects

用明确的目标状态、推进步骤、安排与实际反馈管理个人项目。Local project facts, goal progress diagrams and a conversational CLI.

这是仍在探索的个人管理原型，公开通用代码、测试和虚构初始化示例；没有公开实际求职、生活记录、外部项目注册或聊天。开发源继续在原项目维护，公开导出不形成另一套独立代码。目标验收与任务完成分开，未知日期和关系保持未知。

## 本地 CLI

需要 Python 3.11 或更新、macOS/Linux 的文件锁支持。CLI 本身使用标准库。

```sh
python3 -m app.pilot init --data demo-output/project.json
python3 -m app.pilot check --data demo-output/project.json
python3 -m app.pilot show --data demo-output/project.json
python3 -m unittest discover -s tests -v
```

初始化的是公开的虚构求职路线模板，不是本人现状，也不代表任何人已批准。变更使用带 expected_revision 的明确 JSON 请求；见 python3 -m app.pilot apply --help。对话助手可以准备请求，但脚本不会读取聊天或推断批准。

## 全景图

渲染需要 Java 与 PlantUML。把 PlantUML 1.2026.2 jar 放入 .architecture/cache/plantuml.jar，SHA256 必须为 7a3eacbccd08311f14b107b1254e179adc1f81fa8bd52bbaf563a37f00ea026f；Java 命令需在 PATH。rendering.json 使用可移植的 java 入口，未附带个人运行时或二进制。

```sh
python3 -m app.pilot render --data demo-output/project.json --output demo-output/views
```

阅读生成的 SVG 与当日表。复杂读取面板代码也保留在 app/；本导出首先验证 CLI 和隔离测试。实际工作台需要调用者提供有效的 workspace/context，不能复制私人账本来启动公开例子。工具不自动建立人生优先级，不连接外部日历、不调用付费 API。个人使用效果尚未得到通用证明。

## 数据和来源

原 Git 历史、private、真实 data、views、项目登记和本机 LaunchAgent 设置均排除。保存冲突、备份和依赖检查不能代替结果是否符合本人用途的验收。PUBLIC_EXPORT.json 对应逐文件来源和公开适配。尚未指定全项目复用许可，公开上传不自动授予开源复用许可。
