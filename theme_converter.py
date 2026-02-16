import sys
import os

def extract_first_column(filepath):
    """
    指定されたテキストファイルを読み込み、各行をTABで分割し、
    最初の要素（インデックス[0]）のみを抽出したリストを返します。
    """
    extracted_list = []
    
    # ファイルの存在確認
    if not os.path.exists(filepath):
        print(f"エラー: ファイルが見つかりません: {filepath}")
        return None

    try:
        # ファイルを読み込み (エンコーディングはUTF-8を想定)
        with open(filepath, 'r', encoding='utf-8') as f:
            print(f"✅ ファイル '{filepath}' を読み込み中...")
            
            for line_number, line in enumerate(f, 1):
                # 行頭・行末の空白文字（改行など）を削除
                clean_line = line.strip()
                
                if not clean_line:
                    continue  # 空行はスキップ
                
                # TAB (\t) で分割
                parts = clean_line.split('\t')
                
                if len(parts) > 0:
                    # インデックス [0] の要素を抽出（これがテーマ名などの文字列本体）
                    extracted_list.append(parts[0])
                # else: 1列もない行は除外される

        print(f"✅ 処理完了。合計 {len(extracted_list)} 行のデータを抽出しました。")
        return extracted_list

    except UnicodeDecodeError:
        print(f"❌ エラー: ファイルのエンコーディングがUTF-8ではありません。ファイルのエンコーディングを確認してください。")
        return None
    except Exception as e:
        print(f"❌ 予期せぬエラーが発生しました: {e}")
        return None

if __name__ == "__main__":
    
    # 実行時にファイルパスを引数として受け取る
    if len(sys.argv) < 2:
        print("使用方法: python extract_first_column.py <ファイルパス>")
        print("例: python extract_first_column.py LOOKUP-GKGTHEMES.TXT")
        sys.exit(1)
        
    input_file = sys.argv[1]
    result_list = extract_first_column(input_file)
    
    if result_list is not None:
        # 結果を画面に出力（最初の10件のみ表示）
        print("\n--- 抽出結果のプレビュー (最初の10件) ---")
        for item in result_list[:10]:
            print(item)
        print("------------------------------------------")

        output_file = "cleaned_themes.txt"
        with open(output_file, 'w', encoding='utf-8') as out_f:
            for item in result_list:
                out_f.write(item + '\n')
        print(f"結果をファイル '{output_file}' に保存しました。")