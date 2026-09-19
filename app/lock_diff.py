"""两份锁定清单的差异比较（POST /lock-diff）。

调用本模块前，``before_lock`` 与 ``after_lock`` 已在**同一仓库快照、
同一目标环境、同一根约束**下各自通过 :func:`app.audit.audit_lock` 审计
（无 missing / unavailable / constraint_mismatch / extraneous）。因此
这里不做任何版本选择与约束核对，只在两张**已激活依赖图**（与
/dependency-paths 同一张图的构造规则：按锁定版本展开目标环境下
marker 求值后激活的依赖）之间比较**根可达包**。

* 有效锁的根可达包集恰为锁中全部包（无 missing / extraneous），因此
  差异只可能来自包集合变化（added / removed）与版本变化
  （upgraded / downgraded）；
* 版本方向按 PEP 440 判断：after 严格更高为 ``upgraded``，严格更低为
  ``downgraded``；等价版本（如 ``1.0`` 与 ``1.0.0``）不产生变化项；
* 每个变化项附它在前后两张图中各自的**全部最短根路径**（复用
  :func:`app.paths.shortest_paths`，菱形等长路径全保留、环安全）；
  包不存在的一侧（added 的 before、removed 的 after）自然为空数组；
* 四个列表均按规范化包名排序，路径按字典序排序，输出与包键、依赖
  声明、锁键的输入顺序无关。
"""

from __future__ import annotations

from packaging.version import Version

from .paths import shortest_paths


def diff_locks(
    *,
    root_name: str,
    before_lock: dict[str, Version],
    after_lock: dict[str, Version],
    before_graph: dict[str, list[str]],
    after_graph: dict[str, list[str]],
) -> dict:
    """比较两份有效锁定清单的根可达包，返回四组确定性排序的变化项。

    ``before_graph`` / ``after_graph`` 为
    :func:`app.paths.build_locked_graph` 在各自锁定清单上产出的根可达
    子图（节点集恰为各自锁中全部包）。返回
    ``{"added": [...], "removed": [...], "upgraded": [...],
    "downgraded": [...]}``，每个列表按规范化包名排序。
    """
    added: list[dict] = []
    removed: list[dict] = []
    upgraded: list[dict] = []
    downgraded: list[dict] = []

    # 只比较根可达包：有效锁中可达集 == 锁中全部包，以图节点为准。
    for name in sorted(set(before_graph) | set(after_graph)):
        before_v = before_lock.get(name)
        after_v = after_lock.get(name)
        item = {
            "name": name,
            "from": str(before_v) if before_v is not None else None,
            "to": str(after_v) if after_v is not None else None,
            # 不存在的一侧不在图中，shortest_paths 自然返回空数组。
            "before_paths": shortest_paths(before_graph, root_name, name),
            "after_paths": shortest_paths(after_graph, root_name, name),
        }
        if before_v is None:
            added.append(item)
        elif after_v is None:
            removed.append(item)
        elif before_v < after_v:
            upgraded.append(item)
        elif before_v > after_v:
            downgraded.append(item)
        # PEP 440 等价版本（1.0 == 1.0.0）：不产生变化项。

    return {
        "added": added,
        "removed": removed,
        "upgraded": upgraded,
        "downgraded": downgraded,
    }
