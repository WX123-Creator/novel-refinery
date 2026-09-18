# -*- coding: utf-8 -*-
import io, sys, os, zipfile, re
os.chdir(r"D:\网文工具\novel-refinery")
sys.stdout.reconfigure(encoding="utf-8")

BOOK = "颠倒世界从中奖千亿开始"
DOCS = [
    r"C:\Users\W\Coze\Drive\扣子\1颠倒世界_1789162391670_f43z.docx",
    r"C:\Users\W\Coze\Drive\扣子\2试炼之地_1789162391670_6oji.docx",
    r"C:\Users\W\Coze\Drive\扣子\3中奖千亿_1789162391670_lihy.docx",
]


def docx_text(p):
    with zipfile.ZipFile(p) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab[^>]*/>", "\t", xml)
    return re.sub(r"<[^>]+>", "", xml)


text = "\n\n".join(docx_text(p).strip() for p in DOCS)
print(f"合并正文长度: {len(text)} 字，书名: {BOOK}")

from novel_processor import process_novel


def prog(title, meta=None):
    print(f"  进度: {title}")


def st(m):
    print("  ·", m)


res = process_novel(text=text, tier_key="balance", model_id="glm-4.5-air",
                    progress_callback=prog, status_callback=st,
                    budget_check=True, book_name=BOOK)
print("=== 提炼完成 ===")
print("导出文件:")
for k, v in (res.get("files") or {}).items():
    print(f"  {k}: {v}")
print("cost_summary:", res.get("cost_summary"))
print("total_tokens:", res.get("total_tokens"))