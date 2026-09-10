# memory-harness-chat

Claude Code のチャットから、メモリや会話メモに残った指示を「出典・条件・例外・理由を持つ候補」として取り出し、確認のうえ CLAUDE.md・rules・skills へ可逆的に反映するツール。

**判断はチャットの Claude が行い、検証は Python が行う。** 候補の抽出と、候補同士の重複・矛盾の照合は、チャットの Claude がその場で読んで JSON ファイルに書く。Python は検証カーネルだけを担う。原文の凍結と引用の照合、計画と承認の hash 結合、盲検の比較評価、hash 検証つきの適用と復元である。

## 前提

| 項目 | 内容 |
| --- | --- |
| 使う場所 | Claude Code の VS Code 拡張機能のチャット。スキル `memory-harness` 1つで最後まで動く |
| 契約 | サブスクリプション前提。金額に関する概念・オプション・記録を持たない |
| 外部起動 | 外部から Claude を起動しない。CLI の構造化出力などの機能に依存しない |
| 実行環境 | Python 3.11 以上。追加パッケージなし。Windows / Python 3.13 で検証（Linux / Python 3.12 でも確認） |

## 使い方

### チャットから使う

対象プロジェクトを開いた Claude Code のチャットで「メモリを整理して」と言う。スキル `memory-harness`（`.claude/skills/memory-harness/SKILL.md`）が次の順で進める。

1. 入力（メモリのフォルダやメモ）を確認して収集する。
2. 読み込む入力の一覧を見せ、中身を読む承諾を求める。承諾前に中身は読まない。
3. 候補を抽出して応答ファイルに書き、検証を通す。
4. 候補同士の重複・矛盾を照合し、反映案（REVIEW.md と差分）を作る。
5. 利用者が選んだ候補 ID と理由で承認し、承認後に適用する。問題があれば復元する。

### 手動で回す

`run_tool.py` のあるフォルダで実行する。`python` が使えない環境では `python3` または `py -3` に読み替える。`--run` には存在しない新しいフォルダを指定する。メモリの場所は、対象プロジェクトの Claude Code で `/memory` を実行して確認する。ホームや他プロジェクトの自動一括探索は行わない。

```console
python run_tool.py collect --project "C:/work/my-project" --memory-dir "C:/work/my-project-memory" --run "C:/work/mh-run-01"
python run_tool.py manifest --run "C:/work/mh-run-01"
python run_tool.py requests --run "C:/work/mh-run-01" --stage extract
# チャットの Claude が requests/extract.json を読み、responses/extract.json を書く
python run_tool.py validate --run "C:/work/mh-run-01"
python run_tool.py requests --run "C:/work/mh-run-01" --stage review
# チャットの Claude が requests/review.json を読み、responses/review.json を書く
python run_tool.py relations --run "C:/work/mh-run-01"
python run_tool.py plan --run "C:/work/mh-run-01"
python run_tool.py status --run "C:/work/mh-run-01"
python run_tool.py approve --run "C:/work/mh-run-01" --ids c_0123456789abcdef --notes "原文と照合し、このプロジェクトの継続方針として採用する"
python run_tool.py evaluate --run "C:/work/mh-run-01" --cases examples/cases.json --evaluator examples/mechanical-evaluator.json
python run_tool.py apply --run "C:/work/mh-run-01"
python run_tool.py rollback --run "C:/work/mh-run-01"
```

**ID は例である。** 実際に表示された候補 ID へ置き換える。複数件は `--ids ID1 ID2` と指定する。メモリ由来の候補は利用者の意図を確認する必要があるため、判断理由 `--notes` を記録する。承認時もプロジェクトはまだ変更されない。

メモリが複数なら `--memory-dir` を繰り返せる。過去会話の JSONL や評価失敗の記録は `--source "path/to/file.jsonl"` で追加できる。ディレクトリ指定では .md/.txt/.json/.jsonl を対象にする。保存・処理するデータは指定した範囲と、対象プロジェクトの既存ハーネス（CLAUDE.md・.claude/rules・.claude/skills）である。指定した範囲の中でも `.memory-harness/`（過去の run の snapshot・inventory・hook 仕様案）は収集しない。

### コマンド一覧

`python run_tool.py [--json] <command> ...`。`--json` をサブコマンドより前に置くと、結果全体を JSON で出力する。

| コマンド | 引数 | 出力 |
| --- | --- | --- |
| collect | `--project P`, `--memory-dir D`（複数可）, `--source S`（複数可）, `--run R`, `[--chunk-chars 8000]`, `[--max-file-bytes 4194304]` | inventory.json, snapshots/, chunks/。`--memory-dir` も `--source` も無ければ終了 2 で run を作らない |
| manifest | `--run R` | 収集した入力の一覧（origin・bytes・文字数・チャンク数）を表示。Claude が中身を読む前に利用者へ見せるためのもの |
| requests | `--run R`, `--stage extract` または `--stage review`, `[--max-pairs 1000]`, `[--review-batch-size 8]`（review のみ） | R/requests/extract.json または R/requests/review.json |
| validate | `--run R` | R/responses/extract.json を読んで検証し candidates.json を書く。応答の無い key はそのチャンクの error として記録し complete=false（終了 3） |
| relations | `--run R`, `[--max-pairs 1000]`, `[--review-batch-size 8]` | R/responses/review.json を読んで検証し relations.json を書く |
| plan | `--run R` | REVIEW.md, diffs/, plan.json |
| approve | `--run R`, `--ids ID...`（必須）, `[--notes ""]`, `[--allow-incomplete]` | approval.json |
| evaluate | `--run R`, `--cases C`, `--evaluator E`, `[--repeats 1]`, `[--timeout 120]`, `[--max-copy-bytes 104857600]` | evaluation.json |
| apply | `--run R`, `[--ignore-evaluation]`, `[--notes ""]` | transaction.json |
| rollback | `--run R` | transaction.json |
| status | `--run R` | 各段階の状態と候補 ID |

終了コードは 0 = 完了、2 = 入力・整合性・操作エラー、3 = 未完了（complete が false）、130 = 中断である。

`requests --stage review` と `relations` には同じ `--max-pairs` と `--review-batch-size` を渡す。値が違うとペアの組み方と応答の key が一致しない。

## responses の書き方

### 依頼書（requests）

`requests` は、検証側が必要とする入力をそのまま依頼書に書き出す。`items` は `validate`／`relations` が内部で provider に渡す (key, payload, schema) と同じ関数から生成されるため、依頼書と検証の対象が食い違うことはない。

```json
{
  "schema_version": 1,
  "stage": "extract",
  "run": "<run の絶対パス>",
  "response_file": "responses/extract.json",
  "instructions": "<共通指示 + 段階別の指示>",
  "response_schema": { ...JSON Schema（EXTRACT_SCHEMA または REVIEW_SCHEMA）... },
  "items": [
    {"key": "extract:<chunk_id>", "payload": { ...検証側が provider に渡す payload と完全に同一... }}
  ]
}
```

| stage | payload の内容 |
| --- | --- |
| extract | `task`, `source_id`, `source_kind`, `line_range`, `source_data`（チャンク本文）, `instructions` |
| review | `task`, `pairs`（`{left: <候補>, right: <候補>}` の配列）, `instructions` |

### 応答（responses）

チャットの Claude は、依頼書の `instructions` と `response_schema` に従い、item ごとの応答を `responses` に key で対応づけて書く。

```json
{"schema_version": 1, "stage": "extract", "responses": {"extract:<chunk_id>": { ...response_schema に従う JSON... }}}
```

- key は item の `key` をそのまま使う。全 item に応答を書く。応答の無い key は、そのチャンクの error として記録され complete=false（終了 3）になる。
- 応答の中身の検査（引用の照合・schema との整合・ペアの網羅）は、検証側の既存の検査がそのまま適用される。通らなかった key だけ直して再実行すればよい。
- extract の要点: `quote` は原文の文字列をそのまま写す。行番号は item の `line_range` の内側。根拠のない候補を作らない。過去の事実は `memory`、既存ハーネス由来は `reference`。一回限りの作業指示は候補にしない。
- review の要点: 全ペアに 1 件ずつ判定を書く（`none` を含む）。`conflict` には両立できない具体例が必須。`preferred_id` はそのペアの ID か null。
- 応答ファイルは run に残る。判断の根拠を後から追うための記録でもある。

## どこに出力するか

| 候補の種類 | 反映先・扱い |
| --- | --- |
| プロジェクト全体の方針 | CLAUDE.md の末尾に ID 付きの節を追加。既存本文のバイト列を保持 |
| 特定のファイル群で使う規約 | .claude/rules/mh-*.md。paths frontmatter を生成 |
| 作業の手順 | .claude/skills/mh-*/SKILL.md。呼出条件、手順、例外、確認方法を生成 |
| hook 向きの機械的な要件 | .memory-harness/hook-specs/ へ**仕様案**を保存。実行コードや hook 登録は行わない |
| 事実・背景・一時記録 | 候補台帳に残し、元メモリを維持 |
| 古い情報・退避候補 | 退避の提案まで。元ファイルを自動削除しない |

利用者全体の `~/.claude/CLAUDE.md` を自動変更する機能はない。対象は一つのプロジェクトである。`.claude/settings.json`、親ディレクトリや組織の管理方針を含めた全ハーネスの移行ツールではない。

## 抽出と照合のしくみ

1. Python がファイルを列挙し、UTF-8 の原文と hash を保存する。長い単一行も分割し、全ての文字に対応するチャンクを作る。
2. `requests` が、チャンク本文・JSON Schema・指示をまとめた依頼書を書く。チャットの Claude がそれを読み、発言の出所、条件、行動、例外、理由、確信度、根拠を分けた候補を応答ファイルに書く。
3. `validate` が引用と行番号を原文に照合する。存在しない引用や範囲外の出典は受理しない。
4. 候補同士と既存指示をペアで照合する（依頼書 → 応答 → `relations`）。重複と矛盾を区別し、矛盾には両立しない具体例を求める。元からある指示の参照記録は再適用しない。
5. `plan` が配置ファイルと差分を生成する。利用者が選んだ候補 ID とその時点の計画の hash を承認記録へ結び付ける。

同じ内容の候補は安定 ID で束ね、複数の出典を保持する。同じ経験の繰り返し記録から、独立した成功回数を推定することはしない。confidence は判断側の申告値で、採用に必要な成功率や較正済み確率ではない。

**処理完了は「意味を全部取り出せた」という保証ではない。** 読めないファイル、サイズ上限、応答の欠落や形式違反、照合上限は未完了として表示される。通常はそれらを解消して再実行する。範囲を理解して一部だけ進める場合には、`approve --allow-incomplete --notes "未処理範囲と採用理由"` を明示できる。外来情報の規範化や、選んだ候補間の未解決矛盾まで解除する指定ではない。

既存指示と候補が矛盾・重複する場合、元の指示を自動削除・置換せず保留する。先に正式な方針を整理してから再収集する。元のハーネスが収集後に追加・変更・削除された場合にも、古い分析を使わず再収集を要求する。

## 保存するものと復元の境界

run には入力一覧・原文 snapshot・チャンク・依頼書・応答・候補・引用・照合・計画・承認・評価・バックアップ・適用記録を保存する。生成した指示の根拠 ID は、その run の `inventory.json` と `candidates.json` で原文へ対応する。来歴を保つため run 一式を保存する。原文を含むため、共有するときは内容を確認する。

本ツールが外部へ送信するものは無い。チャットの Claude が読むのは依頼書の中身であり、それは利用者が `manifest` の一覧で承諾した入力に限られる。

適用はファイルごとの一時書込＋置換である。ツール同士はプロジェクトロックで排他し、全対象の hash を確認してから進める。途中停止の記録がある場合、変更前・変更後のどちらかに完全一致するファイルだけを回復できる。外部エディターとの同時変更や、ファイルシステムの停電時耐久性まで保証するトランザクションではない。

ファイルを戻しても、進行中のチャットの文脈や外部作用は戻らない。適用・復元後の動作確認は新しいセッションで行う。

## 評価して適用する

承認済みの候補は、適用前に現行版との比較評価ができる。後述の任意の評価器を指定する。

```console
python run_tool.py evaluate --run "C:/work/mh-run-01" --cases examples/cases.json --evaluator examples/mechanical-evaluator.json --repeats 3 --timeout 180
```

`examples/` の評価器は、生成ファイルを機械的に検査する見本である。入出力の形を示すためのもので、Claude の行動改善を測るものではない。サンプル評価器の `argv` の `python` は、使用環境の Python コマンドまたは絶対パスへ変更する。

`apply` は承認済みの差分だけを反映する。`rollback` はその変更を戻す。後から人が編集したファイルは上書きせず停止するので、差分を確認する。自動的な三方向マージは範囲外である。

**評価は適用の関門である。** `evaluate` を実行していれば、`apply` はその結果を読み、候補が baseline より悪ければ適用を拒否する。止まるのは、平均スコアが下がった場合、baseline で通っていた検査が候補で落ちた場合、評価が完了していない場合、そして評価が別の承認に対するものだった場合である。評価を実行していなければこの検査は行われず、そのまま適用できる（評価自体は任意のまま）。

判断のうえで適用したい場合は `--ignore-evaluation` に理由を添える。理由は `transaction.json` に記録される。

```console
python run_tool.py apply --run "C:/work/mh-run-01" --ignore-evaluation --notes "退行は無関係な課題によるものと確認した"
```

適用済みで正常な同じ run の再適用は重複追記しない。復元済み run を再び適用する場合は、新しい run を作成する。評価は適用前の状態から実行する。

## 評価器を接続する

`evaluate` はプロジェクトの別コピーを毎回作り、同じ課題を baseline と candidate に渡す。元のプロジェクトには適用しない。`.git`、仮想環境、node_modules、キャッシュ、`.memory-harness` はコピー対象外である。その他のシンボリックリンク（Windows のジャンクションを含む）は拒否する。既定のコピー容量は 100 MiB である。

評価器の設定例:

```json
{"argv": ["python", "{config_dir}/my_evaluator.py"], "kind": "my-behavior-eval"}
```

評価器の標準入力:

```json
{"case": {"id": "regression-01", "prompt": "評価する課題"}, "arm": "3f9c1a7e04b2"}
```

非 ASCII 文字は `\uXXXX` にエスケープして渡す。評価器の標準入力の文字コードが何であっても、JSON として読めば元の文字に戻る。

標準出力は JSON 一つ（ログは標準エラーへ）:

```json
{"score": 0.8, "checks": {"scope_correct": true, "no_regression": true}, "feedback": "失敗内容や観測した応答"}
```

`arm` は**その回だけの不透明な識別子**で、baseline と candidate のどちらかは分からない。作業ディレクトリ名も同じ識別子を使う。評価器がどちらの側かを知ると、ラベルだけで点差を作れてしまい、観測した差が改善によるものか判別できなくなるためである。評価器は渡されたプロジェクトコピーの内容そのものを見て採点する。同じ case の 2 回の実行を対応づけたい場合にだけ `arm` を使える。

作業ディレクトリはその回のプロジェクトコピーである。引数内で `{project}`、`{arm}`、`{config_dir}` を使える。shell 文字列は実行せず、argv 配列で起動する。スコア差、個別検査の退行、失敗を `evaluation.json` に記録する。悪化していれば `apply` が拒否するが、**良い結果が自動採用につながることはない**。適用には常に人の承認が要る。学習用・採否確認用・最後の未使用課題は利用者側で分ける。

評価器のタイムアウト時はプロセスグループの終了を試みる。出力（標準出力と標準エラーの合計）は 16 MiB を上限とし、超えた回はプロセスを止めて失敗として記録する。別 session へ離脱した子孫プロセスまで止める保証はなく、Windows の終了は最善努力である。評価器設定の hash を記録するが、評価器のスクリプト本体・実行バイナリの版は利用者側でも固定する。

既存の評価システムは、この入出力を満たすアダプターから呼べる。評価器は利用者が指定する実行プログラムである。コピーは OS の隔離ではないため、そのプログラムのネットワークや外部ファイルへの作用までは制限しない。

## 既知の限界

- **rule の paths。** `rule` は「特定のパスに限定した規約」を前提とし、paths が空の候補は適用不可として保留になる。常時効く方針は `claude_md` を選ぶ。プロジェクト全体に共通する方針が多い環境では、保留が大量に発生しうる。
- **照合は候補数の二乗で増える。** ペア照合は候補数に対して二乗で増える。既定の上限は 1,000 ペアで、上限で切れた分は照合済みにはしない。大量データはプロジェクトや期間で区切り、必要に応じて `--max-pairs` を増やす。大きな候補では `--review-batch-size 1` で一回の入力を小さくできる。
- **引用の実在は保証するが、意味の忠実さは保証しない。** 照合は引用文字列の実在と行範囲だけを検査する。抽出された条件・行動・理由がその引用から意味的に導けるかは検査しない。
- **判断の品質はチャットの文脈に依存する。** 多数のチャンクを一つのチャットで処理すると文脈が長くなる。入力を分けて複数の run にする方が安定する。

## 応用した先行例

| 先行例 | 取り入れた設計 |
| --- | --- |
| [AutoGuide](https://arxiv.org/abs/2403.08978) | 条件と行動を分けて抽出し、範囲の違いを矛盾と混同しない |
| [ACE](https://arxiv.org/abs/2510.04618) | 知見の原子化、安定 ID、全文再要約に頼らない差分管理 |
| [Trace2Skill](https://github.com/Qwen-Applications/Trace2Skill) | 経験から skill の本文・手順へ変換し、候補を比較する工程 |
| [Recuris](https://github.com/Gen-Verse/Recuris) | 修正先の部品を分け、評価の後に採用する考え方 |
| [GEPA](https://gepa-ai.github.io/gepa/guides/claude-cli-as-proposer/) | 候補生成（Claude）と評価器を分離する構成 |

これらの論文アルゴリズムの完全再現ではない。重み学習や自動進化は実装せず、明示的なバッチ処理、根拠検証、比較評価、可逆な反映に応用している。

## 検証

```console
python -m unittest discover -s tests -v
```

原文と引用の照合、未処理範囲、応答の欠落、差分、承認改変、既存本文保持、パス逸脱（シンボリックリンク・ジャンクション）、途中停止、後からの編集保護、別コピーの評価、評価器の出力上限、依頼書と検証呼出しの一致、CLI の既定値と終了コード、架空メモリ 3 件の通し（収集から復元までの原本バイト一致）をテストしている。

テストは 115 件。Windows 11 / Python 3.13 で全件の成功を確認している（8 件はスキップ。6 件はシンボリックリンク作成に特権が要るため、2 件は POSIX 専用のため）。Linux / Python 3.12 でも全件成功する（Windows 専用のジャンクション検査 3 件をスキップ）。Windows のプロセス終了処理は最善努力のままである。

## 由来

前身の memory-harness は、Python から Claude Code CLI を外部起動して候補を抽出していた。本ツールはその検証カーネルを残し、判断の部分をチャットの Claude に移したものである。外部起動と金額の概念は持たない。
