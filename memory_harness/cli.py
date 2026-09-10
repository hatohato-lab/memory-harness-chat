"""Thin command-line entry: one kernel step per command, no orchestration.

The chat runs the commands in order and writes the judgement files itself.
Nothing here starts another process, except the evaluator chosen for evaluate.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .analyze import (EXTRACT_INSTRUCTIONS, EXTRACT_SCHEMA, REVIEW_INSTRUCTIONS, REVIEW_SCHEMA, SYSTEM,
                      analyze, extract_requests, review, review_requests)
from .collect import collect
from .common import HarnessError, read_json, write_json
from .evaluate import evaluate
from .plan import approve_plan, make_plan
from .responses import ResponseFileProvider
from .transaction import apply_approval, rollback

STAGES = {"extract": (EXTRACT_SCHEMA, EXTRACT_INSTRUCTIONS), "review": (REVIEW_SCHEMA, REVIEW_INSTRUCTIONS)}


def manifest(run_dir: Path) -> dict:
    """List every collected input, so it can be shown before any text is read."""
    run_dir = Path(run_dir)
    inventory = read_json(run_dir / "inventory.json")
    characters: dict[str, int] = {}
    chunks: dict[str, int] = {}
    for chunk in inventory["chunks"]:
        sid = chunk["source_id"]
        characters[sid] = characters.get(sid, 0) + chunk["end_offset"] - chunk["start_offset"]
        chunks[sid] = chunks.get(sid, 0) + 1
    sources = [{"origin": s["origin"], "kind": s["kind"], "status": s["status"], "bytes": s["bytes"],
                "characters": characters.get(s["id"], 0), "chunks": chunks.get(s["id"], 0), "reason": s["reason"]}
               for s in inventory["sources"]]
    return {"run": str(run_dir.resolve()), "project": inventory["project"],
            "sources": sources, "counts": inventory["counts"]}


def write_requests(run_dir: Path, stage: str, max_pairs: int = 1000, batch_size: int = 8) -> dict:
    """Write requests/<stage>.json from the very (key, payload) pairs the kernel validates."""
    run_dir = Path(run_dir)
    schema, instructions = STAGES[stage]
    summary = {}
    if stage == "extract":
        items = [{"key": key, "payload": payload} for _chunk, _source, key, payload in extract_requests(run_dir)]
    else:
        pairs_total, batches = review_requests(run_dir, max_pairs, batch_size)
        items = [{"key": key, "payload": payload} for key, _ids, payload in batches]
        summary = {"pairs_total": pairs_total, "pairs_requested": sum(len(ids) for _key, ids, _payload in batches)}
    document = {"schema_version": 1, "stage": stage, "run": str(run_dir.resolve()),
                "response_file": f"responses/{stage}.json", "instructions": SYSTEM + instructions,
                "response_schema": schema, "items": items}
    path = run_dir / "requests" / f"{stage}.json"
    write_json(path, document)
    return {"run": document["run"], "stage": stage, "file": str(path.resolve()),
            "response_file": document["response_file"], "items": len(items), **summary}


def status(run_dir: Path) -> dict:
    run_dir = Path(run_dir)
    result = {"run": str(run_dir.resolve())}
    for name in ("inventory", "candidates", "relations", "plan", "approval", "evaluation", "transaction"):
        path = run_dir / f"{name}.json"
        if not path.exists():
            continue
        value = read_json(path)
        if name == "inventory":
            result[name] = value.get("counts", {})
        elif name == "candidates":
            result[name] = {"count": len(value["records"]), "complete": value["complete"], "errors": value["errors"],
                            "coverage": value["coverage"], "provider": value["provider"]}
        elif name == "relations":
            result[name] = {k: value.get(k) for k in ("complete", "pairs_total", "pairs_checked", "relations", "errors")}
        elif name == "plan":
            result[name] = {"complete": value["complete"], "items": [
                {k: item[k] for k in ("candidate_id", "target", "summary", "blocked_reasons")} for item in value["items"]]}
        elif name == "approval":
            result[name] = {"candidate_ids": value["candidate_ids"], "approved_at": value["approved_at"]}
        elif name == "evaluation":
            result[name] = {"complete": value["complete"], "summary": value.get("summary"), "kind": value["kind"]}
        else:
            result[name] = {"state": value["state"]}
    for folder in ("requests", "responses"):
        found = {stage: (run_dir / folder / f"{stage}.json").is_file() for stage in STAGES}
        if any(found.values()):
            result[folder] = found
    if len(result) == 1:
        raise HarnessError("指定runに処理記録がありません")
    return result


def _collect(args) -> dict:
    if not args.memory_dir and not args.source:
        raise HarnessError("--memory-dir または --source を明示してください。ホーム全体は自動走査しません")
    return collect(args.project, args.memory_dir, args.source, args.run,
                   chunk_chars=args.chunk_chars, max_file_bytes=args.max_file_bytes)


COMMANDS = {
    "collect": _collect,
    "manifest": lambda a: manifest(a.run),
    "requests": lambda a: write_requests(a.run, a.stage, a.max_pairs, a.review_batch_size),
    "validate": lambda a: analyze(a.run, ResponseFileProvider(Path(a.run) / "responses" / "extract.json")),
    "relations": lambda a: review(a.run, ResponseFileProvider(Path(a.run) / "responses" / "review.json"),
                                  a.max_pairs, a.review_batch_size),
    "plan": lambda a: make_plan(a.run),
    "approve": lambda a: approve_plan(a.run, a.ids, a.allow_incomplete, a.notes),
    "evaluate": lambda a: evaluate(a.run, a.cases, a.evaluator, a.repeats, a.timeout, a.max_copy_bytes),
    "apply": lambda a: apply_approval(a.run, a.ignore_evaluation, a.notes),
    "rollback": lambda a: rollback(a.run),
    "status": lambda a: status(a.run),
}


def _review_options(parser):
    parser.add_argument("--max-pairs", type=int, default=1000, help="照合するペア数の上限")
    parser.add_argument("--review-batch-size", type=int, default=8, help="1要求あたりのペア数")


def parser():
    p = argparse.ArgumentParser(
        description="メモリから根拠付きのハーネス候補を作り、承認・評価・適用・復元を検証するカーネル。"
                    "候補の抽出と照合はチャットのClaudeが行い、結果を requests/responses ファイルで受け渡す")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--json", action="store_true", help="結果全体をJSONで出力（サブコマンドの前に指定）")
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("collect", help="指定範囲を収集し、原文snapshotとchunkを保存する")
    c.add_argument("--project", type=Path, required=True)
    c.add_argument("--memory-dir", type=Path, action="append", default=[], help="対象memoryディレクトリ。複数指定可")
    c.add_argument("--source", type=Path, action="append", default=[], help="追加の会話・記録等のファイル/ディレクトリ。複数指定可")
    c.add_argument("--run", type=Path, required=True, help="新しい出力ディレクトリ。既存runは上書きしない")
    c.add_argument("--chunk-chars", type=int, default=8000)
    c.add_argument("--max-file-bytes", type=int, default=4*1024*1024)
    q = sub.add_parser("manifest", help="収集した入力の一覧を表示する。内容を読む前の確認用")
    q.add_argument("--run", type=Path, required=True)
    q = sub.add_parser("requests", help="チャットのClaudeへの要求ファイル requests/<stage>.json を書く")
    q.add_argument("--run", type=Path, required=True)
    q.add_argument("--stage", choices=tuple(STAGES), required=True)
    _review_options(q)
    q = sub.add_parser("validate", help="responses/extract.json を検査して candidates.json を作る")
    q.add_argument("--run", type=Path, required=True)
    q = sub.add_parser("relations", help="responses/review.json を検査して relations.json を作る")
    q.add_argument("--run", type=Path, required=True)
    _review_options(q)
    q = sub.add_parser("plan", help="候補から REVIEW.md と diffs/ を作る")
    q.add_argument("--run", type=Path, required=True)
    q = sub.add_parser("approve", help="確認した候補IDと判断理由を記録する。まだ適用しない")
    q.add_argument("--run", type=Path, required=True)
    q.add_argument("--ids", nargs="+", required=True)
    q.add_argument("--notes", default="")
    q.add_argument("--allow-incomplete", action="store_true")
    q = sub.add_parser("evaluate", help="適用前に、別コピーで現行と候補を比較する")
    q.add_argument("--run", type=Path, required=True)
    q.add_argument("--cases", type=Path, required=True)
    q.add_argument("--evaluator", type=Path, required=True)
    q.add_argument("--repeats", type=int, default=1)
    q.add_argument("--timeout", type=float, default=120)
    q.add_argument("--max-copy-bytes", type=int, default=100*1024*1024)
    q = sub.add_parser("apply", help="承認済みの差分を適用する")
    q.add_argument("--run", type=Path, required=True)
    q.add_argument("--ignore-evaluation", action="store_true", help="評価が候補を支持しない場合でも適用する（--notes で理由が必要）")
    q.add_argument("--notes", default="", help="--ignore-evaluation を使う理由")
    q = sub.add_parser("rollback", help="変更を復元する。後の編集があれば停止する")
    q.add_argument("--run", type=Path, required=True)
    q = sub.add_parser("status", help="各段階の状態と候補IDを表示する")
    q.add_argument("--run", type=Path, required=True)
    return p


def _display(command: str, result: dict, run_dir) -> None:
    if run_dir is not None:
        print(f"run: {Path(run_dir).resolve()}")
    if command == "collect":
        counts = result["counts"]
        print(f"収集: {counts['sources']} 件（ok {counts['ok']} / empty {counts['empty']} / error {counts['error']}"
              f" / excluded {counts['excluded']}）/ {counts['characters']:,} 文字 / {counts['chunks']} チャンク")
        print("manifest で一覧を確認してから requests --stage extract に進んでください。")
    elif command == "manifest":
        for source in result["sources"]:
            note = f"  ({source['reason']})" if source["reason"] else ""
            print(f"  {source['kind']:<8}{source['status']:<9}{source['bytes']:>10,} bytes"
                  f"{source['characters']:>10,} 文字{source['chunks']:>5} チャンク  {source['origin']}{note}")
        counts = result["counts"]
        print(f"合計 {counts['sources']} 件 / {counts['characters']:,} 文字 / {counts['chunks']} チャンク。"
              "内容を読んでよいか、この一覧で利用者の承諾を得てください。")
    elif command == "requests":
        print(f"要求ファイル: {result['file']}（{result['items']} 件）")
        if "pairs_total" in result:
            print(f"照合ペア: {result['pairs_requested']} / {result['pairs_total']}")
        print(f"応答は run 内の {result['response_file']} に書いてください。")
    elif command == "validate":
        print(f"抽出候補: {len(result['records'])} 件 / 処理状態: {'完了' if result['complete'] else '未完了あり'}")
        for error in result["errors"]:
            print(f"  extract:{error['chunk_id']}: {error['error']}")
    elif command == "relations":
        print(f"照合: {result['pairs_checked']}/{result['pairs_total']} ペア / 関係: {len(result['relations'])} 件"
              f" / 処理状態: {'完了' if result['complete'] else '未完了あり'}")
        for error in result["errors"]:
            print(f"  {error['key']}: {error['error']}")
    elif command == "plan":
        print(f"反映候補: {len(result['items'])} 件 / 処理状態: {'完了' if result['complete'] else '未完了あり'}")
        for item in result["items"]:
            print(f"  {item['candidate_id']}  {item['target']}  {item['summary']}" + (" [保留]" if item["blocked_reasons"] else ""))
        print("REVIEW.md と diffs/ を確認してください。元のprojectへの反映はまだ行っていません。")
    elif command == "approve":
        print(f"承認記録: {len(result['candidate_ids'])} 件。approval.jsonを作成しました。適用前にevaluateで比較できます。")
    elif command == "evaluate":
        summary = result.get("summary") or {}
        print(f"評価: {result['kind']} / 処理状態: {'完了' if result['complete'] else '未完了あり'}"
              f" / 対比 {summary.get('paired_runs', 0)} 件 / 平均delta {summary.get('mean_delta')}"
              f" / 退行 {summary.get('regressions', [])}")
    elif command in ("apply", "rollback"):
        print(f"状態: {result['state']}")
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    args = parser().parse_args(argv)
    try:
        result = COMMANDS[args.command](args)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            _display(args.command, result, getattr(args, "run", None))
        return 3 if result.get("complete") is False else 0
    except (HarnessError, OSError, KeyError, TypeError, ValueError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("中断しました。runの処理記録を確認し、同じコマンドで再開できます。", file=sys.stderr)
        return 130
