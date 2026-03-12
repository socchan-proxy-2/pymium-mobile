# Pymium Remote Chromium

`python app.py` だけで起動できる、Pterodactyl 向けの簡易リモートブラウザです。

- 起動時に `quart` / `playwright` が無ければ自動で `pip install`
- 初回起動時に Playwright の Chromium を自動ダウンロード
- WebSocket で JPEG フレームをライブ配信
- マウス / キーボード / スクロール入力をブラウザへ転送
- 低遅延化として「古いフレーム破棄」「操作中は高頻度 / 静止中は低頻度」を実装
- ログを `logs/pymium.log` に保存
- 一時ディレクトリをプロジェクト内の `.pymium-tmp/` に固定して `/tmp` 制限を回避

## 起動

```bash
python app.py
```

ブラウザで `http://<host>:<PORT>` を開いて使います。`PORT` が未指定なら `8000` です。

## 主な環境変数

- `PORT` / `SERVER_PORT`: 待受ポート
- `START_URL`: 起動時URL（デフォルト `about:blank`）
- `BROWSER_WIDTH`: 初期幅（デフォルト `1600`）
- `BROWSER_HEIGHT`: 初期高さ（デフォルト `900`）
- `ACTIVE_FPS`: 操作中の目標FPS（デフォルト `24`）
- `IDLE_FPS`: 静止時の目標FPS（デフォルト `5`）
- `ACTIVE_JPEG_QUALITY`: 操作中JPEG品質（デフォルト `80`）
- `IDLE_JPEG_QUALITY`: 静止時JPEG品質（デフォルト `62`）
- `PYMIUM_TEMP_DIR`: 一時ディレクトリ
- `PYMIUM_CACHE_DIR`: キャッシュディレクトリ
- `PYMIUM_LOG_DIR`: ログディレクトリ
- `PYMIUM_LOG_LEVEL`: ログレベル

## ログと一時ファイル

- ログファイル: `logs/pymium.log`
- 一時ディレクトリ: `.pymium-tmp/`
- キャッシュディレクトリ: `.pymium-cache/`

`/tmp` に容量制限がある環境でも、ダウンロードや展開がプロジェクト内ディレクトリを使うようにしています。

## 注意

Python パッケージと Chromium 本体の自動取得は `app.py` が行いますが、**Chromium の OS 共有ライブラリ不足**までは Python だけでは解決できません。もし Chromium 起動に失敗したら、トップ画面のオーバーレイまたは `/api/status` の `error` を確認してください。

CDP の `Page.startScreencast` が使えない環境では、自動で screenshot fallback に切り替わります。