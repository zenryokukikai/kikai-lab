# `kikai publish` — 登録済みレシピを kikai 非依存パッケージへ書き出す

Issue: #63

## 1. 目的と完了条件

登録済みの script bundle・コンテナ定義・op 列から、**kikai が無い機械でも同じ処理を再実行できる 1
ディレクトリ**を作る。受け手 (engine 側、セルフサーブ経路) は `env.example` を埋めて
`bash run.sh` を叩くだけでよい。

完了条件:

- `adapter=script_bundle_run` の op 列が、依存順そのままの `docker run` コマンド列になる
- 参照される bundle の実体がパッケージに入り、`run.sh` はパッケージ内の bundle だけを見る
- ホスト固有値 (マウント元パス・イメージ名) が変数化され、パッケージ本体に literal で残らない
- 翻訳できない op は **除外され、README に位置と理由が載る** (黙って落とさない)
- 秘密の値がパッケージに入らない (鍵の名前だけ)

## 2. データの流れ

```
op JSON 列 (--ops a.json b.json …)   ← 実行順 = 依存順。並べ替えない
        │
        │  request.bundle_id / container_id / entrypoint / args / env / workdir / detach
        ▼
project_root (ローカルのレジストリ)
        ├─ containers/<container_id>.yaml   → image / gpus / mounts / shm_size / workdir …
        └─ script_bundles/<bundle_id>/      → bundle.json (entrypoints, files+sha256) + root/…
        │
        ▼
   translate_operation()          純関数。env 参照を **解決しない**
        │  → TranslatedOp(command=[Word], skipped_reason)。Word = (Seg(literal|var), …)
        ▼
   write_package()
        publish/<name>/
          bundles/<bundle_id>/bundle.json      bundle 実体のコピー (sha256 検証済み)
          bundles/<bundle_id>/root/…
          run.sh        set -euo pipefail + docker run コマンド列
          env.example   変数名だけ (値は空)
          README.md     GPU/イメージ/データ配置の前提・除外された op の表
```

### なぜサーバの HTTP API を使わないか

publish に必要なのは **bundle のファイル実体**とコンテナ定義。現行サーバは

- `GET /v1/projects/{p}/bundles/{id}` … manifest だけ返す (ファイル本体は返さない)
- コンテナ定義の GET … **存在しない**

の 2 点で足りない。両方を新設するより、サーバ自身が読んでいる**レジストリのディレクトリを直接
読む**方が口が 1 つで済む (「設定の口が 2 通りある」を作らない)。publish はレジストリのある機械
(= kikai サーバの走っている host) で実行する。手元から遠隔のレジストリを publish したい場合は、
bundle tar 取得と container 取得の 2 エンドポイントを後から足す — 本 issue の範囲外。

このため CLI のプロジェクト指定は `kikai remote` の `<project>` (サーバ上の id) ではなく、
ローカルの他コマンド (`validate` / `current` / `show` / `script-bundle`) と同じ **`--project-root`**
に揃える。

## 3. CLI

```
kikai publish <name> --project-root <p> --ops op1.json op2.json … [--out <dir>]
```

- `<name>` … パッケージ名。既定の出力先は `./publish/<name>`
- `--ops` … 1 個以上。**並び順が実行順**
- `--out` … 出力先を明示。既存で空でなければ `publish.output_exists` で拒否 (上書きしない)
- 出力は他コマンドと同じ envelope (`ok/schema_version/data/warnings/errors/next_actions`)

## 4. 変換規則

### 4.1 translate できる op

`adapter == "script_bundle_run"` のみ。argv は
`script_bundle_entrypoint_argv(bundle, …) + request.args`。bundle manifest の argv は
`script_bundles/<id>/root/<file>` の**相対パス**なので、workdir を変えずに済むよう
`bundles/` を `/workspace/kikai_project/script_bundles` へマウントする。

| kikai 側 | published run.sh |
| --- | --- |
| `docker run --rm` / `detach=true` の `docker run -d --name <n>` | そのまま |
| foreground でも `docker.name` が安全な名前なら `--name <n>` | そのまま (unsafe な literal 名は本体同様に付けない。detach の unsafe 名は本体同様に拒否) |
| `--gpus` / `--network` / `--ipc` / `--shm-size` | そのまま |
| `--workdir` (`request.workdir` → `container.workdir` → `/workspace/kikai_project`) | そのまま |
| `request.env` の `-e K=V` | そのまま (値の env 参照は変数のまま) |
| `KIKAI_RUN_ID` / `KIKAI_CONTAINER_ID` / `KIKAI_OPERATION` の自動注入 | そのまま (中のスクリプトが読む) |
| 実行時に env / secrets から値を解決 | run.sh 冒頭で `$PUBLISH_ROOT/.env` を自動読み込みし、使う変数全部に `: "${NAME:?…}"` の門を置く (未設定のまま走らせない) |
| — | `env.example` は変数ごとに出所と publish 元での値をコメントで添える (値の行は `NAME=` のまま空) |
| `docker.image` の literal 値 | `IMAGE_<container_id の大文字 slug>` として変数化 (mount source の `SRC_<target slug>` と同じ台帳) |
| `project_root:/workspace/kikai_project:ro` | `"$PUBLISH_ROOT/bundles":/workspace/kikai_project/script_bundles:ro` |
| `container.mounts[]` の `source:target:mode` | source のみ変数化。**target と mode は不変** |
| `--label kikai.container_id=…` | 落とす (kikai の帳簿。実行に影響しない) |
| `request.docker_host` | 落とす (どの daemon に話すかは kikai 側の話。README に明記) |
| `request.timeout_sec` | 落とす (kikai の監視。README に明記) |

`env:NAME` / `${NAME}` の NAME はシェル識別子 (`[A-Za-z_][A-Za-z0-9_]*`) に限る。外れる名前
(例: `env:DATA-DIR`) は `${DATA-DIR}` がデフォルト展開として黙って誤動作するので、
`publish.env_ref_invalid` で publish 自体を拒否する。

### 4.2 translate できない op → 除外して README に書く

- `adapter` が `script_bundle_run` 以外 (`noop` / `artifact_delivery` / `webhook_notification` /
  `operation_sequence` / 各 guard / `remote_docker_teardown` など)
- mount が `source_kind: kikai_managed_source_snapshot` (snapshot の書き出しは本 issue の範囲外)

除外は 3 か所に必ず出る: envelope の `warnings`、`run.sh` の該当位置のコメント、README の表
(位置・operation 名・adapter・理由)。**沈黙しない**のが要件。

## 5. §設計論点 への答え

1. **マウント元パスの変数化の粒度** … source パス 1 つにつき変数 1 つ。target と mode は literal
   のまま (`:ro` を落とさない)。変数名は target パスから決める (`/srv/data/faces` →
   `SRC_SRV_DATA_FACES`)。同じ slug で source が違えば `_2`, `_3` を足す。source がもともと
   `env:FOO` / `${FOO}` なら **その FOO をそのまま使う** (同じものに 2 つの名前を付けない)。
2. **秘密情報** … publish は `resolve_text_ref` を**呼ばない**。`env:NAME` / `${NAME}` は
   `"${NAME}"` としてシェル変数に落ち、`env.example` には `NAME=` と鍵名だけが並ぶ。
   `~/.kikai/secrets.json` の値はパッケージに入らない。
3. **kikai 専用アダプタ** … §4.2。翻訳可能なものだけ翻訳し、残りは除外 + 明記。
4. **単位** … 単発 op 列。`--ops` の並び順が依存順で、並べ替えも重複除去もしない。
   将来の run / submit-from レシピ展開は同じ translate 層の上流を足すだけで済む形にする。
5. **同期しない** … publish は snapshot。bundle が immutable なのと同じ割り切りで、
   書き出し後にレジストリが変わってもパッケージは追随しない。パッケージには `bundle.json`
   (sha256 付き) をそのまま入れ、どの版から出したかが後から分かるようにする。

## 6. モジュール分割

| ファイル | 責務 | 行数 |
| --- | --- | --- |
| `kikai_lab/publish.py` | op を読んで語列へ翻訳する。op を知っているのはここだけ | 525 |
| `kikai_lab/publish_package.py` | パッケージの形 — 語の模型・字面・書き出し。op を知らない | 349 |
| `kikai_lab/cli.py` | `publish` サブコマンドの parser と dispatch | 既存 +47 |
| `tests/test_publish.py` | 翻訳規則と除外の検査 | 647 |
| `tests/test_publish_shell.py` | 生成 run.sh の構文妥当性・引用の検査 | 184 |

依存は一方向 (`publish` → `publish_package`)。翻訳側は純関数に閉じてあり、副作用を持つのは
`publish_package.write_package` だけ。env を読まないので、テストが実環境の変数に汚染されない。

## 7. 検査計画

### 7.1 通常の検査

- `translate_operation` … gpus/network/ipc/shm/workdir/env/mounts/image/argv がトークン列に
  出ること、detach で `-d --name` になること
- `bundles/` に bundle 実体が sha256 一致でコピーされること
- `env.example` に鍵名だけが並び、値が入っていないこと
- `README.md` に除外 op の表が出ること
- **parity**: 全 ref を解決できる環境で、published のトークン列を解決した結果が
  `operation.docker_run_command()` の出力と、文書化した差分 (project_root マウント・label・
  変数化) を除いて一致すること — 翻訳が本体から乖離したら赤になる網
- **run.sh の妥当性**: `bash -n` で構文検査 (実行しない)。`shellcheck` があれば併せて実行、
  無ければ skip

### 7.2 変異 (4 種以上、いずれも赤になること)

| # | 変異 | 落ちるべき検査 |
| --- | --- | --- |
| 1 | op の順序を無視して並べ替える / ソートする | run.sh のコマンド順の検査 |
| 2 | mount の `:ro` / `:rw` を落とす | mount spec の mode 検査 |
| 3 | 翻訳不能 adapter を黙って落とす (warning・README・run.sh コメントのどれかを消す) | 除外の 3 面検査 |
| 4 | source パスの変数化をやめて literal を直書きする | 「run.sh に host のパスが現れない」検査 |

変異の復元は `cp` の控えから戻す (`git checkout` は未コミットの実装ごと消すので使わない)。

### 7.3 走らせ方

```
uv run --frozen pytest tests/test_publish.py tests/test_publish_shell.py -q
```

利用者の実機なので **単一プロセス・並列オプションなし**。リポジトリ全体を回すときも同じ。

## 8. 範囲外 (この issue では作らない)

- 遠隔レジストリからの publish (bundle tar / container の GET エンドポイント新設)
- `source_snapshot` の書き出し
- run / submit-from レシピ単位の publish
- publish したパッケージの実行 (生成物の検査は文字列としての妥当性まで)
