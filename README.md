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
已发布的松散对象（thin pack）。

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
  manifest.json       已发布导入清单（提交点）
  manifest.json.tmp   发布期临时文件
```

1. 全部对象在内存中完成解析、差量重建与 ID 核验后，才以松散对象格式
   写入隔离目录并 fsync；
2. `publish` 先把暂存对象以原子 rename 移入 `objects/`（内容寻址，
   可幂等重入），再把导入记录并入清单，write-temp + fsync +
   `os.replace` 原子替换 `manifest.json`——**清单替换是唯一提交点**；
3. 提交点之前取消或崩溃：旧清单原样可读，隔离目录可由
   `cleanup`（或 `abort`）清除；对象已移动而清单未替换的崩溃窗口只会
   留下无害的未引用松散对象（与 git 自身行为一致），重新导入幂等。

## 用法

```bash
python3 -m pack_import --store STORE import file.pack   # 校验+暂存+原子发布
python3 -m pack_import --store STORE import --no-publish file.pack  # 只暂存
python3 -m pack_import --store STORE manifest           # 查看已发布清单
python3 -m pack_import --store STORE cleanup            # 清理中断的暂存
```

Python API：

```python
from pack_import import ObjectStore, PackImporter

store = ObjectStore("STORE")
record = PackImporter(store).import_pack("file.pack")   # 一步完成

staged = PackImporter(store).stage_pack("file.pack")    # 两阶段：先暂存
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
  并用 `git fsck --strict` 复核；
- `tests/test_packfile.py`：手工构造的篡改校验和、截断 zlib、声明长度
  不符、越界拷贝、指令 0、超深差量链、缺失基、依赖环、超限等攻击向量；
- `tests/test_store.py`：隔离、取消、发布前后崩溃与清理的语义；
- `tests/test_cli.py`：命令行冒烟。

带 --tip 的导入代表完整提交图交付：tip 可为 commit 或指向 commit 的 annotated tag，需要验证可达提交、树与文件内容均存在且类型匹配。gitlink 是外部仓库引用，不要求本库含其对象。失败时导入记录和公开对象集合保持原状。未指定 tip 时继续支持原有对象级导入。
