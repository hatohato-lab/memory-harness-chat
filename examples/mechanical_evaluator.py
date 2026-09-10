"""Mechanical inspection of generated harness files, for the evaluate step.

Reads the case on stdin, inspects the project copy it is started in, and prints
the score/checks/feedback object. The score only counts expected files and
preserved wording; it does not measure how Claude behaves with those files.
The expected CLAUDE.md text is the one used by tests/test_workflow.py.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ORIGINAL_CLAUDE_MD = "# 既存の方針\n説明は日本語で書く。\n".encode("utf-8")


def main() -> None:
    # The request is consumed so the caller's stdin write always completes.
    json.load(sys.stdin)
    root = Path.cwd()
    rules = sorted((root / ".claude/rules").glob("mh-*.md"))
    skills = sorted((root / ".claude/skills").glob("*/SKILL.md"))
    rule_text = "\n".join(path.read_text(encoding="utf-8") for path in rules)
    claude_md = root / "CLAUDE.md"
    checks = {
        "scoped_rule_exists": "blog/**/*.md" in rule_text,
        "exception_preserved": "文字列リテラル" in rule_text,
        "skill_exists": bool(skills),
        "original_preserved": claude_md.is_file() and claude_md.read_bytes() == ORIGINAL_CLAUDE_MD,
    }
    # ASCII-only output stays decodable whatever code page the console uses.
    print(json.dumps({"score": sum(checks.values()) / len(checks), "checks": checks,
                      "feedback": "生成ファイルの機械的な検査。Claudeの行動改善を測った点数ではありません。"}))


if __name__ == "__main__":
    main()
