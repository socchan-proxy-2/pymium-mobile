# Pymium for mobile
このフォークはnazogeさんによる[pymium](https://github.com/nazoge/pymium)をモバイル向けにしたりwebp化したものです。
以下は本家のREADMEをコピペしただけのものです。
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
- `PYMIUM_AUTO_INSTALL_SYSTEM_DEPS`: Linux 共有ライブラリ不足時に Python から自動導入を試みるか（デフォルト `1`）
- `PYMIUM_LOCAL_LIB_DIR`: root不要で展開した共有ライブラリ置き場
- `PYMIUM_LOCAL_FONT_DIR`: root不要で展開した日本語フォント置き場
- `PYMIUM_FONTCONFIG_DIR`: fontconfig 設定ディレクトリ

## ログと一時ファイル

- ログファイル: `logs/pymium.log`
- 一時ディレクトリ: `.pymium-tmp/`
- キャッシュディレクトリ: `.pymium-cache/`
- ローカル共有ライブラリ: `.pymium-system-libs/`
- ローカル日本語フォント: `.pymium-fonts/`

`/tmp` に容量制限がある環境でも、ダウンロードや展開がプロジェクト内ディレクトリを使うようにしています。

Linux コンテナでは、Chromium 起動に必要な **OS 共有ライブラリ** が別途必要です。たとえば `libnspr4.so` や `libnss3.so` が無いと、ブラウザ本体のダウンロードは成功しても起動で失敗します。

Pymium は不足を検出すると、Python 側から `apt-get` / `apk` / `dnf` / `yum` を使って自動導入を試みます。ただし **パッケージマネージャが存在し、かつ root 権限で動いている場合に限ります**。

Debian/Ubuntu 系では追加で `python -m playwright install-deps chromium` 相当も Python 側から自動実行します。

さらに **root 権限が無くても Debian/Ubuntu 系なら**、`apt download` + `dpkg-deb -x` を Python から実行して `.pymium-system-libs/` に共有ライブラリを展開し、`LD_LIBRARY_PATH` で読み込む fallback を試みます。

同様に、日本語が豆腐になる環境向けに `fonts-noto-cjk` を `.pymium-fonts/` へ自動展開し、fontconfig をローカル設定へ切り替える処理も入れています。

- Debian/Ubuntu 系の例: `apt-get update && apt-get install -y libnspr4 libnss3`
- Alpine 系の例: `apk add --no-cache nspr nss`

不足がある場合は `logs/pymium.log` と `/api/status` の `error` / `missing_shared_library` に診断が出ます。

## 注意

Python パッケージと Chromium 本体の自動取得は `app.py` が行いますが、**Chromium の OS 共有ライブラリ不足**までは Python だけでは解決できません。もし Chromium 起動に失敗したら、トップ画面のオーバーレイまたは `/api/status` の `error` を確認してください。

CDP の `Page.startScreencast` が使えない環境では、自動で screenshot fallback に切り替わります。
