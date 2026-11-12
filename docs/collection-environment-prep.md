# 远程采集的环境准备与回滚

本文档对应任务 3.3（MKACG-776），是 `backend/src/system_analyzer/collection/` 里
「环境检查 / 备份 / 临时配置 / 恢复」四步的设计与验收依据。三阶段编排本身见
`docs/collection-orchestration.md`，本文只讲每一步对目标机器做了什么。

- 范围：采集前提检查、提权通道、临时 systemd 配置（资源记账）、配置备份与恢复记录、
  远端暂存目录与脚本上传、恢复的可重复执行与手工重跑、清理。
- 不在范围：具体采集什么（3.4 资源、3.5 Journal、3.6 LightDM 登录）、数据回传（3.7）。

## 1. 四步在计划里的位置

`collection/plan.py` 的默认计划没有改动，3.3 只把四个占位换成了真实实现：

| 阶段 | 步骤 | 做什么 | 代码 |
| --- | --- | --- | --- |
| Pre | 检查目标环境 | 取系统基本信息 + 能力检查，不满足就失败 | `plan.inspect_target` |
| Pre | 备份系统配置 | 下载要改的文件，记下权限、属主、校验和 | `plan.backup_configuration` |
| Pre | 应用临时配置 | 建远端暂存目录、临时打开资源记账、放恢复脚本 | `plan.apply_temporary_configuration` |
| Post | 恢复环境 | 写回备份、撤销自己的改动、重载配置、删暂存目录 | `plan.restore_configuration` |

新增模块：

| 模块 | 内容 |
| --- | --- |
| `collection/environment.py` | 能力检查：一条只读命令取字段，一条提权命令探 `sudo`，结论与判定 |
| `collection/accounting.py` | 临时打开资源记账：临时 drop-in 的内容与安装 |
| `collection/restore.py` | 改动记录、备份、提权安装、恢复、恢复脚本生成 |
| `collection/staging.py` | 远端暂存目录：创建、上传、删除 |

## 2. 采集前提检查

一条合并的 `run()` 命令取回全部字段，每项都用 `$(...) 2>/dev/null` 包住：某一项取不到时
输出空值，而不是让整条命令失败。「这个字段没取到」与「整台机器连不上」是两件事。

| 字段 | 取自 | 判定 |
| --- | --- | --- |
| `systemd_version` | `systemctl --version` 第一行 | 必须取到；取不到说明没有可用的 systemd |
| `cgroup_filesystem` | `stat -c %T -f /sys/fs/cgroup` | 必须是 `cgroup2fs`，即 cgroup v2 统一层级 |
| `cgroup_controllers` | `cat /sys/fs/cgroup/cgroup.controllers` | 记录可用控制器，供 3.4 判断能读哪些 `*.stat` |
| `default_*_accounting` | `systemctl show -p Default*Accounting --value` | 记录四个记账默认开关，决定要不要临时打开 |
| `privileged` | `run_privileged("true")` 是否以 0 退出 | 必须为真，否则无法临时修改配置 |

不满足时 `ensure_supported()` 抛 `CollectionEnvironmentError`，消息里逐条写清原因，例如
「目标机器不允许 SSH 用户用登录口令执行 sudo，无法临时修改配置」。检查发生在「检查目标环境」
这一步，阶段因此失败，后续步骤不会在没确认过的机器上动手。检查结论在失败之前就写进
`ctx.data["environment"]`，读记录的人不必再连一次机器。

## 3. 提权通道

改目标机器 `/etc` 下的配置需要 root，而 SSH 用户只有自己的登录口令。通道收在
`remote/client.py` 里，`SshClient` 协议与 `AsyncSshClient` 各有一个方法：

```python
async def run_privileged(
    self, command: str, *, timeout: float | None = None, check: bool = True
) -> CommandResult
```

`run()` 的签名一个字都没改。这样做的理由：

- 口令只存在于 `SshConnectionConfig`（`repr=False`）与连接过程里。采集层的 `PhaseContext`
  只把 `SshClient` 交给步骤，步骤拿不到口令，也就不可能把它写进日志、记录或磁盘。
- 口令经 stdin 送给 `sudo -S -p ''`，不出现在命令行里，`ps` 看不到；也不写进目标机器的文件。
- 错误分类与 `run()` 共用同一段逻辑：`sudo` 拒绝是 `SshCommandError`，连不上、超时仍是
  `SshConnectError` / `SshTimeoutError`。

三条实现约束（都在验证机上实测过）：

1. **一次调用只跑一条 sudo 命令。** `sudo -v` 之后再 `sudo -n` 在 SSH 通道上不成立，
   同一行用 `&&` 串两条时第二条仍会再要口令。所以「装文件」与「`systemctl daemon-reload`」
   是两次 `run_privileged()`。
2. **不解析提权命令的 stdout。** 目标机器的 sudo 在验证成功后会往 stdout 打一行横幅，
   标准输出不是干净数据；需要数据就让命令写到暂存文件，再用 `run()` 以普通身份读回来。
   配置改动与恢复只看退出码。
   唯一的例外是「读不到文件状态」时的探测：命令输出里带固定标记
   （`__sas_absent__` / `__sas_unknown__`），解析时只认这两行标记与严格的三段属性行，
   sudo 横幅不会被当成数据。它回答的是「不存在」还是「读不到」，不能靠退出码区分。
3. **命令可能没读口令就退出**（sudo 直接拒绝、`install` 立刻报错）。此时写 stdin 会拿到
   已关闭的管道，实现忽略这个写入失败，结论仍以退出码为准。

## 4. 临时打开资源记账

手段是**临时 drop-in**：`/etc/systemd/system.conf.d/99-system-analyzer-collection.conf`，
里面是 `[Manager]` 段加需要打开的开关。不用 `systemctl set-property` 的理由：

- `set-property` 按 unit 逐个设置，覆盖不了「采集窗口内新起的 unit」，逐个点名既叫不全也补不上。
- drop-in 是一个文件：改动前后可逐行 diff，备份与恢复都是文件级操作，路径与校验和可追溯。
- 恢复就是删掉这次新建的那一个文件（或写回原内容），不必逐个 unit 回退，也就不会漏。

只打开「当前明确关着」的开关，从不关闭任何东西；读不到状态的开关也不动（猜错方向的代价
比少开一项大）。什么都不用打开时同样留一条记录说明「已经够了」，而不是安静跳过。

代价是每改一次配置执行一次 `systemctl daemon-reload`：它只让 systemd 重读配置，不重启任何
服务，不影响已有 unit 的记账数据。装上之后与删掉之后各一次。

> 记账不是采集磁盘 I/O 的前提。cgroup v2 下 unit 的 `io.stat` 与开关无关，`IOAccounting=no`
> 时 `system.slice` 下照样有 `rbytes` / `wbytes`。3.4 直接从 cgroup 文件读，不要设计成
> 依赖这一步临时打开的开关；这一步的真实用途是给 3.6 那类「临时改系统配置再改回来」的
> 动作提供机制。

## 5. 备份与改动记录

`ctx.data["backup"]` 记录 payload 声明要备份的文件（`options.backup_paths`），
`ctx.data["changes"]` 记录采集过程自己改过的文件。两者都随采集记录持久化，所以即使本次尝试
在 In Progress 阶段失败、任务被重新领取，Post 的「恢复环境」仍然知道要收尾什么。

每个文件一条记录：

```json
{
  "path": "/etc/systemd/system.conf.d/99-system-analyzer-collection.conf",
  "existed": false,
  "mode": "644",
  "uid": 0,
  "gid": 0,
  "backup_local": "systemd-accounting.conf",
  "backup_remote": "backup/systemd-accounting.conf",
  "staged_remote": "stage/systemd-accounting.conf",
  "checksum": "备份内容的 SHA-256"
}
```

- `existed` 是**改动前**是否存在：原先是新建出来的文件，恢复时删掉；原先存在的，写回内容。
- `mode` / `uid` / `gid` 是**改动前**的属性：恢复要连权限与属主一起写回去。
- `checksum` 让事后能核对「写回去的」与「当初备份的」是不是同一份。
- 3.2 的旧形状是 `{远端路径: 本地文件名}`，没有属性也没有远端副本。旧记录照读：属性未知时
  恢复用 `cp -f`（只写内容，目标文件现有属性保持不变），不因为记录是旧的而报错。

**「不存在」必须有证据。** 普通身份 `stat` 失败时用提权再探测一次：提权探测明确回答
「不存在」才记 `existed: false`；提权本身失败或探测跑完仍拿不到状态，备份阶段直接失败并
说明是哪个路径。这样恢复不会以 root 删掉一个原本存在、本次也没有创建的文件。

**改动记录先于安装落盘。** 每次安装之前，当前改动清单先写成远端暂存目录里的
`changes.json`（先写临时文件再原子替换）。采集记录按步骤保存，进程若在安装中途被
`kill -9`、内存不足或发布重启打断，这次改动还没有进入采集记录，但上一次尝试留下的
`changes.json` 仍在。下一次尝试在「备份系统配置」里先读回它：

- `ctx.data["changes"]` 补上这些路径，Post 因此知道要收尾什么。
- 同一路径已经在改动记录里时不再重新下载，避免把已被改过的内容当成原始配置。
- 记录里的路径必须是绝对路径且不含换行，因为它最终会拼进提权命令。

`changes.json` 解析不了属于状态不明，备份阶段失败并报出远端路径，不继续猜。

备份是「每次采集一次」而不是「每次尝试一次」：记录里已经有备份清单时不再下载。否则重试时
若重新下载，会把目标机器上已被临时配置改过的内容当成原始配置保存。

## 6. 恢复

`restore_configuration` 按顺序做四件事：

1. 按 `ctx.data["backup"]` 把 payload 备份过的文件写回原内容与原来的属性。
2. 按 `ctx.data["changes"]` 撤销采集过程自己的改动：原先存在的写回备份，原先不存在的删掉。
3. 改动过 `/etc/systemd` 下的文件时执行一次 `systemctl daemon-reload`。
4. 删掉远端暂存目录。

**可重复执行**：原先存在的文件每次都被写回同一份备份内容，原先不存在的文件每次都被删掉
（删不存在的文件也是成功），暂存目录先按需重建再删除。跑两次结果一致，这是恢复的硬要求：
Post 在每次尝试都会执行。

**什么都没动过时就什么都不做**：如果 Pre 在「检查目标环境」就失败，记录里既没有备份也没有
改动，`restore_configuration` 直接返回、不再连一次机器——这时连不上不该被记成「清理失败」，
因为这台机器从来没被改过。

**备份丢了就报错**：本地备份文件不存在时抛 `RemoteError`，让阶段失败。这种情况下远端很可能
停在一份被改过的配置上，必须让人看见，不能安静地跳过。

## 7. 远端暂存目录与恢复脚本

位置：`/var/tmp/system-analyzer-collection/<run_id>/`。放在 `/var/tmp` 而不是 `/tmp`：
`/tmp` 在重新启动时会被清空，而采集过程本身可能包含一次 LightDM 重启（3.6），暂存内容不能
跟着消失。`run_id` 由 `collection.jobs` 限定为 UUID，`staging.workspace_dir()` 再校验一次
字符集——这个名字会拼进 `rm -rf` 的参数。

布局：

```text
/var/tmp/system-analyzer-collection/<run_id>/
├── backup/     原件副本（每个被改过的文件一份）
├── stage/      待安装的新内容、恢复时用的临时文件
└── restore.sh  可脱离 Worker 手工执行的恢复脚本
```

`restore.sh` 是同一份记录的另一种执行方式，进程被强杀时人可以在目标机器上直接收尾：

```sh
sh /var/tmp/system-analyzer-collection/<run_id>/restore.sh
```

它把有备份的文件恢复成原内容与原来的权限属主，把原本不存在的文件删掉，改过 `/etc/systemd`
下的东西就重新加载一次 systemd 配置。脚本可重复执行；缺备份时明确报错退出，不假装恢复成功。

恢复脚本在每次登记改动后重新生成一份，所以 3.4 / 3.6 通过 `restore.stage_and_install()`
改的文件也会出现在脚本里。采集正常结束时它随暂存目录一起删掉。

## 8. 四种结局分别走哪条路

| 结局 | 编排的行为 | 恢复 |
| --- | --- | --- |
| 成功 | 三个阶段都成功 | Post 的「恢复环境」正常执行 |
| 失败 | 阶段记 failed，随后无条件执行 Post | 同上；恢复失败则本次尝试记 failed |
| 取消 | 在下一个步骤之前停下，Post 不响应取消 | 同上；「取消且清理成功」才算 cancelled |
| 超时 | 阶段记 timed_out，随后执行 Post | 同上；恢复失败同样让本次尝试记 failed |

Post 无条件执行这一条由 3.2 的编排保证（`docs/collection-orchestration.md` 第 1、7 节），
3.3 只保证「恢复环境」这一步本身可重复执行、可脱离进程手工重跑。

对应的测试：

| 用例 | 覆盖 |
| --- | --- |
| `test_collection_preparation.py::test_successful_run_prepares_and_cleans_up` | 成功：准备 → 备份 → 恢复 → 清理，记录可追溯 |
| `test_collection_preparation.py::test_every_ending_still_restores_the_configuration` | 失败 / 取消 / 超时三种结局都恢复并清理 |
| `test_collection_preparation.py::test_machine_without_sudo_fails_before_touching_anything` | 不能提权时在检查阶段失败，说清原因 |
| `test_collection_restore.py::test_restore_is_repeatable` | 恢复两次结果一致 |
| `test_collection_restore.py::test_old_shape_record_restores_content_without_touching_attributes` | 旧记录照读，只写内容 |
| `test_collection_restore.py::test_restore_script_*` | 恢复脚本的内容、幂等语义、缺备份时报错 |
| `tests/integration/test_collection_jobs_db.py::test_preparation_record_is_stored_and_honours_file_attributes` | 记录在真实 PostgreSQL 里存得下、读得回 |
| `tests/test_ssh_client.py::test_run_privileged_sends_the_password_on_stdin` | 口令走 stdin，不进命令行 |

## 9. 部署前提

目标机器要满足两件事，缺一不可：

1. systemd，且 cgroup 为 v2 统一层级。
2. SSH 用户能**用登录口令**执行 `sudo`（在 `sudo` 组里，sudo 用同一组凭据）。

第 2 条是平台本身的性质：要临时改系统配置就需要管理员权限。采集不会去改 `sudoers`、
不会安一条免密规则，也不会把口令写到目标机器上；不满足时在能力检查阶段用可读的理由失败。
如果将来的目标机器采用「固定命令免密 sudo」的部署方式，只需要换掉提权实现，
`collection/` 里调用 `run_privileged()` 的地方不用动。

## 10. 已知限制

1. `sudo` 的凭据缓存不能指望，因此每一次提权都是一条独立命令、都要送一次口令。提权命令数量
   控制在个位数（检查一次、装片段一次、删片段或写回一次、`daemon-reload` 两次）。
2. 记账的 drop-in 只影响**之后启动**的 unit，已在运行的 unit 不会补上记账数据；这是 systemd
   的行为，不是实现的取舍。
3. 恢复以「本地备份 + 记录」为准。远端副本只服务于手工恢复；Worker 本地磁盘丢了、备份也
   没了时，恢复会明确报错而不是猜内容。
4. `docs/collection-orchestration.md` 第 10 节第 3 条写的是 3.2 交付时的状态（「当前带真实
   实现的步骤是三个」）。3.3 之后这四步都有了真实实现；该文档按 3.3 的派单约束保持不变，
   以本文为准。
