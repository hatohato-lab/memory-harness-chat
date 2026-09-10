"""Reviewable harness proposals; this module never changes the source project."""

from __future__ import annotations

import difflib
import json
import re
from pathlib import Path
from typing import Any

from .common import (
    HarnessError, atomic_write, canonical_hash, hash_file, read_json,
    safe_project_path, sha256_bytes, utc_now, write_json,
)

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}\Z")
_ACTIVE = {"claude_md", "rule", "skill", "hook_spec"}
_RELATIONS = {"duplicate", "conflict", "overlap", "supersedes"}


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _lines(value: Any) -> list[str]:
    return [v for v in value if isinstance(v, str) and v.strip()] if isinstance(value, list) else []


def _reference(candidate: dict) -> str:
    refs = []
    for evidence in candidate.get("evidence", []):
        # Source IDs, rather than filesystem paths or @imports, preserve provenance.
        sid = _text(evidence.get("source_id"))
        if not _ID.fullmatch(sid):
            continue
        start, end = evidence.get("start_line"), evidence.get("end_line")
        if isinstance(start, int) and isinstance(end, int):
            refs.append(f"{sid}（{start}–{end} 行）")
    return "、".join(dict.fromkeys(refs)) or "根拠情報は REVIEW.md と candidates.json を参照"


def _body(candidate: dict, *, skill: bool = False) -> str:
    title = _text(candidate.get("title")).replace("\n", " ").strip()
    condition = _text(candidate.get("condition")).strip()
    action = _text(candidate.get("action")).strip()
    rationale = _text(candidate.get("rationale")).strip()
    parts = [f"# {title}", "", f"適用条件: {condition or '指定なし。適用範囲を確認すること。'}", "", action]
    exceptions = _lines(candidate.get("exceptions"))
    if exceptions:
        parts.extend(["", "例外:", *[f"- {item}" for item in exceptions]])
    if skill:
        steps = _lines(candidate.get("steps")) or [action]
        parts.extend(["", "## 手順", "", *[f"{n}. {step}" for n, step in enumerate(steps, 1)]])
        parts.extend([
            "", "## 完了確認", "",
            "適用条件と例外を再確認し、上の手順が満たされたかを既存のテスト・評価基準で確認する。",
            "判定できない項目は、確認済みとせず未確認として報告する。",
        ])
    if rationale:
        parts.extend(["", f"理由: {rationale}"])
    parts.extend(["", f"根拠: {_reference(candidate)}", f"候補 ID: {candidate['id']}", ""])
    return "\n".join(parts)


def _validate_paths(value: Any) -> tuple[list[str], list[str], list[str]]:
    paths = _lines(value)
    blocked, warnings = [], []
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        blocked.append("paths は文字列の配列で指定する必要があります。")
    if not paths:
        blocked.append("パス限定ルールには空でない paths が必要です。常時必要なら claude_md を選んでください。")
    for glob in paths:
        if (len(glob) > 4096 or any(ord(char) < 32 for char in glob)
                or glob.startswith(("/", "\\", "!", "~")) or "\\" in glob
                or re.match(r"^[A-Za-z]:", glob)
                or any(part in (".", "..") for part in glob.split("/"))):
            blocked.append(f"安全なプロジェクト相対パターンではありません: {glob!r}")
        if glob in {"*", "**", "**/*", "**/**", "**/*.*"}:
            warnings.append(f"広範囲に一致する paths です（{glob}）。読み込み削減の効果を確認してください。")
        if "{" in glob:
            warnings.append(f"ブレース展開の一致範囲・展開数を確認してください: {glob}")
    return paths, blocked, warnings


def _item(candidate: dict) -> dict:
    cid, target = candidate["id"], candidate.get("target")
    blocked, warnings = [], _lines(candidate.get("issues"))
    relative, content = None, ""
    if target not in _ACTIVE | {"memory", "archive"}:
        blocked.append("不明な振り分け先です。")
    elif target == "claude_md":
        relative = "CLAUDE.md"
        body = _body(candidate).replace("# ", "## ", 1)
        content = f"<!-- memory-harness:{cid}:start -->\n{body}<!-- memory-harness:{cid}:end -->\n"
    elif target == "rule":
        relative = f".claude/rules/mh-{cid}.md"
        paths, invalid, caution = _validate_paths(candidate.get("paths"))
        blocked.extend(invalid)
        warnings.extend(caution)
        content = "---\npaths: " + json.dumps(paths, ensure_ascii=False) + "\n---\n\n" + _body(candidate)
    elif target == "skill":
        skill_name = "mh-" + cid.lower().replace("_", "-")
        relative = f".claude/skills/{skill_name}/SKILL.md"
        if len(skill_name) > 64 or "--" in skill_name or skill_name.endswith("-"):
            blocked.append("候補 ID から有効なスキル名を生成できません。ID を再生成してください。")
        description = " ".join(filter(None, [_text(candidate.get("title")), _text(candidate.get("condition"))]))
        content = ("---\nname: " + json.dumps(skill_name) + "\ndescription: "
                   + json.dumps(description, ensure_ascii=False) + "\n---\n\n" + _body(candidate, skill=True))
    elif target == "hook_spec":
        relative = f".memory-harness/hook-specs/mh-{cid}.md"
        content = ("# フック実装候補（未実装・未登録）\n\n"
                   "このファイルは実行されません。以下は実装前の仕様案です。\n"
                   "実装時には対象イベント、入力形式、遮断条件、許可条件、タイムアウト時の挙動、"
                   "誤遮断と見逃しの評価を具体化してください。\n\n" + _body(candidate))
        warnings.append("フック仕様の作成のみです。機械的な強制やフック登録は行いません。")
    else:
        warnings.append("メモリ・記録として保持する候補です。このツールは原文を移動・削除しません。")
    if target in _ACTIVE:
        if candidate.get("authority") == "external":
            blocked.append("外部資料の記述をそのまま運用指示に昇格できません。利用者の意思を確認して再抽出してください。")
        if not _text(candidate.get("action")).strip():
            blocked.append("行動指示が空です。")
        if not candidate.get("evidence"):
            blocked.append("出所を追跡できる根拠がありません。")
    return {
        "candidate_id": cid, "target": target, "relative_path": relative,
        "before_sha256": None, "content": content,
        "summary": _text(candidate.get("title")) or cid,
        "blocked_reasons": blocked, "warnings": warnings,
        "needs_review": bool(candidate.get("needs_review")) or bool(warnings and target == "rule"),
        "authority": candidate.get("authority"), "evidence": candidate.get("evidence", []),
    }


def _relation_records(relations: dict | None) -> list[dict]:
    if relations is None:
        return []
    records = relations.get("relations", [])
    if not isinstance(records, list):
        raise HarnessError("relations.json の relations は配列である必要があります。")
    for record in records:
        if not isinstance(record, dict) or record.get("relation") not in _RELATIONS:
            raise HarnessError("relations.json に不正な関係があります。")
    return records


def _read_base(project: Path, relative: str) -> dict:
    path = safe_project_path(project, relative)
    if not path.exists():
        return {"sha256": None, "content": ""}
    if not path.is_file():
        raise HarnessError(f"変更先は通常ファイルではありません: {relative}")
    data = path.read_bytes()
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HarnessError(f"変更先が UTF-8 ではありません: {relative}") from exc
    return {"sha256": sha256_bytes(data), "content": content}


def _verify_harness_inventory(project: Path, inventory: dict) -> None:
    """Reject analysis against a changed harness, including newly added files."""
    # Reuse collection's exact discovery scope, including errors and links.
    from .collect import _harness_sources

    recorded = {source["origin"]: source for source in inventory.get("sources", [])
                if source.get("kind") == "harness"}
    current = {str(source.path): source for source in _harness_sources(project)}
    added, removed = current.keys() - recorded.keys(), recorded.keys() - current.keys()
    if added or removed:
        details = []
        if added:
            details.append("追加: " + ", ".join(sorted(added)))
        if removed:
            details.append("削除: " + ", ".join(sorted(removed)))
        raise HarnessError("収集後に既存ハーネスの構成が変わりました。再収集・再分析してください。" + " / ".join(details))
    for origin, source in recorded.items():
        if source.get("status") not in {"ok", "empty"} or current[origin].status is not None:
            raise HarnessError(f"既存ハーネスを検証できません: {origin}。読取エラー等を解決して再収集してください。")
        try:
            relative = Path(origin).relative_to(project).as_posix()
            path = safe_project_path(project, relative)
            unchanged = hash_file(path) == source.get("sha256")
        except (OSError, ValueError, HarnessError) as exc:
            raise HarnessError(f"既存ハーネスを検証できません: {origin}。再収集してください。") from exc
        if not unchanged:
            raise HarnessError(f"収集後に既存ハーネスが更新されました: {origin}。再収集・再分析してください。")


def _merge_claude(before: str, fragments: list[str]) -> str:
    separator = ""
    if before:
        separator = "\n" if before.endswith("\n") else "\n\n"
    return before + separator + "\n".join(fragments)


def assemble_operations(plan: dict, ids: list[str]) -> list[dict]:
    """Bind an approved subset to immutable plan bytes, without filesystem reads."""
    if not ids or len(ids) != len(set(ids)):
        raise HarnessError("承認する候補 ID を重複なく明示してください。")
    items = {item["candidate_id"]: item for item in plan.get("items", [])}
    unknown = set(ids) - items.keys()
    if unknown:
        raise HarnessError("不明な候補 ID: " + ", ".join(sorted(unknown)))
    selected = set(ids)
    for cid in ids:
        item = items[cid]
        if item.get("blocked_reasons"):
            raise HarnessError(f"候補 {cid} は承認できません: " + " / ".join(item["blocked_reasons"]))
        if item.get("target") not in _ACTIVE or not item.get("relative_path"):
            raise HarnessError(f"候補 {cid} はハーネス変更の対象ではありません。")
    for relation in plan.get("relations", []):
        if (relation.get("relation") in {"duplicate", "conflict", "supersedes"}
                and relation.get("left_id") in selected and relation.get("right_id") in selected):
            raise HarnessError("重複・矛盾・置換関係のある候補は同時に承認できません: "
                               + f"{relation['left_id']} / {relation['right_id']}。片方を選択してください。")
    grouped: dict[str, list[dict]] = {}
    for item in plan.get("items", []):
        if item["candidate_id"] in selected:
            grouped.setdefault(item["relative_path"], []).append(item)
    operations = []
    for relative, group in grouped.items():
        base = plan.get("bases", {}).get(relative)
        if not isinstance(base, dict) or not isinstance(base.get("content"), str):
            raise HarnessError(f"計画に変更前の内容がありません: {relative}")
        expected_before = sha256_bytes(base["content"].encode("utf-8")) if base.get("sha256") is not None else None
        if expected_before != base.get("sha256") or any(item.get("before_sha256") != expected_before for item in group):
            raise HarnessError(f"計画の変更前ハッシュが一致しません: {relative}")
        if relative == "CLAUDE.md" and all(item["target"] == "claude_md" for item in group):
            after = _merge_claude(base["content"], [item["content"] for item in group])
        else:
            if len(group) != 1 or base["sha256"] is not None:
                raise HarnessError(f"既存ファイルを上書きする計画は承認できません: {relative}")
            after = group[0]["content"]
        operations.append({
            "relative_path": relative, "before_sha256": base["sha256"],
            "after_sha256": sha256_bytes(after.encode("utf-8")), "after_content": after,
            "candidate_ids": [item["candidate_id"] for item in group],
        })
    return operations


def _markdown_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _unified_diff(before: str, after: str, relative: str) -> str:
    lines = difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                fromfile=f"a/{relative}", tofile=f"b/{relative}")
    # difflib omits the conventional marker, otherwise a final unterminated
    # before-line and the following addition visually run together.
    return "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
                   for line in lines)


def _review(plan: dict) -> str:
    lines = [
        "# メモリからハーネスへの反映候補", "",
        "この計画はプロジェクトを変更していません。承認した候補だけを、別の apply 操作で反映します。",
        "原メモリは移動・削除しません。抽出は意味上の完全性を保証するものではありません。", "",
        f"対象プロジェクト: `{plan['project']}`", f"分析状態: {'入力の処理が完了' if plan['complete'] else '未完了の処理があります'}",
        f"候補数: {len(plan['items'])} / 既存指示として除外: {plan['excluded_reference_count']}", "",
        "## 確認手順", "",
        "1. 根拠が利用者の意図を表しているか、条件・例外・理由が保持されているかを確認します。",
        "2. 反映先と差分を確認し、採用する候補 ID を明示します。重複・矛盾の両方は採用できません。",
        "3. 要確認候補には判断理由を notes として記録します。未完了の分析には明示的な許可と理由が必要です。",
        "4. 承認後は approval.json の対象・本文・ハッシュを確認し、評価してから反映します。", "",
        "各 diff は候補を単独で反映した内容です。CLAUDE.md は承認した候補だけを既存本文の末尾へ追記します。",
        "フック候補は仕様書にとどまり、実行コードや settings.json を生成しません。", "",
        "| 候補 ID | 要約 | 反映先 | 状態 |", "|---|---|---|---|",
    ]
    for item in plan["items"]:
        status = "保留: 適用不可" if item["blocked_reasons"] else "記録のみ" if not item["relative_path"] else "理由付き確認が必要" if item["needs_review"] else "承認待ち"
        lines.append("| " + " | ".join(_markdown_cell(x) for x in [item["candidate_id"], item["summary"], item["relative_path"] or item["target"], status]) + " |")
    for item in plan["items"]:
        lines.extend(["", f"## {item['candidate_id']}: {_markdown_cell(item['summary'])}", ""])
        for reason in item["blocked_reasons"]:
            lines.append(f"- 適用不可: {reason}")
        for warning in item["warnings"]:
            lines.append(f"- 確認事項: {warning}")
        if item["relative_path"]:
            lines.extend([f"- 差分: [diffs/{item['candidate_id']}.diff](diffs/{item['candidate_id']}.diff)",
                          f"- 変更前 SHA-256: `{item['before_sha256'] or '新規ファイル'}`"])
        lines.append("- 根拠:")
        for evidence in item["evidence"]:
            lines.append(f"  - {_markdown_cell(evidence.get('source_id', '?'))}: {evidence.get('start_line')}–{evidence.get('end_line')} 行")
            for quote_line in _text(evidence.get("quote")).splitlines():
                lines.append(f"    > {quote_line}")
        if item["content"]:
            lines.extend(["", "提案本文:", "", *["    " + line for line in item["content"].splitlines()]])
    if plan["relations"]:
        lines.extend(["", "## 重複・矛盾・範囲の重なり", ""])
        for relation in plan["relations"]:
            lines.append(f"- {relation['left_id']} / {relation['right_id']}: {relation['relation']} — {relation.get('reason', '')}")
            if relation.get("example"):
                lines.append(f"  - 具体例: {relation['example']}")
    for warning in plan.get("warnings", []):
        lines.extend(["", f"注意: {warning}"])
    return "\n".join(lines) + "\n"


def make_plan(run_dir: Path) -> dict:
    run_dir = Path(run_dir)
    inventory = read_json(run_dir / "inventory.json")
    analysis = read_json(run_dir / "candidates.json")
    if analysis.get("inventory_sha256") != hash_file(run_dir / "inventory.json"):
        raise HarnessError("収集情報が分析時から変更されています。再分析してください。")
    project = Path(inventory["project"])
    if not project.is_absolute() or analysis.get("project") != str(project):
        raise HarnessError("収集結果と分析結果の対象プロジェクトが一致しません。")
    _verify_harness_inventory(project, inventory)
    analysis_hash = hash_file(run_dir / "candidates.json")
    relations_path = run_dir / "relations.json"
    relations = read_json(relations_path) if relations_path.exists() else None
    if relations is not None and relations.get("analysis_sha256") != analysis_hash:
        raise HarnessError("関係分析が現在の候補と一致しません。関係分析を再実行してください。")
    relation_records = _relation_records(relations)
    records = analysis.get("records", [])
    if not isinstance(records, list):
        raise HarnessError("候補は配列である必要があります。")
    ids = [record.get("id") for record in records]
    if any(not isinstance(cid, str) or not _ID.fullmatch(cid) for cid in ids) or len(ids) != len(set(ids)):
        raise HarnessError("候補 ID が不正、または重複しています。")
    references = {record["id"] for record in records if record.get("source_kind") == "harness"}
    items = [_item(record) for record in records if record["id"] not in references]
    by_id = {item["candidate_id"]: item for item in items}
    for relation in relation_records:
        left, right = relation.get("left_id"), relation.get("right_id")
        if left not in ids or right not in ids or left == right:
            raise HarnessError("関係分析が存在しない候補または同一候補を参照しています。")
        for cid, other in ((left, right), (right, left)):
            if cid not in by_id:
                continue
            item = by_id[cid]
            item["warnings"].append(f"{other} と {relation['relation']}: {relation.get('reason', '')}")
            if relation["relation"] == "overlap":
                item["needs_review"] = True
            if other in references and relation["relation"] in {"duplicate", "conflict", "supersedes"}:
                item["blocked_reasons"].append("既存のハーネス指示との重複・矛盾・置換関係を先に解決してください。既存指示は自動削除しません。")
    bases = {}
    for item in items:
        relative = item["relative_path"]
        if not relative:
            continue
        try:
            if relative not in bases:
                bases[relative] = _read_base(project, relative)
            base = bases[relative]
            item["before_sha256"] = base["sha256"]
            if item["target"] != "claude_md" and base["sha256"] is not None:
                item["blocked_reasons"].append("同名ファイルが既に存在します。既存ファイルは上書きしません。")
            if item["target"] == "claude_md" and f"<!-- memory-harness:{item['candidate_id']}:start -->" in base["content"]:
                item["blocked_reasons"].append("この候補は CLAUDE.md に既に反映されています。")
        except (OSError, HarnessError) as exc:
            item["blocked_reasons"].append(str(exc))
    warnings = []
    eligible_pairs_exist = any(
        left["id"] not in references or right["id"] not in references
        for n, left in enumerate(records) for right in records[n + 1:]
    )
    if relations is None and eligible_pairs_exist:
        warnings.append("意味照合未実施: 候補間の関係分析がありません。重複・矛盾がないとは判定していません。")
    if analysis.get("errors"):
        warnings.append("分析中のエラーが candidates.json に記録されています。")
    complete = bool(analysis.get("complete")) and not bool(analysis.get("errors"))
    if relations is None and eligible_pairs_exist:
        complete = False
    if relations is not None:
        complete = complete and bool(relations.get("complete")) and not bool(relations.get("errors"))
    plan = {
        "schema_version": 1, "project": str(project), "created_at": utc_now(),
        "inventory_sha256": hash_file(run_dir / "inventory.json"), "analysis_sha256": analysis_hash,
        "relations_sha256": hash_file(relations_path), "complete": complete,
        "items": items, "bases": bases, "relations": relation_records,
        "excluded_reference_count": len(references), "warnings": warnings,
    }
    diffs_dir = run_dir / "diffs"
    diffs_dir.mkdir(exist_ok=True)
    for item in items:
        relative = item["relative_path"]
        if not relative or relative not in bases:
            continue
        before = bases[relative]["content"]
        after = _merge_claude(before, [item["content"]]) if item["target"] == "claude_md" else item["content"]
        diff = _unified_diff(before, after, relative)
        atomic_write(diffs_dir / f"{item['candidate_id']}.diff", diff.encode("utf-8"))
    write_json(run_dir / "plan.json", plan)
    atomic_write(run_dir / "REVIEW.md", _review(plan).encode("utf-8"))
    return plan


def verify_plan_inputs(run_dir: Path, plan: dict) -> None:
    """Verify frozen analysis inputs and the complete current project harness."""
    run_dir = Path(run_dir)
    for filename, field in (("inventory.json", "inventory_sha256"), ("candidates.json", "analysis_sha256"), ("relations.json", "relations_sha256")):
        if filename != "relations.json" and not (run_dir / filename).is_file():
            raise HarnessError(f"必須の入力ファイルがありません: {filename}。再収集・再分析してください。")
        if hash_file(run_dir / filename) != plan.get(field):
            raise HarnessError(f"{filename} が計画作成後に変更されました。計画を再作成してください。")
    inventory = read_json(run_dir / "inventory.json")
    if inventory.get("project") != plan.get("project"):
        raise HarnessError("計画と収集結果のプロジェクトが一致しません。")
    _verify_harness_inventory(Path(plan["project"]), inventory)


def approve_plan(run_dir: Path, ids: list[str], allow_incomplete: bool = False, notes: str = "") -> dict:
    run_dir = Path(run_dir)
    plan = read_json(run_dir / "plan.json")
    verify_plan_inputs(run_dir, plan)
    if not plan.get("complete") and not (allow_incomplete and notes.strip()):
        raise HarnessError("分析が未完了です。継続する場合は allow_incomplete と判断理由の notes が必要です。")
    operations = assemble_operations(plan, ids)
    selected = set(ids)
    if any(item.get("needs_review") for item in plan["items"] if item["candidate_id"] in selected) and not notes.strip():
        raise HarnessError("要確認の候補を承認するには判断理由の notes が必要です。")
    project = Path(plan["project"])
    for operation in operations:
        path = safe_project_path(project, operation["relative_path"])
        if hash_file(path) != operation["before_sha256"]:
            raise HarnessError(f"計画後に変更先が更新されました: {operation['relative_path']}。計画を再作成してください。")
    approval = {
        "schema_version": 1, "project": plan["project"],
        "plan_sha256": hash_file(run_dir / "plan.json"), "approved_at": utc_now(),
        "candidate_ids": [item["candidate_id"] for item in plan["items"] if item["candidate_id"] in selected],
        "notes": notes, "operations": operations,
    }
    approval["approval_hash"] = canonical_hash(approval)
    write_json(run_dir / "approval.json", approval)
    return approval
