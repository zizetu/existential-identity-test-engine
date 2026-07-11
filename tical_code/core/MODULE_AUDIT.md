# 模块深度审计 — 每个模块为什么存在

不是看"有没有被 import"，而是看"为什么写出来的"。

## 已确认被替代 → _legacy/（8个，8585行）

| 模块 | 行数 | 原因 | 被谁替代 |
|------|------|------|----------|
| worker_loop.py | 1887 | v0.4 旧worker循环 | unified_worker.py |
| model_router.py | 1553 | v0.3 模型路由 | model_failover.py |
| session.py | 951 | v0.3 内存版会话 | modules/session_manager.py (SQLite) |
| trace.py | 1279 | v0.3 调用追踪 | trace/ 包 |
| verify_pipeline.py | 748 | v0.3 验证管线 | eite/verify_engine_v2.py |
| compaction.py | 966 | v0.5 上下文压缩 | modules/context_compactor.py |
| llm_interface.py | 349 | v0.3 LLM调用 | llm_backend.py |
| worker.py | 852 | v0.2 旧worker | unified_worker.py |

这些是真死代码，没有任何活跃模块引用，已移到 _legacy/。

## 设计了但没接通的功能（33个）

### 第一梯队：有真实逻辑，是系统核心能力的未完成版

| 模块 | 行数 | 设计目的 | 为什么没接 | 接通优先级 |
|------|------|----------|------------|------------|
| **worker_framework.py** | 3074 | 配置驱动的Worker生命周期框架，WorkerConfig+BootstrapAnchor | 被unified_worker替代了入口，但WorkerConfig/BootstrapAnchor仍是配置体系核心 | ⚠️ 需评估：WorkerConfig和anchor.json仍在用 |
| **decision_engine.py** | 2085 | 多条件决策引擎，DesignParser+ConditionDetector，让AI做结构化决策而非随机选 | 写完发现unified_worker的tool-calling loop够用，没接入 | 中 — 复杂任务链需要 |
| **self_repair.py** | 2471 | **主人要求的自主修复**。AI发现代码问题→自动修→验证→提交。SandboxMode隔离执行 | 写完但依赖sandbox.py和agent_runtime，后两者也没接通 | 高 — 主人明确要求过 |
| **constitution.py** | 1308 | **行为宪法**。AI不能做什么的规则引擎，和axioms.py联动 | v0.5审计发现reflection泄露风险(4.3分)，修了安全问题但没接入主循环 | 高 — 安全刚需 |
| **checkpoint.py** | 1413 | **Shadow Checkpoint**。文件/对话快照+选择性回滚，FileWatcher实时监控 | v0.5审计发现pickle反序列化RCE风险(5.3分)，修成JSON+SHA-256但没接入 | 高 — 自修复的前提 |
| **hive.py** | 1978 | **分布式协作+隐私过滤+能力胶囊**。SoulAgent平台的Hive三层架构：Identity→Capability→Birth | 设计了多Agent协作模型，但当前只有单Worker运行，多VPS也是同构副本 | 中 — 多Agent差异化时需要 |
| **doom_loop.py** | 925 | **Agent循环/停滞检测**。检测AI在tool-calling loop里反复调用同一工具 | v0.5蒸馏模块，审计发现检测器组合状态爆炸风险，修了但没接入 | 高 — unified_worker已有iteration上限但检测不够智能 |
| **memory.py** | 1073 | FAISS语义记忆（v0.3版），后被memory_store.py(SQLite+FTS5)替代 | 两个MemoryStore同名冲突，v0.7用memory_store | 低 — 已被替代 |
| **subagent.py** | 967 | 子Agent生成。让Worker派生子任务给独立进程 | 写了但exec_delegate_task只是写SQLite就返回，subagent从未真正启动 | 中 — 并行任务需要 |
| **workflow.py** | 1219 | 工作流编排。多步任务定义+DAG执行 | 设计了但unified_worker的tool-calling loop覆盖了简单场景 | 低 — 简单场景够用 |
| **truthful_reporting.py** | 1152 | **报告验证**。AI声称完成但实际没做→检测并纠正 | 主人明确要求"反幻觉"，写完但验证管线没接入 | 高 — 防幻觉核心 |

### 第二梯队：增强能力，接通后提升质量

| 模块 | 行数 | 设计目的 | 接通优先级 |
|------|------|----------|------------|
| **reflection.py** | 736 | 异模型评判+量化评分。让另一个LLM检查输出质量 | 中 — 安全修了但未接 |
| **clarify.py** | 836 | 澄清请求系统。AI不确定时主动提问而非瞎猜 | 中 — 减少误解 |
| **sandbox.py** | 906 | Python代码沙箱。执行AI生成的代码时隔离运行 | 高 — self_repair的前提 |
| **cron.py** | 910 | 更完整的cron实现（vs cron_scheduler.py的简化版） | 低 — cron_scheduler够用 |
| **cron_scheduler.py** | 750 | cron简化版 | 已接通(模块内) |
| **enhanced_router.py** | 640 | 增强版模型路由，质量反馈驱动路由选择 | 低 — model_failover够用 |
| **tool_router.py** | 958 | 工具路由+保护文件列表 | 低 — tool_executor够用 |
| **tool_registry.py** | 795 | 工具注册表 | 低 — tool_executor够用 |
| **tool_call_parser.py** | 307 | 工具调用解析 | 低 — LLM原生tool-calling |
| **eval.py** | 844 | 评测框架 | 低 — 测试基础设施 |
| **memory_evolve.py** | 1026 | **记忆进化**。从经验中提取模式存入记忆。主人说"AI应该自己学会记住什么" | 中 — 有真逻辑但依赖agent_runtime |
| **memory_store.py** | 628 | SQLite+FTS5记忆存储 | 低 — modules/有替代 |
| **memory_sense.py** | 366 | FTS5记忆搜索（中文n-gram） | 低 — unified_worker已用tool |
| **prompt_generator.py** | 335 | 动态prompt生成，axioms注入 | 低 — prompt.py已够用 |
| **session_snapshot.py** | 516 | 会话快照 | 低 — session_manager已够用 |
| **web_sense.py** | 336 | 网页抓取(SSRF保护) | 低 — bash+curl已够用 |

### 第三梯队：概念模块/桩文件

| 模块 | 行数 | 设计目的 | 接通优先级 |
|------|------|----------|------------|
| **plugin_interface.py** | 461 | 插件接口协议。7个stub=半实现 | 低 — 桩文件 |
| **detection.py** | 329 | 版本检测。3个stub | 低 |
| **state_sense.py** | 228 | KV状态持久化 | 低 — 已有exec_state_save |
| **anchor.py** | 388 | Anchor协议（Bootstrap Anchor的一部分） | 低 — 概念完整但无运行时 |
| **axioms.py** | 381 | **物理公理基座**。6条物理定律映射到推理规则，让AI的决策有物理约束 | 中 — 独特设计，constitution.py依赖它 |
| **errors.py** | 430 | 错误分类体系 | 低 — 日志格式化 |
| **identity.py** | 169 | 身份管理 | 低 — 配置层 |

## 关键结论

1. **不是废代码**。33个未连接模块里，至少10个有真逻辑、是主人要求的功能，只是没接进主循环。
2. **自修复链最关键**：self_repair → 依赖 sandbox + checkpoint → 依赖 constitution。这条链通了，AI才能真正"自己修自己"。
3. **防幻觉链**：truthful_reporting + doom_loop + constitution。主人最在意的就是AI说做了但其实没做。
4. **worker_framework 的 WorkerConfig**：虽然入口被替代了，但 WorkerConfig 和 BootstrapAnchor 是配置体系核心，可能仍有代码引用。
5. **memory_evolve 有真逻辑**："AI应该自己学会记住什么"——主人原话。不是空壳。

## 下一步：先修 bug，再接能力链

当前7个bug补丁已部署。接下来按优先级接能力：

1. **doom_loop → unified_worker**（最简单，检测点插入即可）
2. **truthful_reporting → unified_worker**（验证管线接入reply阶段）
3. **constitution → unified_worker**（行为约束接入tool执行前）
4. **checkpoint + sandbox → self_repair**（完整自修复链）
5. **memory_evolve**（依赖agent_runtime，最后接）
