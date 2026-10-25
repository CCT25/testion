# EPUB 英文 → 中文翻譯器

用 Claude API 把英文 EPUB 電子書翻成中文，保留原書的排版、粗體斜體、連結、圖片與目錄。

## 安裝

```bash
pip install -r requirements.txt
```

設定 API 金鑰（到 https://console.anthropic.com 取得）：

```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-..."
```

## 使用

```bash
python epub_translate.py book.epub
```

輸出 `book.zh-TW.epub`。常用選項：

| 選項 | 說明 |
|---|---|
| `--bilingual` | 中英對照，每段原文後面接中文 |
| `--lang zh-CN` | 改成簡體中文（預設 `zh-TW` 繁體） |
| `--glossary terms.json` | 自訂譯名，例如 `{"Hermione": "妙麗"}` |
| `--effort low` | 較快、較便宜；`high` 品質更好但更慢更貴（預設 `medium`） |
| `--model` | 換模型（預設 `claude-opus-5-5`） |
| `--workers 4` | 同時送出的請求數 |
| `--dry-run` | 不呼叫 API，只檢查書能不能正確處理、要翻多少字 |

## 不用 API 金鑰：在 Claude 對話裡翻譯

```bash
python epub_translate.py book.epub --export segments.json
```

把 `segments.json`（每段 `{"id", "text"}`）交給對話中的 Claude 翻譯，譯文存成 `{"id": "譯文", ...}` 的 JSON 檔（可以分成很多個檔），再匯入組回 EPUB：

```bash
python epub_translate.py book.epub --import part1.json part2.json
```

匯入的譯文會存進快取，可以分很多次匯入；再跑一次 `--export` 只會匯出還沒翻的段落。

## 運作方式

- 讀取 OPF 清單裡所有 XHTML 章節、目錄（nav / NCX）與書名。
- 以段落為單位翻譯；粗體、連結等格式會轉成 `<t0>…</t0>` 佔位標記讓模型保留，翻完再還原成原本的標籤。`<pre>`、`<code>`、SVG、數學式不翻。
- 每批約 6000 字元一起送出，並讓模型回報新出現的人名地名，累積成術語表，讓整本書譯名一致。
- 譯文即時存到 `輸出檔名.cache.json`。中途中斷或有段落失敗，**重新執行同一指令**就會從斷點繼續，已翻好的不會重複付費。

## 費用估算

一本 10 萬字的英文書，大約 15–25 萬 tokens 輸入／輸出，用 `claude-opus-5-5`（$4 / $20 每百萬 tokens）約 US$5–10。先跑 `--dry-run` 可以看到總字元數。
