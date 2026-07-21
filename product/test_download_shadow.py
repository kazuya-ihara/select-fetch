#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""影テスト成果物の302リダイレクト処理を外部通信なしで確認する。"""
import os
import sys
import unittest
from unittest import mock
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import download_shadow


class _Response:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body


class _RedirectingOpener:
    def open(self, request, timeout):
        self.request = request
        raise urllib.error.HTTPError(
            request.full_url, 302, "Found",
            {"Location": "https://signed.example.invalid/archive.zip?sig=hidden"},
            None,
        )


class DownloadShadowTest(unittest.TestCase):
    def test_redirect_download_does_not_forward_github_token(self):
        opener = _RedirectingOpener()

        def signed_urlopen(request, timeout):
            self.assertNotIn("Authorization", request.headers)
            self.assertTrue(request.full_url.startswith("https://signed.example.invalid/"))
            return _Response(b"zip-bytes")

        with mock.patch.object(download_shadow.urllib.request,
                               "build_opener", return_value=opener), \
             mock.patch.object(download_shadow.urllib.request,
                               "urlopen", side_effect=signed_urlopen):
            result = download_shadow.download_artifact(
                "https://api.github.com/repos/example/repo/actions/artifacts/1/zip",
                "github-token-not-printed",
            )

        self.assertEqual(b"zip-bytes", result)
        self.assertEqual(
            "Bearer github-token-not-printed",
            opener.request.headers["Authorization"],
        )


if __name__ == "__main__":
    unittest.main()
