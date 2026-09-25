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

- `station`：观测台站；`event`：地震事件；`revision`：挂在已发布事件下、等待复核的修订草案。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建普通对象；请求体为JSON。
- `GET /api/entities/<id>`：读取事件、台站或修订草案的当前记录。
- `POST /api/entities/<id>/actions`：执行状态动作；请求体为`{"action":"动作名","data":{...},"expected_version":数字}`。
- `POST /api/events/<id>/revisions`：分析员在已发布事件下提交修订草案。
- `GET /api/events/<id>/revisions?status=pending_review`：查看事件的草案及排队状态。
- `POST /api/entities/<revision-id>/actions`：复核员提交`approve`或`reject`。
- `GET /api/events/<id>/history`：查询事件所有对外版本、草案、版本处理人和审计时间线。
- `GET /api/audit`：读取审计记录。

### 修订草案规则

1. 只有`analyst`和`admin`可对`published`或`revised`事件提交修订；草案沿用原事件编号并追加草案序号，例如`E123-R001`。
2. 草案必须包含至少一份`added_reports`新增报告和非负的`magnitude`震级复核结果；没有新增报告不能进入复核。
3. 同一事件已有`pending_review`草案时，新提交返回`409 Conflict`，错误详情中的`details.revision_id`给出当前草案编号，不能形成第二条排队。
4. 只有`reviewer`和`admin`可复核。`approve`在同一数据库事务中追加新增报告、更新震级并生成新的事件版本；此前对外版本完整保留。`reject`只更新草案状态，不替换对外事件。
5. 历史接口保留每个版本的创建人、最后处理人、完整数据、草案提交人/复核人和动作时间线。
6. 根页面提供可操作列表：筛选并打开事件、填写新增报告与震级复核结果、查看草案状态、提交复核以及执行通过/退回。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

事件关联使用简化时间差和距离阈值，不包含完整地震定位、震级标定或台站仪器响应。
