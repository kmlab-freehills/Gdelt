# demand_fetcher — 需要シグナル収集ツール

GDELT DOC API を使い、指定した対象（原材料・製品・業種など）に関する需要動向ニュースを収集し、LLMへ貼り付けるプロンプトを自動生成するスクリプトです。

---

## 目的

任意の対象（原材料・製品・業種など）に関する需要変動を示す英語ニュース記事をGDELTから取得し、LLM（ChatGPT等）による需要シグナル分析に利用できるプロンプトを出力します。現在試用段階であり、将来的にはローカルLLMにそのプロンプトを渡し、分析まで完了した状態で出力（DB保存）予定。

現在は原材料（コモディティ）を対象としていますが、今後は自動車などの製品レベルへの拡張を予定しています。

---

## 検索対象リスト（テストリスト）


| キー | 対象 |
|------|------|
| `copper` | 銅（デフォルト） |
| `gold` | 金 |
| `uranium` | ウラン |
| `rare earth` | レアアース（ネオジム・ジスプロシウム等） |
| `natural gas` | 天然ガス・LNG |
| `silver` | 銀 |

> 拡張予定の例: 自動車、半導体、電池材料 など

---

## セットアップ

### 必要環境

- Python 3.9 以上
- インターネット接続（GDELT API・記事サイトへのアクセス）

### インストール

```bash
pip install -r requirements.txt
```

> `trafilatura` は記事本文の取得に使用します。未インストールでも動作しますが、本文抜粋なしのタイトルのみモードになります。  
> `pyyaml` はコモディティ定義ファイル（`commodities.yaml`）の読み込みに必須です。

---

## 使い方

```bash
# 銅（デフォルト）を過去14日分取得
python demand_fetcher.py

# 複数コモディティを指定
python demand_fetcher.py --commodity gold uranium

# 全コモディティを過去7日分取得
python demand_fetcher.py --commodity all --days 7

# 本文取得をスキップして高速化
python demand_fetcher.py --commodity copper --no-scrape

# コンセンサス的な長期トレンド記事を除外して分析（デフォルトは含める）
python demand_fetcher.py --commodity copper --exclude-consensus
```

### オプション一覧

| オプション | デフォルト | 説明 |
|-----------|-----------|------|
| `--commodity NAME [NAME ...]` | `copper` | 対象コモディティ。`all` で全件 |
| `--days N` | `14` | 遡及日数（最大約90日） |
| `--no-scrape` | （未指定） | 本文取得をスキップしタイトルのみで実行（タイトルのみで判断させると、精度が著しく低下する恐れあり） |
| `--exclude-consensus` | （未指定） | コンセンサス的な長期トレンド記事をLLMプロンプトの除外対象に含める |

---

## 処理の流れ

```
1. commodities.yaml からクエリ定義を読み込み
       ↓
2. コモディティごとに複数のGDELTクエリを実行（OR演算子で集約済み）
       ↓
3. URL・タイトルの重複排除 + ドメインブラックリストフィルタ
       ↓
4. trafilatura で各記事の本文を並列取得（--no-scrape で省略可）
       ↓
5. LLM分析用プロンプトを標準出力に表示
       ↓
6. プロンプトをコピーしてLLMに貼り付けて分析
```

---

## クエリ設計（3層構造）

各コモディティに対し、以下の3層でクエリを構成しています。クエリ定義は `targets/commodities.yaml` で管理します。

| 層 | 目的 | 例 |
|----|------|-----|
| Layer 1 フレーズ層 | 精度優先のアンカー検索 | `"copper demand" (surge OR shortage OR deficit)` |
| Layer 2 類義語層 | 表現の揺れをカバー（再現率向上） | `copper (consumption OR offtake OR procurement) (surge OR shortage)` |
| Layer 3 デルタ検知層 | コンセンサス超えのサプライズ検知 | `copper demand ("beats expectations" OR "ahead of forecast")` |

OR演算子を活用してクエリ数を集約し、APIコール数を削減しています。  
市況まとめ・株価サマリー系の記事はクエリ段階でマイナス検索により除外されます。

### コモディティの追加・変更

`targets/commodities.yaml` を編集するだけで対象を追加・変更できます。自動車・半導体などカテゴリが増えた場合は `targets/` 以下に新しいYAMLファイルを追加してください。コードの修正は不要です。

```yaml
"lithium":
  label: "リチウム (Lithium)"
  queries:
    - '"lithium demand" (surge OR shortage OR deficit)'
    - 'lithium (consumption OR procurement) (surge OR shortage)'
    - 'lithium demand ("beats expectations" OR "ahead of forecast" OR unexpected)'
```

---

## ドメインブラックリスト

`sourcelang=eng` を指定しても混入する低品質ドメインを個別にリスト管理して除外します（`DOMAIN_BLACKLIST` に定義）。

除外カテゴリ例：
- 中国系金融メディア（eastmoney.com等）
- 韓国系メディア（hankyung.com等）
- プレスリリース配信サービス（prnewswire.com等）

---

## 出力プロンプトの構成

出力されるLLMプロンプトには以下が含まれます。

1. **除外ルール**: 市況まとめ・重複記事を除外させる指示。`--exclude-consensus` 指定時はコンセンサス的な長期トレンド記事も除外対象に追加
2. **評価基準（★3段階）**:
   - ★★★: コンセンサスからの「ズレ・変化率（デルタ）」を示す記事
   - ★★☆: 需要増の二次的影響（代替品シフト・SCボトルネック等）を報じる記事
   - ★☆☆: 既知トレンドの再確認にとどまる記事（常に低評価として扱う。`--exclude-consensus` を付けると「低評価」ではなく「分析対象外（除外）」として扱われる）
3. **記事一覧**: タイトル・日付・メディア・URL・本文抜粋（最大600文字）

---

## 主要パラメータ（スクリプト内定数）

| 定数 | 値 | 説明 |
|------|----|------|
| `MAX_RECORDS_PER_QUERY` | 10 | 1クエリあたりの最大取得件数 |
| `SLEEP_BETWEEN_QUERIES` | 6.0秒 | GDELT APIへのリクエスト間隔 |
| `MAX_RETRIES` | 3 | APIリクエスト失敗時のリトライ回数 |
| `SCRAPE_TEXT_LIMIT` | 600文字 | 本文抜粋の最大文字数 |
| `_SCRAPE_MAX_WORKERS` | 5 | 本文取得の並列スレッド数 |

---

## 注意事項

- GDELT DOC API は無料・認証不要ですが、リクエスト頻度に制限があります。過度な連続実行は避けてください。
- `--days` に大きな値を指定すると取得件数・実行時間が増加します。
- 本文取得（scrape）は各記事サイトへアクセスするため、ペイウォール記事は取得できません。
