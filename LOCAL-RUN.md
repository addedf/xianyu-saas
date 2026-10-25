# 本机运行

当前源码版本：v0.4.7。使用本机已有 Python 3.12 与 Node.js，依赖隔离在 `backend/.venv/` 和 `worker/.venv/`。

二开仓库：[addedf/xianyu-saas](https://github.com/addedf/xianyu-saas)，开发分支 `codex/local-development`。`origin` 指向该 Fork，`upstream` 保留 [tswawa/xianyu-saas](https://github.com/tswawa/xianyu-saas)。

首次克隆到新目录时，准备 Python 3.12、Node.js 和 uv，然后安装运行依赖：

```bash
git clone --branch codex/local-development https://github.com/addedf/xianyu-saas.git
cd xianyu-saas
uv venv --python 3.12 backend/.venv
uv pip install --python backend/.venv/bin/python -r backend/requirements.txt
uv venv --python 3.12 worker/.venv
uv pip install --python worker/.venv/bin/python -r worker/requirements.txt
cp -n config/saas.env.example config/saas.env
chmod 600 config/saas.env
```

已有本地环境可直接运行下方命令。

在项目目录运行：

```bash
./local.command start   # 后台启动；也可以在 Finder 双击 local.command
./local.command status  # 查看是否就绪
./local.command stop    # 停止服务，保留数据
```

线上接管店铺后，可用 `SAAS_LOCAL_OBSERVE_ONLY=1 ./local.command start` 只启动本地 API 和网页，不启动任务消费者，并关闭店铺自动恢复及自更新。此方式不会同步线上新订单，也不是限制所有 API 写入或外部请求的完整隔离环境；不要手动启动本地店铺。测试订单由线上采集，再另取快照到本地分析。

执行 `./同步闲鱼登录.command` 时，启动器会自动使用 `SAAS_LOCAL_LOGIN_ONLY=1` 的登录专用模式：本地工作台可完成闲鱼扫码并保存店铺 Cookie，但登录完成后的自动恢复不会拉起本地 Worker。同步完成后本地服务默认停止。

观察／登录专用模式未启动消费者时，手动点击「同步闲鱼商品」会由 API 直接完成商品与规格刷新，不再留下无人处理的后台任务；正常部署仍使用异步队列。商品页显示平台上下架状态与同步时间。平台规格数量和可发货卡密库存独立统计，下架后平台仍可能保留规格数量，卡密余量以「卡密库存」为准。

访问 <http://127.0.0.1:4173/xianyu-saas/>，首次在页面设置管理员账号和密码，密码至少 12 位。

本地配置在 `config/saas.env`，数据库、加密主密钥、店铺数据和日志在 `.local/`；这些文件均被 Git 忽略。日志为 `.local/local.log` 和 `.local/local-logs/`，后者分别记录 API、consumer、Web。

启动器直接运行 FastAPI、任务消费者和 Node 静态服务，关闭源码自动热重载，退出时正常关闭服务与受管 Worker。Mac 开发环境不施加 Linux 的 Worker 地址空间上限，Linux `/proc` 资源采样与遗留进程接管保持不可用；受管的本次子进程仍可正常启动和停止。更新源码前先停止本地服务。

Mac 启动时会把更新配置中的系统 `/var/` 路径转换为实际的 `/private/var/`，避免符号链接触发维护状态的可信路径校验。启动检查同时确认数据库就绪及维护状态可读，维护状态异常会明确报错。

卡密配置顺序：建立卡密池 → 每行一条导入 → 创建发货模板 → 绑定对应商品。店铺接入、商品同步与真实订单发货需在界面完成配置后验证。

可建立多个独立池，例如「5 元号池」「10 元号池」。导入时选择目标池，在每个发货模板中选择对应卡密池并绑定商品。发货按商品绑定的池分配卡密，池内库存不足则整单转人工，不会取其他池的卡密。同一条卡密不能重复放入不同池。模板中的价格字段不是分池依据。

多池目录保存于店铺的 `card_pools.json`，每条库存的 `pool_id` 对应一个池；发货记录和库存预占也保存池 ID。旧单池配置仍按 `redeem` 读取，修改后会保存为多池目录。

同一个商品链接有多个规格时，商品同步只对平台标记 `isSKU=1` 的商品追加详情请求，展示 SKU ID、规格、售价和平台库存。「发货模板」先选一个商品，再选一个规格和对应卡密池，每个 SKU 单独建立模板。不同 SKU 可以共用商品 ID；同一 SKU 重复绑定、整商品与 SKU 模板混用会被拒绝。

SKU 发货优先使用平台订单详情中的 `skuId`。部分真实订单只返回 `itemInfo.skuInfo`（如 `额度:5美元额度（USD）`），此时按完整属性名和值，与即时请求的同商品 `skuList.propertyList` 做唯一精确匹配，取得平台 SKU ID；发送前再次读取订单和商品规格复核。不会按价格、聊天或不完整规格推断，重复属性组合也转人工。SKU 保存到 `delivery_events.sku_id`，预占、断线重试及每次发送前均核对原 SKU 和卡密池。无法确认 SKU、未绑定规格、SKU 改变或该池缺货时转人工，不借用其他池。资料表在 Worker 启动时兼容新增可空字段，旧记录保留；回滚前检查结构兼容，勿恢复旧库重置已消耗库存。页面的「平台库存」来自闲鱼，只读展示，与本地可用卡密数量分别管理。

补货前在「会话工作台」点击「暂停自动处理」，导入完成后点击「恢复自动处理」。恢复后台时保留现有客服开关；只做卡密发货时可保持 AI 和规则回复关闭。
