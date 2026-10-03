# pack-import

一个纯 Python 的 Git **PACK v2** 校验导入工具。解析、差量重建、对象 ID
计算全部自行实现（只用标准库 `zlib` / `hashlib`），**不调用
`git index-pack`**；测试用 git 生成合法包并用 `git cat-file` /
`git fsck` 交叉核对。

## 硬性限制

| 限制 | 值 |
|---|---|
| 单个 pack 文件 | ≤ 8 MB |
| 每包对象数 | ≤ 200 |
| 单对象重建后大小 | ≤ 1 MB（超限拒绝整批） |
| 差量链深度 | ≤ 8 层 |
| 差量程序（解压后） | ≤ 1 MB |

支持 `commit` / `tree` / `blob` / `tag` 普通对象以及包内
`OFS_DELTA`、`REF_DELTA`；`REF_DELTA` 的基对象也可以来自目标对象库中
**已成功归档**（清单在录）的对象（thin pack）。

## 校验内容（任一失败即拒绝整批）

- 包尾 SHA-1 与全部前置字节逐一核对；
- 头部签名 / 版本（仅 v2）/ 对象数上限；
- 每个对象头的类型与声明长度 varint 合法性；
- 每条 zlib 压缩流：输出上限截断保护、流必须恰好结束（`eof`）、
  解压长度必须等于声明长度、**压缩流边界决定下一对象起点**——最后一个
  对象之后只允许剩下 20 字节校验和；
- `OFS_DELTA` 偏移必须非零、向后、且指向此前某个对象的起始处；
- 差量程序逐条指令校验：拷贝区间不得越出基对象、字面插入不得越出程序、
  指令 0 拒绝、产出必须恰好等于声明的目标长度；
- 缺失基对象、依赖环、单对象重建超 1 MB → 拒绝整批。

## 暂存与原子发布协议

```
<store>/
  objects/            已发布松散对象（git 对象目录布局）
  staging/<token>/    隔离区：一次在途导入
  manifest.json       已发布导入清单（提交点，也是"哪些对象算已归档"的唯一依据）
  manifest.json.tmp   发布期临时文件
  manifest.lock       发布串行锁
```

1. 全部对象在内存中完成解析、差量重建与 ID 核验，**指定的 tip
   还要完成可达性与引用类型校验**（见下节）；任何一步失败都在写入
   隔离目录之前中止；
2. 校验全部通过后才以松散对象格式写入隔离目录并 fsync；
3. `publish` 先把暂存对象以原子 rename 移入 `objects/`（内容寻址，
   可幂等重入），再把导入记录并入清单，write-temp + fsync +
   `os.replace` 原子替换 `manifest.json`——**清单替换是唯一提交点**。
   提交点之前发生失败（含清单写入失败）会把本次新移动的对象回滚回
   隔离目录，因此被拒绝的导入既不会留下公开对象，也不会追加成功
   清单；真正的硬崩溃窗口只会留下无害的未引用松散对象（与 git 自身
   行为一致），而这些对象**不在清单里**——后续导入不会把它们当作已
   归档内容或 thin pack 基对象，重新导入幂等；
4. 提交点之前取消或崩溃：旧清单原样可读，隔离目录可由
   `cleanup`（或 `abort`）清除。

## tip 交付语义

`--tip <oid>`（可重复）声明本次导入的提交入口：**成功即代表从 tip
可达的全部提交、树与文件内容在本库齐备且类型匹配**；包内对象各自
哈希正确并不够：

- tip 可以是 commit，也可以是最终剥离（peel）到 commit 的
  annotated tag（支持 tag 链）；tag 指向非 commit、tip 本身是
  blob/tree 一律拒绝；
- commit 的 `tree` 必须解析到 tree 对象；**每个** `parent` 都必须
  解析到 commit——合并提交的多条祖先线都会遍历，缺任一侧祖先即拒绝；
- tree 条目按模式校验引用种类：普通文件（`100644`/`100755`）与
  符号链接（`120000`，其 blob 内容即链接目标路径）必须解析到 blob，
  目录（`40000`）必须解析到 tree；模式非法、条目名缺失、对象 id
  截断均拒绝；条目名按原始字节解析，二进制文件名合法；
- `160000` 是 gitlink（子模块指针）：被引用的提交属于**外部**
  仓库，不要求本地存在，只校验 id 形状；
- 引用对象可由本次包提供，也可来自**已成功归档**（在清单中且在
  磁盘上）的共享对象；清单之外的松散对象（崩溃遗留）不能满足任何
  引用，也不能充当 thin pack 基；
- 未指定 `--tip` 时不做图闭包校验，原有对象级导入语义保持可用。

## 用法

```bash
python3 -m pack_import --store STORE import file.pack            # 校验+暂存+原子发布
python3 -m pack_import --store STORE import --tip <commit-oid> file.pack  # 完整提交图交付
python3 -m pack_import --store STORE import --no-publish file.pack        # 只暂存
python3 -m pack_import --store STORE manifest           # 查看已发布清单
python3 -m pack_import --store STORE cleanup            # 清理中断的暂存
```

Python API：

```python
from pack_import import ObjectStore, PackImporter

store = ObjectStore("STORE")
record = PackImporter(store).import_pack("file.pack")                    # 一步完成
record = PackImporter(store).import_pack("file.pack", tips=[commit_oid])  # 校验提交闭包

staged = PackImporter(store).stage_pack("file.pack", tips=[commit_oid])  # 两阶段：先暂存
store.publish(staged)                                   # 提交
# 或 store.abort(staged)                                # 取消
```

## 测试

```bash
python3 -m pytest
```

- `tests/test_git_roundtrip.py`：用 `git pack-objects` 生成
  OFS_DELTA / REF_DELTA / `--thin` 包，导入后用 `git cat-file`
  （`GIT_OBJECT_DIRECTORY` 指向导入库）逐对象比对类型与原始字节，
  并用 `git fsck --strict` 复核；也包含带 tip 的合并提交、annotated
  tag、二进制文件名、符号链接、子模块 gitlink 与分包交付场景；
- `tests/test_connectivity.py`：手工构造对象覆盖 tip 可达性/类型
  校验、合并祖先、tag 链、gitlink、崩溃遗留对象不可见等语义；
- `tests/test_packfile.py`：手工构造的篡改校验和、截断 zlib、声明长度
  不符、越界拷贝、指令 0、超深差量链、缺失基、依赖环、超限等攻击向量；
- `tests/test_store.py`：隔离、取消、发布失败回滚、清单视图与崩溃
  清理的语义；
- `tests/test_cli.py`：命令行冒烟（含 `--tip` 成功与失败原子性）。
