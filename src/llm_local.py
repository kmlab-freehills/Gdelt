import torch
import json
import os
from transformers import AutoModelForCausalLM, AutoTokenizer


MODEL_ID = "Qwen/Qwen2.5-1.5B-Instruct"
# MODEL_ID = "Qwen/Qwen2.5-14B-Instruct"

SYSTEM_PROMPT = """
あなたは製造業のサプライチェーン・リスク管理の専門家です。
入力されたニュース記事が、特定のキーワード（企業や製品）の「供給網」「生産活動」「事業継続性」に影響を与えるか判定してください。

【判定基準】
以下の4つの観点のいずれかに該当する場合、`is_relevant: true` と判定してください。
直接的な言及だけでなく、間接的な影響（バタフライ効果）も考慮してください。

1. 製造・生産（Direct）: 工場新設、設備投資、撤退、生産停止、技術提携。
2. 物流・インフラ（Logistics）: 港湾ストライキ、コンテナ不足、運河の封鎖、燃料高騰。
3. 災害・有事（Crisis）: 工場地帯での地震・洪水、火災、戦争、パンデミック。
4. 政治・経済（Macro）: 関税導入、輸出規制、貿易摩擦、原材料の輸出禁止措置。

【除外対象（ノイズ）】
* 一般消費者向けの製品レビュー
* 単なる日々の株価変動速報
* キーワードが含まれるだけの無関係なニュース

【回答フォーマット】
以下のJSON形式のみで回答してください。Markdownのコードブロック（```json）は不要です。
{
  "is_relevant": true または false,
  "summary_japanese": "「米国で関税引き上げ」のように、事象を客観的に25文字以内で要約"
}
"""

class LocalLLMHandler:
    def __init__(self):
        print(f"Loading Local Model: {MODEL_ID}...")
        
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        print(f"Device: {self.device}")

        self.tokenizer = AutoTokenizer.from_pretrained(
            MODEL_ID, 
            trust_remote_code=True
        )
        
        self.model = AutoModelForCausalLM.from_pretrained(
            MODEL_ID,
            device_map="auto",
            torch_dtype=torch.float16 if self.device == "cuda" else torch.float32,
            trust_remote_code=True
        )
        print("✅ Model Loaded Successfully.")

    def process_batch(self, items):
        """
        evidence_fetcherから渡された辞書のリストを順次処理する
        """
        results = []
        total = len(items)
        
        for i, item in enumerate(items):
            print(f"  Thinking... [{i+1}/{total}] {item.get('keyword')} - {item.get('source_name')}")
            
            user_content = f"""
            【入力情報】
            キーワード: {item.get('keyword')}
            メディア: {item.get('source_name')}
            記事URL: {item.get('url')}
            記事本文: {item.get('scraped_text', '本文取得失敗またはデータなし')}
            """
            
            try:
                res = self._predict_single(user_content)
                
                item['ai_summary'] = res.get("summary_japanese", "要約生成失敗")
                item['status'] = "RELEVANT" if res.get("is_relevant", False) else "NOISE"
                
                print(f"    -> Result: {item['status']}")
                
            except Exception as e:
                print(f"    -> Prediction Error: {e}")
                item['ai_summary'] = f"Error: {str(e)}"
                item['status'] = "UNCHECKED"
            
            results.append(item)
            
        return results

    def _predict_single(self, text):
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text}
        ]
        
        input_text = self.tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )
        
        inputs = self.tokenizer([input_text], return_tensors="pt").to(self.model.device)

        with torch.no_grad():
            generated_ids = self.model.generate(
                **inputs,
                max_new_tokens=256,
                temperature=0.01,
                do_sample=False
            )
            
        raw_output = self.tokenizer.batch_decode(
            generated_ids[:, inputs['input_ids'].shape[1]:], 
            skip_special_tokens=True
        )[0]

        return self._parse_json(raw_output)

    def _parse_json(self, raw_output):
        try:
            clean_json = raw_output.replace("```json", "").replace("```", "").strip()
            
            start_idx = clean_json.find("{")
            end_idx = clean_json.rfind("}")
            
            if start_idx != -1 and end_idx != -1:
                clean_json = clean_json[start_idx : end_idx + 1]
                return json.loads(clean_json)
            else:
                print(f"Warning: JSON not found in output: {raw_output[:50]}...")
                return {"is_relevant": False, "summary_japanese": "JSON解析失敗"}
                
        except Exception as e:
            print(f"JSON Parse Error: {e}")
            return {"is_relevant": False, "summary_japanese": f"Parse Error: {str(e)}"}
  