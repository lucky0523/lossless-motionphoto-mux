# Motion Photo 合成工具

把同名（不含扩展名）的照片和视频合成为 Google Motion Photo（动态照片 / 实况照片），在 OPPO、小米、三星手机相册里都能被识别为动态照片。

vivo 等一些机型的实况照片是以「一张 JPG + 一个同名 MP4」的形式分开保存的，换到别的品牌手机上就只是一张静态照片。这个工具把它们合成回单文件格式。

**核心特性：照片和视频的内容一个字节都不改。** 只在 JPEG 的段序列里替换/插入一个 XMP(APP1) 段，不重新编码、不动 EXIF / gainmap / ICC / MakerNotes / 厂商 APPn 段和图像数据，文件名、修改时间、权限也原样保留。

- `make_motionphoto.py` —— 合成
- `verify_motionphoto.py` —— 独立校验（合成结束后自动调用，也可单独跑）

依赖：Python 3.7+。可选 Pillow（校验 GainMap 解码）、exiftool（校验元数据一致性）。合成本身零依赖，纯标准库。

---

## 1. 使用方法

### 最简用法

```bash
python3 make_motionphoto.py
```

默认从 `DCIM` 递归扫描，按「同目录 + 同主文件名」配对，输出到三个目录：

```
motionphoto_output/DCIM/Camera/...   合成好的 Motion Photo
staticphoto_output/DCIM/Camera/...   没有同名视频、未合成的静态照片
movieout/DCIM/Camera/...             没有同名照片的普通视频
```

输出目录内复用数据源的完整目录结构。跑完打印报告，并写出`motionphoto_report_年月日_时分秒.txt`，然后自动做一遍整体校验。

### 常用命令

```bash
python3 make_motionphoto.py --dry-run              # 只分析不写文件，1 秒出结果
python3 make_motionphoto.py --limit 5 --workers 1  # 只处理前 5 个，单线程，便于排错
python3 make_motionphoto.py --single-out 相册       # 三类输出合并到 相册/ 下
python3 make_motionphoto.py --src /Volumes/SD/DCIM --motion-out ~/out
python3 make_motionphoto.py --inspect a.jpg b.jpg  # 诊断：打印文件的 Motion Photo 结构
python3 make_motionphoto.py -h                     # 完整用法（也可以写 help）
python3 make_motionphoto.py -V                     # 版本号（--version 同义）
MP_DEBUG=1 python3 make_motionphoto.py             # 失败时在报告里附完整 traceback
```

### 参数

| 参数 | 说明 |
|---|---|
| `--src DIR` | 数据源目录，递归遍历（默认 `DCIM`） |
| `--motion-out DIR` | Motion Photo 输出目录（默认 `motionphoto_output`） |
| `--static-out DIR` | 无配对视频的静态照片输出目录（默认 `staticphoto_output`） |
| `--movie-out DIR` | 无配对照片的普通视频输出目录（默认 `movieout`） |
| `--single-out [DIR]` | 上面三类输出到同一个目录，不带值时用 `output` |
| `--name-style` | 输出文件名风格：`keep`（默认，原样）/ `mp`（`IMG_xxxMP.jpg`，规范建议）/ `mvimg`（`MVIMG_xxx.jpg`）/ `mvimg-mp`（两者都满足） |
| `--report FILE` | 报告路径，默认带时间戳不覆盖；`--report ""` 只打印不写文件 |
| `--workers N` | 并发线程数（默认 4） |
| `--limit N` | 只处理前 N 个任务，0=全部 |
| `--dry-run` | 只分析不写任何文件 |
| `--skip-existing` | 输出已存在就跳过，可用于断点续跑 |
| `--no-verify` | 跳过逐文件自检和合成后整体校验（不建议） |
| `--inspect FILE...` | 诊断模式：打印文件的 Motion Photo 结构，不做合成 |

退出码：`0` 全部成功，`1` 有文件失败或校验未通过，`2` 参数/目录有问题。

### `--dry-run` 用来做什么

走完整个分析流程但不写文件：扫描配对 → 解析每张 JPEG 的段结构 → 探测视频`ftyp`/`mvhd` → 生成完整 XMP → 算好 MPF 修正和 Padding 补偿 → 出完整报告。**所有可能出错的判断都跑了，只差最后落盘那一步。**

用途：大批量写盘前的预检（5.65 GB 的活儿，dry-run 只要 1 秒）、确认配对结果对不对、看会对文件做哪些结构处理（换一批新素材时能立刻发现没见过的结构）、试参数、估磁盘占用。报告里的「输出总字节」在 dry-run 下是**精确值**而非估算——那时图片部分已在内存里拼好，加上视频长度就是最终大小（实测 40 个文件逐个比对，字节数完全相同）。

### 校验脚本

合成结束后会自动调用，也可以单独跑：

```bash
python3 verify_motionphoto.py                  # 校验默认三个输出目录
python3 verify_motionphoto.py --single-out 相册
python3 verify_motionphoto.py --limit 10       # 抽查前 10 个
```

校验内容：

- **字节完整性** —— 视频末尾与源 mp4 逐字节一致；XMP 段之前与源图逐字节一致（EXIF 一个字节都不许动）；XMP 段之后最多 4 字节差异且只能落在 MPF 段内
- **XMP 合法性** —— 是合法 XML；`<rdf:RDF>...</rdf:RDF>` 单独截出来也能解析（厂商相册常这么干）；`xmlns:rdf` 声明在 `rdf:RDF` 上
- **踩坑回归** —— 不得出现 `MicroVideo*` / `OpCamera` / `oplus`，文件尾不得是 `SEFT`
- **容器目录** —— 首项 `Primary`；`MotionPhoto` 唯一且是最后一项；`GainMap` 排在`MotionPhoto` 之前；至少两项带 `Item:Padding`
- **定位一致性** —— 逐项累加 `Length+Padding` 与「文件长度 − MotionPhoto Length」必须一致且落在 `ftyp` box 上；`GainMap` 项指向的位置必须是 `FFD8FF` 且能解码
- **元数据** —— exiftool 逐标签比对 EXIF / ICC / MakerNotes / Composite
- **覆盖率** —— 源目录里每个照片/视频都被消费掉了（按「消费了哪些源文件」判断，与输出文件名无关）
- **文件属性** —— 修改时间、权限保留

**关键设计：它刻意不 `import make_motionphoto`**，把 JPEG 段扫描、XMP 提取、容器目录定位全部自己重写了一遍。这样合成和校验不会用同一套（错的）假设——本文第 3 节那个GainMap 对齐字节的 bug 就是靠这个独立性查出来的。调用方向永远是「合成 → 校验」。

校验要把输出全部重读一遍并逐字节比对，所以比合成慢：5.65 GB 的数据，合成 5~8 秒，校验约 1 分钟。

---

## 2. Motion Photo 格式与三家厂商对比

### 格式本身

规范：[Motion Photo format 1.0](https://developer.android.com/media/platform/motion-photo-format)

一个 Motion Photo 就是「主图 JPEG ＋ 追加在后面的 MP4」，位置写在主图的 XMP 里：

```
[主图 JPEG][（可选）Ultra HDR 的 GainMap 子图][（可选）厂商私有数据][MP4 视频]
```

读取器定位视频有两种算法：

- **倒推法**：`视频起点 = 文件长度 − MotionPhoto 的 Item:Length`（Google Photos、ExoPlayer 用这个；规范原文也说 `Item:Length` 取代了旧的 `MicroVideoOffset`）
- **累加法**：`视频起点 = 主图编码长度 + 逐项累加 Length 与 Padding`（exiftool 用这个，外加一个按 `ftyp` magic 重同步的兜底）

**实测发现：原厂文件并不保证两种算法都对得上**，详见下面的对比。本工具的输出两种算法都精确一致，且每一项都指向它实际所在的位置——校验脚本会逐个文件检查这一点。

### 三家真机样片对比

样片来自 OPPO Find X9 Ultra、Xiaomi 17T、Samsung Galaxy S24+，均为各自相机直出的实况照片；最后一列是本工具从 vivo X200 Pro 素材合成的输出，作为对照。

| | OPPO Find X9 Ultra | Xiaomi 17T | Samsung Galaxy S24+ | 本工具输出（vivo X200 Pro） |
|---|---|---|---|---|
| 文件大小 | 15.26 MB | 4.99 MB | 8.06 MB | 12.10 MB |
| **静态主图分辨率** | 5888 × 4416 | 3072 × 4096 | 4000 × 3000 | 4096 × 3072 |
| 主图编码长度 | 6 710 975 | 2 086 054 | 3 604 214 | 3 863 908 |
| **GainMap 分辨率** | 2944 × 2208（主图 1/2） | 1536 × 2048（1/2） | 1000 × 750（1/4） | 2048 × 1536（1/2） |
| GainMap 字节数 | 939 419 | 519 891 | 58 180 | 645 601 |
| ISO 21496-1 HDR 标记 | 无 | 有 | 有 | 无（源图只有 Adobe `hdrgm`） |
| **嵌入视频段数** | **2** | 1 | **2** | 1 |
| 主视频 | HEVC 1728×1296 +AAC 2.85 s | HEVC 1296×1728 +AAC 1.34 s | HEVC 1312×984 +AAC 3.08 s | H.264 1728×1296 +AAC 3.07 s |
| 次视频 | HEVC 1728×1296 无音轨 0.73 s | — | HEVC 1440×1080 无音轨 0.93 s | — |
| `MotionPhoto` 的 `Item:Length` | 8 346 922<br>**比正确值少 131 字节** | 2 630 378<br>（= 视频长度） | 4 082 307<br>（= 视频 + 尾部索引 104 字节） | 8 176 500<br>（= 视频长度） |
| 倒推法能否定位到视频起点 | **✗ 偏进 mp4 内部 131 字节** | ✓ | ✓ | ✓ |
| 累加法能否定位到视频起点 | ✓ | ✓ | ✓ | ✓ |
| 按目录能否定位到 GainMap | ✓ | ✓ | **✗ Padding 位置错** | ✓ |
| Samsung SEF trailer | 无 | 无 | **有，7 个标签** | 无 |
| 厂商私有 XMP | `OpCamera:*` 4 个 | 无 | 无 | 无 |
| EXIF `UserComment` | `oplus_8601468928` | 无 | 无 | 原样保留（vivo 调试串） |
| 封面帧位置 | 1.210 s / 2.85 s ≈ **42%** | 1.339 s / 1.34 s ≈ **100%** | 3.065 s / 3.08 s ≈ **99%** | 50%（duration/2） |

三家的**共同核心完全一致**，只有这三个 `GCamera` 属性加一个三项容器目录：

```xml
GCamera:MotionPhoto="1"
GCamera:MotionPhotoVersion="1"
GCamera:MotionPhotoPresentationTimestampUs="..."
Container:Directory → Primary + GainMap + MotionPhoto
```

差异全在「核心之外的附加层」，而且**两家原厂文件都有自相矛盾的地方**。

#### OPPO：`Item:Length` 少了 131 字节

额外写 `OpCamera:MotionPhotoOwner="oplus"` / `OLivePhotoVersion="2"` / `VideoLength` / `MotionPhotoPrimaryPresentationTimestampUs`，EXIF 的 `UserComment` 是 `oplus_` 开头的一串数字。布局：

```
0            主图 JPEG                                     6710975
6710975      GainMap（Item:Length=939419）                  7650394
7650394      mp4 #1：HEVC 1728×1296 +AAC 2.85s，6794201 字节  ← = OpCamera:VideoLength
14444595     4487 字节 OPPO 私有块（含 "lighthousetele" 字样） 14449082
14449082     mp4 #2：HEVC 1728×1296 无音轨 0.73s              15997447
```

真实的视频起点是 **7650394**（那里是 `ftyp` box），但 `MotionPhoto` 的`Item:Length=8346922` 倒推出来是 **7650525**，落在 mp4 内部（`moov` 里面）——正确值应该是 8347053，**少写了 131 字节**。用 ffprobe 从倒推位置切片，直接报`moov atom not found`。

也就是说 **ColorOS 自己并不用倒推法定位视频**，否则它连自己拍的照片都播不了。它大概是用累加法（`主图长度 + GainMap Length`，正好落在 7650394）或者直接扫 `ftyp`。

另外 `OpCamera:VideoLength=6794201` **不是**「视频距文件尾的字节数」，它等于第一段mp4 的长度。

#### Xiaomi：最干净

除标准标签外什么都没加，单段视频，两种定位算法都精确对得上。

#### Samsung：SEF trailer，且 `Padding` 挂错了位置

```
0            主图 JPEG                                       3604214
3604214      GainMap（Item:Length=58180）                     3662394
3662394      5 个信息标签：Image_UTC_Data / Camera_Scene_Info /
             Color_Display_P3 / Photo_HDR_Info /
             Camera_Capture_Mode_Info                144 字节  3662538
3662538      SEF 标签 MotionPhoto_AutoPlay（703443 字节）
               └ 28 字节头 + mp4：HEVC 1440×1080 0.93s 无音轨   4365981
4365981      SEF 标签 MotionPhoto_Data（4082227 字节）
               └ 24 字节头 + mp4：HEVC 1312×984 +AAC 3.08s     8448208
8448208      SEFH 索引(7 条) + 长度 + "SEFT"           104 字节  8448312
```

XMP 里把 `Item:Padding=703611` 写在了 **`Primary`（第一项）上**，而这段空隙（144 + 703443 + 24 = 703611，数值完全对得上）**实际位于 GainMap 之后**。后果是：

- 视频定位没问题——加法可交换，`3604214 + 703611 + 58180 = 4366005` 正好是视频起点
- 但按容器目录定位 GainMap 会算到 `3604214 + 703611 = 4307825`，那里不是 JPEG 起点。gainmap 真实位置是 3604214（解码出来 1000×750）

顺便一提，上一张三星样片把这个 `Padding` 写在 `GainMap` 项上（语义正确，因为那张的信息标签排在视频之后）。规范原文说「只有第一个 item 可以带 Padding」，三星两种写法都出现过，可见这个字段在真机上并不严谨。

#### `MotionPhoto` 的 `Item:Length` 到底是什么

不是「视频的字节长度」，而是「**视频起点到文件尾**」。三星把尾部 104 字节的 SEFH 索引算进去了；OPPO 本意也是这样（想涵盖两段视频 + 私有块），只是算差了 131 字节。小米单段视频且尾部无附加数据，所以恰好等于视频长度。

### 那段短的无音轨次视频是干什么的

三星的 SEF 标签名字直接叫 `MotionPhoto_AutoPlay`，基本自证了用途：相册**自动播放**用的那一小段。推断（合理但无直接证据）是相册列表里滚动经过时需要立刻起播、循环、且不能出声，这种场景要的就是一段极短、无音轨、解码开销极小的片段；完整带音频的版本只在点开时才播。OPPO 那段没有名字（前面是一个 4487 字节私有块），但形态一样（0.73 s、无音轨），按类比推断同一用途。

规范里也留了口子：允许一条可选的次视频轨，读取器「应当用它的内容来替代静态主图显示」，让「静态图 → 开始动」的切换不突兀。三星那段分辨率比主视频略高（1440×1080 vs 1312×984）算符合这个描述，但 OPPO 那段和主视频分辨率完全相同（都是 1728×1296），这条解释对 OPPO 不成立。

**这两段都是可选的**，本工具输出单段视频，三家手机都能识别和播放。

### 封面帧位置

`MotionPhotoPresentationTimestampUs` 指明静态图对应视频里的哪一帧。小米和三星都放在**最后一帧**（100%/99%），说明它们录的是「快门之前」那一段；OPPO 放在 **42%**，说明它录的是快门前后各一段。

本工具用 `duration/2`。这个选择是有依据的：拿 vivo 原始素材的照片 EXIF 时刻对比 mp4的 `creation_time` 反推，快门大约落在视频的 37%~46%（三个样本），和 OPPO 的 42% 也接近。规范同样规定该属性缺失时读取器默认取中点。

---

## 3. 研究过程与踩过的坑

整个格式兼容性是靠**单变量对照实测**一步步锁定的：每轮只改一个变量，生成两组文件，在真机上看识别结果。下面按时间顺序记录，包括所有走错的路。

### 坑 1：源素材是 Ultra HDR，不能简单「写个 XMP + cat 视频」

一开始以为就是「XMP 里写上视频长度，把 mp4 追加到 JPG 后面」。实际打开 vivo 的照片才发现它是 **Ultra HDR**：已经有一个 `GContainer` 目录（`Primary` + `GainMap`）、一张追加在后面的 gainmap 子图、以及一个 MPF（多图索引）APP2 段。

所以必须：把 `MotionPhoto` 项**追加到 `GainMap` 之后**（规范要求 gainmap 排在 video之前），并且因为插入 XMP 段改变了段长度，**MPF 里的主图长度字段要跟着修正**。

MPF 的偏移是相对「MP Endian 字段起始处」的，所以只要把 XMP 段插在 MPF 段**之前**，基准点和后面的图像数据一起平移，相对偏移就仍然有效——只需要修正主图的长度字段（2 字节）。

### 坑 2：vivo 在文件尾部塞了私有数据

文件末尾还有 161 KB（部分照片是 385 字节）的 vivo 私有 `streamdata` 块，不被容器目录描述。不处理的话，「累加法」的读取器会把视频位置算错 161 KB。

处理办法：把这段字节并进上一个 secondary item（`GainMap`）的 `Length`。这样两种定位算法都能落到正确位置，而该项本身仍可正常解码（JPEG 解码器在 EOI 处停止，忽略多余尾字节）。没有 secondary item 可归并时（6 张照片），记进 `Primary` 的 `Item:Padding`。

### 坑 3：exiftool 会因为「只有一个 item 带 Padding」而崩溃

给 `Primary` 写了非零 `Item:Padding` 后，`exiftool 13.50` 直接报`Can't use string ("385") as an ARRAY ref ... Trailer.pm line 276`，整个文件读不出来。

翻源码发现 exiftool 把 `DirectoryItemPadding` 当数组解引用，而 XMP 里只有一个 item 带Padding 时它拿到的是标量。那行代码上面还有一句注释：*"(haven't seen non-zero padding, but I assume this is how it works"* ——作者自己都说没测过。

解法是从三星真机文件里学来的：**至少让两个 item 带 `Padding`**（其余填 0），凑成等长列表就绕过了这个 bug。

### 坑 4（最大的坑）：ElementTree 把 xmlns 全提到根元素，小米认不出来

第一版用 `ElementTree` 序列化 XMP，它会把所有 `xmlns:` 声明**全部提到根元素**上：

```xml
<x:xmpmeta xmlns:Container="..." xmlns:GCamera="..." xmlns:rdf="..." xmlns:x="...">
  <rdf:RDF>                                  <!-- 没有 xmlns:rdf -->
    <rdf:Description GCamera:MotionPhoto="1"  <!-- 没有 xmlns:GCamera -->
```

真机文件是 `xmlns:rdf` 在 `rdf:RDF` 上、厂商命名空间在 `rdf:Description` 上。XML 语义上两者完全等价，但厂商相册的解析器经常是「正则截出 `<rdf:RDF>...</rdf:RDF>` 再单独解析」，这时提到根元素上的声明就全丢了，前缀未声明 → 解析失败 → 当成普通照片。

改成自己接管 XMP 序列化、产出和真机一致的布局后，小米就能识别了。同时把前缀从`Camera:` 改成通用的 `GCamera:`（前缀在 XML 里无所谓，但厂商解析器经常按字面匹配）。

校验脚本里加了对应的回归项：`rdf:RDF` 片段单独解析必须成立。

### 坑 5（OPPO 认不出的真凶）：写了已废弃的 `MicroVideo*`

小米通了，OPPO 却只认 HDR 不认动图。这轮我一次改了三处，然后用单变量对照才定位到元凶：

| 变体（只差一个变量） | 小米 | OPPO |
|---|---|---|
| **S**：写 `GCamera:MicroVideo*` 那 4 个属性 | 认 | **不认** |
| **T**：不写，仅 `Container:Item` 属性顺序不同 | 认 | 认 |

**ColorOS 相册一看到 `GCamera:MicroVideo="1"` 就认定这是旧格式文件，走进它不支持的旧 MicroVideo 分支，从而放弃解析后面标准的 `Container:Directory`，直接当普通照片。**

#### 规范其实明确禁止这么做

规范的属性表里把 `Camera:MicroVideo` / `MicroVideoVersion` / `MicroVideoOffset` / `MicroVideoPresentationTimestampUs` 四个单独列出来，说明它们属于 MicroVideo V1、在本规范中已删除、读取器遇到时必须忽略，并特别指出 `MicroVideoOffset` 的功能已由 `GContainer` 的 `Item:Length` 取代（内容据许可要求改写）。答案就写在我一开始就读过的那份文档里。

#### 那我为什么还是写了

因为我**照抄了参考实现，而不是照着规范做**。手边这几个项目里有三个都在写这组标签：

| 项目 | 情况 |
|---|---|
| `live-photo-conv` | `livemaker.vala` 里 `MicroVideo*` 四个和 `MotionPhoto*` 三个一起写 |
| pip 的 `motionphoto` | 只写 `MicroVideo*` + 三星 trailer，压根没写 `Container:Directory` |
| flashlab 的网页工具 | 默认 XMP 模板里 `MicroVideo*` 齐全 |

（`MotionPhoto2` 是个反例，它的模板里只有 `MotionPhoto*` 三个，没有 `MicroVideo*`——我当时没注意到这个差别。）

再加上搜到两条信息强化了判断：一是有 issue 写着「Android 11 之前看 `MicroVideo`，之后看 `MotionPhoto`」；二是有人说「小米的动态照片用的是旧版 MotionPhoto 格式」。于是推理变成了：两组都写，新读取器认 `MotionPhoto`、老读取器认 `MicroVideo`，向后兼容，**加上又没坏处**。

#### 这个推理错在三个地方

1. **把「实现共识」当成了规范。** 几个项目都这么写，只说明它们都在照抄更早的实现。规范原文和实现打架时，我选择了相信实现。
2. **「加了没坏处」是个没验证过的假设。** 规范说读取器「必须忽略」，但厂商不一定守规矩——ColorOS 恰恰是据此分支。规范里的 MUST 是对读取器的约束，不是对写入器的免责声明。
3. **本可以更早拿到真机样片。** 三张原厂样片里一个 `MicroVideo` 标签都没有，包括那台被说成「用旧版格式」的小米。先要样片对照、而不是先抄开源项目，这个坑根本不会踩。

#### 一个值得注意的细节

我写进去的**值本身是对的**：`MicroVideoOffset` 的语义是「视频起点距文件尾的字节数」，等于视频长度，和文件布局完全吻合。所以这不是「值算错了」的问题——**仅仅是这个标志位存在，就足够让 ColorOS 走错分支**。这也是为什么当初三处改动一起上、必须靠单变量对照才能定位到它。而且按规范 `MicroVideoOffset` 的功能已被 `Item:Length` 完全覆盖，所以哪怕只从「向后兼容」的动机看，它也是纯冗余的。

现在本工具永不写这组标签，源文件里带了也会清掉，`build_xmp()` 里留了「不要顺手补上」的注释，校验脚本也有回归项——输出里一旦出现 `MicroVideo` 字样就报错。

### 坑 6：一路上加过的东西，事后逐个证否

定位坑 5 的那一轮里我还顺手做了几件事，后来靠单变量测试逐个证否，四样全删了：

| 我加过的东西 | 出处 | 实测结论 |
|---|---|---|
| EXIF `UserComment = oplus_8388608` | OPPO 真机样片里是 `oplus_` 加一串数字；某个开源网页工具的 OPPO 模式写死了 `oplus_8388608` 这个值 | **不需要**。不改 EXIF 照样识别 → 删掉，回到「EXIF 一个字节都不动」 |
| `OpCamera:*` 4 个私有属性 | OPPO 真机样片 | **不需要**。小米样片一个都没有，OPPO 照样能识别 → 删掉 |
| Samsung SEF trailer（`mpv3`） | MotionPhoto2 复刻 Galaxy S23 的写法 | **不需要**。三星 Gallery 认标准格式 → 删掉 |
| `--flat-container`（把容器目录压平成两项） | 我自己防「写死了视频是第 2 项」的假想读取器 | **无用**。三家都用倒推法定位视频；而且压平后三家 HDR 仍正常（它们靠 MPF 找 gainmap，不看容器目录）→ 删掉 |
| `--name-style`（`MVIMG_` 前缀 / `MP` 后缀） | 规范原文允许读取器在文件名不匹配 `^...MP\.(jpg\|jpeg\|heic\|avif)$` 时**直接忽略**动态照片；`MVIMG_` 是旧版 Google Camera 约定 | 三家都不看文件名。保留了这个开关，零风险且规范推荐，可能对别的软件有用 |

一句话总结：**三家相册认的就是同一套纯 Google 标准元数据，任何厂商私有的东西都不需要。** OPPO 之前认不出，不是因为缺了 OPPO 私有的东西，而是因为**多写了会把它带偏的东西**。

这也解释了「为什么小米拍的照片在 OPPO 上能识别」——小米文件里只有标准元数据，说明 ColorOS 本来就完整支持 Google 标准格式。

### 坑 7：GainMap 前面的对齐字节（靠独立校验查出来的）

给校验脚本加「按容器目录定位并解码 GainMap」这一项后，319 张里报出 8 张失败。

查下来是真 bug：vivo 在这 8 张照片的主图 EOI 和 gainmap 的 SOI 之间塞了 2~3 个 `\x00`对齐字节，源 XMP 里没有用 `Padding` 描述。我原来把「所有未被描述的字节」（前置这 2字节 + 尾部 438 字节厂商数据）一起加进了 `GainMap` 的 `Length`，结果这一项指向的位置开头是 `\x00\x00`，严格的 Ultra HDR 读取器解不出 gainmap，**Google Photos 上这 8 张的HDR 大概会失效**。手机上没暴露，正是因为三家都走 MPF 找 gainmap。

修法是区分前后两段空隙：**前置**对齐字节记到前一项的 `Item:Padding`，只有**尾部**的厂商数据才并进 `Length`：

```
[1] Primary      Length=0       Padding=2         ← 那 2 字节对齐填充
[2] GainMap      Length=250048  Padding=0         ← 现在正好指向 FFD8
[3] MotionPhoto  Length=7166578
```

判定「实际起点」用的是在 64 字节小窗口内找 `FFD8FF`，找不到就不动，不会误伤正常文件（313 张里只有这 8 张触发）。

**这个 bug 只可能被独立实现的校验脚本发现**——如果校验复用合成脚本的解析逻辑，两边会用同一套错误假设，永远查不出来。

### 坑 8：传输方式

微信、QQ、Google Photos 这类会重新编码或剥掉 JPEG 尾部数据，那样元数据写得再对也没用。要用数据线/MTP、LocalSend 之类不改文件的方式。传完可以在手机上核对文件大小是否和电脑上一致（合成后的文件应该明显变大）。

---

## 4. 最终结论：写什么、不写什么

真机实测通过：OPPO Find X9 Ultra / ColorOS，Xiaomi 17T / HyperOS，Samsung Galaxy S24+ / One UI。

**写**（纯 Google 标准，一个厂商私有标签都没有）：

```xml
<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="...">
  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
    <rdf:Description rdf:about=""
        xmlns:hdrgm="http://ns.adobe.com/hdr-gain-map/1.0/"
        xmlns:GCamera="http://ns.google.com/photos/1.0/camera/"
        xmlns:Container="http://ns.google.com/photos/1.0/container/"
        xmlns:Item="http://ns.google.com/photos/1.0/container/item/"
      hdrgm:Version="1.0"
      GCamera:MotionPhoto="1"
      GCamera:MotionPhotoVersion="1"
      GCamera:MotionPhotoPresentationTimestampUs="1533700">
      <Container:Directory>
        <rdf:Seq>
          <rdf:li rdf:parseType="Resource">
            <Container:Item Item:Mime="image/jpeg" Item:Semantic="Primary"
                            Item:Length="0" Item:Padding="0"/>
          </rdf:li>
          <rdf:li rdf:parseType="Resource">
            <Container:Item Item:Mime="image/jpeg" Item:Semantic="GainMap"
                            Item:Length="645601" Item:Padding="0"/>
          </rdf:li>
          <rdf:li rdf:parseType="Resource">
            <Container:Item Item:Mime="video/mp4" Item:Semantic="MotionPhoto"
                            Item:Length="8176500"/>
          </rdf:li>
        </rdf:Seq>
      </Container:Directory>
    </rdf:Description>
  </rdf:RDF>
</x:xmpmeta>
```

**不写**：

| 项目 | 原因 |
|---|---|
| `GCamera:MicroVideo*` | **有害**，写了 OPPO 就认不出（源文件带了也会清掉） |
| `OpCamera:*` | 不需要 |
| EXIF `UserComment = oplus_xxx` | 不需要，且会破坏「EXIF 不动」 |
| Samsung SEF trailer | 不需要 |
| 短的无音轨次视频 | 可选，不影响识别 |

**必须注意的实现细节**：

- `xmlns` 声明要放在真机的位置上（`xmlns:rdf` 在 `rdf:RDF` 上、其余在`rdf:Description` 上），不能全提到根元素
- 前缀用 `GCamera` / `Container` / `Item` 这一套通用写法
- 至少两个 item 带 `Item:Padding`，否则 exiftool ≤13.50 读取时会崩
- 容器目录里的 `GainMap` 项必须保留：三家相册不看它（走 MPF），但 Google Photos 的Ultra HDR 依赖它
- 插入 XMP 段后要修正 MPF 里的主图长度字段
- secondary item 的实际起点可能因对齐填充而后移，前置空隙要记到前一项的 `Padding`

---

## 5. 目录结构

### 进仓库的（约 28 MB）

```
make_motionphoto.py                  合成脚本，零第三方依赖
verify_motionphoto.py                独立校验脚本，合成结束后自动调用
README.md                            本文件
.gitignore
samples/                             三家真机相机直出的实况照片，格式分析的依据
├── OPPO_sample.jpg                   OPPO Find X9 Ultra   15.3 MB
├── Xiaomi_sample.jpg                 Xiaomi 17T            5.0 MB
└── Samsung_sample.jpg                Samsung Galaxy S24+   8.1 MB
```

样片，使用`python3 make_motionphoto.py --inspect samples/新样片.jpg` 就能读出它的结构来参考。

### 不进仓库的（已在 `.gitignore` 里屏蔽）

```
DCIM/                                数据源，本例是 vivo X200 Pro 导出的 jpg + mp4 配对
└── Camera/2025/{08,09}/               IMG_20250827_180604.jpg + .mp4  ← 同名即为一对

motionphoto_output/                  合成好的 Motion Photo
└── DCIM/Camera/2025/{08,09}/          复用数据源的完整目录结构
staticphoto_output/                  没有同名视频、未合成的静态照片
└── DCIM/Camera/2025/{08,09}/
movieout/                            没有同名照片的普通视频
└── DCIM/Camera/2025/{08,09}/

motionphoto_report_年月日_时分秒.txt   每次运行的报告，不覆盖历史
```

三个输出目录都在自己下面重建一层数据源目录名（本例是 `DCIM/`），再往下完整复用源目录层级，所以输出目录之间、以及和源目录之间路径可以直接对应。加`--single-out [目录]` 则三类输出合并到同一棵树里。
