#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""直近の手動影テスト成果物を GitHub Actions から安全に取得する。

正式反映時に商品検索をやり直さないための補助スクリプト。GitHub の一時トークンは
環境変数から読み、画面やログには絶対に出さない。成果物は商品候補JSONだけで、
APIキー・Supabaseトークンは含まれない。
"""
import io
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile


GITHUB_API_VERSION = "2026-03-10"


def api_get(url, token, accept="application/vnd.github+json"):
    req = urllib.request.Request(url, headers={
        "Accept": accept,
        "Authorization": "Bearer " + token,
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
        "User-Agent": "select-fetch-shadow-reuse",
    })
    with urllib.request.urlopen(req, timeout=30) as res:
        return res.read()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """GitHub APIの302を、署名付きURLへ認証ヘッダーを持ち越さず処理する。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def download_artifact(artifact_url, token):
    """GitHub APIの302を一度受け、署名付きURLを無認証で取得する。

    APIのダウンロード先は短時間だけ有効なAzure Blobの署名URLになる。
    GitHubトークンをその別ドメインへ転送すると、ダウンロードが401/403で
    拒否されることがあるため、302を手動で処理してヘッダーを分離する。
    """
    req = urllib.request.Request(artifact_url, headers={
        "Accept": "application/vnd.github+json",
        "Authorization": "Bearer " + token,
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
        "User-Agent": "select-fetch-shadow-reuse",
    })
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=30) as res:
            return res.read()
    except urllib.error.HTTPError as e:
        if e.code != 302:
            raise
        location = e.headers.get("Location")
        e.close()
        if not location:
            raise RuntimeError("成果物の署名付きURLが返されませんでした")
        # 署名URLにはGitHubトークンを付けない。URL自体もログに出さない。
        signed_req = urllib.request.Request(location, headers={
            "User-Agent": "select-fetch-shadow-reuse",
        })
        with urllib.request.urlopen(signed_req, timeout=30) as res:
            return res.read()


def fail(message):
    print("‼ " + message)
    return 1


def main():
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    workflow = os.environ.get("GITHUB_WORKFLOW_REF", "").split("@", 1)[0].split("/", 1)[-1]
    if not token or not repo:
        return fail("GitHub Actionsの実行情報がありません。影テストの成果物を取得できません。")

    # workflow_ref は owner/repo/.github/workflows/products.yml@main の形式。
    workflow_path = "products.yml"
    ref = os.environ.get("GITHUB_WORKFLOW_REF", "")
    if "/.github/workflows/" in ref:
        workflow_path = ref.split("/.github/workflows/", 1)[1].split("@", 1)[0]
    workflow_encoded = urllib.parse.quote(workflow_path, safe="")
    base = api.rstrip("/") + "/repos/" + repo
    runs_url = (base + "/actions/workflows/" + workflow_encoded +
                "/runs?event=workflow_dispatch&status=success&per_page=30")
    try:
        runs = json.loads(api_get(runs_url, token).decode("utf-8"))
    except urllib.error.HTTPError as e:
        return fail("影テストの実行一覧を取得できません（HTTP %s）。" % e.code)
    except Exception as e:
        return fail("影テストの実行一覧を取得できません（%s）。" % type(e).__name__)

    current_run = os.environ.get("GITHUB_RUN_ID")
    candidates = []
    for run in runs.get("workflow_runs", []):
        if current_run and str(run.get("id")) == str(current_run):
            continue
        # artifactの存在確認を優先するため、日付の新しい順に見る。
        candidates.append(run)
    candidates.sort(key=lambda x: x.get("run_started_at") or x.get("created_at") or "", reverse=True)
    for run in candidates:
        run_id = run.get("id")
        if not run_id:
            continue
        try:
            artifacts = json.loads(api_get(base + "/actions/runs/%s/artifacts" % run_id,
                                           token).decode("utf-8"))
        except Exception:
            continue
        artifact = next((a for a in artifacts.get("artifacts", [])
                         if a.get("name") == "shadow-results" and not a.get("expired")), None)
        if not artifact or not artifact.get("id"):
            continue
        try:
            raw_zip = download_artifact(
                base + "/actions/artifacts/%s/zip" % artifact["id"], token)
            with zipfile.ZipFile(io.BytesIO(raw_zip)) as zf:
                names = [n for n in zf.namelist()
                         if n == "shadow_results.json" or n.endswith("/shadow_results.json")]
                if len(names) != 1:
                    continue
                payload = json.loads(zf.read(names[0]).decode("utf-8"))
            # 最低限の形式確認。詳細な有効期限・重複確認は fetch_claimed.py で行う。
            if payload.get("schema_version") != 1 or not isinstance(payload.get("items"), list):
                continue
            out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shadow_results.json")
            with open(out, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            print("影テスト結果を取得しました（Run #%s、再検索なしで正式反映します）。" % run_id)
            return 0
        except (zipfile.BadZipFile, KeyError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        except urllib.error.HTTPError as e:
            # トークンや署名URLは出さず、切り分けに必要なHTTPコードだけ残す。
            print("  成果物%dの取得をスキップ（HTTP %s）" % (artifact.get("id", 0), e.code))
            continue
        except Exception as e:
            print("  成果物%dの取得をスキップ（%s）" % (artifact.get("id", 0), type(e).__name__))
            continue

    return fail("有効な影テスト成果物が見つかりません。正式反映は行いません（既存結果は変更しません）。")


if __name__ == "__main__":
    sys.exit(main())
