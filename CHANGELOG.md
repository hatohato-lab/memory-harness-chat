# 変更履歴

## 0.1.0

memory-harness の検証カーネルを、Claude Code のチャットから使う形に組み替えた初版。

- **由来。** 前身の memory-harness は、Python から Claude Code CLI を外部起動して候補を抽出していた。本版は同じ検証カーネル（原文の凍結と引用の照合、計画と承認の hash 結合、盲検の比較評価、hash 検証つきの適用と復元）を残し、判断の部分をチャットの Claude に移した。
- **外部 CLI 起動と金額計上の廃止。** `claude.py`（CLI 呼出し・記帳・キャッシュ）、コマンド `run`・`doctor`・`demo`、モデル名・実行ファイル・呼出回数・金額上限・送信確認の各オプションを削除した。サブスクリプション前提で、金額に関する概念をコード・文書・記録から取り除いた。
- **requests / responses 方式。** `requests` が抽出・照合の依頼書（`requests/extract.json`・`requests/review.json`）を書き、チャットの Claude がそれを読んで応答（`responses/extract.json`・`responses/review.json`）を書く。`validate` と `relations` が応答を読み、既存の検査（引用の照合・schema との整合・ペアの網羅）をそのまま適用する。依頼書の items は、検証側が provider に渡す (key, payload, schema) と同じ関数から生成し、両者の一致を保証する。
- **manifest による読込前の確認。** `manifest` が収集した入力の一覧（origin・bytes・文字数・チャンク数）を表示する。スキルは、この一覧を利用者に見せて承諾を得るまで中身を読まない。
- **継承。** 盲検の比較評価（評価器には baseline / candidate を伏せた不透明な識別子だけを渡す）と、評価の関門（悪化・退行・未完了・古い評価のいずれかで `apply` が拒否する）は前身からそのまま引き継いだ。
- **検証カーネルの補強。** Windows のジャンクション検査を `common.is_link` に共通化し、`Path.is_junction` の無い Python 3.11 でも収集・評価コピー・パス検査の全てで効くようにした。評価器の出力（標準出力と標準エラーの合計）に 16 MiB の上限を設け、超えた回はプロセスを止めて失敗として記録する。`.memory-harness/` 配下の過去 run（snapshot・inventory）を `--source` で再収集しない。評価器へ渡す課題 JSON を ASCII エスケープにし、Windows の cp932 でも日本語の課題文が化けないようにした。いずれも回帰テストを添えた。
- **テスト。** `test_cli.py`（入力なしの collect の終了コード、依頼書と検証呼出しの一致、応答なしの validate・relations、status）と `test_workflow.py`（架空メモリ 3 件を collect から rollback まで CLI 経由で通し、原本のバイト一致を確認）を追加し、前身の CLI 依存テストを削除した。
