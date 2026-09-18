"""
小说提炼工坊 - 核心处理逻辑（节拍级·完整序列故事版）
==========================================
功能（聚焦故事，只提炼故事，已移除人物/设定/好词好句等其它产物）：
1. 正则识别分卷/分章
2. 按卷把连续章节聚合成「候选序列段」，LLM 提炼成完整序列故事，
   层级严格遵循：全书 ⊃ 卷 ⊃ 序列故事 ⊃ 场景 ⊃ 节拍(动作→反应)
3. 每个序列标注：
   - 原著章节范围(chapters)：整个序列覆盖的起止章
   - 故事架构(arc)：按真实节奏动态标注（trend 走向 + pulses 节点），
     不强制六段式；常见如低→高，内含小推进、小高潮等节点，带力度级别(大/中/小)
4. 卷级串接生成卷故事；全书总览生成全书故事
5. 导出 1 份「全书故事结构」报告（全书→卷→序列(章范围+架构)→场景→节拍 逐层嵌套）
6. 断点续跑支持；Token 预算检查
"""

import json
import os
import re
import time
from typing import Optional

from config import (
    MAX_CHARS_PER_BLOCK,
    MAX_TOTAL_TOKENS,
    CHECKPOINT_DIR,
    CHECKPOINT_FILE,
    estimate_tokens,
)
from llm_service import LLMService


def book_output_dir(book_name: str, base: str = "output") -> str:
    """按书名返回导出子目录，非法字符替换为 _。"""
    safe = re.sub(r'[\\/:*?"<>|]', "_", book_name.strip() or "未命名")
    return os.path.join(base, safe)


# ============================================================
# 正则分割
# ============================================================

# 中文数字映射
CN_NUM_MAP = {
    "零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
    "十": 10, "百": 100, "千": 1000,
}


def _cn_to_int(cn_str: str) -> int:
    """中文数字转阿拉伯数字"""
    result = 0
    current = 0
    for char in cn_str:
        if char in CN_NUM_MAP:
            val = CN_NUM_MAP[char]
            if val >= 10:
                if current == 0:
                    current = 1
                result += current * val
                current = 0
            else:
                current = val
    result += current
    return result if result != 0 else 1


def split_novel(text: str) -> list[dict]:
    """
    将小说文本按卷/章分割成块
    返回: [{"title": "...", "content": "...", "type": "volume|chapter|section", "seq": 1}, ...]
    """
    text = text.strip()
    if not text:
        return []

    blocks = []
    lines = text.split("\n")

    # 尝试识别卷标记
    vol_pattern = re.compile(r"^\s*第\s*(\d+|[一二三四五六七八九十百千零]+)\s*卷\s*[：:]?\s*(.*)$")
    chap_pattern = re.compile(r"^\s*第\s*(\d+|[一二三四五六七八九十百千零]+)\s*章\s*[：:.]?\s*(.*)$")
    section_pattern = re.compile(r"^\s*第\s*(\d+|[一二三四五六七八九十百千零]+)\s*[节回部]\s*[：:.]?\s*(.*)$")

    # 特殊标题
    special_titles = re.compile(
        r"^\s*(序章|序幕|尾声|后记|番外|引子|楔子|前言|序言|终章|第零章|第零卷)\s*[：:.]?\s*(.*)$"
    )

    current_block = {"title": "开头", "content": "", "type": "section", "seq": 0}
    blocks.append(current_block)
    has_volume = False
    has_chapter = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            current_block["content"] += "\n"
            continue

        # 检查是否是卷标题
        vm = vol_pattern.match(stripped)
        if vm:
            has_volume = True
            num = int(vm.group(1)) if vm.group(1).isdigit() else _cn_to_int(vm.group(1))
            title_text = vm.group(2).strip()
            current_block = {
                "title": f"第{num}卷 {title_text}" if title_text else f"第{num}卷",
                "content": stripped + "\n",
                "type": "volume",
                "seq": num,
            }
            blocks.append(current_block)
            continue

        # 检查是否是章标题
        cm = chap_pattern.match(stripped)
        if cm:
            has_chapter = True
            num = int(cm.group(1)) if cm.group(1).isdigit() else _cn_to_int(cm.group(1))
            title_text = cm.group(2).strip()
            current_block = {
                "title": f"第{num}章 {title_text}" if title_text else f"第{num}章",
                "content": stripped + "\n",
                "type": "chapter",
                "seq": num,
            }
            blocks.append(current_block)
            continue

        # 特殊标题
        sm = special_titles.match(stripped)
        if sm:
            current_block = {
                "title": stripped,
                "content": stripped + "\n",
                "type": "chapter",
                "seq": 0,
            }
            blocks.append(current_block)
            continue

        # 小节
        sm2 = section_pattern.match(stripped)
        if sm2 and (has_volume or has_chapter):
            num = int(sm2.group(1)) if sm2.group(1).isdigit() else _cn_to_int(sm2.group(1))
            title_text = sm2.group(2).strip()
            current_block = {
                "title": f"第{num}节 {title_text}" if title_text else f"第{num}节",
                "content": stripped + "\n",
                "type": "section",
                "seq": num,
            }
            blocks.append(current_block)
            continue

        current_block["content"] += line + "\n"

    # 如果只有一个大块，说明没识别出结构，返回原始文本作为单块
    if len(blocks) == 1 and blocks[0]["title"] == "开头":
        blocks[0] = {"title": "全文", "content": text, "type": "volume", "seq": 1}

    # 按最大字符数进一步拆分过长的块
    final_blocks = []
    for block in blocks:
        content = block["content"]
        if len(content) > MAX_CHARS_PER_BLOCK:
            # 按段落拆分子块
            sub_blocks = split_large_block(block)
            final_blocks.extend(sub_blocks)
        else:
            final_blocks.append(block)

    return final_blocks


def split_large_block(block: dict) -> list[dict]:
    """将过长的块按段落拆分为多个子块"""
    paragraphs = block["content"].split("\n\n")
    sub_blocks = []
    current_content = ""
    sub_idx = 1

    for para in paragraphs:
        if len(current_content) + len(para) > MAX_CHARS_PER_BLOCK and current_content:
            sub_blocks.append({
                "title": f"{block['title']}（续{sub_idx}）",
                "content": current_content.strip(),
                "type": block["type"],
                "seq": block["seq"],
                "parent": block["title"],
            })
            current_content = para + "\n\n"
            sub_idx += 1
        else:
            current_content += para + "\n\n"

    if current_content.strip():
        if sub_idx > 1:
            sub_blocks.append({
                "title": f"{block['title']}（续{sub_idx}）",
                "content": current_content.strip(),
                "type": block["type"],
                "seq": block["seq"],
                "parent": block["title"],
            })
        else:
            sub_blocks.append(block)

    return sub_blocks


# ============================================================
# 候选序列段构建（跨章聚集，保证序列完整）
# ============================================================

def build_sequence_batches(blocks: list[dict]) -> tuple[list[list[dict]], list[str]]:
    """
    把连续的内容块（章/节）按卷聚合成「候选序列段」。
    - 卷标记只做分隔，不属于任何段。
    - 同一卷内连续块按累计字数聚合为一个候选序列段（目标：一个完整序列的量）。
    返回 (batches, vol_titles)：batches[i] 是若干连续块，vol_titles[i] 是该段所属卷标题。
    """
    batches: list[list[dict]] = []
    vol_titles: list[str] = []
    cur: list[dict] = []
    cur_vol = ""
    cur_chars = 0
    for b in blocks:
        if b["type"] == "volume":
            if cur:
                batches.append(cur)
                vol_titles.append(cur_vol)
                cur = []
                cur_chars = 0
            cur_vol = b["title"]
            continue
        cur.append(b)
        cur_chars += len(b["content"] or "")
        if cur_chars >= MAX_CHARS_PER_BLOCK:
            batches.append(cur)
            vol_titles.append(cur_vol)
            cur = []
            cur_chars = 0
    if cur:
        batches.append(cur)
        vol_titles.append(cur_vol)
    # 空卷标题（开头/无卷）归到"开头"或全书
    for i, vt in enumerate(vol_titles):
        if not vt:
            vol_titles[i] = "开头" if i == 0 else "全书"
    return batches, vol_titles


def _batch_range(batch: list[dict]) -> str:
    """返回候选序列段的章节范围，如：第3章~第7章"""
    titles = []
    for b in batch:
        t = b.get("title", "")
        # 忽略"（续N）"子块后缀，取主章名
        if t and t not in titles:
            titles.append(t)
    if not titles:
        return ""
    if len(titles) == 1:
        return titles[0]
    return f"{titles[0]} ~ {titles[-1]}"


# ============================================================
# 提示词模板（节拍级·完整序列故事）
# ============================================================

SYSTEM_EXTRACT = """你是一位专业的叙事结构分析师。请把给定的小说文本，按「序列故事 ⊃ 场景 ⊃ 节拍(动作→反应)」提炼成故事，并标注每个序列覆盖的原著章节范围与真实故事节奏。

父子层级规则（必须严格遵守）：
- 序列故事(sequence)：一个**完整的故事情节单元**，跨若干章，遵循 目标→冲突→推进→结果 的完整推进。一个序列故事由 2~5 个场景构成。
- 场景(scene)：同一时间、同一地点、同一组角色对峙的一个连贯冲突单元。
- 节拍(beat)：场景内推动情节的最小节奏单元，每拍都是「某方的一个动作 → 引发另一方的反应」。

节奏标注规则：
- 序列的 **章节范围(chapters)** 标注整个序列覆盖的原著起止章（开场章~收束章）。
- **故事架构(arc)** 按本序列**真实的节奏**动态标注，不要死套固定流程（不必都有低谷/转折/高潮六段）。
  常见形态举例：低→高（内含小推进、小高潮）；高→低；低→高→回落；平缓蓄力→小高潮→收束 等。
  用 trend 描述整体走向，用 pulses 逐个列出关键的节奏节点，每节点给出所在章节、类型与力度级别(大/中/小)。

请严格按照以下 JSON 格式输出，不要添加任何额外说明，不要用 markdown 包裹：

{
  "sequences": [
    {
      "chapters": "整个序列覆盖的原著章节范围，如：第3章~第7章；若只在一章内则写：第5章",
      "story": "本序列故事一句话概括（承接上文→发生什么→导向下文）",
      "goal": "本序列的故事目标",
      "conflict": "本序列的核心冲突/阻力",
      "turn": "本序列的关键转折（一句话，若无重大转折可写\"平稳推进\"）",
      "result": "本序列的结果与留给下文的钩子",
      "arc": {
        "trend": "本序列整体走向形态，如：低→高（小推进→小高潮）、低→高→回落、平缓蓄力→爆发→收束 等",
        "pulses": [
          {
            "chapter": "该节奏节点所处的原著章节，如：第4章",
            "type": "节点类型，按真实节奏选：低谷/平缓/推进/转折/小高潮/高潮/收束 等。一段序列常见只有少量节点（如小推进、小高潮），不要硬凑",
            "level": "力度级别：大/中/小",
            "note": "此刻局势或情绪如何，发生了什么"
          }
        ]
      },
      "scenes": [
        {
          "setting": "时间·地点·视角人物",
          "summary": "本场景一句话概括",
          "objective": "本场景主角目标",
          "conflict": "本场景冲突",
          "disaster": "场景结尾的转折/意外（若无重大转折可写\"平稳过渡\"）",
          "beats": [
            {"action": "动作：谁做了什么", "reaction": "反应：对方/另一方如何回应"}
          ]
        }
      ]
    }
  ]
}

注意：
- sequences 是数组，通常 1 个；若文本内含明显相互独立的多个完整情节单元可返回 2 个（各标各自章节范围）。
- 优先把文本组织成一个**完整的序列故事**，不要人为切成半截；若确有 2 个完整序列再拆。
- **每个序列的 scenes 必须是 2~5 个场景**。
- 每个场景的 beats 给出 4~8 条，每条一句话概括，必须体现"动作→反应"。
- arc 的 trend 与 pulses 必须贴合原文真实节奏，pulses 数量按实际来（少则 2 个，多则可含多个小节点），每个节点都要具体并注明所在章节与力度级别。
- 信息必须来自原文，不虚构、不添加原文没有的内容。
- 如本段无有效情节，仅返回 {"sequences": [{"story": "本段无有效情节", "scenes": []}]}"""

SYSTEM_AGGREGATE = """你是一位专业的长篇叙事编辑。请根据一部作品的某一卷中，多个序列故事（sequence）的概要，提炼成该卷的「卷故事」。

请按以下格式输出（不用 JSON，用中文段落）：

**卷故事一句话**：本卷在全书中的位置与作用（一句话）

**卷目标**：（本卷主角/各方要达成的目标）

**卷冲突**：（本卷最主要的矛盾与阻力）

**卷走向**：（按顺序概述本卷各序列故事如何推进：开头承接→过程起伏→关键转折→高潮→结尾）

**卷结局**：（本卷结尾的状态、悬念与对下卷的接续）

注意：保持客观精炼，严格基于给定的序列故事概要，不添加原文未出现的内容。"""

SYSTEM_OVERVIEW = """你是一位专业的文学评论家。请根据一部作品各卷的「卷故事」概要，生成完整的「全书故事」总览。

请按以下格式输出（不用 JSON，用中文段落）：

# 全书故事

## 一、一句话内核
（用一句话概括全书在讲什么）

## 二、主题与核心矛盾
（全书的主旨，以及贯穿始终的核心矛盾/主线冲突）

## 三、总体结构
（全书如何推进：开场布局→过程升温→几处大转折→高潮→结局。按卷串联描述）

## 四、大结局与最终落点
（全书结尾如何收束，角色/世界达成何种状态）

注意：保持客观专业，严格基于给定的卷故事内容，不添加原文未出现的内容。"""


# ============================================================
# 处理函数
# ============================================================

def extract_batch_content(
    batch: list[dict],
    llm_service: LLMService,
    progress_callback: Optional[callable] = None,
) -> dict:
    """对单个候选序列段进行 LLM 提炼（完整序列故事，含章节范围与动态节奏）"""
    title = _batch_range(batch)
    content = "\n".join(b["content"] for b in batch)

    # 截断过长的内容（按 token 估算）
    estimated_tokens = estimate_tokens(content)
    if estimated_tokens > 8000:
        max_chars = int(8000 / 1.5)
        content = content[:max_chars] + "\n\n[内容较长，已截断]"

    user_prompt = f"""请分析以下小说内容（本段覆盖原著章节范围：{title}）：

{content}

请按要求的 JSON 格式输出故事结构分析结果，并将每个序列的 chapters 标注为原著实际的章节范围。"""

    result_text, meta = llm_service.invoke(
        system_prompt=SYSTEM_EXTRACT,
        user_prompt=user_prompt,
        temperature=0.3,
    )

    # 尝试解析 JSON
    result = {"title": title, "meta": meta}
    try:
        json_str = result_text
        if "```json" in json_str:
            json_str = json_str.split("```json")[1].split("```")[0]
        elif "```" in json_str:
            json_str = json_str.split("```")[1].split("```")[0]
        parsed = json.loads(json_str.strip())
        seqs = parsed.get("sequences") or []
        norm = []
        for s in seqs:
            if not isinstance(s, dict):
                continue
            seq = s.get("sequence") or {}
            scene_list = s.get("scenes") if isinstance(s.get("scenes"), list) else []
            arc = s.get("arc") or {}
            if not isinstance(arc, dict):
                arc = {}
            pulses = arc.get("pulses") if isinstance(arc.get("pulses"), list) else []
            norm_arc = {
                "trend": arc.get("trend", ""),
                "pulses": [
                    {
                        "chapter": (p.get("chapter") or "") if isinstance(p, dict) else "",
                        "type": (p.get("type") or "") if isinstance(p, dict) else "",
                        "level": (p.get("level") or "") if isinstance(p, dict) else "",
                        "note": (p.get("note") or "") if isinstance(p, dict) else "",
                    }
                    for p in pulses
                ],
            }
            norm.append({
                "sequence": {
                    "chapters": seq.get("chapters", ""),
                    "story": seq.get("story", ""),
                    "goal": seq.get("goal", ""),
                    "conflict": seq.get("conflict", ""),
                    "turn": seq.get("turn", ""),
                    "result": seq.get("result", ""),
                },
                "arc": norm_arc,
                "scenes": [x for x in scene_list if isinstance(x, dict)],
            })
        result["sequences"] = norm
    except (json.JSONDecodeError, IndexError):
        # 非 JSON 格式，直接存储文本
        result["sequences"] = [{"sequence": {"story": result_text[:200]}, "arc": {"trend": "", "pulses": []}, "scenes": []}]

    if progress_callback:
        progress_callback(title, meta)

    return result


def aggregate_volume(
    title: str,
    block_results: list[dict],
    llm_service: LLMService,
) -> dict:
    """
    汇总同一卷/部的多个候选序列段的提炼结果，串成「卷故事」。
    层级：卷 ⊃ 序列故事 ⊃ 场景 ⊃ 节拍。序列/场景/节拍完整保留（不做 LLM 二次压缩），
    卷故事概述由 LLM 生成。
    """
    if not block_results:
        return {"title": title, "vol_story": "无内容", "sequences": []}

    # 每个候选序列段内可能有 1~2 个序列故事，全部按顺序平铺进本卷
    sequences = []
    for br in block_results:
        if not isinstance(br, dict):
            continue
        for item in br.get("sequences") or []:
            if not isinstance(item, dict):
                continue
            seq = item.get("sequence") or {}
            arc = item.get("arc") or {}
            scenes = [s for s in (item.get("scenes") or []) if isinstance(s, dict)]
            seq_hit = {
                "chapters": seq.get("chapters", ""),
                "story": seq.get("story", ""),
                "goal": seq.get("goal", ""),
                "conflict": seq.get("conflict", ""),
                "turn": seq.get("turn", ""),
                "result": seq.get("result", ""),
            }
            sequences.append({"sequence": seq_hit, "arc": arc, "scenes": scenes})

    if len(sequences) <= 1:
        return {"title": title, "vol_story": "", "sequences": sequences}

    # 用 LLM 生成卷故事概述（严格基于各序列的 story/goal/conflict/turn/result）
    combined = ""
    for i, item in enumerate(sequences):
        seq = item["sequence"]
        combined += f"\n### 序列{i+1}（原著 {seq.get('chapters', '-')}）\n"
        combined += f"概述: {seq.get('story', '')}\n"
        combined += f"目标: {seq.get('goal', '')}\n"
        combined += f"冲突: {seq.get('conflict', '')}\n"
        combined += f"转折: {seq.get('turn', '')}\n"
        combined += f"结果: {seq.get('result', '')}\n"

    user_prompt = f"""请将以下某一卷的多个序列故事汇总为一份「卷故事」。

卷标题：{title}

各序列故事：
{combined}

请按要求的格式输出卷故事。"""
    result_text, meta = llm_service.invoke(
        system_prompt=SYSTEM_AGGREGATE,
        user_prompt=user_prompt,
        temperature=0.4,
    )

    return {
        "title": title,
        "vol_story": result_text.strip(),
        "sequences": sequences,
        "meta": meta,
    }


def generate_overview(
    volume_results: list[dict],
    llm_service: LLMService,
) -> str:
    """生成全书故事总览"""
    if not volume_results:
        return "无内容"

    if len(volume_results) == 1:
        # 只有一卷/整本成卷时，尽量基于该卷的序列生成全书总览
        vr = volume_results[0]
        if vr.get("vol_story"):
            combined = vr["vol_story"]
        else:
            seq_texts = "\n".join(f"- {it['sequence'].get('story','')}" for it in vr.get("sequences", [])[:50])
            combined = seq_texts or "无"
        user_prompt = f"请根据以下卷故事内容，生成全书故事总览。\n\n{combined}\n\n请按要求的格式输出全书故事。"
        try:
            result_text, _ = llm_service.invoke(
                system_prompt=SYSTEM_OVERVIEW,
                user_prompt=user_prompt,
                temperature=0.4,
            )
            return result_text.strip()
        except Exception:
            return combined[:1000]

    # 多卷：拼接各卷卷故事概述
    combined = ""
    for i, vr in enumerate(volume_results):
        combined += f"\n## 卷{i+1}: {vr.get('title', '')}\n"
        combined += (vr.get("vol_story", "") or "")[:800] + "\n\n"

    user_prompt = f"""请根据以下各卷故事，生成完整的全书故事总览。

{combined}

请按要求的格式输出全书故事。"""
    result_text, meta = llm_service.invoke(
        system_prompt=SYSTEM_OVERVIEW,
        user_prompt=user_prompt,
        temperature=0.4,
    )

    return result_text.strip()


# ============================================================
# 导出文件（只导出故事结构，节拍级）
# ============================================================

def _fmt_scene(scene: dict) -> str:
    """将单个场景格式化为文本"""
    lines = []
    setting = scene.get("setting", "")
    summary = scene.get("summary", "")
    obj = scene.get("objective", "")
    conf = scene.get("conflict", "")
    dis = scene.get("disaster", "")
    if setting:
        lines.append(f"[场景 · {setting}]")
    if summary:
        lines.append(f"  概述：{summary}")
    if obj:
        lines.append(f"  目标：{obj}")
    if conf:
        lines.append(f"  冲突：{conf}")
    if dis:
        lines.append(f"  转折：{dis}")
    beats = scene.get("beats", [])
    if beats:
        lines.append("  节拍（动作→反应）：")
        for idx, b in enumerate(beats, 1):
            if isinstance(b, dict):
                a = (b.get("action") or "").strip()
                r = (b.get("reaction") or "").strip()
                if a and r:
                    lines.append(f"    {idx}. {a} → {r}")
                elif a:
                    lines.append(f"    {idx}. {a}")
                elif r:
                    lines.append(f"    {idx}. ↔ {r}")
    return "\n".join(lines)


def _fmt_arc(arc: dict) -> str:
    """将故事架构（动态节奏）格式化为多行文本"""
    lines = ["  【故事架构·节奏】"]
    found = False
    if isinstance(arc, dict):
        trend = (arc.get("trend") or "").strip()
        if trend:
            lines.append(f"    走向：{trend}")
            found = True
        pulses = arc.get("pulses")
        if isinstance(pulses, list):
            for p in pulses:
                if not isinstance(p, dict):
                    continue
                chap = (p.get("chapter") or "").strip()
                typ = (p.get("type") or "").strip()
                lvl = (p.get("level") or "").strip()
                note = (p.get("note") or "").strip()
                tag = f"{chap} · {typ}" if chap else typ
                if lvl:
                    tag = f"{tag}({lvl})"
                line = f"    {tag}"
                if note:
                    line += f"：{note}"
                lines.append(line)
                found = True
    return "\n".join(lines) if found else ""


def _fmt_sequence(item: dict, seq_no: int) -> str:
    """将单个序列故事格式化为文本（含章节范围、动态节奏、多个场景与节拍）"""
    seq = item.get("sequence") or {}
    lines = []
    chap = (seq.get("chapters") or "").strip()
    head = f"◇ 序列{seq_no}"
    if chap:
        head += f"（原著 {chap}）"
    lines.append(head)
    if seq.get("story"):
        lines.append(f"  故事：{seq['story']}")
    if seq.get("goal"):
        lines.append(f"  目标：{seq['goal']}")
    if seq.get("conflict"):
        lines.append(f"  冲突：{seq['conflict']}")
    if seq.get("turn"):
        lines.append(f"  转折：{seq['turn']}")
    if seq.get("result"):
        lines.append(f"  结果/钩子：{seq['result']}")
    arc_txt = _fmt_arc(item.get("arc"))
    if arc_txt:
        lines.append(arc_txt)
    scenes = item.get("scenes") or []
    if scenes:
        lines.append("")
        for sc in scenes:
            lines.append(_fmt_scene(sc))
    return "\n".join(lines)


def export_files(
    volume_results: list[dict],
    overview: str,
    output_dir: str,
) -> dict[str, str]:
    """
    导出故事结构报告（全书→卷→序列(章范围+节奏)→场景→节拍 逐层嵌套）。
    只输出故事，已移除人物/设定/好词好句等其它产物。
    返回: {文件名: 文件路径, ...}
    """
    os.makedirs(output_dir, exist_ok=True)
    files = {}

    path = os.path.join(output_dir, "01_全书故事结构.txt")
    with open(path, "w", encoding="utf-8") as f:
        # 1. 全书故事
        f.write("# 全书故事结构\n\n")
        f.write(overview.strip())
        f.write("\n\n")

        # 2. 各卷故事（卷 ⊃ 序列(章范围+节奏) ⊃ 场景 ⊃ 节拍 逐层嵌套）
        f.write("=" * 26 + "\n")
        f.write("各卷故事\n")
        f.write("=" * 26 + "\n\n")
        for vol_idx, vr in enumerate(volume_results, 1):
            f.write(f"\n{'='*20}\n第{vol_idx}卷：{vr.get('title', '')}\n{'='*20}\n")
            vol_story = vr.get("vol_story", "")
            if vol_story:
                f.write("\n【卷故事】\n" + vol_story.strip() + "\n")
            sequences = vr.get("sequences") or []
            if not sequences:
                continue
            f.write("\n【卷内序列故事】\n")
            for seq_no, item in enumerate(sequences, 1):
                f.write("\n" + _fmt_sequence(item, seq_no) + "\n")
            f.write("\n")

    files["全书故事结构"] = path

    return files


# ============================================================
# 检查点（断点续跑）
# ============================================================

def _is_content_filter_err(e):
    """判断是否为内容安全拦截错误（智谱 GLM 的 contentFilter / 1301）"""
    s = str(e)
    return any(k in s for k in ("contentFilter", "1301", "内容安全", "敏感"))


def export_from_blocks(blocks, block_results, output_dir):
    """
    降级导出：出错时不依赖 LLM，直接从已提炼的候选序列段结果聚合生成故事结构报告。
    blocks: 原始分块列表（此处为扁平列表）
    block_results: 与候选序列段对应的提炼结果（可能含 None）
    """
    os.makedirs(output_dir, exist_ok=True)
    valid = []
    for i, br in enumerate(block_results):
        if br is None or not isinstance(br, dict):
            continue
        title = br.get("title", f"段{i+1}")
        for item in br.get("sequences") or []:
            if isinstance(item, dict):
                valid.append((title, item))
    files = {}

    path = os.path.join(output_dir, "01_全书故事结构.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("# 全书故事结构（降级导出）\n\n")
        f.write("【降级导出】AI 全书总览未生成，以下为各序列故事的结构整合。\n\n")
        for i, (title, item) in enumerate(valid, 1):
            seq = item.get("sequence") or {}
            chap = (seq.get("chapters") or "").strip()
            head = f"◇ 序列{i}（原著 {chap}）" if chap else f"◇ 序列{i}"
            f.write(f"\n{'='*18}\n{head}\n{'='*18}\n")
            if seq.get("story"):
                f.write(f"  故事：{seq.get('story','')}\n")
            if seq.get("goal"):
                f.write(f"  目标：{seq.get('goal','')}\n")
            if seq.get("conflict"):
                f.write(f"  冲突：{seq.get('conflict','')}\n")
            if seq.get("turn"):
                f.write(f"  转折：{seq.get('turn','')}\n")
            if seq.get("result"):
                f.write(f"  结果/钩子：{seq.get('result','')}\n")
            arc_txt = _fmt_arc(item.get("arc"))
            if arc_txt:
                f.write(arc_txt + "\n")
            for sc in item.get("scenes") or []:
                if isinstance(sc, dict):
                    f.write("\n" + _fmt_scene(sc) + "\n")
            f.write("\n")
        f.write(f"\n共整合已提炼序列 {len(valid)} 个。\n")
    files["全书故事结构"] = path

    return files


def save_checkpoint(state: dict):
    """保存处理状态到检查点"""
    os.makedirs(CHECKPOINT_DIR, exist_ok=True)
    path = os.path.join(CHECKPOINT_DIR, CHECKPOINT_FILE)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def load_checkpoint() -> Optional[dict]:
    """加载检查点"""
    path = os.path.join(CHECKPOINT_DIR, CHECKPOINT_FILE)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def clear_checkpoint():
    """清除检查点"""
    path = os.path.join(CHECKPOINT_DIR, CHECKPOINT_FILE)
    if os.path.exists(path):
        os.remove(path)


# ============================================================
# 主流程
# ============================================================

def process_novel(
    text: str,
    tier_key: str = "free",
    model_id: Optional[str] = None,
    progress_callback: Optional[callable] = None,
    status_callback: Optional[callable] = None,
    budget_check: bool = True,
    book_name: str = "",
) -> dict:
    """
    完整小说处理流程（节拍级·完整序列故事版）
    Returns: {
        "blocks": [...],
        "block_results": [...],
        "volume_results": [...],
        "overview": "...",
        "files": {...},
        "cost_summary": "...",
        "total_tokens": int,
        "total_cost": float,
    }
    """
    if status_callback:
        status_callback("正在分析小说结构...")

    # 1. 分块
    blocks = split_novel(text)
    if status_callback:
        status_callback(f"共识别出 {len(blocks)} 个分块，正在聚合为候选序列段...")

    # 2. 检查预算
    estimated_total_tokens = estimate_tokens(text) * 2  # 输入+输出粗略估算
    if budget_check and MAX_TOTAL_TOKENS > 0 and estimated_total_tokens > MAX_TOTAL_TOKENS:
        raise RuntimeError(
            f"预估 Token 消耗 ({estimated_total_tokens:,}) 超出预算上限 "
            f"({MAX_TOTAL_TOKENS:,})，请增大预算或缩短文本"
        )

    # 3. 候选序列段聚合（跨章，保证序列完整）
    batches, vol_titles = build_sequence_batches(blocks)
    if status_callback:
        status_callback(f"聚合出 {len(batches)} 个完整序列候选段")

    # 4. 初始化 LLM 服务
    llm = LLMService(tier_key=tier_key, model_id=model_id)

    # 5. 尝试恢复检查点（以候选段标题为对齐键）
    checkpoint = load_checkpoint()
    completed_indices = set()
    batch_keys = [_batch_range(b) for b in batches]
    if checkpoint and checkpoint.get("blocks") == batch_keys:
        completed_indices = set(checkpoint.get("completed_indices", []))
        if "llm_stats" in checkpoint:
            llm.total_input_tokens = checkpoint["llm_stats"].get("total_input_tokens", 0)
            llm.total_output_tokens = checkpoint["llm_stats"].get("total_output_tokens", 0)
            llm.total_cost = checkpoint["llm_stats"].get("total_cost", 0.0)
            llm.total_calls = checkpoint["llm_stats"].get("total_calls", 0)
        if status_callback:
            status_callback(f"发现检查点，已完成的段: {len(completed_indices)}/{len(batches)}")

    # 6. 逐候选段提炼（并行）
    block_results = []
    if checkpoint and "block_results" in checkpoint:
        block_results = checkpoint["block_results"]

    import threading as _t
    from concurrent.futures import ThreadPoolExecutor, as_completed
    _CONC = 4
    _cplock = _t.Lock()
    def _run_batch(_i):
        if status_callback:
            status_callback('processing [%d/%d]: %s' % (_i + 1, len(batches), _batch_range(batches[_i])))
        try:
            _r = extract_batch_content(batches[_i], llm, progress_callback)
        except Exception as _e:
            _r = {
                "title": _batch_range(batches[_i]),
                "sequences": [{"sequence": {"story": "（本段处理失败，未生成故事结构。原因：%s）" % _e}, "arc": {"trend": "", "pulses": []}, "scenes": []}],
            }
        return _i, _r
    _todo = [i for i in range(len(batches)) if i not in completed_indices]
    with ThreadPoolExecutor(max_workers=_CONC) as _ex:
        _futs = {_ex.submit(_run_batch, i): i for i in _todo}
        for _f in as_completed(_futs):
            _i, _r = _f.result()
            while len(block_results) <= _i:
                block_results.append(None)
            block_results[_i] = _r
            with _cplock:
                completed_indices.add(_i)
                save_checkpoint({
                    'blocks': batch_keys,
                    'completed_indices': list(completed_indices),
                    'block_results': block_results,
                    'llm_stats': llm.get_stats(),
                })
            if budget_check and MAX_TOTAL_TOKENS > 0:
                if llm.total_input_tokens + llm.total_output_tokens >= MAX_TOTAL_TOKENS:
                    if status_callback:
                        status_callback('Token budget reached, stopping')
                    break

    block_results = [r for r in block_results if r is not None]

    # 7. 按卷汇总（把同一卷标题的连续候选段归并）
    if status_callback:
        status_callback("正在汇总各卷/部...")

    volume_results = []
    i = 0
    n = len(block_results)
    while i < n:
        vol = vol_titles[i] if i < len(vol_titles) else "全书"
        group = []
        j = i
        while j < n and vol_titles[j] == vol:
            group.append(block_results[j])
            j += 1
        if status_callback:
            status_callback(f"正在汇总: {vol}")
        volume_results.append(aggregate_volume(vol, group, llm))
        i = j

    if not volume_results:
        volume_results.append(aggregate_volume("全书", block_results, llm))

    # 8. 生成全书总览
    if status_callback:
        status_callback("正在生成全书总览...")

    overview = generate_overview(volume_results, llm)

    # 9. 导出文件
    if status_callback:
        status_callback("正在导出文件...")

    output_dir = book_output_dir(book_name)
    files = export_files(volume_results, overview, output_dir)

    # 10. 清除检查点（处理完成）
    clear_checkpoint()

    cost_summary = llm.get_cost_summary()
    total_tokens = llm.total_input_tokens + llm.total_output_tokens

    return {
        "blocks": blocks,
        "block_results": block_results,
        "volume_results": volume_results,
        "overview": overview,
        "files": files,
        "cost_summary": cost_summary,
        "total_tokens": total_tokens,
        "total_cost": llm.total_cost,
    }