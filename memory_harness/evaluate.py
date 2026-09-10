from __future__ import annotations

import json
import math
import os
import shutil
import stat
import statistics
import subprocess
import tempfile
import uuid
from pathlib import Path

from .common import RUN_DIR_NAME, HarnessError, atomic_write, canonical_hash, hash_file, is_link, read_json, safe_project_path, sha256_bytes, utc_now, write_json
from .process import OutputLimitExceeded, run_process


def _copy_project(source: Path, destination: Path, max_bytes: int):
    excluded = {".git", ".venv", "venv", "node_modules", "__pycache__", RUN_DIR_NAME}
    copied = 0; count = 0; omitted = []
    destination.mkdir()
    for root, dirs, files in os.walk(source, followlinks=False):
        root = Path(root)
        keep = []
        for name in dirs:
            p = root / name
            if name in excluded:
                omitted.append(str(p.relative_to(source)))
            elif is_link(p.lstat()):
                # os.walk descends into junctions even with followlinks=False.
                raise HarnessError(f"評価コピー内にリンクがあります: {p.relative_to(source)}")
            else:
                keep.append(name)
        dirs[:] = keep
        for name in files:
            p = root / name
            info = p.lstat()
            if is_link(info) or not stat.S_ISREG(info.st_mode):
                raise HarnessError(f"評価コピー内に通常ファイル以外があります: {p.relative_to(source)}")
            size = info.st_size
            copied += size; count += 1
            if copied > max_bytes:
                raise HarnessError("評価用コピーの容量上限を超えました。対象projectを絞るか上限を明示してください")
            out = destination / p.relative_to(source)
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, out)
    return {"bytes": copied, "files": count, "excluded_directories": omitted}


def evaluate(run_dir: Path, cases_path: Path, evaluator_path: Path, repeats=1,
             timeout=120, max_copy_bytes=100*1024*1024) -> dict:
    """Run a user-supplied evaluator against fresh paired project copies.

    This is a filesystem copy, not a container/OS sandbox. The evaluator is
    explicitly selected local code and is responsible for its external effects.
    """
    run_dir = Path(run_dir).resolve()
    if repeats < 1 or timeout <= 0 or max_copy_bytes <= 0:
        raise HarnessError("repeats・timeout・max_copy_bytesは正の値が必要です")
    approval = read_json(run_dir / "approval.json")
    body = {k: v for k, v in approval.items() if k != "approval_hash"}
    if approval.get("approval_hash") != canonical_hash(body):
        raise HarnessError("承認内容のハッシュが一致しません")
    if approval["plan_sha256"] != hash_file(run_dir / "plan.json"):
        raise HarnessError("計画が承認後に変更されています")
    from .plan import assemble_operations, verify_plan_inputs
    plan = read_json(run_dir / "plan.json")
    if (plan["project"] != approval["project"] or
            approval["operations"] != assemble_operations(plan, approval["candidate_ids"])):
        raise HarnessError("承認された変更が計画と一致しません")
    verify_plan_inputs(run_dir, plan)
    project = Path(approval["project"]).resolve(strict=True)
    for op in approval["operations"]:
        dest = safe_project_path(project, op["relative_path"])
        if hash_file(dest) != op["before_sha256"]:
            raise HarnessError("評価の基準状態が変わっています。適用前に評価するか、新しいrunで収集してください")
        if not isinstance(op["after_content"], str):
            raise HarnessError("変更本文は文字列である必要があります")
        if sha256_bytes(op["after_content"].encode("utf-8")) != op["after_sha256"]:
            raise HarnessError("変更本文のハッシュが一致しません")
    config = read_json(evaluator_path)
    command = config.get("argv")
    if not isinstance(command, list) or not command or any(not isinstance(a, str) or not a for a in command):
        raise HarnessError("evaluatorには空でないargv配列が必要です")
    # Resolve only interpreter/executable. Script paths should be absolute or use
    # {config_dir}; command placeholders never pass through a shell.
    config_dir = Path(evaluator_path).resolve().parent
    command = [a.replace("{config_dir}", str(config_dir)) for a in command]
    executable = shutil.which(command[0])
    if not executable:
        raise HarnessError(f"評価プログラムが見つかりません: {command[0]}")
    command[0] = executable
    cases_document = read_json(cases_path)
    cases = cases_document.get("cases") if isinstance(cases_document, dict) else cases_document
    if not isinstance(cases, list) or not cases or any(not isinstance(c, dict) for c in cases):
        raise HarnessError("casesは1件以上のJSONオブジェクト配列です")
    case_ids = [c.get("id") for c in cases]
    if any(not isinstance(c, str) or not c for c in case_ids) or len(set(case_ids)) != len(case_ids):
        raise HarnessError("評価課題には重複しない文字列idが必要です")
    report = {"schema_version": 1, "created_at": utc_now(), "approval_hash": approval["approval_hash"],
              "cases_sha256": hash_file(cases_path), "evaluator_sha256": hash_file(evaluator_path),
              "repeats": repeats, "rows": [], "complete": False,
              "kind": config.get("kind", "external-evaluator"),
              "limitation": "評価器の出力を記録します。点数がClaudeの行動を測るかは評価器に依存します。別コピーはOSの隔離ではありません。"}
    with tempfile.TemporaryDirectory(prefix="memory-harness-eval-") as temporary:
        base = Path(temporary) / "seed"
        report["copy"] = _copy_project(project, base, max_copy_bytes)
        # Ensure the copied baseline is the exact planned before-state.
        for op in approval["operations"]:
            if hash_file(safe_project_path(base, op["relative_path"])) != op["before_sha256"]:
                raise HarnessError("コピー中に基準ファイルが変更されました")
        for index, case in enumerate(cases):
            for repeat in range(repeats):
                row = {"case_id": case["id"], "repeat": repeat+1}
                # Alternate order to reduce simple time/order bias.
                order = ["baseline", "candidate"] if (index+repeat) % 2 == 0 else ["candidate", "baseline"]
                # Blind the evaluator: an evaluator that can tell the arms apart
                # can manufacture a delta from the label alone, so neither the
                # working directory, the argv, nor the stdin payload names them.
                labels = {arm: uuid.uuid4().hex[:12] for arm in order}
                for arm in order:
                    label = labels[arm]
                    cwd = Path(temporary) / f"case-{index}-repeat-{repeat}-{label}"
                    shutil.copytree(base, cwd)
                    if arm == "candidate":
                        for op in approval["operations"]:
                            atomic_write(safe_project_path(cwd, op["relative_path"]), op["after_content"].encode("utf-8"))
                    argv = [a.replace("{project}", str(cwd)).replace("{arm}", label) for a in command]
                    try:
                        # ASCII-escaped JSON reads correctly whatever locale the
                        # evaluator decodes stdin with (cp932 on Japanese Windows).
                        proc = run_process(argv, input=json.dumps({"case": case, "arm": label}),
                                              cwd=cwd, text=True, encoding="utf-8", capture_output=True,
                                              timeout=timeout, shell=False)
                        if proc.returncode:
                            raise HarnessError(f"評価器の終了コード: {proc.returncode}")
                        outcome = json.loads(proc.stdout)
                        if not isinstance(outcome, dict):
                            raise HarnessError("評価器はJSONオブジェクトを返してください")
                        score = outcome.get("score")
                        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
                            raise HarnessError("scoreは有限の数値が必要です")
                        checks = outcome.get("checks", {})
                        if not isinstance(checks, dict) or any(type(v) is not bool for v in checks.values()):
                            raise HarnessError("checksは検査名からboolへの辞書です")
                        feedback = outcome.get("feedback", "")
                        if not isinstance(feedback, str):
                            raise HarnessError("feedbackは文字列です")
                        row[arm] = {"status": "ok", "score": score, "checks": checks, "feedback": feedback}
                    except (OSError, subprocess.TimeoutExpired, OutputLimitExceeded, ValueError, HarnessError) as exc:
                        row[arm] = {"status": "error", "error": str(exc)}
                    finally:
                        shutil.rmtree(cwd)
                if all(row[a]["status"] == "ok" for a in ("baseline", "candidate")):
                    row["delta"] = row["candidate"]["score"] - row["baseline"]["score"]
                    row["check_regressions"] = [k for k, v in row["baseline"]["checks"].items()
                                                if v and not row["candidate"]["checks"].get(k, False)]
                report["rows"].append(row)
                write_json(run_dir / "evaluation.json", report)
    valid = [r for r in report["rows"] if "delta" in r]
    report["complete"] = len(valid) == len(cases)*repeats
    if valid:
        report["summary"] = {"paired_runs": len(valid),
                             "baseline_mean": statistics.mean(r["baseline"]["score"] for r in valid),
                             "candidate_mean": statistics.mean(r["candidate"]["score"] for r in valid),
                             "mean_delta": statistics.mean(r["delta"] for r in valid),
                             "delta_stdev": statistics.stdev(r["delta"] for r in valid) if len(valid)>1 else None,
                             "regressions": [r["case_id"] for r in valid if r["delta"] < 0 or r["check_regressions"]]}
    write_json(run_dir / "evaluation.json", report)
    return report
