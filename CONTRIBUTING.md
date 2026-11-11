# 贡献指南

## 开发环境

要求 Python 3.11+。在源码根目录创建虚拟环境：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install ".[dev]"
```

默认使用零模型后端。未经任务发起人明确授权，不调用真实供应商，不上传任务库，不把密钥写入代码、日志或命令历史。

## 修改原则

- 先写能复现问题的反例，再做最小修复；每个小模块验证通过后再进入下一个。
- 复用现有接口与状态机，不用文档或测试掩盖业务缺陷。
- 不顺带格式化无关文件，不删除历史失败记录，不把复跑成功写成首轮成功。
- 新增公开行为时同步 README、使用指南与 `agent.md` 当前状态。

## 测试

常规：

```powershell
python -m pytest tests techstack_full_v2/tests techstack_research_v2/tests -m "not slow" --basetemp=work/tmp-pytest/change -q
```

slow：

```powershell
python -m pytest tests techstack_full_v2/tests techstack_research_v2/tests -m slow --basetemp=work/tmp-pytest/change-slow -q
```

扫描器自测：

```powershell
python scripts/publication_scan.py --self-test
```

测试输出与运行数据放在项目 `work/` 下，不写入系统临时目录或公开包。公开冻结夹具由 `.gitattributes` 的 `-text` 保护，不得为迎合本机换行设置而改写其字节或删除哈希检查。

## Git 与凭据

- 明确暂存每个目标文件，禁止笼统 `git add -A`；内部 `work/`、`记忆/` 和历史运行数据不等于公开内容。
- `.env`、`.env.*`、数据库、锁文件和缓存不得进入发布包。
- 合成安全反例只能使用明确虚构值。扫描器的豁免必须同时绑定精确相对路径和命中值 SHA256，禁止前缀、长度或全局模式豁免。
- 提交前检查 `git diff --cached --check` 和实际暂存清单。

## 发布白名单

公开来源由 `scripts/export_release_subset.py` 的白名单生成。新增公开源码、测试资源或文档时，必须同时评估：

1. 是否确属公开必要内容；
2. 是否为必需输入，缺失是否应失败；
3. 是否进入 sdist/wheel 或只进入源码；
4. README 是否引用了未随包文件；
5. CI 是否覆盖安装后使用，而不只验证源码内运行。

严格导出与核验：

```powershell
# Windows 若启用 autocrlf，先新建字节固定的干净检出，目标目录须不存在：
git -c core.autocrlf=false clone . .\work\release-source-byte-clean
Set-Location .\work\release-source-byte-clean
python scripts/export_release_subset.py --require-clean --out .\release_subset\dag-agent-v3.2.1
$head = git rev-parse HEAD
if ($LASTEXITCODE -ne 0) { throw "git rev-parse failed" }
python scripts/verify_release.py .\release_subset\dag-agent-v3.2.1 --source-repo . --expected-commit $head
if ($LASTEXITCODE -ne 0) { throw "release verification failed" }
```

该模式要求发布文件与指定 HEAD 原始字节一致，包括换行；不能先做 CRLF/LF 归一化再宣称逐字节相同。

## 文档与发布声明

测试数量、工具数量、版本和支持平台必须来自当前实现或当前日志。零模型成功不等于真实模型、生产级、事实质量或所有平台验收通过。公开上传前必须单独确认仓库、可见性、文件范围、扫描报告和制品摘要。
