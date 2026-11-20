# 澄湾观测湖

澄湾观测资料中心把站端观测日表交给异地夜班。站端记录员提交原始分段，资料值班员选择勘误修订并冻结，异地值班员投递复制完成通知并申请签发，夜班核账员凭原回执领取。日表的段号在勘误前后可以相同，回执对应指定修订的原段、出处和升数。

## 运行

使用 Python 3.11。平台提供标准回归入口。

```text
python3 -B check_project.py
python3 -B demo.py
```

独立检出可直接运行 `python3 -B -m unittest discover -s tests -t .`。演示读取 `sample.json`，在临时目录创建三个独立 SQLite 库，通过真实复制与领取接口输出 B 的 11、13 L、段和行的出处以及合计 24 L。两段 B 实际写成后，仍受理的旧 A 首段才写成。演示随后投递 B 的通知，签发并重开接收库领取。演示还运行 `settlement_examples.py` 的 `partial` 更正链，断开源库与执行库、只重开接收库后输出承接 `6–10 L`、未结 `0–4 L`。`check_project.py` 同时执行原回归与两条更正样例并传递失败退出码。

## Python 接口

| 职责 | 调用 | 返回 |
| --- | --- | --- |
| 打开源库与接收库 | `lake_db.open_source(path)`、`lake_db.open_receiver(path)` | 独立连接，调用方负责关闭 |
| 录入或勘误 | `intake.revisions.record(source, RevisionInput(day, segments))` | `DayRevision`，含日表及其修订号 |
| 冻结 | `freeze.issue.freeze(source, day, revision)` | `Manifest`，含清单号与有序条目 |
| 冻结更正 | `freeze.issue.freeze_correction(source, parent_manifest_id, child_revision, allocations)` | 绑定父清单与分配的 `Manifest` |
| 提交或续办 | `transfer.dispatch.copy(source, receiver, port, request_id, manifest_id)` | `CopyAttempt`，含固定清单和当前资格 |
| 接纳完成事实 | `transfer.completions.event(receiver, port, notice)` | 按操作去重的 `CompletionOutcome` |
| 撤销 | `transfer.dispatch.cancel(receiver, request_id)` | 持久撤销的 `CopyAttempt` |
| 签发 | `publish.service.publish(receiver, port, request_id)` | 固定 `Receipt` |
| 签发更正 | `publish.corrections.publish_correction(receiver, port, request_id, parent_receipt_id)` | 固定更正 `Receipt` |
| 领取 | `audit.readback.read(receiver, receipt_id)` | `ReadbackResult`，含明细、出处和整数合计 |
| 断源核账 | `audit.readback.read_settlement(receiver, base_receipt_id, segment_position, row_position, target_receipt_id)` | 承接与未结区间的字典 |

`intake.rows` 提供 `ObservationRow(liters, provenance)`、`SourceSegment(segment_id, provenance, rows)` 和 `RevisionInput(day, segments)`。段和行使用元组，日表至少有一个段，段内允许零行。段号在同一修订内唯一。日表、段号及出处使用非空白 UTF-8 字符串，按原字符串保存。升数为有符号整数，拒绝空值、浮点数和布尔值。源库按日表从 1 分配修订号，每次冻结分配新的清单号。规范字节以十六进制字符串编码原行有符号整数，避免 SQLite 整数上界以及十进制转换长度对计量的影响。内容摘要覆盖有序升数，清单同时固定段与行的出处。

`transfer.port.CopyPort(path)` 打开独立执行库，支持上下文管理。一个接收库配套一个执行库。`list_operations(receiver, request_id)` 列出按位置登记的操作。`port.execute(operation_id)` 提交该操作的实际写入和完成事实，`port.deliver(operation_id, callback)` 将真实通知交给回调，回调返回后持久确认投递。回调可以使用 `lambda notice: event(receiver, port, notice)`。`port.trace()` 返回受理、写成和投递的持久顺序。受理中断后同参重投沿同一操作续办，已登记且远端已受理的操作可以在 `source=None` 时补记；首次登记或尚缺远端受理材料时需要源库。撤销可重复，已受理命令继续执行，迟到通知保留结清事实。已签发请求的同参重投保留原结果。

错误类型均定义在 `lake_db`。非法输入产生 `InvalidInput`，未知对象产生 `Unknown`，同号异参产生 `Conflict`，资格不符产生 `StateError`，缺段或缺通知产生 `NotReady`，物料、证据或出处错配产生 `MaterialMismatch`。SQLite 事务错误沿原异常返回。发布错误不会留下本次成功回执。领取先核对整份回执，再按清单位置返回全部原段。具名空段保留段级出处和 0 L。重新打开及重复初始化三库保留现有记录。底层登记和发布写入原语由各业务入口调用，接受这些原语的独立夹具应在 `lake_db.write_transaction` 内组合接收库写入。

## 更正与结清

`freeze_correction` 在同一源库接续父清单，子修订须恰为父修订加一。`allocations` 每项为 `(前版段位, 前版行位, 子版段位, 子版行位, 升数)`，位置按原序零基，同一前后位置对只允许一项，升数为正整数，空分配合法。两版全部行值须为非负整数；每行转出与转入不超各自原值。前版未分配余量退出，子版未收到余量新增。`Manifest` 增加 `parent_manifest_id` 与 `allocations` 字段，普通清单两者保持 `None` 与空元组。

`publish_correction` 走原复制与完成通知入口，要求接收库父回执恰为更正清单命名的父清单。同一请求重投同父回执返回首执，异父回执即使来自同一父清单也报 `Conflict`；跨入口报 `Conflict`。缺件报 `NotReady` 可重试，证据错配报 `MaterialMismatch`，拒绝及提交失败不留新回执或父子绑定。

`read_settlement` 沿目标回执实际固定的父链回溯到基回执，至少一次更正，用实际物料与分配复核每个版本。承接量为指定原行仍留存升数在所有相容解释下的最小与最大值，未结量为其补集，只有承接下界等于原行升量才确认结清。未知回执报 `Unknown`，非法位置报 `InvalidInput`，目标等于基或不同父链报 `StateError`，链证据缺失或错配报 `MaterialMismatch`。`settlement_examples.py` 提供 `partial`、`complete` 两条四版样例，`python3 -B settlement_examples.py` 用普通入口与真实更正签发核对。
