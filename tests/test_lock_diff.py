"""POST /lock-diff 的端到端测试。

覆盖：四类变化项（added/removed/upgraded/downgraded）与版本方向、
前后依赖图的全部最短根路径（不存在的一侧为空数组）、PEP 440 等价
版本不产生变化项、根包自身版本变化、菱形/环路径、marker 激活、
PEP 503 规范化、确定性（与输入顺序无关）、任一锁无效的 409 短路
（标注 before/after 且 issues 与 /audit-lock 一致）以及 422。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

ENV = {
    "python_version": "3.11",
    "sys_platform": "linux",
    "platform_machine": "x86_64",
}
WIN_ENV = {
    "python_version": "3.11",
    "sys_platform": "win32",
    "platform_machine": "AMD64",
}


def c(version, deps=None):
    return {"version": version, "dependencies": deps or []}


def d(name, spec="", marker=""):
    return {"name": name, "specifier": spec, "marker": marker}


def diff(repo, before, after, *, root="app", env=None, **overrides):
    body = {
        "repository": repo,
        "root": root,
        "target_environment": env or ENV,
        "before_lock": before,
        "after_lock": after,
    }
    body.update(overrides)
    return client.post("/lock-diff", json=body)


def diff_ok(repo, before, after, **kw):
    r = diff(repo, before, after, **kw)
    assert r.status_code == 200, r.text
    return r.json()


# 一个同时产生四类变化的仓库：web-framework 两个版本的依赖声明不同，
# 升级它拉入 observability-lib、淘汰 legacy-helper；cache-lib 降级。
def _full_repo():
    return {
        "app": [c("1.0.0", [d("web-framework", ">=2.0"), d("auth-lib"),
                            d("cache-lib")])],
        "web-framework": [
            c("3.2.0", [d("shared-util", ">=1.0,<3.0"), d("observability-lib")]),
            c("2.4.0", [d("shared-util", ">=1.0,<2.0"), d("legacy-helper")]),
        ],
        "auth-lib": [c("0.9.0", [d("shared-util", "<2.0")])],
        "shared-util": [c("2.0.0"), c("1.5.0")],
        "cache-lib": [c("2.0.0"), c("1.5.0")],
        "observability-lib": [c("1.0.0")],
        "legacy-helper": [c("0.8.0")],
    }


BEFORE_LOCK = {
    "app": "1.0.0",
    "auth-lib": "0.9.0",
    "cache-lib": "2.0.0",
    "legacy-helper": "0.8.0",
    "shared-util": "1.5.0",
    "web-framework": "2.4.0",
}
AFTER_LOCK = {
    "app": "1.0.0",
    "auth-lib": "0.9.0",
    "cache-lib": "1.5.0",
    "observability-lib": "1.0.0",
    "shared-util": "1.5.0",
    "web-framework": "3.2.0",
}


# --------------------------------------------------------------------- #
# 1. 四类变化项与响应结构
# --------------------------------------------------------------------- #
def test_full_diff_all_four_categories():
    body = diff_ok(_full_repo(), BEFORE_LOCK, AFTER_LOCK)
    assert body["status"] == "ok"
    assert body["root"] == "app"

    assert body["added"] == [{
        "name": "observability-lib",
        "from": None,
        "to": "1.0.0",
        "before_paths": [],
        "after_paths": [["app", "web-framework", "observability-lib"]],
    }]
    assert body["removed"] == [{
        "name": "legacy-helper",
        "from": "0.8.0",
        "to": None,
        "before_paths": [["app", "web-framework", "legacy-helper"]],
        "after_paths": [],
    }]
    assert body["upgraded"] == [{
        "name": "web-framework",
        "from": "2.4.0",
        "to": "3.2.0",
        "before_paths": [["app", "web-framework"]],
        "after_paths": [["app", "web-framework"]],
    }]
    assert body["downgraded"] == [{
        "name": "cache-lib",
        "from": "2.0.0",
        "to": "1.5.0",
        "before_paths": [["app", "cache-lib"]],
        "after_paths": [["app", "cache-lib"]],
    }]


def test_identical_locks_produce_no_changes():
    body = diff_ok(_full_repo(), BEFORE_LOCK, dict(BEFORE_LOCK))
    assert body == {
        "status": "ok",
        "root": "app",
        "added": [],
        "removed": [],
        "upgraded": [],
        "downgraded": [],
    }


def test_swapping_locks_flips_directions():
    forward = diff_ok(_full_repo(), BEFORE_LOCK, AFTER_LOCK)
    backward = diff_ok(_full_repo(), AFTER_LOCK, BEFORE_LOCK)
    assert [e["name"] for e in backward["added"]] == [
        e["name"] for e in forward["removed"]
    ]
    assert [e["name"] for e in backward["removed"]] == [
        e["name"] for e in forward["added"]
    ]
    assert [e["name"] for e in backward["upgraded"]] == [
        e["name"] for e in forward["downgraded"]
    ]
    assert [e["name"] for e in backward["downgraded"]] == [
        e["name"] for e in forward["upgraded"]
    ]
    # 交换前后后，路径数组也随之互换。
    for fwd, bwd in zip(forward["upgraded"], backward["downgraded"]):
        assert fwd["name"] == bwd["name"]
        assert fwd["before_paths"] == bwd["after_paths"]
        assert fwd["after_paths"] == bwd["before_paths"]


# --------------------------------------------------------------------- #
# 2. 版本方向按 PEP 440 判断
# --------------------------------------------------------------------- #
def test_pep440_equivalent_versions_produce_no_change():
    """1.0 与 1.0.0 是同一版本：不产生任何变化项。"""
    repo = {"app": [c("1.0", [d("libx")])], "libx": [c("1.0.0")]}
    body = diff_ok(repo, {"app": "1.0", "libx": "1.0"},
                   {"app": "1.0.0", "libx": "1.0.0"})
    assert body["added"] == body["removed"] == []
    assert body["upgraded"] == body["downgraded"] == []


def test_version_direction_uses_pep440_not_lexicographic():
    """1.9.0 -> 1.10.0 是升级（字典序会误判为降级）。"""
    repo = {
        "app": [c("1.0", [d("libx")])],
        "libx": [c("1.10.0"), c("1.9.0")],
    }
    body = diff_ok(repo, {"app": "1.0", "libx": "1.9.0"},
                   {"app": "1.0", "libx": "1.10.0"})
    assert [e["name"] for e in body["upgraded"]] == ["libx"]
    assert body["downgraded"] == []


def test_prerelease_direction_and_root_version_change():
    """根包自身也是根可达包：根版本变化同样产生变化项（单节点路径）。"""
    repo = {
        "app": [c("2.0"), c("1.0")],
    }
    body = diff_ok(repo, {"app": "1.0"}, {"app": "2.0"})
    assert body["upgraded"] == [{
        "name": "app",
        "from": "1.0",
        "to": "2.0",
        "before_paths": [["app"]],
        "after_paths": [["app"]],
    }]


# --------------------------------------------------------------------- #
# 3. 路径：菱形保留、环安全、深层新增
# --------------------------------------------------------------------- #
def test_diamond_paths_preserved_on_both_sides():
    """变化项在两张图中各自保留全部最短根路径（菱形等长路径全保留）。"""
    repo = {
        "app": [c("1.0", [d("left"), d("right")])],
        "left": [c("1.0", [d("shared")])],
        "right": [c("1.0", [d("shared")])],
        "shared": [c("1.0"), c("2.0")],
    }
    before = {"app": "1.0", "left": "1.0", "right": "1.0", "shared": "1.0"}
    after = {"app": "1.0", "left": "1.0", "right": "1.0", "shared": "2.0"}
    body = diff_ok(repo, before, after)
    assert body["upgraded"] == [{
        "name": "shared",
        "from": "1.0",
        "to": "2.0",
        "before_paths": [
            ["app", "left", "shared"],
            ["app", "right", "shared"],
        ],
        "after_paths": [
            ["app", "left", "shared"],
            ["app", "right", "shared"],
        ],
    }]


def test_paths_can_differ_between_sides_for_same_package():
    """同一包在前后两张图中的引入路径可以不同（经由不同的中间包）。"""
    repo = {
        "app": [c("1.0", [d("via-a"), d("via-b")])],
        "via-a": [c("1.0", [d("shared")])],
        "via-b": [c("1.0"), c("2.0", [d("shared")])],
        "shared": [c("1.0")],
    }
    before = {"app": "1.0", "via-a": "1.0", "via-b": "1.0", "shared": "1.0"}
    after = {"app": "1.0", "via-a": "1.0", "via-b": "2.0", "shared": "1.0"}
    body = diff_ok(repo, before, after)
    upgraded = {e["name"]: e for e in body["upgraded"]}
    assert set(upgraded) == {"via-b"}
    # shared 版本未变，不出现在任何变化列表中。
    assert body["added"] == body["removed"] == body["downgraded"] == []


def test_added_package_deep_in_graph_gets_full_path():
    repo = {
        "app": [c("1.0", [d("liba")])],
        "liba": [c("1.0"), c("2.0", [d("libb")])],
        "libb": [c("1.0", [d("libc")])],
        "libc": [c("1.0")],
    }
    before = {"app": "1.0", "liba": "1.0"}
    after = {"app": "1.0", "liba": "2.0", "libb": "1.0", "libc": "1.0"}
    body = diff_ok(repo, before, after)
    assert [e["name"] for e in body["added"]] == ["libb", "libc"]
    assert body["added"][0]["after_paths"] == [["app", "liba", "libb"]]
    assert body["added"][1]["after_paths"] == [["app", "liba", "libb", "libc"]]
    assert all(e["before_paths"] == [] for e in body["added"])
    assert [e["name"] for e in body["upgraded"]] == ["liba"]


def test_cycle_in_graph_is_safe_for_diff_paths():
    repo = {
        "app": [c("1.0", [d("cyc-a")])],
        "cyc-a": [c("1.0", [d("cyc-b")])],
        "cyc-b": [c("1.0", [d("cyc-a")]), c("2.0", [d("cyc-a")])],
    }
    before = {"app": "1.0", "cyc-a": "1.0", "cyc-b": "1.0"}
    after = {"app": "1.0", "cyc-a": "1.0", "cyc-b": "2.0"}
    body = diff_ok(repo, before, after)
    assert body["upgraded"] == [{
        "name": "cyc-b",
        "from": "1.0",
        "to": "2.0",
        "before_paths": [["app", "cyc-a", "cyc-b"]],
        "after_paths": [["app", "cyc-a", "cyc-b"]],
    }]


# --------------------------------------------------------------------- #
# 4. marker 激活与 PEP 503 规范化
# --------------------------------------------------------------------- #
def test_marker_gated_dependency_added_and_removed_by_environment():
    """win32 下 libx 1.0 拉入 win-thing、2.0 不拉：升级 libx 即移除它。"""
    repo = {
        "app": [c("1.0", [d("libx")])],
        "libx": [
            c("1.0", [d("win-thing", marker="sys_platform == 'win32'")]),
            c("2.0"),
        ],
        "win-thing": [c("1.0")],
    }
    before = {"app": "1.0", "libx": "1.0", "win-thing": "1.0"}
    after = {"app": "1.0", "libx": "2.0"}
    body = diff_ok(repo, before, after, env=WIN_ENV)
    assert [e["name"] for e in body["removed"]] == ["win-thing"]
    assert body["removed"][0]["before_paths"] == [["app", "libx", "win-thing"]]
    assert body["removed"][0]["after_paths"] == []
    assert [e["name"] for e in body["upgraded"]] == ["libx"]

    # linux 下 win-thing 未激活：before_lock 含它即 extraneous -> 409。
    r = diff(repo, before, after, env=ENV)
    assert r.status_code == 409
    assert [f["side"] for f in r.json()["failures"]] == ["before"]
    assert r.json()["failures"][0]["issues"][0]["type"] == "extraneous"


def test_pep503_normalization_in_lock_keys():
    repo = {
        "App": [c("1.0", [d("Some_Lib")])],
        "some.lib": [c("1.0"), c("2.0")],
    }
    before = {"APP": "1.0", "Some.Lib": "1.0"}
    after = {"app": "1.0", "some_lib": "2.0"}
    body = diff_ok(repo, before, after, root="App")
    assert body["root"] == "app"
    assert body["upgraded"] == [{
        "name": "some-lib",
        "from": "1.0",
        "to": "2.0",
        "before_paths": [["app", "some-lib"]],
        "after_paths": [["app", "some-lib"]],
    }]


# --------------------------------------------------------------------- #
# 5. 确定性：与包键、依赖声明、锁键的输入顺序无关
# --------------------------------------------------------------------- #
def test_deterministic_across_input_ordering():
    repo = _full_repo()
    body1 = diff_ok(repo, BEFORE_LOCK, AFTER_LOCK)
    body2 = diff_ok(
        dict(reversed(list(repo.items()))),
        dict(reversed(list(BEFORE_LOCK.items()))),
        dict(reversed(list(AFTER_LOCK.items()))),
    )
    assert body1 == body2
    # 四个列表均按规范化包名排序。
    for key in ("added", "removed", "upgraded", "downgraded"):
        names = [e["name"] for e in body1[key]]
        assert names == sorted(names)


# --------------------------------------------------------------------- #
# 6. 任一锁无效：409，标注 before/after，issues 与 /audit-lock 一致
# --------------------------------------------------------------------- #
def _invalid_repo():
    return {
        "app": [c("1.0", [d("needs-thing"), d("old-lib", ">=2.0")])],
        "old-lib": [c("1.0"), c("2.0")],
        "needs-thing": [c("1.0")],
        "stale": [c("1.0")],
    }


def test_invalid_before_lock_returns_409_with_side_label():
    repo = _invalid_repo()
    before = {"app": "1.0", "old-lib": "1.0", "stale": "1.0"}  # 缺 needs-thing
    after = {"app": "1.0", "needs-thing": "1.0", "old-lib": "2.0"}
    r = diff(repo, before, after)
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["status"] == "invalid_lock"
    assert "before_lock" in body["message"]
    assert [f["side"] for f in body["failures"]] == ["before"]
    assert [(i["type"], i["name"]) for i in body["failures"][0]["issues"]] == [
        ("missing", "needs-thing"),
        ("constraint_mismatch", "old-lib"),
        ("extraneous", "stale"),
    ]
    # 409 响应不携带任何差异结果。
    for key in ("added", "removed", "upgraded", "downgraded"):
        assert key not in body


def test_invalid_after_lock_returns_409_with_side_label():
    repo = _invalid_repo()
    before = {"app": "1.0", "needs-thing": "1.0", "old-lib": "2.0"}
    after = {"app": "1.0", "needs-thing": "9.9", "old-lib": "2.0"}  # 版本不存在
    r = diff(repo, before, after)
    assert r.status_code == 409, r.text
    body = r.json()
    assert "after_lock" in body["message"]
    assert [f["side"] for f in body["failures"]] == ["after"]
    assert body["failures"][0]["issues"][0]["type"] == "unavailable"


def test_both_locks_invalid_lists_both_sides_in_order():
    repo = _invalid_repo()
    before = {"app": "1.0", "old-lib": "1.0"}  # missing needs-thing 等
    after = {"app": "1.0", "needs-thing": "9.9", "old-lib": "2.0"}
    r = diff(repo, before, after)
    assert r.status_code == 409, r.text
    body = r.json()
    assert [f["side"] for f in body["failures"]] == ["before", "after"]
    assert "before_lock" in body["message"] and "after_lock" in body["message"]


def test_409_issues_match_audit_lock_endpoint_for_each_side():
    """同一锁定清单下，409 各侧的 issues 必须与 /audit-lock 逐字节一致。"""
    repo = _invalid_repo()
    before = {"app": "1.0", "old-lib": "1.0"}
    after = {"app": "1.0", "needs-thing": "9.9", "old-lib": "2.0"}
    body = diff(repo, before, after)
    assert body.status_code == 409
    failures = {f["side"]: f["issues"] for f in body.json()["failures"]}
    for side, lock in (("before", before), ("after", after)):
        audit_body = client.post("/audit-lock", json={
            "repository": repo, "root": "app", "target_environment": ENV,
            "current_lock": lock,
        }).json()
        assert failures[side] == audit_body["issues"]


def test_valid_lock_despite_other_side_invalid_is_still_audited():
    """一侧无效不短路另一侧的审计：两侧问题都会出现在 409 中。"""
    repo = _invalid_repo()
    before = {"app": "1.0", "needs-thing": "1.0", "old-lib": "2.0"}
    after = {"app": "1.0", "needs-thing": "1.0", "old-lib": "2.0",
             "stale": "1.0"}  # extraneous
    r = diff(repo, before, after)
    assert r.status_code == 409
    assert [f["side"] for f in r.json()["failures"]] == ["after"]


# --------------------------------------------------------------------- #
# 7. 422：非法快照 / 锁 / 根包 / 字段
# --------------------------------------------------------------------- #
def test_bad_snapshot_422():
    r = diff({"app": [c("oops")]}, {"app": "1.0"}, {"app": "1.0"})
    assert r.status_code == 422
    assert r.json()["status"] == "invalid_snapshot"


def test_bad_before_lock_version_422_names_field():
    r = diff({"app": [c("1.0")]}, {"app": "not-a-version"}, {"app": "1.0"})
    assert r.status_code == 422
    assert "before_lock" in r.json()["message"]
    assert "PEP 440" in r.json()["message"]


def test_bad_after_lock_version_422_names_field():
    r = diff({"app": [c("1.0")]}, {"app": "1.0"}, {"app": "not-a-version"})
    assert r.status_code == 422
    assert "after_lock" in r.json()["message"]


def test_conflicting_versions_in_one_lock_422():
    r = diff({"app": [c("1.0")], "LibX": [c("1.0"), c("2.0")]},
             {"app": "1.0", "libx": "1.0", "LIBX": "2.0"}, {"app": "1.0"})
    assert r.status_code == 422
    assert "before_lock" in r.json()["message"]


def test_unknown_root_422():
    r = diff({"app": [c("1.0")]}, {"app": "1.0"}, {"app": "1.0"}, root="missing")
    assert r.status_code == 422
    assert "missing" in r.json()["message"]


def test_missing_lock_field_422():
    body = {
        "repository": {"app": [c("1.0")]},
        "root": "app",
        "target_environment": ENV,
        "before_lock": {"app": "1.0"},
    }
    assert client.post("/lock-diff", json=body).status_code == 422


def test_extra_field_forbidden_422():
    r = diff({"app": [c("1.0")]}, {"app": "1.0"}, {"app": "1.0"}, unknown=1)
    assert r.status_code == 422


def test_current_lock_field_not_accepted_422():
    """/lock-diff 只接受 before_lock/after_lock，不接受 current_lock。"""
    r = diff({"app": [c("1.0")]}, {"app": "1.0"}, {"app": "1.0"},
             current_lock={"app": "1.0"})
    assert r.status_code == 422


def test_audit_lock_still_rejects_lock_diff_fields():
    """既有 /audit-lock 接口保持兼容：仍禁止 before_lock/after_lock 字段。"""
    r = client.post("/audit-lock", json={
        "repository": {"app": [c("1.0")]},
        "root": "app",
        "target_environment": ENV,
        "current_lock": {"app": "1.0"},
        "before_lock": {"app": "1.0"},
    })
    assert r.status_code == 422
