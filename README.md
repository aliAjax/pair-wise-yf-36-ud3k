# 生物样本库知情同意与撤回

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8302`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

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
python3 app.py --db ./data.db --port 8302
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `participant`：参与者；`consent`：同意版本；`sample`：样本；`withdrawal`：撤回申请。

## 同意版本更替

- 样本入库（`store`）时必须登记用途`purpose`，且用途须落在所绑同意版本的`scope`内；登记后保存为样本的`use`字段。
- 草稿版同意激活（`activate`）时，同一参与者的旧 active 版本自动变为`superseded`，绑定旧版且未终结（未`anonymized`/`destroyed`）的样本改绑到新版。
- 新版`scope`未覆盖某条样本登记用途的，该样本转为`suspended`继续停用（不可借出），并列入激活结果的`activation.suspended_samples`；结果同时给出`superseded_consent_ids`和`rebound_sample_ids`。
- 参与者存在`requested`或`approved`状态的撤回申请时，新版本拒绝激活。
- 版本更替、样本改绑/停用和审计写入在同一事务内完成，任一步失败整体回滚，不留半套结果。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

样本销毁和外部机构调用是流程演示，不会自动删除真实存储中的样本。
