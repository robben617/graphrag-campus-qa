"""校园知识问答系统 —— 第一阶段（P0）可运行最小链路。

包结构：
- app.config   配置加载（.env / 环境变量）
- app.errors   统一错误类型与提示
- app.db       SQLite 存储层（图谱节点/关系/条款/文档）
- app.ingest   解析 + 幂等导入 + 统计检查
- app.retrieval 关键词/精确检索
- app.service  QA 编排（提问→检索→回答/拒答）
- app.web      FastAPI REST API（Depends 依赖注入 + 统一异常 + 结构化日志）
- app.cli      CLI：导入 / 统计 / 问答
"""

__version__ = "0.1.0"
