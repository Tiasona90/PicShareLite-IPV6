import os
import threading
import time
import logging
import subprocess
import socket
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import shutil
import urllib.parse
import re
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, send_file, render_template_string, request, abort, url_for, jsonify
from PIL import Image
# ====== 0. 全局变量 & 配置 (不变) ======
gui_app = None


class ServerState:
    def __init__(self):
        self.base_dir = r"F:\共享照片"
        self.preview_subdir = "._preview_ipv6_opt"
        self.marked_subdir = "被标记的照片"

        # [修改] 提高分辨率到 640x640
        self.thumb_size = (640, 640)
        self.thumb_quality = 60
        self.port = 5000

        # 定义 RAW 扩展名 (这些文件将被禁止查看原图)
        self.raw_extensions = {
            '.cr2', '.cr3', '.nef', '.arw', '.dng', '.orf', '.rw2', '.pef', '.sr2'
        }

        # 允许扫描的所有扩展名 (RAW + 普通图片)
        self.allowed_extensions = {
                                      '.jpg', '.jpeg', '.png', '.bmp', '.webp', '.gif', '.heic'
                                  } | self.raw_extensions  # 合并集合


state = ServerState()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s', encoding='utf-8')
logger = logging.getLogger(__name__)


def update_global_status(message):
    if gui_app:
        gui_app.update_status(message)


# ====== 1. 核心逻辑工具 (不变) ======
def safe_join(base_path: str, *paths: str) -> Path:
    try:
        base = Path(base_path).resolve()
        decoded_paths = [urllib.parse.unquote(p) for p in paths]
        final_path = base.joinpath(*decoded_paths).resolve()
        if base in final_path.parents or base == final_path:
            return final_path
        return None
    except Exception:
        return None


class PreviewGenerator:
    def __init__(self):
        # 线程池用于并发扫描和生成
        self.executor = ThreadPoolExecutor(max_workers=8)
        self.scanned_files = set()

    @staticmethod
    def generate_raw_preview_with_magick(original_path: Path, preview_path: Path) -> bool:
        """
        使用 ImageMagick 命令行工具 (magick) 生成 RAW 预览图。
        修复了参数传递问题，并增加了 Windows 下隐藏黑框的处理。
        """
        command = 'magick'

        try:
            # 1. 确保目标预览文件夹存在
            preview_path.parent.mkdir(parents=True, exist_ok=True)

            # 2. 构造 Magick 命令
            # -auto-orient : 根据 EXIF 自动旋转图片 (RAW文件常需要这个)
            # -thumbnail   : 生成缩略图
            # -quality     : JPEG 质量
            magick_cmd = [
                command,
                str(original_path),
                '-auto-orient',
                '-thumbnail', f"{state.thumb_size[0]}x{state.thumb_size[1]}>",
                '-quality', str(state.thumb_quality),
                f"JPG:{str(preview_path)}"
            ]

            logger.info(f"⚡ 尝试用 Magick 生成: {original_path.name}")

            # [新增] 防止 Windows 下弹出黑色命令行窗口
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

            # 3. 执行命令
            result = subprocess.run(
                magick_cmd,
                capture_output=True,
                text=True,
                timeout=60,  # 增加超时时间到 60秒
                check=False,
                startupinfo=startupinfo  # 应用隐藏窗口设置
            )

            # 4. 检查结果
            if result.returncode != 0:
                logger.error(f"❌ Magick 失败 (代码 {result.returncode}): {original_path.name}")
                if result.stderr.strip():
                    logger.error(f"   错误信息: {result.stderr.strip()}")
                return False

            # 5. 验证文件是否有效
            if preview_path.exists() and preview_path.stat().st_size > 1024:
                logger.info(f"✅ Magick 成功: {original_path.name}")
                return True
            else:
                logger.warning(f"⚠️ Magick 运行成功但文件无效: {original_path.name}")
                return False

        except FileNotFoundError:
            logger.error(f"🚨 找不到命令 '{command}'。请确认 ImageMagick 已安装并添加到 PATH 环境变量。")
            return False
        except subprocess.TimeoutExpired:
            logger.error(f"⏱️ Magick 处理超时: {original_path.name}")
            return False
        except Exception as e:
            logger.exception(f"Magick 运行时异常: {original_path.name} - {e}")
            return False

    @staticmethod
    def extract_embedded_thumbnail(image_path: Path) -> Image.Image | None:
        """尝试从 RAW 文件中提取内嵌的 JPEG 缩略图"""
        try:
            from PIL import Image, ExifTags, JpegImagePlugin
            from io import BytesIO

            with open(image_path, 'rb') as f:
                img = JpegImagePlugin.JpegImageFile(f)
                exif = img.getexif()
                if exif:
                    for tag, value in exif.items():
                        if ExifTags.TAGS.get(tag) == 'JPEGInterchangeFormat':
                            offset = value
                            length_tag = next(
                                (k for k, v in ExifTags.TAGS.items() if v == 'JPEGInterchangeFormatLength'), None)
                            length = exif.get(length_tag, 0) if length_tag else 0
                            if offset and length:
                                f.seek(offset)
                                thumbnail_data = f.read(length)
                                return Image.open(BytesIO(thumbnail_data))
        except Exception:
            pass
        return None

    def generate_sync(self, original_path: Path, preview_path: Path):
        """
        同步生成预览图逻辑：
        1. 检查是否存在 -> 2. PIL 读取 -> 3. 提取内嵌缩略图 -> 4. ImageMagick 转码
        """
        try:
            from PIL import Image, ImageOps

            # 检查文件是否已存在且大小正常
            if preview_path.exists() and preview_path.stat().st_size > 100:
                return True

            preview_path.parent.mkdir(parents=True, exist_ok=True)
            img = None

            # 定义 RAW 扩展名集合
            raw_exts = {'.cr2', '.cr3', '.nef', '.arw', '.dng', '.orf', '.rw2', '.pef', '.sr2'}
            is_raw = original_path.suffix.lower() in raw_exts

            # [尝试 1] 直接用 PIL 打开 (适合 JPG, PNG, 部分简单 RAW)
            try:
                with Image.open(original_path) as im:
                    im.load()
                    img = im.copy()
            except Exception:
                img = None

            # [尝试 2] 如果是 RAW 且 PIL 失败，尝试提取内嵌预览图
            if img is None and is_raw:
                img = self.extract_embedded_thumbnail(original_path)

            # [尝试 3] 如果前两者都失败，且是 RAW，调用 ImageMagick
            if img is None and is_raw:
                # 注意：Magick 会直接生成文件，不需要后续的 PIL save 操作
                # 直接返回 Magick 的执行结果
                return self.generate_raw_preview_with_magick(original_path, preview_path)

            # 如果以上方法都无法获取图像对象，则宣告失败
            if img is None:
                return False

            # === 保存逻辑 (仅针对 PIL 或 内嵌缩略图 成功的情况) ===
            img = ImageOps.exif_transpose(img)  # 处理手机照片的旋转
            if img.mode != "RGB":
                img = img.convert("RGB")

            # 缩放并保存
            img.thumbnail(state.thumb_size, Image.Resampling.LANCZOS)
            img.save(preview_path, "JPEG", quality=state.thumb_quality, optimize=True)
            return True

        except Exception as e:
            # 这里的日志级别改为 ERROR，确保你能看到为什么失败
            logger.error(f"生成预览图最终失败: {original_path} \n原因: {e}")
            return False

    def generate_task(self, original_path, preview_path):
        self.generate_sync(original_path, preview_path)

    def scan_all(self, root_path: Path):
        if not root_path.exists():
            return
        update_global_status("⏳ 正在后台预热缩略图...")
        count = 0
        try:
            for item in root_path.iterdir():
                # 跳过系统文件夹
                if item.name in (state.marked_subdir, state.preview_subdir):
                    continue

                if item.is_dir():
                    for file_path in item.rglob("*"):
                        if not file_path.is_file():
                            continue
                        if file_path.suffix.lower() not in state.allowed_extensions:
                            continue
                        # 防御性检查
                        if state.marked_subdir in file_path.parts or state.preview_subdir in file_path.parts:
                            continue

                        try:
                            rel_path = file_path.relative_to(root_path)
                            preview_path = root_path / state.preview_subdir / rel_path

                            if str(preview_path) not in self.scanned_files:
                                if not preview_path.exists():
                                    self.executor.submit(self.generate_task, file_path, preview_path)
                                    count += 1
                                self.scanned_files.add(str(preview_path))
                        except ValueError:
                            continue

            if count > 0:
                update_global_status(f"⚡ 处理中: {count} 张新图片")
            else:
                update_global_status("✅ 就绪: 所有图片已索引")
        except Exception as e:
            logger.exception("扫描出错")


generator = PreviewGenerator()


def get_ipv6_addresses_v2():
    addrs = set()
    try:
        if os.name == 'nt':
            result = subprocess.run(['ipconfig'], capture_output=True, text=True, encoding='gbk', errors='ignore',
                                    check=False)
            lines = result.stdout.splitlines()
            for line in lines:
                if 'IPv6 地址' in line:
                    ip = line.split()[-1].strip()
                    if not ip.startswith(('fe80:', '::1')):
                        ip = ip.split('%')[0].strip()
                        addrs.add(ip)
        else:
            result = subprocess.run(['ip', 'addr'], capture_output=True, text=True, check=False)
            lines = result.stdout.splitlines()
            for line in lines:
                if 'inet6' in line and 'global' in line:
                    match = re.search(r'inet6\s+([\w:]+)/\d+', line)
                    if match:
                        addrs.add(match.group(1).strip())
    except Exception:
        pass
    return list(addrs)


# ====== 2. Web 模板设计 (增加加载进度条) ======
app = Flask(__name__)


@app.after_request
def add_header(response):
    if 'image' in response.mimetype:
        response.headers['Cache-Control'] = 'public, max-age=604800'
    return response


# 现代 SVG 图标定义
ICONS = {
    'back': '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 18l-6-6 6-6"/></svg>',
    'star_empty': '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"></polygon></svg>',
    'star_fill': '<svg width="24" height="24" viewBox="0 0 24 24" fill="#FFD700" stroke="#FFD700" stroke-width="2"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"></polygon></svg>',
    'hd': '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><line x1="12" y1="3" x2="12" y2="21"/><path d="M7 12h-2"/><path d="M7 15h-2"/><path d="M17 12h2"/><path d="M17 15h2"/></svg>',
    'close': '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>',
    'prev': '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6"></polyline></svg>',
    'next': '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"></polyline></svg>',
}

CSS_STYLE = '''
:root { --bg: #000; --bar-bg: rgba(20, 20, 20, 0.85); --accent: #0A84FF; --text: #fff; }
body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; background: var(--bg); color: var(--text); margin: 0; overflow-x: hidden; -webkit-tap-highlight-color: transparent; }

/* 导航栏 */
.navbar { position: fixed; top: 0; width: 100%; height: 44px; z-index: 100;
    background: var(--bar-bg); backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px);
    border-bottom: 0.5px solid rgba(255,255,255,0.1);
    display: flex; align-items: center; justify-content: space-between;
    padding: env(safe-area-inset-top) 10px 0 10px; height: calc(44px + env(safe-area-inset-top)); }
.nav-btn { color: var(--accent); background: none; border: none; padding: 10px; cursor: pointer; display: flex; align-items: center;}
.nav-title { font-weight: 600; font-size: 17px; max-width: 60%; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

/* 网格布局 */
.grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 2px; padding: calc(50px + env(safe-area-inset-top)) 0 20px 0; }
@media (min-width: 600px) { .grid { grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 4px; padding-left: 4px; padding-right: 4px;} }
.cell { aspect-ratio: 1; background: #1c1c1e; overflow: hidden; position: relative; cursor: pointer;}
.cell img { width: 100%; height: 100%; object-fit: cover; opacity: 0; transition: opacity 0.4s ease; will-change: opacity; }
.cell img.loaded { opacity: 1; }

/* 图片查看器 */
.viewer { display: none; position: fixed; inset: 0; background: #000; z-index: 200; flex-direction: column; animation: fadeIn 0.2s ease-out; }
@keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
.v-header { position: absolute; top: 0; width: 100%; padding-top: env(safe-area-inset-top); display: flex; justify-content: flex-end; z-index: 202; pointer-events: none;}
.v-close { pointer-events: auto; padding: 15px; background: none; border: none; color: #fff; opacity: 0.8; }
.v-main { flex: 1; display: flex; align-items: center; justify-content: center; width: 100%; height: 100%; }
.v-main img { max-width: 100%; max-height: 100%; object-fit: contain; transition: opacity 0.2s; }

/* 新增：图片加载动画/进度条 */
.v-loading-overlay {
    position: absolute;
    inset: 0;
    display: none; /* 默认隐藏 */
    align-items: center;
    justify-content: center;
    background: rgba(0, 0, 0, 0.7);
    z-index: 201;
    color: white;
    font-size: 14px;
    flex-direction: column;
}
.loader {
    border: 4px solid rgba(255, 255, 255, 0.3);
    border-top: 4px solid #fff;
    border-radius: 50%;
    width: 30px;
    height: 30px;
    animation: spin 1s linear infinite;
    margin-bottom: 10px;
}
@keyframes spin {
    0% { transform: rotate(0deg); }
    100% { transform: rotate(360deg); }
}

/* 底部控制栏 */
.controls { position: absolute; bottom: 0; width: 100%; padding-bottom: env(safe-area-inset-bottom);
    background: var(--bar-bg); backdrop-filter: blur(25px); -webkit-backdrop-filter: blur(25px);
    border-top: 0.5px solid rgba(255,255,255,0.1);
    display: flex; justify-content: space-around; align-items: center; height: calc(60px + env(safe-area-inset-bottom)); z-index: 202;}
.c-btn { background: none; border: none; color: #fff; padding: 10px 10px; display: flex; flex-direction: column; align-items: center; font-size: 10px; gap: 4px; opacity: 0.7; transition: all 0.2s; }
.c-btn:active { transform: scale(0.9); opacity: 1; }
.c-btn svg { width: 24px; height: 24px; }
.c-btn.active { color: #FFD700; opacity: 1; text-shadow: 0 0 10px rgba(255, 215, 0, 0.4); }
.c-btn.hd-active { color: var(--accent); opacity: 1; }

/* 首页卡片 */
.card-container { display: flex; align-items: center; justify-content: center; height: 100vh; background: #000; }
.card { background: #1c1c1e; padding: 40px 30px; border-radius: 24px; width: 85%; max-width: 340px; text-align: center; border: 1px solid #333; }
.card h2 { margin-top: 0; color: #fff; font-weight: 700; }
.card input { width: 100%; padding: 16px; margin: 20px 0; border-radius: 14px; background: #2c2c2e; border: none; color: #fff; font-size: 16px; text-align: center; outline: none; }
.card button { width: 100%; padding: 16px; border-radius: 14px; background: var(--accent); border: none; color: #fff; font-size: 16px; font-weight: 600; cursor: pointer; }
.card button:active { opacity: 0.8; }
'''

ALBUM_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="theme-color" content="#000000">
    <title>{{ album_name }}</title>
    <style>''' + CSS_STYLE + '''
    /* [新增] 禁用按钮的样式 */
    .c-btn.disabled {
        opacity: 0.2 !important;
        pointer-events: none;
        filter: grayscale(100%);
    }
    </style>
</head>
<body>
    <div class="navbar">
        <a href="/" class="nav-btn">''' + ICONS['back'] + '''&nbsp;返回</a>
        <div class="nav-title">{{ album_name }}</div>
        <div style="width: 44px;"></div>
    </div>

    <div class="grid">
        {% for photo in photos %}
        <div class="cell" onclick="openViewer({{ loop.index0 }})">
            <img data-src="{{ photo.preview }}" loading="lazy">
        </div>
        {% endfor %}
    </div>

    <div class="viewer" id="viewer">
        <div class="v-header">
            <button class="v-close" onclick="closeViewer()">''' + ICONS['close'] + '''</button>
        </div>
        <div class="v-main">
            <img id="v-img" onclick="next()"> 
        </div>

        <div class="v-loading-overlay" id="loading-overlay">
            <div class="loader"></div>
            <span>正在加载原图...</span>
        </div>

        <div class="controls">
            <button class="c-btn" onclick="prev(event)">
                <div>''' + ICONS['prev'] + '''</div>
                <span>上一张</span>
            </button>

            <button class="c-btn" id="mark-btn" onclick="toggleMark(event)">
                <div id="mark-icon">''' + ICONS['star_empty'] + '''</div>
                <span>收藏</span>
            </button>

            <button class="c-btn" id="orig-btn" onclick="toggleOriginal(event)">
                <div id="hd-icon">''' + ICONS['hd'] + '''</div>
                <span>原图</span>
            </button>

            <button class="c-btn" onclick="next(event)">
                <div>''' + ICONS['next'] + '''</div>
                <span>下一张</span>
            </button>
        </div>
    </div>

    <script>
        const photos = {{ photos | tojson }};
        const albumName = "{{ album_name }}";
        let curIdx = 0;
        let isOrig = false;

        let markedState = {}; 

        // Lazy Load Logic
        const observer = new IntersectionObserver((entries, obs) => {
            entries.forEach(e => {
                if(e.isIntersecting) {
                    const img = e.target;
                    img.src = img.dataset.src;
                    img.onload = () => img.classList.add('loaded');
                    obs.unobserve(img);
                }
            });
        }, {rootMargin: "200px"});
        document.querySelectorAll('img[data-src]').forEach(img => observer.observe(img));

        // Viewer Logic
        const viewer = document.getElementById('viewer');
        const vImg = document.getElementById('v-img');
        const markBtn = document.getElementById('mark-btn');
        const markIcon = document.getElementById('mark-icon');
        const origBtn = document.getElementById('orig-btn');
        const loadingOverlay = document.getElementById('loading-overlay');

        const ICONS = {
            empty: `''' + ICONS['star_empty'] + '''`,
            fill: `''' + ICONS['star_fill'] + '''`
        };

        function showLoading(show) {
            loadingOverlay.style.display = show ? 'flex' : 'none';
        }

        function openViewer(idx) { 
            curIdx = idx; 
            viewer.style.display = 'flex'; 
            loadPhoto(); 
        }

        function closeViewer() { 
            viewer.style.display = 'none'; 
            vImg.src = '';
            showLoading(false); 
        }

        function loadPhoto() {
            // 每次切换图片，重置原图状态
            isOrig = false;
            showLoading(false); 

            // 加载预览图
            vImg.style.opacity = 0.3;
            vImg.src = photos[curIdx].preview;
            vImg.onload = () => vImg.style.opacity = 1;

            // [修改] 更新原图按钮状态（检查是否为 RAW）
            updateOrigUI();

            // 检查收藏状态
            const currentFile = photos[curIdx].filename;
            if (currentFile in markedState) {
                renderMark(markedState[currentFile]);
            } else {
                renderMark(false);
                fetch(`/api/check_mark?album=${encodeURIComponent(albumName)}&filename=${encodeURIComponent(currentFile)}`)
                    .then(r=>r.json()).then(d => {
                        markedState[currentFile] = d.is_marked;
                        if(curIdx === photos.findIndex(p => p.filename === currentFile)) {
                            renderMark(d.is_marked);
                        }
                    });
            }
        }

        function next(e) { 
            if(e) e.stopPropagation(); 
            if(curIdx < photos.length - 1) { 
                curIdx++; 
                loadPhoto(); 
            }
        }

        function prev(e) { 
            if(e) e.stopPropagation(); 
            if(curIdx > 0) { 
                curIdx--; 
                loadPhoto(); 
            }
        }

        function toggleOriginal(e) {
            e.stopPropagation();
            // 如果是 RAW 文件，直接忽略点击（虽然 CSS 已经禁用了 pointer-events，这里做双重保险）
            if (photos[curIdx].is_raw) return;

            const isNowOriginal = !isOrig;
            isOrig = isNowOriginal;
            updateOrigUI();

            vImg.style.opacity = 0.5;

            if (isOrig) {
                showLoading(true); 
                const tempImg = new Image();
                tempImg.onload = () => {
                    showLoading(false); 
                    vImg.src = tempImg.src;
                    vImg.style.opacity = 1;
                };
                tempImg.onerror = () => {
                    showLoading(false); 
                    alert('加载原图失败或文件不存在。');
                    vImg.style.opacity = 1; 
                };
                tempImg.src = photos[curIdx].original; 
            } else {
                showLoading(false); 
                vImg.src = photos[curIdx].preview;
                vImg.style.opacity = 1;
            }
        }

        function updateOrigUI() {
            // [新增] 检查当前图片是否为 RAW
            const isRaw = photos[curIdx].is_raw;

            if (isRaw) {
                // 如果是 RAW，禁用按钮并变灰
                origBtn.classList.add('disabled');
                origBtn.classList.remove('hd-active');
            } else {
                // 如果是普通图片，启用按钮
                origBtn.classList.remove('disabled');
                // 根据是否处于查看原图模式，切换高亮颜色
                if(isOrig) origBtn.classList.add('hd-active');
                else origBtn.classList.remove('hd-active');
            }
        }

        function toggleMark(e) {
            e.stopPropagation();
            const currentFile = photos[curIdx].filename;
            const nextState = !markedState[currentFile];

            markedState[currentFile] = nextState;
            renderMark(nextState);

            fetch('/api/toggle_mark', {
                method:'POST', headers:{'Content-Type':'application/json'},
                body:JSON.stringify({album:albumName, filename:currentFile})
            }).then(r=>r.json()).then(d => {
                if(!d.success) {
                    markedState[currentFile] = !nextState; 
                    renderMark(markedState[currentFile]);
                    alert('收藏操作失败，请检查网络。');
                }
            }).catch(() => {
                markedState[currentFile] = !nextState; 
                renderMark(markedState[currentFile]);
                alert('网络连接错误。');
            });
        }

        function renderMark(isMarked) {
            markIcon.innerHTML = isMarked ? ICONS.fill : ICONS.empty;
            if(isMarked) markBtn.classList.add('active');
            else markBtn.classList.remove('active');
        }
    </script>
</body>
</html>
'''

HOME_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>私有相册</title>
<style>''' + CSS_STYLE + '''</style>
</head>
<body>
    <div class="card-container">
        <div class="card">
            <h2>🔐 私有相册</h2>
            <form action="/check_album">
                <input name="name" placeholder="请输入相册文件夹名称" autocomplete="off" style="max-width: 280px;">
                <button>进入相册</button>
            </form>
        </div>
    </div>
</body>
</html>
'''


# ====== 3. Flask 路由 (不变) ======
@app.route('/')
def home(): return render_template_string(HOME_TEMPLATE)


@app.route('/check_album')
def check_album():
    name = request.args.get('name', '').strip()
    if name == state.marked_subdir: return "禁止访问", 403
    return render_template_string("<script>window.location.href='/album/'+encodeURIComponent('{{n}}')</script>", n=name)


@app.route('/album/<path:album_name>')
def album_view(album_name):
    # 🔒 禁止访问特殊系统文件夹
    if album_name == state.marked_subdir or album_name == state.preview_subdir:
        return "⛔ 禁止访问系统缓存文件夹", 403

    path = safe_join(state.base_dir, album_name)
    if not path or not path.exists():
        return "相册不存在", 404

    # 额外检查：解析后的路径是否指向预览或标记目录
    try:
        rel_path = path.relative_to(Path(state.base_dir).resolve())
        if rel_path.parts and (rel_path.parts[0] == state.marked_subdir or rel_path.parts[0] == state.preview_subdir):
            return "⛔ 禁止访问系统文件夹", 403
    except ValueError:
        pass  # 路径不在 base_dir 下，后续 404 处理

    photos = []
    for f in path.rglob("*"):
        if f.is_file() and f.suffix.lower() in state.allowed_extensions:
            # 双重保险：跳过任何包含系统目录的文件
            if state.marked_subdir in f.parts or state.preview_subdir in f.parts:
                continue
            try:
                rel = f.relative_to(path).as_posix()

                # [新增] 判断是否为 RAW 文件
                is_raw_file = f.suffix.lower() in state.raw_extensions

                photos.append({
                    'filename': rel,
                    'preview': url_for('get_preview', album=album_name, filename=rel),
                    'original': url_for('get_original', album=album_name, filename=rel),
                    'is_raw': is_raw_file  # 将此标记传递给前端
                })
            except:
                continue
    return render_template_string(ALBUM_TEMPLATE, album_name=album_name, photos=photos)


@app.route('/file/preview/<path:album>/<path:filename>')
@app.route('/file/preview/<path:album>/<path:filename>')
def get_preview(album, filename):
    # 原始文件的完整路径 (state.base_dir / album / filename)
    original_path = safe_join(state.base_dir, album, filename)
    if not original_path or not original_path.exists():
        abort(404)

    # 计算预览文件的完整路径
    # 预览路径 = 根目录 / 预览子目录 / album / filename
    # 注意：Path(state.base_dir) / state.preview_subdir 是预览缓存的根目录
    # album/filename 是相对于共享根目录的路径部分
    preview_path = safe_join(str(Path(state.base_dir) / state.preview_subdir), album, filename)

    if not preview_path: abort(404)

    # 检查预览文件是否存在
    if not preview_path.exists():
        # 如果不存在，则生成它
        success = generator.generate_sync(original_path, preview_path)
        if not success:
            # 如果生成失败，直接返回原图，但不返回原图的 mime-type
            # 这是一个简单的降级策略，虽然返回原图，但文件路径仍是 /file/preview/...
            return send_file(original_path)

    return send_file(preview_path)


@app.route('/file/original/<path:album>/<path:filename>')
def get_original(album, filename):
    path = safe_join(state.base_dir, album, filename)
    if not path or not path.exists(): abort(404)
    return send_file(path)


@app.route('/api/check_mark')
def check_mark():
    p = safe_join(state.base_dir, state.marked_subdir, request.args.get('album'), request.args.get('filename'))
    return jsonify({'is_marked': p and p.exists()})


@app.route('/api/toggle_mark', methods=['POST'])
def toggle_mark():
    d = request.json
    src = safe_join(state.base_dir, d['album'], d['filename'])
    dst = safe_join(state.base_dir, state.marked_subdir, d['album'], d['filename'])
    if not src or not src.exists(): return jsonify({'success': False})
    try:
        if dst.exists():
            os.remove(dst)
            update_global_status(f"🗑️ 取消: {Path(d['filename']).name}")
            return jsonify({'success': True, 'is_marked': False})
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            update_global_status(f"⭐ 标记: {Path(d['filename']).name}")
            return jsonify({'success': True, 'is_marked': True})
    except Exception as e:
        return jsonify({'success': False})


# ====== 4. Tkinter GUI (新增帮助按钮) ======
class ServerGUI:
    def __init__(self, root):
        self.root = root
        global gui_app
        gui_app = self
        self.timer = None

        self.style = {
            'bg': '#1E1E1E',
            'panel': '#252526',
            'input': '#333333',
            'fg': '#CCCCCC',
            'text': '#FFFFFF',
            'accent': '#3794FF',
            'success': '#4EC9B0'
        }

        root.title("IPv6 Photo Server")
        root.geometry("480x560")
        root.configure(bg=self.style['bg'])

        style = ttk.Style()
        style.theme_use('clam')
        style.configure('TButton', font=('Segoe UI', 10), borderwidth=0)

        header = tk.Frame(root, bg=self.style['bg'], pady=25)
        header.pack(fill='x')
        tk.Label(header, text="IPv6 相册服务", bg=self.style['bg'], fg=self.style['text'],
                 font=("Microsoft YaHei UI", 18, "bold")).pack()
        tk.Label(header, text="极速预览 · 智能缓存 · 安全访问", bg=self.style['bg'], fg=self.style['accent'],
                 font=("Microsoft YaHei UI", 10)).pack(pady=(5, 0))

        card = tk.Frame(root, bg=self.style['panel'], padx=25, pady=25)
        card.pack(fill='both', expand=True, padx=20, pady=(0, 20))

        self.create_label(card, "📂 相册根目录")
        path_box = tk.Frame(card, bg=self.style['panel'])
        path_box.pack(fill='x', pady=(5, 20))

        self.path_var = tk.StringVar(value=state.base_dir)
        e = tk.Entry(path_box, textvariable=self.path_var, bg=self.style['input'], fg='white',
                     relief='flat', font=("Segoe UI", 10))
        e.pack(side='left', fill='x', expand=True, ipady=8, padx=(0, 10))

        btn_browse = tk.Button(path_box, text="选择", command=self.browse,
                               bg=self.style['input'], fg='white', relief='flat', font=('Segoe UI', 9))
        btn_browse.pack(side='right', ipady=4, padx=0)

        self.create_label(card, "🌐 公网访问地址")
        self.ip_frame = tk.Frame(card, bg=self.style['panel'])
        self.ip_frame.pack(fill='x', pady=(5, 10))

        btn_frame = tk.Frame(card, bg=self.style['panel'])
        btn_frame.pack(fill='x', pady=10)

        # 刷新按钮
        tk.Button(btn_frame, text="🔄 刷新网络状态", command=self.refresh,
                  bg=self.style['accent'], fg='white', relief='flat', font=("Microsoft YaHei UI", 10, "bold")
                  ).pack(side='left', fill='x', expand=True, ipady=6, padx=(0, 5))

        # 新增：帮助与提示按钮
        tk.Button(btn_frame, text="❓ 帮助与提示", command=self.show_help,
                  bg=self.style['input'], fg='white', relief='flat', font=("Microsoft YaHei UI", 10)
                  ).pack(side='left', fill='x', expand=True, ipady=6, padx=(5, 0))

        tk.Label(card, text="运行日志", bg=self.style['panel'], fg='#666', font=("Segoe UI", 9)).pack(anchor='w',
                                                                                                      pady=(15, 5))
        self.status_var = tk.StringVar(value="正在初始化...")
        self.status_lbl = tk.Label(card, textvariable=self.status_var, bg=self.style['input'], fg=self.style['success'],
                                   anchor='w', padx=10, font=("Segoe UI", 9))
        self.status_lbl.pack(fill='x', ipady=8)

        self.refresh()
        threading.Thread(target=app.run, kwargs={'host': '::', 'port': 5000, 'debug': False, 'use_reloader': False},
                         daemon=True).start()
        threading.Thread(target=lambda: generator.scan_all(Path(state.base_dir)), daemon=True).start()

    def create_label(self, parent, text):
        tk.Label(parent, text=text, bg=self.style['panel'], fg=self.style['fg'],
                 font=("Microsoft YaHei UI", 10, "bold")).pack(anchor='w')

    def update_status(self, msg):
        self.root.after(0, lambda: self._upd(msg))

    def _upd(self, msg):
        if self.timer: self.root.after_cancel(self.timer)
        self.status_var.set(msg)
        self.status_lbl.config(fg=self.style['success'])
        self.timer = self.root.after(5000, lambda: [
            self.status_var.set("✅ 服务运行中 (等待连接)"),
            self.status_lbl.config(fg='#888')
        ])

    def browse(self):
        p = filedialog.askdirectory(initialdir=self.path_var.get())
        if p:
            self.path_var.set(p)
            state.base_dir = p
            self.refresh()
            threading.Thread(target=lambda: generator.scan_all(Path(p)), daemon=True).start()

    def copy_ip(self, event):
        try:
            txt = self.ip_text.get("1.0", tk.END).strip()
            url = txt.split('\n')[0].split(' ')[-1] if 'http' in txt else txt
            self.root.clipboard_clear()
            self.root.clipboard_append(url)
            messagebox.showinfo("提示", "地址已复制到剪贴板")
        except:
            pass

    def refresh(self):
        ipv6_addrs = get_ipv6_addresses_v2()[:5]  # 最多取前5个
        # 清空旧的地址显示
        for widget in self.ip_frame.winfo_children():
            widget.destroy()

        if ipv6_addrs:
            tk.Label(self.ip_frame, text="点击以下任意地址复制完整链接：", bg=self.style['panel'],
                     fg=self.style['fg'], font=("Segoe UI", 9)).pack(anchor='w', pady=(0, 5))
            for ip in ipv6_addrs:
                url = f"http://[{ip}]:{state.port}"
                lbl = tk.Label(
                    self.ip_frame,
                    text=url,
                    bg=self.style['input'],
                    fg=self.style['accent'],
                    relief='flat',
                    font=("Consolas", 10),
                    padx=10,
                    pady=5,
                    cursor="hand2",  # 手型光标
                    anchor="w"
                )
                lbl.pack(fill='x', pady=2)
                # 绑定点击复制事件，使用 lambda 闭包捕获当前 ip
                lbl.bind("<Button-1>", lambda e, u=url: self.copy_single_ip(u))
            self.update_status(f"🌐 检测到 {len(ipv6_addrs)} 个公网 IPv6 地址")
        else:
            lbl = tk.Label(
                self.ip_frame,
                text="⚠️ 未检测到 IPv6 地址，请检查网络设置。",
                bg=self.style['input'],
                fg='#FF6B6B',
                font=("Segoe UI", 10),
                padx=10,
                pady=8,
                anchor="w"
            )
            lbl.pack(fill='x')
            self.update_status("⚠️ 网络检测失败")

    def copy_single_ip(self, url):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(url)
            messagebox.showinfo("已复制", f"已复制地址到剪贴板：\n{url}", parent=self.root)
        except Exception as e:
            messagebox.showerror("错误", f"复制失败：{e}", parent=self.root)

    def show_help(self):
        help_message = """
【使用教程】
1. 设置根目录: 点击“选择”按钮，指定您要共享的大文件夹作为相册根目录。
2. 刷新地址: 确保底部状态显示“检测到 IPv6 地址”。
3. 访问相册: 复制上方显示的 `http://[...]` 地址，在手机或电脑浏览器中访问。
4. 输入相册名: 在网页输入框中输入根目录下的子文件夹名（即相册名）即可访问。

【文件夹格式要求】
- 根目录: 存放所有相册子文件夹的主目录（如：F:\\共享照片）。
- 相册子文件夹: 根目录下包含图片的子文件夹（如：F:\\共享照片\\2025年旅行）。
- 预览缓存: 程序会自动创建 `._preview_ipv6_opt` 文件夹用于存放缩略图缓存，请勿删除。
- 收藏照片: 收藏的照片副本会保存在 `被标记的照片` 文件夹内。

【网络安全风险提示】
- 本服务默认使用 IPv6 地址和 5000 端口。如果您的网络允许公网访问（例如，许多家庭宽带自动支持 IPv6 公网），则任何知道您地址的人都可以访问。
- 重要: 请确保您选择的“相册根目录”下只存放您想要共享的照片。
- 本程序目前没有访问密码，安全性依赖于 IPv6 地址的随机性和复杂性。请谨慎分享您的地址。
        """
        messagebox.showinfo("帮助与网络风险提示", help_message)


if __name__ == '__main__':
    root = tk.Tk()
    ServerGUI(root)
    root.mainloop()