# gb-0044 验收记录

本目录不进入源码。设备采样验收脚本不依赖 PostgreSQL/Redis，使用临时 SQLite 与进程内 stub 队列。

## 设备日志采样（本轮新增）

```bash
cd /workspace
python3 test-artifacts/gb-0044/verify_device_sampling.py
```

脚本覆盖 33 项断言：工序窗登记、乱序补报、LOCF 时间加权、窗前无值/断采超限隔离、报齐线门禁与不可回退、同刻异值阻止封窗与裁定、封窗幂等与崩溃恢复、迟到争议不改已封样本、研究只冻结完整批次、手录来源并行可研究、审计事件。

最近一次结果：`RESULT: ALL PASS`。

### 并发与批次恢复（本地推演，未随仓库提交）

- 4 个线程用不同幂等键同时封窗：各自得到独立完整批次，无重复行。
- 并发补报与封窗：撞到同刻唯一键的请求整批回滚并返回 409 重试提示，已提交批次行数不变。
- 人为清空已封批次行并置 `writing`：`/batches/{id}` 标记不完整，`POST /runs` 409；`POST /batches/{id}/resume` 重新生成完整行，二次调用幂等。

### 限制

- 本机无 Docker、无 Chromium 系统库，未执行真实浏览器截图；页面渲染通过 Node 导入页面模块、对接本地运行的后端完成结构断言，交互绑定函数做了无 DOM 冒烟测试。
- PostgreSQL 行锁（`SELECT ... FOR UPDATE`）在 SQLite 测试下退化为库级锁；生产并发语义按 PostgreSQL 设计。
