# select-fetch（商品取得ワーカー）

社内ツール「Select.」の**商品取得だけ**を担う公開リポジトリです。GitHub Actionsを無料・無制限で使うために公開にしています。

## このリポジトリに「入っていないもの」（＝流出しない資産）
- ❌ テーマ・切り口の**生成ロジック**（`batch/run.py`）
- ❌ **軸ライブラリ**（`memory/axis-library.json`＝購買軸・悩み・シーンの蓄積＝本体の資産）
- ❌ 過去の**カタログ／収集データ**（`data/`）
- ❌ すべての**APIキー・トークン**（GitHub Secretsに暗号化保存。コードにも履歴にも無い）
- ❌ SupabaseプロジェクトURL（Secret `SUPABASE_URL` から注入）
- ❌ Amazonアフィリタグ（Secret `AMZ_PARTNER_TAG` から注入）

入っているのは「Supabaseから今日のカタログを読み、公式APIで商品を検索・照合して保存するコード」だけです。

## 動作
- `prefetch.yml`：毎朝、上位テーマの切り口を先読み取得
- `products.yml`：一定間隔で「対応済み」切り口を取得
- カタログ生成は**別の非公開リポジトリ**が担当（Supabase経由で連携）

## 必要なGitHub Secrets（Settings → Secrets and variables → Actions）
RAKUTEN_APP_ID / RAKUTEN_ACCESS_KEY / YAHOO_CLIENT_ID / AMZ_CREDENTIAL_ID /
AMZ_CREDENTIAL_SECRET / GEMINI_API_KEY / BATCH_TOKEN / AMZ_PARTNER_TAG / SUPABASE_URL

**値はGitHubのSecrets画面にだけ入力してください。コード・コミット・Issue・チャットに貼らないこと。**
