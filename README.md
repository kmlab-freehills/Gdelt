# GDELT 需要シグナル収集・分析システム

このシステムは、GDELT Doc APIからコモディティ関連ニュースを定期的に収集し、PostgreSQLに保存、GeminiによるLLM分析を経て、REST API経由で需要シグナルを提供します。

## 主な機能

- **フェッチャー (fetcher.py)**: `targets/commodities.yaml` に定義されたコモディティごとに定期収集。ドメインブラックリスト・タイトル重複排除・trafilaturaによる本文スクレイピングを実施。429エラー時は自動リトライ。
- **LLMプロセッサ (llm_processor.py)**: DB内の未処理記事をコモディティごとにバッチ処理し、Geminiによる需要シグナル分析（★評価・除外判定・ドライバー特定・top3）を `llm_analysis` カラムに保存。
- **API (api.py)**: FastAPIによるデータ提供。コモディティ・日付・LLM処理状態でフィルタリング可能。
- **Docker対応**: DB・フェッチャー・LLMプロセッサ・APIの4サービスをDocker Composeで一元管理。

## ディレクトリ構造

```
/
├── demand_fetcher.py       # コア収集ロジック（フィルタリング・スクレイプ・LLMプロンプト生成）
├── fetcher.py              # 定期収集スケジューラ
├── llm_processor.py        # LLM分析スケジューラ（Gemini → DB保存）
├── database.py             # DBモデル定義
├── api.py                  # REST API サーバー
├── targets/
│   └── commodities.yaml    # コモディティ定義（クエリ・ラベル）
├── docker-compose.yml      # 全サービス構成
├── Dockerfile              # コンテナ定義
├── .env.example            # 環境変数テンプレート
├── requirements.txt
└── roadmap.yaml            # 開発ロードマップ
```

> このシステムは **Kizue_get_demand ブランチ**（収集・フィルタリング・LLMプロンプト生成）と **Migita ブランチ**（DB・スケジューラ・APIインフラ）を統合したものです。

## セットアップ手順

### 1. リポジトリの準備

```bash
git clone <repository_url>
cd <repository_directory>
git checkout integration
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

# スケジュール間隔（任意）
FETCH_INTERVAL_HOURS=6
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
| `gdelt_fetcher` | 定期収集ワーカー | - |
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
# 収集（copper のみ）
docker exec gdelt_fetcher python -c \
  "from fetcher import fetch_and_store_commodity; fetch_and_store_commodity('copper')"

# LLM処理（copper のみ）
docker exec gdelt_llm_processor python -c \
  "from llm_processor import process_commodity, _get_llm_backend; \
   process_commodity('copper', _get_llm_backend())"

# LLM処理（全コモディティ）
docker exec gdelt_llm_processor python -c \
  "from llm_processor import run_all, _get_llm_backend; run_all(_get_llm_backend())"

# llm_analysis のリセット（再処理したいとき）
docker exec gdelt_postgres psql -U gdelt_user -d gdelt_db -c \
  "UPDATE articles SET is_llm_processed=false, llm_analysis=null WHERE task_name='copper';"
```

## コモディティの追加

`targets/commodities.yaml` にエントリを追加するだけで次回収集から対象に含まれます。コードの変更は不要です。

```yaml
"lithium":
  label: "リチウム (Lithium)"
  queries:
    - '"lithium demand" (surge OR shortage OR deficit)'
    - 'lithium (consumption OR procurement) (surge OR shortage OR increase)'
    - 'lithium demand ("beats expectations" OR "ahead of forecast" OR unexpected)'
```

## API の利用

APIサーバー起動後、Swagger UIでエンドポイントを確認できます：

- **http://localhost:8000/docs**

主なエンドポイント：

| エンドポイント | 説明 |
|---|---|
| `GET /api/v1/articles` | 記事一覧（commodity・日付・is_llm_processed でフィルタ可） |
| `GET /api/v1/stats` | コモディティごとの収集件数・LLM処理件数・最新記事日時 |

`llm_analysis` フィールドの構造：

```json
{
  "rating": 3,
  "excluded": false,
  "reason": "判定理由（日本語）",
  "drivers": ["需要ドライバー1", "需要ドライバー2"],
  "top3": [{"id": 5, "why_consensus_breaking": "コンセンサスを超える理由"}]
}
```

| rating | 評価 | 意味 |
|---|---|---|
| 3 | ★★★ | コンセンサスからの乖離・新規シグナル |
| 2 | ★★☆ | 需要の二次的影響・周辺変化 |
| 1 | ★☆☆ | 既知トレンドの再確認（参考情報） |
| null | 除外 | 無関係・重複記事 |

## 今後の開発方針

詳細は `roadmap.yaml` を参照。

| Phase | 内容 | 状態 |
|---|---|---|
| 3 | API拡張（手動トリガー・評価フィルタ） | 🔲 未着手 |
| 4 | ローカルLLMへの差し替え | 🔲 未着手 |
| 5 | コモディティ自動生成（LLMでYAML拡張） | 🔲 未着手 |

---

### 注意事項

- **GDELTのレート制限**: 429エラーが頻発する場合は数分待機してから再実行してください。`demand_fetcher.py` の `SLEEP_BETWEEN_QUERIES`（現在12秒）で間隔を調整できます。
- **ドメインブラックリスト**: `sourcelang=eng` 指定でも混入する非英語ドメインは `demand_fetcher.py` の `DOMAIN_BLACKLIST` に随時追加してください。
- **ローカルLLMへの差し替え**: `llm_processor.py` の `_get_llm_backend()` に `call(prompt: str) -> str` インターフェースを実装するだけで切り替え可能です。
