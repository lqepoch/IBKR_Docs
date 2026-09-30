# IBKR_Docs

IBKR 官方 **TWS API / IB Gateway API** 文档的自动化 Markdown 镜像，面向多个交易、研究和 Agent 项目共享使用。

> 本仓库不是 Interactive Brokers 官方仓库，也不隶属于 Interactive Brokers。权威来源始终是 IBKR 官方文档。

## 稳定目录契约

镜像严格保留 IBKR 官方 URL 的目录层级，只在文件末尾增加 `.md`。消费方可以长期依赖这些路径：

```text
https://www.interactivebrokers.com/docs/tws-api/doc/...       -> docs/tws-api/doc/....md
https://www.interactivebrokers.com/docs/tws-api/ref/...       -> docs/tws-api/ref/....md
https://www.interactivebrokers.com/docs/tws-api/protobuf/...  -> docs/tws-api/protobuf/....md
```

仓库结构：

```text
IBKR_Docs/
├── docs/
│   └── tws-api/
│       ├── doc/          # 行为、流程、限制、示例等文档
│       ├── ref/          # Contract / Order / Execution 等对象参考
│       ├── protobuf/     # Protobuf Reference
│       └── ...           # IBKR 后续新增的 TWS API 官方子目录自动保留
├── .meta/
│   ├── manifest.json     # 路径、官方 URL、SHA-256、字节数
│   ├── catalog.txt       # 全量稳定文件路径索引
│   └── source.json       # 上游和镜像规则
├── scripts/
│   └── sync_ibkr_docs.py
├── tests/
└── .github/workflows/
    └── sync-ibkr-docs.yml
```

`.meta/manifest.json` 是跨项目消费时的机器可读契约。不要依赖 GitHub 网页目录排序来发现文档。

## 自动更新

GitHub Actions 每周日自动同步一次：

- `01:17 UTC`
- `09:17 UTC+8`

也支持 `workflow_dispatch` 手工运行。同步流程会：

1. 从 IBKR 官方 `llms.txt`、TWS API 当前站点导航和固定入口发现页面；
2. 优先获取官方 `.md`；当 CI 网络无法访问 Markdown 端点时，从当前官方 HTML 页面确定性转换为 Markdown；
3. 继续跟踪 `/docs/tws-api/...` 内部链接，补齐索引可能遗漏的页面；
4. 在临时目录构建完整快照；
5. 校验最小页面数量、`doc/ref/protobuf` 核心分区、路径安全、SHA-256 与文件集一致性；
6. 对大比例删除执行 fail-closed 保护，降低上游临时异常导致仓库被清空的风险；
7. 仅在实际文档发生变化时提交到 `main`。

任何下载、完整性或并发检查失败都会让 Action 失败，已有镜像保持不变。

## 多项目使用

### Git submodule

```bash
git submodule add https://github.com/lqepoch/IBKR_Docs.git vendor/ibkr-docs
```

然后固定使用：

```text
vendor/ibkr-docs/docs/tws-api/
```

### 普通 clone

```bash
git clone https://github.com/lqepoch/IBKR_Docs.git
rg -n "placeOrder|reqOpenOrders|permId" IBKR_Docs/docs/tws-api
```

### Agent / Codex

项目级 `AGENTS.md` 可以声明：

```text
Authoritative local IBKR documentation mirror:
<path-to-IBKR_Docs>/docs/tws-api

Before changing IBKR/TWS/IB Gateway behavior, search this mirror first and
cite the corresponding local path in implementation notes. If SDK behavior
conflicts with the mirror, verify the current official IBKR source before
changing production trading semantics.
```

## 手工校验

```bash
python scripts/sync_ibkr_docs.py validate
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

手工同步：

```bash
python scripts/sync_ibkr_docs.py sync
python scripts/sync_ibkr_docs.py validate
```

## 数据来源与版权

同步数据来自 `https://www.interactivebrokers.com/docs/tws-api`。自动化代码受本仓库 `LICENSE` 约束；`docs/` 下的镜像内容保持其原始权利归属，不因进入本仓库而重新授权为 MIT。详见 `NOTICE.md`。
