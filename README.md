# 地震台网事件编目与修订

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8307`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8307
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `station`：观测台站；`event`：地震事件及其多个修订版本。
- `revision`：已发布事件的修订草案，状态流为`draft → pending → applied/rejected`。

## 修订草案流程

1. 分析员对`published`/`revised`状态的事件创建草案：`POST /api/revisions`，请求体带`event_id`（沿用原事件编号）、`reason`、`magnitude`（震级复核结果）和`reports`（新增报告）。
2. 同一事件同一时间只允许一个未结草案；重复提交返回409，错误信息中带当前草案编号。
3. 提交复核：`POST /api/entities/<草案id>/actions`，`action=submit`。没有新增报告或缺少复核震级的草案不能进入复核。
4. 复核员`approve`后，新增报告与复核震级才合并进事件（事件编号不变，版本号递增，状态变为`revised`）；`reject`（需`reason`）则草案作废，可重新创建。
5. 旧版本快照和两次处理人（提交的分析员、复核通过的复核员）可通过`GET /api/entities/<事件id>/history`查询。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/entities/<id>/history`：读取历史版本快照和处理记录。
- `GET /api/audit`：读取审计记录，可用`?entity_id=`过滤。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

事件关联使用简化时间差和距离阈值，不包含完整地震定位、震级标定或台站仪器响应。
