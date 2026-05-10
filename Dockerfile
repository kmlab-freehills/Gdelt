# ベースイメージとして軽量なPython 3.11を使用
FROM python:3.11-slim

# 環境変数の設定
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1
ENV TZ=Asia/Tokyo

# 作業ディレクトリの設定
WORKDIR /app

# 依存関係のインストール
# キャッシュを有効活用するため、requirements.txtを先にコピー
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# アプリケーションコードのコピー
COPY . .

# 実行権限の付与（必要に応じて）
RUN chmod +x fetcher.py api.py

# デフォルトのコマンド（docker-composeで上書き可能）
CMD ["python", "api.py"]
