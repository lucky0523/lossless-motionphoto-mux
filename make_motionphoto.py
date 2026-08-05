#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把同名（不含扩展名）的照片 + 视频合成为 Google Motion Photo v1.0 格式。

规范参考: https://developer.android.com/media/platform/motion-photo-format

真机实测结论（OPPO / ColorOS、Xiaomi / HyperOS、Samsung Galaxy / One UI 均已验证）
--------------------------------------------------------------------------------
* 不要写 GCamera:MicroVideo / MicroVideoVersion / MicroVideoOffset /
  MicroVideoPresentationTimestampUs。单变量对照实测：只要多了这 4 个属性，
  OPPO 就不再把文件识别为动态照片（小米两种都能识别）。ColorOS 会因此走进
  它不支持的旧 MicroVideo 分支，忽略标准的 Container:Directory。
* XMP 的 xmlns 声明必须放在真机的位置上（xmlns:rdf 在 rdf:RDF 上、厂商
  命名空间在 rdf:Description 上）。全部提到根元素上会让小米无法识别。
* 前缀要用 GCamera / Container / Item 这一套通用写法。
* 不需要任何厂商私有的东西，三家都认这一套纯 Google 标准元数据：
  - OPPO 的 OpCamera:* 那一组，以及 EXIF 里 oplus_ 开头的 UserComment：不需要
    （小米原厂样片两者皆无，OPPO 一样能识别），所以本工具完全不动 EXIF。
  - 三星的 SEF trailer（SEFH/SEFT + MotionPhoto_Data 标签）：不需要，三星
    Gallery 同样能识别标准格式。
  - 厂商原厂文件里那段短的无音轨"自动播放"次视频：可选，不影响识别。
* Ultra HDR：三家相册都靠 MPF(APP2) 定位 gainmap，不依赖容器目录里的 GainMap
  项（实测把该项删掉，三家仍正常显示 HDR）。但 Google Photos 依赖它，所以
  容器目录里的 GainMap 项必须原样保留。

设计要点
--------
1. 完全保留原始照片字节：只在 JPEG 段序列中「替换 / 插入」XMP(APP1) 段，
   不重新编码、不动 EXIF / ICC / MPF / 厂商 APPn 段，也不动图像熵编码数据。
2. 完全保留原始视频字节：视频原样追加到文件末尾（MotionPhoto item 必须是文件最后一项）。
3. 正确处理 Ultra HDR：这些 vivo 照片已经带有 GContainer Directory
   (Primary + GainMap)，脚本把 MotionPhoto item 追加到 GainMap **之后**
   （规范要求 gainmap item 必须排在 video item 之前）。
4. 正确处理厂商私有尾部数据（vivo 的 "streamdata" 尾巴，约 161KB）：
   这些字节没有被容器目录描述，会破坏「累加 Length 定位」的算法。脚本把它们
   吸收进上一个 secondary item 的 Length（若没有 secondary item，则记入
   Primary 的 Item:Padding，这正是 Padding 的用途）。这样一来
   「累加 Length」和「文件尾部倒推」两种读取方式都能正确定位视频，且不丢任何字节。
5. 修正 MPF(APP2) 里因 XMP 段长度变化而失效的偏移/长度字段。
6. 保留文件名、修改时间、权限。

输出布局
--------
    motionphoto_output/DCIM/...   合成好的 Motion Photo
    staticphoto_output/DCIM/...   没有同名视频、未合成的静态照片
    movieout/DCIM/...             没有同名照片的普通视频
    加 --single-out [DIR] 则三者合并输出到同一个目录（默认 output）

用法
----
    python3 make_motionphoto.py                       # 使用默认的三个输出目录
    python3 make_motionphoto.py --single-out           # 全部输出到 output/
    python3 make_motionphoto.py --single-out 相册      # 全部输出到 相册/
    python3 make_motionphoto.py --src DCIM --limit 5  # 先试跑 5 个
    python3 make_motionphoto.py --dry-run
"""

from __future__ import annotations

__version__ = "1.0.0"

import argparse
import contextlib
import io
import json
import os
import re
import shutil
import struct
import sys
import tempfile
import threading
import time
import traceback
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

NS_X = "adobe:ns:meta/"
NS_RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
NS_CONTAINER = "http://ns.google.com/photos/1.0/container/"
NS_ITEM = "http://ns.google.com/photos/1.0/container/item/"
NS_CAMERA = "http://ns.google.com/photos/1.0/camera/"
# OPPO / 一加 / 真我的私有命名空间。本工具不写它，只在 --inspect 读别人的文件
# 时会遇到，这里留着是为了给它一个稳定的前缀。
NS_OPCAMERA = "http://ns.oplus.com/photos/1.0/camera/"

# ET 的命名空间前缀表是进程全局的，多线程下必须串行化 XMP 的构建与序列化
_XMP_LOCK = threading.Lock()

XMP_SIG = b"http://ns.adobe.com/xap/1.0/\x00"
XMP_EXT_SIG = b"http://ns.adobe.com/xmp/extension/\x00"
MPF_SIG = b"MPF\x00"

# 原文件没有 XMP 时，新建的 XMP 包里写这个 toolkit 标识；原文件已有 XMP 则沿用它自己的
XMP_TOOLKIT = "make_motionphoto.py %s" % __version__

# APP1 段的 payload 上限 = 65535 - 2(长度字段本身)
MAX_APP1_PAYLOAD = 65533
MAX_XMP_PACKET = MAX_APP1_PAYLOAD - len(XMP_SIG)

IMAGE_EXTS = {".jpg", ".jpeg"}
# 这些格式理论上也能做 Motion Photo，但需要 ISOBMFF 的 mpvd box，本脚本不处理
UNSUPPORTED_IMAGE_EXTS = {".heic", ".heif", ".avif", ".png", ".webp", ".dng", ".bmp", ".gif", ".tif", ".tiff"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v"}
IGNORED_NAMES = {".ds_store", "thumbs.db", ".picasa.ini"}


# --------------------------------------------------------------------------- #
# JPEG 结构解析
# --------------------------------------------------------------------------- #

@dataclass
class Segment:
    marker: int        # 0xE1 等（不含前导 0xFF）
    start: int         # 0xFF 所在偏移
    end: int           # 段结束后的第一个字节偏移
    payload_start: int # 数据区起始（跳过 marker 与 2 字节长度）


class JpegError(Exception):
    pass


def parse_jpeg(data: bytes) -> Tuple[List[Segment], int]:
    """返回 (段列表, 主图像结束偏移)。

    主图像结束偏移 = 第一个 EOI(FFD9) 之后的位置，也就是「primary image encoding」
    的长度。后面的字节都是追加内容（gainmap / 厂商尾巴 / 视频）。
    """
    if len(data) < 4 or data[0:2] != b"\xff\xd8":
        raise JpegError("不是 JPEG（缺少 SOI）")

    segs: List[Segment] = []
    i = 2
    n = len(data)
    while i < n - 1:
        # 跳过填充的 0xFF
        while i < n - 1 and data[i] == 0xFF and data[i + 1] == 0xFF:
            i += 1
        if data[i] != 0xFF:
            raise JpegError(f"标记同步失败 @ {i}")
        m = data[i + 1]

        if m == 0xD9:  # EOI
            return segs, i + 2
        if m == 0x01 or 0xD0 <= m <= 0xD7:  # TEM / RSTn: 无长度字段
            i += 2
            continue

        if i + 4 > n:
            raise JpegError("段头被截断")
        length = int.from_bytes(data[i + 2:i + 4], "big")
        if length < 2:
            raise JpegError(f"非法段长 {length} @ {i}")
        seg_end = i + 2 + length
        if seg_end > n:
            raise JpegError(f"段越界 @ {i}")
        segs.append(Segment(m, i, seg_end, i + 4))

        if m == 0xDA:  # SOS: 之后是熵编码数据，需要扫描到下一个真实标记
            j = seg_end
            while True:
                j = data.find(b"\xff", j)
                if j < 0 or j + 1 >= n:
                    raise JpegError("扫描熵编码数据时未找到 EOI")
                nxt = data[j + 1]
                if nxt == 0x00 or 0xD0 <= nxt <= 0xD7 or nxt == 0xFF:
                    j += 1 if nxt == 0xFF else 2
                    continue
                break
            i = j
            continue

        i = seg_end

    raise JpegError("未找到 EOI")


def find_xmp_segment(data: bytes, segs: List[Segment]) -> Optional[Segment]:
    for s in segs:
        if s.marker == 0xE1 and data[s.payload_start:s.payload_start + len(XMP_SIG)] == XMP_SIG:
            return s
    return None


def find_mpf_segment(data: bytes, segs: List[Segment]) -> Optional[Segment]:
    for s in segs:
        if s.marker == 0xE2 and data[s.payload_start:s.payload_start + 4] == MPF_SIG:
            return s
    return None


def xmp_insert_position(data: bytes, segs: List[Segment]) -> int:
    """没有 XMP 段时，决定插入位置。

    插到 Exif(APP1) 之后 / JFIF(APP0) 之后，并且一定在 APP2(MPF/ICC) 之前 ——
    这样 MPF 的相对偏移基准点会和图像数据一起平移，偏移仍然有效。
    """
    pos = 2
    for s in segs:
        if s.marker == 0xE0:
            pos = max(pos, s.end)
        elif s.marker == 0xE1:
            pos = max(pos, s.end)
        else:
            break
    return pos


# --------------------------------------------------------------------------- #
# MPF (CIPA DC-007 Multi-Picture Format) 偏移修正
# --------------------------------------------------------------------------- #

def fix_mpf(buf: bytearray, mpf_seg_start_in_new: int, delta: int,
            insert_pos: int) -> Optional[str]:
    """XMP 段长度变化 delta 字节后，修正 MPF 表。

    MPF 的 Individual Image Data Offset 是相对「MP Endian 字段起始处」的偏移。
    - 插入点在 MPF 之前：基准点和后面的图像一起平移，相对偏移不变，只需修正
      第一张图（主图）的 size。
    - 插入点在 MPF 之后：基准点不动，位于插入点之后的图像偏移都要 += delta。
    返回警告信息（如果有）。
    """
    p = mpf_seg_start_in_new
    seg_len = int.from_bytes(buf[p + 2:p + 4], "big")
    base = p + 4 + 4  # 跳过 FFE2 + 长度 + "MPF\0"
    tiff_end = p + 2 + seg_len
    if bytes(buf[base:base + 2]) not in (b"II", b"MM"):
        return "MPF 字节序标记异常，跳过修正"
    bo = ">" if bytes(buf[base:base + 2]) == b"MM" else "<"
    try:
        ifd_off = struct.unpack_from(bo + "I", buf, base + 4)[0]
        count = struct.unpack_from(bo + "H", buf, base + ifd_off)[0]
        entry_ptr = None
        num_images = 0
        for k in range(count):
            e = base + ifd_off + 2 + 12 * k
            tag, typ, cnt = struct.unpack_from(bo + "HHI", buf, e)
            if tag == 0xB001:  # NumberOfImages
                num_images = struct.unpack_from(bo + "I", buf, e + 8)[0]
            elif tag == 0xB002:  # MPEntry
                entry_ptr = base + struct.unpack_from(bo + "I", buf, e + 8)[0]
        if entry_ptr is None or num_images == 0:
            return None
        for k in range(num_images):
            eo = entry_ptr + 16 * k
            if eo + 16 > tiff_end:
                return "MPEntry 越界，跳过修正"
            size = struct.unpack_from(bo + "I", buf, eo + 4)[0]
            start = struct.unpack_from(bo + "I", buf, eo + 8)[0]
            if k == 0:
                # 主图长度变化了 delta
                struct.pack_into(bo + "I", buf, eo + 4, max(0, size + delta))
            elif start != 0 and mpf_seg_start_in_new < insert_pos:
                # 基准点在插入点之前 => 相对偏移需要 += delta
                struct.pack_into(bo + "I", buf, eo + 8, start + delta)
        return None
    except (struct.error, IndexError):
        return "解析 MPF 失败，跳过修正"


# --------------------------------------------------------------------------- #
# EXIF UserComment 原地改写（OPPO / ColorOS 相册的识别依据）
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# MP4 / MOV 解析（纯 Python，无外部依赖）
# --------------------------------------------------------------------------- #

def _iter_boxes(fh, start: int, end: int):
    pos = start
    while pos + 8 <= end:
        fh.seek(pos)
        hdr = fh.read(8)
        if len(hdr) < 8:
            return
        size = int.from_bytes(hdr[0:4], "big")
        typ = hdr[4:8]
        body = pos + 8
        if size == 1:
            ext = fh.read(8)
            if len(ext) < 8:
                return
            size = int.from_bytes(ext, "big")
            body = pos + 16
        elif size == 0:
            size = end - pos
        if size < 8 or pos + size > end:
            return
        yield typ, body, pos + size
        pos += size


def probe_video(path: str) -> Tuple[str, Optional[int]]:
    """返回 (mime, 时长微秒)。时长未知时为 None。"""
    size = os.path.getsize(path)
    mime = "video/mp4"
    duration_us: Optional[int] = None
    with open(path, "rb") as fh:
        top = list(_iter_boxes(fh, 0, size))
        if not top:
            raise ValueError("不是有效的 ISOBMFF/MP4 容器（找不到顶层 box）")
        brands = [t for t, _, _ in top]
        if b"ftyp" not in brands:
            raise ValueError("缺少 ftyp box")
        for typ, body, end in top:
            if typ == b"ftyp":
                fh.seek(body)
                major = fh.read(4)
                if major in (b"qt  ",):
                    mime = "video/quicktime"
            elif typ == b"moov":
                for t2, b2, e2 in _iter_boxes(fh, body, end):
                    if t2 == b"mvhd":
                        fh.seek(b2)
                        raw = fh.read(min(32, e2 - b2))
                        if len(raw) >= 20:
                            ver = raw[0]
                            if ver == 1 and len(raw) >= 28:
                                ts = int.from_bytes(raw[20:24], "big")
                                dur = int.from_bytes(raw[24:32], "big") if len(raw) >= 32 else 0
                            else:
                                ts = int.from_bytes(raw[12:16], "big")
                                dur = int.from_bytes(raw[16:20], "big")
                            if ts > 0 and 0 < dur < (1 << 62):
                                duration_us = dur * 1_000_000 // ts
                        break
    return mime, duration_us


# --------------------------------------------------------------------------- #
# XMP 处理
# --------------------------------------------------------------------------- #

def _q(ns: str, name: str) -> str:
    return "{%s}%s" % (ns, name)


# 真机文件里通用的前缀。前缀在 XML 语义上无所谓，但厂商相册的解析器经常按字面
# 字符串/正则匹配，所以必须用最通用的那一套。
KNOWN_PREFIXES = {
    NS_X: "x",
    NS_RDF: "rdf",
    NS_CONTAINER: "Container",
    NS_ITEM: "Item",
    NS_CAMERA: "GCamera",
    NS_OPCAMERA: "OpCamera",
    "http://ns.adobe.com/hdr-gain-map/1.0/": "hdrgm",
    "http://ns.adobe.com/xap/1.0/": "xmp",
    "http://ns.adobe.com/photoshop/1.0/": "photoshop",
    "http://ns.adobe.com/xmp/note/": "xmpNote",
    "http://ns.adobe.com/tiff/1.0/": "tiff",
    "http://ns.adobe.com/exif/1.0/": "exif",
    "http://ns.google.com/photos/1.0/panorama/": "GPano",
    "http://ns.google.com/photos/1.0/image/": "GImage",
    "http://ns.google.com/photos/1.0/audio/": "GAudio",
    "http://ns.google.com/photos/dd/1.0/profile/": "GDepth",
    "http://ns.xiaomi.com/photos/1.0/camera/": "MiCamera",
}


def _build_prefix_map(xmp_text: str) -> Dict[str, str]:
    """URI -> 前缀。已知的用固定前缀，其余沿用原文件里的前缀。"""
    pm = dict(KNOWN_PREFIXES)
    used = set(pm.values())
    for prefix, uri in re.findall(r'xmlns:([A-Za-z_][\w.\-]*)\s*=\s*"([^"]+)"', xmp_text or ""):
        if uri in pm:
            continue
        p, i = prefix, 2
        while p in used:
            p, i = "%s%d" % (prefix, i), i + 1
        pm[uri] = p
        used.add(p)
    return pm


def _split_tag(tag: str) -> Tuple[Optional[str], str]:
    if tag.startswith("{"):
        uri, name = tag[1:].split("}", 1)
        return uri, name
    return None, tag


def _pname(tag: str, pm: Dict[str, str]) -> str:
    uri, name = _split_tag(tag)
    if uri is None:
        return name
    prefix = pm.get(uri)
    if prefix is None:  # 兜底：给一个稳定的自动前缀
        prefix = "ns%d" % (abs(hash(uri)) % 1000)
        pm[uri] = prefix
    return "%s:%s" % (prefix, name)


def _collect_uris(el: ET.Element, out: set) -> None:
    uri, _ = _split_tag(el.tag)
    if uri:
        out.add(uri)
    for k in el.attrib:
        u, _ = _split_tag(k)
        if u:
            out.add(u)
    for c in el:
        _collect_uris(c, out)


# xmlns 声明的排列顺序（照抄真机样片：hdrgm, GCamera, Container, Item）
NS_DECL_ORDER = ["http://ns.adobe.com/hdr-gain-map/1.0/", NS_CAMERA, NS_OPCAMERA,
                 "http://ns.xiaomi.com/photos/1.0/camera/", NS_CONTAINER, NS_ITEM]
# Container:Item 的属性顺序（真机样片是 Mime 在前）
ITEM_ATTR_ORDER = ["Mime", "Semantic", "Length", "Padding"]


def _emit(el: ET.Element, pm: Dict[str, str], indent: str, out: List[str],
          extra_ns: Optional[List[str]] = None) -> None:
    """按真机样片的排版输出：rdf:Description 与 Container:Item 的属性每行一个。"""
    name = _pname(el.tag, pm)
    is_desc = el.tag == _q(NS_RDF, "Description")
    is_item = el.tag == _q(NS_CONTAINER, "Item")

    keys = list(el.attrib)
    if is_item:
        keys.sort(key=lambda k: ITEM_ATTR_ORDER.index(_split_tag(k)[1])
                  if _split_tag(k)[1] in ITEM_ATTR_ORDER else len(ITEM_ATTR_ORDER))
    else:
        keys.sort(key=lambda k: 0 if k == _q(NS_RDF, "about") else 1)

    parts = ["%s<%s" % (indent, name)]
    if is_desc:
        # rdf:about 跟在元素名后面，xmlns 缩进 +4，属性缩进 +2
        if _q(NS_RDF, "about") in el.attrib:
            parts.append(' rdf:about="%s"' % escape_attr(el.attrib[_q(NS_RDF, "about")]))
            keys = [k for k in keys if k != _q(NS_RDF, "about")]
        for uri in (extra_ns or []):
            parts.append('\n%s    xmlns:%s="%s"' % (indent, pm[uri], uri))
        for k in keys:
            parts.append('\n%s  %s="%s"' % (indent, _pname(k, pm), escape_attr(el.attrib[k])))
    elif is_item and len(keys) > 1:
        for k in keys:
            parts.append('\n%s  %s="%s"' % (indent, _pname(k, pm), escape_attr(el.attrib[k])))
    else:
        for uri in (extra_ns or []):
            parts.append(' xmlns:%s="%s"' % (pm[uri], uri))
        for k in keys:
            parts.append(' %s="%s"' % (_pname(k, pm), escape_attr(el.attrib[k])))

    children = list(el)
    text = (el.text or "").strip()
    if not children and not text:
        parts.append("/>")
        out.append("".join(parts))
        return
    parts.append(">")
    if text and not children:
        out.append("".join(parts) + escape_text(text) + "</%s>" % name)
        return
    out.append("".join(parts))
    for c in children:
        _emit(c, pm, indent + "  ", out)
    out.append("%s</%s>" % (indent, name))


def escape_attr(v: str) -> str:
    return (v.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def escape_text(v: str) -> str:
    return v.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def serialize_xmp(root: ET.Element, pm: Dict[str, str]) -> str:
    """按真机文件的惯用布局序列化 XMP。

    ElementTree 自带的序列化会把所有 xmlns 提到根元素上，而真机文件是
    xmlns:x 在 x:xmpmeta、xmlns:rdf 在 rdf:RDF、厂商命名空间在各个
    rdf:Description 上。语义等价，但很多厂商相册的解析器会把 rdf:RDF 片段
    单独截出来再解析，那时提到根上的声明就丢了。
    """
    out: List[str] = []
    rdf = _find_rdf(root)
    out.append('<x:xmpmeta xmlns:x="%s" x:xmptk="%s">'
               % (NS_X, escape_attr(root.get(_q(NS_X, "xmptk")) or XMP_TOOLKIT)))
    out.append('  <rdf:RDF xmlns:rdf="%s">' % NS_RDF)
    for desc in list(rdf):
        uris: set = set()
        _collect_uris(desc, uris)
        uris.discard(NS_RDF)  # rdf 已在 rdf:RDF 上声明
        extra = sorted(uris, key=lambda u: (NS_DECL_ORDER.index(u)
                                            if u in NS_DECL_ORDER else 99, pm.get(u, u)))
        _emit(desc, pm, "    ", out, extra_ns=extra)
    out.append("  </rdf:RDF>")
    out.append("</x:xmpmeta>")
    return "\n".join(out)


def _new_empty_xmp() -> ET.Element:
    meta = ET.Element(_q(NS_X, "xmpmeta"))
    meta.set(_q(NS_X, "xmptk"), XMP_TOOLKIT)
    rdf = ET.SubElement(meta, _q(NS_RDF, "RDF"))
    ET.SubElement(rdf, _q(NS_RDF, "Description"))
    return meta


def _find_rdf(root: ET.Element) -> ET.Element:
    if root.tag == _q(NS_RDF, "RDF"):
        return root
    rdf = root.find(_q(NS_RDF, "RDF"))
    if rdf is None:
        rdf = ET.SubElement(root, _q(NS_RDF, "RDF"))
    return rdf


def _item_of(li: ET.Element) -> Optional[ET.Element]:
    """rdf:li 里的 Container:Item（既支持子元素写法，也支持属性直挂写法）。"""
    it = li.find(_q(NS_CONTAINER, "Item"))
    if it is not None:
        return it
    if any(k.startswith("{%s}" % NS_ITEM) for k in li.attrib):
        return li
    return None


def _skew_to_soi(data: bytes, pos: int, window: int = 64) -> int:
    """返回 pos 到最近的 JPEG SOI(FFD8FF) 之间的字节数，找不到则返回 0。

    只在很小的窗口里找，避免把无关内容误判成 item 起点。
    """
    if data[pos:pos + 3] == b"\xff\xd8\xff":
        return 0
    k = data.find(b"\xff\xd8\xff", pos, pos + window)
    return k - pos if k > pos else 0


def build_xmp(existing: Optional[str],
              primary_mime: str,
              primary_len: int,
              file_len_before_video: int,
              video_len: int,
              video_mime: str,
              presentation_ts_us: Optional[int],
              image_data: Optional[bytes] = None) -> Tuple[str, List[str]]:
    """生成新的 XMP 包文本。

    existing               原有 XMP 包文本（没有则 None）
    primary_len            主 JPEG 编码长度（第一个 EOI 之后的偏移）
    file_len_before_video  追加视频前的原始文件总长度
    image_data             原始图片字节，用于校正 secondary item 的实际起点
    """
    notes: List[str] = []

    if existing:
        try:
            root = ET.fromstring(existing)
        except ET.ParseError as exc:
            raise ValueError("原有 XMP 无法解析: %s" % exc)
    else:
        root = _new_empty_xmp()

    rdf = _find_rdf(root)

    # ---- 定位（或创建）承载 Container:Directory 的 rdf:Description ----
    descs = rdf.findall(_q(NS_RDF, "Description"))
    holder: Optional[ET.Element] = None
    directory: Optional[ET.Element] = None
    for d in descs:
        cand = d.find(_q(NS_CONTAINER, "Directory"))
        if cand is not None:
            holder, directory = d, cand
            break
    if holder is None:
        holder = descs[0] if descs else ET.SubElement(rdf, _q(NS_RDF, "Description"))
    holder.set(_q(NS_RDF, "about"), holder.get(_q(NS_RDF, "about")) or "")

    # ---- 必须清掉已废弃的 GCamera:MicroVideo* 属性，而且绝不能重新写回去 ----
    #
    # 【重要，不要"顺手补上"这一组】单变量对照实测（OPPO Find X9 Ultra / ColorOS，
    # Xiaomi 17T / HyperOS；两个变体只差这 4 个属性，图片与视频字节完全相同）：
    #     写了 MicroVideo*  ->  OPPO 认不出动态照片，小米能认
    #     不写 MicroVideo*  ->  OPPO 能认，小米能认
    # ColorOS 相册一看到 GCamera:MicroVideo="1" 就走进它不支持的旧 MicroVideo
    # 分支，从而忽略后面标准的 Container:Directory。两台真机的原厂样片里也都
    # 没有这组标签，新版规范同样明确把它们标记为已删除。
    legacy = {"MicroVideo", "MicroVideoVersion", "MicroVideoOffset",
              "MicroVideoPresentationTimestampUs"}
    for d in rdf.iter():
        for key in [k for k in d.attrib if k.startswith("{%s}" % NS_CAMERA)
                    and k.split("}", 1)[1] in legacy]:
            del d.attrib[key]
        for child in [c for c in list(d) if c.tag.startswith("{%s}" % NS_CAMERA)
                      and c.tag.split("}", 1)[1] in legacy]:
            d.remove(child)

    ts = presentation_ts_us if presentation_ts_us is not None else -1

    # ---- GCamera：Google Motion Photo v1（Google Photos / Android 现行标准）----
    holder.set(_q(NS_CAMERA, "MotionPhoto"), "1")
    holder.set(_q(NS_CAMERA, "MotionPhotoVersion"), "1")
    holder.set(_q(NS_CAMERA, "MotionPhotoPresentationTimestampUs"), str(ts))

    # 注：这里不写任何厂商私有标签。OPPO/ColorOS 的 OpCamera:MotionPhotoOwner /
    # OLivePhotoVersion / VideoLength / MotionPhotoPrimaryPresentationTimestampUs
    # 经单变量对照实测确认不是识别的必要条件（写与不写，OPPO Find X9 Ultra 和
    # Xiaomi 17T 都能识别），故不写，XMP 保持纯 Google 标准的一套。

    # ---- Container:Directory / rdf:Seq ----
    if directory is None:
        directory = ET.SubElement(holder, _q(NS_CONTAINER, "Directory"))
    seq = directory.find(_q(NS_RDF, "Seq"))
    if seq is None:
        seq = ET.SubElement(directory, _q(NS_RDF, "Seq"))

    lis = seq.findall(_q(NS_RDF, "li"))

    # 主 item
    if not lis:
        li = ET.SubElement(seq, _q(NS_RDF, "li"))
        li.set(_q(NS_RDF, "parseType"), "Resource")
        it = ET.SubElement(li, _q(NS_CONTAINER, "Item"))
        it.set(_q(NS_ITEM, "Semantic"), "Primary")
        it.set(_q(NS_ITEM, "Mime"), primary_mime)
        lis = [li]
        notes.append("原文件无 Container 目录，已新建 Primary item")

    primary_item = _item_of(lis[0])
    if primary_item is None:
        raise ValueError("Container 目录第一项不是合法的 Container:Item")
    primary_item.set(_q(NS_ITEM, "Semantic"), "Primary")
    if not primary_item.get(_q(NS_ITEM, "Mime")):
        primary_item.set(_q(NS_ITEM, "Mime"), primary_mime)
    primary_item.set(_q(NS_ITEM, "Length"), "0")  # 规范要求主 item 的 Length 为 0

    secondary_items: List[ET.Element] = []
    for li in lis[1:]:
        it = _item_of(li)
        if it is None:
            raise ValueError("Container 目录中存在非法条目")
        if (it.get(_q(NS_ITEM, "Semantic")) or "") == "MotionPhoto":
            raise ValueError("照片已包含 MotionPhoto item（可能已是 Motion Photo）")
        secondary_items.append(it)

    # ---- 对齐 secondary item 的实际起点 ----
    # 有些机型（实测 vivo 部分照片）会在主图 EOI 和 gainmap 的 SOI 之间塞几个
    # \x00 对齐字节，而源 XMP 里并没有用 Padding 描述它。这段前置空隙必须记到
    # 「前一项」的 Item:Padding 上，否则 GainMap 项会指到 \x00 上，严格的
    # Ultra HDR 读取器解不出 gainmap（走 MPF 的厂商相册看不出问题）。
    cursor = primary_len + int(primary_item.get(_q(NS_ITEM, "Padding")) or 0)
    prev_item = primary_item
    for it in secondary_items:
        if image_data is not None and (it.get(_q(NS_ITEM, "Mime")) or "").startswith("image/"):
            skew = _skew_to_soi(image_data, cursor)
            if skew:
                key = _q(NS_ITEM, "Padding")
                prev_item.set(key, str(int(prev_item.get(key) or 0) + skew))
                cursor += skew
                notes.append("%s item 实际起点比累加值靠后 %d 字节（对齐填充），"
                             "已记入前一项的 Item:Padding"
                             % (it.get(_q(NS_ITEM, "Semantic")) or "?", skew))
        cursor += int(it.get(_q(NS_ITEM, "Length")) or 0)
        prev_item = it

    # 注：原有的 GainMap 等 secondary item 一律原样保留在目录里。
    # 实测三家相册（OPPO/小米/三星）都是通过 MPF(APP2) 定位 gainmap 的，把
    # GainMap 项从目录里删掉它们照样显示 HDR；但 Google Photos 的 Ultra HDR
    # 依赖这一项，所以必须留着。
    #
    # ---- 处理视频之前「未被目录描述」的字节 ----
    # 来源是厂商私有尾部数据（例如 vivo 的 streamdata 块）。不处理的话，
    #「累加 Length 定位」的读取器会算错视频位置。
    gap = file_len_before_video - cursor

    if gap > 0:
        if secondary_items:
            # Padding 只能描述「主图之后、下一个 item 之前」的空隙，而这些字节位于
            # GainMap 之后，所以只能并入最后一个 secondary item 的 Length。
            # 该 item 本身仍可正常解码（JPEG 解码器在 EOI 处停止，忽略多余尾字节）。
            last = secondary_items[-1]
            old = int(last.get(_q(NS_ITEM, "Length")) or 0)
            last.set(_q(NS_ITEM, "Length"), str(old + gap))
            notes.append("视频前 %d 字节未被容器描述，已计入最后一个 %s item 的 Length"
                         % (gap, last.get(_q(NS_ITEM, "Semantic")) or "?"))
        else:
            # 没有 secondary item 时这段空隙紧跟主图，正是 Item:Padding 的语义。
            key = _q(NS_ITEM, "Padding")
            primary_item.set(key, str(int(primary_item.get(key) or 0) + gap))
            notes.append("视频前 %d 字节已写入 Primary 的 Item:Padding" % gap)
    elif gap < 0:
        notes.append("警告：目录声明的长度超出文件实际长度 %d 字节，未做补偿" % (-gap))

    # ---- 追加 MotionPhoto item（必须是最后一项）----
    li = ET.SubElement(seq, _q(NS_RDF, "li"))
    li.set(_q(NS_RDF, "parseType"), "Resource")
    it = ET.SubElement(li, _q(NS_CONTAINER, "Item"))
    it.set(_q(NS_ITEM, "Semantic"), "MotionPhoto")
    it.set(_q(NS_ITEM, "Mime"), video_mime)
    it.set(_q(NS_ITEM, "Length"), str(video_len))

    # Item:Padding 的写法照抄真机样片：Primary 和 GainMap 带 Padding，
    # MotionPhoto item 不带。但 exiftool<=13.50 在「整个目录只有一个 item 带
    # Padding」时会把它当数组解引用而崩溃，所以至少要保证有两个 item 带 Padding。
    all_items = [it for it in (_item_of(li) for li in seq.findall(_q(NS_RDF, "li")))
                 if it is not None]
    for it in all_items[:-1]:
        if it.get(_q(NS_ITEM, "Padding")) is None:
            it.set(_q(NS_ITEM, "Padding"), "0")
    if sum(1 for it in all_items if it.get(_q(NS_ITEM, "Padding")) is not None) < 2:
        all_items[-1].set(_q(NS_ITEM, "Padding"), "0")

    body = serialize_xmp(root, _build_prefix_map(existing or ""))
    packet = ('<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
              + body + '\n<?xpacket end="w"?>')
    return packet, notes


def extract_xmp_text(data: bytes, seg: Segment) -> str:
    raw = data[seg.payload_start + len(XMP_SIG):seg.end]
    text = raw.decode("utf-8", errors="strict")
    # 去掉 xpacket 包裹，交给 ET 解析纯 XML
    text = re.sub(r"^\s*<\?xpacket[^>]*\?>", "", text)
    text = re.sub(r"<\?xpacket[^>]*\?>\s*$", "", text)
    return text.strip().rstrip("\x00").strip()


def make_app1_xmp(packet: str) -> bytes:
    body = XMP_SIG + packet.encode("utf-8")
    if len(body) > MAX_APP1_PAYLOAD:
        raise ValueError("XMP 包过大（%d 字节），超出单个 APP1 段上限" % len(body))
    return b"\xff\xe1" + struct.pack(">H", len(body) + 2) + body


# --------------------------------------------------------------------------- #
# 单个文件的合成
# --------------------------------------------------------------------------- #

@dataclass
class Result:
    kind: str                  # merged / static / unsupported / video / other / failed / skipped
    src: str
    out: Optional[str] = None
    reason: str = ""
    notes: List[str] = field(default_factory=list)
    out_bytes: int = 0
    src_video: Optional[str] = None   # merged 时记下配对的视频，供合成后校验用


def merge_motion_photo(image_path: str, video_path: str, out_path: str,
                       dry_run: bool = False, verify: bool = True,
                       ) -> Tuple[List[str], int]:
    """把 image + video 合成 Motion Photo 写到 out_path。

    返回 (提示信息列表, 输出文件字节数)。dry-run 下不写文件，但返回的字节数是
    精确值而非估算——那时图片部分已经在内存里拼好了，加上视频长度就是最终大小。
    """
    with open(image_path, "rb") as fh:
        data = fh.read()

    segs, primary_end = parse_jpeg(data)
    video_size = os.path.getsize(video_path)
    if video_size <= 0:
        raise ValueError("视频文件为空")
    video_mime, dur_us = probe_video(video_path)
    ts_us = dur_us // 2 if dur_us else None

    xmp_seg = find_xmp_segment(data, segs)
    for s in segs:
        if s.marker == 0xE1 and data[s.payload_start:s.payload_start + len(XMP_EXT_SIG)] == XMP_EXT_SIG:
            raise ValueError("包含 Extended XMP 段，暂不支持（避免破坏 XMP 分块结构）")

    existing_text = extract_xmp_text(data, xmp_seg) if xmp_seg else None
    with _XMP_LOCK:
        packet, notes = build_xmp(
            existing=existing_text,
            primary_mime="image/jpeg",
            primary_len=primary_end,
            file_len_before_video=len(data),
            video_len=video_size,
            video_mime=video_mime,
            presentation_ts_us=ts_us,
            image_data=data,
        )
    new_seg = make_app1_xmp(packet)

    if xmp_seg is not None:
        insert_pos = xmp_seg.start
        old_len = xmp_seg.end - xmp_seg.start
        head = data[:xmp_seg.start]
        tail = data[xmp_seg.end:]
    else:
        insert_pos = xmp_insert_position(data, segs)
        old_len = 0
        head = data[:insert_pos]
        tail = data[insert_pos:]
        notes.append("原文件无 XMP 段，已在偏移 %d 处新建" % insert_pos)

    delta = len(new_seg) - old_len
    buf = bytearray(head + new_seg + tail)

    mpf_seg = find_mpf_segment(data, segs)
    if mpf_seg is not None and delta != 0:
        new_mpf_start = mpf_seg.start + (delta if mpf_seg.start >= insert_pos else 0)
        warn = fix_mpf(buf, new_mpf_start, delta, insert_pos)
        if warn:
            notes.append(warn)
        else:
            notes.append("已修正 MPF 偏移/长度（delta=%+d）" % delta)

    out_size = len(buf) + video_size
    if dry_run:
        notes.append("dry-run：未写文件")
        return notes, out_size

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    tmp = out_path + ".part"
    try:
        with open(tmp, "wb") as out, open(video_path, "rb") as vf:
            out.write(buf)
            shutil.copyfileobj(vf, out, 1024 * 1024)
        if verify:
            verify_output(tmp, video_path, video_size, len(buf))
        os.replace(tmp, out_path)
    except BaseException:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise

    # 保留时间戳与权限
    shutil.copystat(image_path, out_path)
    return notes, out_size


def verify_output(out_path: str, video_path: str, video_size: int,
                  video_start: int) -> None:
    total = os.path.getsize(out_path)
    expect = video_start + video_size
    if total != expect:
        raise ValueError("输出长度校验失败: %d != %d" % (total, expect))
    item_len = video_size

    with open(out_path, "rb") as fh:
        head = fh.read(min(video_start, 1 << 20))
        # XMP 必须能被找到且声明了正确的视频长度
        pos = head.find(XMP_SIG)
        if pos < 0:
            raise ValueError("校验失败：输出文件中找不到 XMP 段")
        # FFE1 位于 pos-4，2 字节长度位于 pos-2，payload 从 pos 开始
        seg_len = int.from_bytes(head[pos - 2:pos], "big")
        packet = head[pos + len(XMP_SIG):pos - 2 + seg_len].decode("utf-8")
        items = re.findall(r"<[\w.\-]+:Item\b([^>]*?)/?>", packet)
        mp_items = [a for a in items if 'Semantic="MotionPhoto"' in a]
        if len(mp_items) != 1:
            raise ValueError("校验失败：XMP 中的 MotionPhoto item 数量为 %d" % len(mp_items))
        if items and 'Semantic="MotionPhoto"' not in items[-1]:
            raise ValueError("校验失败：MotionPhoto item 不是容器目录的最后一项")
        m = re.search(r'Length="(\d+)"', mp_items[0])
        if not m or int(m.group(1)) != item_len:
            raise ValueError("校验失败：MotionPhoto item 的 Length 应为 %d" % item_len)
        if 'GCamera:MotionPhoto="1"' not in packet:
            raise ValueError('校验失败：XMP 中缺少 GCamera:MotionPhoto="1"')
        if "MicroVideo" in packet:
            raise ValueError("校验失败：XMP 里出现了已废弃的 MicroVideo 属性"
                             "（会导致 OPPO 无法识别）")

        # 从文件尾倒推 item_len 字节，必须正好是视频的开头
        fh.seek(total - item_len)
        got = fh.read(64)
    with open(video_path, "rb") as vf:
        want = vf.read(64)
    if got != want:
        raise ValueError("校验失败：倒推 %d 字节处不是视频起始位置" % item_len)


# --------------------------------------------------------------------------- #
# 进度条
# --------------------------------------------------------------------------- #

class Progress:
    def __init__(self, total: int, enabled: bool = True):
        self.total = total
        self.done = 0
        self.enabled = enabled and sys.stderr.isatty()
        self.lock = threading.Lock()
        self.current = ""
        self.start = time.time()
        self.plain_every = max(1, total // 50) if total else 1

    def _width(self) -> int:
        try:
            return max(60, min(140, shutil.get_terminal_size((100, 24)).columns))
        except OSError:
            return 100

    def set_current(self, name: str) -> None:
        with self.lock:
            self.current = name
            self._render()

    def advance(self, name: str = "") -> None:
        with self.lock:
            self.done += 1
            if name:
                self.current = name
            self._render()

    def _render(self) -> None:
        if not self.enabled:
            if self.done and self.done % self.plain_every == 0:
                pct = self.done * 100 // max(1, self.total)
                print("  [%3d%%] %d/%d  %s" % (pct, self.done, self.total, self.current),
                      file=sys.stderr, flush=True)
            return
        width = self._width()
        frac = self.done / self.total if self.total else 1.0
        elapsed = time.time() - self.start
        eta = (elapsed / frac - elapsed) if frac > 0 else 0
        counter = "%d/%d" % (self.done, self.total)
        stat = " %3.0f%% %s [%s<%s]" % (frac * 100, counter,
                                        fmt_dur(elapsed), fmt_dur(eta))
        bar_w = 24
        filled = int(bar_w * frac)
        bar = "#" * filled + "-" * (bar_w - filled)
        left = "[%s]%s " % (bar, stat)
        room = width - len(left) - 1
        name = self.current
        if room > 4 and len(name) > room:
            name = "..." + name[-(room - 3):]
        sys.stderr.write("\r\033[2K" + left + name[:max(0, room)])
        sys.stderr.flush()

    def close(self) -> None:
        if self.enabled:
            sys.stderr.write("\r\033[2K")
            sys.stderr.flush()


def fmt_dur(sec: float) -> str:
    sec = int(max(0, sec))
    if sec >= 3600:
        return "%d:%02d:%02d" % (sec // 3600, sec % 3600 // 60, sec % 60)
    return "%02d:%02d" % (sec // 60, sec % 60)


def fmt_size(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return "%.2f %s" % (f, unit) if unit != "B" else "%d B" % n
        f /= 1024
    return "%d B" % n


# --------------------------------------------------------------------------- #
# 扫描与调度
# --------------------------------------------------------------------------- #

@dataclass
class Job:
    kind: str                 # pair / static / unsupported / video / other
    image: Optional[str] = None
    video: Optional[str] = None
    other: Optional[str] = None


def scan(src_root: str) -> Tuple[List[Job], Dict[str, int]]:
    groups: Dict[Tuple[str, str], Dict[str, List[str]]] = {}
    others: List[str] = []
    stats = {"files": 0, "images": 0, "videos": 0, "others": 0}

    for dirpath, dirnames, filenames in os.walk(src_root):
        dirnames.sort()
        for fn in sorted(filenames):
            path = os.path.join(dirpath, fn)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            stats["files"] += 1
            stem, ext = os.path.splitext(fn)
            ext = ext.lower()
            key = (dirpath, stem)
            if ext in IMAGE_EXTS or ext in UNSUPPORTED_IMAGE_EXTS:
                stats["images"] += 1
                groups.setdefault(key, {}).setdefault("img", []).append(path)
            elif ext in VIDEO_EXTS:
                stats["videos"] += 1
                groups.setdefault(key, {}).setdefault("vid", []).append(path)
            else:
                stats["others"] += 1
                if fn.lower() not in IGNORED_NAMES:
                    others.append(path)

    jobs: List[Job] = []
    for (dirpath, stem) in sorted(groups):
        g = groups[(dirpath, stem)]
        imgs = sorted(g.get("img", []))
        vids = sorted(g.get("vid", []))
        for idx, img in enumerate(imgs):
            ext = os.path.splitext(img)[1].lower()
            if ext not in IMAGE_EXTS:
                jobs.append(Job("unsupported", image=img))
            elif idx == 0 and vids:
                jobs.append(Job("pair", image=img, video=vids[0]))
            else:
                jobs.append(Job("static", image=img))
        if vids and not any(os.path.splitext(i)[1].lower() in IMAGE_EXTS for i in imgs):
            for v in vids:
                jobs.append(Job("video", video=v))
        elif len(vids) > 1:
            for v in vids[1:]:
                jobs.append(Job("video", video=v))
    for o in others:
        jobs.append(Job("other", other=o))
    return jobs, stats


def out_path_for(src: str, src_root: str, out_root: str) -> str:
    """staticphoto_output/DCIM/Camera/... —— 输出目录内保留含数据源目录名的完整结构。"""
    root_name = os.path.basename(os.path.normpath(src_root))
    rel = os.path.relpath(src, src_root)
    return os.path.join(out_root, root_name, rel)


def apply_name_style(path: str, style: str) -> str:
    """按需改写输出文件名。

    规范原文：文件名不匹配 ^([^\\s/\\\\][^/\\\\]*MP)\\.(JPG|jpg|JPEG|jpeg|HEIC|heic|AVIF|avif)$
    时，读取器「可以」直接忽略 XMP 和追加的视频。另外 MVIMG_ 前缀是 Google Camera
    早期的约定，小米等厂商的相册也沿用了这个识别方式。
    """
    if style == "keep":
        return path
    d, fn = os.path.split(path)
    stem, ext = os.path.splitext(fn)
    if style in ("mvimg", "mvimg-mp"):
        if not stem.startswith("MVIMG_"):
            stem = "MVIMG_" + re.sub(r"^IMG_", "", stem)
    if style in ("mp", "mvimg-mp") and not stem.endswith("MP"):
        stem += "MP"
    return os.path.join(d, stem + ext)


def copy_plain(src: str, dst: str, dry_run: bool) -> int:
    if dry_run:
        return os.path.getsize(src)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + ".part"
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)
    shutil.copystat(src, dst)
    return os.path.getsize(dst)


def run_job(job: Job, args, progress: Progress) -> Result:
    src = job.image or job.video or job.other or "?"
    progress.set_current(os.path.relpath(src, args.src))
    try:
        if job.kind == "pair":
            out = apply_name_style(out_path_for(job.image, args.src, args.motion_out),
                                  args.name_style)
            if os.path.exists(out) and args.skip_existing:
                return Result("skipped", src, out, "输出已存在")
            notes, size = merge_motion_photo(job.image, job.video, out,
                                             dry_run=args.dry_run,
                                             verify=not args.no_verify)
            return Result("merged", src, out, notes=notes, out_bytes=size,
                          src_video=job.video)

        if job.kind in ("static", "unsupported"):
            out = out_path_for(job.image, args.src, args.static_out)
            if os.path.exists(out) and args.skip_existing:
                return Result("skipped", src, out, "输出已存在")
            size = copy_plain(job.image, out, args.dry_run)
            reason = "无同名视频" if job.kind == "static" else "图片格式不支持合成（已按静态照片复制）"
            return Result(job.kind, src, out, reason, out_bytes=size)

        if job.kind == "video":
            out = out_path_for(job.video, args.src, args.movie_out)
            if os.path.exists(out) and args.skip_existing:
                return Result("skipped", src, out, "输出已存在")
            size = copy_plain(job.video, out, args.dry_run)
            return Result("video", src, out, "无同名照片，按普通视频输出", out_bytes=size)

        return Result("other", src, None, "非照片/视频文件（已忽略）")
    except Exception as exc:  # noqa: BLE001
        detail = "%s: %s" % (type(exc).__name__, exc)
        if os.environ.get("MP_DEBUG"):
            detail += "\n" + traceback.format_exc()
        return Result("failed", src, None, detail)
    finally:
        progress.advance(os.path.relpath(src, args.src))


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #

def build_report(results: List[Result], stats: Dict[str, int], args,
                 elapsed: float) -> str:
    by: Dict[str, List[Result]] = {}
    for r in results:
        by.setdefault(r.kind, []).append(r)

    merged = by.get("merged", [])
    static = by.get("static", [])
    unsupported = by.get("unsupported", [])
    failed = by.get("failed", [])
    skipped = by.get("skipped", [])
    videos = by.get("video", [])
    other = by.get("other", [])
    out_bytes = sum(r.out_bytes for r in results)

    L: List[str] = []
    add = L.append
    add("=" * 78)
    add("Motion Photo 合成报告")
    add("=" * 78)
    add("工具版本   : make_motionphoto.py %s" % __version__)
    add("生成时间   : %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    add("数据源     : %s" % os.path.abspath(args.src))
    if args.single_out:
        add("统一输出   : %s（Motion Photo / 静态照片 / 视频合并到同一目录）"
            % os.path.abspath(args.single_out))
    else:
        add("动态照片   : %s" % os.path.abspath(args.motion_out))
        add("静态照片   : %s" % os.path.abspath(args.static_out))
        add("视频       : %s" % os.path.abspath(args.movie_out))
    add("规范        : Motion Photo format 1.0"
        " (https://developer.android.com/media/platform/motion-photo-format)")
    if args.dry_run:
        add("模式        : DRY-RUN（未写任何文件）")
    add("")
    add("--- 输入统计 ---")
    add("扫描到文件总数        : %d" % stats["files"])
    add("  图片               : %d" % stats["images"])
    add("  视频               : %d" % stats["videos"])
    add("  其他文件           : %d" % stats["others"])
    add("")
    add("--- 处理结果 ---")
    add("成功合成 Motion Photo : %d" % len(merged))
    add("无需合成（静态照片）  : %d" % len(static))
    add("失败                  : %d" % len(failed))
    add("跳过（输出已存在）    : %d" % len(skipped))
    add("图片格式不支持合成    : %d" % len(unsupported))
    add("视频（无同名照片）    : %d" % len(videos))
    add("忽略的其他文件        : %d" % len(other))
    add("")
    add("输出总字节            : %s%s" % (fmt_size(out_bytes),
                                      "（dry-run 未真正写盘，此为将占用的大小）"
                                      if args.dry_run else ""))
    add("耗时                  : %s (%.1f 秒)" % (fmt_dur(elapsed), elapsed))
    if elapsed > 0 and not args.dry_run:
        add("平均吞吐              : %s/s" % fmt_size(int(out_bytes / elapsed)))

    if failed:
        add("")
        add("--- 失败明细 (%d) ---" % len(failed))
        for r in failed:
            add("  ✗ %s" % r.src)
            for line in r.reason.splitlines():
                add("      %s" % line)

    if static:
        add("")
        add("--- 无需合成的照片 (%d) ---" % len(static))
        for r in static:
            add("  · %s" % r.src)

    if unsupported:
        add("")
        add("--- 格式不支持的图片 (%d) ---" % len(unsupported))
        for r in unsupported:
            add("  · %s" % r.src)

    if videos:
        add("")
        add("--- 视频（无同名照片，已按普通视频输出）(%d) ---" % len(videos))
        for r in videos:
            add("  · %s" % r.src)

    if merged:
        add("")
        add("--- 写入的元数据（真机实测：OPPO / 小米 / 三星 三家相册均可识别）---")
        add("  只写 Google 标准标签，不写任何厂商私有标签：")
        add("    GCamera:MotionPhoto=1 / MotionPhotoVersion=1 /")
        add("    MotionPhotoPresentationTimestampUs")
        add("    Container:Directory（Primary [+ GainMap] + MotionPhoto）")
        add("  不写 GCamera:MicroVideo*（实测写了会让 OPPO 认不出动态照片），"
            "源文件带了也会清掉")
        add("  不写 OpCamera:*、不改 EXIF UserComment（实测均非识别所必需）")
        add("  不写 Samsung SEF trailer（实测三星 Gallery 认标准格式，不需要它）")
        add("  EXIF / ICC / MakerNotes 一个字节都没动")

    note_counter: Dict[str, int] = {}
    for r in merged:
        for n in r.notes:
            key = re.sub(r"\d+", "N", n)
            note_counter[key] = note_counter.get(key, 0) + 1
    if note_counter:
        add("")
        add("--- 合成过程中的结构处理汇总 ---")
        for k, v in sorted(note_counter.items(), key=lambda kv: -kv[1]):
            add("  [%d 次] %s" % (v, k))

    add("")
    add("文件名风格            : %s" % {
        "keep": "保留原文件名（注意：规范允许读取器在文件名不匹配 "
                "^...MP\\.(jpg|jpeg|heic|avif)$ 时直接忽略 XMP 与追加的视频）",
        "mp": "IMG_xxxMP.jpg（符合规范建议）",
        "mvimg": "MVIMG_xxx.jpg（小米/旧版 Google Camera 约定）",
        "mvimg-mp": "MVIMG_xxxMP.jpg（同时满足规范建议与 MVIMG_ 约定）",
    }[args.name_style])
    add("=" * 78)
    return "\n".join(L)


# --------------------------------------------------------------------------- #
# 合成后的整体校验（调用同目录下的 verify_motionphoto.py）
# --------------------------------------------------------------------------- #

VERIFIER = "verify_motionphoto.py"


def _load_verifier():
    """按路径加载 verify_motionphoto.py。

    方向是本脚本调用校验脚本，而不是反过来——校验脚本刻意不 import 本脚本，
    自己把 JPEG 段扫描、XMP 提取、容器目录定位重写了一遍，这样合成和校验不会
    一起错。这个独立性是它的全部价值，改动时别破坏。
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), VERIFIER)
    if not os.path.isfile(path):
        return None, "找不到 %s" % path
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("verify_motionphoto", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod, ""
    except Exception as exc:  # noqa: BLE001
        return None, "加载 %s 失败: %s" % (VERIFIER, exc)


def verifier_name() -> str:
    """校验脚本的名字，带上它自己的版本号（拿不到就只给文件名）。"""
    mod, _ = _load_verifier()
    ver = getattr(mod, "__version__", None) if mod else None
    return "%s %s" % (VERIFIER, ver) if ver else VERIFIER


def build_manifest(results: List[Result], args) -> dict:
    """把本次实际写出的文件整理成校验清单。

    用清单而不是让校验脚本自己扫目录，是因为 --name-style 改名后
    「输出相对路径 == 源相对路径」不再成立，而且 --limit / --skip-existing
    下只处理了一部分文件。
    """
    label = {"static": "静态照片", "unsupported": "静态照片", "video": "视频"}
    motion = [[r.src, r.src_video, r.out] for r in results
              if r.kind == "merged" and r.out and r.src_video]
    copies = [[label[r.kind], r.src, r.out] for r in results
              if r.kind in label and r.out]
    # 只有完整跑过一遍才做覆盖率检查（--limit / --skip-existing 下本来就是部分处理）
    full = not args.limit and not args.skip_existing
    return {"motion": motion, "copies": copies,
            "coverage": {"src": args.src} if full else None}


def run_final_verify(results: List[Result], args) -> Tuple[int, str]:
    """跑合成后的整体校验，返回 (退出码, 输出文本)。"""
    mod, err = _load_verifier()
    if mod is None:
        return 0, "跳过：%s\n（这一步依赖同目录下的 %s）" % (err, VERIFIER)
    manifest = build_manifest(results, args)
    if not manifest["motion"] and not manifest["copies"]:
        return 0, "本次没有写出任何文件，无需校验。"
    fd, path = tempfile.mkstemp(suffix=".json", prefix="mp_manifest_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = mod.main(["--manifest", path])
        return code, buf.getvalue().rstrip()
    except Exception as exc:  # noqa: BLE001
        detail = "%s: %s" % (type(exc).__name__, exc)
        if os.environ.get("MP_DEBUG"):
            detail += "\n" + traceback.format_exc()
        return 1, "校验过程本身出错：" + detail
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# 诊断模式
# --------------------------------------------------------------------------- #

def inspect_file(path: str) -> None:
    """打印一个文件的 Motion Photo 结构诊断，用于和真机文件对照。"""
    print("=" * 78)
    print("文件: %s" % path)
    data = open(path, "rb").read()
    print("大小: %d 字节 (%s)" % (len(data), fmt_size(len(data))))
    print("文件名是否匹配规范建议 ^...MP\\.(jpg|jpeg|heic|avif)$: %s"
          % bool(re.match(r"^[^\s/\\][^/\\]*MP\.(JPG|jpg|JPEG|jpeg|HEIC|heic|AVIF|avif)$",
                          os.path.basename(path))))

    try:
        segs, primary_end = parse_jpeg(data)
    except JpegError as exc:
        print("JPEG 解析失败: %s" % exc)
        return
    print("主图编码长度(第一个 EOI): %d，其后还有 %d 字节"
          % (primary_end, len(data) - primary_end))
    print("APP 段: %s" % ", ".join(
        "APP%d@%d(%d)" % (s.marker - 0xE0, s.start, s.end - s.start)
        for s in segs if 0xE0 <= s.marker <= 0xEF))

    # 视频位置
    ft = data.find(b"ftyp")
    if ft > 0:
        print("首个 ftyp 出现在偏移 %d（box 起点 %d），brand=%s"
              % (ft, ft - 4, data[ft + 4:ft + 8]))
    else:
        print("没找到 ftyp，文件里可能没有嵌入视频")

    # Samsung trailer
    print("Samsung SEF trailer: %s" % ("有（文件尾为 SEFT）" if data[-4:] == b"SEFT" else "无"))
    if b"MotionPhoto_Data" in data:
        print("  含 MotionPhoto_Data 标签，偏移 %d" % data.find(b"MotionPhoto_Data"))

    # XMP
    xmp_seg = find_xmp_segment(data, segs)
    if xmp_seg is None:
        print("没有标准 XMP(APP1) 段")
        return
    packet = extract_xmp_text(data, xmp_seg)
    print("XMP 段位于偏移 %d，长度 %d 字节" % (xmp_seg.start, xmp_seg.end - xmp_seg.start))
    print("-" * 78)
    print(packet)
    print("-" * 78)

    keys = ["MotionPhoto", "MotionPhotoVersion", "MotionPhotoPresentationTimestampUs",
            "MicroVideo", "MicroVideoVersion", "MicroVideoOffset",
            "MicroVideoPresentationTimestampUs", "MotionPhotoOwner",
            "OLivePhotoVersion", "VideoLength",
            "MotionPhotoPrimaryPresentationTimestampUs"]
    print("关键标签:")
    for k in keys:
        m = (re.search(r'[\w.\-]*:%s="([^"]*)"' % k, packet)
             or re.search(r"<[\w.\-]*:%s>([^<]*)</" % k, packet))
        if m:
            print("  %-42s = %s" % (k, m.group(1)))
    items = re.findall(r"<[\w.\-]+:Item\b([^>]*?)/?>", packet)
    if not items:
        items = re.findall(r"<rdf:li[^>]*>(.*?)</rdf:li>", packet, re.S)
    print("容器目录 %d 项:" % len(items))
    for i, a in enumerate(items, 1):
        print("  [%d] %s" % (i, " ".join(a.split())))

    # 定位算法一致性
    lens = [int(x) for x in re.findall(r'Length[">=]+"?(\d+)', packet)]
    m = re.search(r'Semantic="MotionPhoto"[^>]*?Length="(\d+)"', packet) \
        or re.search(r"MotionPhoto.*?Item:Length>(\d+)<", packet, re.S)
    if m:
        vlen = int(m.group(1))
        start = len(data) - vlen
        print("按 MotionPhoto Item:Length=%d 从文件尾倒推 => 视频起点 %d，"
              "该处是否为 ftyp box: %s" % (vlen, start, data[start + 4:start + 8] == b"ftyp"))
    mv = re.search(r'MicroVideoOffset="(\d+)"', packet) \
        or re.search(r"<[\w.\-]*:MicroVideoOffset>(\d+)<", packet)
    if mv:
        start = len(data) - int(mv.group(1))
        print("按 MicroVideoOffset=%s 倒推 => 视频起点 %d，该处是否为 ftyp box: %s"
              % (mv.group(1), start, data[start + 4:start + 8] == b"ftyp"))
    vl = re.search(r'VideoLength="(\d+)"', packet) \
        or re.search(r"<[\w.\-]*:VideoLength>(\d+)<", packet)
    if vl:
        start = len(data) - int(vl.group(1))
        print("按 OpCamera:VideoLength=%s 倒推 => 视频起点 %d，该处是否为 ftyp box: %s"
              % (vl.group(1), start, data[start + 4:start + 8] == b"ftyp"))
    print("=" * 78)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

DESCRIPTION = """\
把同名（不含扩展名）的照片和视频合成为 Google Motion Photo（动态照片 / 实况照片）。

遍历数据源目录，按「同目录 + 同主文件名」配对，例如
    DCIM/Camera/2025/08/IMG_20250827_180604.jpg
  + DCIM/Camera/2025/08/IMG_20250827_180604.mp4
合成为一个 IMG_20250827_180604.jpg。视频原样追加到 JPEG 末尾，位置写进 XMP。

照片和视频的内容、文件名、EXIF、修改时间、权限全部原样保留：只在 JPEG 的段序列
里替换/插入 XMP(APP1) 段，不重新编码、不动 EXIF/ICC/厂商 APPn 段和图像数据。
"""

EPILOG = """\
输出布局
  默认分三个目录，并复用数据源的完整目录结构：
    motionphoto_output/DCIM/Camera/...   合成好的 Motion Photo
    staticphoto_output/DCIM/Camera/...   没有同名视频、未合成的静态照片
    movieout/DCIM/Camera/...             没有同名照片的普通视频
  加 --single-out [目录] 可以让三者输出到同一个目录。

常用示例
  python3 make_motionphoto.py
      用默认目录跑全量（数据源 DCIM），结束后打印并写出报告
  python3 make_motionphoto.py --dry-run
      只分析不写文件，先看看会产出什么
  python3 make_motionphoto.py --limit 5 --workers 1
      只处理前 5 个任务，单线程，方便试跑和排错
  python3 make_motionphoto.py --single-out 相册
      Motion Photo / 静态照片 / 视频全部输出到 相册/ 下
  python3 make_motionphoto.py --src /Volumes/SD/DCIM --motion-out ~/out
      指定数据源和输出目录
  python3 make_motionphoto.py --inspect a.jpg b.jpg
      诊断模式：打印文件的 Motion Photo 结构，可与真机样片对照
  MP_DEBUG=1 python3 make_motionphoto.py
      失败时在报告里附上完整 traceback

手机兼容性（真机实测：OPPO / ColorOS，小米 / HyperOS，三星 Galaxy / One UI）
  默认输出这三家都能识别，正常情况下什么开关都不用加。
  * 单变量对照实测发现：XMP 里只要多了 GCamera:MicroVideo* 这 4 个废弃属性，
    OPPO 就不再识别为动态照片（小米无所谓）。所以本工具永不写这一组，
    源文件里带了也会清掉。想改这个行为前请先读 build_xmp() 里的注释。
  * 反过来，厂商私有的东西一个都不需要：OPPO 的 OpCamera:* 标签、EXIF 里
    oplus_ 开头的 UserComment、三星的 SEF trailer，实测都可以不写。
  * --name-style 是给尚未验证过的机型留的兜底手段。规范建议文件名以 MP 结尾，
    并允许读取器在文件名不匹配时直接忽略动态照片，但上面三家都不看文件名。
  * 传输要用数据线/MTP 或 LocalSend 这类不改文件的方式。微信、QQ、
    Google Photos 会重新编码或剥掉 JPEG 尾部数据，那样元数据写得再对也没用。
    传完可以在手机上核对文件大小是否和电脑上一致。

合成后校验
  每次运行结束会自动调用同目录下的 verify_motionphoto.py，对本次实际写出的
  文件做一遍整体校验：字节完整性、EXIF 是否一字未动、XMP 是否合法、容器目录
  与视频定位是否自洽、GainMap 能否解码、覆盖率、时间戳权限。结果附在报告末尾。
  该脚本刻意不 import 本文件，自己把解析逻辑重写了一遍，这样合成和校验不会
  一起错。加 --no-verify 可以跳过（不建议）。

退出码
  0 全部成功    1 有文件失败或校验未通过（详见报告）    2 参数或数据源目录有问题
"""


class _HelpFormatter(argparse.RawTextHelpFormatter):
    """保留手写的换行与排版。

    argparse 自带的自动折行按「字符数」算宽度，中文是双宽字符，折出来的结果很难看，
    所以这里直接沿用 RawText 的行为，帮助文本里的换行全部手写。
    """

    def __init__(self, prog: str):
        super().__init__(prog, max_help_position=26, width=94)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="make_motionphoto.py",
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=_HelpFormatter,
        add_help=False)

    g = ap.add_argument_group("帮助")
    g.add_argument("-h", "--help", action="help",
                   help="显示这份用法说明并退出\n直接写 help 也可以：make_motionphoto.py help")
    g.add_argument("-V", "--version", action="version",
                   version="%(prog)s " + __version__,
                   help="显示版本号并退出")

    g = ap.add_argument_group("输入与输出")
    g.add_argument("--src", default="DCIM", metavar="DIR",
                   help="数据源目录，会递归遍历（默认 DCIM）")
    g.add_argument("--motion-out", default="motionphoto_output", metavar="DIR",
                   help="合成好的 Motion Photo 输出目录\n（默认 motionphoto_output）")
    g.add_argument("--static-out", default="staticphoto_output", metavar="DIR",
                   help="没有同名视频、未合成的静态照片输出目录\n（默认 staticphoto_output）")
    g.add_argument("--movie-out", default="movieout", metavar="DIR",
                   help="没有同名照片的普通视频输出目录（默认 movieout）")
    g.add_argument("--single-out", nargs="?", const="output", default=None,
                   metavar="DIR",
                   help="上面三类全部输出到同一个目录\n不带值时用 output")
    g.add_argument("--name-style", choices=("keep", "mp", "mvimg", "mvimg-mp"),
                   default="keep",
                   help="Motion Photo 的输出文件名风格（默认 keep）\n"
                        "  keep      原样，不改名\n"
                        "  mp        IMG_xxxMP.jpg，规范建议的写法\n"
                        "  mvimg     MVIMG_xxx.jpg，旧版 Google Camera 约定\n"
                        "  mvimg-mp  MVIMG_xxxMP.jpg，两者都满足")
    g.add_argument("--report", default=None, metavar="FILE",
                   help="报告写到哪个文件\n"
                        "默认 motionphoto_report_年月日_时分秒.txt（每次运行不覆盖）\n"
                        '传空字符串 --report "" 则只打印不写文件')

    g = ap.add_argument_group("运行控制")
    g.add_argument("--workers", type=int, default=4, metavar="N",
                   help="并发线程数（默认 4）")
    g.add_argument("--limit", type=int, default=0, metavar="N",
                   help="只处理前 N 个任务，0 表示全部（默认 0）\n用于小批量试跑")
    g.add_argument("--dry-run", action="store_true",
                   help="只分析不写任何文件，报告照常输出")
    g.add_argument("--skip-existing", action="store_true",
                   help="输出文件已存在就跳过，可用于断点续跑")
    g.add_argument("--no-verify", action="store_true",
                   help="跳过校验：既不做每个文件写出时的自检，\n"
                        "也不做合成结束后由 %s 完成的整体校验（不建议）" % VERIFIER)

    g = ap.add_argument_group("诊断")
    g.add_argument("--inspect", nargs="+", metavar="FILE",
                   help="只分析给定文件的 Motion Photo 结构并打印报告，不做合成\n"
                        "用来和厂商真机拍的样片逐项对照")
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    ap = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0].lower() in ("help", "-help", "--usage", "?"):
        ap.print_help()
        return 0
    args = ap.parse_args(argv)

    # 报告文件名带上日期时间，多次运行不会互相覆盖。
    # 注意 --report "" 是「不写文件」，和「未指定」(None) 要区分开。
    if args.report is None:
        args.report = datetime.now().strftime("motionphoto_report_%Y%m%d_%H%M%S.txt")

    if args.inspect:
        for p in args.inspect:
            if os.path.isfile(p):
                inspect_file(p)
            else:
                print("跳过（不是文件）: %s" % p, file=sys.stderr)
        return 0

    if not os.path.isdir(args.src):
        print("错误: 数据源目录不存在: %s" % args.src, file=sys.stderr)
        return 2

    if args.single_out:
        args.motion_out = args.static_out = args.movie_out = args.single_out

    print("扫描 %s ..." % os.path.abspath(args.src))
    jobs, stats = scan(args.src)
    if args.limit:
        jobs = jobs[:args.limit]
    pairs = sum(1 for j in jobs if j.kind == "pair")
    print("待处理任务 %d 个（其中可合成 %d 对）\n" % (len(jobs), pairs))

    progress = Progress(len(jobs))
    results: List[Result] = []
    t0 = time.time()
    try:
        if args.workers > 1:
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                for r in pool.map(lambda j: run_job(j, args, progress), jobs):
                    results.append(r)
        else:
            for j in jobs:
                results.append(run_job(j, args, progress))
    except KeyboardInterrupt:
        progress.close()
        print("\n已中断，输出已完成部分的报告。", file=sys.stderr)
    finally:
        progress.close()
    elapsed = time.time() - t0

    report = build_report(results, stats, args, elapsed)

    # ---- 合成完成后的整体校验 ----
    verify_code, verify_text = 0, ""
    if args.dry_run:
        verify_text = "跳过：dry-run 模式没有写出文件。"
    elif args.no_verify:
        verify_text = "跳过：指定了 --no-verify。"
    else:
        print("正在校验输出文件 ...", file=sys.stderr)
        t1 = time.time()
        verify_code, verify_text = run_final_verify(results, args)
        verify_text += "\n校验耗时: %s" % fmt_dur(time.time() - t1)
    section = ["", "=" * 78,
               "合成后校验（由 %s 独立完成，不复用本脚本的解析逻辑）" % verifier_name(),
               "=" * 78, verify_text, "=" * 78]
    report += "\n" + "\n".join(section)

    print(report)
    if args.report:
        try:
            with open(args.report, "w", encoding="utf-8") as fh:
                fh.write(report + "\n")
            print("\n报告已写入: %s" % os.path.abspath(args.report))
        except OSError as exc:
            print("\n写报告失败: %s" % exc, file=sys.stderr)

    if verify_code:
        return 1
    return 1 if any(r.kind == "failed" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
