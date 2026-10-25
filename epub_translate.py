#!/usr/bin/env python3
"""EPUB 英文 → 中文翻譯器（使用 Claude API）。

用法：
    python epub_translate.py book.epub                 # 輸出 book.zh-TW.epub（繁體中文）
    python epub_translate.py book.epub --bilingual     # 中英對照
    python epub_translate.py book.epub --lang zh-CN    # 簡體中文
    python epub_translate.py book.epub --dry-run       # 不呼叫 API，只檢查流程與字數
"""
import argparse
import copy
import hashlib
import html.entities
import json
import posixpath
import re
import sys
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import unquote

from lxml import etree

DC_NS = "http://purl.org/dc/elements/1.1/"
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# 可翻譯的區塊元素：只翻譯「內部不再包含其他區塊」的最內層區塊
BLOCK_TAGS = {
    "p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "dt", "dd", "td", "th",
    "caption", "figcaption", "blockquote", "div", "section", "article", "aside",
    "header", "footer", "address", "center", "summary", "legend",
}
# 雙語模式下，這些元素的譯文放在元素內部（換行後接中文），避免破壞表格／清單結構
INNER_BILINGUAL_TAGS = {"li", "dt", "dd", "td", "th", "caption", "figcaption", "summary", "legend"}
# 這些元素裡面的內容不翻譯
SKIP_TAGS = {"head", "pre", "script", "style", "svg", "math", "code"}
# 行內元素中須原封不動保留的（整個複製回去）
ATOMIC_TAGS = {"svg", "math", "code", "kbd", "samp", "var", "script", "style", "img", "br", "hr"}

LANG_NAMES = {"zh-TW": "繁體中文（台灣用語）", "zh-CN": "简体中文"}
QUOTE_STYLE = {"zh-TW": "「」與『』", "zh-CN": "“”与‘’"}

PLACEHOLDER_RE = re.compile(r"^t(\d+)$")
TAG_STRIP_RE = re.compile(r"</?t\d+/?>")
BARE_AMP_RE = re.compile(r"&(?!#\d+;|#x[0-9a-fA-F]+;|\w+;)")
ENTITY_RE = re.compile(rb"&([A-Za-z][A-Za-z0-9]*);")
XML_ENTITIES = {b"amp", b"lt", b"gt", b"quot", b"apos"}


def local(tag):
    return etree.QName(tag).localname if isinstance(tag, str) else None


def esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def has_english(text):
    return re.search(r"[A-Za-z]{2,}", TAG_STRIP_RE.sub("", text)) is not None


# ---------------------------------------------------------------------------
# 區塊 <-> 佔位標記文字
# ---------------------------------------------------------------------------

def encode(el, elems):
    """把元素內容轉成 `文字<t0>粗體</t0><t1/>` 形式，原始行內元素依序存入 elems。"""
    out = esc(el.text)
    for child in el:
        idx = len(elems)
        elems.append(child)
        if not isinstance(child.tag, str) or local(child.tag) in ATOMIC_TAGS or (len(child) == 0 and not child.text):
            out += f"<t{idx}/>"
        else:
            out += f"<t{idx}>{encode(child, elems)}</t{idx}>"
        out += esc(child.tail)
    return out


def decode_into(target, translated, elems, strip_ids=False):
    """把譯文（含佔位標記）還原成真正的元素，寫入 target（會清空 target 原內容）。"""
    try:
        root = etree.fromstring(f"<root>{BARE_AMP_RE.sub('&amp;', translated)}</root>".encode("utf-8"))
        for node in root.iterdescendants():
            m = PLACEHOLDER_RE.match(node.tag) if isinstance(node.tag, str) else None
            if not m or int(m.group(1)) >= len(elems):
                raise ValueError(f"unexpected tag {node.tag!r}")
    except (etree.XMLSyntaxError, ValueError):
        # 標記壞掉時退而求其次：只放純文字
        for child in list(target):
            target.remove(child)
        target.text = html.unescape(TAG_STRIP_RE.sub("", translated))
        return False

    for child in list(target):
        target.remove(child)
    target.text = root.text
    _build(target, root, elems, strip_ids)
    return True


def _build(dst, src, elems, strip_ids):
    for node in src:
        orig = elems[int(node.tag[1:])]
        if not isinstance(orig.tag, str) or local(orig.tag) in ATOMIC_TAGS:
            new = copy.deepcopy(orig)
            dst.append(new)
        else:
            attrib = {k: v for k, v in orig.attrib.items() if not (strip_ids and k == "id")}
            new = etree.SubElement(dst, orig.tag, attrib=attrib)
            new.text = node.text
            _build(new, node, elems, strip_ids)
        if strip_ids and isinstance(new.tag, str):
            for e in new.iter():
                e.attrib.pop("id", None)
        new.tail = node.tail


# ---------------------------------------------------------------------------
# 翻譯引擎
# ---------------------------------------------------------------------------

class ClaudeTranslator:
    def __init__(self, model, effort, lang, book_title, glossary):
        import anthropic

        self.anthropic = anthropic
        self.client = anthropic.Anthropic(max_retries=5)
        self.model = model
        self.effort = effort
        self.glossary = glossary
        self.lock = threading.Lock()
        self.usage = {"input": 0, "output": 0}
        name = LANG_NAMES[lang]
        self.system = f"""你是專業的英翻中書籍譯者，負責把英文書籍內容翻譯成{name}。書名：{book_title}

翻譯要求：
- 譯文要自然流暢，讀起來像中文原創作品，保留作者的語氣與文風；忠實完整，不增譯、不刪減、不加註解。
- 輸入片段可能含有 <t0>…</t0> 或 <t3/> 這類佔位標記，代表粗體、斜體、連結、圖片等格式。譯文中必須保留每一個標記，編號不變，並包住對應的譯文詞語；不可新增標記。保留 &amp; &lt; &gt; 等 XML 跳脫字元。
- 程式碼、網址、電子郵件，以及非英文的內容保持原樣。
- 使用中文全形標點，引號用{QUOTE_STYLE[lang]}。
- 每個片段依 id 分別翻譯，所有片段都要回傳。
- 人名、地名、專有名詞須與提供的術語表一致。另外把這批新出現的專有名詞（人名、地名、組織、自創名詞）及你採用的譯名放進 glossary 回傳；沒有就回傳空陣列。"""

    def translate(self, texts):
        with self.lock:
            glossary = dict(list(self.glossary.items())[-300:])
        payload = {
            "glossary": [{"en": k, "zh": v} for k, v in glossary.items()],
            "segments": [{"id": str(i), "text": t} for i, t in enumerate(texts)],
        }
        schema = {
            "type": "object",
            "properties": {
                "translations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"id": {"type": "string"}, "text": {"type": "string"}},
                        "required": ["id", "text"],
                        "additionalProperties": False,
                    },
                },
                "glossary": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"en": {"type": "string"}, "zh": {"type": "string"}},
                        "required": ["en", "zh"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["translations", "glossary"],
            "additionalProperties": False,
        }
        with self.client.beta.messages.stream(
            model=self.model,
            max_tokens=64000,
            betas=[FALLBACK_BETA],
            fallbacks="default",
            system=self.system,
            output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        ) as stream:
            msg = stream.get_final_message()

        with self.lock:
            self.usage["input"] += msg.usage.input_tokens
            self.usage["output"] += msg.usage.output_tokens

        if msg.stop_reason == "max_tokens" and len(texts) > 1:
            mid = len(texts) // 2
            return self.translate(texts[:mid]) + self.translate(texts[mid:])
        if msg.stop_reason in ("refusal", "max_tokens"):
            print(f"  ! 這批 {len(texts)} 段未翻譯（stop_reason={msg.stop_reason}），保留原文", file=sys.stderr)
            return [None] * len(texts)

        data = json.loads(next(b.text for b in msg.content if b.type == "text"))
        by_id = {t["id"]: t["text"] for t in data["translations"]}
        with self.lock:
            for g in data["glossary"]:
                self.glossary.setdefault(g["en"], g["zh"])
        return [by_id.get(str(i)) for i in range(len(texts))]


class DryRunTranslator:
    """不呼叫 API；在原文前加上標記，用來驗證 EPUB 結構處理是否正確。"""

    usage = {"input": 0, "output": 0}

    def __init__(self, glossary):
        self.glossary = glossary

    def translate(self, texts):
        return ["〔譯〕" + t for t in texts]


# ---------------------------------------------------------------------------
# EPUB 處理
# ---------------------------------------------------------------------------

def parse_xml(data):
    # XHTML 常用的 HTML 具名實體（&nbsp; 等）XML 解析器不認得，先轉成數字實體
    def repl(m):
        name = m.group(1)
        if name in XML_ENTITIES or name.decode() not in html.entities.name2codepoint:
            return m.group(0)
        return b"&#%d;" % html.entities.name2codepoint[name.decode()]

    parser = etree.XMLParser(recover=True, resolve_entities=False, no_network=True, remove_blank_text=False)
    return etree.fromstring(ENTITY_RE.sub(repl, data), parser).getroottree()


def collect_blocks(root):
    blocks = []
    for el in root.iter():
        if local(el.tag) not in BLOCK_TAGS:
            continue
        if any(local(a.tag) in SKIP_TAGS for a in el.iterancestors()):
            continue
        if any(local(d.tag) in BLOCK_TAGS or local(d.tag) in {"pre", "svg", "math"} for d in el.iterdescendants()):
            continue
        blocks.append(el)
    return blocks


class Segment:
    def __init__(self, el, kind):
        self.el = el
        self.kind = kind  # "block" 或 "plain"（NCX 目錄、書名）
        self.elems = []
        self.source = encode(el, self.elems)


def find_opf(files):
    container = etree.fromstring(files["META-INF/container.xml"])
    rootfile = container.find(".//{*}rootfile")
    return rootfile.get("full-path")


class Cache:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.data = {"segments": {}, "glossary": {}}
        if path.exists():
            self.data = json.loads(path.read_text(encoding="utf-8"))

    def save(self):
        with self.lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)


def make_batches(texts, max_chars, max_items=40):
    batch, size = [], 0
    for t in texts:
        if batch and (size + len(t) > max_chars or len(batch) >= max_items):
            yield batch
            batch, size = [], 0
        batch.append(t)
        size += len(t)
    if batch:
        yield batch


def translate_epub(args):
    src = Path(args.input)
    out = Path(args.output) if args.output else src.with_name(f"{src.stem}.{args.lang}{'.bilingual' if args.bilingual else ''}.epub")
    cache = Cache(out.with_name(out.name + ".cache.json"))

    with zipfile.ZipFile(src) as zf:
        infos = zf.infolist()
        files = {i.filename: zf.read(i.filename) for i in infos}

    opf_path = find_opf(files)
    opf_dir = posixpath.dirname(opf_path)
    opf_tree = parse_xml(files[opf_path])
    opf = opf_tree.getroot()

    trees = {opf_path: opf_tree}
    segments = []

    titles = opf.findall(f".//{{{DC_NS}}}title")
    book_title = titles[0].text if titles else src.stem
    for t in titles:
        if t.text and has_english(t.text):
            segments.append(Segment(t, "plain"))
    for lang_el in opf.findall(f".//{{{DC_NS}}}language"):
        lang_el.text = args.lang

    for item in opf.iterfind(".//{*}manifest/{*}item"):
        href = posixpath.normpath(posixpath.join(opf_dir, item.get("href", "")))
        href = unquote(href)
        media = item.get("media-type", "")
        if href not in files:
            continue
        if media in ("application/xhtml+xml", "text/html"):
            tree = parse_xml(files[href])
            trees[href] = tree
            for el in collect_blocks(tree.getroot()):
                seg = Segment(el, "block")
                if has_english(seg.source):
                    segments.append(seg)
        elif media == "application/x-dtbncx+xml":
            tree = parse_xml(files[href])
            trees[href] = tree
            for el in tree.getroot().iterfind(".//{*}navLabel/{*}text"):
                if el.text and has_english(el.text):
                    segments.append(Segment(el, "plain"))

    key = lambda s: hashlib.sha1(f"{args.lang}\0{s}".encode("utf-8")).hexdigest()
    done = cache.data["segments"]
    pending = list(dict.fromkeys(s.source for s in segments if key(s.source) not in done))
    total_chars = sum(len(t) for t in pending)
    print(f"書名：{book_title}")
    print(f"共 {len(segments)} 個段落，其中 {len(pending)} 段待翻譯（約 {total_chars:,} 字元）")

    glossary = cache.data["glossary"]
    if args.glossary:
        glossary.update(json.loads(Path(args.glossary).read_text(encoding="utf-8")))

    if args.export:
        # 匯出待翻段落給人工／對話中的 Claude 翻譯；id 取快取鍵的前 10 碼
        export = [{"id": key(t)[:10], "text": t} for t in pending]
        Path(args.export).write_text(json.dumps(export, ensure_ascii=False, indent=0), encoding="utf-8")
        print(f"已匯出 {len(export)} 段到 {args.export}")
        return

    if args.import_files:
        by_prefix = {key(s.source)[:10]: key(s.source) for s in segments}
        imported = 0
        for f in args.import_files:
            for short_id, zh in json.loads(Path(f).read_text(encoding="utf-8")).items():
                if short_id in by_prefix and zh:
                    done[by_prefix[short_id]] = zh
                    imported += 1
        cache.save()
        print(f"已匯入 {imported} 段譯文")
        translator = None
    elif args.dry_run:
        translator = DryRunTranslator(glossary)
    else:
        translator = ClaudeTranslator(args.model, args.effort, args.lang, book_title, glossary)

    batches = list(make_batches(pending, args.batch_chars)) if translator else []
    finished = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(translator.translate, b): b for b in batches}
        for fut in as_completed(futures):
            batch = futures[fut]
            try:
                results = fut.result()
            except Exception as e:  # 單批失敗不影響其他批；重跑時會從快取續跑
                print(f"  ! 一批翻譯失敗：{type(e).__name__}: {e}", file=sys.stderr)
                results = [None] * len(batch)
            with cache.lock:
                for s, r in zip(batch, results):
                    if r:
                        done[key(s)] = r
            if not args.dry_run:
                cache.save()
            finished += 1
            print(f"  [{finished}/{len(batches)}] 批完成")

    missing = 0
    fallbacks = 0
    for seg in segments:
        zh = done.get(key(seg.source))
        if not zh:
            missing += 1
            continue
        if seg.kind == "plain":
            seg.el.text = html.unescape(TAG_STRIP_RE.sub("", zh))
            continue
        el = seg.el
        if not args.bilingual:
            ok = decode_into(el, zh, seg.elems)
        elif local(el.tag) in INNER_BILINGUAL_TAGS:
            ns = etree.QName(el.tag).namespace
            tag = (lambda n: f"{{{ns}}}{n}" if ns else n)
            span = etree.Element(tag("span"), attrib={"class": "zh-translation", "lang": args.lang})
            ok = decode_into(span, zh, seg.elems, strip_ids=True)
            etree.SubElement(el, tag("br"))
            el.append(span)
        else:
            dup = etree.Element(el.tag, attrib={k: v for k, v in el.attrib.items() if k != "id"})
            dup.set("lang", args.lang)
            cls = (dup.get("class", "") + " zh-translation").strip()
            dup.set("class", cls)
            ok = decode_into(dup, zh, seg.elems, strip_ids=True)
            dup.tail = el.tail
            el.tail = "\n"
            el.addnext(dup)
        fallbacks += not ok

    if not args.bilingual:
        for name, tree in trees.items():
            root = tree.getroot()
            if local(root.tag) == "html":
                root.set("lang", args.lang)
                root.set(XML_LANG, args.lang)

    for name, tree in trees.items():
        files[name] = etree.tostring(tree, xml_declaration=True, encoding="utf-8")

    with zipfile.ZipFile(out, "w") as zf:
        # EPUB 規定 mimetype 必須是第一個檔案且不壓縮
        if "mimetype" in files:
            zf.writestr(zipfile.ZipInfo("mimetype"), files["mimetype"], compress_type=zipfile.ZIP_STORED)
        for info in infos:
            if info.filename == "mimetype":
                continue
            zf.writestr(info.filename, files[info.filename], compress_type=zipfile.ZIP_DEFLATED)

    if not args.dry_run:
        cache.save()
    print(f"\n完成：{out}")
    if missing:
        print(f"  {missing} 段沒有譯文（保留原文），重新執行同一指令即可補翻")
    if fallbacks:
        print(f"  {fallbacks} 段格式標記無法還原，已改為純文字")
    if translator and translator.usage["input"]:
        print(f"  API 用量：輸入 {translator.usage['input']:,} tokens，輸出 {translator.usage['output']:,} tokens")


def main():
    p = argparse.ArgumentParser(description="EPUB 英文 → 中文翻譯器（Claude API）")
    p.add_argument("input", help="要翻譯的 .epub 檔")
    p.add_argument("-o", "--output", help="輸出檔名（預設：原檔名.zh-TW.epub）")
    p.add_argument("--lang", choices=sorted(LANG_NAMES), default="zh-TW", help="zh-TW 繁體（預設）或 zh-CN 簡體")
    p.add_argument("--bilingual", action="store_true", help="中英對照：保留原文，譯文接在後面")
    p.add_argument("--model", default="claude-opus-5-5", help="Claude 模型（預設 claude-opus-5-5）")
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], default="medium",
                   help="思考深度，越高越慢越貴（預設 medium）")
    p.add_argument("--glossary", help="自訂術語表 JSON 檔，例如 {\"Harry\": \"哈利\"}")
    p.add_argument("--workers", type=int, default=4, help="同時送出的請求數（預設 4）")
    p.add_argument("--batch-chars", type=int, default=6000, help="每次請求最多送出的字元數（預設 6000）")
    p.add_argument("--dry-run", action="store_true", help="不呼叫 API，只測試流程")
    p.add_argument("--export", metavar="FILE", help="不翻譯，把待翻段落匯出成 JSON（給對話中的 Claude 翻）")
    p.add_argument("--import", dest="import_files", nargs="+", metavar="FILE",
                   help="匯入譯文 JSON（{id: 譯文}，可多個檔），再組成 EPUB，不呼叫 API")
    args = p.parse_args()

    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    translate_epub(args)


if __name__ == "__main__":
    main()
