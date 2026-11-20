"""标签快照服务：消费标签计算和分群结果，生成全量快照。

快照的用户基数来自标签计算产物里的用户明细：把各计算任务命中的会员合并去重，
重复命中的会员只计一次。产物里只有覆盖计数、没有用户名单的任务（旧格式）会
如实保留在覆盖范围中，但既不会把计数摊到别的标签上，也不会参与用户基数汇总，
快照会标记基数不可确认。
"""
from __future__ import annotations

import copy
from typing import Any

from app.store import store

# 计算产物里可能承载用户明细的字段，按优先级排列。
_DETAIL_KEYS = (
    "user_tag_values",
    "user_details",
    "user_ids",
    "users",
    "matched_users",
)


def _task_row(task_id: str | None) -> dict[str, Any] | None:
    if not task_id:
        return None
    return store.find_by_field("tag_compute", "task_id", task_id)


def _tag_row(tag_id: str | None) -> dict[str, Any] | None:
    if not tag_id:
        return None
    return store.find_by_field("tag_define", "tag_id", tag_id)


def _source_version(artifact: dict[str, Any], task: dict[str, Any] | None) -> str | None:
    """计算任务使用的来源版本：优先产物自带版本，其次输出分区。"""
    version = (
        artifact.get("source_version")
        or artifact.get("output_partition")
        or (task or {}).get("output_partition")
    )
    return str(version) if version not in (None, "") else None


def _extract_user_detail(artifact: dict[str, Any]) -> tuple[list[str], int, bool]:
    """提取任务产物的用户明细。

    返回 (命中的用户编号列表, 明细原始条数, 是否带有名单字段)。
    只有真正带名单的产物才返回 has_list=True；旧格式产物只有 result_count。
    """
    for key in _DETAIL_KEYS:
        if key not in artifact:
            continue
        raw = artifact.get(key)
        if isinstance(raw, dict):
            raw = list(raw.values())
        if not isinstance(raw, list):
            continue

        users: list[str] = []
        for item in raw:
            user_id: Any = None
            matched = True
            if isinstance(item, str):
                user_id = item
            elif isinstance(item, dict):
                user_id = (
                    item.get("user_id")
                    or item.get("member_id")
                    or item.get("id")
                )
                if "tag_value" in item:
                    matched = bool(item.get("tag_value"))
                elif "matched" in item:
                    matched = bool(item.get("matched"))
            if user_id is None:
                continue
            if matched:
                users.append(str(user_id))
        # 同一任务内也可能重复上报，先按任务去重。
        return list(dict.fromkeys(users)), len(raw), True

    return [], 0, False


def _coverage_entry(artifact: dict[str, Any]) -> dict[str, Any]:
    """把一条标签计算产物整理成快照的覆盖范围条目。"""
    task_id = artifact.get("task_id")
    tag_id = artifact.get("tag_id")
    task = _task_row(task_id)
    tag = _tag_row(tag_id)

    result_count = artifact.get("result_count")
    if result_count is None and task is not None:
        result_count = task.get("result_count", 0)
    result_count = int(result_count or 0)

    users, raw_count, has_list = _extract_user_detail(artifact)

    # 明细是否完整：显式标记优先；否则名单条数需要覆盖结果条数。
    explicit = artifact.get("detail_complete")
    if explicit is not None:
        detail_complete = bool(explicit) and has_list
    elif has_list:
        detail_complete = raw_count >= result_count
    else:
        # 没有名单、结果条数也是 0，说明确实没有可丢的用户。
        detail_complete = result_count == 0

    return {
        "task_id": task_id,
        "tag_id": tag_id,
        "tag_name": (tag or {}).get("tag_name", tag_id),
        "compute_scope": artifact.get("compute_scope") or (task or {}).get("compute_scope", ""),
        "source_version": _source_version(artifact, task),
        "tag_version": (tag or {}).get("version"),
        "result_count": result_count,
        "user_count": len(users),
        "user_ids": users,
        "has_user_detail": has_list,
        "detail_complete": detail_complete,
    }


def _build_coverage(compute_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按任务整理覆盖范围。

    同一个标签可能有多次计算（重算、补算），快照只认该标签最新的一份产物，
    避免把已经被替代的旧任务重复算进覆盖范围。
    """
    latest_by_tag: dict[str, tuple[int, int, dict[str, Any]]] = {}
    for index, result in enumerate(compute_results):
        key = str(result.get("tag_id") or result.get("task_id") or "")
        task = _task_row(result.get("task_id"))
        task_order = int((task or {}).get("id", 0) or 0)
        # 任务表顺序优先，其次产物写入顺序，保证“最新”是确定的。
        rank = (task_order, index)
        if key not in latest_by_tag or rank >= latest_by_tag[key][:2]:
            latest_by_tag[key] = (rank[0], rank[1], result)
    ordered = sorted(
        (item[2] for item in latest_by_tag.values()),
        key=lambda r: (str(r.get("tag_id") or ""), str(r.get("task_id") or "")),
    )
    return [_coverage_entry(result) for result in ordered]


def _collect_user_ids(coverage: list[dict[str, Any]]) -> list[str]:
    """合并所有带完整名单的标签会员，重复命中的只保留一次。"""
    users: dict[str, None] = {}
    for entry in coverage:
        if not entry["detail_complete"]:
            continue
        for user_id in entry["user_ids"]:
            users[user_id] = None
    return sorted(users)


def _snapshot_source_version(coverage: list[dict[str, Any]]) -> str | None:
    versions = sorted({e["source_version"] for e in coverage if e.get("source_version")})
    if not versions:
        return None
    return ",".join(versions)


def _coverage_source_versions(coverage: list[dict[str, Any]]) -> dict[str, str]:
    """标签（任务）到来源版本的映射。"""
    mapping: dict[str, str] = {}
    for entry in coverage:
        if not entry.get("source_version"):
            continue
        key = entry.get("task_id") or entry.get("tag_id") or ""
        mapping[str(key)] = str(entry["source_version"])
    return mapping


def create_snapshot(snapshot_id: str) -> dict[str, Any]:
    """创建标签快照，消费标签计算和分群结果。

    已生成的快照直接返回当时的产物，补算或重算标签任务都不会改写历史口径。
    """
    snapshot = store.find_by_field("tag_snapshot", "snapshot_id", snapshot_id)
    if snapshot is None:
        raise ValueError(f"Snapshot not found: {snapshot_id}")

    existing = store.get_artifact("tag_snapshot", snapshot_id)
    if existing is not None:
        return copy.deepcopy(existing)

    tags = store.rows("tag_define")
    segments = store.rows("user_segment")
    compute_results = store.list_artifacts("tag_compute")

    coverage = _build_coverage(compute_results)
    user_ids = _collect_user_ids(coverage)

    # 只有所有参与的任务都拿得到完整名单，整批用户基数才可确认。
    unconfirmed = [e for e in coverage if not e["detail_complete"]]
    base_confirmable = bool(coverage) and not unconfirmed

    source_version = _snapshot_source_version(coverage)
    source_versions = _coverage_source_versions(coverage)

    snapshot_data = {
        "snapshot_id": snapshot_id,
        "snapshot_date": snapshot["snapshot_date"],
        # 实际基数：各标签命中的会员合并去重后的数量。
        "user_count": len(user_ids),
        "user_ids": user_ids,
        "user_details": [
            {"user_id": user_id}
            for user_id in user_ids
        ],
        "base_confirmable": base_confirmable,
        "unconfirmed_tags": [
            {"task_id": e["task_id"], "tag_id": e["tag_id"], "result_count": e["result_count"]}
            for e in unconfirmed
        ],
        "coverage": coverage,
        "source_version": source_version,
        "source_versions": source_versions,
        "tag_count": len(tags),
        "segment_count": len(segments),
        "tags": [{"tag_id": t["tag_id"], "tag_name": t["tag_name"]} for t in tags],
        "segments": [
            {"segment_id": s["segment_id"], "segment_name": s["segment_name"]}
            for s in segments
        ],
        "compute_results": [
            {
                "task_id": e["task_id"],
                "tag_id": e["tag_id"],
                "result_count": e["result_count"],
                "compute_scope": e["compute_scope"],
                "source_version": e["source_version"],
                "has_user_detail": e["has_user_detail"],
                "detail_complete": e["detail_complete"],
            }
            for e in coverage
        ],
        "snapshot_path": snapshot["snapshot_path"],
        "storage_format": snapshot["storage_format"],
    }

    # 存储快照产物（深拷贝，后续产物或计算任务变化不会影响历史快照）。
    stored = copy.deepcopy(snapshot_data)
    store.set_artifact("tag_snapshot", snapshot_id, stored)
    store.add_snapshot("tag_snapshot", copy.deepcopy(stored))

    store.update("tag_snapshot", snapshot["id"], {
        "status": "已完成",
        "pending": False,
        "user_count": snapshot_data["user_count"],
        "tag_count": snapshot_data["tag_count"],
        "segment_count": snapshot_data["segment_count"],
        "base_confirmable": base_confirmable,
        "source_version": source_version,
    })

    return copy.deepcopy(snapshot_data)


def _frozen_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """把已发布的快照记录还原成只含当时信息的产物。

    这类快照没有落过标签明细，重开详情时不能拿当前的计算产物重新拼装，
    否则补算会把历史口径改掉。基数如实记为未知（0），并标记不可确认。
    """
    user_count = int(snapshot.get("user_count") or 0)
    return {
        "snapshot_id": snapshot["snapshot_id"],
        "snapshot_date": snapshot.get("snapshot_date"),
        "user_count": user_count,
        "user_ids": [],
        "user_details": [],
        "base_confirmable": bool(snapshot.get("base_confirmable")) if user_count else False,
        "unconfirmed_tags": [],
        "coverage": [],
        "source_version": snapshot.get("source_version"),
        "source_versions": {},
        "tag_count": snapshot.get("tag_count", 0),
        "segment_count": snapshot.get("segment_count", 0),
        "tags": [],
        "segments": [],
        "compute_results": [],
        "snapshot_path": snapshot.get("snapshot_path", ""),
        "storage_format": snapshot.get("storage_format", ""),
    }


def get_snapshot(snapshot_id: str) -> dict[str, Any] | None:
    """获取标签快照；已有产物不会被重新计算覆盖。"""
    artifact = store.get_artifact("tag_snapshot", snapshot_id)
    if artifact:
        return copy.deepcopy(artifact)

    row = store.find_by_field("tag_snapshot", "snapshot_id", snapshot_id)
    if row is None:
        return None

    # 已经发布过的快照没有产物时按记录冻结，避免用补算后的数据改写历史。
    if str(row.get("status", "")) in ("已完成", "已过期"):
        return _frozen_snapshot(row)

    try:
        return create_snapshot(snapshot_id)
    except ValueError:
        return None


def _snapshot_side(snapshot: dict[str, Any]) -> dict[str, Any]:
    """整理比较结果中单侧快照的来源信息。"""
    return {
        "snapshot_id": snapshot.get("snapshot_id"),
        "snapshot_date": snapshot.get("snapshot_date"),
        "source_version": snapshot.get("source_version"),
        "source_versions": copy.deepcopy(snapshot.get("source_versions", {})),
        "coverage": copy.deepcopy(snapshot.get("coverage", [])),
        "compute_tasks": [
            {
                "task_id": entry.get("task_id"),
                "tag_id": entry.get("tag_id"),
                "compute_scope": entry.get("compute_scope"),
                "source_version": entry.get("source_version"),
                "result_count": entry.get("result_count"),
                "detail_complete": entry.get("detail_complete"),
            }
            for entry in snapshot.get("coverage", [])
        ],
        "user_ids": list(snapshot.get("user_ids", [])),
        "user_details": copy.deepcopy(snapshot.get("user_details", [])),
        "user_count": snapshot.get("user_count", 0),
        "base_confirmable": bool(snapshot.get("base_confirmable")),
        "unconfirmed_tags": copy.deepcopy(snapshot.get("unconfirmed_tags", [])),
    }


def _tag_users(snapshot: dict[str, Any], tag_id: str) -> tuple[set[str], bool]:
    """取某标签在快照中的会员明细，以及该标签明细是否完整。"""
    users: set[str] = set()
    complete = True
    found = False
    for entry in snapshot.get("coverage", []):
        if entry.get("tag_id") != tag_id:
            continue
        found = True
        users.update(entry.get("user_ids", []))
        if not entry.get("detail_complete"):
            complete = False
    return users, complete and found


def compare_snapshots(snapshot_id_a: str, snapshot_id_b: str) -> dict[str, Any]:
    """对比两个快照的差异。

    只读取两份快照当时保存的覆盖范围、用户明细和计算任务，不会因为补算改变结果。
    """
    snap_a = get_snapshot(snapshot_id_a)
    snap_b = get_snapshot(snapshot_id_b)
    if snap_a is None or snap_b is None:
        raise ValueError("Snapshot not found")

    users_a = set(snap_a.get("user_ids", []))
    users_b = set(snap_b.get("user_ids", []))
    new_users = sorted(users_b - users_a)
    removed_users = sorted(users_a - users_b)

    base_confirmable_a = bool(snap_a.get("base_confirmable"))
    base_confirmable_b = bool(snap_b.get("base_confirmable"))
    new_count_confirmable = base_confirmable_a and base_confirmable_b

    # 按标签拆出可交付的新增会员：只有拿到完整名单的标签才谈得上交付。
    tag_diffs: list[dict[str, Any]] = []
    deliverable_users: set[str] = set()
    for entry in snap_b.get("coverage", []):
        tag_id = entry.get("tag_id")
        if not entry.get("detail_complete"):
            tag_diffs.append({
                "tag_id": tag_id,
                "task_id": entry.get("task_id"),
                "source_version": entry.get("source_version"),
                "detail_complete": False,
                "new_user_ids": [],
                "new_user_count": 0,
                "new_user_count_confirmable": False,
            })
            continue
        users_b_tag = set(entry.get("user_ids", []))
        users_a_tag, a_tag_complete = _tag_users(snap_a, tag_id)
        new_tag_users = sorted(users_b_tag - users_a_tag)
        # 只有旧快照里这个标签也有完整名单时，才能确认这些会员确实是新增的。
        if a_tag_complete:
            deliverable_users.update(new_tag_users)
        tag_diffs.append({
            "tag_id": tag_id,
            "task_id": entry.get("task_id"),
            "source_version": entry.get("source_version"),
            "detail_complete": True,
            "new_user_ids": new_tag_users,
            "new_user_count": len(new_tag_users),
            "new_user_count_confirmable": a_tag_complete,
        })

    # 可交付名单必须确实不在旧快照里，避免把换标签命中的老会员算成新增。
    deliverable_users = set(new_users) & deliverable_users

    side_a = _snapshot_side(snap_a)
    side_b = _snapshot_side(snap_b)

    return {
        "snapshot_a": snapshot_id_a,
        "snapshot_b": snapshot_id_b,
        # 两次快照各自使用的来源版本。
        "source_version_a": snap_a.get("source_version"),
        "source_version_b": snap_b.get("source_version"),
        "source_versions_a": copy.deepcopy(snap_a.get("source_versions", {})),
        "source_versions_b": copy.deepcopy(snap_b.get("source_versions", {})),
        "version_changed": snap_a.get("source_version") != snap_b.get("source_version"),
        # 覆盖范围、用户明细和对应的计算任务。
        "detail_a": side_a,
        "detail_b": side_b,
        "coverage_a": side_a["coverage"],
        "coverage_b": side_b["coverage"],
        "compute_tasks_a": side_a["compute_tasks"],
        "compute_tasks_b": side_b["compute_tasks"],
        "user_details_a": side_a["user_ids"],
        "user_details_b": side_b["user_ids"],
        "user_ids_a": side_a["user_ids"],
        "user_ids_b": side_b["user_ids"],
        # 新增会员：只来自有完整名单的标签，整批数量是否可确认单独说明。
        "new_users": new_users,
        "new_user_ids": new_users,
        "new_members": new_users,
        "new_user_count": len(new_users),
        "known_new_user_count": len(new_users),
        "deliverable_new_users": sorted(deliverable_users),
        "removed_users": removed_users,
        "removed_user_count": len(removed_users),
        "tag_diffs": tag_diffs,
        "base_confirmable_a": base_confirmable_a,
        "base_confirmable_b": base_confirmable_b,
        "new_user_count_confirmable": new_count_confirmable,
        "unconfirmed_tags": copy.deepcopy(snap_b.get("unconfirmed_tags", [])),
        "user_count_a": snap_a.get("user_count", 0),
        "user_count_b": snap_b.get("user_count", 0),
        "user_count_diff": snap_b.get("user_count", 0) - snap_a.get("user_count", 0),
        "tag_count_diff": snap_b.get("tag_count", 0) - snap_a.get("tag_count", 0),
        "segment_count_diff": snap_b.get("segment_count", 0) - snap_a.get("segment_count", 0),
        "date_a": snap_a.get("snapshot_date"),
        "date_b": snap_b.get("snapshot_date"),
    }


def list_snapshots() -> list[dict[str, Any]]:
    """列出所有标签快照。"""
    return copy.deepcopy(store.list_snapshots("tag_snapshot"))
