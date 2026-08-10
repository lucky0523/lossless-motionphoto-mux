#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""make_motionphoto.py 的独立校验脚本。

刻意不 import make_motionphoto，所有解析逻辑（JPEG 段扫描、XMP 提取、容器目录
定位）都自己重写一遍。这样工具和校验不会一起错——本脚本曾据此查出一个真实 bug：
vivo 部分照片在主图 EOI 与 gainmap 的 SOI 之间有几个 \\x00 对齐字节，工具原先把
它们并进了 GainMap 的 Item:Length，导致该项指向 \\x00，严格的 Ultra HDR 读取器
解不出 gainmap。

校验内容
--------
Motion Photo（逐个文件）
  * 视频：文件末尾必须与源 mp4 逐字节一致，且文件尾不是 SEF 索引
  * 照片：XMP 段之前的字节必须与源图逐字节一致（EXIF 一个字节都不许动）；
    XMP 段之后最多允许 4 字节差异，且只能是 MPF 里的主图长度字段
  * XMP：是合法 XML；把 <rdf:RDF>...</rdf:RDF> 单独截出来也能解析
    （厂商相册常这么干）；xmlns:rdf 声明在 rdf:RDF 上
  * 回归项：不得出现 GCamera:MicroVideo*（实测会让 OPPO 认不出动态照片）、
    不得出现 OpCamera/oplus（实测无用，已删除）；VCamera 那三个属性（规范外）
    的有无要和源照片的品牌对得上——vivo 拍的必须三个齐全（实测缺任何一个 vivo
    都认不出），别家必须一个都没有。不论哪种期望，只写出一两个永远算错。
    「是不是 vivo 拍的」由本脚本自己读源图的 EXIF Make 重新判断，不采信合成
    脚本的结论，两边不一致会直接报错
  * 标签：GCamera:MotionPhoto=1 / MotionPhotoVersion=1 / 封面帧时间戳 / rdf:about=""
  * 容器目录：首项 Primary；MotionPhoto 唯一且是最后一项；GainMap 排在
    MotionPhoto 之前；至少两项带 Item:Padding（规避 exiftool<=13.50 把单个
    Padding 当数组解引用的崩溃 bug）
  * 定位：逐项累加 Length+Padding 与「文件长度 - MotionPhoto Length」必须一致，
    且该处是 ftyp box；GainMap 项指向的位置必须是 FFD8FF 且能解码
  * 元数据：EXIF / ICC / MakerNotes / Composite 与源图逐标签一致
  * 文件属性：修改时间与权限保留

静态照片 / 视频输出
  * 与源文件逐字节一致，修改时间保留

覆盖率
  * 源目录里每个照片/视频都被消费掉了：合成进某个 Motion Photo，或原样复制到
    输出目录。按「消费了哪些源文件」判断，与输出文件名无关

用法
----
    python3 verify_motionphoto.py                 # 校验默认的三个输出目录
    python3 verify_motionphoto.py --single-out 相册
    python3 verify_motionphoto.py --src /Volumes/SD/DCIM --motion-out ~/out

依赖 Pillow（解码 GainMap）和 exiftool（比对元数据）；缺了会跳过对应检查并提示。
退出码：0 全部通过，1 有校验失败，2 参数或目录有问题。
"""

from __future__ import annotations

__version__ = "1.0.0"

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Tuple

XMP_SIG = b"http://ns.adobe.com/xap/1.0/\x00"
MPF_SIG = b"MPF\x00"
NS = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "C": "http://ns.google.com/photos/1.0/container/",
    "I": "http://ns.google.com/photos/1.0/container/item/",
    "G": "http://ns.google.com/photos/1.0/camera/",
    "V": "http://ns.vivo.com/photos/1.0/camera/",
}
# vivo 相册的识别前提：三个属性必须同时出现，缺一个都不认（单变量实测，
# vivo X300 Pro / OriginOS）。这一组不在规范里，合成脚本按 --vcamera 决定写不写
# （auto / on / off），所以这里的期望值也分两种情况：
#   该写 -> 三个都必须在，且值正确
#   不该写 -> 三个都不许在
# 「该不该写」是本脚本**自己**从源照片重新判断的（auto 模式下看 EXIF Make 等），
# 不复用合成脚本的判断结果，保持两边独立。
# 不管期望是什么，「只写出一两个」永远是错的：vivo 认不出，却又看起来写了。
VCAMERA_REQUIRED = {
    "VMotionPhotoVersion": "1",
    "VMotionPhotoSource": "1",
    "VMediaKitVersion": "1.0.0.5",
}
EXIF_GROUPS = ["-EXIF:all", "-ICC_Profile:all", "-MakerNotes:all", "-Composite:all"]

try:
    from PIL import Image
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False

HAVE_EXIFTOOL = shutil.which("exiftool") is not None


# --------------------------------------------------------------------------- #
# 自己重写的 JPEG / XMP 解析
# --------------------------------------------------------------------------- #

def jpeg_scan(d: bytes) -> Tuple[int, List[Tuple[int, int, int]]]:
    """返回 (主图编码长度, [(marker, 段起点, 段终点)])。"""
    if d[:2] != b"\xff\xd8":
        raise ValueError("不是 JPEG")
    segs: List[Tuple[int, int, int]] = []
    i = 2
    while True:
        while d[i] == 0xFF and d[i + 1] == 0xFF:
            i += 1
        if d[i] != 0xFF:
            raise ValueError("标记同步失败 @ %d" % i)
        m = d[i + 1]
        if m == 0xD9:
            return i + 2, segs
        if m == 0x01 or 0xD0 <= m <= 0xD7:
            i += 2
            continue
        L = int.from_bytes(d[i + 2:i + 4], "big")
        segs.append((m, i, i + 2 + L))
        if m == 0xDA:
            j = i + 2 + L
            while True:
                j = d.find(b"\xff", j)
                if j < 0:
                    raise ValueError("扫描熵编码数据时未找到 EOI")
                nb = d[j + 1]
                if nb == 0x00 or 0xD0 <= nb <= 0xD7:
                    j += 2
                    continue
                i = j
                break
            continue
        i += 2 + L


def xmp_span(d: bytes) -> Optional[Tuple[int, int, str]]:
    """返回 (APP1 段起点, 段终点, XMP 包文本)。"""
    p = d.find(XMP_SIG)
    if p < 0:
        return None
    L = int.from_bytes(d[p - 2:p], "big")
    return p - 4, p - 2 + L, d[p + len(XMP_SIG):p - 2 + L].decode("utf-8", "replace")


def mpf_span(d: bytes, segs: List[Tuple[int, int, int]]) -> Optional[Tuple[int, int]]:
    for m, s, e in segs:
        if m == 0xE2 and d[s + 4:s + 8] == MPF_SIG:
            return s, e
    return None


def looks_like_vivo(d: bytes) -> bool:
    """源照片是不是 vivo 拍的（对应合成脚本 --vcamera auto 的判断）。

    这里刻意自己重新实现一遍 EXIF Make 的读取，不复用合成脚本的函数，
    这样「该不该写 VCamera」两边是各自独立算出来的。
    """
    # 1) EXIF IFD0 的 Make(0x010F)
    p = d.find(b"Exif\x00\x00", 0, 1 << 20)
    if p >= 0:
        base = p + 6
        try:
            bo = d[base:base + 2]
            if bo in (b"II", b"MM"):
                e = ">" if bo == b"MM" else "<"
                import struct as _s
                if _s.unpack_from(e + "H", d, base + 2)[0] == 42:
                    ifd = base + _s.unpack_from(e + "I", d, base + 4)[0]
                    for k in range(_s.unpack_from(e + "H", d, ifd)[0]):
                        q = ifd + 2 + 12 * k
                        tag, typ, num = _s.unpack_from(e + "HHI", d, q)
                        if tag == 0x010F and typ == 2 and 0 < num <= 256:
                            raw = (d[q + 8:q + 8 + num] if num <= 4 else
                                   d[base + _s.unpack_from(e + "I", d, q + 8)[0]:][:num])
                            if raw.split(b"\x00")[0].strip().lower().startswith(b"vivo"):
                                return True
                            break
        except Exception:  # noqa: BLE001  期望值判断不该因为畸形 EXIF 而中断校验
            pass
    # 2) 源 XMP 里的 vivo 命名空间
    if b"ns.vivo.com" in d[:1 << 20]:
        return True
    # 3) 文件尾的 vivo cameralbum! 块
    tail = d[-65536:]
    return b"cameralbum!" in tail and b'vivo{"com.android.camera' in tail


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #

class Checker:
    def __init__(self) -> None:
        self.fail: List[str] = []
        self.skipped: List[str] = []

    def chk(self, cond: bool, rel: str, msg: str) -> bool:
        if not cond:
            self.fail.append("%s: %s" % (rel, msg))
        return bool(cond)


def verify_motion_photo(ck: Checker, src_img: str, src_vid: str, out: str,
                        rel: str, stats: Dict[str, int],
                        vcamera_mode: str = "auto") -> None:
    o = open(src_img, "rb").read()
    n = open(out, "rb").read()
    v = open(src_vid, "rb").read()

    # ---- 视频 ----
    ck.chk(n[len(n) - len(v):] == v, rel, "文件末尾不是原视频字节")
    ck.chk(n[-4:] != b"SEFT", rel, "文件尾出现 Samsung SEF 索引")

    img = n[:len(n) - len(v)]
    nxs = xmp_span(img)
    if not ck.chk(nxs is not None, rel, "输出缺少 XMP 段"):
        return
    ns_, ne, packet = nxs

    # ---- 照片区逐字节 ----
    oxs = xmp_span(o)
    if oxs:
        os_, oe, _ = oxs
        ck.chk(o[:os_] == img[:ns_], rel, "XMP 之前的字节被改动（EXIF 必须一字节不动）")
        a, b = o[oe:], img[ne:]
        if ck.chk(len(a) == len(b), rel, "XMP 之后长度不一致"):
            diff = [i for i in range(len(a)) if a[i] != b[i]]
            if diff:
                _, segs = jpeg_scan(img)
                mp = mpf_span(img, segs)
                inside = mp is not None and all(mp[0] <= ne + i < mp[1] for i in diff)
                ck.chk(len(diff) <= 4 and inside, rel,
                       "XMP 之后有 %d 字节被改动，且不全在 MPF 段内" % len(diff))
    else:
        ck.chk(o == img[:ns_] + img[ne:], rel, "原文件无 XMP，其字节未被完整保留")

    # ---- XMP 是否合法、片段能否单独解析 ----
    body = re.sub(r"<\?xpacket[^>]*\?>", "", packet).strip()
    try:
        tree = ET.fromstring(body)
    except ET.ParseError as exc:
        ck.chk(False, rel, "XMP 不是合法 XML: %s" % exc)
        return
    frag = re.search(r"<rdf:RDF\b.*?</rdf:RDF>", body, re.S)
    if ck.chk(frag is not None, rel, "找不到 rdf:RDF"):
        try:
            ET.fromstring(frag.group(0))
        except ET.ParseError as exc:
            ck.chk(False, rel, "rdf:RDF 片段单独解析失败（厂商相册常这么解析）: %s" % exc)
    ck.chk("<rdf:RDF xmlns:rdf=" in body, rel, "xmlns:rdf 未声明在 rdf:RDF 上")

    # ---- 回归项 ----
    ck.chk("MicroVideo" not in body, rel,
           "XMP 里出现 MicroVideo（实测会让 OPPO 认不出动态照片）")
    ck.chk("OpCamera" not in body and "oplus" not in body, rel,
           "XMP 里出现 OpCamera/oplus（实测无用，应已删除）")

    desc = tree.find(".//rdf:Description", NS)
    if not ck.chk(desc is not None, rel, "找不到 rdf:Description"):
        return

    def g(k: str) -> Optional[str]:
        return desc.get("{%s}%s" % (NS["G"], k))

    ck.chk(desc.get("{%s}about" % NS["rdf"]) == "", rel, "rdf:about 不是空串")
    ck.chk(g("MotionPhoto") == "1", rel, "GCamera:MotionPhoto != 1")
    ck.chk(g("MotionPhotoVersion") == "1", rel, "GCamera:MotionPhotoVersion != 1")
    ck.chk(g("MotionPhotoPresentationTimestampUs") is not None, rel, "缺少封面帧时间戳")

    # vivo 私有的三个属性（见 VCAMERA_REQUIRED 处的说明）
    # 期望值自己算：auto 时按源照片重新判断是不是 vivo 拍的
    if vcamera_mode == "on":
        expect_vcamera = True
    elif vcamera_mode == "off":
        expect_vcamera = False
    else:
        expect_vcamera = looks_like_vivo(o)
    vc = {name: desc.get("{%s}%s" % (NS["V"], name)) for name in VCAMERA_REQUIRED}
    present = [k for k, v in vc.items() if v is not None]
    if expect_vcamera:
        for name, want in VCAMERA_REQUIRED.items():
            ck.chk(vc[name] == want, rel,
                   "VCamera:%s = %r，应为 %r（三个属性缺一个，vivo 就认不出动态照片）"
                   % (name, vc[name], want))
    else:
        ck.chk(not present, rel,
               "源照片不是 vivo 拍的（或指定了 --vcamera off），却出现了 "
               "VCamera:%s（规范外标签）" % ", VCamera:".join(present))
    # 不管期望如何，写一半是最危险的状态
    ck.chk(len(present) in (0, len(VCAMERA_REQUIRED)), rel,
           "VCamera:* 只写出了 %d/%d 个（%s），必须三个齐全或一个都不写"
           % (len(present), len(VCAMERA_REQUIRED), ", ".join(present)))
    if len(present) == len(VCAMERA_REQUIRED):
        stats["vcamera"] = stats.get("vcamera", 0) + 1

    # ---- 容器目录 ----
    items = list(tree.iterfind(".//{%s}Item" % NS["C"]))
    if not ck.chk(len(items) >= 2, rel, "容器目录条目不足"):
        return
    sem = [it.get("{%s}Semantic" % NS["I"]) for it in items]
    ck.chk(sem[0] == "Primary", rel, "容器目录第一项不是 Primary")
    ck.chk(sem.count("MotionPhoto") == 1, rel, "MotionPhoto item 不唯一")
    ck.chk(sem[-1] == "MotionPhoto", rel, "MotionPhoto 不是容器目录最后一项")
    ck.chk(sum(1 for it in items
               if it.get("{%s}Padding" % NS["I"]) is not None) >= 2, rel,
           "带 Item:Padding 的条目少于 2 个（exiftool<=13.50 会崩）")
    if oxs and 'Semantic="GainMap"' in oxs[2]:
        ck.chk("GainMap" in sem, rel, "源图有 GainMap 项但输出里没有（HDR 会丢）")
    if "GainMap" in sem:
        ck.chk(sem.index("GainMap") < sem.index("MotionPhoto"), rel,
               "GainMap 未排在 MotionPhoto 之前")

    # ---- 逐项累加定位 ----
    pe, _ = jpeg_scan(n)
    off = pe + int(items[0].get("{%s}Padding" % NS["I"]) or 0)
    for it in items[1:]:
        s = it.get("{%s}Semantic" % NS["I"])
        L = int(it.get("{%s}Length" % NS["I"]) or 0)
        if s == "GainMap":
            if ck.chk(n[off:off + 3] == b"\xff\xd8\xff", rel,
                      "GainMap 项定位处不是 JPEG SOI，实为 %r" % n[off:off + 4]):
                if HAVE_PIL:
                    import io
                    try:
                        im = Image.open(io.BytesIO(n[off:off + L]))
                        im.load()
                        stats["gainmap"] += 1
                    except Exception as exc:  # noqa: BLE001
                        ck.chk(False, rel, "按 GainMap 项定位解码失败: %s" % exc)
        if s == "MotionPhoto":
            break
        off += L + int(it.get("{%s}Padding" % NS["I"]) or 0)

    vlen = int(items[-1].get("{%s}Length" % NS["I"]) or 0)
    ck.chk(vlen == len(v), rel, "MotionPhoto 的 Item:Length != 视频长度")
    ck.chk(off == len(n) - vlen, rel,
           "累加法(%d)与倒推法(%d)定位不一致" % (off, len(n) - vlen))
    ck.chk(n[off + 4:off + 8] == b"ftyp", rel, "累加法定位处不是 ftyp box")

    # ---- 文件属性 ----
    s1, s2 = os.stat(src_img), os.stat(out)
    ck.chk(int(s1.st_mtime) == int(s2.st_mtime), rel, "修改时间未保留")
    ck.chk(s1.st_mode == s2.st_mode, rel, "权限未保留")


def compare_exif(ck: Checker, srcs: List[str], outs: List[str]) -> Tuple[int, int]:
    def dump(paths: List[str]) -> Tuple[Dict[str, dict], str]:
        r = subprocess.run(["exiftool", "-j", "-a", "-G1", "-s", "-n", "-q", "-q"]
                           + EXIF_GROUPS + paths, capture_output=True, text=True)
        try:
            return {d["SourceFile"]: d for d in json.loads(r.stdout)}, r.stderr
        except json.JSONDecodeError:
            return {}, r.stderr

    ds, _ = dump(srcs)
    do, err = dump(outs)
    bad = 0
    for s, o in zip(srcs, outs):
        a, b = dict(ds.get(s, {})), dict(do.get(o, {}))
        a.pop("SourceFile", None)
        b.pop("SourceFile", None)
        if a != b:
            bad += 1
            if bad <= 5:
                ck.fail.append("%s: EXIF 与源图不一致 %s"
                               % (os.path.basename(o),
                                  sorted({k for k in set(a) | set(b)
                                          if a.get(k) != b.get(k)})))
    errs = [l for l in (err or "").splitlines() if l.strip()]
    if errs:
        ck.fail.append("exiftool 读取输出文件时报错: %s" % errs[:3])
    return len(outs) - bad, len(errs)


def scan_dirs(src: str, mo: str, so: str, mv: str, limit: int) -> dict:
    """目录扫描模式：按「输出文件的相对路径 == 源文件的相对路径」配对。

    注意这个前提在用了 --name-style 改名后不成立，那种情况请用清单模式。
    """
    motion: List[Tuple[str, str, str]] = []
    copies: List[Tuple[str, str, str]] = []
    for root, _, files in os.walk(mo):
        for fn in sorted(files):
            if not fn.lower().endswith((".jpg", ".jpeg")):
                continue
            out = os.path.join(root, fn)
            rel = os.path.relpath(out, mo)
            src_img = os.path.join(src, rel)
            src_vid = os.path.splitext(src_img)[0] + ".mp4"
            if not (os.path.exists(src_img) and os.path.exists(src_vid)):
                continue
            if limit and len(motion) >= limit:
                break
            motion.append((src_img, src_vid, out))
    done = {m[2] for m in motion}
    for label, base, exts in (("静态照片", so, (".jpg", ".jpeg")),
                              ("视频", mv, (".mp4", ".mov", ".m4v"))):
        if not os.path.isdir(base):
            continue
        for root, _, files in os.walk(base):
            for fn in sorted(files):
                if not fn.lower().endswith(exts):
                    continue
                out = os.path.join(root, fn)
                if out in done:
                    continue
                s = os.path.join(src, os.path.relpath(out, base))
                if os.path.exists(s):
                    copies.append((label, s, out))
    return {"motion": motion, "copies": copies,
            "coverage": None if limit else {"src": src}}


def run(plan: dict) -> int:
    """按计划执行校验并打印结果，返回退出码。"""
    if not HAVE_PIL:
        print("提示: 未安装 Pillow，跳过 GainMap 解码检查")
    if not HAVE_EXIFTOOL:
        print("提示: 未找到 exiftool，跳过 EXIF 逐标签比对")

    mode = plan.get("expect", {}).get("vcamera_mode", "auto")
    if mode != "auto":
        print("提示: 按 --vcamera %s 校验 VCamera:* 的有无" % mode)

    ck = Checker()
    stats = {"gainmap": 0, "vcamera": 0}
    srcs: List[str] = []
    outs: List[str] = []
    for entry in plan["motion"]:
        src_img, src_vid, out = entry[:3]
        # 清单里第 4 项是合成脚本声明的「本文件写了 VCamera」，只用来对账，
        # 期望值仍由本脚本自己从源照片判断（见 verify_motion_photo）
        declared = entry[3] if len(entry) > 3 else None
        rel = os.path.basename(out)
        if not (os.path.exists(src_img) and os.path.exists(src_vid)
                and os.path.exists(out)):
            ck.fail.append("%s: 源文件或输出文件不存在" % rel)
            continue
        if declared is not None:
            mine = True if mode == "on" else False if mode == "off" \
                else looks_like_vivo(open(src_img, "rb").read())
            ck.chk(bool(declared) == mine, rel,
                   "合成脚本说 VCamera=%s，本脚本独立判断是 %s（两边的设备识别不一致）"
                   % (declared, mine))
        srcs.append(src_img)
        outs.append(out)
        verify_motion_photo(ck, src_img, src_vid, out, rel, stats, mode)
    print("已校验 Motion Photo: %d（其中 %d 张按容器目录成功解码出 GainMap）"
          % (len(outs), stats["gainmap"]))
    if outs:
        print("带 vivo 私有 VCamera:* 的: %d/%d（--vcamera %s，"
              "按源照片的拍摄设备逐个核对）" % (stats["vcamera"], len(outs), mode))

    counts: Dict[str, int] = {}
    for label, s, out in plan["copies"]:
        counts[label] = counts.get(label, 0) + 1
        rel = os.path.basename(out)
        if not os.path.exists(out):
            ck.fail.append("%s: %s 输出文件不存在" % (rel, label))
            continue
        if open(s, "rb").read() != open(out, "rb").read():
            ck.fail.append("%s: %s 与源文件不一致" % (rel, label))
        if int(os.stat(s).st_mtime) != int(os.stat(out).st_mtime):
            ck.fail.append("%s: %s 修改时间未保留" % (rel, label))
    for label in ("静态照片", "视频"):
        print("已校验%s: %d（与源文件逐字节一致）" % (label, counts.get(label, 0)))

    # 覆盖率：源目录里每个照片/视频都必须被消费掉（合成进某个 Motion Photo，
    # 或原样复制到输出目录）。按「消费了哪些源文件」判断，而不是按文件名去输出
    # 目录里找同名文件——后者在 --name-style 改名后会全部误报。
    cov = plan.get("coverage")
    if cov:
        consumed = set()
        for entry in plan["motion"]:
            consumed.add(os.path.abspath(entry[0]))
            consumed.add(os.path.abspath(entry[1]))
        for _, s, _ in plan["copies"]:
            consumed.add(os.path.abspath(s))
        missing = []
        for root, _, files in os.walk(cov["src"]):
            for fn in files:
                if not fn.lower().endswith((".jpg", ".jpeg", ".mp4", ".mov", ".m4v")):
                    continue
                p = os.path.join(root, fn)
                if os.path.abspath(p) not in consumed:
                    missing.append(os.path.relpath(p, cov["src"]))
        print("未被处理的源文件: %d" % len(missing))
        ck.fail += ["源文件未落地: " + m for m in missing]

    if HAVE_EXIFTOOL and outs:
        same, nerr = compare_exif(ck, srcs, outs)
        print("EXIF/ICC/MakerNotes/Composite 与源图完全一致: %d/%d" % (same, len(outs)))
        print("exiftool 读取输出文件的报错行数: %d" % nerr)

    print()
    if ck.fail:
        print("校验失败 %d 项:" % len(ck.fail))
        for f in ck.fail[:30]:
            print("  ✗", f)
        if len(ck.fail) > 30:
            print("  ...（其余 %d 项省略）" % (len(ck.fail) - 30))
        return 1
    print("全部校验通过 ✓")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="verify_motionphoto.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False)
    ap.add_argument("-h", "--help", action="help",
                    help="显示这份说明并退出（也可以直接写 help）")
    ap.add_argument("-V", "--version", action="version",
                    version="%(prog)s " + __version__,
                    help="显示版本号并退出")
    ap.add_argument("--src", default="DCIM", metavar="DIR", help="数据源目录")
    ap.add_argument("--motion-out", default="motionphoto_output", metavar="DIR")
    ap.add_argument("--static-out", default="staticphoto_output", metavar="DIR")
    ap.add_argument("--movie-out", default="movieout", metavar="DIR")
    ap.add_argument("--single-out", nargs="?", const="output", default=None,
                    metavar="DIR", help="三类输出都在同一个目录时用这个")
    ap.add_argument("--limit", type=int, default=0, metavar="N",
                    help="只校验前 N 个 Motion Photo，0=全部（同时跳过覆盖率检查）")
    ap.add_argument("--vcamera", nargs="?", const="on", default="auto",
                    choices=("auto", "on", "off"), metavar="{auto,on,off}",
                    help="被校验的输出是用 make_motionphoto.py 的哪种 --vcamera\n"
                         "生成的（默认 auto：源照片是 vivo 拍的就要求三个属性\n"
                         "齐全，否则要求一个都没有）。清单模式下自动跟随本次的\n"
                         "选择，不用手动加")
    ap.add_argument("--manifest", metavar="FILE",
                    help="清单模式：从 JSON 文件读取要校验的文件三元组。\n"
                         "make_motionphoto.py 合成结束后就是用这个模式调用本脚本的，"
                         "能精确校验本次实际写出的文件（改名、单目录输出、部分处理都适用）")
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0].lower() in ("help", "-help", "--usage"):
        ap.print_help()
        return 0
    args = ap.parse_args(argv)

    if args.manifest:
        with open(args.manifest, encoding="utf-8") as fh:
            raw = json.load(fh)
        plan = {"motion": [tuple(x) for x in raw.get("motion", [])],
                "copies": [tuple(x) for x in raw.get("copies", [])],
                "coverage": raw.get("coverage"),
                "expect": raw.get("expect", {})}
        return run(plan)

    if args.single_out:
        args.motion_out = args.static_out = args.movie_out = args.single_out
    root_name = os.path.basename(os.path.normpath(args.src))
    mo = os.path.join(args.motion_out, root_name)
    if not os.path.isdir(args.src):
        print("错误: 数据源目录不存在: %s" % args.src, file=sys.stderr)
        return 2
    if not os.path.isdir(mo):
        print("错误: 动态照片输出目录不存在: %s" % mo, file=sys.stderr)
        return 2
    plan = scan_dirs(args.src, mo,
                     os.path.join(args.static_out, root_name),
                     os.path.join(args.movie_out, root_name), args.limit)
    plan["expect"] = {"vcamera_mode": args.vcamera}
    return run(plan)


if __name__ == "__main__":
    sys.exit(main())
