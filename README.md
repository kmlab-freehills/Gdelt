# GDELT 需要シグナル収集・分析システム

このシステムは、GDELT v5 quadgram ngramsファイル（`weblegacy/ngrams`）からコモディティ・インフラ関連ニュースを定期的に収集し、PostgreSQLに保存、GeminiによるLLM分析を経て、REST API経由で需要シグナルを提供します。

## データソース: GDELT v5 quadgram ngrams

旧GDELT DOC 2.0 APIはレート制限（429エラー）が頻発し実運用が困難だったため、認証・レート制限のない静的GCSホスティングの ngrams ファイルへ移行しました。

- 配信URL: `https://storage.googleapis.com/data.gdeltproject.org/gdeltv5/weblegacy/ngrams/YYYYMMDDHHMMSS.{ngrams.txt,toc.json}.gz`（タイムスタンプはUTC、秒は常に`00`）
- 15分ハートビートでまとめて生成されるが生成タイミングはドリフトするため、`fetcher.py` は**全分をプローブして404を許容**する実装になっています
- 公開遅延は実測2〜3分。5分前のタイムスタンプまでを取得します
- `.ngrams.txt.gz`: タブ区切り3列（DOCID・QUADGRAM・COUNT）。QUADGRAMは英語等はスペース区切り最大4トークン、日本語・中国語等（scriptio continua）は連続4文字
- `.toc.json.gz`: 改行区切りJSON。1行1記事（ID・date・img・lang・title・url）
- DOCIDはファイルごとにリセットされるため、ngramsとtocは常にペアで扱います

## 主な機能

- **フェッチャー (fetcher.py)**: 5分ごとに `catch_up()` を実行し、DBに保存したカーソルから現在時刻-5分まで1分刻みでngrams/tocペアを取得・照合・保存。クラッシュ耐性のためタイムスタンプ処理1件ごとにカーソルを保存。
- **照合エンジン (ngrams_fetcher.py)**: `targets/*.yaml` のブール式クエリ（must/exclude）を、英語等はトークン列の連続部分列一致、CJKは4文字窓一致で評価。ドメインブラックリスト・trafilaturaによる本文スクレイピングも実施。
- **LLMプロセッサ (llm_processor.py)**: DB内の未処理記事をターゲットごとにバッチ処理し、Geminiによる需要シグナル分析（★評価・除外判定・ドライバー特定）を `llm_analysis` カラムに保存。
- **API (api.py)**: FastAPIによるデータ提供。ターゲット・日付・LLM処理状態でフィルタリング可能。
- **Docker対応**: DB・フェッチャー・LLMプロセッサ・APIの4サービスをDocker Composeで一元管理。

## ディレクトリ構造

```
/
├── ngrams_fetcher.py       # コア収集ロジック（ダウンロード・照合エンジン・フィルタリング・スクレイプ）
├── fetcher.py              # 定期収集スケジューラ（5分ごとcatch_up）
├── llm_processor.py        # LLM分析スケジューラ（Gemini → DB保存）
├── database.py             # DBモデル定義（Article・FetchState）
├── api.py                  # REST API サーバー
├── targets/
│   ├── commodities.yaml    # コモディティ定義（must/excludeクエリ・ラベル・lang）
│   ├── infrastructure.yaml # インフラ課題定義（日本語ネイティブキーワード）
│   └── construction.yaml   # 高層ビル・大規模建設の進捗追跡定義（英語+日本語）
├── docker-compose.yml      # 全サービス構成
├── Dockerfile              # コンテナ定義
├── .env.example            # 環境変数テンプレート
└── requirements.txt
```

## セットアップ手順

### 1. リポジトリの準備

```bash
git clone <repository_url>
cd <repository_directory>
```

### 2. 環境変数の設定

`.env.example` を `.env` にコピーし、必要な値を設定します。

```bash
cp .env.example .env
```

`.env` の設定項目：

```env
# LLM
GEMINI_API_KEY=your_gemini_api_key_here

# PostgreSQL（Docker Compose使用時はこのまま）
DATABASE_URL=postgresql://gdelt_user:gdelt_password@db:5432/gdelt_db
POSTGRES_USER=gdelt_user
POSTGRES_PASSWORD=gdelt_password
POSTGRES_DB=gdelt_db

# 初回起動時（カーソル未設定）に何分前まで遡ってngramsを取得するか
NGRAMS_BACKFILL_MINUTES=60
LLM_INTERVAL_HOURS=6
```

### 3. Docker Compose で起動

```bash
docker compose up -d --build
```

起動するコンテナ：

| コンテナ | 役割 | ポート |
|---|---|---|
| `gdelt_postgres` | PostgreSQL データベース | 5432 |
| `gdelt_fetcher` | 定期収集ワーカー（5分ごとcatch_up） | - |
| `gdelt_llm_processor` | LLM分析ワーカー | - |
| `gdelt_api` | REST API サーバー | 8000 |

## ログの確認

```bash
docker logs -f gdelt_fetcher
docker logs -f gdelt_llm_processor
docker logs -f gdelt_api
```

## 手動実行

スケジュール実行を待たずに即時実行したい場合：

```bash
# 収集（catch_upを1回実行）
docker exec gdelt_fetcher python -c \
  "from fetcher import catch_up; catch_up()"

# LLM処理（全ターゲット）
docker exec gdelt_llm_processor python -c \
  "from llm_processor import run_all, _get_llm_backend; run_all(_get_llm_backend())"

# llm_analysis のリセット（再処理したいとき）
docker exec gdelt_postgres psql -U gdelt_user -d gdelt_db -c \
  "UPDATE articles SET is_llm_processed=false, llm_analysis=null WHERE target='copper';"
```

## ターゲットの追加

`targets/*.yaml` にエントリを追加するだけで次回収集から対象に含まれます。コードの変更は不要です。

```yaml
"lithium":
  label: "リチウム (Lithium)"
  lang: "en"
  monitor:
    queries:
      - must: ["lithium", ["demand", "shortage", "surplus"]]
  analyze:
    queries:
      - must: ["lithium demand", ["surge", "shortage", "deficit"]]
      - must: ["lithium", ["consumption", "procurement"], ["surge", "shortage", "increase"]]
      - must: ["lithium demand", ["beats expectations", "ahead of forecast", "unexpected"]]
```

各クエリの `must` は「文字列（必須フレーズ）」または「文字列のリスト（ORグループ、うち1つ以上必須）」の配列です。`exclude` を指定すると、いずれかが含まれる記事を除外できます。`lang` はtocの言語コード（`en`/`ja`/`zh`等）で、その言語の記事のみが照合対象になります。CJK言語では4文字を超えるフレーズも4文字窓の照合で正しく扱われます。

## API の利用

APIサーバー起動後、Swagger UIでエンドポイントを確認できます：

- **http://localhost:8000/docs**

主なエンドポイント：

| エンドポイント | 説明 |
|---|---|
| `GET /api/v1/articles` | 記事一覧（target・日付・is_llm_processed でフィルタ可） |
| `GET /api/v1/stats` | ターゲットごとの収集件数・LLM処理件数・最新記事日時 |

`llm_analysis` フィールドの構造：

```json
{
  "rating": 3,
  "excluded": false,
  "tone": "bullish",
  "tone_score": 42.0,
  "reason": "判定理由（日本語）",
  "why_notable": "コンセンサスを超える理由（日本語）",
  "causal": {"trigger": "...", "mechanism": "...", "effect": "...", "timeframe": "short"},
  "drivers": ["需要ドライバー1", "需要ドライバー2"]
}
```

| rating | 評価 | 意味 |
|---|---|---|
| 3 | ★★★ | コンセンサスからの乖離・新規シグナル |
| 2 | ★★☆ | 需要の二次的影響・周辺変化 |
| 1 | ★☆☆ | 既知トレンドの再確認（参考情報） |
| null | 除外 | 無関係・重複記事 |

## ターゲット限定の常時収集パイプライン（fetcher_infra / llm_processor_infra）

`targets/*.yaml` 全体ではなく特定ターゲットのみを、既存の `fetcher`/`llm_processor`（commodities向け）とは独立したカーソルで継続収集・分析したい場合に使います。現在は `targets/infrastructure.yaml` の4ターゲット（英語版 `aging_water_infrastructure`/`seismic_building_risk`、日本語版 `_ja` サフィックス2つ）向けに設定済みです。

### 起動・停止

```bash
# 起動（dbも未起動なら自動起動）
docker compose up -d fetcher_infra llm_processor_infra

# ログ確認（15分ごとに収集・分析ログが出る）
docker compose logs -f fetcher_infra llm_processor_infra

# 停止（コンテナは削除されるがDBデータ・カーソルは保持される）
docker compose stop fetcher_infra llm_processor_infra
```

`restart: no` のため、PC/Docker再起動後は自動復帰しません。使うたびに `docker compose up -d fetcher_infra llm_processor_infra` を実行してください。カーソル（最後に処理したタイムスタンプ、DBの `fetch_state` テーブルに `ngrams_cursor:<ターゲット一覧>` というキーで保存）は停止中も保持されるため、再開時は停止していた期間分を自動で追いつきます（ただしGDELT側でngramsファイルが失効していれば取りこぼします）。

### 対象ターゲット・収集間隔の変更

`docker-compose.yml` の該当サービスの `environment` を編集し、再ビルド・再起動してください。

| 環境変数 | 対象サービス | 説明 |
|---|---|---|
| `TARGET_FILTER` | fetcher_infra | 収集対象ターゲットキー（カンマ区切り）。`ngrams_fetcher.TARGETS` のキーと一致させる |
| `LLM_TARGET_FILTER` | llm_processor_infra | LLM分析対象ターゲットキー（カンマ区切り） |
| `FETCH_INTERVAL_MINUTES` | fetcher_infra | 収集の実行間隔（分）。デフォルト5（未設定時） |
| `LLM_INTERVAL_MINUTES` | llm_processor_infra | LLM分析の実行間隔（分）。未設定なら`LLM_INTERVAL_HOURS`（時間単位）を使用 |

```bash
docker compose up -d --build fetcher_infra llm_processor_infra
```

### 結果の確認

```bash
docker exec gdelt_postgres psql -U gdelt_user -d gdelt_db -c "
SELECT id, target, collection_mode, title, source_domain,
       llm_analysis->>'rating' AS rating,
       llm_analysis->>'excluded' AS excluded,
       llm_analysis->>'tone' AS tone,
       llm_analysis->>'reason' AS reason
FROM articles
WHERE target IN ('aging_water_infrastructure','aging_water_infrastructure_ja',
                  'seismic_building_risk','seismic_building_risk_ja')
ORDER BY id DESC;"
```

収集件数・未処理件数の概観:

```bash
docker exec gdelt_postgres psql -U gdelt_user -d gdelt_db -c "
SELECT target, is_llm_processed, count(*) FROM articles
WHERE target LIKE '%infrastructure%' OR target LIKE '%seismic%'
GROUP BY target, is_llm_processed ORDER BY target;"
```

## 高層ビル・大規模建設の進捗追跡（fetcher_construction / llm_processor_construction）

`targets/construction.yaml` の2ターゲット（`large_scale_construction` 英語グローバル、`large_scale_construction_ja` 日本語）向けの専用パイプラインです。高層ビル・大規模建設プロジェクトの着工・竣工・中断・中止・再開等のニュースを収集します。衛星写真での進捗確認（地面の色の変化等）を別途行う際の補助シグナルとして、記事から建物名・所在地（都市・国・住所）も抽出します。

起動・停止は `fetcher_infra`/`llm_processor_infra` と同様です。

```bash
docker compose up -d fetcher_construction llm_processor_construction
docker compose logs -f fetcher_construction llm_processor_construction
docker compose stop fetcher_construction llm_processor_construction
```

construction系ターゲットの記事では、`llm_analysis` に以下の `construction` フィールドが追加されます（他ターゲットには含まれません）。

```json
{
  "construction": {
    "status": "under_construction",
    "building_name": "...",
    "location": {"city": "...", "country": "...", "address": null}
  }
}
```

| status | 意味 |
|---|---|
| planned | 発表済み・未着工 |
| groundbreaking | 起工式・着工イベント |
| under_construction | 施工中 |
| halted | 一時中断 |
| cancelled | 計画中止 |
| resumed | 中断からの再開 |
| topped_out | 最終高さに到達（躯体工事完了） |
| completed | 竣工・完成 |
| unknown | 記事から判断不可 |

`building_name`/`location` はLLMによる記事本文からの抽出（ベストエフォート）のため、記載がない場合は `null` になります。衛星画像との突き合わせはこのシステムの範囲外で、別途ユーザー側で実施する想定です。

## 今後の開発方針

詳細は `roadmap.yaml` を参照。

| Phase | 内容 | 状態 |
|---|---|---|
| 3 | API拡張（手動トリガー・評価フィルタ） | 🔲 未着手 |
| 4 | ローカルLLMへの差し替え | 🔲 未着手 |
| 5 | ターゲット自動生成（LLMでYAML拡張） | 🔲 未着手 |

---

### 注意事項

- **ngramsファイルの取りこぼし**: ネットワークエラー等で取得に失敗した場合はカーソルを進めずにcatch_upを終了し、次回実行時に再開します。0バイトファイルや404は正常系としてスキップしカーソルを進めます。
- **ドメインブラックリスト**: プレスリリース配信系・アグリゲーター系の低品質ドメインは `ngrams_fetcher.py` の `DOMAIN_BLACKLIST` に随時追加してください（非英語ドメインは `lang` フィルタで対応できるため対象外）。
- **ローカルLLMへの差し替え**: `llm_processor.py` の `_get_llm_backend()` に `call(prompt: str) -> str` インターフェースを実装するだけで切り替え可能です。
